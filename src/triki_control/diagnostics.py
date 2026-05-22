from __future__ import annotations

import argparse
import importlib
import json
import platform
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from triki_control.linux_smoke import probe_uinput_status
from triki_control.key_emitter import is_macos_accessibility_trusted
from triki_control.metadata import APP_NAME, APP_VERSION


DIAGNOSTIC_MODULES = ("bleak", "webview")


def module_import_status(
    names: Sequence[str] = DIAGNOSTIC_MODULES,
    *,
    importer: Callable[[str], object] = importlib.import_module,
) -> dict:
    results = {}
    for name in names:
        try:
            importer(name)
        except Exception as exc:
            results[name] = {
                "status": "missing",
                "message": f"{type(exc).__name__}: {exc}",
            }
        else:
            results[name] = {"status": "ok", "message": ""}
    return results


def collect_diagnostics(
    *,
    config_path: Path | None,
    system_name: str | None = None,
    python_version: str | None = None,
    import_status: Callable[[Sequence[str]], dict] = module_import_status,
    uinput_probe: Callable[[str], dict] = probe_uinput_status,
    macos_accessibility_probe: Callable[[], dict] | None = None,
) -> dict:
    system = system_name if system_name is not None else platform.system()
    modules = import_status(DIAGNOSTIC_MODULES)
    if system == "Linux":
        uinput = uinput_probe("/dev/uinput")
    else:
        uinput = {
            "device_path": "",
            "exists": False,
            "readable": False,
            "writable": False,
            "status": "not-applicable",
        }
    key_output = _key_output_status(system, uinput, macos_accessibility_probe)
    fixes = _diagnostic_fixes(system, modules, uinput, key_output)
    return {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "platform": {
            "system": system,
            "python": python_version if python_version is not None else platform.python_version(),
        },
        "config_path": _display_path(config_path),
        "modules": modules,
        "uinput": uinput,
        "key_output": key_output,
        "fixes": fixes,
    }


def _key_output_status(
    system: str,
    uinput: dict,
    macos_accessibility_probe: Callable[[], dict] | None,
) -> dict:
    if system == "Windows":
        return {"backend": "SendInput", "status": "available"}
    if system == "Linux":
        return {"backend": "uinput", **uinput}
    if system == "Darwin":
        probe = macos_accessibility_probe or probe_macos_accessibility_status
        return {"backend": "Quartz CGEvent", **probe()}
    return {"backend": "", "status": "unsupported"}


def probe_macos_accessibility_status(
    checker: Callable[[], bool] = is_macos_accessibility_trusted,
) -> dict:
    trusted = bool(checker())
    return {
        "trusted": trusted,
        "status": "trusted" if trusted else "untrusted",
        "message": (
            "Accessibility permission is granted."
            if trusted
            else "Accessibility permission is not granted."
        ),
    }


def _diagnostic_fixes(system: str, modules: dict, uinput: dict, key_output: dict) -> list[str]:
    fixes: list[str] = []
    missing = [name for name, status in modules.items() if status.get("status") != "ok"]
    if missing:
        fixes.append(
            "Install Python dependencies with: python -m pip install -r requirements.txt"
        )
    if system == "Linux":
        if uinput.get("status") == "missing":
            fixes.append("Load the Linux uinput module with: sudo modprobe uinput")
        elif uinput.get("status") == "permission":
            fixes.append("Configure the udev rule and group permissions from docs/linux.md.")
    if system == "Darwin":
        if key_output.get("status") == "untrusted":
            fixes.append(
                "Grant Accessibility permission to TRIKI Control in System Settings before key output."
            )
        fixes.append(
            "Launch the packaged macOS app bundle with NSBluetoothAlwaysUsageDescription; "
            "bare Python processes crash under Bluetooth privacy checks."
        )
    return fixes


def _display_path(path: Path | None) -> str:
    if path is None:
        return ""
    return path.as_posix()


def format_text_report(report: dict) -> str:
    lines = [
        f"{report['app_name']} {report['app_version']}",
        f"platform: {report['platform']['system']} python={report['platform']['python']}",
        f"config: {report['config_path'] or '(default)'}",
        f"uinput: {report['uinput']['status']}",
        f"key_output: {report['key_output']['status']}",
    ]
    for name, status in report["modules"].items():
        lines.append(f"module {name}: {status['status']}")
    for fix in report["fixes"]:
        lines.append(f"fix: {fix}")
    return "\n".join(lines) + "\n"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Print TRIKI environment diagnostics.")
    parser.add_argument("--config-path", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout=None,
    system_name: str | None = None,
    import_status: Callable[[Sequence[str]], dict] = module_import_status,
    uinput_probe: Callable[[str], dict] = probe_uinput_status,
    macos_accessibility_probe: Callable[[], dict] | None = None,
) -> int:
    args = build_arg_parser().parse_args(argv)
    report = collect_diagnostics(
        config_path=args.config_path,
        system_name=system_name,
        import_status=import_status,
        uinput_probe=uinput_probe,
        macos_accessibility_probe=macos_accessibility_probe,
    )
    out = stdout if stdout is not None else sys.stdout
    if args.json:
        out.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    else:
        out.write(format_text_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
