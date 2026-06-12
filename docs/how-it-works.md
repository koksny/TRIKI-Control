# How it works — or, why a bottle cap can't be a joystick

This is the long answer to the question everyone asks first: *"why can't I just push it the way I want to go?"* Read it if that bugs you — the limitation shapes every control in the app, and the workaround is where it gets interesting.

## What's actually inside the cap

The cap streams a **6-axis IMU** over a Nordic-UART Bluetooth service: three channels of **gyroscope** (angular rate — how fast it's spinning around each axis) and three channels of **accelerometer** (the force on it — gravity, plus whatever you're doing to it). At rest, lying flat, gravity reads roughly `(24, 0, -2050)` in raw units, with the heavy `-2050` being "down."

That's it. Two sensors. There is **no magnetometer** (no compass), no marked front, no buttons. The cap is a featureless round disc.

## The unobservable degree of freedom

Here's the trap. With a gyroscope you can measure *how fast* the cap is rotating, and you might think you can just add that up over time to know its orientation. You can — for tilt. Gravity is a constant, ever-present reference, so the accelerometer always tells you which way is down, and you can always recover **pitch and roll** (how far, and which way, the cap is leaning).

But the rotation *around the gravity axis* — **yaw**, the heading, the compass direction the cap faces — has **no reference**. Gravity points straight down whether the cap faces north or south, so the accelerometer is blind to yaw. The only thing that sees yaw is the gyroscope, and a gyroscope only measures *rate*. To turn rate into an angle you integrate, and integration accumulates the sensor's tiny bias error without bound. Within seconds the heading estimate has drifted somewhere random and there is nothing — no gravity, no compass — to pull it back.

This is not a tuning problem. It is a **fundamental observability limit**: with a 6-axis IMU and no magnetometer, absolute heading is unrecoverable. A phone solves it with its magnetometer. A game controller solves it with a magnetometer (and recentre buttons). The cap has neither.

So: **the app can always tell you the cap is twisting, and how far it's tilted — but never which way in the world it is pointing.**

## Why that kills a normal joystick

A normal "push-left-go-left" control needs a world-stable frame: it has to agree with you on where "left" is. But "left" relative to a round cap depends entirely on the cap's heading — which we just established is unknowable and constantly drifting. Worse, the cap's natural use is to be *turned continuously in your hand*, so even if you pinned down "forward" for an instant, it would be wrong a moment later.

I tried several ways around this — integrating yaw with aggressive bias correction, anchoring "forward" at the moment a gesture starts, treating the first motion of each stroke as a reference direction. They all feel fine for about two seconds and then the drift wins. There is no honest fix without a third sensor. (If you ever open the cap and glue a small magnet to a known spot, a magnetometer-style heading becomes possible — but that's hardware surgery, not software.)

## The way out: only use heading-free signals

If heading is poisoned, **don't use heading.** Every control in the Game profile is built from a quantity that is invariant to how the cap is turned:

| Control | Signal it's built from | Why it's heading-free |
| --- | --- | --- |
| **Turn left / right** | twist rate about the gravity axis (gyro projected onto "down") | a twist is a *rate*; its sign (left vs right) is well-defined without knowing the absolute heading |
| **Walk forward** | tilt *magnitude* — how far gravity has tipped off the cap's vertical axis | "how far it's leaning" needs no compass; the *direction* of the lean (which we can't trust) is simply ignored |
| **Fire** | a sharp spike of acceleration *along the gravity axis* (a vertical impact) | a downward tap is the same impact whichever way the cap faces |
| **Run** | the cap resting upside-down (gravity flipped to the other pole) | an orientation *state*, not a direction |
| **Use / open door** | a flat translation across the desk with little rotation and no vertical impact | detected as "moving while staying flat," independent of which way it slides |

Because none of these reference a world direction, they keep working no matter how the cap is spun. The cost is the one you feel: you **steer with twist and move with tilt as separate actions**, like a tank. That's not a stylistic choice — it's the shape of what's left after you remove the unobservable axis.

## The tuning, honestly

Getting those five signals to fire cleanly through Bluetooth noise and dropouts ate most of the engineering. A few of the lessons, in case you go digging in `triki_motion_engine.py`:

- **One motion = one action.** The raw signals flicker across thresholds many times per gesture. A per-sample classifier fires a hundred contradictory events for a single twist. The engine instead *locks* a single label for the duration of a gesture and only lets a clearly different, sustained motion preempt it.
- **The neutral has to find itself, and it has to be un-poisonable.** There's no calibration step — the app learns the resting pose live. The hard part was a deadlock: a bad first Bluetooth packet (they happen) could seed the resting estimate wrong, and the very error it created blocked the relock that would have fixed it, leaving the cap reading a permanent phantom slide. The fix is a median bootstrap over the first second and re-locking against a *gravity-independent* flatness measure.
- **Bluetooth is bursty.** Packets arrive in clumps with gaps up to ~100 ms and the occasional multi-second dropout (often: a finger over the antenna during a tilt). Keys are held a little past the last packet to bridge the gaps, and a reconnect *preserves* the learned calibration instead of re-running the dead-time-inducing startup.
- **Telling a tap from a tilt from a slide is all about which axis the energy is on.** A fire-tap is a spike *along gravity*; a slide is motion *across* the flat plane with the cap staying level; a walk is a *sustained* tilt. Each control gates on the axis and the persistence that uniquely identify it — which is why a clean, square tap fires and a twisty one reads as a turn.

It is not a magnetometer. But it plays Doom, start to finish, with a bottle cap. Given two sensors, that'll do.
