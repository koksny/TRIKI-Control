import json
import math
import os
import socket
import tempfile
import threading
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import triki_app
from triki_actions import (
    CONFIG_VERSION,
    ENGINE_MOTION,
    MOTION_LABELS,
    ActionBinding,
    ActionExecutor,
    ActionStep,
    MotionProfileSettings,
    TrikiConfig,
)
from triki_app import (
    APP_CREATOR,
    APP_LICENSE,
    APP_VERSION,
    APP_WEBSITE,
    DEFAULT_MOTION_HOLD_MS,
    AppHttpServer,
    AppSession,
    TrayController,
    binding_from_payload,
    build_about_payload,
    build_detector,
    browser_url_for,
    build_debug_html,
    build_engine_router,
    build_html,
    create_tray_image,
    display_name_for_label,
    handle_control,
    ProfileEngineRouter,
    is_allowed_control_origin,
    is_loopback_host,
    main,
    parse_args,
    post_control_action,
    run_webview_window,
    write_console_line,
)
from triki_calibration_server import ConnectionControl, EventBus
from triki_classifier import GesturePrediction, MotionFeatures
from triki_live import LiveGestureDetector
from triki_motion_engine import (
    FIRE_LABEL,
    MOVE_BACKWARD_LABEL,
    MOVE_FORWARD_LABEL,
    MOVE_STRAFE_LEFT_LABEL,
    MOVE_STRAFE_RIGHT_LABEL,
    TURN_LEFT_LABEL,
    TURN_RIGHT_LABEL,
    MotionControlEngine,
)
from triki_protocol import MotionSample
from triki_key_emitter import NullKeyEmitter
from triki_key_emitter import HoldKeyEmitter
from triki_key_emitter import KeyEmissionError

G = 2050.0


def synth_lean_hold_samples(dt=0.02, deg=25.0, hold_s=1.0, ramp_s=0.30):
    """A deliberate forward lean (rest -> ramp -> hold) as MotionSamples.

    Mirrors the proven FIRING lean from tests/test_triki_motion_engine.py: at
    rest gravity reads (0,0,-G); the cap rocks onto an edge so gravity rotates
    off -z toward +y. Fed through MotionControlEngine this drives MOVE-forward
    after the hold, with no jitter so it is deterministic.
    """
    samples = []
    t = 0.0
    for _ in range(int(1.5 / dt)):
        samples.append((t, MotionSample(packet_id=0, values=(0, 0, 0, 0, 0, -int(G)))))
        t += dt
    th = math.radians(deg)
    ramp = int(ramp_s / dt)
    for k in range(ramp):
        a = th * (k + 1) / ramp
        dth = th / ramp_s
        samples.append(
            (
                t,
                MotionSample(
                    packet_id=0,
                    values=(int(dth * 150.0), 0, 0, 0, int(G * math.sin(a)), int(-G * math.cos(a))),
                ),
            )
        )
        t += dt
    for _ in range(int(hold_s / dt)):
        samples.append(
            (
                t,
                MotionSample(packet_id=0, values=(0, 0, 0, 0, int(G * math.sin(th)), int(-G * math.cos(th)))),
            )
        )
        t += dt
    return samples


def synth_twist_samples(rate=2600, n=80, dt=0.02):
    """Cap rests, then spins about its own (down) axis at a steady yaw rate -- a
    TWIST. Yaw-dominated with no f-axis impulse, so the engine emits a TURN and
    NEVER a FIRE. Returns MotionSamples (no calibration involved)."""
    seq = []
    t = 0.0
    for _ in range(int(1.5 / dt)):
        seq.append((t, (0, 0, 0, 0, 0, -int(G))))
        t += dt
    for _ in range(n):
        seq.append((t, (0, 0, int(rate), 0, 0, -int(G))))
        t += dt
    return [(tt, MotionSample(packet_id=0, values=tuple(int(x) for x in v))) for tt, v in seq]


# Real labeled session log with hand-performed stamps -- the same recording the
# engine-level stamp-fire tests validate against. Used here for the end-to-end
# STAMP -> FIRE -> Ctrl app assertion. Skipped if the log is not on this machine.
REAL_STAMP_LOG = os.path.join(
    os.path.expanduser("~"),
    "AppData", "Roaming", "TRIKI", "sessions", "session-20260605-030830.jsonl",
)
RUN_REAL_LOG_TESTS = os.environ.get("TRIKI_REAL_LOG_TESTS") == "1"


def _load_gesture_rows(path):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("type") == "gesture" and "values" in row:
                rows.append(row)
    return rows


def _stamp_run_starts(rows, gap=10):
    """Indices where a labeled 'lift' run begins (>= gap samples since the last)."""
    idx = [i for i, r in enumerate(rows) if r.get("label") == "lift"]
    if not idx:
        return []
    starts = [idx[0]]
    for k in range(1, len(idx)):
        if idx[k] - idx[k - 1] > gap:
            starts.append(idx[k])
    return starts


def _slice_samples(rows, start):
    """A +/-25-sample window around a run start, re-based to ~0.06s so the engine's
    rolling fire window fills naturally. Returns [(t, MotionSample)]."""
    sl = rows[max(0, start - 25): start + 25]
    t0 = sl[0]["t"]
    return [
        (r["t"] - t0 + 0.06, MotionSample(packet_id=0, values=tuple(int(x) for x in r["values"])))
        for r in sl
    ]


def _real_stamp_slice():
    """A short slice of the real log around a labeled 'lift' (stamp) that the Motion
    engine actually classifies as FIRE -- scans the first dozen stamp runs and
    returns the first slice whose engine replay emits FIRE (the engine fires ~10/12
    real stamps; pinning to a confirmed-firing slice keeps the app test
    deterministic). Re-based for the rolling window. Empty if none fire (the
    skipUnless guard handles a missing log; an empty list fails loudly).
    Caller guards with skipUnless(REAL_STAMP_LOG)."""
    rows = _load_gesture_rows(REAL_STAMP_LOG)
    for s in _stamp_run_starts(rows)[:12]:
        samples = _slice_samples(rows, s)
        probe = MotionControlEngine()
        if any(
            (p := probe.add_sample(t, sample)) is not None and p.label == FIRE_LABEL
            for t, sample in samples
        ):
            return samples
    return []


def prediction(label: str, *, twist: float = 700.0) -> GesturePrediction:
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
            c_mean=twist,
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


