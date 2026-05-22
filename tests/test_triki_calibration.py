import json
import tempfile
import unittest
from pathlib import Path

from triki_control.calibration import (
    CalibrationAction,
    CalibrationRecorder,
    CalibrationSession,
    DEFAULT_ACTIONS,
    normalize_label,
    pairing_button_hint,
)
from triki_control.classifier import GesturePrediction, MotionFeatures
from triki_control.protocol import MotionSample


def sample(c=0):
    return MotionSample(packet_id=3, values=(1, 2, c, 4, 5, 6))


def prediction(label: str) -> GesturePrediction:
    return GesturePrediction(
        label=label,
        confidence=0.88,
        reason="test",
        features=MotionFeatures(
            sample_count=12,
            duration_seconds=0.5,
            gyro_p90=1.0,
            gyro_p99=2.0,
            accel_deviation_p99=3.0,
            accel_delta=4.0,
            orientation_angle_degrees=5.0,
            c_mean=6.0,
            c_positive_fraction=0.7,
            c_negative_fraction=0.1,
            c_sign_runs=1,
            c_sequence="+",
            gyro_peak_count=2,
            accel_peak_count=3,
            c_abs_p99=7.0,
            lateral_gyro_p99=8.0,
            lateral_accel_p99=9.0,
            lateral_accel_area_norm=0.35,
            lateral_accel_pca_ratio=0.55,
            f_abs_peak_delta=10.0,
            f_abs_drop_delta=11.0,
            f_abs_peak_after_drop_delta=12.0,
            f_abs_post_peak_sample_count=13,
        ),
    )


