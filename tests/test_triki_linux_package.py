import tarfile
import tempfile
import unittest
from pathlib import Path

from triki_control.linux_package import build_linux_release, linux_release_name


class TrikiLinuxPackageTests(unittest.TestCase):
    def test_linux_release_name_matches_project_convention(self):
        self.assertEqual(
            linux_release_name("0.1.0-alpha.1"),
            "TRIKI-Control-0.1.0-alpha.1-linux",
        )

    def test_build_linux_release_creates_source_package_with_launcher_docs_and_smoke_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = build_linux_release(
                root=Path.cwd(),
                release_dir=Path(directory),
                version="0.1.0-test",
            )

            self.assertTrue(archive.exists())
            self.assertEqual(archive.name, "TRIKI-Control-0.1.0-test-linux.tar.gz")
            with tarfile.open(archive, "r:gz") as tar:
                names = set(tar.getnames())
                prefix = "TRIKI-Control-0.1.0-test-linux/"
                self.assertIn(prefix + "triki-control", names)
                self.assertIn(prefix + "src/triki_control/app.py", names)
                self.assertIn(prefix + "src/triki_control/linux_smoke.py", names)
                self.assertIn(prefix + "src/triki_control/diagnostics.py", names)
                self.assertIn(prefix + "src/triki_control/metadata.py", names)
                self.assertIn(prefix + "pyproject.toml", names)
                self.assertIn(prefix + "requirements.txt", names)
                self.assertIn(prefix + "README.md", names)
                self.assertIn(prefix + "CREDITS.md", names)
                self.assertIn(prefix + "LICENSE", names)
                self.assertIn(prefix + "docs/linux.md", names)
                self.assertIn(prefix + "START-HERE.txt", names)


if __name__ == "__main__":
    unittest.main()
