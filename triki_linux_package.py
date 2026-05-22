from __future__ import annotations

import argparse
import shutil
import stat
import tarfile
from collections.abc import Sequence
from pathlib import Path


def linux_release_name(version: str) -> str:
    return f"TRIKI-Control-{version}-linux"


def build_linux_release(
    *,
    root: Path,
    release_dir: Path,
    version: str,
) -> Path:
    root = root.resolve()
    release_dir = release_dir.resolve()
    name = linux_release_name(version)
    stage_dir = release_dir / name
    archive_path = release_dir / f"{name}.tar.gz"

    if stage_dir.exists():
        shutil.rmtree(stage_dir)
    if archive_path.exists():
        archive_path.unlink()
    stage_dir.mkdir(parents=True)

    for filename in _linux_release_root_files():
        shutil.copy2(root / filename, stage_dir / filename)
    shutil.copytree(root / "docs", stage_dir / "docs")
    _write_launcher(stage_dir / "triki-control")
    _write_start_here(stage_dir / "START-HERE.txt", version)

    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(stage_dir, arcname=name)
    return archive_path


def _linux_release_root_files() -> tuple[str, ...]:
    return (
        "README.md",
        "CREDITS.md",
        "LICENSE",
        "requirements.txt",
        "triki_actions.py",
        "triki_analyze_recordings.py",
        "triki_app.py",
        "triki_battery.py",
        "triki_calibration.py",
        "triki_calibration_server.py",
        "triki_classifier.py",
        "triki_desktop.py",
        "triki_diagnostics.py",
        "triki_gestures.py",
        "triki_key_emitter.py",
        "triki_linux_package.py",
        "triki_linux_smoke.py",
        "triki_live.py",
        "triki_metadata.py",
        "triki_play.py",
        "triki_probe.py",
        "triki_protocol.py",
        "triki_recording.py",
    )


def _write_launcher(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi
exec "$PYTHON" "$ROOT/triki_app.py" "$@"
""",
        encoding="utf-8",
        newline="\n",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_start_here(path: Path, version: str) -> None:
    path.write_text(
        f"""TRIKI Control {version} for Linux

Created by Wojciech "Koksny" Górny, Koksny.com.
Released under the MIT License.

First run:
  python3 -m venv .venv
  . .venv/bin/activate
  python -m pip install -r requirements.txt
  ./triki-control

Before using key output, read docs/linux.md and run:
  python triki_linux_smoke.py --json
""",
        encoding="utf-8",
        newline="\n",
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Package TRIKI Control for Linux.")
    parser.add_argument("--version", default="")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--release-dir", type=Path, default=Path("release"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    version = args.version
    if not version:
        from triki_metadata import APP_VERSION

        version = APP_VERSION
    archive_path = build_linux_release(
        root=args.root,
        release_dir=args.release_dir,
        version=version,
    )
    print(f"Packaged {archive_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
