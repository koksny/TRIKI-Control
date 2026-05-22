# TRIKI Roadmap

## V1: Practical Desktop Remapper

Scope:

- Windows-first local background app.
- Linux-compatible architecture.
- Pairing button and connection status.
- Persistent gesture-to-action mappings with named profiles.
- Built-in `Default`, `WASD Game`, `Media`, `Presentation`, and `Which Sausage, Mate?` profiles.
- Keyboard, media-key, and macro actions.
- Stable config UI while live input is streaming.
- Tray lifecycle for the standalone build.
- Profile import/export and reset safety controls.
- Hidden diagnostics page for support/debugging.
- Open-source docs for protocol, app, and future integration.

Current implementation entry point:

```powershell
.\.venv\Scripts\python.exe -m triki_control.app
```

## V1.1: Packaging

Package the Python app into a single runnable desktop artifact.

Candidate paths:

- Current: PyInstaller `.exe` with embedded WebView and tray menu.
- Current: PyInstaller macOS `.app` with Bluetooth privacy strings, ad-hoc signing for local alpha testing, and Quartz key output.
- Later: Tauri shell plus Python sidecar if we want a more native installer, auto-start, and update story without rewriting the runtime yet.

The app should keep using a local daemon boundary so packaging can change without changing BLE or action logic.

## V1.5: Linux Output Backend

Add Linux output:

- current: lazy `/dev/uinput` virtual-keyboard backend with documented udev rule, group permissions, and dry-run smoke command in `docs/linux.md`,
- current: source-style Linux tarball with launcher and diagnostics tools.

## V1.6: macOS App

Add macOS support:

- current: PyInstaller `.app` with Bluetooth privacy strings,
- current: Quartz/CoreGraphics keyboard and media-key output,
- current: macOS diagnostics for Accessibility permission,
- later: signing/notarization decision for public distribution.

## Explicit Non-Goals In This Repo

### Windows Virtual Gamepad/HID Driver

No Windows HID/gamepad driver is planned for this app.

The device has a small gesture vocabulary and no continuous stick/button surface large enough to justify a virtual gamepad driver. A custom driver would also add signing, installer, test-mode, and maintenance work that does not fit the practical remapper goal.

### Unity Integration

Unity support should be a separate project/repository, not a milestone inside this desktop app.

This repo should still keep protocol, packet, and gesture notes clear enough that a Unity package can reuse the knowledge later. The Unity project can own its own BLE pairing, C# classifier or native classifier bridge, sample scene, and Unity Input System integration.
