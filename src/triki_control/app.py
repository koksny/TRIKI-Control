from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import threading
import time
import webbrowser
from collections.abc import Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from triki_control.actions import (
    ActionBinding,
    ActionExecutor,
    GESTURE_LABELS,
    TrikiConfig,
    default_actions_for_profile,
    load_config,
    normalize_gesture_label,
    normalize_profile_name,
    parse_macro_text,
    save_config,
)
from triki_control.calibration_server import ConnectionControl, EventBus, encode_sse, quiet_stream_errors
from triki_control.battery import battery_snapshot, normalize_battery_percent
from triki_control.key_emitter import NullKeyEmitter
from triki_control.live import LiveGestureDetector
from triki_control.play import BleCommandBridge, play_button_hint, run_ble_stream
from triki_control.diagnostics import collect_diagnostics
from triki_control.metadata import APP_CREATOR, APP_LICENSE, APP_NAME, APP_VERSION, APP_WEBSITE


_DEFAULT_CONSOLE_STREAM = object()


class AppSession:
    def __init__(
        self,
        *,
        config: TrikiConfig | None = None,
        config_path: Path | None = None,
        executor: ActionExecutor | None = None,
    ) -> None:
        self.config_path = config_path
        self.config = (
            config.merged_with_defaults()
            if config is not None
            else load_config(config_path) if config_path is not None
            else TrikiConfig().merged_with_defaults()
        )
        self.executor = executor if executor is not None else ActionExecutor()
        self._lock = threading.RLock()
        self.status = "idle"
        self.message = "Click Pair TRIKI to connect, then choose a profile."
        self._status_sequence = 0
        self._sample_count = 0
        self._gesture_count = 0
        self._action_count = 0
        self._action_revision = 1
        self._recent_events: list[dict] = []
        self._connection_log: list[dict] = []
        self._battery_percent: int | None = None
        self._battery_message = "Battery level unknown."

    def set_status(self, status: str, message: str) -> dict:
        with self._lock:
            self.status = status
            self.message = message
            self._status_sequence += 1
            self._connection_log.append(
                {
                    "sequence": self._status_sequence,
                    "status": status,
                    "message": message,
                    "timestamp": time.strftime("%H:%M:%S"),
                }
            )
            self._connection_log = self._connection_log[-80:]
            return self.snapshot()

    def set_battery_level(self, percent: int | None, message: str = "") -> dict:
        with self._lock:
            self._battery_percent = normalize_battery_percent(percent)
            self._battery_message = message or (
                "Battery level unknown."
                if self._battery_percent is None
                else "Battery level read from BLE."
            )
            return self.snapshot()

    def set_output_enabled(self, enabled: bool) -> dict:
        with self._lock:
            self.config.output_enabled = enabled
            self._save_config()
            return self.snapshot()

    def update_action(self, gesture_label: str, binding: ActionBinding) -> dict:
        gesture_label = normalize_gesture_label(gesture_label)
        if gesture_label not in GESTURE_LABELS:
            raise ValueError(f"unknown gesture label: {gesture_label}")
        with self._lock:
            self.config.actions[gesture_label] = binding
            self.config.profiles[self.config.active_profile] = dict(self.config.actions)
            self._action_revision += 1
            self._save_config()
            return self.snapshot()

    def create_profile(self, name: str) -> dict:
        profile_name = normalize_profile_name(name)
        with self._lock:
            if profile_name in self.config.profiles:
                raise ValueError(f"profile already exists: {profile_name}")
            self.config.profiles[profile_name] = dict(self.config.actions)
            self.config.active_profile = profile_name
            self.config.actions = dict(self.config.profiles[profile_name])
            self._action_revision += 1
            self._save_config()
            return self.snapshot()

    def switch_profile(self, name: str) -> dict:
        profile_name = normalize_profile_name(name)
        with self._lock:
            if profile_name not in self.config.profiles:
                raise ValueError(f"unknown profile: {profile_name}")
            self.config.active_profile = profile_name
            self.config.actions = dict(self.config.profiles[profile_name])
            self._action_revision += 1
            self._save_config()
            return self.snapshot()

    def delete_profile(self, name: str) -> dict:
        profile_name = normalize_profile_name(name)
        with self._lock:
            if profile_name not in self.config.profiles:
                raise ValueError(f"unknown profile: {profile_name}")
            if len(self.config.profiles) == 1:
                raise ValueError("cannot delete the last profile")
            del self.config.profiles[profile_name]
            if self.config.active_profile == profile_name:
                self.config.active_profile = next(iter(self.config.profiles))
                self.config.actions = dict(self.config.profiles[self.config.active_profile])
            self._action_revision += 1
            self._save_config()
            return self.snapshot()

    def export_profiles(self) -> dict:
        with self._lock:
            config = self.config.merged_with_defaults()
            return {
                "version": config.version,
                "active_profile": config.active_profile,
                "profiles": {
                    name: {
                        gesture: binding.to_dict()
                        for gesture, binding in actions.items()
                    }
                    for name, actions in config.profiles.items()
                },
            }

    def import_profiles(self, data: dict, *, replace: bool = False) -> dict:
        if not isinstance(data, dict):
            raise ValueError("profile import requires a JSON object")
        incoming = TrikiConfig.from_dict(data).merged_with_defaults()
        with self._lock:
            if replace:
                self.config.profiles = {
                    name: dict(actions)
                    for name, actions in incoming.profiles.items()
                }
            else:
                self.config.profiles.update(
                    {
                        name: dict(actions)
                        for name, actions in incoming.profiles.items()
                    }
                )
            self.config.active_profile = (
                incoming.active_profile
                if incoming.active_profile in self.config.profiles
                else next(iter(self.config.profiles))
            )
            self.config.actions = dict(self.config.profiles[self.config.active_profile])
            self._action_revision += 1
            self._save_config()
            return self.snapshot()

    def reset_all_profiles(self) -> dict:
        with self._lock:
            output_enabled = self.config.output_enabled
            self.config = TrikiConfig(output_enabled=output_enabled).merged_with_defaults()
            self._action_revision += 1
            self._save_config()
            return self.snapshot()

    def reset_active_profile(self) -> dict:
        with self._lock:
            self.config.actions = default_actions_for_profile(self.config.active_profile)
            self.config.profiles[self.config.active_profile] = dict(self.config.actions)
            self._action_revision += 1
            self._save_config()
            return self.snapshot()

    def clear_events(self) -> dict:
        with self._lock:
            self._recent_events.clear()
            self._gesture_count = 0
            self._action_count = 0
            return self.snapshot()

    def test_key_output(self, key_name: str) -> dict:
        result = self.executor.execute(ActionBinding.key(key_name))
        with self._lock:
            event = {
                "type": "output-test",
                "gesture_label": "",
                "action_description": result.description,
                "action_emitted": result.emitted,
                "output_enabled": self.config.output_enabled,
                "output_reason": result.reason,
            }
            self._recent_events.append(event)
            self._recent_events = self._recent_events[-80:]
            self.message = f"Output test: {result.reason}"
            return self.snapshot()

    def record_sample(self) -> dict:
        with self._lock:
            self._sample_count += 1
            return {"type": "sample", "sample_count": self._sample_count}

    def record_prediction(self, elapsed_seconds: float, prediction) -> dict:
        with self._lock:
            gesture_label = normalize_gesture_label(prediction.label)
            binding = self.config.actions.get(gesture_label, ActionBinding.disabled())
            if self.config.output_enabled:
                result = self.executor.execute(binding)
            else:
                result = _blocked_result(binding)
            self._gesture_count += 1
            if result.emitted:
                self._action_count += 1
            event = {
                "type": "gesture",
                "elapsed_seconds": round(elapsed_seconds, 6),
                "gesture_label": gesture_label,
                "confidence": round(prediction.confidence, 3),
                "reason": prediction.reason,
                "action_description": result.description,
                "action_emitted": result.emitted,
                "output_enabled": self.config.output_enabled,
                "output_reason": result.reason,
                "features": {
                    "gyro_p99": round(prediction.features.gyro_p99, 3),
                    "accel_deviation_p99": round(prediction.features.accel_deviation_p99, 3),
                    "c_mean": round(prediction.features.c_mean, 3),
                    "c_abs_p99": round(prediction.features.c_abs_p99, 3),
                    "lateral_gyro_p99": round(prediction.features.lateral_gyro_p99, 3),
                    "lateral_accel_p99": round(prediction.features.lateral_accel_p99, 3),
                    "orientation_angle_degrees": round(prediction.features.orientation_angle_degrees, 3),
                },
            }
            self._recent_events.append(event)
            self._recent_events = self._recent_events[-80:]
            return event

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "message": self.message,
                "app_version": APP_VERSION,
                "config_path": str(self.config_path) if self.config_path is not None else "",
                "button_hint": play_button_hint(self.status, self.message),
                "output_enabled": self.config.output_enabled,
                "sample_count": self._sample_count,
                "gesture_count": self._gesture_count,
                "action_count": self._action_count,
                "action_revision": self._action_revision,
                "active_profile": self.config.active_profile,
                "profiles": list(self.config.profiles.keys()),
                "battery": battery_snapshot(self._battery_percent, self._battery_message),
                "actions": [
                    {
                        "gesture_label": gesture,
                        "display_name": display_name_for_gesture(gesture),
                        "binding": self.config.actions[gesture].to_dict(),
                        "description": self.config.actions[gesture].description,
                    }
                    for gesture in GESTURE_LABELS
                ],
                "recent_events": list(reversed(self._recent_events[-30:])),
                "connection_log": list(reversed(self._connection_log[-30:])),
            }

    def _save_config(self) -> None:
        if self.config_path is not None:
            save_config(self.config_path, self.config)


