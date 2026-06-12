# Changelog

## 1.0.0

First public release. The cap is a playable game controller — tuned end-to-end until it could finish Doom.

### The controls

- **Rotation-invariant "tank" scheme** for the Game profile, built from heading-free signals so every control works no matter how the cap is turned:
  - **Twist** → turn left / right
  - **Tilt and hold** → walk forward
  - **Tap down** → fire
  - **Flip over** → run (Shift)
  - **Flat slide** → use / open door
- A **Turn sensitivity** slider and an editable **Action Mapping** table for every control.
- **Music profile** retained for one-shot media gestures (volume, play/pause, mute).

### Why the scheme changed from earlier alphas

Earlier builds chased a directional / WASD-style joystick (strafe, push-to-go). That turned out to be physically impossible on this hardware: with a gyroscope and accelerometer but **no magnetometer**, a round cap's heading is unobservable and drifts within seconds, so world-stable directional movement can't be done reliably. 1.0.0 commits fully to the rotation-invariant design — what's actually achievable, and playable. See [docs/how-it-works.md](docs/how-it-works.md).

### Reliability work

- **One-motion-one-action** gesture locking, to stop the per-sample event storm.
- **Self-learning neutral** with a median bootstrap and a gravity-independent flatness gate, fixing a startup deadlock where a bad first packet could leave the cap reading a permanent phantom slide.
- **Bursty-Bluetooth tolerance**: key-hold bridging for inter-packet gaps, and reconnects that preserve the learned calibration instead of re-running the dead-time startup.
- **Stamp / fire detection** keyed on vertical impact, gated against turns and against the recovery wobble after a tap — reliable shooting, without ghost-firing or ghost-walking.
- **Responsive UI** that scales to fit any window, down to a small monitor.

### Project

- Repository restructured to a flat module layout (`triki_*.py`).
- Documentation rewritten from scratch.
- Version set to 1.0.0.
