import json
import tempfile
import unittest
from pathlib import Path

from triki_protocol import MotionSample
from triki_recording import SampleRecorder, build_recording_paths, summarize_records


class RecordingTests(unittest.TestCase):
    def test_recorder_writes_csv_jsonl_and_summary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = build_recording_paths(Path(temp_dir), label="hold still", stamp="20260520_220000")

            with SampleRecorder(paths, label="hold still") as recorder:
                recorder.record(0.125, MotionSample(packet_id=0, values=(1, -2, 3, -4, 5, -6)))
                recorder.record(0.250, MotionSample(packet_id=1, values=(3, -4, 5, -6, 7, -8)))

            csv_lines = paths.csv.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                csv_lines,
                [
                    "sample_index,elapsed_seconds,label,packet_id,a,b,c,d,e,f",
                    "1,0.125000,hold still,0,1,-2,3,-4,5,-6",
                    "2,0.250000,hold still,1,3,-4,5,-6,7,-8",
                ],
            )

            jsonl_rows = [
                json.loads(line)
                for line in paths.jsonl.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(jsonl_rows[0]["values"], [1, -2, 3, -4, 5, -6])
            self.assertEqual(jsonl_rows[1]["packet_id"], 1)

            summary = json.loads(paths.summary_json.read_text(encoding="utf-8"))
            self.assertEqual(summary["label"], "hold still")
            self.assertEqual(summary["sample_count"], 2)
            self.assertEqual(summary["axes"]["a"]["min"], 1)
            self.assertEqual(summary["axes"]["a"]["max"], 3)
            self.assertEqual(summary["axes"]["a"]["mean"], 2.0)

    def test_recorder_removes_empty_recording_by_default(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            paths = build_recording_paths(Path(temp_dir), label="empty", stamp="20260520_220000")

            with SampleRecorder(paths, label="empty"):
                pass

            self.assertFalse(paths.csv.exists())
            self.assertFalse(paths.jsonl.exists())
            self.assertFalse(paths.summary_json.exists())

    def test_summarize_records_returns_axis_statistics(self):
        summary = summarize_records(
            label="tilt-left",
            records=[
                (0.0, MotionSample(packet_id=0, values=(10, 20, 30, 40, 50, 60))),
                (0.1, MotionSample(packet_id=0, values=(14, 18, 36, 44, 48, 62))),
            ],
        )

        self.assertEqual(summary["sample_count"], 2)
        self.assertEqual(summary["duration_seconds"], 0.1)
        self.assertEqual(summary["axes"]["a"]["mean"], 12.0)
        self.assertEqual(summary["axes"]["a"]["stdev"], 2.0)
        self.assertEqual(summary["axes"]["c"]["range"], 6)

    def test_summarize_records_can_skip_startup_samples(self):
        summary = summarize_records(
            label="still",
            records=[
                (0.0, MotionSample(packet_id=0, values=(1000, 0, 0, 0, 0, 0))),
                (0.3, MotionSample(packet_id=0, values=(10, 0, 0, 0, 0, 0))),
                (0.4, MotionSample(packet_id=0, values=(12, 0, 0, 0, 0, 0))),
            ],
            skip_seconds=0.2,
        )

        self.assertEqual(summary["raw_sample_count"], 3)
        self.assertEqual(summary["sample_count"], 2)
        self.assertEqual(summary["skipped_sample_count"], 1)
        self.assertEqual(summary["axes"]["a"]["mean"], 11.0)


if __name__ == "__main__":
    unittest.main()
