"""Body-frame, zero-calibration control engine ("Motion engine"), v2.

Rewritten and tuned against the maintainer's real calibration recordings (the
known sequence: 10x turn-left, 10x turn-right, 10x stamp, 10x scrub-left, 10x
scrub-right, 10x tilt-left, 10x tilt-right, 10x tilt-forward, 10x tilt-back,
10x flip). Every distinct physical cap motion has its OWN clean raw-IMU signature
and maps to ONE control; the engine emits NOTHING while the cap is still.

Raw sample axes: ``values = [gyro_a, gyro_b, gyro_c, acc_d, acc_e, acc_f]``.
At rest gravity reads ~ ``(24, 0, -2050)`` and the gyro carries a small bias
(~tens of units) that this engine measures and subtracts, so a still cap is
genuinely idle (no phantom output -- the #1 bug in v1).

Signatures (validated on the logs):

* TURN  -- a flat in-place TWIST: large rotation about the vertical (gravity)
  axis with the cap NOT leaned. ``turn = dot(gyro - bias, unit(gravity))``;
  the sign is the direction. (Re-mapped: in v1 the in-game turn was inverted, so
  ``invert_turn`` defaults True.)
* TILT  -- a HELD lean. Gravity rocks into the cap's own d/e plane:
  ``lean_d = g[0]-gref[0]`` (sideways) and ``lean_e = g[1]-gref[1]`` (fwd/back)
  are TWO INDEPENDENT axes -- a forward-left lean drives BOTH tilt-forward and
  tilt-left (no "dominant axis" winner-take-all). Tilt is body-frame STATE, so it
  engages while leaned and holds.
* STAMP -- a vertical set-down IMPACT: a sharp spike in ``accdev = ||accel|-G||``,
  brief, while not leaned/twisting. One-shot (refractory) -> a single fire.
* FLIP  -- the cap turned UPSIDE DOWN: the vertical gravity component ``g[2]``
  flips sign. While inverted the engine emits ``flip`` every sample so a held key
  (Shift) stays down -- "flip on = Shift on, flip off = Shift off".
* SCRUB -- a circular STIR: a sustained twist WITH a lean present (the cap orbits/
  leans as it spins), as opposed to TURN (flat) or TILT (no twist). Sign = stir
  direction.

The engine keeps the ``add_sample(elapsed_seconds, sample) -> GesturePrediction |
None`` / ``reset()`` contract (drop-in for the BLE loop). Movement intents are
RE-EMITTED every active sample so ``HoldKeyEmitter`` holds the bound key while the
motion persists; when BOTH tilt axes are active they are emitted on alternating
samples so both bound keys stay held at once (true independent axes). STAMP is a
one-shot tap. All thresholds are ``__init__`` kwargs tuned from the real logs; the
round cap has no marked front, so per-axis ``invert_*`` flags let the maintainer
flip any direction.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Callable

from triki_classifier import (
    GRAVITY_UNITS,
    MotionFeatures,
)
from triki_gestures import MOTION_LABELS
from triki_protocol import MotionSample


# Intent -> first-class control label. Every emitted label is a real MOTION_LABEL.
TURN_LEFT_LABEL = "turn-left"
TURN_RIGHT_LABEL = "turn-right"
GO_LABEL = "go"                       # tilt the cap any way -> go forward (throttle)
STAMP_LABEL = "stamp"
FLIP_LABEL = "flip"
SCRUB_STRAIGHT_LABEL = "scrub-straight"
# scrub-circular was dropped (a round 6-axis cap can't reliably tell a circle from a
# line). The constant is kept ONLY so old aliases/configs that reference it still
# import; it is never emitted and is not a MOTION_LABEL.
SCRUB_CIRCULAR_LABEL = "scrub-circular"

# Back-compat aliases for importers/tests that still reference the old names. The
# old 4-way tilt is gone (heading is unobservable), so all the directional move
# labels collapse onto the single rotation-invariant GO.
TILT_FORWARD_LABEL = GO_LABEL
TILT_BACK_LABEL = GO_LABEL
TILT_LEFT_LABEL = GO_LABEL
TILT_RIGHT_LABEL = GO_LABEL
MOVE_FORWARD_LABEL = GO_LABEL
MOVE_BACKWARD_LABEL = GO_LABEL
MOVE_STRAFE_LEFT_LABEL = GO_LABEL
MOVE_STRAFE_RIGHT_LABEL = GO_LABEL
SCRUB_LEFT_LABEL = SCRUB_CIRCULAR_LABEL
SCRUB_RIGHT_LABEL = SCRUB_STRAIGHT_LABEL
FIRE_LABEL = STAMP_LABEL

# The classifier gesture label a STAMP set-down produces, kept ONLY as an optional
# cross-check; the primary stamp detector is the raw vertical-impact rule below.
STAMP_CLASSIFIER_LABEL = "lift"

assert {
    TURN_LEFT_LABEL, TURN_RIGHT_LABEL, GO_LABEL,
    STAMP_LABEL, FLIP_LABEL, SCRUB_STRAIGHT_LABEL,
}.issubset(set(MOTION_LABELS))


def _vdot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _vnorm(a) -> float:
    return math.sqrt(_vdot(a, a))


def _vunit(a):
    n = _vnorm(a)
    if n <= 1e-9:
        return (0.0, 0.0, 0.0)
    return (a[0] / n, a[1] / n, a[2] / n)


def _median(xs) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    m = n // 2
    return s[m] if n % 2 else 0.5 * (s[m - 1] + s[m])


class MotionControlEngine:
    """Causal streaming engine: TWIST=turn, LEAN=tilt (two independent axes),
    IMPACT=stamp, INVERT=flip, STIR=scrub. Zero calibration; idle when still."""

    def __init__(
        self,
        *,
        alpha_g: float = 0.25,        # gravity / lean EMA
        alpha_bias: float = 0.05,     # gyro-bias relock rate (when still)
        # stillness (for neutral + bias relock, and the idle guarantee)
        still_gyro: float = 220.0,    # |gyro - bias| below this == still
        still_acc: float = 90.0,      # accdev below this == still
        relock_seconds: float = 0.2,
        af_ref: float = 0.12,         # neutral-gravity relock rate (when still)
        # TILT (held lean) -> GO. Gravity units (rest |g| ~2050; ~320 == 9 deg).
        # Lower than before so GO engages sooner (the maintainer felt a tilt lag);
        # TURN now requires the cap BELOW lean_off (truly flat) so a tilt can't fire
        # a turn.
        lean_on: float = 270.0,         # LOWERED so a gentle forward tilt engages GO
        lean_off: float = 160.0,        # sooner (less "tilt catches after a beat").
        # TURN: a flat in-place twist (gyro about the vertical), needs real spin.
        # Defaults are deliberately NOT too sensitive (the maintainer found the cap
        # turned too easily/fast); tune live with set_turn_sensitivity() / the slider.
        twist_on: float = 1000.0,       # == turn-sensitivity 50, the value the user
        twist_off: float = 690.0,       # settled on (79 was too touchy -> ghost turns).
        turn_spin_on: float = 800.0,    # The axis_frac gate + GO-before-TURN keep a
                                        # tilt from firing a turn. twist_off is HIGH
                                        # (0.69x) so a turn RELEASES quickly when you
                                        # stop twisting (no over-rotation).
        turn_axis_frac: float = 0.55,   # ...AND the rotation must be mostly about the
                                        # VERTICAL axis: |twist| >= this * spin. A real
                                        # twist spins ~entirely about vertical (ratio
                                        # ~0.8); a TILT (go) spins about a HORIZONTAL
                                        # axis so its small vertical projection stays
                                        # below this -> a tilt-to-go no longer
                                        # occasionally fires turn-right.
        # SCRUB: a FLAT translation of the cap on the desk (gravity-removed linear
        # accel), with LOW spin (not a twist). Circular vs straight is told apart by
        # whether the linear-accel direction rotates (circle) or stays on one line.
        # GO vs SCRUB is decided by whether the cap is TILTED (gravity units off the
        # vertical, from the f-axis). A flat slide (scrub) keeps the cap flat; tilting
        # it past this == GO. ~320 == ~9 deg.
        go_tilt_on: float = 200.0,
        go_tilt_sustain: int = 2,     # the tilt must HOLD this many samples to count
                                      # as GO -- a flat slide (scrub) bobs the f-axis
                                      # only in brief jerks, which never sustain.
        # Horizontal-motion thresholds. There is ONE scrub control: a deliberate flat
        # SLIDE on the desk (circular was dropped -- a round 6-axis cap can't reliably
        # tell a circle from a line). A slide must be CLEAN: little rotation (the cap
        # doesn't spin) and no vertical impact (it isn't a stamp). These gates reject
        # the phantom slides (incidental in-air motion / impact-tails) that kept
        # opening the menu.
        scrub_window_s: float = 0.45,
        scrub_spin_max: float = 800.0,   # a slide barely rotates the cap; above this
                                         # it is in-air handling, not a desk slide
        scrub_accdev_max: float = 450.0, # a slide is flat-steady; above this it is a
                                         # vertical impact (a stamp), not a slide
        # STAMP (a sharp VERTICAL set-down impact). Keyed on the deviation ALONG the
        # gravity axis (vert), NOT |accel| magnitude: a horizontal desk SLIDE inflates
        # |accel| too (so the old accdev gate let a hard door-slide fire a phantom
        # stamp), but its deviation along vertical stays ~0. Plus LITTLE rotation (low
        # spin) and no steering (low twist) to separate it from a GO/TURN.
        stamp_vert_min: float = 380.0,  # LOWERED: most real stamps peaked 400-1400 but
                                        # only ~1% topped 650, so most taps were missed
                                        # (and the tap's tilt fired GO instead).
        stamp_flat_max: float = 550.0,  # stamp only when the cap is roughly FLAT: a
                                        # held tilt bobs and throws vert spikes that
                                        # else fire a ghost stamp mid-drive.
        stamp_spin_max: float = 800.0,
        stamp_refractory_seconds: float = 0.45,
        stamp_go_lockout: float = 0.35,  # suppress GO for this long AFTER a stamp: the
                                        # lift-back-up after a tap re-tilts the cap and
                                        # would else fire a phantom forward-walk per shot.
        impact_freeze_accdev: float = 400.0,  # accel deviation above this == a vertical
                                        # IMPACT (a stamp): freeze the gravity EMA so the
                                        # spike can't tip g and fire a phantom GO after.
        # FLIP (orientation): SUSTAINED, STEADY inversion only (a stamp impact must
        # not register as a flip), on the smoothed vertical gravity g[2].
        flip_on: float = 1200.0,      # g[2] above +flip_on -> inverted
        flip_off: float = -1200.0,    # (kept for compat; unused by the new logic)
        flip_spin_max: float = 400.0, # flip needs the cap RESTING (spin below this)
        flip_sustain_seconds: float = 0.30,
        # SETTLE: emit NOTHING until the neutral/bias have had time to establish at
        # connect, so the cap can be picked up without spraying ghost actions.
        settle_seconds: float = 1.2,
        hold_seconds: float = 0.05,   # brief sustain to engage (anti-flicker)
        commit_seconds: float = 0.12, # min dwell on a movement before it can switch
        engage_samples: int = 3,      # a movement must persist this many samples to
                                      # START a gesture (kills sub-blip rebounds)
        scrub_engage_samples: int = 4,   # SCRUB sensitivity. It used to need 10 (a long
                                      # hold) to suppress menu-opening ghosts, but the
                                      # single scrub now maps to "use" (Space) -- a
                                      # stray fire is harmless -- so bias to RESPONSIVE
                                      # instead: 4 makes a door-slide register ~8x more
                                      # often (the "had to stand at the door" fix)
        release_samples: int = 3,     # idle samples that END a locked gesture -- kept
                                      # small so the key releases fast when you stop
                                      # (the turn-lag the maintainer felt was here +
                                      # the key-hold time)
        # Reject corrupt/dropped BLE packets: gravity is ALWAYS present, so a real
        # motion sample's |accel| is near GRAVITY (never ~0, never absurdly high). A
        # glitch packet (|accel| outside this band) would otherwise fire a ghost
        # stamp (accdev ~ gravity) and poison the gravity estimate -> ghost flip/move.
        accel_min: float = 800.0,
        accel_max: float = 8000.0,
        gravity_units: float = GRAVITY_UNITS,
        # direction preferences (round cap, no marked front)
        invert_turn: bool = True,     # v1 in-game turn was inverted
        invert_fwd: bool = False,
        invert_strafe: bool = False,
        swap_tilt_axes: bool = False,
        fire_enabled: bool = True,
        observer: Callable[[dict], None] | None = None,
    ) -> None:
        self.alpha_g = alpha_g
        self.alpha_bias = alpha_bias
        self.still_gyro = still_gyro
        self.still_acc = still_acc
        self.relock_seconds = relock_seconds
        self.af_ref = af_ref
        self.lean_on = lean_on
        self.lean_off = lean_off
        self.twist_on = twist_on
        self.twist_off = twist_off
        self.turn_spin_on = turn_spin_on
        self.turn_axis_frac = turn_axis_frac
        self.go_tilt_on = go_tilt_on
        self.go_tilt_sustain = go_tilt_sustain
        self.scrub_window_s = scrub_window_s
        self.scrub_spin_max = scrub_spin_max
        self.scrub_accdev_max = scrub_accdev_max
        self.stamp_spin_max = stamp_spin_max
        self.stamp_go_lockout = stamp_go_lockout
        self.stamp_vert_min = stamp_vert_min
        self.stamp_flat_max = stamp_flat_max
        self.impact_freeze_accdev = impact_freeze_accdev
        self.stamp_refractory_seconds = stamp_refractory_seconds
        self.flip_on = flip_on
        self.flip_off = flip_off
        self.flip_spin_max = flip_spin_max
        self.flip_sustain_seconds = flip_sustain_seconds
        self.settle_seconds = settle_seconds
        self.hold_seconds = hold_seconds
        self.commit_seconds = commit_seconds
        self.engage_samples = engage_samples
        self.scrub_engage_samples = scrub_engage_samples
        self.release_samples = release_samples
        self.accel_min = accel_min
        self.accel_max = accel_max
        self.gravity_units = gravity_units
        self.invert_turn = invert_turn
        self.invert_fwd = invert_fwd
        self.invert_strafe = invert_strafe
        self.swap_tilt_axes = swap_tilt_axes
        self.fire_enabled = bool(fire_enabled)
        self._observer = observer
        self._reset_state()

    # ``tilt_on``/``tilt_off`` are surfaced to the UI lean-threshold control in
    # DEGREES (the input is a 3..30 deg field); internally the engine gates on the
    # gravity-unit lean magnitude, so the properties convert deg <-> units.
    def _deg_to_units(self, deg: float) -> float:
        return self.gravity_units * math.sin(math.radians(max(0.0, min(89.0, float(deg)))))

    def _units_to_deg(self, units: float) -> float:
        return math.degrees(math.asin(max(0.0, min(1.0, units / self.gravity_units))))

    @property
    def tilt_on(self) -> float:
        return round(self._units_to_deg(self.lean_on), 1)

    @tilt_on.setter
    def tilt_on(self, value: float) -> None:
        self.lean_on = self._deg_to_units(value)
        self.lean_off = self.lean_on * 0.6  # keep the ~0.6 engage/release ratio

    @property
    def tilt_off(self) -> float:
        return round(self._units_to_deg(self.lean_off), 1)

    @tilt_off.setter
    def tilt_off(self, value: float) -> None:
        self.lean_off = self._deg_to_units(value)

    # Turn threshold is the vertical-twist magnitude that starts a turn. It is
    # separate from sensitivity so Music can use a lower knob threshold without
    # changing the Game steering defaults.
    @property
    def turn_threshold(self) -> float:
        return round(max(400.0, min(1600.0, float(self.twist_on))), 0)

    def set_turn_threshold(self, value) -> None:
        value = max(400.0, min(1600.0, float(value)))
        self.twist_on = value
        self.twist_off = self.twist_on * 0.69    # release quickly (no over-rotation)

    # Turn sensitivity 0..100 (the Advanced slider): higher % == a lower total-spin
    # gate and a more forgiving vertical-axis ratio. It does NOT rewrite the twist
    # threshold; that is the separate turn_threshold slider.
    @property
    def turn_sensitivity(self) -> float:
        return round(max(0.0, min(100.0, (1300.0 - self.turn_spin_on) / 10.0)), 0)

    def set_turn_sensitivity(self, pct) -> None:
        pct = max(0.0, min(100.0, float(pct)))
        self.turn_spin_on = 1300.0 - pct * 10.0  # 0% -> 1300, 100% -> 300
        self.turn_axis_frac = 0.75 - pct * 0.004 # 50% -> 0.55 (current Game default)

    def _reset_state(self) -> None:
        self._g: list[float] | None = None
        self._gref: list[float] | None = None
        self._gbias: list[float] | None = None
        self._first_t: float | None = None
        self._still_since: float | None = None
        self._flip_since: float | None = None
        self._flipped = False
        # hysteresis engage flags
        self._turning = 0     # -1 / +1 / 0 turn direction
        self._going = False   # cap tilted past threshold (GO engaged)
        self._last_stamp_t: float | None = None
        self._last_go_t: float | None = None   # for the stamp-after-GO lockout
        self._stamp_armed = True
        # gesture label lock (one motion = one action) + sustain/release counters
        self._gesture_label: str | None = None
        self._idle_n = 0
        self._cand_label: str | None = None
        self._cand_n = 0
        self._go_tilt_n = 0   # consecutive samples the cap has been clearly tilted
        # robust connect-time bootstrap of the rest bias/neutral (median over settle)
        self._boot_gyro: list[tuple[float, float, float]] = []
        self._boot_acc: list[tuple[float, float, float]] = []
        self._booted = False
        # rolling buffer of the horizontal MOTION vector (raw accel - rest) over the
        # window -- its PATTERN (spread / line / line+rotation) classifies the control.
        self._rh_buf: deque[tuple[float, float, float]] = deque()
        self._active = False
        # diagnostics surfaced to the live panel / viz
        self._last_hd = 0.0
        self._last_he = 0.0
        self._last_tilt = 0.0
        self._last_twist = 0.0
        self._last_fire = False
        self._last_direction = "idle"

    def reset(self) -> None:
        # PRESERVE a good calibration across a reset so a BLE RECONNECT doesn't re-pay
        # the cold-start cost on every drop. The play loop resets + re-bases time on
        # each reconnect; a cold start would re-impose the ~1.2s settle dead-zone AND a
        # fresh median bootstrap EACH time -> seconds of "ignored" input per drop (and
        # the cap drops a lot -- e.g. a finger over the antenna during a tilt). The
        # cap's gyro bias / accel neutral do NOT change across a brief drop, so we keep
        # _g/_gref/_gbias/_booted and leave _first_t = None: both the settle gate and
        # the bootstrap are then skipped, so the engine emits IMMEDIATELY on reconnect
        # (the always-on relock still re-trims the neutral if the pose shifted). A cold
        # engine (never booted) still settles normally on the very first connect.
        g, gref, gbias, booted = self._g, self._gref, self._gbias, self._booted
        self._reset_state()
        if booted and g is not None and gref is not None and gbias is not None:
            self._g = list(g)
            self._gref = list(gref)
            self._gbias = list(gbias)
            self._booted = True

    def add_sample(self, elapsed_seconds: float, sample: MotionSample):
        from triki_classifier import GesturePrediction  # local: avoid import cycle

        v = sample.values
        gyro = (float(v[0]), float(v[1]), float(v[2]))
        acc = (float(v[3]), float(v[4]), float(v[5]))

        # ---- GLITCH GUARD: drop corrupt/dropped BLE packets (|accel| not near
        # gravity). Skip ENTIRELY -- don't seed/update gravity/neutral/bias and emit
        # nothing -- so a junk packet can't fire a ghost stamp/flip/move or poison
        # the running estimates. ----
        acc_mag = _vnorm(acc)
        if acc_mag < self.accel_min or acc_mag > self.accel_max:
            return None

        if self._g is None:
            self._g = [acc[0], acc[1], acc[2]]
            self._gref = [acc[0], acc[1], acc[2]]
            self._gbias = [gyro[0], gyro[1], gyro[2]]
            self._first_t = elapsed_seconds
        assert self._g is not None and self._gref is not None and self._gbias is not None

        # ---- ROBUST CONNECT BOOTSTRAP: a SINGLE first sample can be an OUTLIER (a
        # resumed-after-drop packet, or the cap mid-handling at connect). Seeding the
        # gyro bias / accel neutral from it poisons EVERY downstream estimate -- e.g. a
        # one-off gyro spike makes the bias wrong, so spin stays inflated, so the cap
        # never reads "still", so the relock that would fix the bias never runs: a
        # DEADLOCK that leaves the cap reading a permanent phantom slide. So across the
        # settle window we gather samples and re-seed bias/neutral from their per-axis
        # MEDIAN (outlier-proof). The true rest values are very stable, so the median
        # nails them and self-corrects a bad first packet. ----
        if not self._booted:
            self._boot_gyro.append(gyro)
            self._boot_acc.append(acc)
            if (elapsed_seconds - self._first_t) >= self.settle_seconds and len(self._boot_gyro) >= 5:
                self._gbias = [_median([s[i] for s in self._boot_gyro]) for i in range(3)]
                self._gref = [_median([s[i] for s in self._boot_acc]) for i in range(3)]
                self._g = list(self._gref)
                self._boot_gyro = []
                self._boot_acc = []
                self._booted = True

        accdev = abs(acc_mag - self.gravity_units)
        # FREEZE the gravity estimate during a vertical IMPACT (a stamp). An impact is
        # a big momentary accel spike that is NOT gravity; folding it into the smoothed
        # g would tip g[2] -> f_tilt spikes for a few samples AFTER the impact -> a
        # phantom GO right after every STAMP ("stamp gives forward movement"). The cap's
        # true orientation can't change in one impact sample, so holding g across it
        # both kills that ghost AND keeps the tilt signal clean.
        a = self.alpha_g if accdev < self.impact_freeze_accdev else 0.0
        self._g = [(1.0 - a) * self._g[i] + a * acc[i] for i in range(3)]
        g_hat = _vunit((self._g[0], self._g[1], self._g[2]))

        gyro_corr = (
            gyro[0] - self._gbias[0],
            gyro[1] - self._gbias[1],
            gyro[2] - self._gbias[2],
        )
        spin = _vnorm(gyro_corr)
        # VERTICAL impact: deviation of the accel ALONG the gravity axis from rest.
        # A STAMP (set-down) spikes this; a horizontal SLIDE inflates |accel| (accdev)
        # but barely touches the vertical projection -> this is what tells a real
        # stamp from a hard door-slide (which used to fire a phantom stamp).
        vert = abs(abs(_vdot(acc, g_hat)) - self.gravity_units)
        # TWIST about the vertical (gravity) axis, bias-removed.
        twist = _vdot(gyro_corr, g_hat)

        # The cap's horizontal MOTION signal: the RAW accel deviation from the rest
        # pose in the d/e plane. This is INSTANTANEOUS (no EMA lag -> responsive,
        # fixes the tilt-lag) and captures every horizontal control the same way --
        # a tilt (gravity tips into the plane) AND a slide (linear accel). The three
        # are then told apart purely by the PATTERN of this vector over the window:
        #   direction SPREAD (sweeps round)  -> SCRUB circular ("O")
        #   one LINE, no net rotation        -> SCRUB straight ("I"/"-")
        #   one LINE, net rotation           -> GO (tilt/push to drive)
        rh_d = acc[0] - self._gref[0]
        rh_e = acc[1] - self._gref[1]
        rh_mag = math.hypot(rh_d, rh_e)
        tilt_deg = math.degrees(math.asin(max(0.0, min(1.0, rh_mag / self.gravity_units))))
        self._rh_buf.append((elapsed_seconds, rh_d, rh_e))
        while self._rh_buf and elapsed_seconds - self._rh_buf[0][0] > self.scrub_window_s:
            self._rh_buf.popleft()

        # TRUE tilt of the cap, read from the VERTICAL gravity axis (the smoothed f):
        # when the cap is flat f == -gravity so this is 0; when it ANGLES, the vertical
        # gravity shrinks and this grows. A flat slide (scrub) keeps the cap flat so
        # f_tilt stays ~0 -- this is what separates a tilt-to-GO from a flat SCRUB,
        # exactly as the maintainer described (scrub keeps the cap flat on the desk).
        gz = self._g[2]
        f_tilt = math.sqrt(max(0.0, self.gravity_units * self.gravity_units - gz * gz))
        # Tilt -> GO via a LEAKY/STICKY counter: +1 while tilted, but only DECAY (not
        # hard-reset) on a dip, capped a few above the engage point. The smoothed f_tilt
        # is NOISY during the tilt MOTION (it bounces across the threshold), so the old
        # reset-on-dip kept zeroing the counter and delayed GO until the cap SETTLED --
        # the "tilt only fires when I lower it" bug. Decaying instead of resetting lets a
        # real (mostly-tilted) push stay engaged through the noise and fire at the START,
        # while a flat slide's brief, sparse f-jerk still can't reach the sustain count.
        if f_tilt >= self.go_tilt_on:
            self._go_tilt_n = min(self._go_tilt_n + 1, self.go_tilt_sustain + 4)
        else:
            self._go_tilt_n = max(self._go_tilt_n - 1, 0)
        tilted = self._go_tilt_n >= self.go_tilt_sustain
        # GO lockout right AFTER a STAMP: lifting the cap back up after the tap re-tilts
        # it, so f_tilt climbs again and fired a phantom GO a few samples after every
        # shot ("when I stamp I sometimes walk forward"). Zeroing the counter only AT the
        # stamp wasn't enough -- the recovery re-accumulates it -- so we hold it cleared
        # for a short window after the stamp.
        if (self._last_stamp_t is not None
                and (elapsed_seconds - self._last_stamp_t) < self.stamp_go_lockout):
            self._go_tilt_n = 0
            tilted = False

        # ---- FLIP: a RESTING-inverted STATE. Flipped ONLY while the cap is upside
        # down AND genuinely at rest there (low linear accel AND LOW SPIN), sustained
        # ~0.3 s. It does NOT latch: the instant the cap is moved/spun (spin up) or
        # turned upright it un-flips. A vigorous TURN tips the cap over with HUGE spin
        # but is not a rest, so it can never register as a flip -- the exact bug the
        # maintainer saw (dozens of flips per turn). ----
        inverted_rest = (
            self._g[2] >= self.flip_on
            and accdev < self.still_acc
            and spin < self.flip_spin_max
        )
        if inverted_rest:
            if self._flip_since is None:
                self._flip_since = elapsed_seconds
            if elapsed_seconds - self._flip_since >= self.flip_sustain_seconds:
                self._flipped = True
        else:
            self._flip_since = None
            self._flipped = False

        # ---- stillness: relock the gyro bias whenever the cap is not moving, and
        # relock the d/e NEUTRAL only when the cap is also FLAT. Relocking neutral
        # during a HELD tilt (which is "still": low gyro, |accel|~G) would learn the
        # tilted pose as rest -> the tilt fades and rebounds as a ghost lean. That
        # was the core ghost/under-detection bug; gating the neutral relock on
        # flatness fixes it. ----
        motion_still = (spin < self.still_gyro) and (accdev < self.still_acc)
        if motion_still:
            if self._still_since is None:
                self._still_since = elapsed_seconds
            if elapsed_seconds - self._still_since >= self.relock_seconds:
                f = self.af_ref
                # Neutral relock: only while right-side up AND genuinely FLAT. Flatness
                # is read from the VERTICAL gravity axis (f_tilt), NOT from horiz: horiz
                # is measured against gref, so a gref seeded wrong at connect (cap not
                # perfectly flat/still that instant) makes horiz perpetually large and
                # DEADLOCKS the very relock meant to fix it -> rh_mag stuck high -> the
                # cap reads as a permanent slide ("constantly Slide"). f_tilt is
                # gref-independent, so a flat-and-still cap ALWAYS re-zeroes the neutral.
                if (not self._flipped) and f_tilt < self.lean_off:
                    self._gref[0] = (1.0 - f) * self._gref[0] + f * acc[0]
                    self._gref[1] = (1.0 - f) * self._gref[1] + f * acc[1]
                fb = self.alpha_bias
                self._gbias = [
                    (1.0 - fb) * self._gbias[i] + fb * gyro[i] for i in range(3)
                ]
        else:
            self._still_since = None

        label, intent, reason, fire = self._decide(
            elapsed_seconds, twist, spin, rh_mag, tilted, accdev, vert, f_tilt
        )
        if label == GO_LABEL:
            self._last_go_t = elapsed_seconds   # arm the stamp lockout (no stamp mid-walk)

        self._last_hd = rh_d
        self._last_he = rh_e
        self._last_tilt = tilt_deg
        self._last_twist = twist
        self._last_fire = fire
        self._last_direction = intent

        features = self._build_features(tilt_deg, twist, spin, accdev, rh_mag, rh_d, rh_e)
        result = None
        if label is not None:
            result = GesturePrediction(label=label, confidence=1.0, reason=reason, features=features)

        if self._observer is not None:
            try:
                self._observer({
                    "t": round(elapsed_seconds, 4),
                    "values": list(v),
                    "intent": intent,
                    "twist": round(twist, 1),
                    "yaw": round(twist, 1),
                    "tilt": round(tilt_deg, 2),
                    "spin": round(spin, 1),
                    "accdev": round(accdev, 1),
                    "flipped": self._flipped,
                    "active_key": label,
                    "hd": round(rh_d, 1),
                    "he": round(rh_e, 1),
                    "fire": fire,
                    "direction": intent,
                })
            except Exception:
                pass

        return result

    def _candidate(self, twist, spin, rh_mag, tilted, accdev):
        """The raw control this ONE sample looks like, with TURN given PRIORITY.
        A TURN is a twist about the vertical axis (a GYRO signal -- a rotation),
        physically distinct from a tilt/slide (an ACCEL signal). So it is tested
        FIRST and wins even while the cap is tilted or moving -- otherwise you can
        never steer while walking (the exact "can't turn" regression). Only when
        there is NO turn-grade twist do we read the horizontal accel: a sustained
        tilt -> GO, an un-tilted clean flat motion -> SCRUB (one bind: a slide)."""
        active_thr = self.lean_off if self._active else self.lean_on
        self._active = rh_mag >= active_thr
        # GO is checked BEFORE turn: a TILTED cap is driving forward, even if the tilt's
        # rotation also carries a little twist. Checking tilt first stops the tilt-onset
        # from flicking a TURN before settling into GO (the "tilt sometimes turns right"
        # bug). Tank scheme: you tilt to go OR twist (flat) to steer -- not both at once.
        if tilted and self._active:
            self._turning = 0
            return (GO_LABEL, "go", "cap tilted -> go forward")
        # TURN: a flat twist that is strong AND mostly about the VERTICAL axis
        # (|twist| >= turn_axis_frac * spin -- a tilt's rotation is about a HORIZONTAL
        # axis so its vertical projection stays a small fraction of spin).
        twist_thr = self.twist_off if self._turning else self.twist_on
        if (spin >= self.turn_spin_on and abs(twist) >= twist_thr
                and abs(twist) >= self.turn_axis_frac * spin):
            self._turning = 1 if twist > 0 else -1
            return self._turn_labels(twist)
        self._turning = 0
        if not self._active:
            return None
        # SCRUB == a DELIBERATE flat SLIDE on the desk (the ONE scrub bind; circular
        # was dropped). It must be CLEAN: little rotation (low spin -- a slide doesn't
        # spin the cap) and no vertical impact (low accdev -- a slide isn't a stamp).
        # In-air handling and stamp impact-tails carry spin/accdev, so they fall
        # through to None instead of firing a phantom slide (the menu-opening ghosts).
        if spin < self.scrub_spin_max and accdev < self.scrub_accdev_max:
            return (SCRUB_STRAIGHT_LABEL, "scrub-straight", "flat deliberate slide")
        return None

    def _turn_labels(self, twist):
        right = (twist > 0)
        if self.invert_turn:
            right = not right
        if right:
            return TURN_RIGHT_LABEL, "turn-right", "flat twist about vertical (turn right)"
        return TURN_LEFT_LABEL, "turn-left", "flat twist about vertical (turn left)"

    def _rh_axial_R(self) -> float:
        """Axial concentration of the horizontal-motion directions over the window.
        ~1.0 == one LINE (a straight slide or a tilt/push); ~0.0 == the direction is
        SPREAD round (a circle). The doubled angle folds 180-degree-opposite
        directions together so a back-and-forth slide still reads as one line."""
        C = S = W = 0.0
        for _t, d, e in self._rh_buf:
            m = math.hypot(d, e)
            if m < 150.0:
                continue
            ang2 = 2.0 * math.atan2(e, d)
            C += m * math.cos(ang2)
            S += m * math.sin(ang2)
            W += m
        if W <= 1e-6:
            return 1.0
        return math.hypot(C, S) / W

    def _rh_rotation(self) -> float:
        """Net signed sweep (radians) of the horizontal-motion vector over the window.
        A circle sweeps round (large); a back-and-forth straight slide cancels (~0);
        a tilt/push that turns the wrist accumulates one way (large). Near-zero
        samples are skipped (no direction)."""
        total = 0.0
        prev = None
        for _t, d, e in self._rh_buf:
            if math.hypot(d, e) < 150.0:
                prev = None
                continue
            if prev is not None:
                pd, pe = prev
                total += math.atan2(pd * e - pe * d, pd * d + pe * e)
            prev = (d, e)
        return total

    def _decide(self, t, twist, spin, rh_mag, tilted, accdev, vert, f_tilt):
        """Return (label|None, intent, reason, stamp_fired). ONE rotation-INVARIANT
        control per sample (a round 6-axis cap has no observable heading):
          * cap TILTED (f-axis) + moving           -> GO (drive forward),
          * cap FLAT + horizontal motion, one LINE -> SCRUB (a desk slide -> use/door),
          * no horizontal motion, just a TWIST     -> TURN left/right,
          * vertical impact -> STAMP ; upside down -> FLIP.
        A gesture-label LOCK holds the one chosen control for the whole gesture, but a
        SCRUB upgrades to GO the moment the cap tilts (a lift that starts flat)."""
        # ---- SETTLE: nothing until the neutral/bias establish at connect. ----
        if self._first_t is not None and (t - self._first_t) < self.settle_seconds:
            self._gesture_label = self._cand_label = None
            self._cand_n = self._idle_n = 0
            self._turning = 0
            self._active = False
            return None, "settling", "establishing neutral at connect", False

        # ---- FLIP (exclusive, robust): cap upside down -> hold the bound key. ----
        if self._flipped:
            self._turning = 0
            self._active = False
            self._gesture_label = self._cand_label = None
            self._cand_n = self._idle_n = 0
            return FLIP_LABEL, "flip", "cap is upside down (flip -> shift)", False

        # ---- STAMP: a sharp VERTICAL set-down impact. Keyed on VERT (deviation along
        # the gravity axis) with LITTLE rotation (low spin) and no steering (low twist).
        # vert (not |accel| magnitude) is the key: a hard horizontal door-SLIDE inflates
        # |accel| but barely moves vert, so it no longer fires a phantom stamp; low spin
        # separates the flat down-stamp from a GO/TURN, which rotate the cap hard. ----
        ready = (
            self._last_stamp_t is None
            or (t - self._last_stamp_t) >= self.stamp_refractory_seconds
        )
        if vert < self.stamp_vert_min * 0.5:
            self._stamp_armed = True
        # A STAMP is a sharp vertical impact made with a roughly CLEAN, FLAT tap. vert is
        # the impact; spin<spin_max rejects the high-spin vert-spikes thrown by TURNS and
        # general cap-handling (the ghost stamps: median spin ~1840), and f_tilt rejects a
        # held tilt's bobbing. Dropping these gates fired stamps "almost non-stop"; the
        # earlier no-stamp-after-GO lockout (now gone) was the real miss-regression, NOT
        # these. The impact-freeze + go-counter reset still stop a phantom GO right after.
        if (self.fire_enabled
                and vert >= self.stamp_vert_min and spin < self.stamp_spin_max
                and f_tilt < self.stamp_flat_max
                and self._stamp_armed and ready):
            self._last_stamp_t = t
            self._stamp_armed = False
            self._go_tilt_n = 0   # drop any tilt the impact built up -> no GO after a stamp
            return STAMP_LABEL, "stamp", "vertical set-down impact (stamp)", True

        # ---- This sample's RAW candidate, with TURN given PRIORITY (see _candidate).
        cand = self._candidate(twist, spin, rh_mag, tilted, accdev)
        cand_label = cand[0] if cand is not None else None

        # ---- GESTURE LABEL LOCK (one motion = one action) WITH PREEMPTION. The lock
        # kills the per-sample flicker the maintainer saw ("fires a million events"),
        # but a DIFFERENT control that sustains engage_samples PREEMPTS the current one
        # WITHOUT waiting for a full return-to-rest -- so you can turn, then go, then
        # turn again fluidly (a twist takes over a go and vice versa). The old lock
        # held the first label until idle, which made steering-while-moving impossible
        # (a twist could never break a held "go"). ----
        if self._gesture_label is not None:
            # A SCRUB upgrades to GO the moment the cap tilts -- a lift that started
            # flat (so it locked as a scrub) becomes movement as soon as it angles.
            if tilted and self._gesture_label == SCRUB_STRAIGHT_LABEL:
                self._gesture_label = GO_LABEL
            if cand_label == self._gesture_label:
                # The SAME control continues -- hold it, drop any challenger.
                self._idle_n = 0
                self._cand_label = None
                self._cand_n = 0
                lab = self._gesture_label
                return lab, lab, "gesture held (locked one action)", False
            if cand_label is None:
                # Nothing right now -> count toward releasing (bridge brief gaps).
                self._cand_label = None
                self._cand_n = 0
                self._idle_n += 1
                if self._idle_n >= self.release_samples:
                    self._gesture_label = None
                    return None, "idle", "gesture ended (cap back to rest)", False
                lab = self._gesture_label
                return lab, lab, "gesture held (bridging brief idle)", False
            # A DIFFERENT control is present -> it must persist engage_samples to take
            # over (this is the anti-flicker debounce, now applied to the SWITCH).
            self._idle_n = 0
            if cand_label == self._cand_label:
                self._cand_n += 1
            else:
                self._cand_label = cand_label
                self._cand_n = 1
            if self._cand_n >= self._engage_need(cand_label):
                self._gesture_label = cand_label
                self._cand_label = None
                self._cand_n = 0
                return cand[0], cand[1], cand[2], False
            lab = self._gesture_label
            return lab, lab, "holding " + lab + " (challenger " + cand_label + ")", False

        # Between gestures: a candidate must persist engage_samples to START one.
        if cand_label is None:
            self._cand_label = None
            self._cand_n = 0
            return None, "idle", "below thresholds (still)", False
        if cand_label == self._cand_label:
            self._cand_n += 1
        else:
            self._cand_label = cand_label
            self._cand_n = 1
        if self._cand_n >= self._engage_need(cand_label):
            self._gesture_label = cand_label
            self._idle_n = 0
            return cand[0], cand[1], cand[2], False
        return None, "engaging", "sustaining " + cand_label, False

    def _engage_need(self, cand_label) -> int:
        """Samples a candidate must persist to win the lock. A SCRUB needs MORE (a
        deliberate slide is held; a phantom slide is a brief flicker) so the
        menu-opening ghosts are debounced away; turn/go stay snappy."""
        if cand_label == SCRUB_STRAIGHT_LABEL:
            return self.scrub_engage_samples
        return self.engage_samples

    def _build_features(self, tilt, twist, spin, accdev, horiz, hd, he) -> MotionFeatures:
        return MotionFeatures(
            sample_count=1,
            duration_seconds=0.0,
            gyro_p90=spin,
            gyro_p99=spin,
            accel_deviation_p99=accdev,
            accel_delta=0.0,
            orientation_angle_degrees=tilt,
            c_mean=twist,
            c_positive_fraction=1.0 if twist > 0 else 0.0,
            c_negative_fraction=1.0 if twist < 0 else 0.0,
            c_sign_runs=0,
            c_sequence="",
            gyro_peak_count=0,
            accel_peak_count=0,
            c_abs_p99=he,
            lateral_gyro_p99=hd,
            lateral_accel_p99=horiz,
        )
