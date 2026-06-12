import math
import unittest
from unittest.mock import patch

from triki_live import LiveGestureDetector, parse_args
from triki_protocol import MotionSample


def sample(a=0, b=0, c=0, d=0, e=0, f=2050):
    return MotionSample(packet_id=0, values=(a, b, c, d, e, f))


def circle_sample(index: int, *, clockwise=True):
    direction = 1 if clockwise else -1
    angle = direction * 2 * math.pi * index / 20
    return sample(
        a=int(900 * math.sin(angle)),
        b=int(900 * math.cos(angle)),
        c=0,
        d=int(700 * math.cos(angle)),
        e=int(700 * math.sin(angle)),
        f=2050,
    )


def labels_from(detector, samples, *, start_index=0):
    labels = []
    for offset, value in enumerate(samples):
        event = detector.add_sample((start_index + offset) * 0.05, value)
        if event is not None:
            labels.append(event.label)
    return labels


class LiveGestureDetectorTests(unittest.TestCase):
    def test_suppresses_tap_like_impulses_by_default(self):
        detector = LiveGestureDetector(window_seconds=0.5, min_samples=5, warmup_seconds=0.0, confirm_windows=1)
        values = [sample(c=0, f=2050)] * 5 + [sample(a=1800, f=2800)] * 5

        events = [
            detector.add_sample(index * 0.05, value)
            for index, value in enumerate(values)
        ]

        self.assertEqual([event for event in events if event is not None], [])

    def test_suppresses_still_events_by_default(self):
        detector = LiveGestureDetector(window_seconds=0.5, min_samples=5, warmup_seconds=0.0, confirm_windows=1)

        events = [
            detector.add_sample(index * 0.05, sample(c=20))
            for index in range(20)
        ]

        self.assertEqual([event for event in events if event is not None], [])

    def test_emits_rotation_after_window_has_enough_samples(self):
        detector = LiveGestureDetector(window_seconds=0.5, min_samples=5, warmup_seconds=0.0, confirm_windows=1)

        events = [
            detector.add_sample(index * 0.05, sample(c=2800))
            for index in range(5)
        ]

        self.assertEqual(events[-1].label, "rotate-cw")

    def test_emits_scrub_without_c_axis_tail_rotation(self):
        detector = LiveGestureDetector(window_seconds=0.75, min_samples=12, warmup_seconds=0.0, confirm_windows=1)
        emitted = None

        for index in range(40):
            event = detector.add_sample(index * 0.05, circle_sample(index, clockwise=True))
            if event is not None:
                emitted = event
                break

        self.assertIsNotNone(emitted)
        self.assertEqual(emitted.label, "scrub-cw")

    def test_suppresses_twist_events_by_default(self):
        detector = LiveGestureDetector(window_seconds=1.4, min_samples=5, warmup_seconds=0.0, confirm_windows=1)
        values = []
        for sign in [1, -1, 1, -1]:
            values.extend([sample(c=sign * 12000) for _ in range(8)])

        events = [
            detector.add_sample(index * 0.05, value)
            for index, value in enumerate(values)
        ]

        self.assertNotIn(
            "twist-cw-ccw-cw-ccw",
            [event.label for event in events if event is not None],
        )

    def test_default_window_is_short_for_responsive_rotation(self):
        detector = LiveGestureDetector()

        self.assertEqual(detector.window_seconds, 0.75)
        self.assertEqual(detector.confirm_windows, 1)
        self.assertEqual(detector.repeat_seconds, 0.55)
        self.assertEqual(detector.warmup_seconds, 0.2)

    def test_live_cli_defaults_match_responsive_detector_defaults(self):
        with patch("sys.argv", ["triki_live.py"]):
            args = parse_args()

        self.assertEqual(args.confirm_windows, 1)
        self.assertEqual(args.repeat_seconds, 0.55)
        self.assertEqual(args.warmup_seconds, 0.2)

    def test_debounces_same_label_until_repeat_delay(self):
        detector = LiveGestureDetector(
            window_seconds=0.5,
            min_samples=5,
            repeat_seconds=1.0,
            warmup_seconds=0.0,
            confirm_windows=1,
        )

        events = [
            detector.add_sample(index * 0.05, sample(c=2800))
            for index in range(20)
        ]

        emitted = [event for event in events if event is not None]
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].label, "rotate-cw")

        next_event = detector.add_sample(1.25, sample(c=2800))

        self.assertEqual(next_event.label, "rotate-cw")

    def test_does_not_repeat_rotation_after_recent_tail_is_quiet(self):
        detector = LiveGestureDetector(
            window_seconds=0.75,
            min_samples=5,
            repeat_seconds=0.25,
            warmup_seconds=0.0,
            confirm_windows=1,
        )

        events = []
        values = [sample(c=2800)] * 6 + [sample(c=0)] * 8
        for index, value in enumerate(values):
            event = detector.add_sample(index * 0.05, value)
            if event is not None:
                events.append((index * 0.05, event.label))

        self.assertEqual(events, [(0.2, "rotate-cw")])

    def test_emits_new_label_after_window_moves_to_new_gesture(self):
        detector = LiveGestureDetector(
            window_seconds=0.25,
            min_samples=5,
            repeat_seconds=10.0,
            warmup_seconds=0.0,
            confirm_windows=1,
        )

        first_events = []
        for index in range(8):
            event = detector.add_sample(index * 0.05, sample(c=2800))
            if event is not None:
                first_events.append(event)

        second_events = []
        for index in range(10, 18):
            event = detector.add_sample(index * 0.05, sample(c=-2800))
            if event is not None:
                second_events.append(event)

        self.assertEqual(first_events[0].label, "rotate-cw")
        self.assertEqual(second_events[-1].label, "rotate-ccw")

    def test_prunes_old_samples_from_window(self):
        detector = LiveGestureDetector(window_seconds=0.2, min_samples=5, warmup_seconds=0.0, confirm_windows=1)

        for index in range(5):
            detector.add_sample(index * 0.05, sample(c=2800))

        events = []
        for index in range(20, 26):
            event = detector.add_sample(index * 0.05, sample(c=-2800))
            if event is not None:
                events.append(event)

        self.assertEqual(events[-1].label, "rotate-ccw")

    def test_warmup_ignores_startup_transients(self):
        detector = LiveGestureDetector(
            window_seconds=0.5,
            min_samples=5,
            warmup_seconds=0.3,
            confirm_windows=1,
        )

        for index in range(5):
            event = detector.add_sample(index * 0.05, sample(c=2000))

        self.assertIsNone(event)

    def test_confirm_windows_requires_repeated_prediction(self):
        detector = LiveGestureDetector(
            window_seconds=0.5,
            min_samples=5,
            warmup_seconds=0.0,
            confirm_windows=2,
        )

        first = None
        for index in range(5):
            first = detector.add_sample(index * 0.05, sample(c=2800))
        second = detector.add_sample(0.25, sample(c=2800))

        self.assertIsNone(first)
        self.assertEqual(second.label, "rotate-cw")

    def test_reset_clears_samples_and_debounce_state(self):
        detector = LiveGestureDetector(
            window_seconds=0.5,
            min_samples=5,
            repeat_seconds=10.0,
            warmup_seconds=0.0,
            confirm_windows=1,
        )
        for index in range(5):
            detector.add_sample(index * 0.05, sample(c=2800))

        detector.reset()
        events = [
            detector.add_sample(index * 0.05, sample(c=2800))
            for index in range(5)
        ]

        self.assertEqual(events[-1].label, "rotate-cw")

    def test_lift_is_one_shot_until_quiet_window(self):
        detector = LiveGestureDetector(
            window_seconds=0.75,
            min_samples=8,
            repeat_seconds=0.0,
            warmup_seconds=0.0,
            confirm_windows=1,
        )
        stamp = (
            [sample(f=2050)] * 16
            + [sample(f=1450)] * 6
            + [sample(f=3000)] * 6
            + [sample(f=2050)] * 24
        )

        first_labels = labels_from(detector, stamp)
        quiet_labels = labels_from(detector, [sample(f=2050)] * 20, start_index=len(stamp))
        second_labels = labels_from(
            detector,
            stamp,
            start_index=len(stamp) + 20,
        )

        self.assertEqual(first_labels.count("lift"), 1)
        self.assertNotIn("lift", quiet_labels)
        self.assertEqual(second_labels.count("lift"), 1)

    def test_flip_is_one_shot_until_quiet_window(self):
        detector = LiveGestureDetector(
            window_seconds=0.75,
            min_samples=8,
            repeat_seconds=0.0,
            warmup_seconds=0.0,
            confirm_windows=1,
        )
        flip = (
            [sample(f=2050)] * 16
            + [sample(a=9000, b=3000, c=1200, d=1800, e=1500, f=900)] * 8
            + [sample(f=-2050)] * 24
        )

        first_labels = labels_from(detector, flip)
        quiet_labels = labels_from(detector, [sample(f=-2050)] * 20, start_index=len(flip))
        second_labels = labels_from(
            detector,
            flip,
            start_index=len(flip) + 20,
        )

        self.assertEqual(first_labels.count("flip-over"), 1)
        self.assertNotIn("flip-over", quiet_labels)
        self.assertEqual(second_labels.count("flip-over"), 1)


