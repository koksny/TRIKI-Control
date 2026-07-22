from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import queue
import threading
import time
from collections.abc import Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bleak.exc import BleakCharacteristicNotFoundError, BleakDeviceNotFoundError

from triki_calibration import CalibrationRecorder, CalibrationSession, DEFAULT_ACTIONS
from triki_live import LiveGestureDetector
from triki_probe import (
    GATT_PROFILE_NUS_CACHED,
    START_STREAM_COMMAND,
    UART_RX_UUID,
    UART_TX_UUID,
    bleak_client_kwargs_for_profile,
    device_not_found_message,
    find_triki,
    hex_bytes,
    iter_gatt_profiles,
)
from triki_protocol import MotionStreamParser

try:
    from bleak import BleakClient
except ImportError as exc:  # pragma: no cover - operator guidance
    raise SystemExit(
        "Missing bleak. Run: .\\.venv\\Scripts\\python.exe -m pip install bleak"
    ) from exc


# Control payloads are small JSON; bound the request body so a bogus or hostile
# Content-Length cannot make the handler read megabytes.
MAX_CONTROL_BODY_BYTES = 256 * 1024


class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[str]] = []

    def subscribe(self) -> queue.Queue[str]:
        subscriber: queue.Queue[str] = queue.Queue()
        with self._lock:
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue[str]) -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def publish(self, payload: dict) -> None:
        encoded = encode_sse(payload)
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.put(encoded)


def encode_sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


class ConnectionControl:
    def __init__(
        self,
        *,
        manual_pairing: bool,
        auto_after_first_pairing: bool = False,
    ) -> None:
        self.manual_pairing = manual_pairing
        self.auto_after_first_pairing = auto_after_first_pairing
        self._auto_reconnect_enabled = False
        self._pairing_requested = threading.Event()
        self._shutdown_requested = threading.Event()

    def is_pairing_requested(self) -> bool:
        return self._pairing_requested.is_set()

    def is_shutdown_requested(self) -> bool:
        return self._shutdown_requested.is_set()

    def request_shutdown(self) -> None:
        self._shutdown_requested.set()
        self._pairing_requested.set()

    def request_pairing(self, session: CalibrationSession, bus: EventBus) -> dict:
        self._pairing_requested.set()
        if self.auto_after_first_pairing:
            self._auto_reconnect_enabled = True
        state = session.set_status(
            "pairing",
            "Press the TRIKI pairing button now; BLE scan is starting.",
        )
        bus.publish({"type": "state", "state": state})
        return state

    def request_disconnect(self, session: CalibrationSession, bus: EventBus) -> dict:
        self._auto_reconnect_enabled = False
        self._pairing_requested.clear()
        state = session.set_status(
            "disconnecting",
            "Disconnecting TRIKI; automatic reconnect is paused.",
        )
        bus.publish({"type": "state", "state": state})
        return state

    def cancel_disconnect(self) -> None:
        if self.auto_after_first_pairing:
            self._auto_reconnect_enabled = True

    async def wait_for_pairing_request(
        self,
        session: CalibrationSession,
        bus: EventBus,
    ) -> None:
        if self.is_shutdown_requested():
            return
        if not self.manual_pairing:
            return
        if self.auto_after_first_pairing and self._auto_reconnect_enabled:
            return
        state = session.set_status(
            "waiting",
            (
                "BLE reconnect is paused. Click Press pairing now when TRIKI "
                "is next to the adapter."
            ),
        )
        bus.publish({"type": "state", "state": state})
        while not self._pairing_requested.is_set():
            if self.is_shutdown_requested():
                return
            await asyncio.sleep(0.05)
        if self.is_shutdown_requested():
            return
        self._pairing_requested.clear()
        if self.auto_after_first_pairing:
            self._auto_reconnect_enabled = True


def handle_control(session: CalibrationSession, action: str, payload: dict) -> dict:
    if action == "next":
        return session.next_action()
    if action == "previous":
        return session.previous_action()
    if action == "reset":
        return session.reset_counts()
    if action == "select":
        return session.select_action(int(payload.get("index", 0)))
    if action == "save":
        if session.recorder is not None:
            session.recorder.write_summary(session.snapshot())
        return session.snapshot()
    raise ValueError(f"unknown control action: {action}")


