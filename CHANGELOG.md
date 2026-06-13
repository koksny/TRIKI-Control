# Changelog

## Unreleased

- Refreshed the README screenshot and packaged application icon assets.
- Fixed Windows builds so the bundled WebView runtime includes OpenSSL DLLs required by Python's SSL module.

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
- **Music profile** retained for one-shot media gestures (volume, play/pause, mute).

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
