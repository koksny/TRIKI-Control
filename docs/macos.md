# TRIKI Control on macOS

macOS support uses the shared TRIKI BLE, classifier, profile, and local UI code.
The native macOS pieces are Bluetooth privacy packaging and keyboard output through
Quartz/CoreGraphics events.

## Requirements

- macOS 15 tested on Apple Silicon.
- Python 3.10 or newer for source runs. The validated local venv uses Python 3.13.13.
- A packaged `.app` for Bluetooth scanning. Bare command-line Python can be killed by
  macOS privacy checks if it does not have an app `Info.plist` containing
  `NSBluetoothAlwaysUsageDescription`.

## First Run

1. Open `TRIKI Control.app`.
2. Allow Bluetooth access when macOS prompts.
3. Click `Pair TRIKI`.
4. Press the physical TRIKI pairing button once when prompted.
5. Wait for the Pair button to turn green.
6. Hold `Test LED` to confirm the app can send a command back to the cap.

## Keyboard Output Permission

macOS requires Accessibility permission before TRIKI can emit configured key
actions.

Grant it in:

```text
System Settings > Privacy & Security > Accessibility
```

Enable permission for `TRIKI Control.app`. During development, permission may need
to be granted to the temporary app bundle or the terminal host process used to run
the app.

If diagnostics report `key_output: untrusted`, Bluetooth and motion streaming can
still work, but mapped key output will be blocked.

After replacing or rebuilding the ad-hoc signed app, macOS may keep an old
Accessibility entry that no longer matches the current bundle. If gestures show
`key emitted` but focused apps receive nothing, remove `TRIKI Control.app` from
Accessibility, add the rebuilt app again, then quit and reopen TRIKI Control.

## Source Diagnostics

From the repo root:

```bash
source .venv/bin/activate
python triki_diagnostics.py --json
```

Expected macOS diagnostics include:

- `platform.system = Darwin`
- `bleak = ok`
- `webview = ok`
- `uinput = not-applicable`
- `key_output.backend = Quartz CGEvent`

## Build

From the repo root on macOS:

```bash
bash tools/build_macos_app.sh
bash tools/package_macos_release.sh
```

The release zip is written to:

```text
release/TRIKI-Control-<version>-macos.zip
```

The build script strips copied extended attributes, then applies an ad-hoc
signature so the local bundle can launch consistently from mounted project
volumes. The alpha app is not notarized. For local testing, right-click the app
and choose `Open` if macOS blocks first launch.

## Validated Alpha Status

Validated on macOS 15.6 Apple Silicon:

- Packaged app launch through LaunchServices.
- BLE scan, GATT connection, UART notifications, and Battery Level read.
- LED on/off command endpoint.
- Gesture detection and action routing in dry-run mode.
- Release zip creation with app symlinks preserved.

Synthetic key delivery still depends on granting Accessibility permission to the
app on the test Mac.