def handle_pairing_request(
    session: CalibrationSession,
    bus: EventBus,
    connection_control: ConnectionControl,
) -> dict:
    return connection_control.request_pairing(session, bus)


def is_quiet_404(path: str) -> bool:
    return path == "/favicon.ico"


def quiet_stream_errors() -> tuple[type[OSError], ...]:
    return (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)


def should_reconnect_after_session(status: int, *, reconnect_forever: bool) -> bool:
    return reconnect_forever and status in {0, 2, 3}


def iter_connect_modes(connect_mode: str) -> tuple[str, ...]:
    if connect_mode == "hybrid":
        return ("cached", "scan")
    if connect_mode in {"cached", "scan"}:
        return (connect_mode,)
    raise ValueError(f"unknown connect mode: {connect_mode}")


def iter_connection_targets(
    connect_mode: str,
    gatt_profile: str,
) -> tuple[tuple[str, str], ...]:
    modes = iter_connect_modes(connect_mode)
    if gatt_profile != "auto":
        profiles = iter_gatt_profiles(gatt_profile)
        return tuple((mode, profile) for mode in modes for profile in profiles)

    targets: list[tuple[str, str]] = []
    for mode in modes:
        if mode == "cached":
            targets.append((mode, GATT_PROFILE_NUS_CACHED))
        else:
            targets.extend((mode, profile) for profile in iter_gatt_profiles("auto"))
    return tuple(targets)


def gatt_characteristic_missing_message(gatt_profile: str, connect_mode: str) -> str:
    return (
        "GATT_PROFILE_STALE "
        f"profile={gatt_profile} mode={connect_mode}: connected to TRIKI, but "
        "Windows did not expose the NUS TX characteristic. Keep TRIKI close, "
        "do not press the pairing button during GATT, and let the retry loop "
        "rediscover the profile."
    )


def gatt_dropped_before_uart_message(
    gatt_profile: str,
    connect_mode: str,
    exc: BaseException,
) -> str:
    return (
        "GATT_DROPPED_BEFORE_UART_READY "
        f"profile={gatt_profile} mode={connect_mode}: Windows may still show "
        "Connected, but this tool did not reach UART_READY on the TRIKI NUS "
        f"channel. {type(exc).__name__}: {exc}. Keep TRIKI close, stop pressing "
        "the pairing button during GATT, and let the retry loop continue."
    )


def session_exception_status_and_message(
    exc: BaseException,
    gatt_profile: str,
    connect_mode: str,
) -> tuple[str, str]:
    if isinstance(exc, OSError):
        return "retrying", gatt_dropped_before_uart_message(
            gatt_profile,
            connect_mode,
            exc,
        )
    return (
        "error",
        (
            f"SESSION_ERROR {type(exc).__name__} "
            f"profile={gatt_profile} mode={connect_mode}: {exc}"
        ),
    )


def disconnect_message(action_title: str, elapsed_seconds: float | None = None) -> str:
    elapsed = ""
    if elapsed_seconds is not None:
        elapsed = f" after {elapsed_seconds:.1f}s"
    return (
        f"TRIKI disconnected during {action_title}{elapsed}; keep it closer to "
        "the Bluetooth adapter and avoid covering it with your hand."
    )


