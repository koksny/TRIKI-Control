from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from collections.abc import Callable, Sequence

from triki_control.key_emitter import (
    KeyEmissionError,
    LinuxUInputKeyEmitter,
    linux_evdev_code_for_key,
    normalize_key_name,
)


def probe_uinput_status(
    device_path: str = "/dev/uinput",
    *,
    exists: Callable[[str], bool] = os.path.exists,
    access: Callable[[str, int], bool] = os.access,
) -> dict:
    present = exists(device_path)
    readable = present and access(device_path, os.R_OK)
    writable = present and access(device_path, os.W_OK)
    if not present:
        status = "missing"
    elif readable and writable:
        status = "ready"
    else:
        status = "permission"
    return {
        "device_path": device_path,
        "exists": present,
        "readable": readable,
        "writable": writable,
        "status": status,
    }


def build_smoke_report(
    *,
    key_name: str = "space",
    emit: bool = False,
    device_path: str = "/dev/uinput",
    system_name: str | None = None,
    emitter_factory: Callable[[], object] | None = None,
    probe: Callable[[str], dict] = probe_uinput_status,
) -> dict:
    system = system_name if system_name is not None else platform.system()
    key = normalize_key_name(key_name)
    report = {
        "platform": system,
        "key": key,
        "evdev_code": linux_evdev_code_for_key(key),
        "emit_requested": emit,
        "emitted": False,
        "uinput": probe(device_path),
    }
    if system != "Linux":
        report["status"] = "unsupported-platform"
        report["error"] = f"Linux smoke requires Linux, got {system or 'unknown'}."
        return report
    if not emit:
        report["status"] = "dry-run-ok"
        return report

    emitter = None
    try:
        emitter = (
            emitter_factory()
            if emitter_factory is not None
            else LinuxUInputKeyEmitter(device_path=device_path)
        )
        emitter.press_key(key)
    except (OSError, KeyEmissionError) as exc:
        report["status"] = "emit-failed"
        report["error"] = str(exc)
        return report
    finally:
        if emitter is not None:
            close = getattr(emitter, "close", None)
            if close is not None:
                close()

    report["status"] = "emit-ok"
    report["emitted"] = True
    return report


def format_text_report(report: dict) -> str:
    lines = [
        f"status: {report['status']}",
    ]
    if "platform" in report:
        lines.insert(0, f"platform: {report['platform']}")
    if "evdev_code" in report:
        lines.append(f"key: {report['key']} evdev={report['evdev_code']}")
    elif "key" in report:
        lines.append(f"key: {report['key']}")
    if "uinput" in report:
        lines.append(f"uinput: {report['uinput'].get('status')} {report['uinput'].get('device_path')}")
    if report.get("error"):
        lines.append(f"error: {report['error']}")
    if report["status"] == "dry-run-ok":
        lines.append("dry run only; pass --emit to create the virtual device and press the key.")
    return "\n".join(lines) + "\n"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Smoke-test TRIKI Linux uinput output.")
    parser.add_argument("--key", default="space", help="Key name to validate or emit.")
    parser.add_argument("--device", default="/dev/uinput", help="uinput device path.")
    parser.add_argument("--emit", action="store_true", help="Actually emit the key through uinput.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout=None,
    system_name: str | None = None,
    emitter_factory: Callable[[], object] | None = None,
    probe: Callable[[str], dict] = probe_uinput_status,
) -> int:
    args = build_arg_parser().parse_args(argv)
    out = stdout if stdout is not None else sys.stdout
    try:
        report = build_smoke_report(
            key_name=args.key,
            emit=args.emit,
            device_path=args.device,
            system_name=system_name,
            emitter_factory=emitter_factory,
            probe=probe,
        )
    except ValueError as exc:
        report = {
            "status": "invalid-key",
            "error": str(exc),
            "key": args.key,
            "emit_requested": args.emit,
            "emitted": False,
        }
    if args.json:
        out.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        out.write(format_text_report(report))
    return 0 if report["status"] in {"dry-run-ok", "emit-ok"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
