from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import mean

from triki_control.protocol import MotionSample


Record = tuple[float, MotionSample]
AXIS_NAMES = ("a", "b", "c", "d", "e", "f")
GRAVITY_UNITS = 2050.0


@dataclass(frozen=True)
class MotionFeatures:
    sample_count: int
    duration_seconds: float
    gyro_p90: float
    gyro_p99: float
    accel_deviation_p99: float
    accel_delta: float
    orientation_angle_degrees: float
    c_mean: float
    c_positive_fraction: float
    c_negative_fraction: float
    c_sign_runs: int
    c_sequence: str
    gyro_peak_count: int
    accel_peak_count: int
    f_abs_peak_delta: float = 0.0
    f_abs_drop_delta: float = 0.0
    f_abs_peak_after_drop_delta: float = 0.0
    f_abs_post_peak_sample_count: int = 0
    c_abs_p99: float = 0.0
    lateral_gyro_p99: float = 0.0
    lateral_accel_p99: float = 0.0
    lateral_accel_area_norm: float = 0.0
    lateral_accel_pca_ratio: float = 0.0


@dataclass(frozen=True)
class GesturePrediction:
    label: str
    confidence: float
    reason: str
    features: MotionFeatures


def load_recording_csv(path: Path) -> tuple[str, list[Record]]:
    with path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))

    if not rows:
        return path.stem, []

    records = []
    for row in rows:
        values = tuple(int(row[axis]) for axis in AXIS_NAMES)
        records.append(
            (
                float(row["elapsed_seconds"]),
                MotionSample(packet_id=int(row["packet_id"]), values=values),  # type: ignore[arg-type]
            )
        )
    return rows[0].get("label") or path.stem, records


def extract_features(
    records: list[Record],
    *,
    startup_skip_seconds: float = 0.25,
    gravity_units: float = GRAVITY_UNITS,
) -> MotionFeatures:
    analyzed = [(t, sample) for t, sample in records if t >= startup_skip_seconds]
    if not analyzed:
        analyzed = list(records)

    if not analyzed:
        return MotionFeatures(
            sample_count=0,
            duration_seconds=0.0,
            gyro_p90=0.0,
            gyro_p99=0.0,
            accel_deviation_p99=0.0,
            accel_delta=0.0,
            orientation_angle_degrees=0.0,
            c_mean=0.0,
            c_positive_fraction=0.0,
            c_negative_fraction=0.0,
            c_sign_runs=0,
            c_sequence="",
            gyro_peak_count=0,
            accel_peak_count=0,
        )

    times = [t for t, _sample in analyzed]
    values = [sample.values for _t, sample in analyzed]
    gyro = [_magnitude(row[:3]) for row in values]
    lateral_gyro = [math.hypot(row[0], row[1]) for row in values]
    accel = [_magnitude(row[3:]) for row in values]
    lateral_accel = [math.hypot(row[3], row[4]) for row in values]
    accel_deviation = [abs(value - gravity_units) for value in accel]
    c_values = [row[2] for row in values]
    c_abs_values = [abs(value) for value in c_values]
    f_values = [row[5] for row in values]
    runs = _sign_runs(times, c_values, threshold=1000, min_duration_seconds=0.05)
    (
        f_abs_peak_delta,
        f_abs_drop_delta,
        f_abs_peak_after_drop_delta,
        f_abs_post_peak_sample_count,
    ) = _axis_abs_motion(f_values)

    return MotionFeatures(
        sample_count=len(analyzed),
        duration_seconds=times[-1] - times[0] if len(times) > 1 else 0.0,
        gyro_p90=_percentile(gyro, 0.90),
        gyro_p99=_percentile(gyro, 0.99),
        accel_deviation_p99=_percentile(accel_deviation, 0.99),
        accel_delta=_vector_delta(values),
        orientation_angle_degrees=_orientation_angle(values),
        c_mean=mean(c_values),
        c_positive_fraction=_fraction(c_values, lambda value: value > 500),
        c_negative_fraction=_fraction(c_values, lambda value: value < -500),
        c_sign_runs=len(runs),
        c_sequence="".join("+" if sign > 0 else "-" for sign, _start, _end, _peak in runs[:8]),
        gyro_peak_count=_peak_count(times, gyro, threshold=1200, refractory_seconds=0.30),
        accel_peak_count=_peak_count(
            times,
            accel_deviation,
            threshold=450,
            refractory_seconds=0.30,
        ),
        f_abs_peak_delta=f_abs_peak_delta,
        f_abs_drop_delta=f_abs_drop_delta,
        f_abs_peak_after_drop_delta=f_abs_peak_after_drop_delta,
        f_abs_post_peak_sample_count=f_abs_post_peak_sample_count,
        c_abs_p99=_percentile(c_abs_values, 0.99),
        lateral_gyro_p99=_percentile(lateral_gyro, 0.99),
        lateral_accel_p99=_percentile(lateral_accel, 0.99),
        lateral_accel_area_norm=_signed_area_norm(values, 3, 4),
        lateral_accel_pca_ratio=_pca_minor_major_ratio(values, 3, 4),
    )


