# Controls & play guide

TRIKI Control ships two built-in profiles. The **Game** profile is the headline act — a rotation-invariant scheme tuned for shooters. The **Music** profile is a simpler set of one-shot gestures for media control. Everything below is remappable in **Advanced → Action Mapping**.

## Game profile (the tank scheme)

This is the default, and the one I actually played Doom with. It's run by the body-frame motion engine (`triki_motion_engine.py`). Read [how-it-works.md](how-it-works.md) for *why* it's shaped like this; here's how to drive it.

| Move | Default key | How to perform it |
| --- | --- | --- |
| **Turn left / right** | `Left` / `Right` arrow | Lay the cap flat and **twist it in place**, like turning a dial. Left twist turns left, right twist turns right. |
| **Walk forward** | `W` | **Tilt** the cap in any direction and **hold** it tilted. It's the *amount* of tilt that matters, not the direction. |
| **Fire** | `Enter` | **Tap** the cap straight down onto the desk — a short, square knock. |
| **Run** | `Shift` (held) | **Flip** the cap upside-down and leave it. Flip back to stop running. |
| **Use / open door** | `Space` | **Slide** the cap flat across the desk in a straight line. |

### Getting clean input

A few habits make the difference between "fighting it" and "playing it":

- **Twist *or* tilt, not both.** The engine checks tilt first: if the cap is leaning, it's a *walk*, and a twist mixed into a lean won't register as a turn. Square up, twist to aim, then tilt to move. Trying to curve while walking won't work — that's the tank trade-off.
- **Keep fire-taps square.** A fire is a clean vertical knock. If you twist as you tap, the cap spins and the engine (correctly) reads a turn instead. Tap down, not down-and-around.
- **Shoot while standing still.** Right after a tap, the app briefly ignores tilt, because lifting the cap back up after a knock re-tilts it and would otherwise nudge you forward. So you fire cleanest when you're not mid-stride.
- **Slides should stay flat.** A door-slide is the cap gliding across the table without lifting or tilting. If you tilt while you slide, it reads as a *walk* instead — so push it along the surface, don't scoop it.

### Tuning

In **Advanced**, while the Game profile is active:

- **Lean engage** — how far you have to tilt before it walks. Lower = touchier.
- **Turn sensitivity** — how hard you have to twist before it turns. The default (50) is a good middle. Bump it up if gentle turns get missed; drop it if you get the occasional ghost turn.

Every row in **Action Mapping** can be rebound to any key, media key, or short macro. The Game control labels are `turn-left` and `turn-right` (turn), `go` (walk), `stamp` (fire), `flip` (run), and `scrub-straight` (use/door).

## Music profile

A calmer, couch-friendly set of discrete one-shot gestures, run by the classifier instead of the motion engine:

| Gesture | Default action |
| --- | --- |
| Twist right / left | Volume up / down |
| Tap down | Play / pause |
| Shake side-to-side | Play / pause |
| Flip over | Mute |

These fire once per gesture rather than holding a key, which is what you want for media.

## A note on "more axes"

You'll notice the Game profile has no strafe and no separate forward/back/left/right movement — just turn and go. That isn't a missing feature; it's the hard limit of the hardware. A round cap with a gyro and accelerometer but **no magnetometer** cannot know which way it's pointing, so directional movement (push-left-go-left) is physically impossible to do reliably. The tank scheme is what's achievable, and it's enough to finish a game. The full explanation is in [how-it-works.md](how-it-works.md).

## Output, safety, and the panic switch

- The big **Output ON / OFF** toggle is the master switch. Keys only reach your system when it's ON. Flip it OFF before you set the cap down, or it'll keep "playing."
- Closing the app releases any held keys. If something ever feels stuck, toggling Output OFF (or quitting) clears it.
- Bindings and the active profile are saved to a single JSON config file, so your setup survives restarts.
