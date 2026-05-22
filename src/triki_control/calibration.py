from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import RLock

from triki_control.classifier import GesturePrediction
from triki_control.gestures import normalize_gesture_label
from triki_control.protocol import MotionSample


@dataclass(frozen=True)
class CalibrationAction:
    label: str
    title: str
    instruction: str
    duration_seconds: float = 8.0


DEFAULT_ACTIONS = (
    CalibrationAction("still", "Still", "Put TRIKI flat on the table and do not touch it.", 6.0),
    CalibrationAction("rotate-cw", "Rotate Right", "Rotate TRIKI clockwise on the table.", 8.0),
    CalibrationAction("rotate-ccw", "Rotate Left", "Rotate TRIKI counterclockwise on the table.", 8.0),
    CalibrationAction(
        "scrub-cw",
        "Scrub Right",
        "Move TRIKI around a clockwise circle on the table, like stirring a spoon in a cup. Do not intentionally rotate the cap itself.",
        8.0,
    ),
    CalibrationAction(
        "scrub-ccw",
        "Scrub Left",
        "Move TRIKI around a counterclockwise circle on the table, like stirring a spoon in a cup. Do not intentionally rotate the cap itself.",
        8.0,
    ),
    CalibrationAction(
        "back-forth",
        "Back-and-forth",
        "Slide TRIKI side to side in a mostly straight line on the table.",
        8.0,
    ),
    CalibrationAction(
        "lift",
        "Stamp / Enter",
        "Lift TRIKI slightly, then set TRIKI down with a firm stamp.",
        8.0,
    ),
    CalibrationAction("flip-over", "Flip / Space", "Flip TRIKI face-up to face-down and back.", 8.0),
)


def normalize_label(label: str) -> str:
    normalized = label.strip().lower().replace("_", "-")
    if normalized in {"lift-up", "lift-down"}:
        return "lift"
    if normalized.startswith("twist-cw-ccw-cw") or normalized.startswith("rotate-cw-ccw"):
        return "twist-cw-ccw-cw-ccw"
    if normalized.startswith("twist-ccw-cw-ccw") or normalized.startswith("rotate-ccw-cw"):
        return "twist-ccw-cw-ccw-cw"
    return normalize_gesture_label(normalized)


def build_session_id() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def pairing_button_hint(status: str, message: str) -> str:
    normalized_status = status.strip().lower()
    normalized_message = message.strip().lower()
    if normalized_status == "waiting" or "press pairing now" in normalized_message:
        return (
            "Pairing button: reconnect is paused. Click Press pairing now in "
            "the web UI, then press the physical button once."
        )
    if normalized_status == "pairing" or "triki pairing button now" in normalized_message:
        return (
            "Pairing button: press the physical TRIKI button once now, then "
            "stop pressing and keep it close."
        )
    if "mode scan" in normalized_message or "scan_waiting" in normalized_message:
        return (
            "Pairing button: click once now only if TRIKI is not found; "
            "then stop pressing and keep it close."
        )
    if "connecting gatt" in normalized_message or normalized_status in {
        "connected",
        "ready",
    }:
        return (
            "Pairing button: do not press now. During GATT/UART it can "
            "reset pairing or drop the link."
        )
    if normalized_status in {"retrying", "reconnecting", "disconnected", "error"}:
        return (
            "Pairing button: wait until the status says mode scan; click only "
            "during scan, not during GATT."
        )
    return (
        "Pairing button: leave it alone unless the status explicitly says "
        "mode scan."
    )