def classify_records(records: list[Record]) -> GesturePrediction:
    return classify_features(extract_features(records))


def classify_features(features: MotionFeatures) -> GesturePrediction:
    if features.sample_count == 0:
        return _prediction("unknown", 0.0, "no samples", features)

    if features.gyro_p99 < 180 and features.accel_deviation_p99 < 80:
        return _prediction("still", 0.99, "near-zero gyro and stable gravity vector", features)

    if features.gyro_p99 >= 25000:
        return _prediction("unknown", 0.30, "extreme toss-like motion is not a controller gesture", features)

    if (
        features.orientation_angle_degrees >= 55
        and features.gyro_p99 >= 4000
        and features.lateral_gyro_p99 >= 3500
        and features.lateral_accel_p99 <= 3900
    ):
        return _prediction("flip-over", 0.82, "large gravity-vector orientation change", features)

    if (
        features.gyro_p99 < 5000
        and features.accel_deviation_p99 >= 320
        and features.lateral_accel_p99 <= 3000
        and features.c_abs_p99 < 3500
        and features.f_abs_drop_delta >= 350
        and features.f_abs_peak_after_drop_delta >= 320
        and features.f_abs_post_peak_sample_count >= 5
        and features.gyro_p99
        <= max(5000, features.f_abs_peak_after_drop_delta * 1.35)
        and features.f_abs_peak_after_drop_delta > features.f_abs_drop_delta * 1.45
    ):
        return _prediction("lift", 0.68, "vertical set-down impact", features)

    if features.accel_deviation_p99 >= 6500:
        return _prediction("back-forth", 0.92, "large repeated acceleration deviation", features)

    if (
        features.gyro_p99 >= 3500
        and features.lateral_gyro_p99 >= 3500
        and features.lateral_accel_p99 >= 1000
        and features.orientation_angle_degrees >= 30
        and features.orientation_angle_degrees < 45
        and features.lateral_accel_p99 <= 3900
        and features.accel_deviation_p99 < 1800
    ):
        return _prediction("flip-over", 0.70, "high lateral-gyro flip tail", features)

    has_table_translation = (
        features.gyro_p99 >= 500
        and features.lateral_accel_p99 >= 350
        and features.lateral_gyro_p99 <= 3000
        and features.accel_deviation_p99 < 1800
        and features.c_abs_p99 < 4000
        and features.orientation_angle_degrees < 45
    )
    has_clockwise_scrub_loop = (
        features.lateral_accel_p99 >= 600
        and features.lateral_accel_area_norm >= 0.35
        and features.lateral_accel_pca_ratio >= 0.20
    )
    has_counterclockwise_scrub_loop = (
        (
            (
                features.lateral_accel_p99 >= 1050
                and features.lateral_accel_area_norm <= -0.35
            )
            or (
                features.lateral_accel_p99 >= 600
                and features.lateral_accel_area_norm <= -0.60
            )
        )
        and features.lateral_accel_pca_ratio >= 0.20
    )
    if (
        has_table_translation
        and (has_clockwise_scrub_loop or has_counterclockwise_scrub_loop)
        and features.accel_deviation_p99 <= 980
        and features.f_abs_drop_delta <= 700
    ):
        label = "scrub-cw" if features.lateral_accel_area_norm > 0 else "scrub-ccw"
        return _prediction(label, 0.74, "signed lateral accelerometer loop", features)

    if (
        has_table_translation
        and features.lateral_accel_p99 >= 1500
        and features.lateral_gyro_p99 <= 2200
        and features.c_abs_p99 <= 2200
        and features.f_abs_drop_delta <= 700
        and features.f_abs_peak_after_drop_delta <= 500
        and features.orientation_angle_degrees < 25
        and features.lateral_accel_pca_ratio >= 0.005
        and features.lateral_accel_pca_ratio <= 0.16
    ):
        return _prediction("back-forth", 0.70, "linear lateral table motion", features)

    twist_start_sign = _first_alternating_sign(features.c_sequence, min_length=4)
    if (
        features.gyro_p99 >= 6500
        and features.c_sign_runs >= 4
        and features.c_positive_fraction >= 0.12
        and features.c_negative_fraction >= 0.12
        and twist_start_sign
    ):
        label = (
            "twist-cw-ccw-cw-ccw"
            if twist_start_sign > 0
            else "twist-ccw-cw-ccw-cw"
        )
        return _prediction(label, 0.86, "alternating table twist on c-axis", features)

    if (
        features.c_mean >= 600
        and features.c_positive_fraction >= 0.20
        and features.accel_deviation_p99 < 900
    ):
        return _prediction("rotate-cw", 0.88, "dominant positive c-axis rotation", features)

    if (
        features.c_mean <= -600
        and features.c_negative_fraction >= 0.20
        and features.accel_deviation_p99 < 900
    ):
        return _prediction("rotate-ccw", 0.88, "dominant negative c-axis rotation", features)

    if (
        features.gyro_p99 >= 9000
        and features.accel_deviation_p99 >= 800
        and features.c_sign_runs <= 2
        and features.lateral_gyro_p99 >= 3500
        and features.lateral_accel_p99 <= 3900
    ):
        return _prediction("flip-over", 0.74, "short high-gyro flip impulse", features)

    if (
        features.accel_delta >= 2000
        and features.gyro_p99 >= 6500
        and features.lateral_gyro_p99 >= 3500
        and features.lateral_accel_p99 <= 3900
    ):
        return _prediction("flip-over", 0.78, "large start-to-end acceleration vector change", features)

    if (
        4200 <= features.gyro_p99 < 9000
        and features.accel_deviation_p99 >= 1000
        and features.lateral_gyro_p99 >= 4200
        and features.lateral_accel_p99 <= 2200
        and features.c_abs_p99 < 3500
        and features.orientation_angle_degrees >= 15
        and features.orientation_angle_degrees < 45
    ):
        return _prediction("flip-over", 0.72, "moderate low-lateral-accel flip impulse", features)

    if (
        features.accel_deviation_p99 >= 2200
        and features.lateral_accel_p99 >= 3000
        and features.orientation_angle_degrees < 45
    ):
        return _prediction("back-forth", 0.72, "forceful lateral table back-and-forth", features)

    if (
        6500 <= features.gyro_p99 < 9000
        and features.accel_deviation_p99 >= 1800
        and features.lateral_accel_p99 >= 2200
        and features.c_abs_p99 < 6000
        and features.orientation_angle_degrees < 45
    ):
        return _prediction("back-forth", 0.68, "high-energy repeated back-and-forth disturbance", features)

    return _prediction("unknown", 0.25, "no rule matched cleanly", features)


