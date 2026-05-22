import io
import json
import unittest
from pathlib import Path

from triki_control.diagnostics import collect_diagnostics, main, module_import_status


class TrikiDiagnosticsTests(unittest.TestCase):
    def test_module_import_status_reports_available_and_missing_modules(self):
        def importer(name):
            if name == "present":
                return object()
            raise ImportError("missing module")

        status = module_import_status(("present", "missing"), importer=importer)

        self.assertEqual(status["present"]["status"], "ok")
        self.assertEqual(status["missing"]["status"], "missing")
        self.assertIn("missing module", status["missing"]["message"])

    def test_collect_diagnostics_includes_platform_config_uinput_and_fixes(self):
        diagnostics = collect_diagnostics(
            config_path=Path("/tmp/triki-config.json"),
            system_name="Linux",
            python_version="3.12.3",
            import_status=lambda _names: {"bleak": {"status": "missing", "message": "no bleak"}},
            uinput_probe=lambda _path: {"status": "permission", "device_path": "/dev/uinput"},
        )

        self.assertEqual(diagnostics["app_name"], "TRIKI Control")
        self.assertEqual(diagnostics["platform"]["system"], "Linux")
        self.assertEqual(diagnostics["platform"]["python"], "3.12.3")
        self.assertEqual(diagnostics["config_path"], "/tmp/triki-config.json")
        self.assertEqual(diagnostics["modules"]["bleak"]["status"], "missing")
        self.assertEqual(diagnostics["uinput"]["status"], "permission")
        self.assertTrue(any("pip install" in fix for fix in diagnostics["fixes"]))
        self.assertTrue(any("udev" in fix for fix in diagnostics["fixes"]))

    def test_collect_diagnostics_marks_uinput_not_applicable_off_linux(self):
        diagnostics = collect_diagnostics(
            config_path=None,
            system_name="Windows",
            import_status=lambda _names: {},
        )

        self.assertEqual(diagnostics["uinput"]["status"], "not-applicable")
        self.assertEqual(diagnostics["config_path"], "")

    def test_collect_diagnostics_includes_macos_accessibility_status_and_fix(self):
        diagnostics = collect_diagnostics(
            config_path=None,
            system_name="Darwin",
            import_status=lambda _names: {},
            macos_accessibility_probe=lambda: {
                "status": "untrusted",
                "trusted": False,
                "message": "Accessibility permission is not granted.",
            },
        )

        self.assertEqual(diagnostics["key_output"]["backend"], "Quartz CGEvent")
        self.assertEqual(diagnostics["key_output"]["status"], "untrusted")
        self.assertFalse(diagnostics["key_output"]["trusted"])
        self.assertTrue(any("Accessibility" in fix for fix in diagnostics["fixes"]))
        self.assertTrue(any("NSBluetoothAlwaysUsageDescription" in fix for fix in diagnostics["fixes"]))

    def test_cli_json_outputs_diagnostics(self):
        stdout = io.StringIO()

        code = main(
            ["--json", "--config-path", "/tmp/triki.json"],
            stdout=stdout,
            system_name="Linux",
            import_status=lambda _names: {},
            uinput_probe=lambda _path: {"status": "ready", "device_path": "/dev/uinput"},
        )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["config_path"], "/tmp/triki.json")
        self.assertEqual(payload["uinput"]["status"], "ready")


if __name__ == "__main__":
    unittest.main()