class CalibrationSession:
    def __init__(
        self,
        actions: tuple[CalibrationAction, ...] | list[CalibrationAction] = DEFAULT_ACTIONS,
        *,
        recorder: CalibrationRecorder | None = None,
    ) -> None:
        if not actions:
            raise ValueError("CalibrationSession requires at least one action")
        self.actions = tuple(actions)
        self.recorder = recorder
        self._lock = RLock()
        self._current_index = 0
        self._sample_count = 0
        self._event_count = 0
        self._steps = {
            action.label: {
                "samples": 0,
                "events": 0,
                "matches": 0,
                "conflicts": 0,
                "last_prediction": None,
            }
            for action in self.actions
        }
        self._recent_events: list[dict] = []
        self._connection_log: list[dict] = []
        self._status_sequence = 0
        self.status = "idle"
        self.message = "Calibration server ready."

    @property
    def current_action(self) -> CalibrationAction:
        return self.actions[self._current_index]

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
                    "action_label": self.current_action.label,
                    "action_title": self.current_action.title,
                    "timestamp": datetime.now().strftime("%H:%M:%S"),
                }
            )
            self._connection_log = self._connection_log[-80:]
            return self.snapshot()

    def select_action(self, index: int) -> dict:
        with self._lock:
            self._current_index = max(0, min(index, len(self.actions) - 1))
            return self.snapshot()

    def next_action(self) -> dict:
        return self.select_action(self._current_index + 1)

    def previous_action(self) -> dict:
        return self.select_action(self._current_index - 1)

    def reset_counts(self) -> dict:
        with self._lock:
            self._sample_count = 0
            self._event_count = 0
            self._recent_events.clear()
            for step in self._steps.values():
                step.update(
                    {
                        "samples": 0,
                        "events": 0,
                        "matches": 0,
                        "conflicts": 0,
                        "last_prediction": None,
                    }
                )
            return self.snapshot()

    def record_sample(self, elapsed_seconds: float, sample: MotionSample) -> dict:
        with self._lock:
            expected = self.current_action.label
            self._sample_count += 1
            self._steps[expected]["samples"] += 1
            if self.recorder is not None:
                self.recorder.write_sample(elapsed_seconds, expected, sample)
            return {
                "type": "sample",
                "elapsed_seconds": round(elapsed_seconds, 6),
                "expected_label": expected,
                "sample_count": self._sample_count,
                "values": list(sample.values),
            }

    def record_prediction(
        self,
        elapsed_seconds: float,
        prediction: GesturePrediction,
    ) -> dict:
        with self._lock:
            expected = self.current_action.label
            predicted = normalize_label(prediction.label)
            match = normalize_label(expected) == normalize_label(predicted)
            self._event_count += 1
            step = self._steps[expected]
            step["events"] += 1
            if match:
                step["matches"] += 1
            else:
                step["conflicts"] += 1
            step["last_prediction"] = predicted
            event = {
                "type": "gesture",
                "elapsed_seconds": round(elapsed_seconds, 6),
                "expected_label": expected,
                "predicted_label": predicted,
                "confidence": round(prediction.confidence, 3),
                "reason": prediction.reason,
                "match": match,
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
                    "lateral_accel_area_norm": round(
                        prediction.features.lateral_accel_area_norm,
                        3,
                    ),
                    "lateral_accel_pca_ratio": round(
                        prediction.features.lateral_accel_pca_ratio,
                        3,
                    ),
                    "f_abs_peak_delta": round(
                        prediction.features.f_abs_peak_delta,
                        3,
                    ),
                    "f_abs_drop_delta": round(
                        prediction.features.f_abs_drop_delta,
                        3,
                    ),
                    "f_abs_peak_after_drop_delta": round(
                        prediction.features.f_abs_peak_after_drop_delta,
                        3,
                    ),
                    "f_abs_post_peak_sample_count": prediction.features.f_abs_post_peak_sample_count,
                    "c_sequence": prediction.features.c_sequence,
                    "orientation_angle_degrees": round(
                        prediction.features.orientation_angle_degrees,
                        3,
                    ),
                },
            }
            self._recent_events.append(event)
            self._recent_events = self._recent_events[-80:]
            if self.recorder is not None:
                self.recorder.write_event(event)
            return event

    def snapshot(self) -> dict:
        with self._lock:
            action = self.current_action
            steps = []
            for index, item in enumerate(self.actions):
                state = self._steps[item.label]
                steps.append(
                    {
                        "index": index,
                        "label": item.label,
                        "title": item.title,
                        "instruction": item.instruction,
                        "duration_seconds": item.duration_seconds,
                        "active": index == self._current_index,
                        **state,
                    }
                )
            current = steps[self._current_index]
            return {
                "status": self.status,
                "message": self.message,
                "current_index": self._current_index,
                "current": current,
                "actions": steps,
                "button_hint": pairing_button_hint(self.status, self.message),
                "sample_count": self._sample_count,
                "event_count": self._event_count,
                "recent_events": list(reversed(self._recent_events[-30:])),
                "connection_log": list(reversed(self._connection_log[-30:])),
            }


