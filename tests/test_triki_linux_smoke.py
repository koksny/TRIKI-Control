import io
import json
import unittest

from triki_control.linux_smoke import build_smoke_report, main, probe_uinput_status
from triki_control.key_emitter import KeyEmissionError


class FakeEmitter:
    def __init__(self):
        self.pressed = []
        self.closed = False

    def press_key(self, key_name):
        self.pressed.append(key_name)

    def close(self):
        self.closed = True


class FailingEmitter:
    def press_key(self, key_name):
        raise KeyEmissionError(f"cannot emit {key_name}")


class TrikiLinuxSmokeTests(unittest.TestCase):
    def test_probe_uinput_status_reports_missing_device(self):
        status = probe_uinput_status(
            "/tmp/not-uinput",
            exists=lambda _path: False,
            access=lambda _path, _mode: False,
        )

        self.assertEqual(status["device_path"], "/tmp/not-uinput")
        self.assertFalse(status["exists"])
        self.assertFalse(status["readable"])
        self.assertFalse(status["writable"])
        self.assertEqual(status["status"], "missing")

    def test_dry_run_smoke_report_does_not_create_emitter(self):
        report = build_smoke_report(
            key_name="space",
            emit=False,
            system_name="Linux",
            emitter_factory=lambda: self.fail("dry-run should not create an emitter"),
            probe=lambda _path: {"status": "ready"},
        )

        self.assertEqual(report["status"], "dry-run-ok")
        self.assertEqual(report["key"], "space")
        self.assertEqual(report["evdev_code"], 57)
        self.assertFalse(report["emit_requested"])
        self.assertFalse(report["emitted"])

    def test_emit_smoke_report_uses_injected_emitter_and_closes_it(self):
        emitter = FakeEmitter()

        report = build_smoke_report(
            key_name="right",
            emit=True,
            system_name="Linux",
            emitter_factory=lambda: emitter,
            probe=lambda _path: {"status": "ready"},
        )

        self.assertEqual(report["status"], "emit-ok")
        self.assertEqual(emitter.pressed, ["right"])
        self.assertTrue(emitter.closed)
        self.assertTrue(report["emitted"])

    def test_emit_smoke_report_reports_output_errors(self):
        report = build_smoke_report(
            key_name="space",
            emit=True,
            system_name="Linux",
            emitter_factory=FailingEmitter,
            probe=lambda _path: {"status": "ready"},
        )

        self.assertEqual(report["status"], "emit-failed")
        self.assertFalse(report["emitted"])
        self.assertIn("cannot emit space", report["error"])

    def test_cli_json_emit_failure_returns_nonzero(self):
        stdout = io.StringIO()

        code = main(
            ["--json", "--emit", "--key", "space"],
            stdout=stdout,
            system_name="Linux",
            emitter_factory=FailingEmitter,
            probe=lambda _path: {"status": "ready"},
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 2)
        self.assertEqual(payload["status"], "emit-failed")

    def test_cli_text_invalid_key_returns_nonzero_without_crashing(self):
        stdout = io.StringIO()

        code = main(["--key", "not-a-key"], stdout=stdout, system_name="Linux")

        self.assertEqual(code, 2)
        self.assertIn("status: invalid-key", stdout.getvalue())
        self.assertIn("unsupported Linux key name", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