class CalibrationTests(unittest.TestCase):
    def test_default_actions_cover_reliable_gestures(self):
        labels = [action.label for action in DEFAULT_ACTIONS]

        self.assertEqual(
            labels,
            [
                "still",
                "rotate-cw",
                "rotate-ccw",
                "scrub-cw",
                "scrub-ccw",
                "back-forth",
                "lift",
                "flip-over",
            ],
        )
        self.assertNotIn("tap-single", labels)
        self.assertNotIn("tap-double", labels)
        self.assertNotIn("toss-catch", labels)
        self.assertNotIn("lift-up", labels)
        self.assertNotIn("lift-down", labels)
        self.assertNotIn("twist-cw-ccw-cw-ccw", labels)
        self.assertNotIn("twist-ccw-cw-ccw-cw", labels)
        self.assertNotIn("swirl-cw", labels)
        self.assertNotIn("swirl-ccw", labels)
        self.assertNotIn("shake", labels)
        scrub_action = next(action for action in DEFAULT_ACTIONS if action.label == "scrub-cw")
        self.assertEqual(scrub_action.title, "Scrub Right")
        self.assertIn("spoon", scrub_action.instruction)
        back_forth_action = next(action for action in DEFAULT_ACTIONS if action.label == "back-forth")
        self.assertEqual(back_forth_action.title, "Back-and-forth")
        self.assertIn("straight line", back_forth_action.instruction)
        lift_action = next(action for action in DEFAULT_ACTIONS if action.label == "lift")
        self.assertEqual(lift_action.title, "Stamp / Enter")
        self.assertIn("set TRIKI down", lift_action.instruction)

    def test_normalize_label_accepts_old_underscore_rotation_names(self):
        self.assertEqual(normalize_label("rotate_cw"), "rotate-cw")

    def test_normalize_label_maps_old_swirl_and_shake_names_to_canonical_controls(self):
        self.assertEqual(normalize_label("swirl-cw"), "scrub-cw")
        self.assertEqual(normalize_label("swirl_ccw"), "scrub-ccw")
        self.assertEqual(normalize_label("shake"), "back-forth")
        self.assertEqual(normalize_label("slide-back-forth"), "back-forth")

    def test_session_tracks_current_step_and_prediction_counts(self):
        session = CalibrationSession(
            actions=[
                CalibrationAction(
                    label="rotate-cw",
                    title="Rotate CW",
                    instruction="Rotate clockwise.",
                )
            ]
        )

        event = session.record_prediction(1.25, prediction("rotate-cw"))

        self.assertTrue(event["match"])
        snapshot = session.snapshot()
        self.assertEqual(snapshot["current"]["label"], "rotate-cw")
        self.assertEqual(snapshot["current"]["matches"], 1)
        self.assertEqual(snapshot["event_count"], 1)

    def test_session_can_advance_and_record_conflict(self):
        session = CalibrationSession(
            actions=[
                CalibrationAction("rotate-cw", "Rotate CW", "Clockwise."),
                CalibrationAction("rotate-ccw", "Rotate CCW", "Counterclockwise."),
            ]
        )

        session.next_action()
        event = session.record_prediction(2.0, prediction("rotate-cw"))

        self.assertFalse(event["match"])
        self.assertEqual(event["expected_label"], "rotate-ccw")
        self.assertEqual(session.snapshot()["current"]["conflicts"], 1)

    def test_prediction_event_includes_scrub_disambiguation_features(self):
        session = CalibrationSession(
            actions=[CalibrationAction("scrub-cw", "Scrub Right", "Scrub.")]
        )

        event = session.record_prediction(0.5, prediction("scrub-cw"))

        self.assertEqual(event["features"]["c_abs_p99"], 7.0)
        self.assertEqual(event["features"]["lateral_gyro_p99"], 8.0)
        self.assertEqual(event["features"]["lateral_accel_p99"], 9.0)
        self.assertEqual(event["features"]["lateral_accel_area_norm"], 0.35)
        self.assertEqual(event["features"]["lateral_accel_pca_ratio"], 0.55)
        self.assertEqual(event["features"]["f_abs_drop_delta"], 11.0)
        self.assertEqual(event["features"]["f_abs_peak_after_drop_delta"], 12.0)
        self.assertEqual(event["features"]["f_abs_post_peak_sample_count"], 13)

    def test_pairing_button_hint_allows_click_during_scan(self):
        hint = pairing_button_hint("connecting", "BLE cycle 1, attempt 3/5, mode scan")

        self.assertIn("click", hint.lower())
        self.assertIn("now", hint.lower())

    def test_pairing_button_hint_prompts_for_manual_web_button(self):
        hint = pairing_button_hint(
            "waiting",
            "Click Press pairing now when TRIKI is ready.",
        )

        self.assertIn("press pairing now", hint.lower())
        self.assertIn("paused", hint.lower())

    def test_pairing_button_hint_blocks_click_during_gatt(self):
        hint = pairing_button_hint(
            "connecting",
            "Connecting GATT mode=cached profile=nus-cached timeout=8.0s.",
        )

        self.assertIn("do not press", hint.lower())

    def test_snapshot_includes_pairing_button_hint(self):
        session = CalibrationSession()

        session.set_status("connecting", "BLE cycle 1, attempt 1/5, mode scan")

        self.assertIn("button_hint", session.snapshot())

    def test_snapshot_includes_recent_connection_log(self):
        session = CalibrationSession()

        session.set_status("connecting", "BLE cycle 1, attempt 1/5, mode cached")
        session.set_status("retrying", "CONNECT_TIMEOUT TimeoutError profile=nus-cached mode=cached")

        snapshot = session.snapshot()
        self.assertEqual(snapshot["connection_log"][0]["status"], "retrying")
        self.assertIn("CONNECT_TIMEOUT", snapshot["connection_log"][0]["message"])
        self.assertEqual(snapshot["connection_log"][1]["status"], "connecting")

    def test_connection_log_records_active_action(self):
        session = CalibrationSession(
            actions=[
                CalibrationAction("still", "Still", "Still."),
                CalibrationAction("lift", "Lift", "Lift it."),
            ]
        )

        session.select_action(1)
        session.set_status("disconnected", "TRIKI disconnected during lift.")

        self.assertEqual(session.snapshot()["connection_log"][0]["action_label"], "lift")

    def test_recorder_writes_samples_events_and_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            recorder = CalibrationRecorder(Path(temp_dir), session_id="session-test")
            session = CalibrationSession(
                actions=[CalibrationAction("still", "Still", "Keep still.")],
                recorder=recorder,
            )

            with recorder:
                session.record_sample(0.1, sample(c=20))
                session.record_prediction(0.2, prediction("still"))
                summary_path = recorder.write_summary(session.snapshot())

            self.assertTrue(recorder.samples_csv.exists())
            self.assertTrue(recorder.events_jsonl.exists())
            self.assertTrue(summary_path.exists())
            self.assertIn("expected_label,predicted_label", recorder.events_csv.read_text(encoding="utf-8"))
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["sample_count"], 1)
            self.assertEqual(summary["event_count"], 1)


if __name__ == "__main__":
    unittest.main()
