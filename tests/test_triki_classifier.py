import unittest
import math

from triki_control.classifier import MotionFeatures, classify_features, classify_records, extract_features
from triki_control.protocol import MotionSample


def records(values):
    return [
        (index * 0.05, MotionSample(packet_id=index % 16, values=value))
        for index, value in enumerate(values)
    ]


def stable_sample(a=0, b=0, c=0, d=0, e=0, f=2050):
    return (a, b, c, d, e, f)


def circle_samples(*, clockwise=True, count=80, radius=700, gyro=900):
    direction = 1 if clockwise else -1
    samples = []
    for index in range(count):
        angle = direction * 2 * math.pi * index / 20
        samples.append(
            stable_sample(
                a=int(gyro * math.sin(angle)),
                b=int(gyro * math.cos(angle)),
                c=0,
                d=int(radius * math.cos(angle)),
                e=int(radius * math.sin(angle)),
                f=2050,
            )
        )
    return samples


def line_samples(*, count=80, amplitude=950, gyro=900):
    samples = []
    for index in range(count):
        phase = math.sin(2 * math.pi * index / 16)
        samples.append(
            stable_sample(
                a=int(gyro * phase),
                b=80,
                c=0,
                d=int(amplitude * phase),
                e=60 + int(120 * math.cos(2 * math.pi * index / 16)),
                f=2050,
            )
        )
    return samples


def features(**overrides):
    defaults = {
        "sample_count": 24,
        "duration_seconds": 0.75,
        "gyro_p90": 0.0,
        "gyro_p99": 0.0,
        "accel_deviation_p99": 0.0,
        "accel_delta": 0.0,
        "orientation_angle_degrees": 0.0,
        "c_mean": 0.0,
        "c_positive_fraction": 0.0,
        "c_negative_fraction": 0.0,
        "c_sign_runs": 0,
        "c_sequence": "",
        "gyro_peak_count": 0,
        "accel_peak_count": 0,
        "f_abs_peak_delta": 0.0,
        "f_abs_drop_delta": 0.0,
        "f_abs_peak_after_drop_delta": 0.0,
        "f_abs_post_peak_sample_count": 0,
            "c_abs_p99": 0.0,
            "lateral_gyro_p99": 0.0,
            "lateral_accel_p99": 0.0,
            "lateral_accel_area_norm": 0.0,
            "lateral_accel_pca_ratio": 0.0,
        }
    defaults.update(overrides)
    return MotionFeatures(**defaults)


