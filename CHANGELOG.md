# Changelog

## 1.0.5

- Preserved Game/Doom key overrides from 1.0.1 and 1.0.2 configs across the 1.0.4 Motion tuning schema update.
- Preserved Music key overrides from 1.0.2 configs while still resetting the broken 1.0.1 Music-as-Game defaults.
- Kept Game's motion thresholds and default Doom controls unchanged from 1.0.1/1.0.2.

## 1.0.4

- Moved profile-specific threshold tuning to the actual turn/twist threshold instead of the lean threshold.
- Added a separate Turn threshold slider next to Turn sensitivity in Advanced, with Game keeping the old 1000 default and Music using a lower 580 default.
- Kept the volume-knob media tap repeat from 1.0.3, while making Music's twist detection easier to tune without changing Game steering.

## 1.0.3

- Made volume up/down behave like repeated media-key taps while twisting, instead of being swallowed by the keyboard hold bridge.
- Added the first pass of per-profile Motion tuning for Music/Game.
- Persisted Motion tuning through config export/import.

## 1.0.2

- Restored media-key defaults for the Music profile while keeping the shared Motion/Game action rows in Advanced.
- Bumped the config schema so Music settings saved by 1.0.1 reset from accidental Game keys back to media controls.
- Kept the Windows, macOS and Linux package helpers/tests aligned with the repo-root source layout.

## 1.0.1

- Refreshed the README screenshot and packaged application icon assets.
- Fixed Windows builds so the bundled WebView runtime includes OpenSSL DLLs required by Python's SSL module.
- Changed Music and custom profiles to use the same Motion/Game action mapping as Game, removing the stale classifier-only rows from Advanced.
- Renamed the Advanced dialog title to plain "Advanced settings" / "Ustawienia zaawansowane".

## 1.0.0

First public release. The cap is a playable game controller, tuned end-to-end until it could finish Doom.

### The controls

- **Rotation-invariant "tank" scheme** for the Game profile, built from heading-free signals so every control works no matter how the cap is turned:
  - **Twist** turns left / right
  - **Tilt and hold** walks forward
  - **Tap down** fires
  - **Flip over** runs (Shift)
  - **Flat slide** uses / opens a door
- A **Turn sensitivity** slider and an editable **Action Mapping** table for every control.
- **Music profile** retained as the second built-in profile slot.

### Why the scheme changed from earlier alphas

Earlier builds chased a directional / WASD-style joystick (strafe, push-to-go). That turned out to be physically impossible on this hardware: with a gyroscope and accelerometer but **no magnetometer**, a round cap's heading is unobservable and drifts within seconds, so world-stable directional movement cannot be done reliably. It is the same reason the cap's official games each use a single axis of motion. 1.0.0 commits fully to the rotation-invariant design, which is what is actually achievable, and playable. See [docs/how-it-works.md](docs/how-it-works.md).

### Reliability work

- **One-motion-one-action** gesture locking, to stop the per-sample event storm.
- **Self-learning neutral** with a median bootstrap and a gravity-independent flatness gate, fixing a startup deadlock where a bad first packet could leave the cap reading a permanent phantom slide.
- **Bursty-Bluetooth tolerance:** key-hold bridging for inter-packet gaps, and reconnects that preserve the learned calibration instead of re-running the dead-time startup.
- **Stamp / fire detection** keyed on vertical impact, gated against turns and against the recovery wobble after a tap, for reliable shooting without ghost-firing or ghost-walking.
- **Responsive UI** that scales to fit any window, down to a small monitor.

### Project

- Source organised under a flat `src/triki_*.py` layout.
- Documentation rewritten from scratch.
- Version set to 1.0.0.