class CalibrationHttpHandler(BaseHTTPRequestHandler):
    server: CalibrationHttpServer

    def log_message(self, format, *args) -> None:  # noqa: A002
        return

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
        if is_quiet_404(parsed.path):
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/control":
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
            return
        query = parse_qs(parsed.query)
        action = query.get("action", [""])[0]
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except (TypeError, ValueError):
            self._send_json({"type": "error", "message": "invalid Content-Length"}, HTTPStatus.BAD_REQUEST)
            return
        if length < 0:
            self._send_json({"type": "error", "message": "invalid Content-Length"}, HTTPStatus.BAD_REQUEST)
            return
        if length > MAX_CONTROL_BODY_BYTES:
            self.send_response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            self.send_header("Connection", "close")
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.close_connection = True
            return
        payload = self._read_json_body()
        try:
            if action == "pairing":
                state = handle_pairing_request(
                    self.server.session,
                    self.server.bus,
                    self.server.connection_control,
                )
            else:
                state = handle_control(self.server.session, action, payload)
        except (ValueError, TypeError) as exc:
            self._send_json({"type": "error", "message": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.server.bus.publish({"type": "state", "state": state})
        self._send_json({"type": "state", "state": state})

    def _read_json_body(self) -> dict:
        length = max(0, int(self.headers.get("Content-Length") or "0"))
        if not length:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8"))

    def _stream_events(self) -> None:
        subscriber = self.server.bus.subscribe()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.wfile.write(
                encode_sse(
                    {"type": "state", "state": self.server.session.snapshot()}
                ).encode("utf-8")
            )
            self.wfile.flush()
            while True:
                try:
                    payload = subscriber.get(timeout=15)
                except queue.Empty:
                    payload = ": keep-alive\n\n"
                self.wfile.write(payload.encode("utf-8"))
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


class CalibrationHttpServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        session: CalibrationSession,
        bus: EventBus,
        connection_control: ConnectionControl,
    ) -> None:
        super().__init__(server_address, CalibrationHttpHandler)
        self.session = session
        self.bus = bus
        self.connection_control = connection_control


async def run_ble_stream(
    session: CalibrationSession,
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
) -> int:
    last_status = 1
    targets = iter_connection_targets(connect_mode, gatt_profile)
    reconnect_cycle = 0

    while True:
        await connection_control.wait_for_pairing_request(session, bus)
        if connection_control.is_shutdown_requested():
            return 0
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
                    )
                except BleakDeviceNotFoundError:
                    message = device_not_found_message(mode)
                    session.set_status("retrying", message)
                    bus.publish({"type": "state", "state": session.snapshot()})
                    last_status = 1
                except BleakCharacteristicNotFoundError:
                    message = gatt_characteristic_missing_message(profile, mode)
                    session.set_status("retrying", message)
                    bus.publish({"type": "state", "state": session.snapshot()})
                    last_status = 3
                except TimeoutError as exc:
                    message = f"CONNECT_TIMEOUT {type(exc).__name__} profile={profile} mode={mode}"
                    session.set_status("retrying", message)
                    bus.publish({"type": "state", "state": session.snapshot()})
                    last_status = 3
                except OSError as exc:
                    status_label, message = session_exception_status_and_message(
                        exc,
                        profile,
                        mode,
                    )
                    session.set_status(status_label, message)
                    bus.publish({"type": "state", "state": session.snapshot()})
                    last_status = 3
                except Exception as exc:
                    status_label, message = session_exception_status_and_message(
                        exc,
                        profile,
                        mode,
                    )
                    session.set_status(status_label, message)
                    bus.publish({"type": "state", "state": session.snapshot()})
                    last_status = 3
                else:
                    last_status = status
                    if session.recorder is not None:
                        session.recorder.write_summary(session.snapshot())
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
                if (
                    has_more_targets
                    or has_more_attempts
                ) and retry_delay_seconds > 0:
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
            if session.recorder is not None:
                session.recorder.write_summary(session.snapshot())
            continue

        if session.recorder is not None:
            session.recorder.write_summary(session.snapshot())
        session.set_status(
            "reconnecting",
            f"BLE unavailable after {connect_attempts} attempts; retrying in {reconnect_delay_seconds:.1f}s.",
        )
        bus.publish({"type": "state", "state": session.snapshot()})
        if reconnect_delay_seconds > 0:
            await asyncio.sleep(reconnect_delay_seconds)


