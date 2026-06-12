import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class TrikiBuildAssetTests(unittest.TestCase):
    def test_build_icon_assets_are_present(self):
        for relative_path in (
            "assets/triki-control-icon.png",
            "assets/triki-control-icon-tray.png",
            "assets/triki-control-icon.ico",
            "assets/triki-control-icon.icns",
        ):
            with self.subTest(relative_path=relative_path):
                self.assertTrue((ROOT / relative_path).is_file())

    def test_pyinstaller_specs_reference_app_icons(self):
        for spec_name in ("TRIKI-Control.spec", "TRIKI-Control-Debug.spec"):
            with self.subTest(spec=spec_name):
                text = (ROOT / spec_name).read_text(encoding="utf-8")
                self.assertIn("('assets/triki-control-icon-tray.png', 'assets')", text)
                self.assertIn("icon='assets/triki-control-icon.ico'", text)

        macos_text = (ROOT / "TRIKI-Control-macOS.spec").read_text(encoding="utf-8")
        self.assertIn('("assets/triki-control-icon-tray.png", "assets")', macos_text)
        self.assertIn('icon="assets/triki-control-icon.icns"', macos_text)


if __name__ == "__main__":
    unittest.main()
