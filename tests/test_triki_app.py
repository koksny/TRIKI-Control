import json
import tempfile
import threading
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from triki_control.actions import ActionBinding, ActionExecutor, ActionStep, TrikiConfig
from triki_control.app import (
    APP_CREATOR,
    APP_LICENSE,
    APP_VERSION,
    APP_WEBSITE,
    AppHttpServer,
    AppSession,
    TrayController,
    build_about_payload,
    browser_url_for,
    build_debug_html,
    build_html,
    handle_control,
    parse_args,
    post_control_action,
    run_webview_window,
    write_console_line,
)
from triki_control.calibration_server import ConnectionControl, EventBus
from triki_control.classifier import GesturePrediction, MotionFeatures
from triki_control.key_emitter import NullKeyEmitter
from triki_control.key_emitter import KeyEmissionError


def prediction(label: str) -> GesturePrediction:
    return GesturePrediction(
        label=label,
        confidence=0.88,
        reason="test prediction",
        features=MotionFeatures(
            sample_count=20,
            duration_seconds=0.5,
            gyro_p90=1000.0,
            gyro_p99=2000.0,
            accel_deviation_p99=300.0,
            accel_delta=400.0,
            orientation_angle_degrees=5.0,
            c_mean=700.0,
            c_positive_fraction=0.6,
            c_negative_fraction=0.0,
            c_sign_runs=1,
            c_sequence="+",
            gyro_peak_count=1,
            accel_peak_count=0,
            c_abs_p99=2200.0,
            lateral_gyro_p99=1200.0,
            lateral_accel_p99=800.0,
        ),
    )