class _RecordingBase:
    """Minimal base emitter for HoldKeyEmitter end-to-end tests.

    RecordingEmitter lives in tests.test_triki_key_emitter and is not imported
    here, so this local stand-in records key_down/key_up while leaving press_key
    a no-op (the hold path never taps). A lock keeps it safe against the hold
    worker thread, though tests release deterministically before asserting.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.downs = []
        self.ups = []

    def press_key(self, key_name):
        pass

    def key_down(self, key_name):
        with self._lock:
            self.downs.append(key_name)

    def key_up(self, key_name):
        with self._lock:
            self.ups.append(key_name)


class TrikiAppTests(unittest.TestCase):
    def test_app_session_executes_configured_action_when_output_enabled(self):
        emitter = NullKeyEmitter()
        session = AppSession(
            config=TrikiConfig(
                actions={"turn-right": ActionBinding.key("volume-up")},
                output_enabled=True,
            ),
            executor=ActionExecutor(key_emitter=emitter),
        )

        event = session.record_prediction(1.0, prediction("turn-right"))

        self.assertTrue(event["action_emitted"])
        self.assertEqual(event["action_description"], "volume-up")
        self.assertEqual(emitter.pressed, ["volume-up"])
        self.assertEqual(session.snapshot()["action_count"], 1)

    def test_disabling_output_releases_a_held_key(self):
        null = NullKeyEmitter()
        emitter = HoldKeyEmitter(null, hold_ms=400)
        session = AppSession(
            config=TrikiConfig(output_enabled=True),
            executor=ActionExecutor(key_emitter=emitter),
        )

        session.record_prediction(1.0, prediction("turn-right"))
        self.assertEqual(len(null.downs), 1)
        self.assertEqual(len(null.ups), 0)

        session.set_output_enabled(False)
        self.assertEqual(len(null.ups), 1)
        self.assertEqual(null.downs, null.ups)
        emitter.close()

    def test_connection_loss_disables_output_releases_keys_and_persists_off(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            null = NullKeyEmitter()
            emitter = HoldKeyEmitter(null, hold_ms=400)
            session = AppSession(
                config=TrikiConfig(output_enabled=True),
                config_path=config_path,
                executor=ActionExecutor(key_emitter=emitter),
            )
            session.set_status("ready", "UART_READY")
            session.record_prediction(1.0, prediction("turn-right"))

            state = session.set_status("disconnected", "TRIKI disconnected")
            reloaded = AppSession(
                config_path=config_path,
                executor=ActionExecutor(key_emitter=NullKeyEmitter()),
            )

        self.assertFalse(state["output_enabled"])
        self.assertEqual(null.downs, null.ups)
        self.assertFalse(reloaded.config.output_enabled)
        emitter.close()

    def test_switching_profile_releases_a_held_key(self):
        null = NullKeyEmitter()
        emitter = HoldKeyEmitter(null, hold_ms=400)
        session = AppSession(
            config=TrikiConfig(output_enabled=True),
            executor=ActionExecutor(key_emitter=emitter),
        )

        session.record_prediction(1.0, prediction("turn-right"))
        self.assertEqual(len(null.downs), 1)
        self.assertEqual(len(null.ups), 0)

        session.switch_profile("Music")
        self.assertEqual(len(null.ups), 1)
        self.assertEqual(null.downs, null.ups)
        emitter.close()

    def test_game_profile_holds_movement_key_through_executor(self):
        # The Game profile binds the tank GO control to 'w'; a sustained lean
        # re-emits GO every sample and the HoldKeyEmitter holds 'w' continuously,
        # releasing once when the intent stops.
        base = _RecordingBase()
        emitter = HoldKeyEmitter(base, hold_ms=400)
        session = AppSession(
            config=TrikiConfig(output_enabled=True),
            executor=ActionExecutor(key_emitter=emitter),
        )
        session.switch_profile("Game")

        for _ in range(3):
            session.record_prediction(1.0, prediction("go"))

        self.assertEqual(base.downs, ["w"])
        emitter.set_hold_ms(0)
        self.assertEqual(base.ups, ["w"])
        emitter.close()

    def test_create_profile_rejects_duplicate_name(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        session.create_profile("Arena")

        with self.assertRaisesRegex(ValueError, "profile already exists: Arena"):
            session.create_profile("Arena")
        with self.assertRaisesRegex(ValueError, "profile already exists: Arena"):
            session.create_profile("  Arena  ")
        # The two built-ins are also reserved.
        with self.assertRaisesRegex(ValueError, "profile already exists: Game"):
            session.create_profile("Game")

    def test_switch_profile_rejects_unknown_name(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))

        with self.assertRaisesRegex(ValueError, "unknown profile: Nope"):
            session.switch_profile("Nope")

    def test_delete_profile_rejects_unknown_name(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))

        with self.assertRaisesRegex(ValueError, "unknown profile: Nope"):
            session.delete_profile("Nope")

    def test_delete_profile_refuses_to_delete_the_last_profile(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        # Build single-profile state through the public API: merged_with_defaults
        # re-expands the built-ins, so the constructor cannot produce it.
        while len(session.config.profiles) > 1:
            active = session.config.active_profile
            victim = next(name for name in session.config.profiles if name != active)
            session.delete_profile(victim)

        self.assertEqual(len(session.config.profiles), 1)
        with self.assertRaisesRegex(ValueError, "cannot delete the last profile"):
            session.delete_profile(session.config.active_profile)

    def test_handle_control_action_requires_gesture_label(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        bus = EventBus()
        control = ConnectionControl(manual_pairing=True)

        with self.assertRaises(ValueError) as context:
            handle_control(
                session,
                "action",
                {"action_type": "key", "key_name": "a"},
                bus=bus,
                connection_control=control,
            )
        self.assertIn("gesture", str(context.exception))

    def test_binding_from_payload_requires_key_name_for_key_action(self):
        with self.assertRaises(ValueError) as context:
            binding_from_payload({"action_type": "key"})
        self.assertIn("key", str(context.exception))

    def test_handle_control_rejects_non_dict_payload(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        bus = EventBus()
        control = ConnectionControl(manual_pairing=True)

        with self.assertRaises(ValueError) as context:
            handle_control(
                session,
                "action",
                ["not", "a", "dict"],
                bus=bus,
                connection_control=control,
            )
        self.assertIn("JSON object", str(context.exception))

    def test_handle_control_rejects_unknown_action(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        bus = EventBus()
        control = ConnectionControl(manual_pairing=True)

        with self.assertRaisesRegex(ValueError, "unknown control action: frobnicate"):
            handle_control(session, "frobnicate", {}, bus=bus, connection_control=control)

    def test_handle_control_rejects_unknown_profile_operation(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        bus = EventBus()
        control = ConnectionControl(manual_pairing=True)

        with self.assertRaisesRegex(ValueError, "unknown profile operation: bogus"):
            handle_control(
                session,
                "profile",
                {"operation": "bogus"},
                bus=bus,
                connection_control=control,
            )

    def test_is_loopback_host_recognizes_loopback_addresses(self):
        for host in ("127.0.0.1", "::1", "localhost", ""):
            self.assertTrue(is_loopback_host(host), host)
        for host in ("0.0.0.0", "::", "192.168.1.5"):
            self.assertFalse(is_loopback_host(host), host)

    def test_control_origin_accepts_only_local_app_pages(self):
        for origin in (None, "http://127.0.0.1:8766", "http://localhost:8766", "https://[::1]:8766"):
            self.assertTrue(is_allowed_control_origin(origin), origin)
        for origin in ("https://example.com", "null", "file:///tmp/index.html"):
            self.assertFalse(is_allowed_control_origin(origin), origin)

    def test_app_session_persists_mapping_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "triki.json"
            session = AppSession(config_path=config_path, executor=ActionExecutor(key_emitter=NullKeyEmitter()))
            # Music uses the same Motion/Game action vocabulary, with media-key defaults.
            session.switch_profile("Music")

            state = session.update_action("stamp", ActionBinding.macro((ActionStep.key("escape"), ActionStep.delay(50))))

            reloaded = AppSession(config_path=config_path, executor=ActionExecutor(key_emitter=NullKeyEmitter()))

        self.assertGreater(state["action_revision"], 0)
        self.assertEqual(reloaded.config.actions["stamp"].type, "macro")

    def test_app_session_creates_switches_and_deletes_profiles(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        session.create_profile("Arena")
        session.switch_profile("Arena")
        state = session.update_action("turn-right", ActionBinding.key("d"))

        session.switch_profile("Game")
        game_state = session.snapshot()
        session.switch_profile("Arena")
        arena_state = session.snapshot()
        session.delete_profile("Arena")
        final_state = session.snapshot()

        self.assertIn("Arena", state["profiles"])
        self.assertEqual(game_state["active_profile"], "Game")
        # Game lists its first-class motion controls; turn-right -> right.
        game_actions = {a["gesture_label"]: a for a in game_state["actions"]}
        self.assertEqual(game_actions["turn-right"]["binding"]["key"], "right")
        self.assertEqual(arena_state["active_profile"], "Arena")
        # Arena is a custom profile with the same rows as Game; the edit persisted.
        arena_actions = {a["gesture_label"]: a for a in arena_state["actions"]}
        self.assertEqual(arena_actions["turn-right"]["binding"]["key"], "d")
        self.assertEqual(final_state["active_profile"], "Game")
        self.assertNotIn("Arena", final_state["profiles"])

    def test_app_session_exports_imports_and_resets_profiles(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        session.create_profile("Arena")
        session.update_action("turn-left", ActionBinding.key("d"))
        exported = session.export_profiles()

        target = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        imported_state = target.import_profiles(exported)
        reset_state = target.reset_all_profiles()

        self.assertEqual(exported["active_profile"], "Arena")
        self.assertEqual(exported["profiles"]["Arena"]["turn-left"]["key"], "d")
        self.assertIn("Arena", imported_state["profiles"])
        self.assertEqual(imported_state["active_profile"], "Arena")
        imported_actions = {item["gesture_label"]: item for item in imported_state["actions"]}
        self.assertEqual(imported_actions["turn-left"]["binding"]["key"], "d")
        # reset-all collapses back to the two built-ins, active Game (first-class
        # motion controls: turn-right -> right).
        self.assertEqual(reset_state["active_profile"], "Game")
        self.assertEqual(reset_state["profiles"], ["Game", "Music"])
        reset_actions = {a["gesture_label"]: a for a in reset_state["actions"]}
        self.assertEqual(reset_actions["turn-right"]["binding"]["key"], "right")
        session.switch_profile("Music")
        music_state = session.reset_active_profile()
        self.assertEqual(music_state["active_profile"], "Music")
        music_actions = {item["gesture_label"]: item for item in music_state["actions"]}
        self.assertEqual(music_actions["turn-left"]["binding"]["key"], "volume-down")

    def test_handle_control_maps_key_and_macro_actions(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        bus = EventBus()
        control = ConnectionControl(manual_pairing=True)
        # Music exposes the same Motion/Game action rows as Game, but starts on media keys.
        session.switch_profile("Music")

        key_state = handle_control(
            session,
            "action",
            {"gesture_label": "stamp", "action_type": "key", "key_name": "volume-down"},
            bus=bus,
            connection_control=control,
        )
        macro_state = handle_control(
            session,
            "action",
            {"gesture_label": "turn-left", "action_type": "macro", "macro_text": "escape, 50ms, enter"},
            bus=bus,
            connection_control=control,
        )

        actions = {item["gesture_label"]: item for item in macro_state["actions"]}
        self.assertEqual(actions["stamp"]["binding"]["key"], "volume-down")
        self.assertEqual(actions["turn-left"]["binding"]["type"], "macro")
        self.assertGreaterEqual(macro_state["action_revision"], key_state["action_revision"])

    def test_handle_control_sets_hold_ms_on_config_and_emitter(self):
        emitter = HoldKeyEmitter(NullKeyEmitter(), hold_ms=0)
        session = AppSession(executor=ActionExecutor(key_emitter=emitter))
        bus = EventBus()
        control = ConnectionControl(manual_pairing=True)

        state = handle_control(
            session,
            "hold",
            {"ms": 400},
            bus=bus,
            connection_control=control,
        )

        self.assertEqual(state["hold_ms"], 400)
        self.assertEqual(session.config.hold_ms, 400)
        self.assertEqual(emitter.hold_ms, 400)

        cleared = handle_control(
            session,
            "hold",
            {"ms": 0},
            bus=bus,
            connection_control=control,
        )
        self.assertEqual(cleared["hold_ms"], 0)
        self.assertEqual(emitter.hold_ms, 0)

    def test_mouse_speed_is_saved_and_applied_per_profile(self):
        emitter = HoldKeyEmitter(NullKeyEmitter(), hold_ms=0)
        session = AppSession(executor=ActionExecutor(key_emitter=emitter))
        bus = EventBus()
        control = ConnectionControl(manual_pairing=True)

        game = handle_control(
            session,
            "mouse-speed",
            {"value": 7},
            bus=bus,
            connection_control=control,
        )
        session.switch_profile("Music")
        music = handle_control(
            session,
            "mouse-speed",
            {"value": 30},
            bus=bus,
            connection_control=control,
        )
        session.switch_profile("Game")

        self.assertEqual(game["motion"]["mouse_speed"], 7)
        self.assertEqual(music["motion"]["mouse_speed"], 30)
        self.assertEqual(session.snapshot()["motion"]["mouse_speed"], 7)
        self.assertEqual(emitter.mouse_speed, 7)
        emitter.close()

    def test_continuous_mouse_axis_is_saved_per_profile(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        bus = EventBus()
        control = ConnectionControl(manual_pairing=True)

        game = handle_control(
            session,
            "mouse-axis",
            {"enabled": False},
            bus=bus,
            connection_control=control,
        )
        session.switch_profile("Music")
        music = handle_control(
            session,
            "mouse-axis",
            {"enabled": True},
            bus=bus,
            connection_control=control,
        )
        session.switch_profile("Game")

        self.assertFalse(game["motion"]["mouse_axis_enabled"])
        self.assertTrue(music["motion"]["mouse_axis_enabled"])
        self.assertFalse(session.snapshot()["motion"]["mouse_axis_enabled"])

    def test_continuous_mouse_axis_scales_pointer_distance_with_twist_speed(self):
        base = NullKeyEmitter()
        emitter = HoldKeyEmitter(base, hold_ms=DEFAULT_MOTION_HOLD_MS, mouse_speed=20)
        session = AppSession(
            config=TrikiConfig(
                actions={"turn-right": ActionBinding.key("mouse-move-right")},
                output_enabled=True,
                profile_settings={
                    "Game": MotionProfileSettings(
                        turn_threshold=1000.0,
                        mouse_speed=20,
                        mouse_axis_enabled=True,
                    ),
                },
            ),
            executor=ActionExecutor(key_emitter=emitter),
        )

        slow = session.record_prediction(1.0, prediction("turn-right", twist=1000.0))
        fast = session.record_prediction(1.1, prediction("turn-right", twist=3000.0))

        self.assertEqual(base.pointer_moves, [(2, 0), (20, 0)])
        self.assertLess(slow["mouse_axis_strength"], fast["mouse_axis_strength"])
        self.assertEqual(fast["mouse_axis_strength"], 1.0)
        emitter.close()

    def test_disabling_continuous_mouse_axis_restores_fixed_steps(self):
        base = NullKeyEmitter()
        emitter = HoldKeyEmitter(base, hold_ms=DEFAULT_MOTION_HOLD_MS, mouse_speed=20)
        session = AppSession(
            config=TrikiConfig(
                actions={"turn-right": ActionBinding.key("mouse-move-right")},
                output_enabled=True,
                profile_settings={
                    "Game": MotionProfileSettings(
                        turn_threshold=1000.0,
                        mouse_speed=20,
                        mouse_axis_enabled=False,
                    ),
                },
            ),
            executor=ActionExecutor(key_emitter=emitter),
        )

        event = session.record_prediction(1.0, prediction("turn-right", twist=1000.0))

        self.assertEqual(base.pointer_moves, [(20, 0)])
        self.assertIsNone(event["mouse_axis_strength"])
        emitter.close()

    def test_output_control_requires_connection_to_enable_but_always_allows_disable(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        bus = EventBus()
        control = ConnectionControl(manual_pairing=True)

        with self.assertRaisesRegex(ValueError, "Connect TRIKI"):
            handle_control(
                session,
                "output",
                {"enabled": True},
                bus=bus,
                connection_control=control,
            )

        session.set_status("ready", "UART_READY")
        enabled = handle_control(
            session,
            "output",
            {"enabled": True},
            bus=bus,
            connection_control=control,
        )
        session.set_status("disconnected", "Disconnected")
        disabled = handle_control(
            session,
            "output",
            {"enabled": False},
            bus=bus,
            connection_control=control,
        )

        self.assertTrue(enabled["output_enabled"])
        self.assertFalse(disabled["output_enabled"])

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
            {"operation": "create", "name": "Arena"},
            bus=bus,
            connection_control=control,
        )
        switched = handle_control(
            session,
            "profile",
            {"operation": "switch", "name": "Music"},
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

        self.assertIn("Arena", created["profiles"])
        self.assertEqual(created["active_profile"], "Arena")
        self.assertEqual(switched["active_profile"], "Music")
        self.assertEqual(reset_all["profiles"], ["Game", "Music"])

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
                    "version": CONFIG_VERSION,
                    "active_profile": "Arena",
                    "profiles": {
                        "Arena": {
                            "turn-left": {"type": "key", "key": "d"},
                            "turn-right": {"type": "key", "key": "a"},
                        }
                    },
                },
            },
            bus=bus,
            connection_control=control,
        )

        self.assertIn("Arena", state["profiles"])
        self.assertEqual(state["active_profile"], "Arena")
        imported_actions = {item["gesture_label"]: item for item in state["actions"]}
        self.assertEqual(imported_actions["turn-left"]["binding"]["key"], "d")

    def test_default_app_session_exposes_builtin_profiles(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))

        state = session.snapshot()

        # EXACTLY two built-ins, default active Game. Every profile lists the same
        # first-class Motion/Game controls (nothing hidden, no legacy classifier
        # rows in Advanced).
        self.assertEqual(state["active_profile"], "Game")
        self.assertEqual(state["profiles"], ["Game", "Music"])
        game_actions = {item["gesture_label"]: item for item in state["actions"]}
        self.assertEqual(set(game_actions), set(MOTION_LABELS))
        self.assertEqual(game_actions["turn-right"]["binding"]["key"], "right")
        self.assertEqual(game_actions["turn-left"]["binding"]["key"], "left")
        self.assertEqual(game_actions["go"]["binding"]["key"], "w")
        self.assertEqual(game_actions["stamp"]["binding"]["key"], "enter")
        self.assertEqual(game_actions["flip"]["binding"]["key"], "shift")
        self.assertEqual(game_actions["scrub-straight"]["binding"]["key"], "space")
        # No overloaded discrete labels leak into any profile rows.
        self.assertNotIn("scrub-cw", game_actions)
        self.assertNotIn("flip-over", game_actions)
        # The kid-facing row name says what the control DOES (no band-aid rename).
        self.assertEqual(display_name_for_label("go"), "Go forward (tilt)")
        self.assertEqual(display_name_for_label("stamp"), "Stamp (fire)")
        # Music has the same rows as Game, but defaults to media controls.
        session.switch_profile("Music")
        music_state = session.snapshot()
        music_actions = {item["gesture_label"]: item for item in music_state["actions"]}
        self.assertEqual(set(music_actions), set(game_actions))
        self.assertEqual(music_actions["turn-right"]["binding"]["key"], "volume-up")
        self.assertEqual(music_actions["turn-left"]["binding"]["key"], "volume-down")
        self.assertEqual(music_actions["go"]["binding"]["key"], "media-prev")
        self.assertEqual(music_actions["stamp"]["binding"]["key"], "media-play-pause")
        self.assertEqual(music_actions["flip"]["binding"]["key"], "volume-mute")
        self.assertEqual(music_actions["scrub-straight"]["binding"]["key"], "media-next")

    def test_builtin_profile_set_is_exactly_two(self):
        # The spec contract (pt 5): EXACTLY two built-ins -- Game + Music -- and the
        # old nine (Default, WASD Game, Presentation, 'Which Sausage, Mate?', Doom,
        # Doom Motion, Doom / Steering, Experimental Pointer, Media) are GONE. Locked
        # at BOTH the defaults source and a live default session so neither can drift.
        from triki_actions import default_profile_map

        self.assertEqual(set(default_profile_map()), {"Game", "Music"})
        for banished in (
            "Default", "WASD Game", "Presentation", "Which Sausage, Mate?",
            "Doom", "Doom Motion", "Doom / Steering", "Experimental Pointer", "Media",
        ):
            self.assertNotIn(banished, default_profile_map())

        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        self.assertEqual(session.snapshot()["profiles"], ["Game", "Music"])

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
        project_root = Path(__file__).resolve().parents[1]
        for relative_path in payload["docs"]:
            self.assertTrue((project_root / relative_path).is_file(), relative_path)

    def test_pairing_control_keeps_output_off_until_explicit_step_three(self):
        session = AppSession(
            config=TrikiConfig(output_enabled=True),
            executor=ActionExecutor(key_emitter=NullKeyEmitter()),
        )
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
        self.assertFalse(state["output_enabled"])
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
        self.assertIn("'action.record': 'Record key'", html)
        self.assertIn("profileNames", html)
        self.assertNotIn('href="/debug"', html)

    def test_html_exposes_clear_quit_and_mouse_mapping_controls(self):
        html = build_html()

        self.assertIn('id="quit-button"', html)
        self.assertIn("quit.confirm", html)
        self.assertIn('id="mouse-speed"', html)
        self.assertIn('id="mouse-axis-enabled"', html)
        self.assertIn("control('mouse-axis'", html)
        self.assertIn("mouseAxis.checked = m.mouse_axis_enabled !== false", html)
        self.assertNotIn("document.activeElement !== mouseAxis", html)
        self.assertIn("mouse-left-button", html)
        self.assertIn("mouse-move-up", html)
        self.assertIn("TURN CONTROL OFF", html)
        self.assertIn("const STATUS_TEXT", html)
        self.assertIn("TRIKI zostało rozłączone. Sterowanie jest wyłączone.", html)
        self.assertIn("battery.titleUnknown", html)

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
        # The button label is now i18n-keyed (PL default / EN), so it goes through
        # the T() lookup instead of a hard-coded English literal.
        self.assertIn("pairButton.textContent = isConnected ? T('connect.connected') : T('connect.pair');", html)
        self.assertIn("'connect.connected': 'Connected'", html)
        self.assertIn("'connect.connected': 'Połączono'", html)

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
        self.assertIn("#fit-stage {", html)
        self.assertIn("width: 1020px;", html)

    def test_html_embeds_real_cap_art_as_inline_data_uri(self):
        # The live-cap hero reuses the existing GPT-5.5 cap PNGs, base64-inlined
        # so they load in the frozen one-file EXE with no file-path dependence.
        html = build_html()

        from triki_app import (
            CAP_FRONT_DATA_URI,
            CAP_REVERSE_DATA_URI,
            CAP_SIDE_DATA_URI,
        )

        self.assertTrue(CAP_FRONT_DATA_URI.startswith("data:image/png;base64,"))
        self.assertIn('class="cap-face front" src="data:image/png;base64,', html)
        self.assertIn('class="cap-face side" src="data:image/png;base64,', html)
        self.assertIn('class="cap-face reverse" src="data:image/png;base64,', html)
        self.assertIn(CAP_FRONT_DATA_URI, html)
        self.assertIn(CAP_SIDE_DATA_URI, html)
        self.assertIn(CAP_REVERSE_DATA_URI, html)
        # No leftover placeholders or relative asset paths that would 404 frozen.
        self.assertNotIn("__CAP_FRONT__", html)
        self.assertNotIn("../assets/", html)

    def test_html_drives_live_cap_from_motion_and_gesture_events(self):
        html = build_html()

        # The live cap container + spin marker + face cross-fade layers.
        self.assertIn('id="live-cap"', html)
        self.assertIn("cap-spin-marker", html)
        self.assertIn("is-reverse", html)
        self.assertIn("is-tilted", html)
        # The 6-axis readout ("osie") cells and bars.
        self.assertIn('id="axis-readout"', html)
        self.assertIn('id="ax-0"', html)
        self.assertIn('id="ax-5"', html)
        self.assertIn('id="bar-2"', html)
        self.assertIn('id="energy-fill"', html)
        # Big current-gesture callout driven by SSE 'gesture'.
        self.assertIn('id="gesture-callout"', html)
        self.assertIn('id="gesture-word"', html)
        self.assertIn("triggerCallout", html)
        self.assertIn("pulseGesture(payload.gesture_label)", html)
        # The additive SSE 'motion' event feeds the cap animator.
        self.assertIn("payload.type === 'motion'", html)
        self.assertIn("onMotion(payload.values, payload.energy, payload.rotation)", html)
        self.assertIn("function onMotion", html)
        # Dual heartbeat (setInterval) + rAF keeps the cap alive in the WebView.
        self.assertIn("setInterval(tick, 30)", html)
        self.assertIn("requestAnimationFrame(raf)", html)
        # Grafted mascot face that reacts to gestures.
        self.assertIn('id="mascot-face"', html)
        self.assertIn("blinkLoop", html)

    def test_html_exposes_kid_friendly_game_tiles_and_big_toggles(self):
        html = build_html()

        # Game picker: one tile per profile, switching by exact profile name. The
        # built-in tiles are EXACTLY the two-profile world (Game + Music); the old
        # 9-profile gameMeta (Doom/WASD Game/Sausage/etc.) is gone.
        self.assertIn('id="game-grid"', html)
        self.assertIn("renderGameTiles", html)
        self.assertIn("'Game':", html)
        self.assertIn("'Music':", html)
        self.assertNotIn("'Which Sausage, Mate?'", html)
        self.assertNotIn("'WASD Game'", html)
        self.assertNotIn("'Doom Motion'", html)
        self.assertIn("operation: 'switch', name", html)
        # Step 3 output toggle (big ON/OFF) wired to the real output action.
        self.assertIn('id="power-btn"', html)
        self.assertIn("control('output', { enabled: !state.output_enabled })", html)
        # NO Step-4 game-mode rocker and NO hold UI: the Game profile auto-holds
        # internally (continuous output on connect). The whole hold/gamemode path is
        # removed from the kid flow.
        self.assertNotIn('id="gamemode-switch"', html)
        self.assertNotIn('id="hold-enabled"', html)
        self.assertNotIn('id="hold-ms"', html)
        self.assertNotIn("Step 4", html)
        # Advanced controls now live in a contained, scrollable OVERLAY (no longer
        # an in-DOM collapsible inside the grid -- the old <details> overpainted the
        # main screen). Opener button + backdrop + close control.
        self.assertIn('id="advanced-open"', html)
        self.assertIn('id="advanced-backdrop"', html)
        self.assertIn('id="advanced-close"', html)
        self.assertNotIn('<details class="advanced">', html)

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
        self.assertEqual(args.host, "127.0.0.1")  # loopback-only security boundary
        self.assertEqual(parse_args(["--host", "0.0.0.0"]).host, "0.0.0.0")  # explicit opt-in

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

    def test_activate_existing_instance_posts_show_control(self):
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
                    return b'{"state": {}}'

            return Response()

        activated = triki_app.activate_existing_instance(
            "http://127.0.0.1:8766/",
            opener=opener,
            timeout=0.25,
        )

        self.assertTrue(activated)
        self.assertEqual(requests[0]["url"], "http://127.0.0.1:8766/control?action=show")
        self.assertEqual(requests[0]["method"], "POST")
        self.assertEqual(requests[0]["body"], "{}")
        self.assertEqual(requests[0]["timeout"], 0.25)

    def test_activate_existing_instance_returns_false_when_no_server_is_running(self):
        def opener(request, timeout):
            raise OSError("connection refused")

        self.assertFalse(
            triki_app.activate_existing_instance(
                "http://127.0.0.1:8766/",
                opener=opener,
                timeout=0.25,
            )
        )

    def test_http_show_control_uses_server_window_callback(self):
        shown = []
        server = AppHttpServer(
            ("127.0.0.1", 0),
            AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter())),
            EventBus(),
            ConnectionControl(manual_pairing=True),
            show_window=lambda: shown.append(True),
        )
        thread = threading.Thread(target=server.handle_request)
        thread.start()

        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/control?action=show",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            response = urlopen(request, timeout=2)
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(shown, [True])
        self.assertEqual(payload["state"]["active_profile"], "Game")

    def test_main_activates_existing_instance_and_exits_before_starting_server(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "app.log"
            with (
                patch("triki_app.activate_existing_instance", return_value=True) as activate,
                patch("triki_app.AppHttpServer") as server_class,
            ):
                result = main([
                    "--ui",
                    "none",
                    "--port",
                    "9876",
                    "--log-path",
                    str(log_path),
                    "--no-session-log",
                ])

        self.assertEqual(result, 0)
        activate.assert_called_once_with("http://127.0.0.1:9876/")
        server_class.assert_not_called()

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

        self.assertTrue(fake.created["kwargs"]["resizable"])
        self.assertEqual(fake.created["kwargs"]["width"], 1020)
        self.assertEqual(fake.created["kwargs"]["height"], 820)

    def test_run_webview_window_registers_show_callback(self):
        class FakeWindow:
            def __init__(self):
                self.loaded = []
                self.shown = 0

            def load_url(self, url):
                self.loaded.append(url)

            def show(self):
                self.shown += 1

        class FakeWebview:
            def __init__(self):
                self.window = FakeWindow()

            def create_window(self, *args, **kwargs):
                return self.window

            def start(self):
                pass

        fake = FakeWebview()
        callbacks = []

        run_webview_window(
            "http://127.0.0.1:8765/debug",
            webview_module=fake,
            enable_tray=False,
            on_show_window=callbacks.append,
        )
        callbacks[0]()

        self.assertEqual(fake.window.loaded, ["http://127.0.0.1:8765/"])
        self.assertEqual(fake.window.shown, 1)

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

    def test_http_unknown_control_action_returns_400_with_error_body(self):
        server = AppHttpServer(
            ("127.0.0.1", 0),
            AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter())),
            EventBus(),
            ConnectionControl(manual_pairing=True),
        )
        thread = threading.Thread(target=server.handle_request)
        thread.start()

        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/control?action=frobnicate",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as context:
                urlopen(request, timeout=2)
            payload = json.loads(context.exception.read().decode("utf-8"))
        finally:
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(context.exception.code, 400)
        self.assertIn("unknown control action", payload["error"])

    def test_http_control_rejects_non_json_content_type(self):
        server = AppHttpServer(
            ("127.0.0.1", 0),
            AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter())),
            EventBus(),
            ConnectionControl(manual_pairing=True),
        )
        thread = threading.Thread(target=server.handle_request)
        thread.start()

        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/control?action=output",
                data=b"enabled=false",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as context:
                urlopen(request, timeout=2)
            payload = json.loads(context.exception.read().decode("utf-8"))
        finally:
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(context.exception.code, 415)
        self.assertIn("application/json", payload["error"])

    def test_http_control_rejects_foreign_browser_origin(self):
        server = AppHttpServer(
            ("127.0.0.1", 0),
            AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter())),
            EventBus(),
            ConnectionControl(manual_pairing=True),
        )
        thread = threading.Thread(target=server.handle_request)
        thread.start()

        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/control?action=output",
                data=b'{"enabled": false}',
                headers={
                    "Content-Type": "application/json",
                    "Origin": "https://example.com",
                },
                method="POST",
            )
            with self.assertRaises(HTTPError) as context:
                urlopen(request, timeout=2)
            payload = json.loads(context.exception.read().decode("utf-8"))
        finally:
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(context.exception.code, 403)
        self.assertIn("local app", payload["error"])

    def test_http_quit_disables_output_and_invokes_app_callback(self):
        callback_called = threading.Event()
        session = AppSession(
            config=TrikiConfig(output_enabled=True),
            executor=ActionExecutor(key_emitter=NullKeyEmitter()),
        )
        server = AppHttpServer(
            ("127.0.0.1", 0),
            session,
            EventBus(),
            ConnectionControl(manual_pairing=True),
            quit_app=callback_called.set,
        )
        thread = threading.Thread(target=server.handle_request)
        thread.start()

        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/control?action=quit",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            payload = json.loads(urlopen(request, timeout=2).read().decode("utf-8"))
            self.assertTrue(callback_called.wait(timeout=2))
        finally:
            thread.join(timeout=2)
            server.server_close()

        self.assertFalse(payload["state"]["output_enabled"])

    def _raw_control_status(self, content_length: str, body: bytes = b"{}") -> str:
        # Declare an arbitrary Content-Length without necessarily sending the
        # bytes, so an oversize/hostile length cannot be transmitted in full. A
        # short socket timeout proves the handler answers promptly (no hang).
        server = AppHttpServer(
            ("127.0.0.1", 0),
            AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter())),
            EventBus(),
            ConnectionControl(manual_pairing=True),
        )
        thread = threading.Thread(target=server.handle_request)
        thread.start()

        connection = socket.create_connection(("127.0.0.1", server.server_port), timeout=2)
        try:
            connection.settimeout(2)
            request = (
                f"POST /control?action=output HTTP/1.1\r\n"
                f"Host: 127.0.0.1\r\n"
                f"Content-Length: {content_length}\r\n"
                f"Content-Type: application/json\r\n"
                f"Connection: close\r\n"
                f"\r\n"
            ).encode("ascii") + body
            connection.sendall(request)
            chunks = []
            while True:
                data = connection.recv(4096)
                if not data:
                    break
                chunks.append(data)
        finally:
            connection.close()
            thread.join(timeout=2)
            server.server_close()

        response = b"".join(chunks).decode("latin-1")
        status_line = response.split("\r\n", 1)[0]
        return status_line

    def test_http_oversize_content_length_is_rejected_with_413(self):
        status_line = self._raw_control_status("999999999", body=b"{}")
        self.assertIn("413", status_line)

    def test_http_malformed_content_length_is_rejected_with_400(self):
        status_line = self._raw_control_status("abc", body=b"{}")
        self.assertIn("400", status_line)

    def test_http_negative_content_length_does_not_hang(self):
        # A negative length must be rejected promptly (here as 400) rather than
        # putting the handler into a blocking read; the socket timeout in the
        # helper would surface a hang as an error instead of a status line.
        status_line = self._raw_control_status("-5", body=b"{}")
        self.assertIn("400", status_line)

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
        session.create_profile("Arena")
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
        self.assertEqual(payload["active_profile"], "Arena")
        self.assertIn("Arena", payload["profiles"])
        self.assertIn("Game", payload["profiles"])
        self.assertIn("Music", payload["profiles"])

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

        class FakeIcon:
            def __init__(self):
                self.notifications = []
                self.stopped = 0

            def notify(self, message, title):
                self.notifications.append((message, title))

            def stop(self):
                self.stopped += 1

        window = FakeWindow()
        stopped = []
        controller = TrayController(
            window,
            on_quit=lambda: stopped.append(True),
            language="pl",
        )
        controller.icon = FakeIcon()
        controller.attach_close_handler()

        should_close = window.events.closing.handlers[0]()
        controller.open_window()
        controller.quit()

        self.assertFalse(should_close)
        self.assertEqual(window.hidden, 1)
        self.assertEqual(window.shown, 1)
        self.assertEqual(window.destroyed, 1)
        self.assertEqual(stopped, [True])
        self.assertEqual(controller.icon.stopped, 1)
        self.assertEqual(len(controller.icon.notifications), 1)
        message, title = controller.icon.notifications[0]
        self.assertIn("nadal działa", title)
        self.assertIn("Wyłącz sterowanie", message)
        self.assertIn("Zakończ", message)

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
            ["Open TRIKI Control", "Disable control", "Pair TRIKI", "Diagnostics", "Quit"],
        )
        self.assertTrue(controller.icon.detached)

    def test_tray_image_uses_packaged_app_icon_asset(self):
        from PIL import Image, ImageDraw

        image = create_tray_image(Image, ImageDraw)

        self.assertEqual(image.size, (64, 64))
        self.assertEqual(image.mode, "RGBA")
        self.assertLess(image.getpixel((0, 0))[3], 12)
        bottom_right = image.getpixel((54, 54))
        self.assertGreater(bottom_right[0], 120)
        self.assertGreater(bottom_right[2], 120)

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


def synth_dir_lean_samples(dx, dy, deg=20.0, hold_s=1.0, ramp_s=0.30, dt=0.02, spin=250):
    """A HELD body-frame directional lean toward (dx,dy) as MotionSamples, with a
    small steady in-plane gyro so the cap stays 'in motion' (gref does not relock).
    NO calibration -- neutral is the rest pose auto-seeded from the first sample.
    Mirrors synth_dir_lean in tests/test_triki_motion_engine.py: gravity rocks into
    the cap's own d/e plane toward (dx,dy), so the engine's hd/he decode picks the
    matching WASD label. (+e -> backward / scrub-ccw; -e -> forward / scrub-cw;
    All held leans now emit the tank GO label; hd/he remain diagnostics for the
    on-screen cap direction.
    """
    seq = []
    t = 0.0
    for _ in range(int(1.5 / dt)):
        seq.append((t, (0, 0, 0, 0, 0, -int(G))))
        t += dt
    th = math.radians(deg)
    ramp = int(ramp_s / dt)
    gx, gy = int(spin * dy), int(-spin * dx)
    for k in range(ramp):
        a = th * (k + 1) / ramp
        seq.append(
            (
                t,
                (gx, gy, 0, int(G * math.sin(a) * dx), int(G * math.sin(a) * dy), int(-G * math.cos(a))),
            )
        )
        t += dt
    for _ in range(int(hold_s / dt)):
        seq.append(
            (
                t,
                (gx, gy, 0, int(G * math.sin(th) * dx), int(G * math.sin(th) * dy), int(-G * math.cos(th))),
            )
        )
        t += dt
    return [(tt, MotionSample(packet_id=0, values=tuple(int(x) for x in v))) for tt, v in seq]


class MotionEngineAppIntegrationTests(unittest.TestCase):
    """End-to-end integration for the body-frame Motion engine behind the single
    "Game" profile (the DEFAULT): it routes through build_detector, and a held lean
    drives record_prediction -> mapped Game key -> HoldKeyEmitter holds it. NO
    calibration anywhere. Music/custom profiles use the same Motion engine wiring.
    The engine-level signs/thresholds are covered in tests/test_triki_motion_engine.py;
    here we lock the APP wiring.
    """

    def _args(self):
        # A full default args namespace (carries window_seconds/min_samples/etc.
        # that the classifier branch of build_detector reads).
        return parse_args([])

    def test_build_detector_routes_to_motion_engine_for_game_profile(self):
        # The Game profile is the DEFAULT and runs the Motion engine.
        config = TrikiConfig().merged_with_defaults()
        self.assertEqual(config.active_profile, "Game")
        self.assertEqual(config.engine, ENGINE_MOTION)

        detector = build_detector(config, self._args())

        self.assertIsInstance(detector, MotionControlEngine)

    def test_build_detector_routes_to_motion_engine_for_music_profile(self):
        config = TrikiConfig(active_profile="Music").merged_with_defaults()
        self.assertEqual(config.engine, ENGINE_MOTION)

        detector = build_detector(config, self._args())

        self.assertIsInstance(detector, MotionControlEngine)

    def test_build_detector_attaches_observer_to_motion_engine(self):
        # The session-log 'gesture' channel must keep recording in Motion mode, and
        # the body-frame tilt diagnostics (hd/he/fire) ride the observer record.
        config = TrikiConfig().merged_with_defaults()
        records = []
        detector = build_detector(config, self._args(), observer=records.append)

        for t, sample in synth_lean_hold_samples(deg=25.0):
            detector.add_sample(t, sample)

        self.assertTrue(records)
        self.assertIn("intent", records[-1])
        self.assertIn("hd", records[-1])
        self.assertIn("he", records[-1])
        self.assertIn("fire", records[-1])
        # A held lean commits the tank GO intent.
        self.assertTrue(any(r["intent"] == "go" for r in records))

    def test_engine_router_follows_runtime_profile_switch(self):
        # REGRESSION: the live engine must FOLLOW a runtime profile switch. With a
        # detector fixed at stream start, switching Game<->Music mid-stream left the
        # stale engine emitting the OTHER vocabulary's labels, which the new profile's
        # action map lacks -> record_prediction missed every lookup and silently
        # dropped ALL input until restart. The router re-routes per sample and resets
        # the newly-active engine on the switch.
        class _Spy:
            def __init__(self, label):
                self.label, self.samples, self.resets = label, 0, 0

            def add_sample(self, t, sample):
                self.samples += 1
                return prediction(self.label)

            def reset(self):
                self.resets += 1

        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        motion, classifier = _Spy("go"), _Spy("rotate-cw")
        router = ProfileEngineRouter(session, motion, classifier)
        s = MotionSample(packet_id=0, values=(0, 0, 0, 0, 0, -2050))

        session.switch_profile("Game")  # engine == motion
        self.assertEqual(router.add_sample(0.0, s).label, "go")
        self.assertEqual((motion.samples, classifier.samples), (1, 0))
        self.assertEqual(motion.resets, 1)  # newly-active engine reset on first sample

        session.switch_profile("Music")  # engine == motion, same action vocabulary as Game
        self.assertEqual(router.add_sample(0.1, s).label, "go")
        self.assertEqual((motion.samples, classifier.samples), (2, 0))
        self.assertEqual(classifier.resets, 0)

        session.switch_profile("Game")  # same engine; no classifier detour
        self.assertEqual(router.add_sample(0.2, s).label, "go")
        self.assertEqual(motion.resets, 1)

    def test_build_engine_router_exposes_motion_engine_for_diagnostics(self):
        # main() hands router.motion to set_motion_engine so the tilt diagnostics /
        # threshold control keep working; it must be the real MotionControlEngine.
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        router = build_engine_router(session, self._args())
        self.assertIsInstance(router, ProfileEngineRouter)
        self.assertIsInstance(router.motion, MotionControlEngine)
        self.assertIsInstance(router.classifier, LiveGestureDetector)

    def test_default_motion_hold_is_non_zero(self):
        # hold_ms MUST default > 0 for Motion mode (hold_ms == 0 stutters); the
        # constant the wiring seeds sits in the recommended 150-250ms band.
        self.assertGreater(DEFAULT_MOTION_HOLD_MS, 0)
        self.assertLessEqual(DEFAULT_MOTION_HOLD_MS, 250)

    def test_lean_sequence_through_engine_holds_bound_movement_key(self):
        # END-TO-END: feed a held e-axis lean through the Motion engine; every
        # emitted GesturePrediction goes to record_prediction, which maps the
        # committed GO label -> its Game key 'w' and the
        # HoldKeyEmitter keeps the key held with a SINGLE key_down (continuous walk,
        # no re-tapping). The physical +/- direction is a remappable preference; the
        # contract is "one committed MOVE label -> its bound arrow, held".
        base = _RecordingBase()
        emitter = HoldKeyEmitter(base, hold_ms=DEFAULT_MOTION_HOLD_MS)
        config = TrikiConfig(output_enabled=True, hold_ms=DEFAULT_MOTION_HOLD_MS).merged_with_defaults()
        self.assertEqual(config.active_profile, "Game")
        session = AppSession(config=config, executor=ActionExecutor(key_emitter=emitter))
        engine = MotionControlEngine()

        emitted_labels = []
        for t, sample in synth_lean_hold_samples(deg=25.0, hold_s=1.0):
            pred = engine.add_sample(t, sample)
            if pred is not None:
                emitted_labels.append(pred.label)
                session.record_prediction(t, pred)

        # The +e lean commits GO -> Game key 'w'.
        self.assertIn(MOVE_BACKWARD_LABEL, emitted_labels)
        self.assertEqual(base.downs, ["w"])  # held continuously: exactly one down
        self.assertEqual(base.ups, [])  # not released while the lean persists
        self.assertGreater(session.snapshot()["action_count"], 1)  # re-emitted every sample

        # Lean ends -> releasing the hold (deterministically) lets the key up once.
        emitter.set_hold_ms(0)
        self.assertEqual(base.ups, ["w"])
        emitter.close()

    def test_forward_lean_maps_to_up_arrow(self):
        # A clean forward lean commits GO -> Game key 'w', held continuously.
        base = _RecordingBase()
        emitter = HoldKeyEmitter(base, hold_ms=DEFAULT_MOTION_HOLD_MS)
        config = TrikiConfig(output_enabled=True, hold_ms=DEFAULT_MOTION_HOLD_MS).merged_with_defaults()
        session = AppSession(config=config, executor=ActionExecutor(key_emitter=emitter))
        engine = MotionControlEngine()

        emitted = []
        for t, sample in synth_dir_lean_samples(0.0, -1.0, deg=20.0, hold_s=1.0):
            pred = engine.add_sample(t, sample)
            if pred is not None:
                emitted.append(pred.label)
                session.record_prediction(t, pred)

        self.assertIn(MOVE_FORWARD_LABEL, emitted)  # he<0 -> forward
        self.assertEqual(base.downs, ["w"])  # bound to 'w', held once
        self.assertEqual(base.ups, [])
        emitter.close()

    def test_twist_holds_a_turn_arrow_through_the_game_profile(self):
        # END-TO-END "twist -> arrows": a steady in-place TWIST (yaw-dominated, no
        # f-axis impulse) emits a TURN GesturePrediction which the Game profile maps
        # onto an ARROW key (rotate-cw/ccw -> right/left), held continuously by the
        # HoldKeyEmitter. NEVER fire, NEVER a movement key -- a twist only steers.
        # The SIGN is locked too: an opposite twist holds the OPPOSITE arrow.
        config = TrikiConfig(output_enabled=True, hold_ms=DEFAULT_MOTION_HOLD_MS).merged_with_defaults()
        self.assertEqual(config.active_profile, "Game")
        self.assertEqual(config.actions["turn-right"].key_name, "right")
        self.assertEqual(config.actions["turn-left"].key_name, "left")

        def run_twist(rate):
            base = _RecordingBase()
            emitter = HoldKeyEmitter(base, hold_ms=DEFAULT_MOTION_HOLD_MS)
            session = AppSession(config=config, executor=ActionExecutor(key_emitter=emitter))
            engine = MotionControlEngine()
            labels = []
            for t, sample in synth_twist_samples(rate=rate, n=80):
                pred = engine.add_sample(t, sample)
                if pred is not None:
                    labels.append(pred.label)
                    session.record_prediction(t, pred)
            emitter.set_hold_ms(0)  # deterministic release
            emitter.close()
            return labels, base

        pos_labels, pos = run_twist(2600)
        neg_labels, neg = run_twist(-2600)

        # A twist only ever emits TURN labels here (no move, no fire).
        self.assertTrue(pos_labels)
        self.assertTrue(neg_labels)
        for label in pos_labels + neg_labels:
            self.assertIn(label, (TURN_RIGHT_LABEL, TURN_LEFT_LABEL))
            self.assertNotEqual(label, FIRE_LABEL)
        # Each twist holds exactly ONE arrow continuously, released once at the end.
        self.assertEqual(len(set(pos.downs)), 1)
        self.assertEqual(len(set(neg.downs)), 1)
        self.assertIn(pos.downs[0], ("left", "right"))
        self.assertIn(neg.downs[0], ("left", "right"))
        # SIGN contract: opposite twists -> opposite arrows (never the same key).
        self.assertNotEqual(pos.downs[0], neg.downs[0])
        self.assertEqual(sorted({pos.downs[0], neg.downs[0]}), ["left", "right"])

    def test_output_off_releases_held_motion_key(self):
        base = _RecordingBase()
        emitter = HoldKeyEmitter(base, hold_ms=DEFAULT_MOTION_HOLD_MS)
        config = TrikiConfig(output_enabled=True, hold_ms=DEFAULT_MOTION_HOLD_MS).merged_with_defaults()
        session = AppSession(config=config, executor=ActionExecutor(key_emitter=emitter))
        engine = MotionControlEngine()

        for t, sample in synth_lean_hold_samples(deg=25.0, hold_s=0.6):
            pred = engine.add_sample(t, sample)
            if pred is not None:
                session.record_prediction(t, pred)
        self.assertEqual(base.downs, ["w"])

        session.set_output_enabled(False)  # disabling output releases the held key
        self.assertEqual(base.ups, ["w"])
        emitter.close()

    def test_switching_out_of_game_profile_releases_held_key(self):
        base = _RecordingBase()
        emitter = HoldKeyEmitter(base, hold_ms=DEFAULT_MOTION_HOLD_MS)
        config = TrikiConfig(output_enabled=True, hold_ms=DEFAULT_MOTION_HOLD_MS).merged_with_defaults()
        session = AppSession(config=config, executor=ActionExecutor(key_emitter=emitter))
        engine = MotionControlEngine()

        for t, sample in synth_lean_hold_samples(deg=25.0, hold_s=0.6):
            pred = engine.add_sample(t, sample)
            if pred is not None:
                session.record_prediction(t, pred)
        self.assertEqual(base.downs, ["w"])

        # switch_profile already calls _release_held_keys() on the executor.
        # Music is the other built-in, with the same Motion engine settings.
        session.switch_profile("Music")
        self.assertEqual(base.ups, ["w"])
        self.assertEqual(session.config.engine, ENGINE_MOTION)
        emitter.close()

    def _run_go_lean(self, base, emitter, session, dx, dy):
        # Any held lean drives the tank GO label, body-frame, NO calibrate.
        engine = MotionControlEngine()
        emitted = []
        for t, sample in synth_dir_lean_samples(dx, dy, hold_s=1.0):
            pred = engine.add_sample(t, sample)
            if pred is not None:
                emitted.append(pred.label)
                session.record_prediction(t, pred)
        return emitted

    def test_held_right_lean_holds_the_tank_go_key(self):
        # END-TO-END: a held lean to the RIGHT (hd>0) commits GO -> Game key 'w'
        # and HoldKeyEmitter holds it continuously.
        base = _RecordingBase()
        emitter = HoldKeyEmitter(base, hold_ms=DEFAULT_MOTION_HOLD_MS)
        config = TrikiConfig(output_enabled=True, hold_ms=DEFAULT_MOTION_HOLD_MS).merged_with_defaults()
        session = AppSession(config=config, executor=ActionExecutor(key_emitter=emitter))

        emitted = self._run_go_lean(base, emitter, session, 1.0, 0.0)
        self.assertIn(MOVE_STRAFE_RIGHT_LABEL, emitted)
        self.assertEqual(emitted[-1], MOVE_STRAFE_RIGHT_LABEL)  # committed GO
        self.assertIn("w", base.downs)
        self.assertEqual(base.downs.count("w"), 1)  # exactly one down (continuous)
        self.assertEqual(base.ups, [])  # never released while the lean persists

        emitter.set_hold_ms(0)  # deterministic flush
        self.assertIn("w", base.ups)
        emitter.close()

    def test_held_left_lean_holds_the_tank_go_key(self):
        # A held lean to the LEFT (hd<0) also commits GO -> Game key 'w'.
        base = _RecordingBase()
        emitter = HoldKeyEmitter(base, hold_ms=DEFAULT_MOTION_HOLD_MS)
        config = TrikiConfig(output_enabled=True, hold_ms=DEFAULT_MOTION_HOLD_MS).merged_with_defaults()
        session = AppSession(config=config, executor=ActionExecutor(key_emitter=emitter))

        emitted = self._run_go_lean(base, emitter, session, -1.0, 0.0)
        self.assertIn(MOVE_STRAFE_LEFT_LABEL, emitted)
        self.assertEqual(emitted[-1], MOVE_STRAFE_LEFT_LABEL)
        self.assertIn("w", base.downs)
        self.assertEqual(base.downs.count("w"), 1)
        self.assertEqual(base.ups, [])
        emitter.close()

    @unittest.skipUnless(
        RUN_REAL_LOG_TESTS and os.path.exists(REAL_STAMP_LOG),
        "set TRIKI_REAL_LOG_TESTS=1 with labeled real session logs to run",
    )
    def test_stamp_fires_enter_through_the_game_profile(self):
        # END-TO-END: a real STAMP slice from the session log drives the Motion
        # engine to emit STAMP, which the Game profile maps to Enter
        # (Doom-default fire). Fire is a TAP (per-stamp pulse), not a held key, so
        # the press goes through press_key, not key_down.
        slice_samples = _real_stamp_slice()
        emitter = NullKeyEmitter()
        config = TrikiConfig(output_enabled=True).merged_with_defaults()
        self.assertEqual(config.actions[FIRE_LABEL].key_name, "enter")
        session = AppSession(config=config, executor=ActionExecutor(key_emitter=emitter))
        engine = MotionControlEngine()

        fired = False
        for t, sample in slice_samples:
            pred = engine.add_sample(t, sample)
            if pred is not None and pred.label == FIRE_LABEL:
                fired = True
                session.record_prediction(t, pred)
        self.assertTrue(fired, "a real stamp slice must fire stamp")
        self.assertIn("enter", emitter.pressed)  # STAMP -> Enter

    def test_held_tilt_and_twist_do_not_fire_through_the_app(self):
        # A held tilt (static gravity) and a twist (yaw-dominated) NEVER fire 'lift'
        # in the engine, so the Game profile never pulses Ctrl from them -- the
        # structural guarantee that a tilt/turn can't be mistaken for a shot.
        # Held tilt.
        engine = MotionControlEngine()
        for t, sample in synth_dir_lean_samples(0.0, -1.0, deg=20.0, hold_s=1.5):
            pred = engine.add_sample(t, sample)
            if pred is not None:
                self.assertNotEqual(pred.label, FIRE_LABEL, "held tilt must not fire")
        # Twist.
        engine2 = MotionControlEngine()
        for t, sample in synth_twist_samples(rate=2600, n=80):
            pred = engine2.add_sample(t, sample)
            if pred is not None:
                self.assertNotEqual(pred.label, FIRE_LABEL, "twist must not fire")

    def test_snapshot_exposes_body_frame_tilt_diagnostics(self):
        # The snapshot surfaces hd/he/tilt/tilt_on/direction/fire (NOT f/r/heading/
        # calibrated -- that machinery is gone).
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        motion = session.snapshot()["motion"]
        for key in ("hd", "he", "tilt", "tilt_on", "direction", "fire"):
            self.assertIn(key, motion)
        for gone in ("f", "r", "heading", "calibrated"):
            self.assertNotIn(gone, motion)

    def test_snapshot_surfaces_live_engine_tilt(self):
        # With a live engine attached, the snapshot reflects its body-frame lean.
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        engine = MotionControlEngine()
        for t, sample in synth_dir_lean_samples(0.0, -1.0, deg=20.0, hold_s=0.6):
            engine.add_sample(t, sample)
        session.set_motion_engine(engine)
        motion = session.snapshot()["motion"]
        self.assertGreater(motion["tilt"], 5.0)  # a real lean is surfaced
        self.assertEqual(motion["tilt_on"], engine.tilt_on)

    def test_app_snapshot_exposes_engine_and_defaults_to_motion(self):
        # Every profile uses the Motion engine.
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        self.assertEqual(session.snapshot()["engine"], ENGINE_MOTION)
        session.switch_profile("Music")
        self.assertEqual(session.snapshot()["engine"], ENGINE_MOTION)
        session.switch_profile("Game")
        self.assertEqual(session.snapshot()["engine"], ENGINE_MOTION)

    def test_music_and_custom_profiles_route_through_motion(self):
        # Guard: Music and user-created profiles use the same Motion engine as Game.
        for profile in ("Music",):
            config = TrikiConfig(active_profile=profile).merged_with_defaults()
            self.assertIsInstance(build_detector(config, self._args()), MotionControlEngine, profile)
        # A custom profile created through the API also routes to Motion.
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        session.create_profile("Arena")
        self.assertEqual(session.config.engine, ENGINE_MOTION)
        self.assertIsInstance(
            build_detector(session.config, self._args()), MotionControlEngine
        )

    def test_tilt_control_adjusts_live_engine_threshold(self):
        # handle_control('tilt') updates the live Motion engine's lean engage
        # threshold (and its hysteresis release proportionally) -- the ONLY tilt
        # knob, with NO calibration.
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        engine = MotionControlEngine()
        session.set_motion_engine(engine)
        old_off_ratio = engine.tilt_off / engine.tilt_on

        snap = handle_control(
            session,
            "tilt",
            {"threshold": 14.0},
            bus=EventBus(),
            connection_control=ConnectionControl(manual_pairing=True),
        )
        self.assertEqual(engine.tilt_on, 14.0)
        self.assertAlmostEqual(engine.tilt_off / engine.tilt_on, old_off_ratio, places=2)
        self.assertEqual(snap["motion"]["tilt_on"], 14.0)

    def test_tilt_control_is_null_safe_without_motion_engine(self):
        # No engine attached (classifier path / tests): 'tilt' is a safe no-op that
        # still returns a snapshot.
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        snap = handle_control(
            session,
            "tilt",
            {"threshold": 12.0},
            bus=EventBus(),
            connection_control=ConnectionControl(manual_pairing=True),
        )
        self.assertEqual(snap["status"], "idle")

    def test_motion_tuning_controls_are_profile_specific(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        engine = MotionControlEngine()
        session.set_motion_engine(engine)
        default_tilt_on = engine.tilt_on

        game_snap = handle_control(
            session,
            "turn-sensitivity",
            {"value": 44},
            bus=EventBus(),
            connection_control=ConnectionControl(manual_pairing=True),
        )
        self.assertEqual(game_snap["motion"]["turn_sensitivity"], 44)
        self.assertEqual(session.config.profile_settings["Game"].turn_sensitivity, 44.0)
        game_snap = handle_control(
            session,
            "turn-threshold",
            {"value": 1000},
            bus=EventBus(),
            connection_control=ConnectionControl(manual_pairing=True),
        )
        self.assertEqual(game_snap["motion"]["turn_threshold"], 1000)
        self.assertEqual(session.config.profile_settings["Game"].turn_threshold, 1000.0)
        self.assertEqual(engine.tilt_on, default_tilt_on)

        session.switch_profile("Music")
        self.assertGreater(engine.turn_sensitivity, 44.0)
        music_snap = handle_control(
            session,
            "turn-threshold",
            {"value": 580},
            bus=EventBus(),
            connection_control=ConnectionControl(manual_pairing=True),
        )
        music_snap = handle_control(
            session,
            "turn-sensitivity",
            {"value": 92},
            bus=EventBus(),
            connection_control=ConnectionControl(manual_pairing=True),
        )
        self.assertEqual(music_snap["motion"]["turn_threshold"], 580)
        self.assertEqual(music_snap["motion"]["turn_sensitivity"], 92)
        self.assertEqual(session.config.profile_settings["Music"].turn_threshold, 580.0)
        self.assertEqual(engine.tilt_on, default_tilt_on)

        session.switch_profile("Game")
        self.assertEqual(engine.turn_sensitivity, 44)
        self.assertEqual(engine.turn_threshold, 1000)
        self.assertEqual(engine.tilt_on, default_tilt_on)
        session.switch_profile("Music")
        self.assertEqual(engine.turn_sensitivity, 92)
        self.assertEqual(engine.turn_threshold, 580)
        self.assertEqual(engine.tilt_on, default_tilt_on)

    def test_build_detector_applies_active_profile_motion_settings(self):
        config = TrikiConfig(
            active_profile="Music",
            profile_settings={"Music": MotionProfileSettings(turn_threshold=560.0, turn_sensitivity=95.0)},
        ).merged_with_defaults()

        detector = build_detector(config, self._args())

        self.assertEqual(detector.turn_threshold, 560.0)
        self.assertEqual(detector.turn_sensitivity, 95.0)

    def test_no_calibrate_control_action(self):
        # The 'calibrate' control is removed entirely (no calibration anywhere).
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        with self.assertRaisesRegex(ValueError, "unknown control action: calibrate"):
            handle_control(
                session,
                "calibrate",
                {},
                bus=EventBus(),
                connection_control=ConnectionControl(manual_pairing=True),
            )


class TiltControlHtmlTests(unittest.TestCase):
    def test_html_exposes_turn_tuning_separate_from_tilt_readout(self):
        html = build_html()

        # The body-frame Motion block is toggled by state.engine == 'motion',
        # but profile-specific tuning is for TURN, not the lean threshold.
        self.assertIn('id="tilt-section"', html)
        self.assertIn('id="turn-threshold"', html)
        self.assertIn("control('turn-threshold'", html)
        self.assertIn('id="engine-name"', html)
        self.assertIn("function renderTilt", html)
        self.assertIn("renderTilt()", html)
        self.assertIn("state.engine) === 'motion'", html)
        self.assertIn("control('turn-sensitivity'", html)
        self.assertIn('id="motion-hd"', html)
        self.assertIn('id="motion-he"', html)
        self.assertIn('id="motion-tilt"', html)
        self.assertIn("profile-specific TURN", html)
        # Twist/lean copy is present; NO calibration / heading machinery survives.
        self.assertIn("twist", html.lower())
        self.assertIn("lean", html.lower())
        self.assertNotIn("function renderMotion", html)
        self.assertNotIn("Calibrate forward", html)
        self.assertNotIn("Heading", html)
        self.assertNotIn("orientation-free", html)

    def test_html_viz_shares_engine_hd_he_sign_convention(self):
        # Bug #8: the cap rotation AND the directional arrows must derive from the
        # SAME body-frame hd/he sign source the engine decodes, so a forward lean
        # tilts the cap forward and lights the forward arrow (never flipped).
        html = build_html()
        # The shared sign signals (forward = he<0 -> -tiltY; right = hd>0 -> tiltX).
        self.assertIn("const fwdSig = -vis.tiltY;", html)
        self.assertIn("const strafeSig = vis.tiltX;", html)
        # Cap rotation driven by the shared signals (not the old inverted lines).
        self.assertIn("cap.style.setProperty('--cap-tilt-x', (fwdSig * 26)", html)
        self.assertIn("cap.style.setProperty('--cap-tilt-y', (strafeSig * 26)", html)
        # Arrows driven by the SAME signals so they agree with the key mapping.
        self.assertIn("setArrow('pedal-forward', fwdSig > 0.12)", html)
        self.assertIn("setArrow('pedal-backward', fwdSig < -0.12)", html)
        self.assertIn("setArrow('side-right', strafeSig > 0.12", html)
        self.assertIn("setArrow('side-left', strafeSig < -0.12", html)


class AdvancedOverlayUiTests(unittest.TestCase):
    """The Advanced panel must be a CONTAINED, scrollable overlay rendered OUTSIDE
    the stage grid (the old <details> child overpainted the whole main screen)."""

    def test_advanced_is_not_a_child_of_the_stage_grid(self):
        html = build_html()
        stage_start = html.index('<div class="stage-grid">')
        stage_end = html.index("</div>", html.index("</aside>"))
        stage_grid_html = html[stage_start:stage_end]
        # The advanced overlay markup must NOT live inside the stage grid.
        self.assertNotIn("adv-backdrop", stage_grid_html)
        self.assertNotIn('id="actions"', stage_grid_html)
        # And the old collapsible is gone entirely.
        self.assertNotIn('<details class="advanced">', html)
        self.assertNotIn("Advanced settings (grown-ups)</summary>", html)

    def test_advanced_title_is_plain_advanced_settings(self):
        html = build_html()

        self.assertIn('data-i18n="advanced.title">Advanced settings</h2>', html)
        self.assertIn("'advanced.title': 'Ustawienia zaawansowane'", html)
        self.assertIn("'advanced.title': 'Advanced settings'", html)
        self.assertNotIn("grown-ups", html)
        self.assertNotIn("dla doros", html.lower())

    def test_advanced_overlay_is_fixed_scrollable_and_closeable(self):
        html = build_html()
        # Fixed backdrop + a high stacking context above the main grid.
        self.assertIn(".adv-backdrop {", html)
        self.assertIn("position: fixed;", html)
        self.assertIn("z-index: 50;", html)
        # The inner panel scrolls internally (never spills the viewport).
        self.assertIn(".adv-body {", html)
        self.assertIn("overflow-y: auto;", html)
        self.assertIn("max-height: 88vh;", html)
        # Close control + open/close + backdrop-click + Esc handlers exist.
        self.assertIn('id="advanced-close"', html)
        self.assertIn("function openAdvanced", html)
        self.assertIn("function closeAdvanced", html)
        self.assertIn("event.key === 'Escape'", html)

    def test_advanced_panel_preserves_all_backend_control_ids(self):
        # Moving Advanced out of the grid must keep every element id the rest of
        # the UI / id-based tests rely on. The hold/game-mode + calibrate ids are
        # GONE (the Game profile auto-holds; there is no calibration); the Motion
        # block exposes profile-specific turn tuning.
        html = build_html()
        for element_id in (
            'id="profile-select"', 'id="new-profile-name"', 'id="create-profile"',
            'id="delete-profile"', 'id="reset-profile"', 'id="export-profiles"',
            'id="import-profiles"', 'id="reset-all-profiles"', 'id="import-profile-file"',
            'id="actions"', 'id="tilt-section"', 'id="turn-threshold"', 'id="engine-name"',
        ):
            self.assertIn(element_id, html)
        # The removed hold/game-mode + calibrate controls are gone for good.
        for gone_id in (
            'id="hold-enabled"', 'id="hold-ms"', 'id="hold-hint"',
            'id="gamemode-switch"', 'id="calibrate-forward"', 'id="motion-section"',
        ):
            self.assertNotIn(gone_id, html)


class GameModeToggleRemovedUiTests(unittest.TestCase):
    """The Step-4 game-mode rocker and ALL hold UI are removed from the kid flow:
    the Game profile auto-holds continuously on connect (internal), so the flow is
    Connect -> Pick game -> Output ON with no toggle to misconfigure."""

    def test_gamemode_rocker_and_hold_ui_are_gone(self):
        html = build_html()
        # No rocker element, knob, or its switch styling.
        self.assertNotIn('id="gamemode-switch"', html)
        self.assertNotIn('id="gamemode-knob"', html)
        self.assertNotIn("toggleGameMode", html)
        # No hold checkbox / ms input / hint, and no JS that drives them.
        self.assertNotIn('id="hold-enabled"', html)
        self.assertNotIn('id="hold-ms"', html)
        self.assertNotIn('id="hold-hint"', html)
        self.assertNotIn("function renderHold", html)
        # Output ON/OFF is still the lone Step-3 control.
        self.assertIn('id="power-btn"', html)
        self.assertIn("control('output', { enabled: !state.output_enabled })", html)

    def test_game_profile_auto_holds_without_a_toggle(self):
        # main() seeds a non-zero hold for the Motion engine (the Game profile)
        # without any toggle; the constant sits in the recommended band.
        self.assertGreater(DEFAULT_MOTION_HOLD_MS, 0)
        self.assertLessEqual(DEFAULT_MOTION_HOLD_MS, 250)
        config = TrikiConfig(active_profile="Game").merged_with_defaults()
        self.assertEqual(config.engine, ENGINE_MOTION)


class GhostRotationUiTests(unittest.TestCase):
    def test_cap_spin_decays_to_rest_instead_of_ghost_spinning(self):
        # The on-screen cap must unwind to rest: driven by instantaneous gyro with a
        # frame-rate-independent decay, NOT an unconditional velocity accumulator.
        html = build_html()
        self.assertIn("if (Math.abs(turnRaw) > 0.035)", html)
        self.assertIn("vis.spinAngle = (vis.spinAngle + vis.turn * spinGain * dt) % 360;", html)
        self.assertIn("else: HOLD -- do not decay", html)
        # The old velocity accumulator is gone.
        self.assertNotIn("spinVelocity", html)


class I18nUiTests(unittest.TestCase):
    def test_build_html_ships_polish_default_with_english_toggle(self):
        html = build_html()
        # Polish is the first-paint default, applied before any SSE state.
        self.assertIn("<html lang=\"pl\">", html)
        self.assertIn("let lang = 'pl';", html)
        self.assertIn("setLang('pl');", html)
        # Both language tables + the walker + the data-i18n attribute mechanism.
        self.assertIn("const I18N = {", html)
        self.assertIn("function setLang", html)
        self.assertIn("data-i18n", html)
        self.assertIn("state.lang", html)
        # A PL/EN toggle in the header wired to the 'lang' control.
        self.assertIn('id="lang-toggle"', html)
        self.assertIn("control('lang'", html)
        # Representative Polish copy is present (PL-default, not just English).
        self.assertIn("Połącz TRIKI", html)
        self.assertIn("Zaawansowane", html)
        self.assertIn("Sterowanie obrotem", html)  # Turn control (PL)
        # No leftover Polish calibration copy (calibration is gone entirely).
        self.assertNotIn("Skalibruj przód", html)

    def test_debug_page_stays_english(self):
        debug = build_debug_html()
        self.assertIn("<html lang=\"en\">", debug)
        self.assertNotIn("data-i18n", debug)
        self.assertNotIn("Zaawansowane", debug)

    def test_config_round_trips_lang_default_polish(self):
        from triki_actions import normalize_lang

        self.assertEqual(TrikiConfig().merged_with_defaults().lang, "pl")
        self.assertEqual(normalize_lang("EN"), "en")
        self.assertEqual(normalize_lang("nonsense"), "pl")
        self.assertEqual(normalize_lang(None), "pl")
        # Round-trip through to_dict / from_dict preserves an explicit choice.
        config = TrikiConfig(lang="en").merged_with_defaults()
        self.assertEqual(config.to_dict()["lang"], "en")
        self.assertEqual(TrikiConfig.from_dict(config.to_dict()).lang, "en")
        # An old config carrying no 'lang' field defaults to Polish.
        legacy = config.to_dict()
        del legacy["lang"]
        self.assertEqual(TrikiConfig.from_dict(legacy).lang, "pl")

    def test_snapshot_exposes_lang_and_set_lang_persists(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        self.assertEqual(session.snapshot()["lang"], "pl")  # PL default
        snap = session.set_lang("en")
        self.assertEqual(snap["lang"], "en")
        self.assertEqual(session.config.lang, "en")
        # Unknown values fall back to Polish.
        self.assertEqual(session.set_lang("klingon")["lang"], "pl")

    def test_handle_control_lang_action_sets_language(self):
        session = AppSession(executor=ActionExecutor(key_emitter=NullKeyEmitter()))
        snap = handle_control(
            session,
            "lang",
            {"lang": "en"},
            bus=EventBus(),
            connection_control=ConnectionControl(manual_pairing=True),
        )
        self.assertEqual(snap["lang"], "en")

    def test_set_lang_round_trips_through_saved_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "triki.json"
            session = AppSession(
                config_path=config_path,
                executor=ActionExecutor(key_emitter=NullKeyEmitter()),
            )
            session.set_lang("en")
            reloaded = AppSession(
                config_path=config_path,
                executor=ActionExecutor(key_emitter=NullKeyEmitter()),
            )
            self.assertEqual(reloaded.snapshot()["lang"], "en")


class MovementSurfacingUiTests(unittest.TestCase):
    def test_html_surfaces_tilt_live_diagnostics_without_calibration(self):
        html = build_html()
        # Exactly two built-in tiles (Game + Music); NO Doom Motion / 9-profile meta.
        self.assertIn("'Game':", html)
        self.assertIn("'Music':", html)
        self.assertNotIn("'Doom Motion':", html)
        self.assertNotIn("DOOM Motion", html)
        # NO calibrate control of any kind.
        self.assertNotIn('id="calibrate-forward"', html)
        self.assertNotIn("control('calibrate'", html)
        # Live body-frame tilt diagnostics surfaced from snapshot().motion
        # (hd/he/tilt/direction) -- NOT the old f/r/heading.
        self.assertIn('id="motion-hd"', html)
        self.assertIn('id="motion-he"', html)
        self.assertIn('id="motion-tilt"', html)
        self.assertIn('id="motion-direction"', html)
        self.assertIn("state.motion", html)
        self.assertNotIn('id="motion-heading"', html)
        # The deleted calibration-tied tunables are gone (gyro scale / cal angle /
        # heading-based dominance readout).
        self.assertNotIn("tun.gyroScale", html)
        self.assertNotIn("tun.calAngle", html)
        self.assertNotIn("Calibrate angle", html)


if __name__ == "__main__":
    unittest.main()