class DetectorObserverTests(unittest.TestCase):
    def test_observer_reports_every_sample_with_outcome_and_values(self):
        seen = []
        detector = LiveGestureDetector(
            warmup_seconds=0.0,
            min_samples=3,
            observer=lambda record: seen.append(record),
        )
        # Feed 5 "still" samples (near gravity, no motion). The first two buffer,
        # then "still" classifies and is suppressed.
        for index in range(5):
            detector.add_sample(index * 0.05, sample())

        self.assertEqual(len(seen), 5)
        outcomes = [record["outcome"] for record in seen]
        self.assertEqual(outcomes[0], "buffering")
        self.assertIn("suppressed", outcomes)
        self.assertTrue(all(len(record["values"]) == 6 for record in seen))

    def test_observer_does_not_change_emission_behaviour(self):
        # Same input with and without observer yields identical return values.
        without = LiveGestureDetector(warmup_seconds=0.0, min_samples=3)
        captured = []
        with_obs = LiveGestureDetector(
            warmup_seconds=0.0, min_samples=3, observer=lambda r: captured.append(r)
        )
        results_a, results_b = [], []
        for index in range(6):
            s = sample(c=900)
            results_a.append(without.add_sample(index * 0.05, s) is not None)
            results_b.append(with_obs.add_sample(index * 0.05, s) is not None)
        self.assertEqual(results_a, results_b)
        self.assertEqual(len(captured), 6)


if __name__ == "__main__":
    unittest.main()