def _prediction(
    label: str,
    confidence: float,
    reason: str,
    features: MotionFeatures,
) -> GesturePrediction:
    return GesturePrediction(
        label=label,
        confidence=confidence,
        reason=reason,
        features=features,
    )


def _magnitude(values: tuple[int, ...]) -> float:
    return math.sqrt(sum(value * value for value in values))


def _percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(round((len(ordered) - 1) * quantile))
    return ordered[index]


def _fraction(values: list[int], predicate) -> float:
    if not values:
        return 0.0
    return sum(1 for value in values if predicate(value)) / len(values)


def _signed_area_norm(
    values: list[tuple[int, int, int, int, int, int]],
    x_index: int,
    y_index: int,
) -> float:
    if len(values) < 3:
        return 0.0
    x_values = [row[x_index] for row in values]
    y_values = [row[y_index] for row in values]
    x_mean = mean(x_values)
    y_mean = mean(y_values)
    area = 0.0
    scale = 0.0
    for index in range(1, len(values)):
        x0 = x_values[index - 1] - x_mean
        y0 = y_values[index - 1] - y_mean
        x1 = x_values[index] - x_mean
        y1 = y_values[index] - y_mean
        area += x0 * y1 - y0 * x1
        scale += math.hypot(x1 - x0, y1 - y0) * max(math.hypot(x0, y0), 1.0)
    if not scale:
        return 0.0
    return area / scale


def _pca_minor_major_ratio(
    values: list[tuple[int, int, int, int, int, int]],
    x_index: int,
    y_index: int,
) -> float:
    if len(values) < 3:
        return 0.0
    x_values = [row[x_index] for row in values]
    y_values = [row[y_index] for row in values]
    x_mean = mean(x_values)
    y_mean = mean(y_values)
    xx = mean((x - x_mean) ** 2 for x in x_values)
    yy = mean((y - y_mean) ** 2 for y in y_values)
    xy = mean((x - x_mean) * (y - y_mean) for x, y in zip(x_values, y_values))
    trace = xx + yy
    determinant = xx * yy - xy * xy
    discriminant = math.sqrt(max(0.0, trace * trace - 4 * determinant))
    major = (trace + discriminant) / 2
    minor = (trace - discriminant) / 2
    if major <= 0:
        return 0.0
    return max(0.0, minor / major)


