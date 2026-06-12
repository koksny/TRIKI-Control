from __future__ import annotations

import argparse
import asyncio
import contextlib
import time
from collections import deque

from bleak.exc import BleakDeviceNotFoundError

from triki_classifier import GesturePrediction, classify_features, extract_features
from triki_probe import (
    GATT_PROFILES,
    START_STREAM_COMMAND,
    UART_RX_UUID,
    UART_TX_UUID,
    bleak_client_kwargs_for_profile,
    device_not_found_message,
    find_triki,
    hex_bytes,
    iter_gatt_profiles,
    setup_disconnect_message,
)
from triki_protocol import MotionSample, MotionStreamParser

try:
    from bleak import BleakClient
except ImportError as exc:  # pragma: no cover - operator guidance
    raise SystemExit(
        "Missing bleak. Run: .\\.venv\\Scripts\\python.exe -m pip install bleak"
    ) from exc


class LiveGestureDetector:
    def __init__(
        self,
        *,
        window_seconds: float = 0.75,
        min_samples: int = 12,
        min_confidence: float = 0.55,
        repeat_seconds: float = 0.55,
        warmup_seconds: float = 0.20,
        confirm_windows: int = 1,
        active_tail_seconds: float = 0.16,
        one_shot_labels: tuple[str, ...] = ("lift", "flip-over"),
        one_shot_reset_labels: tuple[str, ...] = ("still", "unknown"),
        suppress_labels: tuple[str, ...] = (
            "still",
            "unknown",
            "toss-catch",
            "rock-edge",
            "slide-back-forth",
            "twist-cw-ccw-cw-ccw",
            "twist-ccw-cw-ccw-cw",
            "swirl-cw",
            "swirl-ccw",
            "shake",
            "tap-single",
            "tap-double",
        ),
        observer=None,
    ) -> None:
        self.window_seconds = window_seconds
        self.min_samples = min_samples
        self.min_confidence = min_confidence
        self.repeat_seconds = repeat_seconds
        self.warmup_seconds = warmup_seconds
        self.confirm_windows = confirm_windows
        self.active_tail_seconds = active_tail_seconds
        self.one_shot_labels = set(one_shot_labels)
        self.one_shot_reset_labels = set(one_shot_reset_labels)
        self.suppress_labels = set(suppress_labels)
        self._observer = observer
        self._records: deque[tuple[float, MotionSample]] = deque()
        self._last_emit_label: str | None = None
        self._last_emit_time = -1e9
        self._candidate_label: str | None = None
        self._candidate_count = 0
        self._one_shot_latches: set[str] = set()

    def reset(self) -> None:
        self._records.clear()
        self._last_emit_label = None
        self._last_emit_time = -1e9
        self._clear_candidate()
        self._one_shot_latches.clear()

    def add_sample(
        self,
        elapsed_seconds: float,
        sample: MotionSample,
    ) -> GesturePrediction | None:
        # Single-exit form: classify the OUTCOME of every sample (including the
        # dropped ones, with the reason) so a session log can explain exactly why
        # a gesture did or did not fire. Return values are unchanged from before.
        result: GesturePrediction | None = None
        prediction: GesturePrediction | None = None
        if elapsed_seconds < self.warmup_seconds:
            outcome = "warmup"
        else:
            self._records.append((elapsed_seconds, sample))
            self._prune(elapsed_seconds)
            if len(self._records) < self.min_samples:
                outcome = "buffering"
            else:
                prediction = classify_features(
                    extract_features(list(self._records), startup_skip_seconds=0.0)
                )
                if prediction.label in self.one_shot_reset_labels:
                    self._one_shot_latches.clear()
                if prediction.label in self.suppress_labels:
                    self._clear_candidate()
                    outcome = "suppressed"
                elif prediction.confidence < self.min_confidence:
                    self._clear_candidate()
                    outcome = "below_confidence"
                elif not self._is_active_now(prediction.label, elapsed_seconds):
                    self._clear_candidate()
                    outcome = "inactive_tail"
                elif not self._confirm_candidate(prediction.label):
                    outcome = "unconfirmed"
                elif prediction.label in self._one_shot_latches:
                    outcome = "one_shot_latched"
                elif not self._should_emit(prediction.label, elapsed_seconds):
                    outcome = "repeat_gated"
                else:
                    self._last_emit_label = prediction.label
                    self._last_emit_time = elapsed_seconds
                    if prediction.label in self.one_shot_labels:
                        self._one_shot_latches.add(prediction.label)
                    outcome = "emitted"
                    result = prediction

        if self._observer is not None:
            try:
                self._observer(self._decision_record(elapsed_seconds, sample, prediction, outcome))
            except Exception:
                pass
        return result

    def _decision_record(self, elapsed_seconds, sample, prediction, outcome) -> dict:
        record = {
            "t": round(elapsed_seconds, 4),
            "values": list(sample.values),
            "outcome": outcome,
        }
        if prediction is not None:
            features = prediction.features
            record.update(
                label=prediction.label,
                confidence=round(prediction.confidence, 3),
                reason=prediction.reason,
                feat={
                    "c_mean": round(features.c_mean, 1),
                    "gyro_p99": round(features.gyro_p99, 1),
                    "lat_gyro_p99": round(features.lateral_gyro_p99, 1),
                    "lat_acc_p99": round(features.lateral_accel_p99, 1),
                    "accel_dev_p99": round(features.accel_deviation_p99, 1),
                    "orient_deg": round(features.orientation_angle_degrees, 1),
                },
            )
        return record

    def _prune(self, elapsed_seconds: float) -> None:
        cutoff = elapsed_seconds - self.window_seconds
        while self._records and self._records[0][0] < cutoff:
            self._records.popleft()

    def _should_emit(self, label: str, elapsed_seconds: float) -> bool:
        if label != self._last_emit_label:
            return True
        return elapsed_seconds - self._last_emit_time >= self.repeat_seconds

    def _is_active_now(self, label: str, elapsed_seconds: float) -> bool:
        if label in self.one_shot_labels:
            return True
        if label not in {"rotate-cw", "rotate-ccw", "scrub-cw", "scrub-ccw", "back-forth"}:
            return True
        tail = [
            sample.values
            for timestamp, sample in self._records
            if timestamp >= elapsed_seconds - self.active_tail_seconds
        ]
        if not tail:
            return True

        c_mean = sum(row[2] for row in tail) / len(tail)
        if label == "rotate-cw":
            return c_mean >= 450
        if label == "rotate-ccw":
            return c_mean <= -450

        accel_deviation = [
            abs((row[3] ** 2 + row[4] ** 2 + row[5] ** 2) ** 0.5 - 2050.0)
            for row in tail
        ]
        lateral_accel = [
            (row[3] ** 2 + row[4] ** 2) ** 0.5
            for row in tail
        ]
        gyro = [
            (row[0] ** 2 + row[1] ** 2 + row[2] ** 2) ** 0.5
            for row in tail
        ]
        if label in {"scrub-cw", "scrub-ccw", "back-forth"}:
            return max(lateral_accel, default=0.0) >= 250 or max(gyro, default=0.0) >= 500
        return max(accel_deviation, default=0.0) >= 1600 or max(gyro, default=0.0) >= 6000

    def _confirm_candidate(self, label: str) -> bool:
        if label == self._candidate_label:
            self._candidate_count += 1
        else:
            self._candidate_label = label
            self._candidate_count = 1
        return self._candidate_count >= self.confirm_windows

    def _clear_candidate(self) -> None:
        self._candidate_label = None
        self._candidate_count = 0


