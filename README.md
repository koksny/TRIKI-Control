# TRIKI Control

![Version](https://img.shields.io/badge/version-0.1.0--alpha.1-blue.svg) ![License](https://img.shields.io/badge/license-MIT-green.svg) ![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)

Desktop remapper for the TRIKI Bluetooth motion cap.

TRIKI Control pairs with the cap, reads motion data over Bluetooth Low Energy, detects gestures, and maps them to keyboard, media-key, or macro actions. It is built as a practical background utility: one pairing button, simple profiles, and persistent remapping.

Screenshots and GIFs will be added before the first public release.

[Features](#features) | [Quick Start](#quick-start) | [Usage](#usage) | [Documentation](#documentation) | [Contributing](CONTRIBUTING.md)

---

## Overview

TRIKI is a Bluetooth motion cap originally intended for Android. The device name is `Triki`, the model is `CAP001`, and the producer is Caps Apps. This project provides an independent desktop control app for Windows, macOS, and Linux, with a shared BLE parser, gesture classifier, local configuration UI, and platform-specific input output.

The current app supports rotate, scrub, back-and-forth, stamp, and flip gestures.

## Features

- BLE pairing and TRIKI motion streaming.
- Local desktop configuration UI with tray behavior in packaged builds.
- Gesture detection for rotate left/right, scrub left/right, back-and-forth, stamp, and flip.
- Persistent gesture-to-action mappings.
- Built-in profiles:
  - `Default`
  - `WASD Game`
  - `Media`
  - `Presentation`
  - `Which Sausage, Mate?`
- Keyboard, media-key, and simple macro actions.
- Header battery indicator when the standard BLE Battery Level characteristic is available.
- Press-and-hold LED test control for checking the paired cap.
- Hidden diagnostics page for debugging connection and output issues.
- Windows, macOS, and Linux build/package scripts.

## Quick Start

### Development

```bash
python -m venv .venv
```

Windows:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe triki_app.py
```

macOS/Linux:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
python triki_app.py
```

The app opens the local configuration UI. If it does not open automatically, use:

```text
http://127.0.0.1:8766/
```

### Pairing

1. Start TRIKI Control.
2. Click `Pair TRIKI`.
3. Press the physical TRIKI pairing button once when prompted.
4. Wait for the Pair button to turn green and read `Connected`.
5. Hold `Test LED` to confirm the app can send a command back to the controller.
6. Choose a profile or edit mappings, then focus the game or app you want to control.

## Usage

### Profiles

Profiles can be selected, edited, exported, imported, reset individually, or reset as a full built-in set.

`Default` maps rotation to arrow keys, scrub to Page Up/Page Down, stamp to Enter, flip to Space, and back-and-forth to Escape.

`WASD Game` maps rotation to A/D and scrub to W/S.

`Media` maps gestures to volume and playback controls.

`Presentation` maps gestures to slide-style controls.

`Which Sausage, Mate?` maps rotate right/left to Right/Left Arrow, scrub right/left to `=`/`Z`, back-and-forth to Backspace, stamp to Enter, and flip to Space.

### Action Types

TRIKI Control supports:

- `disabled`
- single keyboard keys, for example `left`, `right`, `w`, `a`, `s`, `d`, `enter`, `space`, `=`
- media keys, for example `volume-up`, `volume-down`, `media-play-pause`
- simple macros, for example `left, 100ms, enter`

### Diagnostics

The hidden diagnostics page is available at:

```text
http://127.0.0.1:8766/debug
```

The diagnostics JSON endpoint is available at:

```text
http://127.0.0.1:8766/diagnostics
```

## Build

Install build dependencies:

```bash
python -m pip install -r requirements-build.txt
```

Windows:

```powershell
.\tools\build_windows_exe.ps1
.\tools\package_windows_release.ps1
```

Add `-ExeAsset` to also create a direct-download `TRIKI-Control-<version>-windows.exe` release asset alongside the full zip package.

macOS:

```bash
bash tools/build_macos_app.sh
bash tools/package_macos_release.sh
```

Linux source package:

```bash
bash tools/package_linux_release.sh
```

## Project Structure

```text
TRIKI-Control/
|-- docs/                         # Architecture, protocol, platform, release notes
|-- tests/                        # Unit tests
|-- tools/                        # Build and packaging scripts
|-- triki_app.py                  # Background app and local UI
|-- triki_probe.py                # BLE discovery and motion sample probe
|-- triki_protocol.py             # Packet parser
|-- triki_classifier.py           # Gesture classifier
|-- triki_live.py                 # Rolling-window live detector
|-- triki_actions.py              # Profiles, mappings, macros, executor
|-- triki_key_emitter.py          # Windows, macOS, and Linux output backends
|-- triki_diagnostics.py          # Environment diagnostics
|-- requirements.txt              # Runtime dependencies
|-- requirements-build.txt        # Build dependencies
`-- README.md
```

## Platform Notes

### Windows

Keyboard actions use `SendInput` scancodes for normal keys and virtual-key events for media keys. Some games that reject synthetic input may still ignore these events.

### macOS

The packaged app includes Bluetooth privacy strings required by CoreBluetooth. Keyboard and media-key output uses Quartz/CoreGraphics and requires Accessibility permission in System Settings.

### Linux

Keyboard output uses `/dev/uinput` and requires uinput permissions. See [docs/linux.md](docs/linux.md).

## Documentation

- [Architecture](docs/architecture.md)
- [Protocol Notes](docs/protocol.md)
- [Linux Notes](docs/linux.md)
- [macOS Notes](docs/macos.md)
- [Release Notes](docs/release.md)
- [Roadmap](docs/roadmap.md)

## Contributing

Contributions are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) before submitting pull requests.

## Credits

- Author: [Wojciech "Koksny" Górny](https://koksny.com)
- Device: `Triki` / `CAP001`, produced by Caps Apps

## Trademark and Affiliation

TRIKI Control is an independent open-source project. It is not affiliated with, endorsed by, or sponsored by Caps Apps, Żabka, or their partners.

TRIKI, Żabka, and related names and logos are trademarks or registered trademarks of their respective owners.

## License

This project is licensed under the [MIT License](LICENSE).