class CalibrationRecorder:
    def __init__(self, output_dir: Path, *, session_id: str | None = None) -> None:
        self.output_dir = output_dir
        self.session_id = session_id or build_session_id()
        self.samples_csv = output_dir / f"{self.session_id}_samples.csv"
        self.events_csv = output_dir / f"{self.session_id}_events.csv"
        self.events_jsonl = output_dir / f"{self.session_id}_events.jsonl"
        self.summary_json = output_dir / f"{self.session_id}_summary.json"
        self._samples_file = None
        self._events_file = None
        self._events_jsonl_file = None
        self._sample_writer = None
        self._event_writer = None

    def __enter__(self) -> CalibrationRecorder:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._samples_file = self.samples_csv.open("w", encoding="utf-8", newline="")
        self._events_file = self.events_csv.open("w", encoding="utf-8", newline="")
        self._events_jsonl_file = self.events_jsonl.open("w", encoding="utf-8", newline="")
        self._sample_writer = csv.writer(self._samples_file, lineterminator="\n")
        self._event_writer = csv.writer(self._events_file, lineterminator="\n")
        self._sample_writer.writerow(
            [
                "elapsed_seconds",
                "expected_label",
                "packet_id",
                "a",
                "b",
                "c",
                "d",
                "e",
                "f",
            ]
        )
        self._event_writer.writerow(
            [
                "elapsed_seconds",
                "expected_label",
                "predicted_label",
                "confidence",
                "match",
                "reason",
                "gyro_p99",
                "accel_deviation_p99",
                "c_mean",
                "c_abs_p99",
                "lateral_gyro_p99",
                "lateral_accel_p99",
                "lateral_accel_area_norm",
                "lateral_accel_pca_ratio",
                "f_abs_peak_delta",
                "f_abs_drop_delta",
                "f_abs_peak_after_drop_delta",
                "f_abs_post_peak_sample_count",
                "c_sequence",
                "orientation_angle_degrees",
            ]
        )
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        for file in (self._events_jsonl_file, self._events_file, self._samples_file):
            if file is not None:
                file.close()

    def write_sample(
        self,
        elapsed_seconds: float,
        expected_label: str,
        sample: MotionSample,
    ) -> None:
        if self._sample_writer is None or self._samples_file is None:
            raise RuntimeError("CalibrationRecorder must be opened before writing")
        self._sample_writer.writerow(
            [
                f"{elapsed_seconds:.6f}",
                expected_label,
                sample.packet_id,
                *sample.values,
            ]
        )
        self._samples_file.flush()

    def write_event(self, event: dict) -> None:
        if (
            self._event_writer is None
            or self._events_file is None
            or self._events_jsonl_file is None
        ):
            raise RuntimeError("CalibrationRecorder must be opened before writing")
        features = event["features"]
        self._event_writer.writerow(
            [
                f"{event['elapsed_seconds']:.6f}",
                event["expected_label"],
                event["predicted_label"],
                event["confidence"],
                event["match"],
                event["reason"],
                features["gyro_p99"],
                features["accel_deviation_p99"],
                features["c_mean"],
                features["c_abs_p99"],
                features["lateral_gyro_p99"],
                features["lateral_accel_p99"],
                features["lateral_accel_area_norm"],
                features["lateral_accel_pca_ratio"],
                features["f_abs_peak_delta"],
                features["f_abs_drop_delta"],
                features["f_abs_peak_after_drop_delta"],
                features["f_abs_post_peak_sample_count"],
                features["c_sequence"],
                features["orientation_angle_degrees"],
            ]
        )
        self._events_file.flush()
        self._events_jsonl_file.write(json.dumps(event, separators=(",", ":")) + "\n")
        self._events_jsonl_file.flush()

    def write_summary(self, snapshot: dict) -> Path:
        for file in (self._samples_file, self._events_file, self._events_jsonl_file):
            if file is not None:
                file.flush()
        self.summary_json.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return self.summary_json