async def run_live_session(
    device,
    *,
    gatt_profile: str,
    listen_seconds: float,
    detector: LiveGestureDetector,
) -> int:
    parser = MotionStreamParser()
    started_at = 0.0
    sample_count = 0
    event_count = 0
    uart_ready = False
    disconnected = asyncio.Event()

    def on_disconnect(_client) -> None:
        print("DISCONNECTED")
        disconnected.set()

    def on_notify(_sender, data: bytearray) -> None:
        nonlocal sample_count, event_count
        elapsed = time.monotonic() - started_at
        for sample in parser.feed(bytes(data)):
            sample_count += 1
            event = detector.add_sample(elapsed, sample)
            if event is None:
                continue
            event_count += 1
            print(
                f"EVENT t={elapsed:7.3f}s #{event_count:04d} "
                f"label={event.label} confidence={event.confidence:.2f} "
                f"reason={event.reason}",
                flush=True,
            )

    print(f"GATT_PROFILE {gatt_profile}")
    print("CONNECTING_GATT")
    try:
        async with BleakClient(
            device,
            timeout=20.0,
            disconnected_callback=on_disconnect,
            **bleak_client_kwargs_for_profile(gatt_profile),
        ) as client:
            print(f"CONNECTED_GATT={client.is_connected}")
            await client.start_notify(UART_TX_UUID, on_notify)
            print("UART_READY")
            uart_ready = True
            print(f"WRITE_START hex={hex_bytes(START_STREAM_COMMAND)}")
            await client.write_gatt_char(UART_RX_UUID, START_STREAM_COMMAND, response=False)
            print("LIVE_READY start moving TRIKI; Ctrl+C to stop.", flush=True)
            started_at = time.monotonic()

            deadline = None if listen_seconds <= 0 else started_at + listen_seconds
            while not disconnected.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    break
                timeout = 0.25
                if deadline is not None:
                    timeout = min(timeout, max(0.0, deadline - time.monotonic()))
                    if timeout == 0:
                        break
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(disconnected.wait(), timeout=timeout)

            if client.is_connected:
                with contextlib.suppress(Exception):
                    await client.stop_notify(UART_TX_UUID)
    except OSError as exc:
        if disconnected.is_set() and not uart_ready:
            print(setup_disconnect_message(gatt_profile, exc))
        raise

    print(f"DONE samples={sample_count} events={event_count}")
    return 0 if event_count else 2


