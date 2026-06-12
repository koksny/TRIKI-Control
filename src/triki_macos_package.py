from __future__ import annotations

import argparse
import os
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path


MACOS_BLUETOOTH_USAGE_DESCRIPTION = (
    "TRIKI Control uses Bluetooth to scan for and connect to the TRIKI motion cap."
)
MACOS_ACCESSIBILITY_USAGE_DESCRIPTION = (
    "TRIKI Control uses Accessibility permission to emit configured keyboard shortcuts."
)


def macos_release_name(version: str) -> str:
    return f"TRIKI-Control-{version}-macos"


def macos_info_plist() -> dict:
    from triki_metadata import APP_VERSION

    return {
        "CFBundleIdentifier": "com.koksny.triki.control",
        "CFBundleName": "TRIKI Control",
        "CFBundleDisplayName": "TRIKI Control",
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "13.0",
        "NSBluetoothAlwaysUsageDescription": MACOS_BLUETOOTH_USAGE_DESCRIPTION,
        "NSBluetoothPeripheralUsageDescription": MACOS_BLUETOOTH_USAGE_DESCRIPTION,
        "NSAccessibilityUsageDescription": MACOS_ACCESSIBILITY_USAGE_DESCRIPTION,
    }


def build_macos_release(
    *,
    root: Path,
    release_dir: Path,
    version: str,
    app_path: Path,
) -> Path:
    root = root.resolve()
    release_dir = release_dir.resolve()
    app_path = app_path.resolve()
    if not app_path.exists():
        raise FileNotFoundError(f"Expected built macOS app at {app_path}")

    name = macos_release_name(version)
    legacy_stage_dir = release_dir / name
    archive_path = release_dir / f"{name}.zip"

    if legacy_stage_dir.exists():
        shutil.rmtree(legacy_stage_dir)
    if archive_path.exists():
        archive_path.unlink()
    release_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f".{name}.", dir=release_dir) as stage:
        stage_dir = Path(stage)
        for filename in ("README.md", "CREDITS.md", "LICENSE"):
            shutil.copy2(root / filename, stage_dir / filename)
        shutil.copytree(root / "docs", stage_dir / "docs")
        _write_start_here(stage_dir / "START-HERE.txt", version)

        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            package_root = Path(name)
            _zip_tree(archive, stage_dir, package_root)
            _zip_tree(archive, app_path, package_root / app_path.name)
    return archive_path


def _write_start_here(path: Path, version: str) -> None:
    path.write_text(
        f"""TRIKI Control {version} for macOS

Created by Wojciech "Koksny" Gorny, Koksny.com.
Released under the MIT License.

Start with TRIKI Control.app.

First run:
1. Open TRIKI Control.app.
2. Allow Bluetooth access when macOS asks.
3. Click Pair TRIKI, then press the physical TRIKI pairing button once.
4. Grant Accessibility permission in System Settings before using keyboard output.

This alpha build is ad-hoc signed and not notarized. If macOS blocks first
launch, right-click the app, choose Open, and confirm the prompt.
""",
        encoding="utf-8",
        newline="\n",
    )


def _zip_tree(archive: zipfile.ZipFile, source: Path, archive_root: Path) -> None:
    _write_zip_path(archive, source, archive_root)
    if source.is_symlink() or not source.is_dir():
        return

    for directory, dirnames, filenames in os.walk(source, followlinks=False):
        dirnames.sort()
        filenames.sort()
        source_dir = Path(directory)
        relative_dir = source_dir.relative_to(source)
        archive_dir = archive_root if relative_dir == Path(".") else archive_root / relative_dir

        if source_dir != source:
            _write_zip_path(archive, source_dir, archive_dir)

        for dirname in dirnames:
            source_path = source_dir / dirname
            if source_path.is_symlink():
                _write_zip_path(archive, source_path, archive_dir / dirname)

        for filename in filenames:
            source_path = source_dir / filename
            _write_zip_path(archive, source_path, archive_dir / filename)


def _write_zip_path(archive: zipfile.ZipFile, path: Path, archive_path: Path) -> None:
    if path.is_symlink():
        _write_zip_symlink(archive, path, archive_path)
    elif path.is_dir():
        _write_zip_directory(archive, archive_path, path)
    else:
        archive.write(path, archive_path.as_posix())


def _write_zip_directory(
    archive: zipfile.ZipFile,
    archive_path: Path,
    source_path: Path | None = None,
) -> None:
    info = zipfile.ZipInfo(_directory_archive_name(archive_path))
    info.create_system = 3
    mode = 0o755
    if source_path is not None:
        mode = stat.S_IMODE(os.lstat(source_path).st_mode)
    info.external_attr = (stat.S_IFDIR | mode) << 16
    archive.writestr(info, b"")


def _write_zip_symlink(archive: zipfile.ZipFile, path: Path, archive_path: Path) -> None:
    info = zipfile.ZipInfo(archive_path.as_posix())
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive.writestr(info, os.readlink(path))


def _directory_archive_name(path: Path) -> str:
    return path.as_posix().rstrip("/") + "/"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Package TRIKI Control for macOS.")
    parser.add_argument("--version", default="")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--release-dir", type=Path, default=Path("release"))
    parser.add_argument("--app-path", type=Path, default=Path("dist") / "TRIKI Control.app")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    version = args.version
    if not version:
        from triki_metadata import APP_VERSION

        version = APP_VERSION
    archive_path = build_macos_release(
        root=args.root,
        release_dir=args.release_dir,
        version=version,
        app_path=args.app_path,
    )
    print(f"Packaged {archive_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
