# Controls & play guide

TRIKI Control ships two built-in profile slots. **Game** is the default, rotation-invariant scheme tuned for shooters. **Music** is a second built-in slot with the same controls but media-key defaults, so **Advanced > Action Mapping** shows one consistent table for every profile.

## Game profile (the tank scheme)

This is the default, and the one I actually played Doom with. It is run by the body-frame motion engine (`src/triki_motion_engine.py`). Read [how-it-works.md](how-it-works.md) for *why* it is shaped like this; here is how to drive it.

| Move | Default key | How to perform it |
| --- | --- | --- |
| **Turn left / right** | `Left` / `Right` arrow | Lay the cap flat and **twist it in place**, like turning a dial. Left twist turns left, right twist turns right. |
| **Walk forward** | `W` | **Tilt** the cap in any direction and **hold** it tilted. It is the *amount* of tilt that matters, not the direction. |
| **Fire** | `Enter` | **Tap** the cap straight down onto the desk, a short, square knock. |
| **Run** | `Shift` (held) | **Flip** the cap upside-down and leave it. Flip back to stop running. |
| **Use / open door** | `Space` | **Slide** the cap flat across the desk in a straight line. |

### Getting clean input

A few habits make the difference between "fighting it" and "playing it":

- **Twist *or* tilt, not both.** The engine checks tilt first: if the cap is leaning, it is a *walk*, and a twist mixed into a lean will not register as a turn. Square up, twist to aim, then tilt to move. Trying to curve while walking will not work; that is the tank trade-off.
- **Keep fire-taps square.** A fire is a clean vertical knock. If you twist as you tap, the cap spins and the engine (correctly) reads a turn instead. Tap down, not down-and-around.
- **Shoot while standing still.** Right after a tap, the app briefly ignores tilt, because lifting the cap back up after a knock re-tilts it and would otherwise nudge you forward. So you fire cleanest when you are not mid-stride.
- **Slides should stay flat.** A door-slide is the cap gliding across the table without lifting or tilting. If you tilt while you slide, it reads as a *walk* instead, so push it along the surface, do not scoop it.

### Tuning

In **Advanced**, for any profile:

- **Turn threshold:** how strong the twist has to be before it counts as a turn. Lower is touchier.
- **Turn sensitivity:** how forgiving the turn detector is for slower or less-perfect twists. These turn tuning values are saved per profile, so Game can stay steady while Music can be much more sensitive for volume control.
- **Mouse speed:** how far the pointer moves for every detected sample when a movement direction is mapped. It is saved per profile, so a game can stay precise while another profile moves quickly.

Every row in **Action Mapping** can be rebound to any key, media key, left/right/middle mouse button, mouse movement direction, or short macro. The Game control labels are `turn-left` and `turn-right` (turn), `go` (walk), `stamp` (fire), `flip` (run), and `scrub-straight` (use/door).

For the **Music** profile, those same rows default to media controls: `turn-left` lowers volume, `turn-right` raises volume, `go` goes to the previous track, `stamp` toggles play/pause, `flip` mutes, and `scrub-straight` skips to the next track. Volume up/down repeat while the twist is held, so a held twist acts more like a knob than a single click.

## A note on "more axes"

You will notice the Game profile has no strafe and no separate forward/back/left/right movement, just turn and go. That is not a missing feature; it is the hard limit of the hardware. A round cap with a gyro and accelerometer but **no magnetometer** cannot know which way it is pointing, so directional movement (push-left-go-left) is physically impossible to do reliably. It is the same reason the cap's official games each use a single axis of motion: ask this device to track two things at once and the signals smear into noise. The tank scheme is what is achievable, and it is enough to finish a game. The full explanation is in [how-it-works.md](how-it-works.md).

## Control, safety, and the panic switch

- The big **TURN CONTROL ON / OFF** button is the master switch. Keyboard and mouse input only reaches your system while control is ON. Turn it OFF before setting the cap down.
- Clicking **X** hides the window but leaves the app running by the clock. A notification explains this. Choose **Disable control** from the tray to stop input immediately.
- The red **Quit** button in the app closes TRIKI Control completely. The tray also has a separate **Quit** command.
- Losing the Bluetooth connection turns control OFF and releases held keys or mouse buttons. Every fresh launch also starts with control OFF.
- Bindings and the active profile are saved to one JSON config file. On Windows it is `%APPDATA%\TRIKI\config.json`.