def _blocked_result(binding: ActionBinding):
    from triki_control.actions import ActionResult

    return ActionResult(False, binding.description, "output disabled")


def display_name_for_gesture(gesture_label: str) -> str:
    return {
        "rotate-cw": "Rotate Right",
        "rotate-ccw": "Rotate Left",
        "scrub-cw": "Scrub Right",
        "scrub-ccw": "Scrub Left",
        "back-forth": "Back-and-forth",
        "lift": "Stamp / Lift",
        "flip-over": "Flip",
    }.get(gesture_label, gesture_label)


def binding_from_payload(payload: dict) -> ActionBinding:
    action_type = str(payload.get("action_type", payload.get("type", "key"))).lower()
    if action_type == "disabled":
        return ActionBinding.disabled()
    if action_type in {"key", "media"}:
        return ActionBinding.key(str(payload["key_name"]))
    if action_type == "macro":
        return parse_macro_text(str(payload.get("macro_text", "")))
    raise ValueError(f"unknown action type: {action_type}")


def build_about_payload(session: AppSession) -> dict:
    return {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "creator": APP_CREATOR,
        "website": APP_WEBSITE,
        "license": APP_LICENSE,
        "config_path": str(session.config_path) if session.config_path is not None else "",
        "docs": [
            "README.md",
            "CREDITS.md",
            "LICENSE",
            "docs/linux.md",
            "docs/protocol.md",
            "docs/architecture.md",
            "docs/roadmap.md",
        ],
    }


