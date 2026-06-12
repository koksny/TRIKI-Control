from __future__ import annotations

import argparse
import asyncio
import contextlib
import time
from pathlib import Path

from bleak.backends.device import BLEDevice
from bleak.exc import BleakCharacteristicNotFoundError, BleakDeviceNotFoundError

from triki_battery import BATTERY_SERVICE_UUID
from triki_protocol import MotionStreamParser
from triki_recording import SampleRecorder, build_recording_paths

try:
    from bleak import BleakClient, BleakScanner
except ImportError as exc:  # pragma: no cover - operator guidance
    raise SystemExit(
        "Missing bleak. Run: .\\.venv\\Scripts\\python.exe -m pip install bleak"
    ) from exc

TRIKI_ADDRESS = "FB:EE:B9:8C:15:F9"
TRIKI_ADDRESS_COMPACT = "FBEEB98C15F9"
TRIKI_NAME = "Triki 308531776"
NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
UART_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
UART_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
LED_CHARACTERISTIC_UUID = "6e400004-b5a3-f393-e0a9-e50e24dcca9e"
START_STREAM_COMMAND = bytes.fromhex("20 10 00 d0 07 34 00 03")
GATT_PROFILE_NUS_CACHED = "nus-cached"
GATT_PROFILE_NUS_UNCACHED = "nus-uncached"
GATT_PROFILES = (GATT_PROFILE_NUS_CACHED, GATT_PROFILE_NUS_UNCACHED)


def compact_address(value: str | None) -> str:
    return "".join(ch for ch in (value or "").upper() if ch in "0123456789ABCDEF")


def hex_bytes(payload: bytes) -> str:
    return " ".join(f"{byte:02x}" for byte in payload)


def iter_gatt_profiles(gatt_profile: str) -> tuple[str, ...]:
    if gatt_profile == "auto":
        return GATT_PROFILES
    if gatt_profile in GATT_PROFILES:
        return (gatt_profile,)
    raise ValueError(f"Unknown GATT profile: {gatt_profile}")


def bleak_client_kwargs_for_profile(gatt_profile: str) -> dict:
    if gatt_profile == GATT_PROFILE_NUS_CACHED:
        use_cached_services = True
    elif gatt_profile == GATT_PROFILE_NUS_UNCACHED:
        use_cached_services = False
    else:
        raise ValueError(f"Unknown GATT profile: {gatt_profile}")

    return {
        "services": [NUS_SERVICE_UUID, BATTERY_SERVICE_UUID],
        "winrt": {"use_cached_services": use_cached_services},
    }


def bleak_client_kwargs(use_cached_services: bool) -> dict:
    gatt_profile = (
        GATT_PROFILE_NUS_CACHED
        if use_cached_services
        else GATT_PROFILE_NUS_UNCACHED
    )
    return bleak_client_kwargs_for_profile(gatt_profile)


def format_service_dump(services) -> str:
    if services is None:
        return "services=<unavailable>"

    lines = []
    for service in services:
        lines.append(f"service={service.uuid} {service.description}")
        for characteristic in service.characteristics:
            props = ",".join(characteristic.properties)
            lines.append(
                f"  char={characteristic.uuid} props={props} "
                f"{characteristic.description}"
            )
    return "\n".join(lines) if lines else "services=<empty>"


def device_not_found_message(connect_mode: str) -> str:
    if connect_mode == "cached":
        return (
            "CACHED_DEVICE_NOT_READY: Windows knows the paired TRIKI, but WinRT "
            "did not return an active BLE device for this attempt; retrying."
        )
    return "DEVICE_NOT_FOUND: TRIKI was not found during BLE scan; retrying."


def rssi_feedback(rssi: int | None) -> str | None:
    if rssi is None or rssi > -80:
        return None
    return (
        f"RSSI_WEAK rssi={rssi}: move TRIKI closer to the Bluetooth adapter; "
        "weak signal can connect and then drop before UART_READY."
    )


def setup_disconnect_message(gatt_profile: str, exc: BaseException) -> str:
    return (
        f"DISCONNECTED_BEFORE_UART_READY profile={gatt_profile}: "
        f"{type(exc).__name__}: {exc}. Move TRIKI closer, do not press the "
        "pairing button, then retry."
    )


def make_cached_triki_device() -> BLEDevice:
    return BLEDevice(TRIKI_ADDRESS, TRIKI_NAME, details=None)


