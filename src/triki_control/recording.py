from __future__ import annotations

import csv
import contextlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, pstdev

from triki_control.protocol import MotionSample


AXIS_NAMES = ("a", "b", "c", "d", "e", "f")


@dataclass(frozen=True)
class RecordingPaths:
    csv: Path
    jsonl: Path
    summary_json: Path


def build_recording_paths(
    output_dir: Path,
    label: str,
    stamp: str | None = None,
) -> RecordingPaths:
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_label = _safe_filename(label)
    base = output_dir / f"{stamp}_{safe_label}"
    return RecordingPaths(
        csv=base.with_suffix(".csv"),
        jsonl=base.with_suffix(".jsonl"),
        summary_json=Path(str(base) + ".summary.json"),
    )


class SampleRecorder:
    def __init__(
        self,
        paths: RecordingPaths,
        label: str,
        summary_skip_seconds: float = 0.0,
        keep_empty: bool = False,
    ) -> None:
        self.paths = paths
        self.label = label
        self.summary_skip_seconds = summary_skip_seconds
        self.keep_empty = keep_empty
        self._records: list[tuple[float, MotionSample]] = []
        self._csv_file = None
        self._jsonl_file = None
        self._csv_writer = None

    def __enter__(self) -> "SampleRecorder":
        self.paths.csv.parent.mkdir(parents=True, exist_ok=True)
        self._csv_file = self.paths.csv.open("w", encoding="utf-8", newline="")
        self._jsonl_file = self.paths.jsonl.open("w", encoding="utf-8", newline="")
        self._csv_writer = csv.writer(self._csv_file, lineterminator="\n")
        self._csv_writer.writerow(
            ["sample_index", "elapsed_seconds", "label", "packet_id", *AXIS_NAMES]
        )
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        try:
            if self._records or self.keep_empty:
                summary = summarize_records(
                    self.label,
                    self._records,
                    skip_seconds=self.summary_skip_seconds,
                )
                self.paths.summary_json.write_text(
                    json.dumps(summary, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
        finally:
            if self._jsonl_file is not None:
                self._jsonl_file.close()
            if self._csv_file is not None:
                self._csv_file.close()
            if not self._records and not self.keep_empty:
                self.remove_files()

    @property
    def sample_count(self) -> int:
        return len(self._records)

    def record(self, elapsed_seconds: float, sample: MotionSample) -> None:
        if self._csv_writer is None or self._jsonl_file is None:
            raise RuntimeError("SampleRecorder must be used as a context manager")

        sample_index = len(self._records) + 1
        self._records.append((elapsed_seconds, sample))
        values = list(sample.values)
        elapsed_text = f"{elapsed_seconds:.6f}"
        self._csv_writer.writerow(
            [sample_index, elapsed_text, self.label, sample.packet_id, *values]
        )
        self._jsonl_file.write(
            json.dumps(
                {
                    "sample_index": sample_index,
                    "elapsed_seconds": round(elapsed_seconds, 6),
                    "label": self.label,
                    "packet_id": sample.packet_id,
                    "values": values,
                },
                separators=(",", ":"),
            )
            + "\n"
        )

    def remove_files(self) -> None:
        for path in (self.paths.csv, self.paths.jsonl, self.paths.summary_json):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()


def summarize_records(
    label: str,
    records: list[tuple[float, MotionSample]],
    skip_seconds: float = 0.0,
) -> dict:
    analyzed_records = [
        (elapsed, sample)
        for elapsed, sample in records
        if elapsed >= skip_seconds
    ]

    if analyzed_records:
        duration_seconds = round(analyzed_records[-1][0] - analyzed_records[0][0], 6)
    else:
        duration_seconds = 0.0

    axes = {}
    for axis_index, axis_name in enumerate(AXIS_NAMES):
        values = [
            sample.values[axis_index]
            for _elapsed, sample in analyzed_records
        ]
        if values:
            axis_min = min(values)
            axis_max = max(values)
            axes[axis_name] = {
                "min": axis_min,
                "max": axis_max,
                "range": axis_max - axis_min,
                "mean": round(mean(values), 6),
                "stdev": round(pstdev(values), 6),
            }
        else:
            axes[axis_name] = {
                "min": None,
                "max": None,
                "range": None,
                "mean": None,
                "stdev": None,
            }

    return {
        "label": label,
        "raw_sample_count": len(records),
        "sample_count": len(analyzed_records),
        "skipped_sample_count": len(records) - len(analyzed_records),
        "summary_skip_seconds": skip_seconds,
        "duration_seconds": duration_seconds,
        "axes": axes,
    }


def _safe_filename(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-")
    return slug or "triki"
