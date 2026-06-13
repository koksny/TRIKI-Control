import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


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

    def test_windows_openssl_dlls_are_collected_from_conda_layout(self):
        from build_support import collect_windows_openssl_binaries

        with TemporaryDirectory() as temp_dir:
            prefix = Path(temp_dir)
            library_bin = prefix / "Library" / "bin"
            library_bin.mkdir(parents=True)
            (library_bin / "libssl-3-x64.dll").write_bytes(b"ssl")
            (library_bin / "libcrypto-3-x64.dll").write_bytes(b"crypto")

            entries = collect_windows_openssl_binaries(prefixes=[prefix])

        self.assertEqual(
            {
                (Path(source).name, destination)
                for source, destination in entries
            },
            {
                ("libssl-3-x64.dll", "."),
                ("libcrypto-3-x64.dll", "."),
            },
        )

    def test_windows_pyinstaller_specs_bundle_openssl_dlls(self):
        for spec_name in ("TRIKI-Control.spec", "TRIKI-Control-Debug.spec"):
            with self.subTest(spec=spec_name):
                text = (ROOT / spec_name).read_text(encoding="utf-8")
                self.assertIn("collect_windows_openssl_binaries", text)
                self.assertIn("binaries += collect_windows_openssl_binaries()", text)


if __name__ == "__main__":
    unittest.main()