def handle_control(
    session: AppSession,
    action: str,
    payload: dict,
    *,
    bus: EventBus,
    connection_control: ConnectionControl,
    command_bridge: BleCommandBridge | None = None,
) -> dict:
    if action == "pairing":
        session.set_output_enabled(True)
        return connection_control.request_pairing(session, bus)
    if action == "led":
        if command_bridge is None:
            raise RuntimeError("TRIKI LED control is not available.")
        enabled = bool(payload.get("enabled"))
        command_bridge.set_led(enabled)
        return session.set_status(
            session.status,
            "TRIKI LED test on." if enabled else "TRIKI LED test off.",
        )
    if action == "output":
        return session.set_output_enabled(bool(payload.get("enabled")))
    if action == "test-key":
        return session.test_key_output(str(payload.get("key", "right")))
    if action == "clear":
        return session.clear_events()
    if action == "action":
        return session.update_action(str(payload["gesture_label"]), binding_from_payload(payload))
    if action == "profile":
        operation = str(payload.get("operation", "")).lower()
        if operation == "create":
            return session.create_profile(str(payload.get("name", "")))
        if operation == "switch":
            return session.switch_profile(str(payload.get("name", "")))
        if operation == "delete":
            return session.delete_profile(str(payload.get("name", "")))
        if operation == "import":
            data = payload.get("data", payload.get("config"))
            return session.import_profiles(data, replace=bool(payload.get("replace", False)))
        if operation == "reset":
            return session.reset_active_profile()
        if operation == "reset-all":
            return session.reset_all_profiles()
        raise ValueError(f"unknown profile operation: {operation}")
    raise ValueError(f"unknown control action: {action}")


class AppHttpHandler(BaseHTTPRequestHandler):
    server: AppHttpServer

    def log_message(self, format, *args) -> None:  # noqa: A002
        if self.path == "/favicon.ico":
            return
        write_console_line(
            "%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), format % args),
            stream=sys.stderr,
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_text(build_html(), "text/html; charset=utf-8")
            return
        if parsed.path == "/debug":
            self._send_text(build_debug_html(), "text/html; charset=utf-8")
            return
        if parsed.path == "/about":
            self._send_json(build_about_payload(self.server.session))
            return
        if parsed.path == "/diagnostics":
            self._send_json(collect_diagnostics(config_path=self.server.session.config_path))
            return
        if parsed.path == "/profiles/export":
            self._send_profile_export()
            return
        if parsed.path == "/state":
            self._send_json({"type": "state", "state": self.server.session.snapshot()})
            return
        if parsed.path == "/events":
            self._stream_events()
            return
        if parsed.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/control":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(body or "{}")
            action = parse_qs(parsed.query).get("action", [""])[0]
            state = handle_control(
                self.server.session,
                action,
                payload,
                bus=self.server.bus,
                connection_control=self.server.connection_control,
                command_bridge=self.server.command_bridge,
            )
        except Exception as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.server.bus.publish({"type": "state", "state": state})
        self._send_json({"state": state})

    def _stream_events(self) -> None:
        subscriber = self.server.bus.subscribe()
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(encode_sse({"type": "state", "state": self.server.session.snapshot()}).encode("utf-8"))
            self.wfile.flush()
            while True:
                item = subscriber.get()
                self.wfile.write(item.encode("utf-8"))
                self.wfile.flush()
        except quiet_stream_errors():
            return
        finally:
            self.server.bus.unsubscribe(subscriber)

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_profile_export(self) -> None:
        body = json.dumps(self.server.session.export_profiles(), indent=2).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="triki-profiles.json"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, body: str, content_type: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class AppHttpServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        session: AppSession,
        bus: EventBus,
        connection_control: ConnectionControl,
        command_bridge: BleCommandBridge | None = None,
    ) -> None:
        super().__init__(server_address, AppHttpHandler)
        self.session = session
        self.bus = bus
        self.connection_control = connection_control
        self.command_bridge = command_bridge or BleCommandBridge()


class TrayController:
    def __init__(
        self,
        window,
        *,
        url: str | None = None,
        on_quit=None,
        opener=urlopen,
        pystray_module=None,
        image_module=None,
        image_draw_module=None,
    ) -> None:
        self.window = window
        self.url = url
        self.on_quit = on_quit
        self.opener = opener
        self.pystray_module = pystray_module
        self.image_module = image_module
        self.image_draw_module = image_draw_module
        self.icon = None
        self._allow_close = False
        self._close_handler_attached = False

    def attach_close_handler(self) -> None:
        if self._close_handler_attached:
            return
        self.window.events.closing += self._on_window_closing
        self._close_handler_attached = True

    def start(self) -> bool:
        try:
            pystray_module = self.pystray_module
            image_module = self.image_module
            image_draw_module = self.image_draw_module
            if pystray_module is None:
                import pystray as pystray_module
            if image_module is None or image_draw_module is None:
                from PIL import Image as image_module
                from PIL import ImageDraw as image_draw_module
        except Exception:
            return False

        menu = pystray_module.Menu(
            pystray_module.MenuItem("Open TRIKI Control", self.open_window, default=True),
            pystray_module.MenuItem("Pair TRIKI", self.request_pairing),
            pystray_module.MenuItem("Diagnostics", self.open_diagnostics),
            pystray_module.MenuItem("Quit", self.quit),
        )
        self.icon = pystray_module.Icon(
            "TRIKI Control",
            create_tray_image(image_module, image_draw_module),
            "TRIKI Control",
            menu,
        )
        self.attach_close_handler()
        if hasattr(self.icon, "run_detached"):
            self.icon.run_detached()
        else:
            thread = threading.Thread(target=self.icon.run, daemon=True)
            thread.start()
        return True

    def _on_window_closing(self, *args) -> bool:
        if self._allow_close:
            return True
        self.window.hide()
        return False

    def open_window(self, *args) -> None:
        if self.url is not None and hasattr(self.window, "load_url"):
            self.window.load_url(app_url_for_path(self.url, "/"))
        self.window.show()

    def open_diagnostics(self, *args) -> None:
        if self.url is not None and hasattr(self.window, "load_url"):
            self.window.load_url(app_url_for_path(self.url, "/debug"))
        self.window.show()

    def request_pairing(self, *args) -> None:
        if self.url is None:
            return
        with contextlib.suppress(Exception):
            post_control_action(self.url, "pairing", opener=self.opener)

    def quit(self, *args) -> None:
        self._allow_close = True
        if self.icon is not None:
            with contextlib.suppress(Exception):
                self.icon.stop()
        if self.on_quit is not None:
            self.on_quit()
        self.window.destroy()


