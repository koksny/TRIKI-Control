# TRIKI Architecture

## Goals

TRIKI Control targets the `Triki` / `CAP001` Bluetooth motion cap produced by Caps Apps. The project has one primary delivery target and one future companion track:

1. A desktop app that pairs the cap, runs in the background, and maps gestures to keyboard, media-key, and macro actions.
2. A separate Unity integration project that can reuse the protocol and gesture knowledge without sharing this app's UI or packaging.

The desktop app should stay focused on practical remapping. Unity support should live in its own repository/package because it has different distribution, API, and game-engine requirements.

## Runtime Layers

```text
TRIKI BLE device
  -> protocol parser
  -> motion samples
  -> gesture detector
  -> gesture event
  -> action mapping
  -> action backend
```

The first production slice keeps this in Python:

- `src/triki_control/protocol.py`: packet parser.
- `src/triki_control/probe.py`: BLE discovery and Nordic UART constants.
- `src/triki_control/live.py`: rolling-window live detector.
- `src/triki_control/actions.py`: persistent action model and action executor.
- `src/triki_control/app.py`: background app plus local config UI.

## Desktop App Boundary

The desktop app owns BLE pairing. The input backend should never pair TRIKI directly. That keeps remapping simple:

```text
BLE pairing and streaming: app/daemon
mapping decisions: app/daemon config
keyboard/media/macro output: backend
```

Backends receive already-decided action events, not raw BLE samples.

## Stable UI Rule

The config UI must not rebuild mapping controls during live input. Live sample and gesture events are frequent, so the app uses an `action_revision` number. Mapping controls render only when `action_revision` changes; counters and recent events update independently. This prevents native dropdowns from closing while TRIKI is streaming.

The desktop app uses manual pairing only for the first connection request. Once the user clicks Pair, later disconnects and failed sessions automatically re-enter the reconnect loop so the app behaves like a background controller instead of a calibration tool.

The standalone build runs the same UI in an embedded WebView and adds a tray controller. Closing the window hides it instead of stopping BLE streaming; the tray menu can reopen the main window, request pairing, open the diagnostics page, or quit the process. The browser/dev mode keeps the old foreground server behavior.

The header battery indicator is a UI state field, not part of the motion protocol. The runtime includes both the Nordic UART service and the standard Battery Service in the filtered GATT discovery list, then reads the Battery Level characteristic (`2A19`) after GATT connects and publishes the result into session state. If the characteristic is missing or blocked by the Windows BLE service cache, the app marks battery as unavailable and keeps the UI usable.

The header LED test button is a direct device command, not an action mapping. The HTTP UI sends hold/release requests to a thread-safe BLE command bridge, which schedules writes on the active Bleak event loop. It writes `01`/`00` with response to the TRIKI LED characteristic (`6e400004-b5a3-f393-e0a9-e50e24dcca9e`) and is disabled whenever the BLE session is not connected.

Named profiles live in the same config file as action mappings. Each profile stores a full gesture-to-action map, and the active profile is materialized into `actions` for backward compatibility with older config readers. The built-in profile set is `Default`, `WASD Game`, `Media`, `Presentation`, and `Which Sausage, Mate?`. Version-5 configs migrate older built-in profile defaults to the current bindings while preserving custom overrides, and still include older preset migrations for profile-era configs. The main UI exposes profile selection, mapping controls, profile import/export, active-profile reset, and all-profile reset. The hidden `/debug` page exposes live counters, connection logs, and recent gesture events for development and support.

## Action Output Strategy

Keyboard and media output are the first production targets because they work from user mode. The action executor now chooses the key emitter through a platform boundary instead of constructing a Windows emitter directly.

On Windows, normal keyboard actions are emitted through `SendInput` scancodes rather than only virtual keys so games that poll keyboard state by scancode have a better chance of seeing TRIKI actions. The scancode path covers arrows, page keys, letters, digits, function keys, `=`, and common control keys. Media keys remain virtual-key events.

This is still synthetic input. Games that use lower-level raw input, anti-cheat filtering, or device-specific input may ignore it even when Notepad accepts it. Those cases are outside the desktop remapper's scope and should use native in-game TRIKI support instead.

On Linux, the default output backend is lazy `/dev/uinput` initialization. The app can start without opening `/dev/uinput`; the first emitted action creates a virtual keyboard device and reports a visible output error if permissions are missing. This keeps pairing and configuration usable even before udev/group setup is complete.

On macOS, keyboard and media-key output uses Quartz/CoreGraphics events and requires Accessibility permission for the packaged app.

A Windows HID/gamepad driver is intentionally not planned. TRIKI does not expose enough continuous axes or buttons to make a virtual gamepad driver worth the signing, installer, and maintenance cost.

## Unity Strategy

Unity support should be its own project, not a dependency or milestone inside the desktop app. The game-side project should:

- provide a `Pair TRIKI` option,
- connect to the same BLE/Nordic UART service,
- parse the same motion packets,
- either classify gestures in C# or call a native classifier plugin,
- expose Unity Input System actions or simple C# events.

The desktop app remains useful for games and apps that accept synthetic keyboard/media input. Unity-native games can avoid external setup by pairing TRIKI directly inside the game.