class TrikiClassifierTests(unittest.TestCase):
    def test_classifies_still_baseline(self):
        prediction = classify_records(records([stable_sample(c=20)] * 100))

        self.assertEqual(prediction.label, "still")

    def test_classifies_clockwise_rotation_from_positive_c_axis(self):
        samples = [stable_sample(c=2800) for _ in range(100)]

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "rotate-cw")

    def test_classifies_gentle_clockwise_rotation(self):
        samples = [stable_sample(c=650) for _ in range(100)]

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "rotate-cw")

    def test_classifies_counterclockwise_rotation_from_negative_c_axis(self):
        samples = [stable_sample(c=-2800) for _ in range(100)]

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "rotate-ccw")

    def test_classifies_gentle_counterclockwise_rotation(self):
        samples = [stable_sample(c=-650) for _ in range(100)]

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "rotate-ccw")

    def test_extracts_signed_lateral_loop_features(self):
        clockwise = extract_features(records(circle_samples(clockwise=True)))
        counterclockwise = extract_features(records(circle_samples(clockwise=False)))
        line = extract_features(records(line_samples()))

        self.assertGreater(clockwise.lateral_accel_area_norm, 0.15)
        self.assertLess(counterclockwise.lateral_accel_area_norm, -0.15)
        self.assertGreater(clockwise.lateral_accel_pca_ratio, 0.20)
        self.assertLess(line.lateral_accel_pca_ratio, 0.12)

    def test_classifies_clockwise_scrub_from_lateral_circle_without_self_rotation(self):
        prediction = classify_records(records(circle_samples(clockwise=True)))

        self.assertEqual(prediction.label, "scrub-cw")

    def test_classifies_counterclockwise_scrub_from_lateral_circle_without_self_rotation(self):
        prediction = classify_records(records(circle_samples(clockwise=False)))

        self.assertEqual(prediction.label, "scrub-ccw")

    def test_classifies_back_forth_from_linear_lateral_motion_without_shake_energy(self):
        prediction = classify_records(records(line_samples(amplitude=1600)))

        self.assertEqual(prediction.label, "back-forth")

    def test_keeps_clockwise_rotation_with_lateral_motion_as_rotation(self):
        samples = [
            stable_sample(a=2700, b=400, c=14000, d=1100, e=300)
            for _ in range(100)
        ]

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "rotate-cw")

    def test_keeps_counterclockwise_rotation_with_lateral_motion_as_rotation(self):
        samples = [
            stable_sample(a=-2700, b=-400, c=-14000, d=-1100, e=-300)
            for _ in range(100)
        ]

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "rotate-ccw")

    def test_keeps_lower_lateral_clockwise_rotation_as_rotation(self):
        samples = [
            stable_sample(a=2300, b=300, c=14000, d=1100, e=300)
            for _ in range(100)
        ]

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "rotate-cw")

    def test_keeps_lower_lateral_counterclockwise_rotation_as_rotation(self):
        samples = [
            stable_sample(a=-2300, b=-300, c=-14000, d=-1100, e=-300)
            for _ in range(100)
        ]

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "rotate-ccw")

    def test_keeps_wide_fast_rotation_with_lower_lateral_gyro_as_rotation(self):
        samples = [
            stable_sample(a=1700, b=200, c=18000, d=1500, e=300)
            for _ in range(100)
        ]

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "rotate-cw")

    def test_keeps_compact_rotation_with_low_lateral_acceleration_as_rotation(self):
        samples = [
            stable_sample(a=2700, b=200, c=1200, d=600, e=150)
            for _ in range(80)
        ] + [
            stable_sample(a=2700, b=200, c=8500, d=600, e=150)
            for _ in range(20)
        ]

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "rotate-cw")

    def test_slightly_tilted_compact_counterclockwise_motion_is_not_scrub(self):
        prediction = classify_features(
            features(
                gyro_p99=8617.9,
                accel_deviation_p99=1064.1,
                c_mean=-2024.8,
                c_negative_fraction=0.42,
                c_abs_p99=8306.0,
                lateral_gyro_p99=2801.2,
                lateral_accel_p99=829.4,
                orientation_angle_degrees=5.35,
            )
        )

        self.assertNotIn(prediction.label, {"scrub-cw", "scrub-ccw"})

    def test_keeps_smaller_rotation_with_lateral_motion_as_rotation(self):
        samples = [
            stable_sample(a=2300, b=250, c=5600, d=1200, e=150)
            for _ in range(100)
        ]

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "rotate-cw")

    def test_classifies_batch_clockwise_scrub_from_lateral_loop(self):
        prediction = classify_features(
            features(
                gyro_p99=1964.178,
                accel_deviation_p99=377.071,
                c_mean=40.0,
                c_abs_p99=1894.0,
                lateral_gyro_p99=847.937,
                lateral_accel_p99=1287.418,
                lateral_accel_area_norm=0.599,
                lateral_accel_pca_ratio=0.753,
                f_abs_drop_delta=211.4,
                f_abs_peak_after_drop_delta=132.6,
                orientation_angle_degrees=10.806,
            )
        )

        self.assertEqual(prediction.label, "scrub-cw")

    def test_classifies_batch_counterclockwise_scrub_from_lateral_loop(self):
        prediction = classify_features(
            features(
                gyro_p99=2261.363,
                accel_deviation_p99=532.897,
                c_mean=-60.0,
                c_abs_p99=2116.0,
                lateral_gyro_p99=884.997,
                lateral_accel_p99=1618.842,
                lateral_accel_area_norm=-0.796,
                lateral_accel_pca_ratio=0.555,
                f_abs_drop_delta=167.3,
                f_abs_peak_after_drop_delta=123.778,
                orientation_angle_degrees=34.582,
            )
        )

        self.assertEqual(prediction.label, "scrub-ccw")

    def test_new_batch_clockwise_rotation_with_lateral_motion_is_not_scrub(self):
        prediction = classify_features(
            features(
                gyro_p99=19562.051,
                accel_deviation_p99=805.062,
                c_mean=2074.067,
                c_positive_fraction=0.35,
                c_abs_p99=19508.0,
                lateral_gyro_p99=2740.523,
                lateral_accel_p99=1800.201,
                lateral_accel_area_norm=0.416,
                lateral_accel_pca_ratio=0.516,
                f_abs_drop_delta=480.429,
                f_abs_peak_after_drop_delta=207.571,
                f_abs_post_peak_sample_count=3,
                orientation_angle_degrees=7.586,
            )
        )

        self.assertEqual(prediction.label, "rotate-cw")

    def test_new_batch_counterclockwise_rotation_with_lateral_motion_is_not_scrub(self):
        prediction = classify_features(
            features(
                gyro_p99=14518.115,
                accel_deviation_p99=643.895,
                c_mean=-2170.622,
                c_negative_fraction=0.35,
                c_abs_p99=14491.0,
                lateral_gyro_p99=2418.721,
                lateral_accel_p99=1189.866,
                lateral_accel_area_norm=-0.245,
                lateral_accel_pca_ratio=0.364,
                f_abs_drop_delta=672.333,
                f_abs_peak_after_drop_delta=0.0,
                orientation_angle_degrees=1.933,
            )
        )

        self.assertEqual(prediction.label, "rotate-ccw")

    def test_rotation_preroll_with_small_lateral_loop_is_not_scrub(self):
        prediction = classify_features(
            features(
                gyro_p99=2258.859,
                accel_deviation_p99=151.821,
                c_mean=112.641,
                c_abs_p99=2258.0,
                lateral_gyro_p99=1757.764,
                lateral_accel_p99=677.067,
                lateral_accel_area_norm=0.251,
                lateral_accel_pca_ratio=0.187,
                f_abs_drop_delta=154.222,
                f_abs_peak_after_drop_delta=136.778,
                f_abs_post_peak_sample_count=25,
                orientation_angle_degrees=1.688,
            )
        )

        self.assertNotIn(prediction.label, {"scrub-cw", "scrub-ccw", "back-forth"})

    def test_rotation_tail_with_linear_lateral_noise_is_not_back_forth(self):
        prediction = classify_features(
            features(
                gyro_p99=1692.577,
                accel_deviation_p99=238.919,
                c_mean=-3.0,
                c_abs_p99=1405.0,
                lateral_gyro_p99=943.818,
                lateral_accel_p99=735.976,
                lateral_accel_area_norm=-0.057,
                lateral_accel_pca_ratio=0.132,
                f_abs_drop_delta=145.4,
                f_abs_peak_after_drop_delta=146.6,
                orientation_angle_degrees=0.517,
            )
        )

        self.assertNotEqual(prediction.label, "back-forth")

    def test_back_forth_arc_with_weak_counterclockwise_area_is_not_scrub(self):
        prediction = classify_features(
            features(
                gyro_p99=962.968,
                accel_deviation_p99=414.926,
                c_mean=-44.119,
                c_abs_p99=945.0,
                lateral_gyro_p99=557.918,
                lateral_accel_p99=1081.03,
                lateral_accel_area_norm=-0.126,
                lateral_accel_pca_ratio=0.363,
                f_abs_drop_delta=76.4,
                f_abs_peak_after_drop_delta=165.6,
                f_abs_post_peak_sample_count=1,
                orientation_angle_degrees=14.481,
            )
        )

        self.assertNotIn(prediction.label, {"scrub-cw", "scrub-ccw"})

    def test_stamp_lateral_slide_before_setdown_is_not_back_forth(self):
        prediction = classify_features(
            features(
                gyro_p99=1771.653,
                accel_deviation_p99=1262.685,
                c_mean=-19.909,
                c_abs_p99=1620.0,
                lateral_gyro_p99=1453.669,
                lateral_accel_p99=1324.126,
                lateral_accel_area_norm=0.157,
                lateral_accel_pca_ratio=0.05,
                f_abs_peak_delta=1124.6,
                f_abs_drop_delta=318.4,
                f_abs_peak_after_drop_delta=0.0,
                f_abs_post_peak_sample_count=0,
                orientation_angle_degrees=1.099,
            )
        )

        self.assertNotEqual(prediction.label, "back-forth")

    def test_flip_low_orientation_tail_is_not_scrub(self):
        prediction = classify_features(
            features(
                gyro_p99=1712.664,
                accel_deviation_p99=471.046,
                c_mean=-88.846,
                c_abs_p99=1677.0,
                lateral_gyro_p99=809.03,
                lateral_accel_p99=887.658,
                lateral_accel_area_norm=0.274,
                lateral_accel_pca_ratio=0.293,
                f_abs_drop_delta=233.333,
                f_abs_peak_after_drop_delta=424.667,
                f_abs_post_peak_sample_count=0,
                orientation_angle_degrees=4.807,
            )
        )

        self.assertNotIn(prediction.label, {"scrub-cw", "scrub-ccw"})

    def test_stamp_vertical_energy_is_not_scrub(self):
        prediction = classify_features(
            features(
                gyro_p99=1379.538,
                accel_deviation_p99=909.214,
                c_mean=-54.5,
                c_abs_p99=939.0,
                lateral_gyro_p99=1333.61,
                lateral_accel_p99=482.188,
                lateral_accel_area_norm=0.146,
                lateral_accel_pca_ratio=0.48,
                f_abs_peak_delta=865.0,
                f_abs_drop_delta=839.0,
                f_abs_peak_after_drop_delta=775.0,
                f_abs_post_peak_sample_count=2,
                orientation_angle_degrees=5.46,
            )
        )

        self.assertNotIn(prediction.label, {"scrub-cw", "scrub-ccw"})

    def test_high_lateral_gyro_flip_tail_is_not_table_motion(self):
        prediction = classify_features(
            features(
                gyro_p99=4583.31,
                accel_deviation_p99=534.439,
                c_mean=-161.511,
                c_abs_p99=2382.0,
                lateral_gyro_p99=4583.262,
                lateral_accel_p99=1526.232,
                lateral_accel_area_norm=0.424,
                lateral_accel_pca_ratio=0.211,
                f_abs_drop_delta=960.9,
                f_abs_peak_after_drop_delta=0.0,
                orientation_angle_degrees=34.835,
            )
        )

        self.assertEqual(prediction.label, "flip-over")

    def test_keeps_fast_rotation_with_low_lateral_accel_as_rotation(self):
        samples = [
            stable_sample(a=2300, b=300, c=18000, d=900, e=300)
            for _ in range(100)
        ]

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "rotate-cw")

    def test_keeps_plain_fast_rotation_as_rotation(self):
        samples = [
            stable_sample(a=900, b=200, c=14000, d=350, e=120)
            for _ in range(100)
        ]

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "rotate-cw")

    def test_classifies_clockwise_first_twist_from_four_sign_runs(self):
        samples = []
        for sign in [1, -1, 1, -1]:
            samples.extend([stable_sample(c=sign * 14000)] * 20)

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "twist-cw-ccw-cw-ccw")

    def test_classifies_counterclockwise_first_twist_from_four_sign_runs(self):
        samples = []
        for sign in [-1, 1, -1, 1]:
            samples.extend([stable_sample(c=sign * 14000)] * 20)
        samples.extend([stable_sample(c=0)] * 3)
        samples.extend([stable_sample(c=-14000)] * 20)

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "twist-ccw-cw-ccw-cw")

    def test_classifies_twist_with_one_noisy_duplicate_run(self):
        samples = []
        for sign in [1, -1, 1, 0, 1, -1, 1, -1]:
            samples.extend([stable_sample(c=sign * 14000)] * 12)

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "twist-cw-ccw-cw-ccw")

    def test_classifies_back_forth_from_large_acceleration_deviation(self):
        samples = []
        for index in range(100):
            f = 14000 if index % 2 else -10000
            samples.append(stable_sample(c=4500, f=f))

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "back-forth")

    def test_extreme_toss_like_motion_is_not_a_controller_gesture(self):
        samples = [stable_sample(c=0) for _ in range(90)]
        samples.extend([stable_sample(a=35000, b=-16000, c=22000, f=12000)] * 10)

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "unknown")

    def test_classifies_flip_from_orientation_change(self):
        samples = [stable_sample(f=2050) for _ in range(50)]
        samples.extend([stable_sample(a=8000, c=3000, f=1800)] * 20)
        samples.extend([stable_sample(f=-2050) for _ in range(50)])

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "flip-over")

    def test_high_orientation_flip_takes_priority_over_rotation(self):
        prediction = classify_features(
            features(
                gyro_p99=14745.2,
                accel_deviation_p99=1399.2,
                c_mean=810.2,
                c_positive_fraction=0.35,
                c_abs_p99=2306.0,
                lateral_gyro_p99=14737.9,
                lateral_accel_p99=1958.7,
                orientation_angle_degrees=82.6,
            )
        )

        self.assertEqual(prediction.label, "flip-over")

    def test_high_orientation_flip_takes_priority_over_table_shake(self):
        prediction = classify_features(
            features(
                gyro_p99=5680.1,
                accel_deviation_p99=1558.6,
                c_mean=-643.3,
                c_abs_p99=4173.0,
                lateral_gyro_p99=5664.8,
                lateral_accel_p99=3157.0,
                orientation_angle_degrees=74.9,
            )
        )

        self.assertEqual(prediction.label, "flip-over")

    def test_classifies_short_flip_impulse_with_lateral_flip_motion(self):
        samples = [stable_sample(f=2050) for _ in range(80)]
        samples.extend([stable_sample(a=12000, c=3000, f=2950)] * 5)
        samples.extend([stable_sample(f=2050)] * 40)

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "flip-over")

    def test_moderate_flip_impulse_with_low_lateral_accel_is_not_shake(self):
        prediction = classify_features(
            features(
                gyro_p99=5131.4,
                accel_deviation_p99=1069.9,
                c_mean=-340.0,
                c_abs_p99=1992.0,
                lateral_gyro_p99=5043.5,
                lateral_accel_p99=1796.7,
                orientation_angle_degrees=24.1,
            )
        )

        self.assertEqual(prediction.label, "flip-over")

    def test_low_orientation_vertical_jolt_is_not_flip(self):
        prediction = classify_features(
            features(
                gyro_p99=4515.8,
                accel_deviation_p99=2036.3,
                c_mean=-40.0,
                c_abs_p99=667.0,
                lateral_gyro_p99=4466.3,
                lateral_accel_p99=1087.2,
                orientation_angle_degrees=3.0,
                f_abs_peak_delta=1519.5,
                f_abs_drop_delta=543.5,
                f_abs_peak_after_drop_delta=0.0,
            )
        )

        self.assertNotEqual(prediction.label, "flip-over")

    def test_low_energy_direction_changes_are_not_shake(self):
        samples = []
        for sign in [1, -1, 1, -1, 1]:
            samples.extend([stable_sample(c=sign * 1800, a=sign * 900) for _ in range(12)])

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "unknown")

    def test_flip_onset_with_large_orientation_change_takes_priority_over_shake(self):
        prediction = classify_features(
            features(
                gyro_p99=4934.2,
                accel_deviation_p99=744.1,
                c_mean=364.8,
                c_abs_p99=2681.0,
                lateral_gyro_p99=4862.2,
                lateral_accel_p99=2005.3,
                orientation_angle_degrees=129.3,
            )
        )

        self.assertEqual(prediction.label, "flip-over")

    def test_swirl_like_spin_without_lateral_flip_motion_is_not_flip(self):
        prediction = classify_features(
            features(
                gyro_p99=9943.8,
                accel_deviation_p99=846.8,
                c_mean=223.3,
                c_abs_p99=9899.0,
                lateral_gyro_p99=645.3,
                lateral_accel_p99=2102.2,
                orientation_angle_degrees=8.5,
                c_sign_runs=1,
            )
        )

        self.assertEqual(prediction.label, "unknown")

    def test_tap_like_impulse_is_not_a_controller_gesture(self):
        samples = [stable_sample() for _ in range(20)]
        samples.extend([stable_sample(a=1800, f=2800)] * 8)
        samples.extend([stable_sample() for _ in range(20)])

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "unknown")

    def test_classifies_lift_from_vertical_setdown_after_lifted_phase(self):
        samples = [stable_sample(f=2050)] * 30
        samples.extend([stable_sample(f=1450)] * 8)
        samples.extend([stable_sample(f=3000)] * 8)
        samples.extend([stable_sample(f=2050)] * 30)

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "lift")

    def test_vertical_setdown_peak_at_window_end_is_not_enter_yet(self):
        samples = [stable_sample(f=2050)] * 30
        samples.extend([stable_sample(f=1450)] * 8)
        samples.extend([stable_sample(f=3000)] * 8)

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "unknown")

    def test_classifies_smaller_lift_from_vertical_setdown_after_lifted_phase(self):
        samples = [stable_sample(f=2050)] * 30
        samples.extend([stable_sample(f=1650)] * 8)
        samples.extend([stable_sample(f=2700)] * 8)
        samples.extend([stable_sample(f=2050)] * 30)

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "lift")

    def test_classifies_forceful_high_gyro_stamp_as_lift(self):
        prediction = classify_features(
            features(
                gyro_p99=4515.8,
                accel_deviation_p99=2036.3,
                c_mean=-35.0,
                c_abs_p99=766.0,
                lateral_gyro_p99=4466.3,
                lateral_accel_p99=1087.2,
                orientation_angle_degrees=9.7,
                f_abs_peak_delta=1887.0,
                f_abs_drop_delta=929.0,
                f_abs_peak_after_drop_delta=1887.0,
                f_abs_post_peak_sample_count=28,
            )
        )

        self.assertEqual(prediction.label, "lift")

    def test_flip_start_with_weak_post_drop_ratio_is_not_enter(self):
        prediction = classify_features(
            features(
                gyro_p99=5131.4,
                accel_deviation_p99=1069.9,
                c_mean=-120.0,
                c_abs_p99=946.0,
                lateral_gyro_p99=5043.5,
                lateral_accel_p99=651.3,
                orientation_angle_degrees=8.2,
                f_abs_peak_delta=873.7,
                f_abs_drop_delta=618.3,
                f_abs_peak_after_drop_delta=873.7,
                f_abs_post_peak_sample_count=29,
            )
        )

        self.assertNotEqual(prediction.label, "lift")
        self.assertEqual(prediction.label, "unknown")

    def test_flip_tail_with_high_lateral_gyro_is_not_enter(self):
        prediction = classify_features(
            features(
                gyro_p99=5414.6,
                accel_deviation_p99=1021.2,
                c_mean=-43.5,
                c_abs_p99=317.0,
                lateral_gyro_p99=5411.9,
                lateral_accel_p99=512.1,
                orientation_angle_degrees=12.8,
                f_abs_peak_delta=1029.4,
                f_abs_drop_delta=486.6,
                f_abs_peak_after_drop_delta=1029.4,
                f_abs_post_peak_sample_count=28,
            )
        )

        self.assertNotEqual(prediction.label, "lift")
        self.assertEqual(prediction.label, "unknown")

    def test_lateral_table_back_forth_is_not_enter(self):
        prediction = classify_features(
            features(
                gyro_p99=4061.0,
                accel_deviation_p99=5048.1,
                c_mean=390.0,
                c_abs_p99=3899.0,
                lateral_gyro_p99=3433.9,
                lateral_accel_p99=6591.3,
                orientation_angle_degrees=14.0,
                f_abs_peak_delta=617.9,
                f_abs_drop_delta=355.1,
                f_abs_peak_after_drop_delta=617.9,
                f_abs_post_peak_sample_count=12,
            )
        )

        self.assertNotEqual(prediction.label, "lift")
        self.assertEqual(prediction.label, "back-forth")

    def test_moderate_low_orientation_jolt_is_not_shake(self):
        prediction = classify_features(
            features(
                gyro_p99=5100.0,
                accel_deviation_p99=1250.0,
                c_mean=120.0,
                c_abs_p99=900.0,
                lateral_gyro_p99=5000.0,
                lateral_accel_p99=900.0,
                orientation_angle_degrees=8.0,
            )
        )

        self.assertEqual(prediction.label, "unknown")

    def test_vertical_acceleration_peak_without_prior_lifted_phase_is_not_enter(self):
        samples = [stable_sample(f=2050)] * 30
        samples.extend([stable_sample(f=3400)] * 8)
        samples.extend([stable_sample(f=2050)] * 30)

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "unknown")

    def test_vertical_acceleration_drop_is_not_enter(self):
        samples = [stable_sample(f=2050)] * 30
        samples.extend([stable_sample(f=700)] * 8)
        samples.extend([stable_sample(f=2050)] * 30)

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "unknown")

    def test_smaller_vertical_acceleration_drop_is_not_enter(self):
        samples = [stable_sample(f=2050)] * 30
        samples.extend([stable_sample(f=1650)] * 8)
        samples.extend([stable_sample(f=2050)] * 30)

        prediction = classify_records(records(samples))

        self.assertEqual(prediction.label, "unknown")

    def test_extract_features_keeps_sign_sequence(self):
        samples = []
        for sign in [1, -1, 1]:
            samples.extend([stable_sample(c=sign * 2500)] * 10)

        features = extract_features(records(samples), startup_skip_seconds=0.0)

        self.assertEqual(features.c_sequence, "+-+")
        self.assertEqual(features.c_sign_runs, 3)


if __name__ == "__main__":
    unittest.main()