def create_tray_image(image_module, image_draw_module):
    image = image_module.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = image_draw_module.Draw(image)
    draw.ellipse((6, 6, 58, 58), fill=(35, 134, 54, 255), outline=(46, 160, 67, 255), width=3)
    draw.text((21, 18), "T", fill=(255, 255, 255, 255))
    return image


def app_url_for_path(base_url: str, path: str) -> str:
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def post_control_action(
    base_url: str,
    action: str,
    payload: dict | None = None,
    *,
    opener=urlopen,
    timeout: float = 2.0,
) -> dict:
    body = json.dumps({} if payload is None else payload).encode("utf-8")
    request = Request(
        app_url_for_path(base_url, f"/control?action={action}"),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw or "{}")


def build_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TRIKI Control</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101216;
      --panel: #171b22;
      --soft: #202632;
      --line: #333d4d;
      --text: #f2f6fb;
      --muted: #a9b5c5;
      --blue: #3f72d8;
      --green: #238636;
      --green-line: #2ea043;
      --amber: #d29922;
      --red: #f85149;
    }
    * { box-sizing: border-box; }
    [hidden] { display: none !important; }
    html, body {
      height: 100vh;
      overflow: hidden;
    }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      letter-spacing: 0;
    }
    main { max-width: 1020px; height: 100vh; margin: 0 auto; padding: 14px 18px; display: grid; grid-template-rows: auto minmax(0, 1fr) auto; gap: 12px; overflow: hidden; }
    h1, h2 { margin: 0; line-height: 1.2; }
    h1 { font-size: 22px; }
    h2 { font-size: 16px; }
    .app-header, .panel { border: 1px solid var(--line); background: var(--panel); border-radius: 8px; padding: 14px; }
    .app-header { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 14px; align-items: center; }
    .panel { min-height: 0; overflow: hidden; }
    .header-title { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; min-width: 0; }
    .header-actions { display: flex; align-items: center; gap: 10px; }
    .battery-indicator { display: inline-flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; white-space: nowrap; min-height: 24px; }
    .battery-icon { position: relative; width: 34px; height: 16px; border: 1px solid var(--line); border-radius: 3px; padding: 2px; }
    .battery-icon::after { content: ""; position: absolute; right: -5px; top: 4px; width: 3px; height: 6px; border-radius: 0 2px 2px 0; background: var(--line); }
    .battery-fill { display: block; height: 100%; width: 0%; border-radius: 2px; background: var(--muted); transition: width 160ms ease, background 160ms ease; }
    .battery-indicator.ok .battery-fill { background: var(--green-line); }
    .battery-indicator.medium .battery-fill { background: var(--amber); }
    .battery-indicator.low .battery-fill, .battery-indicator.critical .battery-fill { background: var(--red); }
    .battery-indicator.unknown .battery-icon, .battery-indicator.unavailable .battery-icon { opacity: 0.55; }
    dialog {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      color: var(--text);
      padding: 16px;
      width: min(420px, calc(100vw - 36px));
    }
    dialog::backdrop { background: rgba(0, 0, 0, 0.56); }
    .about-body { display: grid; gap: 10px; }
    .about-body p { margin: 0; color: var(--muted); line-height: 1.45; overflow-wrap: anywhere; }
    .about-actions { display: flex; justify-content: flex-end; margin-top: 14px; }
    .mapping-head { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 12px; align-items: center; margin-bottom: 12px; }
    .profile-controls { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
    .profile-controls input { width: 150px; }
    .actions { display: grid; gap: 8px; }
    .status-footer { color: var(--muted); display: flex; justify-content: space-between; align-items: baseline; gap: 16px; font-size: 13px; padding: 0 2px 4px; }
    .status-footer p { margin: 0; line-height: 1.35; }
    .footer-left { text-align: left; }
    .footer-right { text-align: right; }
    button, select, input {
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--soft);
      color: var(--text);
      padding: 8px 10px;
      font-size: 14px;
    }
    button { cursor: pointer; }
    button:disabled { cursor: not-allowed; opacity: 0.52; }
    button.primary { background: var(--blue); border-color: var(--blue); color: white; }
    .led-button { min-height: 44px; min-width: 104px; font-weight: 700; }
    .led-button.active { border-color: var(--amber); color: #ffe8a3; }
    .about-button { min-height: 44px; min-width: 78px; font-weight: 700; }
    .pair-button { min-height: 56px; min-width: 180px; padding: 14px 22px; font-size: 17px; font-weight: 700; }
    .pair-button.connected { background: var(--green); border-color: var(--green-line); }
    .action-row { border: 1px solid var(--line); background: var(--soft); border-radius: 8px; padding: 8px; }
    .action-row { display: grid; grid-template-columns: 132px 112px minmax(130px, 1fr) auto minmax(180px, 1.2fr) auto; gap: 8px; align-items: center; }
    .gesture-name { display: grid; gap: 2px; }
    .gesture-name small { color: var(--muted); font-size: 11px; }
    .record-key.recording { border-color: var(--green-line); color: #c6f6d5; }
    @media (max-width: 860px) {
      .app-header { grid-template-columns: 1fr; }
      .header-actions { width: 100%; }
      .led-button { flex: 0 0 104px; }
      .pair-button { width: 100%; }
      .mapping-head { grid-template-columns: 1fr; }
      .profile-controls { justify-content: stretch; }
      .profile-controls > * { flex: 1 1 140px; }
      .action-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main>
    <section class="app-header">
      <div class="header-title">
        <h1 id="app-title">TRIKI Control</h1>
        <div class="battery-indicator unknown" id="battery-indicator" aria-label="Battery status" title="Battery level unknown.">
          <span class="battery-icon" aria-hidden="true"><span class="battery-fill" id="battery-fill"></span></span>
          <span class="battery-label" id="battery-label">Battery --</span>
        </div>
      </div>
      <div class="header-actions">
        <button class="led-button" id="led-test" type="button" title="Hold to light the TRIKI LED" disabled>Test LED</button>
        <button class="about-button" id="about-button" type="button">About</button>
        <button class="primary pair-button" data-action="pairing">Pair TRIKI</button>
      </div>
    </section>
    <section class="panel">
      <div class="mapping-head">
        <h2>Action Mapping</h2>
        <div class="profile-controls">
          <select id="profile-select" aria-label="Profile"></select>
          <input id="new-profile-name" placeholder="New profile">
          <button id="create-profile" type="button">New</button>
          <button id="delete-profile" type="button">Delete</button>
          <button id="reset-profile" type="button">Reset</button>
          <button id="export-profiles" type="button">Export</button>
          <button id="import-profiles" type="button">Import</button>
          <button id="reset-all-profiles" type="button">Reset All</button>
          <input id="import-profile-file" type="file" accept="application/json" hidden>
        </div>
      </div>
      <div class="actions" id="actions"></div>
    </section>
    <section class="status-footer">
      <p class="footer-left" id="message">Waiting for state</p>
      <p class="footer-right" id="hint">Pairing button: wait.</p>
    </section>
    <dialog id="about-dialog">
      <div class="about-body">
        <h2>TRIKI Control v__APP_VERSION__</h2>
        <p id="about-version">Version __APP_VERSION__</p>
        <p id="about-credits">Created by Wojciech 'Koksny' Górny, Koksny.com.</p>
        <p id="about-license">Open source under the MIT License.</p>
        <p id="about-config">Config path will appear here.</p>
        <p>Docs: README.md, CREDITS.md, LICENSE, docs/linux.md, docs/protocol.md, docs/architecture.md</p>
      </div>
      <div class="about-actions">
        <button id="about-close" type="button">Close</button>
      </div>
    </dialog>
  </main>
  <script>
    let state = null;
    let renderedActionRevision = null;
    let renderedProfileSignature = null;
    let activeRecorder = null;
    const keyChoices = [
      ['', 'Disabled'],
      ['left', 'Left Arrow'],
      ['right', 'Right Arrow'],
      ['up', 'Up Arrow'],
      ['down', 'Down Arrow'],
      ['enter', 'Enter'],
      ['space', 'Space'],
      ['escape', 'Escape'],
      ['tab', 'Tab'],
      ['backspace', 'Backspace'],
      ['page-up', 'Page Up'],
      ['page-down', 'Page Down'],
      ['=', 'Equals'],
      ['w', 'W'],
      ['a', 'A'],
      ['s', 'S'],
      ['d', 'D'],
      ['volume-up', 'Volume Up'],
      ['volume-down', 'Volume Down'],
      ['volume-mute', 'Volume Mute'],
      ['media-play-pause', 'Play/Pause'],
      ['media-next', 'Media Next'],
      ['media-prev', 'Media Previous']
    ];

    function setState(next) {
      state = next;
      document.getElementById('app-title').textContent = 'TRIKI Control';
      document.getElementById('message').textContent = state.message;
      document.getElementById('hint').textContent = state.button_hint || '';
      renderBattery(state.battery);
      const pairButton = document.querySelector('.pair-button');
      const ledButton = document.getElementById('led-test');
      const isConnected = state.status === 'connected' || state.status === 'ready';
      pairButton.classList.toggle('connected', isConnected);
      pairButton.textContent = isConnected ? 'Connected' : 'Pair TRIKI';
      ledButton.disabled = !isConnected;
      if (!isConnected && ledHeld) {
        ledHeld = false;
        ledButton.classList.remove('active');
      }
      const nextProfileSignature = profileSignature();
      if (nextProfileSignature !== renderedProfileSignature) renderProfiles();
      if (state.action_revision !== renderedActionRevision) renderActions();
    }

    function renderBattery(battery) {
      const root = document.getElementById('battery-indicator');
      const fill = document.getElementById('battery-fill');
      const label = document.getElementById('battery-label');
      const rawPercent = battery && Number.isFinite(battery.percent) ? battery.percent : null;
      const percent = rawPercent === null ? null : Math.max(0, Math.min(100, rawPercent));
      const status = battery && battery.status ? battery.status : percent === null ? 'unknown' : 'ok';
      const text = battery && battery.label ? battery.label : percent === null ? 'Battery --' : `${percent}%`;
      root.className = `battery-indicator ${status}`;
      fill.style.width = percent === null ? '18%' : `${percent}%`;
      label.textContent = text;
      root.title = battery && battery.message ? battery.message : 'Battery level unknown.';
      root.setAttribute('aria-label', `Battery ${text}`);
    }

    function profileNames() {
      return state.profiles && state.profiles.length ? state.profiles : ['Default'];
    }

    function profileSignature() {
      return `${state.active_profile || ''}|${profileNames().join('|')}`;
    }

    function renderProfiles() {
      const select = document.getElementById('profile-select');
      select.innerHTML = '';
      for (const name of profileNames()) {
        const option = document.createElement('option');
        option.value = name;
        option.textContent = name;
        select.appendChild(option);
      }
      select.value = state.active_profile || profileNames()[0];
      renderedProfileSignature = profileSignature();
    }

    function allKeyChoices() {
      const choices = [...keyChoices];
      for (let code = 65; code <= 90; code += 1) {
        const key = String.fromCharCode(code).toLowerCase();
        if (!choices.some(choice => choice[0] === key)) choices.push([key, String.fromCharCode(code)]);
      }
      for (let digit = 0; digit <= 9; digit += 1) {
        choices.push([String(digit), String(digit)]);
      }
      for (let index = 1; index <= 12; index += 1) {
        choices.push([`f${index}`, `F${index}`]);
      }
      return choices;
    }

    function renderActions() {
      const root = document.getElementById('actions');
      root.innerHTML = '';
      for (const item of state.actions || []) {
        const row = document.createElement('div');
        row.className = 'action-row';
        row.dataset.gesture = item.gesture_label;
        row.innerHTML = `
          <strong class="gesture-name"><span>${escapeHtml(item.display_name || item.gesture_label)}</span><small>${escapeHtml(item.gesture_label)}</small></strong>
          <select class="action-type">
            <option value="key">Key / Media</option>
            <option value="macro">Macro</option>
            <option value="disabled">Disabled</option>
          </select>
          <select class="key-select"></select>
          <button class="record-key" type="button">Record Key</button>
          <input class="macro-input" placeholder="left, 100ms, enter">
          <button class="apply-action" type="button">Save</button>`;
        const typeSelect = row.querySelector('.action-type');
        const keySelect = row.querySelector('.key-select');
        const recordButton = row.querySelector('.record-key');
        const macroInput = row.querySelector('.macro-input');
        for (const choice of allKeyChoices()) {
          const option = document.createElement('option');
          option.value = choice[0];
          option.textContent = choice[1];
          keySelect.appendChild(option);
        }
        const binding = item.binding || { type: 'disabled' };
        typeSelect.value = binding.type === 'macro' ? 'macro' : binding.type === 'disabled' ? 'disabled' : 'key';
        keySelect.value = binding.key || '';
        macroInput.value = binding.type === 'macro' ? macroToText(binding.steps || []) : '';
        const syncFields = () => {
          keySelect.disabled = typeSelect.value !== 'key';
          recordButton.disabled = typeSelect.value !== 'key';
          macroInput.disabled = typeSelect.value !== 'macro';
        };
        typeSelect.addEventListener('change', syncFields);
        recordButton.addEventListener('click', () => startKeyRecording(recordButton, keySelect));
        row.querySelector('.apply-action').addEventListener('click', () => {
          saveAction(item.gesture_label, typeSelect.value, keySelect.value, macroInput.value);
        });
        syncFields();
        root.appendChild(row);
      }
      renderedActionRevision = state.action_revision;
    }

    function saveAction(gestureLabel, actionType, keyName, macroText) {
      control('action', {
        gesture_label: gestureLabel,
        action_type: actionType,
        key_name: keyName,
        macro_text: macroText
      });
    }

    function macroToText(steps) {
      return steps.map(step => step.type === 'delay' ? `${step.ms}ms` : step.key).join(', ');
    }

    function startKeyRecording(button, select) {
      if (activeRecorder) activeRecorder.button.classList.remove('recording');
      button.classList.add('recording');
      button.textContent = 'Press Key';
      activeRecorder = { button, select };
      const finish = (event) => {
        event.preventDefault();
        event.stopPropagation();
        const keyName = keyNameFromEvent(event);
        ensureSelectOption(select, keyName, keyName.toUpperCase());
        select.value = keyName;
        button.classList.remove('recording');
        button.textContent = 'Record Key';
        activeRecorder = null;
        document.removeEventListener('keydown', finish, true);
      };
      document.addEventListener('keydown', finish, true);
    }

    function ensureSelectOption(select, value, label) {
      if ([...select.options].some(option => option.value === value)) return;
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    }

    function keyNameFromEvent(event) {
      const map = {
        ArrowLeft: 'left',
        ArrowRight: 'right',
        ArrowUp: 'up',
        ArrowDown: 'down',
        Enter: 'enter',
        Escape: 'escape',
        ' ': 'space',
        Spacebar: 'space',
        Tab: 'tab',
        Backspace: 'backspace',
        PageUp: 'page-up',
        PageDown: 'page-down',
        '=': '='
      };
      if (map[event.key]) return map[event.key];
      if (/^F([1-9]|1[0-2])$/.test(event.key)) return event.key.toLowerCase();
      if (/^[a-zA-Z0-9]$/.test(event.key)) return event.key.toLowerCase();
      return event.key.toLowerCase();
    }

    async function control(action, payload = {}) {
      try {
        const response = await fetch('/control?action=' + encodeURIComponent(action), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (data.error) throw new Error(data.error);
        if (data.state) setState(data.state);
      } catch (error) {
        document.getElementById('message').textContent = error.message || String(error);
      }
    }

    for (const button of document.querySelectorAll('[data-action]')) {
      button.addEventListener('click', () => control(button.dataset.action));
    }
    const ledButton = document.getElementById('led-test');
    let ledHeld = false;
    ledButton.addEventListener('pointerdown', event => {
      if (ledButton.disabled) return;
      event.preventDefault();
      if (ledButton.setPointerCapture) ledButton.setPointerCapture(event.pointerId);
      ledHeld = true;
      ledButton.classList.add('active');
      control('led', { enabled: true });
    });
    async function releaseLed() {
      if (!ledHeld) return;
      ledHeld = false;
      ledButton.classList.remove('active');
      await control('led', { enabled: false });
    }
    ledButton.addEventListener('pointerup', releaseLed);
    ledButton.addEventListener('pointercancel', releaseLed);
    ledButton.addEventListener('lostpointercapture', releaseLed);
    window.addEventListener('blur', releaseLed);
    document.getElementById('profile-select').addEventListener('change', event => {
      control('profile', { operation: 'switch', name: event.target.value });
    });
    document.getElementById('create-profile').addEventListener('click', () => {
      const input = document.getElementById('new-profile-name');
      control('profile', { operation: 'create', name: input.value });
      input.value = '';
    });
    document.getElementById('delete-profile').addEventListener('click', () => {
      if (state) control('profile', { operation: 'delete', name: state.active_profile });
    });
    document.getElementById('reset-profile').addEventListener('click', () => {
      control('profile', { operation: 'reset' });
    });
    document.getElementById('export-profiles').addEventListener('click', () => {
      window.location.href = '/profiles/export';
    });
    document.getElementById('import-profiles').addEventListener('click', () => {
      document.getElementById('import-profile-file').click();
    });
    document.getElementById('import-profile-file').addEventListener('change', async event => {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      try {
        const data = JSON.parse(await file.text());
        await control('profile', { operation: 'import', data });
      } catch (error) {
        document.getElementById('message').textContent = error.message || String(error);
      } finally {
        event.target.value = '';
      }
    });
    document.getElementById('reset-all-profiles').addEventListener('click', () => {
      if (window.confirm('Reset all profiles?')) control('profile', { operation: 'reset-all' });
    });
    document.getElementById('about-button').addEventListener('click', async () => {
      const dialog = document.getElementById('about-dialog');
      try {
        const response = await fetch('/about');
        const about = await response.json();
        document.getElementById('about-version').textContent = `${about.app_name} v${about.app_version}`;
        document.getElementById('about-credits').textContent = `Created by ${about.creator} (${about.website}).`;
        document.getElementById('about-license').textContent = `Open source under the ${about.license}.`;
        document.getElementById('about-config').textContent = about.config_path ? `Config: ${about.config_path}` : 'Config: default app data path';
      } catch (error) {
        document.getElementById('about-config').textContent = error.message || String(error);
      }
      if (dialog.showModal) dialog.showModal();
      else dialog.setAttribute('open', 'open');
    });
    document.getElementById('about-close').addEventListener('click', () => {
      const dialog = document.getElementById('about-dialog');
      if (dialog.close) dialog.close();
      else dialog.removeAttribute('open');
    });
    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[ch]));
    }

    const events = new EventSource('/events');
    events.onmessage = (message) => {
      const payload = JSON.parse(message.data);
      if (payload.type === 'state') setState(payload.state);
      if (payload.type === 'gesture' && state) {
        state.recent_events = [payload, ...state.recent_events].slice(0, 30);
        state.gesture_count += 1;
        if (payload.action_emitted) state.action_count += 1;
      }
      if (payload.type === 'sample' && state) {
        state.sample_count = payload.sample_count;
      }
    };
  </script>
</body>
</html>""".replace("__APP_VERSION__", APP_VERSION)


def build_debug_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TRIKI Diagnostics</title>
  <style>
    :root { color-scheme: dark; --bg: #101216; --panel: #171b22; --line: #333d4d; --text: #f2f6fb; --muted: #a9b5c5; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
    main { max-width: 1120px; margin: 0 auto; padding: 18px; display: grid; gap: 14px; }
    h1, h2 { margin: 0; }
    h1 { font-size: 22px; }
    h2 { font-size: 15px; }
    section { border: 1px solid var(--line); background: var(--panel); border-radius: 8px; padding: 14px; display: grid; gap: 10px; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
    .metric { color: var(--muted); }
    .metric strong { display: block; color: var(--text); font-size: 20px; }
    pre { margin: 0; overflow: auto; color: var(--text); background: #0b0d10; border: 1px solid var(--line); border-radius: 6px; padding: 10px; max-height: 320px; }
    @media (max-width: 760px) { .grid { grid-template-columns: 1fr 1fr; } }
  </style>
</head>
<body>
  <main>
    <h1>TRIKI Diagnostics</h1>
    <section>
      <h2>Live State</h2>
      <div class="grid">
        <div class="metric">Status<strong id="status">-</strong></div>
        <div class="metric">Samples<strong id="samples">0</strong></div>
        <div class="metric">Gestures<strong id="gestures">0</strong></div>
        <div class="metric">Actions<strong id="actions">0</strong></div>
      </div>
      <pre id="summary">{}</pre>
    </section>
    <section>
      <h2>connection_log</h2>
      <pre id="connection-log">[]</pre>
    </section>
    <section>
      <h2>recent_events</h2>
      <pre id="recent-events">[]</pre>
    </section>
  </main>
  <script>
    function render(state) {
      document.getElementById('status').textContent = state.status || '-';
      document.getElementById('samples').textContent = state.sample_count || 0;
      document.getElementById('gestures').textContent = state.gesture_count || 0;
      document.getElementById('actions').textContent = state.action_count || 0;
      document.getElementById('summary').textContent = JSON.stringify({
        message: state.message,
        button_hint: state.button_hint,
        output_enabled: state.output_enabled,
        active_profile: state.active_profile,
        profiles: state.profiles
      }, null, 2);
      document.getElementById('connection-log').textContent = JSON.stringify(state.connection_log || [], null, 2);
      document.getElementById('recent-events').textContent = JSON.stringify(state.recent_events || [], null, 2);
    }
    fetch('/state').then(response => response.json()).then(payload => render(payload.state));
    const events = new EventSource('/events');
    events.onmessage = message => {
      const payload = JSON.parse(message.data);
      if (payload.type === 'state') render(payload.state);
      if (payload.type === 'gesture') fetch('/state').then(response => response.json()).then(next => render(next.state));
    };
  </script>
</body>
</html>"""


def browser_url_for(host: str, port: int) -> str:
    display_host = host.strip("[]")
    if display_host in {"", "0.0.0.0", "::"}:
        display_host = "127.0.0.1"
    elif ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}/"


