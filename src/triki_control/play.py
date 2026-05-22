from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
import contextlib
import json
import threading
import time
from collections.abc import Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bleak.exc import BleakCharacteristicNotFoundError, BleakDeviceNotFoundError

from triki_control.calibration_server import (
    ConnectionControl,
    EventBus,
    encode_sse,
    gatt_characteristic_missing_message,
    iter_connection_targets,
    quiet_stream_errors,
    session_exception_status_and_message,
    should_reconnect_after_session,
)
from triki_control.battery import BATTERY_LEVEL_UUID, battery_snapshot, normalize_battery_percent
from triki_control.key_emitter import DEFAULT_KEYMAP, KeyOutputController, NullKeyEmitter
from triki_control.live import LiveGestureDetector
from triki_control.probe import (
    LED_CHARACTERISTIC_UUID,
    START_STREAM_COMMAND,
    UART_RX_UUID,
    UART_TX_UUID,
    bleak_client_kwargs_for_profile,
    device_not_found_message,
    find_triki,
    hex_bytes,
)
from triki_control.protocol import MotionStreamParser

try:
    from bleak import BleakClient
except ImportError as exc:  # pragma: no cover - operator guidance
    raise SystemExit(
        "Missing bleak. Run: .\\.venv\\Scripts\\python.exe -m pip install bleak"
    ) from exc


def play_button_hint(status: str, message: str) -> str:
    normalized = f"{status} {message}".lower()
    if status == "waiting":
        return "Click Press pairing now, then press the physical TRIKI button once."
    if status == "pairing":
        return "Press the physical TRIKI button once now, then stop pressing."
    if "connecting gatt" in normalized or status in {"connected", "ready"}:
        return "Do not press the pairing button during GATT/UART."
    return "Keep TRIKI close to the Bluetooth adapter."


def disconnect_message(uptime_seconds: float | None) -> str:
    if uptime_seconds is None:
        return "TRIKI disconnected before play stream started."
    return (
        f"TRIKI disconnected after {uptime_seconds:.1f}s; keep it close to the "
        "Bluetooth adapter and avoid covering it with your hand."
    )


async def write_led_state(client, enabled: bool) -> None:
    payload = b"\x01" if enabled else b"\x00"
    await client.write_gatt_char(LED_CHARACTERISTIC_UUID, payload, response=True)