async def run_ble_session(
    session: CalibrationSession,
    bus: EventBus,
    device,
    *,
    connect_mode: str,
    gatt_profile: str,
    gatt_timeout_seconds: float,
    detector: LiveGestureDetector,
) -> int:
    parser = MotionStreamParser()
    started_at = 0.0
    event_count = 0
    uart_ready = False
    last_sample_publish = 0.0
    disconnected = asyncio.Event()

    def publish_state(status: str, message: str) -> None:
        bus.publish({"type": "state", "state": session.set_status(status, message)})

    def on_disconnect(_client) -> None:
        disconnected.set()
        action = session.current_action
        elapsed = None if started_at <= 0 else time.monotonic() - started_at
        publish_state("disconnected", disconnect_message(action.title, elapsed))

    def on_notify(_sender, data: bytearray) -> None:
        nonlocal event_count, last_sample_publish
        elapsed = time.monotonic() - started_at
        for sample in parser.feed(bytes(data)):
            sample_payload = session.record_sample(elapsed, sample)
            event = detector.add_sample(elapsed, sample)
            if elapsed - last_sample_publish >= 0.10:
                bus.publish(sample_payload)
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
            publish_state("connected", "GATT connected; enabling UART notifications.")
            await client.start_notify(UART_TX_UUID, on_notify)
            uart_ready = True
            publish_state("ready", f"UART_READY; start calibration. start={hex_bytes(START_STREAM_COMMAND)}")
            await client.write_gatt_char(UART_RX_UUID, START_STREAM_COMMAND, response=False)
            started_at = time.monotonic()
            while not disconnected.is_set():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(disconnected.wait(), timeout=0.25)
            if client.is_connected:
                with contextlib.suppress(Exception):
                    await client.stop_notify(UART_TX_UUID)
    except OSError as exc:
        if disconnected.is_set() and not uart_ready:
            publish_state(
                "retrying",
                gatt_dropped_before_uart_message(gatt_profile, connect_mode, exc),
            )
        raise
    return 0 if event_count else 2


