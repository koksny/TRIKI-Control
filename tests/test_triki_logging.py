import json
import tempfile
import unittest
from pathlib import Path

from triki_logging import SessionLogger


class SessionLoggerTests(unittest.TestCase):
    def test_writes_jsonl_lines_with_type_and_ts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sub" / "session.jsonl"
            logger = SessionLogger(path, clock=lambda: 123.456)
            logger.log("session_start", {"app_version": "x", "hold_ms": 400})
            logger.log("gesture", {"values": [1, 2, 3, 4, 5, 6], "outcome": "emitted", "label": "rotate-cw"})
            logger.close()
            lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["type"], "session_start")
        self.assertEqual(lines[0]["hold_ms"], 400)
        self.assertEqual(lines[0]["ts"], 123.456)
        self.assertEqual(lines[1]["type"], "gesture")
        self.assertEqual(lines[1]["values"], [1, 2, 3, 4, 5, 6])
        self.assertEqual(lines[1]["outcome"], "emitted")

    def test_close_is_idempotent_and_logging_after_close_is_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            logger = SessionLogger(path)
            logger.close()
            logger.close()
            logger.log("late", {"x": 1})
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

        self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