class BleCommandBridge:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client = None

    def attach(self, client, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            self._client = client
            self._loop = loop

    def detach(self, client=None) -> None:
        with self._lock:
            if client is not None and client is not self._client:
                return
            self._client = None
            self._loop = None

    def set_led(self, enabled: bool, *, timeout_seconds: float = 1.5) -> None:
        with self._lock:
            client = self._client
            loop = self._loop
        if client is None or loop is None or not loop.is_running():
            raise RuntimeError("TRIKI is not connected.")
        if not getattr(client, "is_connected", False):
            raise RuntimeError("TRIKI is not connected.")
        future = asyncio.run_coroutine_threadsafe(write_led_state(client, enabled), loop)
        try:
            future.result(timeout_seconds)
        except FutureTimeoutError as exc:
            future.cancel()
            raise TimeoutError("Timed out writing TRIKI LED state.") from exc


class PlaySession:
    def __init__(
        self,
        *,
        output: KeyOutputController | None = None,
        keymap: dict[str, str | None] | None = None,
    ) -> None:
        self.output = output or KeyOutputController(keymap=keymap)
        self._lock = threading.RLock()
        self.status = "idle"
        self.message = "Play mode ready."
        self._status_sequence = 0
        self._sample_count = 0
        self._gesture_count = 0
        self._key_count = 0
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
            self.output.set_enabled(enabled)
            return self.snapshot()

    def update_mapping(self, gesture_label: str, key_name: str | None) -> dict:
        with self._lock:
            self.output.set_mapping(gesture_label, key_name)
            return self.snapshot()

    def clear_events(self) -> dict:
        with self._lock:
            self._recent_events.clear()
            self._gesture_count = 0
            self._key_count = 0
            return self.snapshot()

    def record_sample(self) -> dict:
        with self._lock:
            self._sample_count += 1
            return {
                "type": "sample",
                "sample_count": self._sample_count,
            }

    def record_prediction(self, elapsed_seconds: float, prediction) -> dict:
        with self._lock:
            result = self.output.handle_gesture(prediction.label)
            self._gesture_count += 1
            if result.emitted:
                self._key_count += 1
            event = {
                "type": "gesture",
                "elapsed_seconds": round(elapsed_seconds, 6),
                "gesture_label": result.gesture_label,
                "confidence": round(prediction.confidence, 3),
                "reason": prediction.reason,
                "key_name": result.key_name,
                "key_emitted": result.emitted,
                "output_enabled": self.output.enabled,
                "output_reason": result.reason,
                "features": {
                    "gyro_p99": round(prediction.features.gyro_p99, 3),
                    "accel_deviation_p99": round(
                        prediction.features.accel_deviation_p99,
                        3,
                    ),
                    "c_mean": round(prediction.features.c_mean, 3),
                    "c_abs_p99": round(prediction.features.c_abs_p99, 3),
                    "lateral_gyro_p99": round(
                        prediction.features.lateral_gyro_p99,
                        3,
                    ),
                    "lateral_accel_p99": round(
                        prediction.features.lateral_accel_p99,
                        3,
                    ),
                    "orientation_angle_degrees": round(
                        prediction.features.orientation_angle_degrees,
                        3,
                    ),
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
                "button_hint": play_button_hint(self.status, self.message),
                "output_enabled": self.output.enabled,
                "sample_count": self._sample_count,
                "gesture_count": self._gesture_count,
                "key_count": self._key_count,
                "battery": battery_snapshot(self._battery_percent, self._battery_message),
                "keymap": [
                    {
                        "gesture_label": gesture,
                        "key_name": key,
                        "enabled": key is not None,
                    }
                    for gesture, key in self.output.keymap.items()
                ],
                "recent_events": list(reversed(self._recent_events[-30:])),
                "connection_log": list(reversed(self._connection_log[-30:])),
            }


def handle_control(
    session: PlaySession,
    action: str,
    payload: dict,
    *,
    bus: EventBus,
    connection_control: ConnectionControl,
) -> dict:
    if action == "pairing":
        return connection_control.request_pairing(session, bus)
    if action == "output":
        return session.set_output_enabled(bool(payload.get("enabled")))
    if action == "clear":
        return session.clear_events()
    if action == "map":
        key_name = payload.get("key_name")
        if key_name == "":
            key_name = None
        return session.update_mapping(str(payload["gesture_label"]), key_name)
    raise ValueError(f"unknown control action: {action}")


async def publish_battery_level(
    session,
    bus: EventBus,
    client,
    *,
    timeout_seconds: float = 1.5,
) -> None:
    set_battery_level = getattr(session, "set_battery_level", None)
    if set_battery_level is None:
        return
    try:
        payload = await asyncio.wait_for(
            client.read_gatt_char(BATTERY_LEVEL_UUID),
            timeout=timeout_seconds,
        )
        if not payload:
            raise ValueError("empty battery payload")
        state = set_battery_level(int(payload[0]), "Battery level read from BLE.")
    except Exception as exc:
        state = set_battery_level(
            None,
            f"Battery level unavailable: {type(exc).__name__}: {exc}",
        )
    bus.publish({"type": "state", "state": state})


class PlayHttpHandler(BaseHTTPRequestHandler):
    server: PlayHttpServer

    def log_message(self, format, *args) -> None:  # noqa: A002
        if self.path == "/favicon.ico":
            return
        super().log_message(format, *args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_text(build_html(), "text/html; charset=utf-8")
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

    def _send_text(self, body: str, content_type: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class PlayHttpServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        session: PlaySession,
        bus: EventBus,
        connection_control: ConnectionControl,
    ) -> None:
        super().__init__(server_address, PlayHttpHandler)
        self.session = session
        self.bus = bus
        self.connection_control = connection_control


async def run_ble_session(
    session: PlaySession,
    bus: EventBus,
    device,
    *,
    connect_mode: str,
    gatt_profile: str,
    gatt_timeout_seconds: float,
    detector: LiveGestureDetector,
    command_bridge: BleCommandBridge | None = None,
) -> int:
    parser = MotionStreamParser()
    started_at = 0.0
    event_count = 0
    last_sample_publish = 0.0
    uart_ready = False
    disconnected = asyncio.Event()

    def publish_state(status: str, message: str) -> None:
        bus.publish({"type": "state", "state": session.set_status(status, message)})

    def on_disconnect(_client) -> None:
        disconnected.set()
        elapsed = None if started_at <= 0 else time.monotonic() - started_at
        publish_state("disconnected", disconnect_message(elapsed))

    def on_notify(_sender, data: bytearray) -> None:
        nonlocal event_count, last_sample_publish
        elapsed = time.monotonic() - started_at
        for sample in parser.feed(bytes(data)):
            sample_payload = session.record_sample()
            event = detector.add_sample(elapsed, sample)
            if elapsed - last_sample_publish >= 0.25:
                bus.publish(sample_payload)
                bus.publish({"type": "state", "state": session.snapshot()})
                last_sample_publish = elapsed
            if event is None:
                continue
            event_count += 1
            bus.publish(session.record_prediction(elapsed, event))
            bus.publish({"type": "state", "state": session.snapshot()})

    publish_state(
        "connecting",
        (
            f"Connecting GATT mode={connect_mode} profile={gatt_profile} "
            f"timeout={gatt_timeout_seconds:.1f}s."
        ),
    )
    try:
        async with BleakClient(
            device,
            timeout=gatt_timeout_seconds,
            disconnected_callback=on_disconnect,
            **bleak_client_kwargs_for_profile(gatt_profile),
        ) as client:
            notify_started = False
            if command_bridge is not None:
                command_bridge.attach(client, asyncio.get_running_loop())
            try:
                publish_state("connected", "GATT connected; enabling UART notifications.")
                await publish_battery_level(session, bus, client)
                await client.start_notify(UART_TX_UUID, on_notify)
                notify_started = True
                uart_ready = True
                publish_state("ready", f"UART_READY; play mode active. start={hex_bytes(START_STREAM_COMMAND)}")
                await client.write_gatt_char(UART_RX_UUID, START_STREAM_COMMAND, response=False)
                started_at = time.monotonic()
                while not disconnected.is_set():
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(disconnected.wait(), timeout=0.25)
            finally:
                if notify_started and client.is_connected:
                    with contextlib.suppress(Exception):
                        await client.stop_notify(UART_TX_UUID)
                if command_bridge is not None:
                    command_bridge.detach(client)
    except OSError as exc:
        if disconnected.is_set() and not uart_ready:
            publish_state("retrying", f"DISCONNECTED_BEFORE_UART_READY {type(exc).__name__}: {exc}")
        raise
    return 0 if event_count else 2


async def run_ble_stream(
    session: PlaySession,
    bus: EventBus,
    *,
    connection_control: ConnectionControl,
    scan_seconds: float,
    connect_attempts: int,
    connect_mode: str,
    gatt_profile: str,
    retry_delay_seconds: float,
    reconnect_forever: bool,
    reconnect_delay_seconds: float,
    gatt_timeout_seconds: float,
    detector: LiveGestureDetector,
    command_bridge: BleCommandBridge | None = None,
) -> int:
    last_status = 1
    targets = iter_connection_targets(connect_mode, gatt_profile)
    reconnect_cycle = 0
    while True:
        await connection_control.wait_for_pairing_request(session, bus)
        reconnect_cycle += 1
        restart_after_disconnect = False
        for attempt in range(1, connect_attempts + 1):
            mode_devices: dict[str, object | None] = {}
            for target_index, (mode, profile) in enumerate(targets, start=1):
                session.set_status(
                    "connecting",
                    (
                        f"BLE cycle {reconnect_cycle}, attempt "
                        f"{attempt}/{connect_attempts}, target "
                        f"{target_index}/{len(targets)}, mode {mode}, "
                        f"profile {profile}"
                    ),
                )
                bus.publish({"type": "state", "state": session.snapshot()})
                if mode not in mode_devices:
                    mode_devices[mode] = await find_triki(scan_seconds, mode)
                device = mode_devices[mode]
                if device is None:
                    last_status = 1
                    continue
                detector.reset()
                try:
                    status = await run_ble_session(
                        session,
                        bus,
                        device,
                        connect_mode=mode,
                        gatt_profile=profile,
                        gatt_timeout_seconds=gatt_timeout_seconds,
                        detector=detector,
                        command_bridge=command_bridge,
                    )
                except BleakDeviceNotFoundError:
                    session.set_status("retrying", device_not_found_message(mode))
                    bus.publish({"type": "state", "state": session.snapshot()})
                    last_status = 1
                except BleakCharacteristicNotFoundError:
                    session.set_status("retrying", gatt_characteristic_missing_message(profile, mode))
                    bus.publish({"type": "state", "state": session.snapshot()})
                    last_status = 3
                except TimeoutError as exc:
                    session.set_status(
                        "retrying",
                        f"CONNECT_TIMEOUT {type(exc).__name__} profile={profile} mode={mode}",
                    )
                    bus.publish({"type": "state", "state": session.snapshot()})
                    last_status = 3
                except Exception as exc:
                    status_label, message = session_exception_status_and_message(exc, profile, mode)
                    session.set_status(status_label, message)
                    bus.publish({"type": "state", "state": session.snapshot()})
                    last_status = 3
                else:
                    last_status = status
                    if not should_reconnect_after_session(
                        status,
                        reconnect_forever=reconnect_forever,
                    ):
                        return status
                    if connection_control.manual_pairing:
                        restart_after_disconnect = True
                        break
                    session.set_status(
                        "reconnecting",
                        f"BLE disconnected; reconnecting in {reconnect_delay_seconds:.1f}s.",
                    )
                    bus.publish({"type": "state", "state": session.snapshot()})
                    if reconnect_delay_seconds > 0:
                        await asyncio.sleep(reconnect_delay_seconds)
                    restart_after_disconnect = True
                    break
                has_more_targets = target_index < len(targets)
                has_more_attempts = attempt < connect_attempts
                if (has_more_targets or has_more_attempts) and retry_delay_seconds > 0:
                    await asyncio.sleep(retry_delay_seconds)
                if restart_after_disconnect:
                    break
            if restart_after_disconnect:
                break
        if restart_after_disconnect:
            continue
        if not reconnect_forever:
            return last_status
        if connection_control.manual_pairing:
            continue
        session.set_status(
            "reconnecting",
            f"BLE unavailable after {connect_attempts} attempts; retrying in {reconnect_delay_seconds:.1f}s.",
        )
        bus.publish({"type": "state", "state": session.snapshot()})
        if reconnect_delay_seconds > 0:
            await asyncio.sleep(reconnect_delay_seconds)


def build_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TRIKI Play Mode</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0f1115;
      --panel: #171c24;
      --soft: #202735;
      --line: #323b4a;
      --text: #f2f6fb;
      --muted: #a8b3c2;
      --ok: #73d99f;
      --bad: #ff897d;
      --warn: #f2c166;
      --blue: #89b4ff;
      --blue-solid: #386fd6;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      letter-spacing: 0;
    }
    main {
      max-width: 1180px;
      margin: 0 auto;
      padding: 18px;
      display: grid;
      gap: 14px;
    }
    h1, h2 { margin: 0; line-height: 1.2; }
    h1 { font-size: 22px; }
    h2 { font-size: 17px; }
    .top, .panel {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      padding: 14px;
    }
    .top {
      display: grid;
      gap: 12px;
    }
    .row {
      display: flex;
      gap: 10px;
      align-items: center;
      flex-wrap: wrap;
    }
    .pill {
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--soft);
      color: var(--muted);
      padding: 5px 10px;
      font-size: 13px;
    }
    .pill.ready { border-color: var(--ok); color: var(--ok); }
    .pill.waiting, .pill.connecting, .pill.connected, .pill.pairing { border-color: var(--warn); color: var(--warn); }
    .pill.error, .pill.disconnected { border-color: var(--bad); color: var(--bad); }
    .pill.output-on { border-color: var(--bad); color: var(--bad); }
    button, select {
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--soft);
      color: var(--text);
      padding: 8px 12px;
      font-size: 14px;
    }
    button { cursor: pointer; }
    button.primary { background: var(--blue-solid); border-color: var(--blue-solid); color: white; }
    button.danger { background: #7f1d1d; border-color: #b91c1c; color: white; }
    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(310px, 0.9fr);
      gap: 14px;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(3, minmax(120px, 1fr));
      gap: 10px;
    }
    .metric {
      border: 1px solid var(--line);
      background: var(--soft);
      border-radius: 8px;
      padding: 12px;
    }
    .metric span { display: block; color: var(--muted); font-size: 12px; margin-bottom: 5px; }
    .metric strong { font-size: 24px; }
    .feed, .keymap, .connection-log {
      display: grid;
      gap: 8px;
      margin-top: 12px;
    }
    .event, .map-row, .connection-row {
      border: 1px solid var(--line);
      background: var(--soft);
      border-radius: 8px;
      padding: 10px;
      display: grid;
      gap: 4px;
      font-size: 14px;
    }
    .event {
      grid-template-columns: auto 1fr auto;
      align-items: start;
      gap: 10px;
    }
    .event .tag.emit { color: var(--ok); }
    .event .tag.block { color: var(--muted); }
    .detail { color: var(--muted); font-size: 12px; }
    .map-row {
      grid-template-columns: minmax(110px, 1fr) minmax(120px, 0.8fr);
      align-items: center;
    }
    @media (max-width: 860px) {
      .grid, .metrics { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <main>
    <section class="top">
      <div class="row">
        <h1>TRIKI Play Mode</h1>
        <span class="pill" id="status">loading</span>
        <span class="pill" id="output">output off</span>
      </div>
      <div class="row">
        <span class="pill" id="message">Waiting for state</span>
        <span class="pill" id="hint">Pairing button: wait.</span>
        <span class="pill" id="focus-hint">Focus the game window after enabling output</span>
      </div>
      <div class="row">
        <button class="primary" data-action="pairing">Press pairing now</button>
        <button id="output-toggle" class="danger" type="button">Enable key output</button>
        <button data-action="clear">Clear events</button>
      </div>
    </section>
    <section class="metrics">
      <div class="metric"><span>Samples</span><strong id="samples">0</strong></div>
      <div class="metric"><span>Gestures</span><strong id="gestures">0</strong></div>
      <div class="metric"><span>Keys Sent</span><strong id="keys">0</strong></div>
    </section>
    <section class="grid">
      <div class="panel">
        <h2>Recent Output</h2>
        <div class="feed" id="feed"></div>
      </div>
      <div class="panel">
        <h2>Keymap</h2>
        <div class="keymap" id="keymap"></div>
        <h2 style="margin-top:16px">Connection</h2>
        <div class="connection-log" id="connection-log"></div>
      </div>
    </section>
  </main>
  <script>
    let state = null;
    const keyChoices = ['', 'left', 'right', 'enter', 'space', 'escape', 'up', 'down'];

    function setState(next) {
      state = next;
      const status = document.getElementById('status');
      status.textContent = state.status;
      status.className = 'pill ' + (state.status || '');
      document.getElementById('message').textContent = state.message;
      document.getElementById('hint').textContent = state.button_hint || '';
      const output = document.getElementById('output');
      output.textContent = state.output_enabled ? 'output on' : 'output off';
      output.className = 'pill ' + (state.output_enabled ? 'output-on' : '');
      const toggle = document.getElementById('output-toggle');
      toggle.textContent = state.output_enabled ? 'Disable key output' : 'Enable key output';
      toggle.className = state.output_enabled ? 'danger' : 'primary';
      document.getElementById('samples').textContent = state.sample_count;
      document.getElementById('gestures').textContent = state.gesture_count;
      document.getElementById('keys').textContent = state.key_count;
      renderFeed();
      renderKeymap();
      renderConnectionLog();
    }

    function renderFeed() {
      const root = document.getElementById('feed');
      root.innerHTML = '';
      for (const event of state.recent_events || []) {
        const item = document.createElement('div');
        item.className = 'event';
        item.innerHTML = `
          <strong class="tag ${event.key_emitted ? 'emit' : 'block'}">${event.key_emitted ? 'sent' : 'debug'}</strong>
          <div>
            <div>${escapeHtml(event.gesture_label)} -> ${escapeHtml(event.key_name || 'unmapped')} (${event.confidence})</div>
            <div class="detail">${escapeHtml(event.output_reason)} | ${escapeHtml(event.reason)} | gyro=${event.features.gyro_p99} | accel=${event.features.accel_deviation_p99}</div>
          </div>
          <span>${Number(event.elapsed_seconds).toFixed(2)}s</span>`;
        root.appendChild(item);
      }
    }

    function renderKeymap() {
      const root = document.getElementById('keymap');
      root.innerHTML = '';
      for (const item of state.keymap || []) {
        const row = document.createElement('div');
        row.className = 'map-row';
        const select = document.createElement('select');
        for (const key of keyChoices) {
          const option = document.createElement('option');
          option.value = key;
          option.textContent = key || 'debug only';
          option.selected = (item.key_name || '') === key;
          select.appendChild(option);
        }
        select.addEventListener('change', () => control('map', {
          gesture_label: item.gesture_label,
          key_name: select.value
        }));
        row.innerHTML = `<strong>${escapeHtml(item.gesture_label)}</strong>`;
        row.appendChild(select);
        root.appendChild(row);
      }
    }

    function renderConnectionLog() {
      const root = document.getElementById('connection-log');
      root.innerHTML = '';
      for (const item of state.connection_log || []) {
        const row = document.createElement('div');
        row.className = 'connection-row';
        row.innerHTML = `<strong>${escapeHtml(item.status)}</strong><span class="detail">${escapeHtml(item.timestamp)} | ${escapeHtml(item.message)}</span>`;
        root.appendChild(row);
      }
    }

    async function control(action, payload = {}) {
      const response = await fetch('/control?action=' + encodeURIComponent(action), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      const data = await response.json();
      if (data.state) setState(data.state);
    }

    for (const button of document.querySelectorAll('[data-action]')) {
      button.addEventListener('click', () => control(button.dataset.action));
    }
    document.getElementById('output-toggle').addEventListener('click', () => {
      control('output', { enabled: !state.output_enabled });
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
        renderFeed();
      }
      if (payload.type === 'sample' && state) {
        state.sample_count = payload.sample_count;
        document.getElementById('samples').textContent = state.sample_count;
      }
    };
  </script>
</body>
</html>"""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve TRIKI play mode with Windows key output.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
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


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_arg_parser().parse_args(argv)


def main() -> int:
    args = parse_args()
    emitter = NullKeyEmitter() if args.dry_run else None
    output = KeyOutputController(keymap=DEFAULT_KEYMAP, emitter=emitter, enabled=args.output_enabled)
    session = PlaySession(output=output)
    bus = EventBus()
    connection_control = ConnectionControl(manual_pairing=not args.auto_reconnect)
    suppress_labels = {
        "unknown",
        "still",
        "tap-single",
        "tap-double",
        "toss-catch",
        "rock-edge",
        "slide-back-forth",
        "twist-cw-ccw-cw-ccw",
        "twist-ccw-cw-ccw-cw",
    }
    detector = LiveGestureDetector(
        window_seconds=args.window_seconds,
        min_samples=args.min_samples,
        min_confidence=args.min_confidence,
        repeat_seconds=args.repeat_seconds,
        warmup_seconds=args.warmup_seconds,
        confirm_windows=args.confirm_windows,
        suppress_labels=tuple(sorted(suppress_labels)),
    )
    server = PlayHttpServer((args.host, args.port), session, bus, connection_control)
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
            )
        ),
        daemon=True,
    )
    thread.start()
    print(f"OPEN http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
