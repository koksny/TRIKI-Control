from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path

from triki_control.classifier import classify_records, load_recording_csv
from triki_control.gestures import normalize_gesture_label


def normalize_label(label: str) -> str:
    normalized = label.replace("_", "-")
    if normalized.startswith("rotate-cw-ccw") or normalized.startswith("twist-cw-ccw"):
        return "twist-cw-ccw-cw-ccw"
    if normalized.startswith("rotate-ccw-cw") or normalized.startswith("twist-ccw-cw"):
        return "twist-ccw-cw-ccw-cw"
    if normalized in {"lift-up", "lift-down"}:
        return "lift"
    if normalized in {"slide-back-forth", "rock-edge"}:
        return "back-forth"
    return normalize_gesture_label(normalized)


def analyze_recordings(recording_dir: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(recording_dir.glob("*.csv")):
        source_label, records = load_recording_csv(path)
        prediction = classify_records(records)
        features = prediction.features
        expected = normalize_label(source_label)
        predicted = normalize_label(prediction.label)
        rows.append(
            {
                "file": path.name,
                "source_label": source_label,
                "expected_label": expected,
                "predicted_label": prediction.label,
                "match": expected == predicted,
                "confidence": round(prediction.confidence, 3),
                "reason": prediction.reason,
                "sample_count": features.sample_count,
                "duration_seconds": round(features.duration_seconds, 3),
                "gyro_p99": round(features.gyro_p99, 1),
                "accel_deviation_p99": round(features.accel_deviation_p99, 1),
                "accel_delta": round(features.accel_delta, 1),
                "orientation_angle_degrees": round(features.orientation_angle_degrees, 1),
                "c_mean": round(features.c_mean, 1),
                "c_positive_fraction": round(features.c_positive_fraction, 3),
                "c_negative_fraction": round(features.c_negative_fraction, 3),
                "c_sign_runs": features.c_sign_runs,
                "c_sequence": features.c_sequence,
                "gyro_peak_count": features.gyro_peak_count,
                "accel_peak_count": features.accel_peak_count,
                "f_abs_peak_delta": round(features.f_abs_peak_delta, 1),
                "f_abs_drop_delta": round(features.f_abs_drop_delta, 1),
            }
        )
    return rows


def write_csv(rows: list[dict[str, object]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "file",
        "source_label",
        "expected_label",
        "predicted_label",
        "match",
        "confidence",
        "reason",
        "sample_count",
        "duration_seconds",
        "gyro_p99",
        "accel_deviation_p99",
        "accel_delta",
        "orientation_angle_degrees",
        "c_mean",
        "c_positive_fraction",
        "c_negative_fraction",
        "c_sign_runs",
        "c_sequence",
        "gyro_peak_count",
        "accel_peak_count",
        "f_abs_peak_delta",
        "f_abs_drop_delta",
    ]
    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(rows: list[dict[str, object]], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    label_counts = Counter(str(row["source_label"]) for row in rows)
    correct = sum(1 for row in rows if row["match"])
    by_expected: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        by_expected[str(row["expected_label"])][str(row["predicted_label"])] += 1

    lines = [
        "# TRIKI Recording Analysis",
        "",
        f"- Recordings: {len(rows)}",
        f"- File-level rule matches: {correct}/{len(rows)}" if rows else "- File-level rule matches: 0/0",
        "",
        "## Label Counts",
        "",
        "| Label | Files |",
        "| --- | ---: |",
    ]
    for label, count in sorted(label_counts.items()):
        lines.append(f"| {label} | {count} |")

    lines.extend(
        [
            "",
            "## Confusion Summary",
            "",
            "| Expected | Predicted counts |",
            "| --- | --- |",
        ]
    )
    for expected, predictions in sorted(by_expected.items()):
        counts = ", ".join(
            f"{predicted}: {count}"
            for predicted, count in sorted(predictions.items())
        )
        lines.append(f"| {expected} | {counts} |")

    lines.extend(
        [
            "",
            "## Most Useful Signals",
            "",
            "- `still`: gyro p99 is near zero and acceleration remains near one gravity vector.",
            "- `rotate-cw` / `rotate-ccw`: sign and mean of axis `c` are the strongest signal.",
            "- `twist-cw-ccw-cw-ccw` / `twist-ccw-cw-ccw-cw`: four alternating sign runs on axis `c` with meaningful energy in both directions.",
            "- `back-forth`: straight table slide is treated as one back action instead of separate slide or edge labels.",
            "- `flip-over`: large start-to-end gravity-vector angle or acceleration-vector delta.",
            "- `lift`: experimental vertical acceleration peak/drop gesture.",
            "- Taps and toss-catch are not treated as controller gestures.",
            "",
        ]
    )
    output_path.write_text("\n".join(lines), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze TRIKI recording CSV files.")
    parser.add_argument("--recording-dir", type=Path, default=Path("output/triki"))
    parser.add_argument("--csv-output", type=Path, default=Path("output/triki_analysis.csv"))
    parser.add_argument("--md-output", type=Path, default=Path("output/triki_analysis.md"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    rows = analyze_recordings(args.recording_dir)
    write_csv(rows, args.csv_output)
    write_markdown(rows, args.md_output)
    correct = sum(1 for row in rows if row["match"])
    print(f"ANALYZED recordings={len(rows)} matches={correct}/{len(rows)}")
    print(f"WROTE_CSV {args.csv_output}")
    print(f"WROTE_MD {args.md_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