def build_html() -> str:
    return HTML


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TRIKI Calibration</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0f1115;
      --side: #151920;
      --panel: #1a1f27;
      --panel-soft: #202632;
      --line: #343c49;
      --text: #f1f5f9;
      --muted: #a8b3c2;
      --ok: #73d99f;
      --bad: #ff897d;
      --warn: #f2c166;
      --blue: #89b4ff;
      --blue-solid: #386fd6;
      --ink: #f1f5f9;
      --button: #222936;
      --button-hover: #2b3442;
      --sample: #151a22;
    }
    body[data-theme="light"] {
      color-scheme: light;
      --bg: #f6f7f9;
      --side: #eef2f6;
      --panel: #ffffff;
      --panel-soft: #fbfcfe;
      --line: #d7dde5;
      --text: #111827;
      --muted: #5b6472;
      --ok: #16794f;
      --bad: #b42318;
      --warn: #b7791f;
      --blue: #2251a4;
      --blue-solid: #2251a4;
      --ink: #263241;
      --button: #ffffff;
      --button-hover: #f3f6fa;
      --sample: #fbfcfe;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      letter-spacing: 0;
    }
    .app {
      min-height: 100vh;
      display: grid;
      grid-template-columns: minmax(230px, 300px) minmax(0, 1fr);
    }
    aside {
      border-right: 1px solid var(--line);
      background: var(--side);
      padding: 18px;
      overflow: auto;
    }
    main {
      padding: 18px;
      display: grid;
      grid-template-rows: auto auto minmax(0, 1fr);
      gap: 14px;
      min-width: 0;
    }
    h1 {
      margin: 0 0 14px;
      font-size: 18px;
      line-height: 1.2;
    }
    h2 {
      margin: 0;
      font-size: 24px;
      line-height: 1.2;
    }
    .status {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
      border-bottom: 1px solid var(--line);
      padding-bottom: 14px;
    }
    .pill {
      border: 1px solid var(--line);
      background: var(--panel-soft);
      border-radius: 999px;
      padding: 5px 10px;
      font-size: 13px;
      color: var(--muted);
    }
    .pill.ready { border-color: var(--ok); color: var(--ok); }
    .pill.connected, .pill.connecting, .pill.reconnecting, .pill.retrying {
      border-color: var(--warn);
      color: var(--warn);
    }
    .pill.error, .pill.disconnected, .pill.offline { border-color: var(--bad); color: var(--bad); }
    .steps {
      display: grid;
      gap: 8px;
    }
    .step {
      width: 100%;
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      padding: 10px;
      text-align: left;
      cursor: pointer;
      color: var(--ink);
    }
    .step:hover {
      background: var(--panel-soft);
    }
    .step.active {
      border-color: var(--blue);
      box-shadow: inset 3px 0 0 var(--blue);
    }
    .step-title {
      font-weight: 700;
      font-size: 14px;
      margin-bottom: 6px;
    }
    .step-meta {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
    }
    .current {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 18px;
      display: grid;
      gap: 14px;
    }
    .instruction {
      color: var(--muted);
      font-size: 16px;
      line-height: 1.45;
    }
    .controls {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    button {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--button);
      color: var(--text);
      min-height: 38px;
      padding: 8px 12px;
      font-size: 14px;
      cursor: pointer;
    }
    button:hover {
      background: var(--button-hover);
    }
    button.primary {
      background: var(--blue-solid);
      border-color: var(--blue-solid);
      color: #ffffff;
    }
    .theme-toggle {
      margin-left: auto;
      min-height: 32px;
      padding: 6px 10px;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(4, minmax(120px, 1fr));
      gap: 10px;
    }
    .metric {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      min-width: 0;
    }
    .metric span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 6px;
    }
    .metric strong {
      font-size: 24px;
      line-height: 1;
    }
    .work {
      display: grid;
      grid-template-columns: minmax(0, 1.1fr) minmax(260px, 0.9fr);
      gap: 14px;
      min-height: 0;
    }
    .feed, .samples {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-height: 0;
      overflow: auto;
    }
    .event {
      border-bottom: 1px solid var(--line);
      padding: 9px 0;
      display: grid;
      grid-template-columns: auto 1fr auto;
      gap: 10px;
      align-items: start;
      font-size: 14px;
    }
    .event.match .tag { color: var(--ok); }
    .event.conflict .tag { color: var(--bad); }
    .event .detail { color: var(--muted); font-size: 12px; margin-top: 3px; }
    .sample-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 8px;
      font-variant-numeric: tabular-nums;
    }
    .axis {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: var(--sample);
    }
    .axis span { color: var(--muted); display: block; font-size: 12px; }
    .axis strong { font-size: 20px; }
    .connection-heading {
      margin-top: 18px;
      font-size: 18px;
    }
    .connection-log {
      display: grid;
      gap: 8px;
      margin-top: 10px;
      font-size: 12px;
    }
    .connection-row {
      display: grid;
      grid-template-columns: auto auto minmax(0, 1fr);
      gap: 8px;
      align-items: start;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--sample);
      padding: 8px;
      color: var(--muted);
    }
    .connection-row strong {
      color: var(--text);
      text-transform: uppercase;
      font-size: 11px;
    }
    .connection-row.error strong,
    .connection-row.disconnected strong {
      color: var(--bad);
    }
    .connection-row.ready strong {
      color: var(--ok);
    }
    @media (max-width: 840px) {
      .app { grid-template-columns: 1fr; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); }
      .metrics, .work { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body data-theme="dark">
  <div class="app">
    <aside>
      <h1>TRIKI Calibration</h1>
      <div class="steps" id="steps"></div>
    </aside>
    <main>
      <section class="status">
        <span class="pill" id="status">loading</span>
        <span class="pill" id="message">Waiting for server state</span>
        <span class="pill button-hint" id="button-hint">Pairing button: wait for scan.</span>
        <button class="theme-toggle" id="theme-toggle" type="button">Light mode</button>
      </section>
      <section class="current">
        <div>
          <h2 id="title">Calibration</h2>
          <div class="instruction" id="instruction"></div>
        </div>
        <div class="controls">
          <button class="primary" data-action="pairing">Press pairing now</button>
          <button data-action="previous">Previous</button>
          <button class="primary" data-action="next">Next action</button>
          <button data-action="save">Save summary</button>
          <button data-action="reset">Reset counts</button>
        </div>
        <div class="metrics">
          <div class="metric"><span>Samples</span><strong id="samples">0</strong></div>
          <div class="metric"><span>Events</span><strong id="events">0</strong></div>
          <div class="metric"><span>Matches</span><strong id="matches">0</strong></div>
          <div class="metric"><span>Conflicts</span><strong id="conflicts">0</strong></div>
        </div>
      </section>
      <section class="work">
        <div class="feed">
          <h2>Detected Events</h2>
          <div id="feed"></div>
        </div>
        <div class="samples">
          <h2>Latest Sample</h2>
          <div class="sample-grid" id="sample-grid"></div>
          <h2 class="connection-heading">Connection</h2>
          <div class="connection-log" id="connection-log"></div>
        </div>
      </section>
    </main>
  </div>
  <script>
    let state = null;
    const axes = ['a', 'b', 'c', 'd', 'e', 'f'];

    function applyTheme(theme) {
      const normalized = theme === 'light' ? 'light' : 'dark';
      document.body.dataset.theme = normalized;
      localStorage.setItem('triki-theme', normalized);
      const button = document.getElementById('theme-toggle');
      button.textContent = normalized === 'dark' ? 'Light mode' : 'Dark mode';
    }

    applyTheme(localStorage.getItem('triki-theme') || 'dark');
    document.getElementById('theme-toggle').addEventListener('click', () => {
      applyTheme(document.body.dataset.theme === 'dark' ? 'light' : 'dark');
    });

    function setState(next) {
      state = next;
      const status = document.getElementById('status');
      status.textContent = state.status;
      status.className = 'pill ' + (state.status || '');
      document.getElementById('message').textContent = state.message;
      document.getElementById('button-hint').textContent = state.button_hint || '';
      document.getElementById('title').textContent = state.current.title;
      document.getElementById('instruction').textContent = state.current.instruction;
      document.getElementById('samples').textContent = state.current.samples;
      document.getElementById('events').textContent = state.current.events;
      document.getElementById('matches').textContent = state.current.matches;
      document.getElementById('conflicts').textContent = state.current.conflicts;
      renderSteps();
      renderFeed();
      renderConnectionLog();
    }

    function renderSteps() {
      const root = document.getElementById('steps');
      root.innerHTML = '';
      for (const step of state.actions) {
        const button = document.createElement('button');
        button.className = 'step' + (step.active ? ' active' : '');
        button.onclick = () => control('select', { index: step.index });
        button.innerHTML = `
          <div class="step-title">${escapeHtml(step.title)}</div>
          <div class="step-meta">
            <span>${escapeHtml(step.label)}</span>
            <span>${step.matches}/${step.events}</span>
            <span>${step.samples} samples</span>
          </div>`;
        root.appendChild(button);
      }
    }

    function renderFeed() {
      const root = document.getElementById('feed');
      root.innerHTML = '';
      for (const event of state.recent_events) {
        appendEvent(event, root);
      }
    }

    function renderConnectionLog() {
      const root = document.getElementById('connection-log');
      root.innerHTML = '';
      for (const item of state.connection_log || []) {
        const row = document.createElement('div');
        row.className = 'connection-row ' + (item.status || '');
        row.innerHTML = `
          <span>${escapeHtml(item.timestamp || '')}</span>
          <strong>${escapeHtml(item.status)}</strong>
          <div>${escapeHtml(item.action_label || '')}: ${escapeHtml(item.message)}</div>`;
        root.appendChild(row);
      }
    }

    function appendEvent(event, root) {
      const item = document.createElement('div');
      item.className = 'event ' + (event.match ? 'match' : 'conflict');
      item.innerHTML = `
        <strong class="tag">${event.match ? 'match' : 'conflict'}</strong>
        <div>
          <div>${escapeHtml(event.expected_label)} -> ${escapeHtml(event.predicted_label)} (${event.confidence})</div>
          <div class="detail">${escapeHtml(event.reason)} | gyro=${event.features.gyro_p99} | accel=${event.features.accel_deviation_p99} | c=${event.features.c_mean} | cAbs=${event.features.c_abs_p99} | latG=${event.features.lateral_gyro_p99} | latA=${event.features.lateral_accel_p99} | loop=${event.features.lateral_accel_area_norm} | line=${event.features.lateral_accel_pca_ratio} | fDrop=${event.features.f_abs_drop_delta} | fPost=${event.features.f_abs_peak_after_drop_delta} | fAfter=${event.features.f_abs_post_peak_sample_count} | seq=${escapeHtml(event.features.c_sequence || '-')}</div>
        </div>
        <span>${Number(event.elapsed_seconds).toFixed(2)}s</span>`;
      root.appendChild(item);
    }

    function renderSample(payload) {
      const root = document.getElementById('sample-grid');
      root.innerHTML = '';
      payload.values.forEach((value, index) => {
        const tile = document.createElement('div');
        tile.className = 'axis';
        tile.innerHTML = `<span>${axes[index]}</span><strong>${value}</strong>`;
        root.appendChild(tile);
      });
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

    for (const button of document.querySelectorAll('.controls [data-action]')) {
      button.addEventListener('click', () => control(button.dataset.action));
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[ch]));
    }

    const events = new EventSource('/events');
    events.onmessage = (message) => {
      const payload = JSON.parse(message.data);
      if (payload.type === 'state') setState(payload.state);
      if (payload.type === 'sample') renderSample(payload);
      if (payload.type === 'gesture' && state) {
        state.recent_events = [payload, ...state.recent_events].slice(0, 30);
        renderFeed();
      }
    };
    events.onerror = () => {
      document.getElementById('status').textContent = 'offline';
      document.getElementById('status').className = 'pill error';
      document.getElementById('message').textContent = 'SSE connection lost.';
    };
  </script>