def schedule_browser_open(url: str, delay_seconds: float, opener=webbrowser.open) -> threading.Timer:
    def open_url() -> None:
        with contextlib.suppress(Exception):
            opener(url)

    timer = threading.Timer(max(0.0, delay_seconds), open_url)
    timer.daemon = True
    timer.start()
    return timer


def run_webview_window(url: str, *, webview_module=None, enable_tray: bool = True) -> None:
    if webview_module is None:
        import webview as webview_module

    window = webview_module.create_window(
        "TRIKI Control",
        url,
        width=1020,
        height=820,
        resizable=False,
        min_size=(760, 560),
    )
    if enable_tray and window is not None:
        TrayController(window, url=url).start()
    webview_module.start()


def write_console_line(message: str, stream=_DEFAULT_CONSOLE_STREAM) -> None:
    output = sys.stdout if stream is _DEFAULT_CONSOLE_STREAM else stream
    if output is None:
        return
    with contextlib.suppress(Exception):
        output.write(f"{message}\n")
        output.flush()


def write_log_line(path: Path | None, message: str) -> None:
    if path is None:
        return
    with contextlib.suppress(Exception):
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {message}\n")


def default_config_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        return base / "TRIKI" / "config.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "triki" / "config.json"


def default_log_path() -> Path:
    return default_config_path().with_name("app.log")