async def live_probe(
    *,
    scan_seconds: float,
    connect_attempts: int,
    connect_mode: str,
    gatt_profile: str,
    retry_delay_seconds: float,
    listen_seconds: float,
    detector: LiveGestureDetector,
) -> int:
    last_status = 1
    profiles = iter_gatt_profiles(gatt_profile)
    for attempt in range(1, connect_attempts + 1):
        if connect_attempts > 1:
            print(f"ATTEMPT {attempt}/{connect_attempts}")

        device = await find_triki(scan_seconds, connect_mode)
        if device is None:
            last_status = 1
            continue

        for profile_index, profile in enumerate(profiles, start=1):
            try:
                status = await run_live_session(
                    device,
                    gatt_profile=profile,
                    listen_seconds=listen_seconds,
                    detector=detector,
                )
            except BleakDeviceNotFoundError:
                print(device_not_found_message(connect_mode))
                last_status = 1
            except TimeoutError as exc:
                print(f"CONNECT_TIMEOUT {type(exc).__name__} profile={profile}")
                last_status = 3
            except KeyboardInterrupt:
                print("STOPPED_BY_USER")
                return 130
            except Exception as exc:
                print(f"SESSION_ERROR {type(exc).__name__} profile={profile}: {exc}")
                last_status = 3
            else:
                if status == 0:
                    return 0
                last_status = status
                break

            has_more_profiles = profile_index < len(profiles)
            has_more_attempts = attempt < connect_attempts
            if (has_more_profiles or has_more_attempts) and retry_delay_seconds > 0:
                await asyncio.sleep(retry_delay_seconds)
    return last_status


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run live TRIKI gesture detection.")
    parser.add_argument("--scan-seconds", type=float, default=30.0)
    parser.add_argument("--connect-attempts", type=int, default=3)
    parser.add_argument("--connect-mode", choices=("cached", "scan"), default="scan")
    parser.add_argument("--gatt-profile", choices=("auto", *GATT_PROFILES), default="auto")
    parser.add_argument("--retry-delay-seconds", type=float, default=2.0)
    parser.add_argument(
        "--listen-seconds",
        type=float,
        default=0.0,
        help="Live duration. Use 0 to run until disconnect or Ctrl+C.",
    )
    parser.add_argument("--window-seconds", type=float, default=0.75)
    parser.add_argument("--min-samples", type=int, default=12)
    parser.add_argument("--min-confidence", type=float, default=0.55)
    parser.add_argument("--repeat-seconds", type=float, default=0.55)
    parser.add_argument("--warmup-seconds", type=float, default=0.20)
    parser.add_argument("--confirm-windows", type=int, default=1)
    parser.add_argument(
        "--show-still",
        action="store_true",
        help="Emit still events too. Unknown is still suppressed.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    suppress_labels = {"unknown"}
    if not args.show_still:
        suppress_labels.add("still")
    suppress_labels.update(
        {
            "tap-single",
            "tap-double",
            "toss-catch",
            "rock-edge",
            "slide-back-forth",
            "twist-cw-ccw-cw-ccw",
            "twist-ccw-cw-ccw-cw",
        }
    )
    detector = LiveGestureDetector(
        window_seconds=args.window_seconds,
        min_samples=args.min_samples,
        min_confidence=args.min_confidence,
        repeat_seconds=args.repeat_seconds,
        warmup_seconds=args.warmup_seconds,
        confirm_windows=args.confirm_windows,
        suppress_labels=tuple(sorted(suppress_labels)),
    )
    try:
        return asyncio.run(
            live_probe(
                scan_seconds=args.scan_seconds,
                connect_attempts=args.connect_attempts,
                connect_mode=args.connect_mode,
                gatt_profile=args.gatt_profile,
                retry_delay_seconds=args.retry_delay_seconds,
                listen_seconds=args.listen_seconds,
                detector=detector,
            )
        )
    except KeyboardInterrupt:
        print("STOPPED_BY_USER")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