class TrikiAppTests(unittest.TestCase):
    def test_app_session_executes_configured_action_when_output_enabled(self):
        emitter = NullKeyEmitter()
        session = AppSession(
            config=TrikiConfig(
                actions={"rotate-cw": ActionBinding.key("volume-up")},
                output_enabled=True,
            ),
            executor=ActionExecutor(key_emitter=emitter),
        )

        event = session.record_prediction(1.0, prediction("rotate-cw"))

        self.assertTrue(event["action_emitted"])
        self.assertEqual(event["action_description"], "volume-up")
        self.assertEqual(emitter.pressed, ["volume-up"])
        self.assertEqual(session.snapshot()["action_count"], 1)

    def test_app_session_persists_mapping_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "triki.json"
            session = AppSession(config_path=config_path, executor=ActionExecutor(key_emitter=NullKeyEmitter()))

            state = session.update_action("back-forth", ActionBinding.macro((ActionStep.key("escape"), ActionStep.delay(50))))

            reloaded = AppSession(config_path=config_path, executor=ActionExecutor(key_emitter=NullKeyEmitter()))

        self.assertGreater(state["action_revision"], 0)
        self.assertEqual(reloaded.config.actions["back-forth"].type, "macro")

    def test_app_session_creates_switches_and_deletes_profiles(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        session.create_profile("Game")
        session.switch_profile("Game")
        state = session.update_action("rotate-cw", ActionBinding.key("d"))

        session.switch_profile("Default")
        default_state = session.snapshot()
        session.switch_profile("Game")
        game_state = session.snapshot()
        session.delete_profile("Game")
        final_state = session.snapshot()

        self.assertIn("Game", state["profiles"])
        self.assertEqual(default_state["active_profile"], "Default")
        self.assertEqual(default_state["actions"][0]["binding"]["key"], "right")
        self.assertEqual(game_state["active_profile"], "Game")
        self.assertEqual(game_state["actions"][0]["binding"]["key"], "d")
        self.assertEqual(final_state["active_profile"], "Default")
        self.assertNotIn("Game", final_state["profiles"])

    def test_app_session_exports_imports_and_resets_profiles(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        session.create_profile("Game")
        session.update_action("rotate-cw", ActionBinding.key("d"))
        exported = session.export_profiles()

        target = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        imported_state = target.import_profiles(exported)
        reset_state = target.reset_all_profiles()

        self.assertEqual(exported["active_profile"], "Game")
        self.assertEqual(exported["profiles"]["Game"]["rotate-cw"]["key"], "d")
        self.assertIn("Game", imported_state["profiles"])
        self.assertEqual(imported_state["active_profile"], "Game")
        self.assertEqual(imported_state["actions"][0]["binding"]["key"], "d")
        self.assertEqual(reset_state["active_profile"], "Default")
        self.assertEqual(reset_state["profiles"], ["Default", "WASD Game", "Media", "Presentation", "Which Sausage, Mate?"])
        self.assertEqual(reset_state["actions"][0]["binding"]["key"], "right")
        session.switch_profile("Media")
        media_state = session.reset_active_profile()
        self.assertEqual(media_state["active_profile"], "Media")
        self.assertEqual(media_state["actions"][0]["binding"]["key"], "volume-up")

    def test_handle_control_maps_key_and_macro_actions(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        bus = EventBus()
        control = ConnectionControl(manual_pairing=True)

        key_state = handle_control(
            session,
            "action",
            {"gesture_label": "lift", "action_type": "key", "key_name": "volume-down"},
            bus=bus,
            connection_control=control,
        )
        macro_state = handle_control(
            session,
            "action",
            {"gesture_label": "back-forth", "action_type": "macro", "macro_text": "escape, 50ms, enter"},
            bus=bus,
            connection_control=control,
        )

        actions = {item["gesture_label"]: item for item in macro_state["actions"]}
        self.assertEqual(actions["lift"]["binding"]["key"], "volume-down")
        self.assertEqual(actions["back-forth"]["binding"]["type"], "macro")
        self.assertGreaterEqual(macro_state["action_revision"], key_state["action_revision"])

    def test_handle_control_test_key_reports_output_backend_error(self):
        class FailingEmitter:
            def press_key(self, key_name):
                raise KeyEmissionError(f"cannot emit {key_name}")

        session = AppSession(executor=ActionExecutor(key_emitter=FailingEmitter()))
        bus = EventBus()
        control = ConnectionControl(manual_pairing=True)

        state = handle_control(
            session,
            "test-key",
            {"key": "right"},
            bus=bus,
            connection_control=control,
        )

        self.assertIn("output error", state["message"])
        self.assertEqual(state["recent_events"][-1]["type"], "output-test")
        self.assertFalse(state["recent_events"][-1]["action_emitted"])
        self.assertIn("cannot emit right", state["recent_events"][-1]["output_reason"])

    def test_handle_control_manages_profiles(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        bus = EventBus()
        control = ConnectionControl(manual_pairing=True)

        created = handle_control(
            session,
            "profile",
            {"operation": "create", "name": "Game"},
            bus=bus,
            connection_control=control,
        )
        switched = handle_control(
            session,
            "profile",
            {"operation": "switch", "name": "Default"},
            bus=bus,
            connection_control=control,
        )
        reset_all = handle_control(
            session,
            "profile",
            {"operation": "reset-all"},
            bus=bus,
            connection_control=control,
        )

        self.assertIn("Game", created["profiles"])
        self.assertEqual(created["active_profile"], "Game")
        self.assertEqual(switched["active_profile"], "Default")
        self.assertEqual(reset_all["profiles"], ["Default", "WASD Game", "Media", "Presentation", "Which Sausage, Mate?"])

    def test_handle_control_imports_profiles(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        bus = EventBus()
        control = ConnectionControl(manual_pairing=True)

        state = handle_control(
            session,
            "profile",
            {
                "operation": "import",
                "data": {
                    "version": 2,
                    "active_profile": "Game",
                    "profiles": {
                        "Game": {
                            "rotate-cw": {"type": "key", "key": "d"},
                            "rotate-ccw": {"type": "key", "key": "a"},
                        }
                    },
                },
            },
            bus=bus,
            connection_control=control,
        )

        self.assertIn("Game", state["profiles"])
        self.assertEqual(state["active_profile"], "Game")
        self.assertEqual(state["actions"][0]["binding"]["key"], "d")

    def test_default_app_session_exposes_builtin_profiles(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))

        state = session.snapshot()

        self.assertEqual(state["active_profile"], "Default")
        self.assertEqual(state["profiles"], ["Default", "WASD Game", "Media", "Presentation", "Which Sausage, Mate?"])
        session.switch_profile("WASD Game")
        wasd_state = session.snapshot()
        self.assertEqual(wasd_state["actions"][0]["binding"]["key"], "d")
        wasd_actions = {item["gesture_label"]: item for item in wasd_state["actions"]}
        self.assertEqual(wasd_actions["scrub-cw"]["binding"]["key"], "w")
        self.assertEqual(wasd_actions["scrub-ccw"]["binding"]["key"], "s")
        session.switch_profile("Media")
        media_state = session.snapshot()
        self.assertEqual(media_state["actions"][0]["binding"]["key"], "volume-up")
        session.switch_profile("Which Sausage, Mate?")
        sausage_state = session.snapshot()
        sausage_actions = {item["gesture_label"]: item for item in sausage_state["actions"]}
        self.assertEqual(sausage_actions["scrub-cw"]["binding"]["key"], "=")
        self.assertEqual(sausage_actions["back-forth"]["binding"]["key"], "backspace")

    def test_app_session_exposes_battery_state(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))

        initial = session.snapshot()
        updated = session.set_battery_level(87)
        unavailable = session.set_battery_level(None, "Battery level unavailable: missing characteristic")

        self.assertEqual(initial["battery"]["status"], "unknown")
        self.assertIsNone(initial["battery"]["percent"])
        self.assertEqual(updated["battery"]["percent"], 87)
        self.assertEqual(updated["battery"]["status"], "ok")
        self.assertEqual(updated["battery"]["label"], "87%")
        self.assertIsNone(unavailable["battery"]["percent"])
        self.assertEqual(unavailable["battery"]["status"], "unavailable")
        self.assertEqual(unavailable["battery"]["label"], "Battery --")

    def test_app_session_snapshot_includes_release_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            session = AppSession(
                config_path=config_path,
                executor=ActionExecutor(key_emitter=NullKeyEmitter()),
            )

            state = session.snapshot()

        self.assertEqual(state["app_version"], APP_VERSION)
        self.assertEqual(state["config_path"], str(config_path))
        self.assertIn("Pair TRIKI", state["message"])

    def test_about_payload_exposes_version_config_and_docs(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            session = AppSession(
                config_path=config_path,
                executor=ActionExecutor(key_emitter=NullKeyEmitter()),
            )

            payload = build_about_payload(session)

        self.assertEqual(payload["app_name"], "TRIKI Control")
        self.assertEqual(payload["app_version"], APP_VERSION)
        self.assertEqual(payload["creator"], APP_CREATOR)
        self.assertEqual(payload["website"], APP_WEBSITE)
        self.assertEqual(payload["license"], APP_LICENSE)
        self.assertEqual(payload["config_path"], str(config_path))
        self.assertIn("README.md", payload["docs"])
        self.assertIn("CREDITS.md", payload["docs"])
        self.assertIn("LICENSE", payload["docs"])
        self.assertIn("docs/linux.md", payload["docs"])

    def test_pairing_control_auto_enables_output_for_end_user_flow(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        bus = EventBus()
        control = ConnectionControl(manual_pairing=True)

        state = handle_control(
            session,
            "pairing",
            {},
            bus=bus,
            connection_control=control,
        )

        self.assertEqual(state["status"], "pairing")
        self.assertTrue(state["output_enabled"])
        self.assertTrue(control.is_pairing_requested())

    def test_led_control_writes_hold_state_through_ble_bridge(self):
        class FakeBridge:
            def __init__(self):
                self.calls = []

            def set_led(self, enabled):
                self.calls.append(enabled)

        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        session.set_status("ready", "UART_READY")
        bus = EventBus()
        control = ConnectionControl(manual_pairing=True)
        bridge = FakeBridge()

        on_state = handle_control(
            session,
            "led",
            {"enabled": True},
            bus=bus,
            connection_control=control,
            command_bridge=bridge,
        )
        off_state = handle_control(
            session,
            "led",
            {"enabled": False},
            bus=bus,
            connection_control=control,
            command_bridge=bridge,
        )

        self.assertEqual(bridge.calls, [True, False])
        self.assertEqual(on_state["status"], "ready")
        self.assertIn("LED test on", on_state["message"])
        self.assertIn("LED test off", off_state["message"])

    def test_html_keeps_action_controls_stable_during_live_state_updates(self):
        html = build_html()

        self.assertIn("TRIKI Control", html)
        self.assertIn("let renderedActionRevision = null", html)
        self.assertIn("if (state.action_revision !== renderedActionRevision)", html)
        self.assertIn("renderActions()", html)
        self.assertIn("Volume Up", html)
        self.assertIn("macro-input", html)

    def test_html_exposes_profiles_and_key_recording_without_debug_link(self):
        html = build_html()

        self.assertIn('id="profile-select"', html)
        self.assertIn('id="new-profile-name"', html)
        self.assertIn("Record Key", html)
        self.assertIn("profileNames", html)
        self.assertNotIn('href="/debug"', html)

    def test_html_exposes_profile_import_export_controls(self):
        html = build_html()

        self.assertIn('id="export-profiles"', html)
        self.assertIn('id="import-profiles"', html)
        self.assertIn('id="import-profile-file"', html)
        self.assertIn('id="reset-all-profiles"', html)
        self.assertIn("operation: 'import'", html)
        self.assertIn("operation: 'reset-all'", html)
        self.assertIn("/profiles/export", html)

    def test_key_mapping_uses_real_select_with_media_options(self):
        html = build_html()

        self.assertIn('<select class="key-select"></select>', html)
        self.assertIn("keySelect.appendChild(option)", html)
        self.assertIn("Volume Up", html)
        self.assertIn("Volume Down", html)
        self.assertIn("Media Next", html)
        self.assertIn("Page Up", html)
        self.assertIn("Page Down", html)
        self.assertIn("Equals", html)
        self.assertNotIn('class="key-input" list="key-options"', html)
        self.assertNotIn("<datalist", html)

    def test_html_is_end_user_simple(self):
        html = build_html()

        self.assertIn("Pair TRIKI", html)
        self.assertIn("Action Mapping", html)
        self.assertNotIn("Enable output", html)
        self.assertNotIn("Clear events", html)
        self.assertNotIn("Samples", html)
        self.assertNotIn("Recent Output", html)
        self.assertNotIn("Connection</h2>", html)
        self.assertNotIn("output-toggle", html)

    def test_html_header_prioritizes_large_pair_button_and_moves_status_to_bottom(self):
        html = build_html()

        self.assertIn('class="app-header"', html)
        self.assertIn('id="app-title"', html)
        self.assertIn('id="battery-indicator"', html)
        self.assertIn('class="battery-icon"', html)
        self.assertIn('id="battery-fill"', html)
        self.assertIn("renderBattery(state.battery)", html)
        self.assertIn("pair-button", html)
        self.assertIn('class="status-footer"', html)
        self.assertLess(html.index('id="app-title"'), html.index('data-action="pairing"'))
        self.assertLess(html.index('data-action="pairing"'), html.index("Action Mapping"))
        self.assertLess(html.index("Action Mapping"), html.index('class="status-footer"'))
        self.assertIn('TRIKI Control</h1>', html)
        self.assertNotIn("TRIKI Control (waiting)", html)
        self.assertNotIn('id="status"', html)
        self.assertNotIn("status-badge", html)

    def test_pair_button_turns_green_when_ble_is_connected_or_ready(self):
        html = build_html()

        self.assertIn(".pair-button.connected", html)
        self.assertIn("const isConnected = state.status === 'connected' || state.status === 'ready';", html)
        self.assertIn("pairButton.classList.toggle('connected', isConnected);", html)
        self.assertIn("pairButton.textContent = isConnected ? 'Connected' : 'Pair TRIKI';", html)

    def test_html_exposes_press_and_hold_led_test_control(self):
        html = build_html()

        self.assertIn('id="led-test"', html)
        self.assertIn("ledButton.disabled = !isConnected;", html)
        self.assertIn("control('led', { enabled: true })", html)
        self.assertIn("control('led', { enabled: false })", html)
        self.assertIn("pointerdown", html)
        self.assertIn("pointerup", html)

    def test_html_exposes_compact_about_dialog(self):
        html = build_html()

        self.assertIn('id="about-button"', html)
        self.assertIn('id="about-dialog"', html)
        self.assertIn("TRIKI Control v", html)
        self.assertIn(APP_VERSION, html)
        self.assertIn(APP_CREATOR, html)
        self.assertIn("Koksny.com", html)
        self.assertIn(APP_LICENSE, html)
        self.assertIn("about-credits", html)
        self.assertIn("about-license", html)
        self.assertIn("fetch('/about')", html)
        self.assertIn("/about", html)
        header_start = html.index('<div class="header-actions">')
        header_end = html.index("</div>", header_start)
        profile_start = html.index('<div class="profile-controls">')
        profile_end = html.index("</div>", profile_start)
        self.assertIn('id="about-button"', html[header_start:header_end])
        self.assertNotIn('id="about-button"', html[profile_start:profile_end])

    def test_bottom_status_is_plain_text_not_a_panel_or_pill(self):
        html = build_html()

        footer_start = html.index('<section class="status-footer"')
        footer_end = html.index("</section>", footer_start)
        footer_html = html[footer_start:footer_end]

        self.assertIn('<p class="footer-left" id="message"', footer_html)
        self.assertIn('<p class="footer-right" id="hint"', footer_html)
        self.assertNotIn("pill", footer_html)
        self.assertNotIn("panel", footer_html)
        self.assertNotIn("span", footer_html)
        self.assertIn("display: flex", html)
        self.assertIn("justify-content: space-between", html)

    def test_app_shell_hides_page_scrollbar(self):
        html = build_html()

        self.assertIn("html, body {", html)
        self.assertIn("height: 100vh;", html)
        self.assertIn("overflow: hidden;", html)
        self.assertIn("main { max-width: 1020px;", html)

    def test_debug_page_renders_hidden_diagnostics(self):
        html = build_debug_html()

        self.assertIn("TRIKI Diagnostics", html)
        self.assertIn("connection_log", html)
        self.assertIn("recent_events", html)
        self.assertIn("EventSource('/events')", html)

    def test_parse_args_defaults_to_background_app_port(self):
        args = parse_args([])

        self.assertEqual(args.port, 8766)
        self.assertEqual(args.ui, "browser")
        self.assertFalse(args.output_enabled)
        self.assertEqual(args.window_seconds, 0.4)
        self.assertEqual(args.min_samples, 6)

    def test_desktop_launcher_can_default_to_webview_ui(self):
        args = parse_args([], default_ui="webview")

        self.assertEqual(args.ui, "webview")

    def test_legacy_browser_flags_map_to_ui_modes(self):
        args = parse_args(["--no-open-browser"])
        browser_args = parse_args(["--open-browser"], default_ui="webview")

        self.assertEqual(args.ui, "none")
        self.assertEqual(browser_args.ui, "browser")

    def test_explicit_ui_mode_overrides_default(self):
        args = parse_args(["--ui", "none"], default_ui="webview")

        self.assertEqual(args.ui, "none")

    def test_parse_args_accepts_log_path_for_windowed_diagnostics(self):
        args = parse_args(["--log-path", "output/windowed-smoke.log"])

        self.assertEqual(args.log_path, Path("output/windowed-smoke.log"))

    def test_browser_url_for_uses_loopback_for_wildcard_hosts(self):
        self.assertEqual(browser_url_for("0.0.0.0", 8765), "http://127.0.0.1:8765/")
        self.assertEqual(browser_url_for("::", 8765), "http://127.0.0.1:8765/")
        self.assertEqual(browser_url_for("localhost", 8765), "http://localhost:8765/")

    def test_console_write_is_safe_for_windowed_builds_without_stdout(self):
        stream = StringIO()

        write_console_line("OPEN http://127.0.0.1:8765/", stream=stream)
        write_console_line("OPEN http://127.0.0.1:8765/", stream=None)

        self.assertEqual(stream.getvalue(), "OPEN http://127.0.0.1:8765/\n")

    def test_run_webview_window_uses_embedded_desktop_window(self):
        class FakeWebview:
            def __init__(self):
                self.created = None
                self.started = False

            def create_window(self, title, url, **kwargs):
                self.created = {"title": title, "url": url, "kwargs": kwargs}

            def start(self):
                self.started = True

        fake = FakeWebview()

        run_webview_window("http://127.0.0.1:8765/", webview_module=fake)

        self.assertEqual(fake.created["title"], "TRIKI Control")
        self.assertEqual(fake.created["url"], "http://127.0.0.1:8765/")
        self.assertGreaterEqual(fake.created["kwargs"]["width"], 900)
        self.assertTrue(fake.started)

    def test_run_webview_window_locks_release_window_size(self):
        class FakeWebview:
            def __init__(self):
                self.created = None

            def create_window(self, title, url, **kwargs):
                self.created = {"title": title, "url": url, "kwargs": kwargs}
                return None

            def start(self):
                pass

        fake = FakeWebview()

        run_webview_window("http://127.0.0.1:8765/", webview_module=fake)

        self.assertFalse(fake.created["kwargs"]["resizable"])
        self.assertEqual(fake.created["kwargs"]["width"], 1020)
        self.assertEqual(fake.created["kwargs"]["height"], 820)

    def test_http_request_succeeds_without_stderr_for_windowed_builds(self):
        server = AppHttpServer(
            ("127.0.0.1", 0),
            AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter())),
            EventBus(),
            ConnectionControl(manual_pairing=True),
        )
        thread = threading.Thread(target=server.handle_request)
        thread.start()

        try:
            with patch("sys.stderr", None):
                body = urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=2).read().decode("utf-8")
        finally:
            thread.join(timeout=2)
            server.server_close()

        self.assertIn("TRIKI Control", body)

    def test_http_debug_route_serves_diagnostics_page(self):
        server = AppHttpServer(
            ("127.0.0.1", 0),
            AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter())),
            EventBus(),
            ConnectionControl(manual_pairing=True),
        )
        thread = threading.Thread(target=server.handle_request)
        thread.start()

        try:
            body = urlopen(f"http://127.0.0.1:{server.server_port}/debug", timeout=2).read().decode("utf-8")
        finally:
            thread.join(timeout=2)
            server.server_close()

        self.assertIn("TRIKI Diagnostics", body)

    def test_http_diagnostics_route_serves_environment_json(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        server = AppHttpServer(
            ("127.0.0.1", 0),
            session,
            EventBus(),
            ConnectionControl(manual_pairing=True),
        )
        thread = threading.Thread(target=server.handle_request)
        thread.start()

        try:
            response = urlopen(f"http://127.0.0.1:{server.server_port}/diagnostics", timeout=2)
            body = json.loads(response.read().decode("utf-8"))
        finally:
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(body["app_name"], "TRIKI Control")
        self.assertIn("platform", body)
        self.assertIn("modules", body)
        self.assertIn("uinput", body)

    def test_http_about_route_serves_release_metadata(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        server = AppHttpServer(
            ("127.0.0.1", 0),
            session,
            EventBus(),
            ConnectionControl(manual_pairing=True),
        )
        thread = threading.Thread(target=server.handle_request)
        thread.start()

        try:
            response = urlopen(f"http://127.0.0.1:{server.server_port}/about", timeout=2)
            body = json.loads(response.read().decode("utf-8"))
        finally:
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(body["app_name"], "TRIKI Control")
        self.assertEqual(body["app_version"], APP_VERSION)
        self.assertEqual(body["creator"], APP_CREATOR)
        self.assertEqual(body["website"], APP_WEBSITE)
        self.assertEqual(body["license"], APP_LICENSE)
        self.assertIn("README.md", body["docs"])
        self.assertIn("CREDITS.md", body["docs"])
        self.assertIn("LICENSE", body["docs"])
        self.assertIn("docs/linux.md", body["docs"])

    def test_http_profile_export_route_serves_json_download(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        session.create_profile("Game")
        server = AppHttpServer(
            ("127.0.0.1", 0),
            session,
            EventBus(),
            ConnectionControl(manual_pairing=True),
        )
        thread = threading.Thread(target=server.handle_request)
        thread.start()

        try:
            response = urlopen(f"http://127.0.0.1:{server.server_port}/profiles/export", timeout=2)
            body = response.read().decode("utf-8")
        finally:
            thread.join(timeout=2)
            server.server_close()

        payload = json.loads(body)
        self.assertEqual(response.headers["Content-Type"], "application/json; charset=utf-8")
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertEqual(payload["active_profile"], "Game")
        self.assertIn("Game", payload["profiles"])
        self.assertIn("Media", payload["profiles"])

    def test_tray_controller_hides_window_on_close_and_can_reopen_or_quit(self):
        class FakeEvent:
            def __init__(self):
                self.handlers = []

            def __iadd__(self, handler):
                self.handlers.append(handler)
                return self

        class FakeWindow:
            def __init__(self):
                self.events = type("Events", (), {"closing": FakeEvent()})()
                self.hidden = 0
                self.shown = 0
                self.destroyed = 0

            def hide(self):
                self.hidden += 1

            def show(self):
                self.shown += 1

            def destroy(self):
                self.destroyed += 1

        window = FakeWindow()
        stopped = []
        controller = TrayController(window, on_quit=lambda: stopped.append(True))
        controller.attach_close_handler()

        should_close = window.events.closing.handlers[0]()
        controller.open_window()
        controller.quit()

        self.assertFalse(should_close)
        self.assertEqual(window.hidden, 1)
        self.assertEqual(window.shown, 1)
        self.assertEqual(window.destroyed, 1)
        self.assertEqual(stopped, [True])

    def test_tray_controller_can_request_pairing_and_open_diagnostics(self):
        class FakeWindow:
            def __init__(self):
                self.loaded = []
                self.shown = 0

            def load_url(self, url):
                self.loaded.append(url)

            def show(self):
                self.shown += 1

        requests = []

        def opener(request, timeout):
            requests.append(
                {
                    "url": request.full_url,
                    "method": request.get_method(),
                    "body": request.data.decode("utf-8"),
                    "timeout": timeout,
                }
            )

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self):
                    return b"{}"

            return Response()

        window = FakeWindow()
        controller = TrayController(window, url="http://127.0.0.1:8765/", opener=opener)

        controller.request_pairing()
        controller.open_diagnostics()

        self.assertEqual(requests[0]["url"], "http://127.0.0.1:8765/control?action=pairing")
        self.assertEqual(requests[0]["method"], "POST")
        self.assertEqual(requests[0]["body"], "{}")
        self.assertEqual(window.loaded, ["http://127.0.0.1:8765/debug"])
        self.assertEqual(window.shown, 1)

    def test_tray_menu_contains_pair_and_diagnostics_items(self):
        class FakeEvent:
            def __iadd__(self, handler):
                return self

        class FakeWindow:
            def __init__(self):
                self.events = type("Events", (), {"closing": FakeEvent()})()

        class FakeIcon:
            def __init__(self, name, image, title, menu):
                self.name = name
                self.menu = menu
                self.detached = False

            def run_detached(self):
                self.detached = True

        class FakePystray:
            Icon = FakeIcon

            @staticmethod
            def Menu(*items):
                return list(items)

            @staticmethod
            def MenuItem(text, action, default=False):
                return {"text": text, "action": action, "default": default}

        class FakeImage:
            @staticmethod
            def new(*args):
                return object()

        class FakeDraw:
            @staticmethod
            def Draw(image):
                class Drawer:
                    def ellipse(self, *args, **kwargs):
                        pass

                    def text(self, *args, **kwargs):
                        pass

                return Drawer()

        controller = TrayController(
            FakeWindow(),
            url="http://127.0.0.1:8765/",
            pystray_module=FakePystray,
            image_module=FakeImage,
            image_draw_module=FakeDraw,
        )

        self.assertTrue(controller.start())
        labels = [item["text"] for item in controller.icon.menu]
        self.assertEqual(
            labels,
            ["Open TRIKI Control", "Pair TRIKI", "Diagnostics", "Quit"],
        )
        self.assertTrue(controller.icon.detached)

    def test_post_control_action_posts_json_to_control_endpoint(self):
        requests = []

        def opener(request, timeout):
            requests.append(
                {
                    "url": request.full_url,
                    "method": request.get_method(),
                    "body": request.data.decode("utf-8"),
                    "content_type": request.headers["Content-type"],
                    "timeout": timeout,
                }
            )

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self):
                    return b'{"ok": true}'

            return Response()

        response = post_control_action(
            "http://127.0.0.1:8765/",
            "pairing",
            opener=opener,
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(requests[0]["url"], "http://127.0.0.1:8765/control?action=pairing")
        self.assertEqual(requests[0]["method"], "POST")
        self.assertEqual(requests[0]["body"], "{}")
        self.assertEqual(requests[0]["content_type"], "application/json")


if __name__ == "__main__":
    unittest.main()