def activation_prompt_for_mode(connect_mode: str) -> str:
    if connect_mode == "cached":
        return "TRYB CACHED: NIE NACISKAJ PRZYCISKU TRIKI; trzymaj kapsel normalnie."
    return "TRYB SCAN: uzyj tylko jesli cached nie dziala; przycisk moze uruchamiac pairing."


def movement_prompt_for_label(label: str) -> str:
    normalized = label.strip().lower()
    if normalized in {"still", "still-cached", "idle", "rest"}:
        return f"KEEP_STILL_NOW label={label}"
    return f"START_MOVING_NOW label={label}"


async def find_triki(scan_seconds: float, connect_mode: str):
    print(activation_prompt_for_mode(connect_mode))
    if connect_mode == "cached":
        print(f"USING_CACHED address={TRIKI_ADDRESS} name={TRIKI_NAME!r}")
        return make_cached_triki_device()

    loop = asyncio.get_running_loop()
    found = loop.create_future()
    seen: dict[str, tuple[str, int]] = {}

    def on_advertisement(device, advertisement_data) -> None:
        name = device.name or advertisement_data.local_name or ""
        seen[device.address] = (name, advertisement_data.rssi)
        if (
            compact_address(device.address) == TRIKI_ADDRESS_COMPACT
            or "triki" in name.lower()
        ):
            if not found.done():
                found.set_result((device, name, advertisement_data.rssi))

    scanner = BleakScanner(on_advertisement)
    print(f"SCAN {scan_seconds:.0f}s")
    print("SCAN_WAITING_FOR_TRIKI")
    await scanner.start()
    try:
        device, name, rssi = await asyncio.wait_for(found, timeout=scan_seconds)
        print(f"FOUND address={device.address} name={name!r} rssi={rssi}")
        feedback = rssi_feedback(rssi)
        if feedback is not None:
            print(feedback)
        return device
    except asyncio.TimeoutError:
        print("TRIKI_NOT_FOUND")
        named = [
            (rssi, address, name)
            for address, (name, rssi) in seen.items()
            if name
        ]
        for rssi, address, name in sorted(named, reverse=True)[:12]:
            print(f"VISIBLE rssi={rssi} address={address} name={name!r}")
        return None
    finally:
        await scanner.stop()


async def run_session(
    device,
    listen_seconds: float,
    max_samples: int | None,
    record_dir: Path | None,
    label: str,
    summary_skip_seconds: float,
    gatt_profile: str,
) -> int:
    parser = MotionStreamParser()
    sample_count = 0
    notification_count = 0
    started_at = 0.0
    recording_paths = build_recording_paths(record_dir, label) if record_dir else None
    recorder: SampleRecorder | None = None

    def on_notify(_sender, data: bytearray) -> None:
        nonlocal notification_count, sample_count
        notification_count += 1
        payload = bytes(data)
        elapsed = time.monotonic() - started_at
        samples = parser.feed(payload)
        if not samples:
            print(
                f"RAW t={elapsed:7.3f}s notify={notification_count:04d} "
                f"len={len(payload):02d} hex={hex_bytes(payload)}",
                flush=True,
            )
            return

        for sample in samples:
            sample_count += 1
            if recorder is not None:
                recorder.record(elapsed, sample)
            a, b, c, d, e, f = sample.values
            print(
                f"SAMPLE t={elapsed:7.3f}s #{sample_count:05d} "
                f"pid={sample.packet_id:02d} "
                f"a={a:6d} b={b:6d} c={c:6d} d={d:6d} e={e:6d} f={f:6d}",
                flush=True,
            )

    disconnected = asyncio.Event()
    uart_ready = False

    def on_disconnect(_client) -> None:
        print("DISCONNECTED")
        disconnected.set()

    print(f"GATT_PROFILE {gatt_profile}")
    print("CONNECTING_GATT")
    with contextlib.ExitStack() as recording_stack:
        try:
            async with BleakClient(
                device,
                timeout=20.0,
                disconnected_callback=on_disconnect,
                **bleak_client_kwargs_for_profile(gatt_profile),
            ) as client:
                print(f"CONNECTED_GATT={client.is_connected}")
                try:
                    await client.start_notify(UART_TX_UUID, on_notify)
                except BleakCharacteristicNotFoundError:
                    print("NUS_TX_CHARACTERISTIC_MISSING")
                    print(format_service_dump(client.services))
                    raise
                print("UART_READY")
                uart_ready = True
                if recording_paths is not None:
                    recorder = recording_stack.enter_context(
                        SampleRecorder(recording_paths, label, summary_skip_seconds)
                    )
                    print(f"RECORD_CSV {recording_paths.csv}")
                    print(f"RECORD_JSONL {recording_paths.jsonl}")
                    print(f"RECORD_SUMMARY {recording_paths.summary_json}")
                print(f"WRITE_START hex={hex_bytes(START_STREAM_COMMAND)}")
                await client.write_gatt_char(
                    UART_RX_UUID,
                    START_STREAM_COMMAND,
                    response=False,
                )
                print(movement_prompt_for_label(label), flush=True)
                started_at = time.monotonic()

                deadline = started_at + listen_seconds
                while time.monotonic() < deadline:
                    if max_samples is not None and sample_count >= max_samples:
                        break
                    timeout = min(0.25, max(0.0, deadline - time.monotonic()))
                    if timeout == 0:
                        break
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(disconnected.wait(), timeout=timeout)
                        break

                if client.is_connected:
                    with contextlib.suppress(Exception):
                        await client.stop_notify(UART_TX_UUID)
        except OSError as exc:
            if disconnected.is_set() and not uart_ready:
                print(setup_disconnect_message(gatt_profile, exc))
            raise

    if recording_paths is not None:
        if sample_count:
            print(f"RECORD_SAVED samples={sample_count}")
        else:
            print("RECORD_EMPTY_REMOVED")

    print(f"DONE notifications={notification_count} samples={sample_count}")
    return 0 if sample_count else 2


