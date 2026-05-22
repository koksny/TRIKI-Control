# Changelog

## 0.1.0-alpha.1

Initial public-style alpha.

### Added

- BLE pairing and motion streaming for TRIKI.
- Gesture detection for rotate, scrub, back-and-forth, stamp, and flip.
- Local configuration UI with mapping profiles.
- Keyboard, media-key, and macro actions.
- Battery indicator when the standard BLE Battery Level characteristic is available.
- Press-and-hold LED test command.
- Windows `SendInput` output backend.
- macOS Quartz/CoreGraphics output backend.
- Linux `/dev/uinput` output backend.
- Diagnostics endpoints and command-line diagnostics.
- Windows, macOS, and Linux packaging scripts.

### Notes

- This is an alpha build. BLE stability depends on adapter range and OS Bluetooth behavior.
- macOS key output requires Accessibility permission.
- Linux key output requires `/dev/uinput` permissions.
- A Windows HID/gamepad driver is not planned for this app.