def build_arg_parser(*, default_ui: str = "browser") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the TRIKI background app and local config UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--config-path", type=Path, default=default_config_path())
    parser.add_argument("--log-path", type=Path, default=default_log_path())
    parser.add_argument("--ui", choices=("webview", "browser", "none"), default=default_ui)
    parser.add_argument("--open-browser", dest="ui", action="store_const", const="browser")
    parser.add_argument("--no-open-browser", dest="ui", action="store_const", const="none")
    parser.add_argument("--no-tray", action="store_true")
    parser.add_argument("--open-delay-seconds", type=float, default=0.75)
    parser.add_argument("--scan-seconds", type=float, default=10.0)
    parser.add_argument("--connect-attempts", type=int, default=5)
    parser.add_argument("--connect-mode", choices=("cached", "scan", "hybrid"), default="scan")
    parser.add_argument("--gatt-profile", choices=("auto", "nus-cached", "nus-uncached"), default="auto")
    parser.add_argument("--retry-delay-seconds", type=float, default=1.0)
    parser.add_argument("--reconnect-delay-seconds", type=float, default=1.0)
    parser.add_argument("--gatt-timeout-seconds", type=float, default=12.0)
    parser.add_argument("--auto-reconnect", action="store_true")
    parser.add_argument("--no-reconnect", action="store_true")
    parser.add_argument("--output-enabled", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--window-seconds", type=float, default=0.4)
    parser.add_argument("--min-samples", type=int, default=6)
    parser.add_argument("--min-confidence", type=float, default=0.55)
    parser.add_argument("--repeat-seconds", type=float, default=0.3)
    parser.add_argument("--warmup-seconds", type=float, default=0.05)
    parser.add_argument("--confirm-windows", type=int, default=1)
    return parser


def parse_args(argv: Sequence[str] | None = None, *, default_ui: str = "browser") -> argparse.Namespace:
    return build_arg_parser(default_ui=default_ui).parse_args(argv)


def main(argv: Sequence[str] | None = None, *, default_ui: str = "browser") -> int:
    args = parse_args(argv, default_ui=default_ui)
    write_log_line(
        args.log_path,
        (
            "START "
            f"host={args.host} port={args.port} "
            f"ui={args.ui} frozen={getattr(sys, 'frozen', False)}"
        ),
    )
    key_emitter = NullKeyEmitter() if args.dry_run else None
    config = load_config(args.config_path)
    if args.output_enabled:
        config.output_enabled = True
    session = AppSession(
        config=config,
        config_path=args.config_path,
        executor=ActionExecutor(key_emitter=key_emitter),
    )
    bus = EventBus()
    connection_control = ConnectionControl(
        manual_pairing=not args.auto_reconnect,
        auto_after_first_pairing=True,
    )
    command_bridge = BleCommandBridge()
    detector = LiveGestureDetector(
        window_seconds=args.window_seconds,
        min_samples=args.min_samples,
        min_confidence=args.min_confidence,
        repeat_seconds=args.repeat_seconds,
        warmup_seconds=args.warmup_seconds,
        confirm_windows=args.confirm_windows,
        suppress_labels=(
            "still",
            "unknown",
            "tap-single",
            "tap-double",
            "toss-catch",
            "rock-edge",
            "slide-back-forth",
            "twist-cw-ccw-cw-ccw",
            "twist-ccw-cw-ccw-cw",
        ),
    )
    server = AppHttpServer((args.host, args.port), session, bus, connection_control, command_bridge)
    url = browser_url_for(args.host, args.port)
    write_log_line(args.log_path, f"SERVER_READY url={url}")
    thread = threading.Thread(
        target=lambda: asyncio.run(
            run_ble_stream(
                session,
                bus,
                connection_control=connection_control,
                scan_seconds=args.scan_seconds,
                connect_attempts=args.connect_attempts,
                connect_mode=args.connect_mode,
                gatt_profile=args.gatt_profile,
                retry_delay_seconds=args.retry_delay_seconds,
                reconnect_forever=not args.no_reconnect,
                reconnect_delay_seconds=args.reconnect_delay_seconds,
                gatt_timeout_seconds=args.gatt_timeout_seconds,
                detector=detector,
                command_bridge=command_bridge,
            )
        ),
        daemon=True,
    )
    thread.start()
    write_log_line(args.log_path, "BLE_THREAD_STARTED")

    if args.ui == "webview":
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        write_console_line(f"OPEN {url}")
        write_log_line(args.log_path, f"WEBVIEW_START url={url}")
        try:
            run_webview_window(url, enable_tray=not args.no_tray)
        except Exception as exc:
            write_log_line(args.log_path, f"WEBVIEW_ERROR {type(exc).__name__}: {exc}")
            raise
        finally:
            server.shutdown()
            server.server_close()
            write_log_line(args.log_path, "SERVER_CLOSED")
        return 0

    if args.ui == "browser":
        schedule_browser_open(url, args.open_delay_seconds)
        write_log_line(args.log_path, f"BROWSER_OPEN_SCHEDULED delay={args.open_delay_seconds}")
    write_console_line(f"OPEN {url}")
    write_log_line(args.log_path, f"SERVE_FOREVER url={url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        write_log_line(default_log_path(), f"FATAL {type(exc).__name__}: {exc}")
        raise
