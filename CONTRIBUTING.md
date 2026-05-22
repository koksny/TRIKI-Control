# Contributing

TRIKI Control is an alpha desktop remapper for a specific BLE motion controller. Contributions should keep the app practical, small, and easy to test.

## Development Setup

```bash
python -m venv .venv
python -m pip install -r requirements.txt
python -m pip install -r requirements-build.txt
python -m pip install -e .
```

Run tests:

```bash
python -m unittest discover -s tests
```

Run the app:

```bash
python -m triki_control.app
```

## Scope

In scope:

- BLE stability improvements.
- Gesture detection and calibration improvements.
- Desktop mapping UI improvements.
- Keyboard, media-key, and macro output.
- Windows, macOS, and Linux packaging.
- Documentation for protocol and platform behavior.

Out of scope for this repository:

- Windows HID/gamepad driver.
- Unity package implementation.
- Android app implementation.

Unity support should be a separate project that reuses this repository as protocol and gesture reference material.

## Pull Requests

- Keep changes focused.
- Add or update tests for behavior changes.
- Update documentation when user-facing behavior changes.
- Do not commit local build output, virtual environments, bugreports, release archives, or generated logs.
- Keep README formatting plain and consistent with the existing style.

## Testing Checklist

Before opening a pull request, run:

```bash
python -m unittest discover -s tests
```

For platform-specific changes, also test the relevant packaged app or platform smoke path:

- Windows: `tools\build_windows_exe.ps1`
- macOS: `tools/build_macos_app.sh`
- Linux: `tools/package_linux_release.sh`

## License

By contributing, you agree that your contribution is provided under the MIT License.