def _peak_count(
    times: list[float],
    signal: list[float],
    *,
    threshold: float,
    refractory_seconds: float,
) -> int:
    count = 0
    last_peak_at = -1e9
    for time_value, signal_value in zip(times, signal):
        if signal_value >= threshold and time_value - last_peak_at >= refractory_seconds:
            count += 1
            last_peak_at = time_value
    return count


def _sign_runs(
    times: list[float],
    values: list[int],
    *,
    threshold: int,
    min_duration_seconds: float,
) -> list[tuple[int, float, float, int]]:
    runs: list[tuple[int, float, float, int]] = []
    active_sign = 0
    run_start = 0.0
    run_peak = 0

    for time_value, value in zip(times, values):
        sign = 1 if value > threshold else -1 if value < -threshold else 0
        if sign == 0:
            if active_sign and time_value - run_start >= min_duration_seconds:
                runs.append((active_sign, run_start, time_value, run_peak))
            active_sign = 0
            run_peak = 0
            continue

        if not active_sign:
            active_sign = sign
            run_start = time_value
            run_peak = abs(value)
        elif active_sign == sign:
            run_peak = max(run_peak, abs(value))
        else:
            if time_value - run_start >= min_duration_seconds:
                runs.append((active_sign, run_start, time_value, run_peak))
            active_sign = sign
            run_start = time_value
            run_peak = abs(value)

    if active_sign and times[-1] - run_start >= min_duration_seconds:
        runs.append((active_sign, run_start, times[-1], run_peak))
    return runs


def _vector_delta(values: list[tuple[int, int, int, int, int, int]]) -> float:
    before, after = _edge_vectors(values)
    return math.sqrt(sum((first - second) ** 2 for first, second in zip(before, after)))


def _orientation_angle(values: list[tuple[int, int, int, int, int, int]]) -> float:
    before, after = _edge_vectors(values)
    before_magnitude = _magnitude(before)
    after_magnitude = _magnitude(after)
    if not before_magnitude or not after_magnitude:
        return 0.0
    dot = sum(first * second for first, second in zip(before, after))
    ratio = max(-1.0, min(1.0, dot / (before_magnitude * after_magnitude)))
    return math.degrees(math.acos(ratio))


def _edge_vectors(
    values: list[tuple[int, int, int, int, int, int]],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    edge_count = min(10, max(1, len(values) // 4))
    before = _mean_accel_vector(values[:edge_count])
    after = _mean_accel_vector(values[-edge_count:])
    return before, after


def _mean_accel_vector(
    values: list[tuple[int, int, int, int, int, int]],
) -> tuple[float, float, float]:
    return (
        mean(row[3] for row in values),
        mean(row[4] for row in values),
        mean(row[5] for row in values),
    )


def _axis_abs_motion(values: list[int]) -> tuple[float, float, float, int]:
    if not values:
        return 0.0, 0.0, 0.0, 0
    edge_count = min(10, max(1, len(values) // 4))
    baseline = mean(abs(value) for value in values[:edge_count])
    absolute_values = [abs(value) for value in values]
    min_index, min_value = min(
        enumerate(absolute_values),
        key=lambda indexed_value: indexed_value[1],
    )
    post_drop_peak = max(absolute_values[min_index:])
    post_drop_peak_index = max(
        index
        for index in range(min_index, len(absolute_values))
        if absolute_values[index] == post_drop_peak
    )
    return (
        max(0.0, max(absolute_values) - baseline),
        max(0.0, baseline - min_value),
        max(0.0, post_drop_peak - baseline),
        max(0, len(absolute_values) - post_drop_peak_index - 1),
    )


def _alternates(sequence: str) -> bool:
    return all(left != right for left, right in zip(sequence, sequence[1:]))


def _first_alternating_sign(sequence: str, *, min_length: int) -> int:
    if len(sequence) < min_length:
        return 0
    for start in range(0, len(sequence) - min_length + 1):
        chunk = sequence[start : start + min_length]
        if _alternates(chunk):
            return 1 if chunk.startswith("+") else -1
    return 0


def _dominant_rotation_sign(features: MotionFeatures) -> int:
    if features.c_mean >= 1000 and features.c_positive_fraction >= 0.25:
        return 1
    if features.c_mean <= -1000 and features.c_negative_fraction >= 0.25:
        return -1
    return 0