async def probe(
    scan_seconds: float,
    listen_seconds: float,
    max_samples: int | None,
    connect_attempts: int,
    record_dir: Path | None,
    label: str,
    summary_skip_seconds: float,
    connect_mode: str,
    gatt_profile: str,
    retry_delay_seconds: float,
) -> int:
    last_status = 1
    gatt_profiles = iter_gatt_profiles(gatt_profile)
    for attempt in range(1, connect_attempts + 1):
        if connect_attempts > 1:
            print(f"ATTEMPT {attempt}/{connect_attempts}")

        device = await find_triki(scan_seconds, connect_mode)
        if device is None:
            last_status = 1
            continue

        for profile_index, profile in enumerate(gatt_profiles, start=1):
            try:
                status = await run_session(
                    device,
                    listen_seconds,
                    max_samples,
                    record_dir,
                    label,
                    summary_skip_seconds,
                    profile,
                )
            except BleakDeviceNotFoundError:
                print(device_not_found_message(connect_mode))
                last_status = 1
            except TimeoutError as exc:
                print(f"CONNECT_TIMEOUT {type(exc).__name__} profile={profile}")
                last_status = 3
            except Exception as exc:
                print(f"SESSION_ERROR {type(exc).__name__} profile={profile}: {exc}")
                last_status = 3
            else:
                if status == 0:
                    return 0
                last_status = status
                break

            has_more_profiles = profile_index < len(gatt_profiles)
            has_more_attempts = attempt < connect_attempts
            if (has_more_profiles or has_more_attempts) and retry_delay_seconds > 0:
                await asyncio.sleep(retry_delay_seconds)

    return last_status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read TRIKI BLE motion samples.")
    parser.add_argument("--scan-seconds", type=float, default=60.0)
    parser.add_argument("--listen-seconds", type=float, default=20.0)
    parser.add_argument("--max-samples", type=int, default=None)
    parser.add_argument("--connect-attempts", type=int, default=3)
    parser.add_argument("--record-dir", type=Path, default=None)
    parser.add_argument("--label", default="session")
    parser.add_argument("--summary-skip-seconds", type=float, default=0.25)
    parser.add_argument("--connect-mode", choices=("cached", "scan"), default="cached")
    parser.add_argument(
        "--use-cached-services",
        action="store_true",
        help="Deprecated shortcut for --gatt-profile nus-cached.",
    )
    parser.add_argument(
        "--gatt-profile",
        choices=("auto", *GATT_PROFILES),
        default="auto",
        help="GATT connection strategy. Default tries cached NUS, then uncached NUS.",
    )
    parser.add_argument("--retry-delay-seconds", type=float, default=1.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    gatt_profile = (
        GATT_PROFILE_NUS_CACHED if args.use_cached_services else args.gatt_profile
    )
    return asyncio.run(
        probe(
            args.scan_seconds,
            args.listen_seconds,
            args.max_samples,
            args.connect_attempts,
            args.record_dir,
            args.label,
            args.summary_skip_seconds,
            args.connect_mode,
            gatt_profile,
            args.retry_delay_seconds,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
