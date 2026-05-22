import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

from triki_control.macos_package import (
    MACOS_BLUETOOTH_USAGE_DESCRIPTION,
    build_macos_release,
    macos_info_plist,
    macos_release_name,
)


class TrikiMacOSPackageTests(unittest.TestCase):
    def test_macos_release_name_matches_project_convention(self):
        self.assertEqual(
            macos_release_name("0.1.0-alpha.1"),
            "TRIKI-Control-0.1.0-alpha.1-macos",
        )

    def test_macos_info_plist_contains_privacy_usage_descriptions(self):
        plist = macos_info_plist()

        self.assertEqual(plist["CFBundleIdentifier"], "com.koksny.triki.control")
        self.assertEqual(plist["NSBluetoothAlwaysUsageDescription"], MACOS_BLUETOOTH_USAGE_DESCRIPTION)
        self.assertIn("Accessibility", plist["NSAccessibilityUsageDescription"])

    def test_build_macos_release_zips_app_docs_and_start_here(self):
        with tempfile.TemporaryDirectory() as directory:
            temp_root = Path(directory)
            app_path = temp_root / "TRIKI Control.app"
            contents = app_path / "Contents"
            contents.mkdir(parents=True)
            (contents / "Info.plist").write_text("plist", encoding="utf-8")

            archive = build_macos_release(
                root=Path.cwd(),
                release_dir=temp_root / "release",
                version="0.1.0-test",
                app_path=app_path,
            )

            self.assertTrue(archive.exists())
            self.assertEqual(archive.name, "TRIKI-Control-0.1.0-test-macos.zip")
            with zipfile.ZipFile(archive) as zip_file:
                names = set(zip_file.namelist())
                prefix = "TRIKI-Control-0.1.0-test-macos/"
                self.assertIn(prefix + "TRIKI Control.app/Contents/Info.plist", names)
                self.assertIn(prefix + "README.md", names)
                self.assertIn(prefix + "CREDITS.md", names)
                self.assertIn(prefix + "LICENSE", names)
                self.assertIn(prefix + "docs/macos.md", names)
                self.assertIn(prefix + "START-HERE.txt", names)

    def test_build_macos_release_preserves_app_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            temp_root = Path(directory)
            app_path = temp_root / "TRIKI Control.app"
            resources = app_path / "Contents" / "Resources"
            frameworks = app_path / "Contents" / "Frameworks"
            resources.mkdir(parents=True)
            frameworks.mkdir(parents=True)
            (resources / "shared.txt").write_text("shared", encoding="utf-8")
            link_path = frameworks / "shared.txt"
            try:
                link_path.symlink_to("../Resources/shared.txt")
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"symlinks are unavailable: {error}")

            archive = build_macos_release(
                root=Path.cwd(),
                release_dir=temp_root / "release",
                version="0.1.0-test",
                app_path=app_path,
            )

            with zipfile.ZipFile(archive) as zip_file:
                info = zip_file.getinfo(
                    "TRIKI-Control-0.1.0-test-macos/"
                    "TRIKI Control.app/Contents/Frameworks/shared.txt"
                )
                mode = info.external_attr >> 16
                self.assertTrue(stat.S_ISLNK(mode))
                self.assertEqual(zip_file.read(info).decode("utf-8"), "../Resources/shared.txt")


if __name__ == "__main__":
    unittest.main()