</body>
</html>
"""


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TRIKI browser calibration suite.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output-dir", type=Path, default=Path("output/calibration"))
    parser.add_argument("--scan-seconds", type=float, default=10.0)
    parser.add_argument("--connect-attempts", type=int, default=5)
    parser.add_argument(
        "--connect-mode",
        choices=("hybrid", "cached", "scan"),
        default="scan",
    )
    parser.add_argument("--gatt-profile", default="auto")
    parser.add_argument("--gatt-timeout-seconds", type=float, default=12.0)
    parser.add_argument("--retry-delay-seconds", type=float, default=3.0)
    parser.add_argument("--reconnect-delay-seconds", type=float, default=4.0)
    parser.add_argument(
        "--no-reconnect",
        action="store_true",
        help="Stop BLE worker after a disconnect instead of reconnecting.",
    )
    parser.add_argument(
        "--auto-reconnect",
        action="store_true",
        help="Keep trying BLE reconnects automatically instead of waiting for the web button.",
    )
    parser.add_argument("--window-seconds", type=float, default=0.75)
    parser.add_argument("--min-samples", type=int, default=12)
    parser.add_argument("--min-confidence", type=float, default=0.55)
    parser.add_argument("--repeat-seconds", type=float, default=0.55)
    parser.add_argument("--warmup-seconds", type=float, default=0.20)
    parser.add_argument("--confirm-windows", type=int, default=1)
    parser.add_argument("--no-ble", action="store_true", help="Serve UI without connecting to TRIKI.")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = build_arg_parser()
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    recorder = CalibrationRecorder(args.output_dir)
    bus = EventBus()
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

    with recorder:
        session = CalibrationSession(DEFAULT_ACTIONS, recorder=recorder)
        connection_control = ConnectionControl(manual_pairing=not args.auto_reconnect)
        server = CalibrationHttpServer(
            (args.host, args.port),
            session,
            bus,
            connection_control,
        )
        if args.no_ble:
            session.set_status("ready", "UI ready in no-BLE mode.")
        else:
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
            session.recorder.write_summary(session.snapshot())
            return 130
        finally:
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
