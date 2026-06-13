"""Unit, orientation/body-frame, and real-log regression tests for the
body-frame Motion control engine.

The engine has ZERO calibration. Two acceptance gates:

* TURN is orientation-invariant: a pure yaw twist, with the SAME random 3D
  rotation applied to BOTH gyro and accel of the whole sequence (the cap oriented
  differently in space), must yield the EXACT SAME turn intent for every
  orientation -- because yaw = dot(gyro, unit(gravity)) is a rotation-free scalar.
* MOVE is BODY-FRAME by design: tilting the cap rocks gravity into the cap's own
  d/e plane, and any deliberate held lean emits the single rotation-invariant
  tank throttle label, ``go``. The observer still exposes hd/he so the on-screen
  cap can show the physical lean direction.

FIRE is a STAMP, detected by the proven classifier 'lift' rule over a short rolling
window. The tests assert (on the real log) that real stamps fire while real twists
and synthetic held tilts do NOT, plus a refractory debounce.
"""

import json
import math
import os
import random
import unittest

from triki_motion_engine import (
    FIRE_LABEL,
    MOVE_BACKWARD_LABEL,
    MOVE_FORWARD_LABEL,
    MOVE_STRAFE_LEFT_LABEL,
    MOVE_STRAFE_RIGHT_LABEL,
    TURN_LEFT_LABEL,
    TURN_RIGHT_LABEL,
    MotionControlEngine,
)
from triki_key_emitter import HoldKeyEmitter, NullKeyEmitter
from triki_protocol import MotionSample

G = 2050.0

REAL_LOG_PATH = os.path.join(
    os.path.expanduser("~"),
    "AppData",
    "Roaming",
    "TRIKI",
    "sessions",
    "session-20260605-030830.jsonl",
)
RUN_REAL_LOG_TESTS = os.environ.get("TRIKI_REAL_LOG_TESTS") == "1"

# 10x circular stir forward/back/left/right -- PROVEN to be spin (gyro-Z) with no
# net horizontal translation: directional cycling is physically unrecoverable on a
# round cap. Used to assert the body-frame tilt joystick does NOT move on a stir.
CYCLE_LOG_PATH = os.path.join(
    os.path.expanduser("~"),
    "AppData",
    "Roaming",
    "TRIKI",
    "sessions",
    "session-20260607-190051.jsonl",
)

REST = (24, 0, -2051)  # confirmed flat-rest accel (gravity reaction)


# --------------------------------------------------------------------------- #
# Helpers: deterministic uniform-random 3D rotations + sequence rotation.
# --------------------------------------------------------------------------- #
def rotation_matrix(rng):
    """Uniformly-distributed random rotation matrix (Shoemake quaternion)."""
    u1, u2, u3 = rng.random(), rng.random(), rng.random()
    q1 = math.sqrt(1 - u1) * math.sin(2 * math.pi * u2)
    q2 = math.sqrt(1 - u1) * math.cos(2 * math.pi * u2)
    q3 = math.sqrt(u1) * math.sin(2 * math.pi * u3)
    q4 = math.sqrt(u1) * math.cos(2 * math.pi * u3)
    x, y, z, w = q1, q2, q3, q4
    return [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]


def _yaw_rotation(angle_deg):
    """Rotation about the DOWN axis (-z): turning the cap in place on the desk.
    Keeps the gravity axis fixed, so the tilt MAGNITUDE is invariant under it."""
    a = math.radians(angle_deg)
    c, s = math.cos(a), math.sin(a)
    return [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]


def matvec(m, v):
    return (
        m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
        m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
        m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2],
    )


def rotate_sequence(seq, m):
    """Apply the SAME rotation R to both gyro (0:3) and accel (3:6) of every
    sample -- i.e. the identical physical action, with the cap oriented
    differently in space."""
    rotated = []
    for t, v in seq:
        g = matvec(m, (v[0], v[1], v[2]))
        a = matvec(m, (v[3], v[4], v[5]))
        rotated.append(
            (
                t,
                (
                    int(round(g[0])),
                    int(round(g[1])),
                    int(round(g[2])),
                    int(round(a[0])),
                    int(round(a[1])),
                    int(round(a[2])),
                ),
            )
        )
    return rotated


def run_engine(seq, **kwargs):
    """Feed a [(t, values6)] sequence through a fresh engine; return per-sample
    (label_or_None) list."""
    engine = MotionControlEngine(**kwargs)
    labels = []
    for t, v in seq:
        pred = engine.add_sample(t, MotionSample(packet_id=0, values=tuple(int(x) for x in v)))
        labels.append(None if pred is None else pred.label)
    return labels


def intent_signature(labels):
    """The ordered tuple of distinct intents seen, used to compare two runs for
    'same control intent regardless of orientation'."""
    sig = []
    last = object()
    for lb in labels:
        if lb != last:
            sig.append(lb)
            last = lb
    return tuple(sig)


_ALL_MOVE_LABELS = (
    MOVE_FORWARD_LABEL,
    MOVE_BACKWARD_LABEL,
    MOVE_STRAFE_LEFT_LABEL,
    MOVE_STRAFE_RIGHT_LABEL,
)


def turn_fraction(labels):
    return sum(1 for lb in labels if lb in (TURN_RIGHT_LABEL, TURN_LEFT_LABEL)) / max(1, len(labels))


def move_fraction(labels):
    return sum(1 for lb in labels if lb in (MOVE_FORWARD_LABEL, MOVE_BACKWARD_LABEL)) / max(1, len(labels))


def any_move_fraction(labels):
    return sum(1 for lb in labels if lb in _ALL_MOVE_LABELS) / max(1, len(labels))


def fire_count(labels):
    return sum(1 for lb in labels if lb == FIRE_LABEL)


# --------------------------------------------------------------------------- #
# Synthetic action builders (in the cap's own frame, gravity rests on -z).
# --------------------------------------------------------------------------- #
def synth_rest(dt=0.02, seconds=1.5, t0=0.0):
    seq = []
    t = t0
    n = int(seconds / dt)
    for _ in range(n):
        seq.append((t, (0, 0, 0, REST[0], REST[1], REST[2])))
        t += dt
    return seq, t


def synth_yaw_twist(dt=0.02, rate=2500, n=60):
    """Cap rests, then spins about its own (down) axis at a steady yaw rate."""
    seq, t = synth_rest(dt=dt, seconds=1.5)
    for _ in range(n):
        seq.append((t, (0, 0, int(rate), REST[0], REST[1], REST[2])))
        t += dt
    return seq


def synth_lean_hold(dt=0.02, deg=12.0, hold_s=1.2, rng=None, ramp_s=0.30):
    """Cap rests, ramps a lean to `deg` on the e-axis, then HOLDS it (with small
    hand-jitter on the gyro). Gravity rotates off -z toward +e as the cap rocks
    onto an edge -- a forward/back lean in the body frame."""
    rng = rng or random
    seq, t = synth_rest(dt=dt, seconds=1.5)
    th = math.radians(deg)
    ramp = int(ramp_s / dt)
    gyro_per_radps = 150.0
    for k in range(ramp):
        a = th * (k + 1) / ramp
        dth = th / ramp_s
        seq.append(
            (
                t,
                (
                    int(dth * gyro_per_radps),
                    0,
                    0,
                    REST[0],
                    int(REST[1] + G * math.sin(a)),
                    int(-G * math.cos(a)),
                ),
            )
        )
        t += dt
    for _ in range(int(hold_s / dt)):
        jx = rng.uniform(-30, 30)
        jy = rng.uniform(-30, 30)
        jz = rng.uniform(-30, 30)
        seq.append(
            (
                t,
                (
                    int(jx),
                    int(jy),
                    int(jz),
                    REST[0],
                    int(REST[1] + G * math.sin(th)),
                    int(-G * math.cos(th)),
                ),
            )
        )
        t += dt
    return seq


# --------------------------------------------------------------------------- #
# Body-frame tilt builders (cap frame, gravity rests on -z; the tilt rocks
# gravity into the d/e plane toward in-plane unit (dx, dy)). A directional lean is
# held with a small STEADY in-plane gyro perpendicular to the lean so the cap
# reads as "in motion" (not motion-still): this keeps the neutral gref from
# relocking and erasing the held lean -- exactly the real-world case (you hold a
# tilt with hand tremor). yaw = dot(gyro, down) stays ~0 because the spin axis is
# perpendicular to gravity, so it never trips TURN, and |gyro| < spin_quiet so the
# calm MOVE gate still passes.
# --------------------------------------------------------------------------- #
def synth_dir_lean(dx, dy, deg=20.0, hold_s=1.0, ramp_s=0.30, dt=0.02, t0=0.0, spin=250, rest_s=1.5):
    seq = []
    t = t0
    for _ in range(int(rest_s / dt)):
        seq.append((t, (0, 0, 0, REST[0], REST[1], REST[2])))
        t += dt
    th = math.radians(deg)
    ramp = int(ramp_s / dt)
    gx, gy = int(spin * dy), int(-spin * dx)
    for k in range(ramp):
        a = th * (k + 1) / ramp
        seq.append(
            (
                t,
                (
                    gx,
                    gy,
                    0,
                    int(REST[0] + G * math.sin(a) * dx),
                    int(REST[1] + G * math.sin(a) * dy),
                    int(-G * math.cos(a)),
                ),
            )
        )
        t += dt
    for _ in range(int(hold_s / dt)):
        seq.append(
            (
                t,
                (
                    gx,
                    gy,
                    0,
                    int(REST[0] + G * math.sin(th) * dx),
                    int(REST[1] + G * math.sin(th) * dy),
                    int(-G * math.cos(th)),
                ),
            )
        )
        t += dt
    return seq, t


def _dominant_move_label(labels):
    """The most common non-None movement label over a label list (the committed
    direction), ignoring the brief ramp transient before the axis commits."""
    from collections import Counter

    counts = Counter(lb for lb in labels if lb in _ALL_MOVE_LABELS)
    return counts.most_common(1)[0][0] if counts else None


# --------------------------------------------------------------------------- #
# A) TURN: pure-yaw twist, orientation-invariance (the turn acceptance gate).
# --------------------------------------------------------------------------- #
class YawTurnInvarianceTests(unittest.TestCase):
    def test_turn_threshold_is_independent_from_turn_sensitivity(self):
        engine = MotionControlEngine()

        engine.set_turn_sensitivity(85)

        self.assertEqual(engine.turn_sensitivity, 85)
        self.assertEqual(engine.turn_threshold, 1000.0)

        engine.set_turn_threshold(580)

        self.assertEqual(engine.turn_threshold, 580.0)
        self.assertEqual(engine.turn_sensitivity, 85)
        self.assertEqual(engine.twist_on, 580.0)
        self.assertAlmostEqual(engine.twist_off, 400.2)
        self.assertEqual(engine.turn_spin_on, 450.0)

    def test_pure_yaw_engages_a_single_consistent_turn_and_move_silent(self):
        # At rest gravity reads (0,0,-G) so g_hat ~ (0,0,-1); a +c gyro spin
        # projects to yaw = dot((0,0,+rate),(0,0,-1)) < 0 -> the engine maps it to
        # rotate-ccw. The ABSOLUTE sign is a binding choice (round cap has no marked
        # front); what matters is that one spin direction yields exactly ONE
        # consistent turn label and the OPPOSITE spin yields the OTHER.
        labels = run_engine(synth_yaw_twist(rate=2500))
        turns = {lb for lb in labels if lb in (TURN_RIGHT_LABEL, TURN_LEFT_LABEL)}
        self.assertEqual(turns, {TURN_RIGHT_LABEL})
        self.assertGreater(turn_fraction(labels), 0.4)
        # A pure twist must NEVER drive MOVE or FIRE.
        self.assertEqual(any_move_fraction(labels), 0.0)
        self.assertEqual(fire_count(labels), 0)

    def test_opposite_spin_yields_the_opposite_turn_label(self):
        plus = {lb for lb in run_engine(synth_yaw_twist(rate=2500)) if lb in (TURN_RIGHT_LABEL, TURN_LEFT_LABEL)}
        minus = {lb for lb in run_engine(synth_yaw_twist(rate=-2500)) if lb in (TURN_RIGHT_LABEL, TURN_LEFT_LABEL)}
        self.assertEqual(plus, {TURN_RIGHT_LABEL})
        self.assertEqual(minus, {TURN_LEFT_LABEL})
        self.assertNotEqual(plus, minus)

    def test_pure_yaw_intent_is_identical_under_tabletop_rotations(self):
        base = synth_yaw_twist(rate=2500)
        base_labels = run_engine(base)
        base_sig = intent_signature(base_labels)
        base_turn = turn_fraction(base_labels)

        mismatches = 0
        turn_fracs = []
        for angle in range(0, 360, 15):
            m = _yaw_rotation(angle)
            labels = run_engine(rotate_sequence(base, m))
            turn_fracs.append(turn_fraction(labels))
            if intent_signature(labels) != base_sig:
                mismatches += 1
            # MOVE must stay silent in every orientation.
            self.assertEqual(any_move_fraction(labels), 0.0)
        self.assertEqual(mismatches, 0)
        # Turn-active fraction is identical across all orientations to ~1e-6.
        self.assertAlmostEqual(min(turn_fracs), base_turn, delta=1e-6)
        self.assertAlmostEqual(max(turn_fracs), base_turn, delta=1e-6)


# --------------------------------------------------------------------------- #
# B) MOVE: a held lean engages MOVE (forward/back) and never turns.
# --------------------------------------------------------------------------- #
class LeanMoveTests(unittest.TestCase):
    def test_held_lean_engages_move_after_hold_and_turn_silent(self):
        random.seed(7)
        labels = run_engine(synth_lean_hold(deg=14.0))
        self.assertGreater(move_fraction(labels), 0.3)
        # A deliberate calm lean must NOT be read as a turn or a fire.
        self.assertEqual(turn_fraction(labels), 0.0)
        self.assertEqual(fire_count(labels), 0)

    def test_move_engages_only_after_the_hold_window(self):
        # Below hold_seconds of sustained lean there is no MOVE; the engine waits
        # for a deliberate held lean rather than a momentary tip.
        engine = MotionControlEngine()
        dt = 0.02
        t = 0.0
        for _ in range(int(1.5 / dt)):
            engine.add_sample(t, MotionSample(0, (0, 0, 0, REST[0], REST[1], REST[2])))
            t += dt
        a = math.radians(20)
        lean = (0, 0, 0, REST[0], int(REST[1] + G * math.sin(a)), int(-G * math.cos(a)))
        # First sample of the lean: tilt is high but hold has not elapsed -> idle.
        first = engine.add_sample(t, MotionSample(0, lean))
        t += dt
        self.assertIsNone(first)
        # After hold_seconds it engages.
        engaged = False
        for _ in range(int(0.4 / dt)):
            pred = engine.add_sample(t, MotionSample(0, lean))
            t += dt
            if pred is not None and pred.label in _ALL_MOVE_LABELS:
                engaged = True
        self.assertTrue(engaged)

    def test_tilt_magnitude_is_invariant_to_turning_the_cap_on_the_desk(self):
        # The realistic invariance for a body-frame engine: turning the whole cap
        # about its DOWN axis (same desk, cap rotated in place -- a yaw rotation)
        # leaves the tilt MAGNITUDE and yaw rate identical sample-by-sample. (Only
        # the lean DIRECTION -- which key -- rotates with the cap, which is exactly
        # the body-frame behaviour the spec asks for; an ARBITRARY 3D rotation of
        # the cap onto a different face is intentionally NOT magnitude-invariant.)
        random.seed(11)
        base = synth_lean_hold(deg=14.0, hold_s=0.6)
        base_tilt, base_yaw = self._signals(base)
        for ang_deg in (0, 30, 90, 137, 180, 270):
            tilt, yaw = self._signals(rotate_sequence(base, _yaw_rotation(ang_deg)))
            for a, b in zip(base_tilt, tilt):
                self.assertAlmostEqual(a, b, delta=0.2)  # degrees
            for a, b in zip(base_yaw, yaw):
                self.assertAlmostEqual(a, b, delta=1.0)  # device counts

    @staticmethod
    def _signals(seq):
        records: list[dict] = []
        engine = MotionControlEngine(observer=records.append)
        for t, v in seq:
            engine.add_sample(t, MotionSample(packet_id=0, values=tuple(int(x) for x in v)))
        tilts = [r["tilt"] for r in records]
        yaws = [r["yaw"] for r in records]
        return tilts, yaws


# --------------------------------------------------------------------------- #
# C) BODY-FRAME TILT SIGNS (zero calibration) -- the bug-#8 sign contract.
#    Each in-plane lean direction decodes to a specific WASD label, and OPPOSITE
#    leans give OPPOSITE keys. The exact physical-direction<->key sign is a user
#    preference, but opposite-must-be-opposite is locked, and the viz reads the
#    SAME hd/he diagnostic source so it can never disagree with the key output.
# --------------------------------------------------------------------------- #
class BodyFrameTiltSignTests(unittest.TestCase):
    def test_each_lean_direction_maps_to_the_tank_go_label(self):
        # The old 4-way body-frame WASD labels collapsed into one rotation-invariant
        # tank throttle. synth_dir_lean(dx,dy) still exercises every physical lean
        # direction; each must now produce GO.
        cases = {
            (0.0, -1.0): MOVE_FORWARD_LABEL,
            (0.0, 1.0): MOVE_BACKWARD_LABEL,
            (1.0, 0.0): MOVE_STRAFE_RIGHT_LABEL,
            (-1.0, 0.0): MOVE_STRAFE_LEFT_LABEL,
        }
        seen = set()
        for (dx, dy), expected in cases.items():
            seq, _ = synth_dir_lean(dx, dy)
            dom = _dominant_move_label(run_engine(seq))
            self.assertEqual(dom, expected, f"lean (dx={dx},dy={dy}) -> {dom!r}, expected {expected!r}")
            seen.add(expected)
        self.assertEqual(seen, {MOVE_FORWARD_LABEL})

    def test_opposite_leans_keep_the_same_tank_go_label(self):
        # Opposite physical leans are no longer different keys; the single reliable
        # motion output is GO.
        fwd = _dominant_move_label(run_engine(synth_dir_lean(0.0, -1.0)[0]))
        back = _dominant_move_label(run_engine(synth_dir_lean(0.0, 1.0)[0]))
        right = _dominant_move_label(run_engine(synth_dir_lean(1.0, 0.0)[0]))
        left = _dominant_move_label(run_engine(synth_dir_lean(-1.0, 0.0)[0]))
        self.assertEqual({fwd, back, right, left}, {MOVE_FORWARD_LABEL})

    def test_diagnostics_hd_he_keep_the_physical_lean_sign(self):
        # The viz reads hd/he from the SAME observer the engine measures. Even though
        # the emitted label is now always GO, the diagnostics still preserve physical
        # lean direction for the on-screen cap.
        records: list[dict] = []
        engine = MotionControlEngine(observer=records.append)
        seq, _ = synth_dir_lean(1.0, 0.0)  # +d lean
        last_label = None
        for t, v in seq:
            pred = engine.add_sample(t, MotionSample(0, tuple(int(x) for x in v)))
            if pred is not None:
                last_label = pred.label
        self.assertEqual(last_label, MOVE_STRAFE_RIGHT_LABEL)
        self.assertGreater(records[-1]["hd"], 0.0)  # +d lean reads hd>0
        # And a forward lean reads he<0.
        records2: list[dict] = []
        engine2 = MotionControlEngine(observer=records2.append)
        seq2, _ = synth_dir_lean(0.0, -1.0)
        for t, v in seq2:
            engine2.add_sample(t, MotionSample(0, tuple(int(x) for x in v)))
        self.assertLess(records2[-1]["he"], 0.0)

    def test_invert_fwd_keeps_the_tank_go_label(self):
        # Direction inversion no longer changes the label because tank movement has
        # one throttle output.
        normal = _dominant_move_label(run_engine(synth_dir_lean(0.0, -1.0)[0]))
        flipped = _dominant_move_label(run_engine(synth_dir_lean(0.0, -1.0)[0], invert_fwd=True))
        self.assertEqual(normal, MOVE_FORWARD_LABEL)
        self.assertEqual(flipped, MOVE_FORWARD_LABEL)

    def test_d_axis_also_drives_the_tank_go_label(self):
        seq, _ = synth_dir_lean(1.0, 0.0)  # +d lean
        normal = _dominant_move_label(run_engine(seq))
        self.assertEqual(normal, MOVE_STRAFE_RIGHT_LABEL)

    def test_small_tilt_inside_deadzone_does_not_move(self):
        # A tiny lean (below tilt_on ~9deg) under the deadzone must not move.
        seq, _ = synth_dir_lean(0.0, -1.0, deg=3.0, hold_s=1.0)
        self.assertEqual(any_move_fraction(run_engine(seq)), 0.0)


# --------------------------------------------------------------------------- #
# D) STAMP -> FIRE. The proven classifier 'lift' detector over a rolling window:
#    real stamps fire, real twists and synthetic held tilts do NOT, with a
#    refractory debounce. (Real-log slices live in RealStampFireTests below.)
# --------------------------------------------------------------------------- #
class StampFireSyntheticTests(unittest.TestCase):
    def test_held_tilt_never_fires(self):
        # A held lean is static gravity (no f-axis drop/rebound, accdev ~0): it
        # MOVES but must never FIRE.
        for dx, dy in ((0.0, -1.0), (0.0, 1.0), (1.0, 0.0), (-1.0, 0.0)):
            seq, _ = synth_dir_lean(dx, dy, deg=25.0, hold_s=2.0)
            labels = run_engine(seq)
            self.assertEqual(fire_count(labels), 0, f"held tilt {(dx, dy)} false-fired")

    def test_twist_never_fires(self):
        for rate in (2500, -2500, 6000):
            self.assertEqual(fire_count(run_engine(synth_yaw_twist(rate=rate))), 0)

    def test_rest_never_fires(self):
        seq, _ = synth_rest(seconds=2.0)
        self.assertEqual(fire_count(run_engine(seq)), 0)

    def test_fire_disabled_flag_silences_fire(self):
        # With fire disabled the engine still moves/turns but never emits FIRE.
        if not os.path.exists(REAL_LOG_PATH):
            self.skipTest("real log absent")
        rows = _load_real_gesture_rows(REAL_LOG_PATH)
        slice_rows = _first_stamp_slice(rows)
        self.assertIsNotNone(slice_rows)
        labels = _run_real_slice(slice_rows, fire_enabled=False)
        self.assertEqual(fire_count(labels), 0)


# --------------------------------------------------------------------------- #
# Hysteresis, calm gate, gref relock.
# --------------------------------------------------------------------------- #
class HysteresisTests(unittest.TestCase):
    def test_tilt_hysteresis_no_chatter_between_on_and_off(self):
        # Engage MOVE at a clear lean above tilt_on, then settle between tilt_off
        # and tilt_on. Because we are already moving, the OFF threshold applies and
        # MOVE stays engaged -- no drop-out.
        engine = MotionControlEngine()
        dt = 0.02
        t = 0.0
        for _ in range(int(1.5 / dt)):
            engine.add_sample(t, MotionSample(0, (0, 0, 0, REST[0], REST[1], REST[2])))
            t += dt

        def lean_sample(deg):
            a = math.radians(deg)
            return MotionSample(0, (0, 0, 0, REST[0], int(REST[1] + G * math.sin(a)), int(-G * math.cos(a))))

        for _ in range(int(0.5 / dt)):
            engine.add_sample(t, lean_sample(14))
            t += dt
        moving_high = engine._gesture_label == MOVE_FORWARD_LABEL
        # settle to 7deg (above tilt_off ~5.6, below tilt_on ~9) and hold
        for _ in range(int(0.5 / dt)):
            engine.add_sample(t, lean_sample(7))
            t += dt
        moving_mid = engine._gesture_label == MOVE_FORWARD_LABEL
        self.assertTrue(moving_high)
        self.assertTrue(moving_mid)  # hysteresis keeps it engaged

    def test_tilt_below_off_threshold_releases_move(self):
        engine = MotionControlEngine()
        dt = 0.02
        t = 0.0

        def lean_sample(deg):
            a = math.radians(deg)
            return MotionSample(0, (0, 0, 0, REST[0], int(REST[1] + G * math.sin(a)), int(-G * math.cos(a))))

        for _ in range(int(1.5 / dt)):
            engine.add_sample(t, MotionSample(0, (0, 0, 0, REST[0], REST[1], REST[2])))
            t += dt
        for _ in range(int(0.5 / dt)):
            engine.add_sample(t, lean_sample(20))
            t += dt
        self.assertEqual(engine._gesture_label, MOVE_FORWARD_LABEL)
        # Drop flat -- below tilt_off -> MOVE releases.
        for _ in range(int(0.3 / dt)):
            engine.add_sample(t, lean_sample(1))
            t += dt
        self.assertIsNone(engine._gesture_label)

    def test_yaw_hysteresis_holds_turn_between_on_and_off(self):
        engine = MotionControlEngine()
        dt = 0.02
        t = 0.0
        for _ in range(int(1.5 / dt)):
            engine.add_sample(t, MotionSample(0, (0, 0, 0, REST[0], REST[1], REST[2])))
            t += dt
        for _ in range(20):
            engine.add_sample(t, MotionSample(0, (0, 0, 1600, REST[0], REST[1], REST[2])))
            t += dt
        self.assertNotEqual(engine._turning, 0)
        # Hold at yaw ~800 (between OFF=690 and ON=1000): still turning.
        for _ in range(20):
            engine.add_sample(t, MotionSample(0, (0, 0, 800, REST[0], REST[1], REST[2])))
            t += dt
        self.assertNotEqual(engine._turning, 0)
        # Drop below YAW_OFF -> release.
        for _ in range(20):
            engine.add_sample(t, MotionSample(0, (0, 0, 100, REST[0], REST[1], REST[2])))
            t += dt
        self.assertEqual(engine._turning, 0)


class CalmGateTests(unittest.TestCase):
    @staticmethod
    def _lean_with_spin(deg, spin_c, dt=0.02, hold_s=0.8):
        seq, t = synth_rest(dt=dt, seconds=1.5)
        a = math.radians(deg)
        for _ in range(int(hold_s / dt)):
            # A deep lean accel held, but with a large concurrent spin about the
            # cap's own axis (high |gyro| and high |yaw|).
            seq.append((t, (0, 0, int(spin_c), REST[0], int(REST[1] + G * math.sin(a)), int(-G * math.cos(a)))))
            t += dt
        return seq

    def test_high_spin_with_strong_tilt_still_keeps_the_tank_throttle(self):
        # Tank mode gives GO priority while the cap is deliberately tilted. A spin
        # during that tilt must not become a turn/fire regression.
        labels = run_engine(self._lean_with_spin(30, 2000))
        self.assertGreater(any_move_fraction(labels), 0.0)
        self.assertEqual(turn_fraction(labels), 0.0)
        self.assertEqual(fire_count(labels), 0)

    def test_same_tilt_fires_move_once_calm(self):
        seq, t = synth_rest(seconds=1.5)
        a = math.radians(20)
        for _ in range(int(0.8 / 0.02)):
            seq.append((t, (0, 0, 0, REST[0], int(REST[1] + G * math.sin(a)), int(-G * math.cos(a)))))
            t += 0.02
        labels = run_engine(seq)
        self.assertGreater(any_move_fraction(labels), 0.0)


class NeutralRelockTests(unittest.TestCase):
    def test_gref_does_not_drift_while_leaning_and_moving(self):
        # Start leaned and HELD with persistent hand-jitter (motion present, so NOT
        # motion-still): gref must NOT relock, so the lean keeps reading as a lean
        # (tilt stays high) -- MOVE keeps firing, no false re-centering.
        random.seed(3)
        seq = synth_lean_hold(deg=20.0, hold_s=3.0)  # > RELOCK_S
        seq2 = []
        for i, (t, v) in enumerate(seq):
            if i > 40:  # during the hold, inject a steady gyro so not motion-still
                v = (v[0] if abs(v[0]) > 0 else 0, v[1], 250, v[3], v[4], v[5])
            seq2.append((t, v))
        records = []
        engine = MotionControlEngine(observer=records.append)
        for t, v in seq2:
            engine.add_sample(t, MotionSample(0, tuple(int(x) for x in v)))
        tail_tilt = [r["tilt"] for r in records[-20:]]
        self.assertGreater(min(tail_tilt), 11.0)

    def test_new_tilted_orientation_held_motionless_does_not_recenter_gref(self):
        # A held tilt must NOT be learned as neutral. Only flat-and-still relocks
        # the neutral; otherwise holding GO would fade out.
        dt = 0.02
        t = 0.0
        a = math.radians(20)
        tilted = (0, 0, 0, REST[0], int(REST[1] + G * math.sin(a)), int(-G * math.cos(a)))
        records = []
        engine = MotionControlEngine(observer=lambda r: records.append(r))
        for _ in range(int(1.5 / dt)):
            engine.add_sample(t, MotionSample(0, (0, 0, 0, REST[0], REST[1], REST[2])))
            t += dt
        for _ in range(int(5.0 / dt)):
            engine.add_sample(t, MotionSample(0, tilted))
            t += dt
        early_tilt = records[int(1.7 / dt)]["tilt"]
        late_tilt = records[-1]["tilt"]
        self.assertGreater(early_tilt, 12.0)  # initially a big apparent lean
        self.assertGreater(late_tilt, 12.0)  # tilted pose was not learned as rest

    def test_tilt_gated_relock_would_deadlock_regression(self):
        # Documents WHY relock must be motion-stillness, not tilt-gated.
        dt = 0.02
        a = math.radians(20)
        tilted = (0, 0, 0, REST[0], int(REST[1] + G * math.sin(a)), int(-G * math.cos(a)))
        g = [float(REST[0]), float(REST[1]), float(REST[2])]
        gref = [float(REST[0]), float(REST[1]), float(REST[2])]
        alpha = 0.12
        af = 0.05
        tilts = []
        for _ in range(int(5.0 / dt)):
            acc = tilted[3:]
            g = [(1 - alpha) * g[i] + alpha * acc[i] for i in range(3)]

            def unit(v):
                n = math.sqrt(sum(c * c for c in v)) or 1.0
                return [c / n for c in v]

            gu, gru = unit(g), unit(gref)
            cos = max(-1.0, min(1.0, sum(gu[i] * gru[i] for i in range(3))))
            tilt = math.degrees(math.acos(cos))
            tilts.append(tilt)
            if tilt < 1.0:  # BROKEN policy: only relock when essentially flat
                gref = [(1 - af) * gref[i] + af * acc[i] for i in range(3)]
        self.assertGreater(tilts[-1], 12.0)  # deadlocked: never re-centers


# --------------------------------------------------------------------------- #
# Continuous-output contract (HoldKeyEmitter + NullKeyEmitter).
# --------------------------------------------------------------------------- #
class ContinuousOutputTests(unittest.TestCase):
    def test_sustained_intent_holds_key_then_auto_releases(self):
        clock = {"now": 1000.0}
        base = NullKeyEmitter()
        emitter = HoldKeyEmitter(base, hold_ms=200, monotonic=lambda: clock["now"])

        active_key = "up"
        for _ in range(30):
            emitter.press_key(active_key)
            clock["now"] += 0.02
        self.assertEqual(base.downs, ["up"])  # held continuously: single down
        self.assertEqual(base.ups, [])
        self.assertEqual(base.pressed, [])

        clock["now"] += 1.0
        emitter.set_hold_ms(0)
        self.assertEqual(base.ups, ["up"])
        emitter.close()

    def test_two_simultaneous_intents_hold_both_keys(self):
        clock = {"now": 0.0}
        base = NullKeyEmitter()
        emitter = HoldKeyEmitter(base, hold_ms=200, monotonic=lambda: clock["now"])
        for _ in range(20):
            emitter.press_key("right")  # turn
            clock["now"] += 0.02
            emitter.press_key("up")  # move
            clock["now"] += 0.02
        self.assertEqual(sorted(base.downs), ["right", "up"])
        self.assertEqual(base.ups, [])
        emitter.set_hold_ms(0)
        self.assertEqual(sorted(base.ups), ["right", "up"])
        emitter.close()


# --------------------------------------------------------------------------- #
# REAL-LOG helpers + regression (skipped cleanly if the labeled session is absent).
# --------------------------------------------------------------------------- #
def _load_real_gesture_rows(path):
    rows = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("type") == "gesture" and "values" in row:
                rows.append(row)
    return rows


def _label_run_starts(rows, want, gap=10):
    idx = [i for i, r in enumerate(rows) if r.get("label") == want]
    if not idx:
        return []
    starts = [idx[0]]
    for k in range(1, len(idx)):
        if idx[k] - idx[k - 1] > gap:
            starts.append(idx[k])
    return starts


def _run_real_slice(slice_rows, **kwargs):
    """Replay a short real slice through a fresh engine, re-basing time to ~0.06s
    so the rolling fire window fills naturally."""
    kwargs.setdefault("settle_seconds", 0.0)
    engine = MotionControlEngine(**kwargs)
    t0 = slice_rows[0]["t"]
    labels = []
    for r in slice_rows:
        pred = engine.add_sample(
            r["t"] - t0 + 0.06, MotionSample(packet_id=0, values=tuple(int(x) for x in r["values"]))
        )
        labels.append(None if pred is None else pred.label)
    return labels


def _first_stamp_slice(rows):
    starts = _label_run_starts(rows, "lift")
    for s in starts:
        lo = max(0, s - 25)
        hi = min(len(rows), s + 25)
        sl = rows[lo:hi]
        if any(r.get("label") == "lift" for r in sl):
            return sl
    return None


def _detect_bursts(rows):
    """Energy-gated bursts, matching tmp/decide_validate.py exactly."""
    indexed_bursts = []
    cur = []
    for i, row in enumerate(rows):
        v = row["values"]
        gyro = math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
        accdev = abs(math.sqrt(v[3] ** 2 + v[4] ** 2 + v[5] ** 2) - G)
        if gyro >= 500 or accdev >= 350:
            cur.append(i)
        else:
            if len(cur) >= 5:
                indexed_bursts.append(cur)
            cur = []
    if len(cur) >= 5:
        indexed_bursts.append(cur)
    return indexed_bursts


@unittest.skipUnless(
    RUN_REAL_LOG_TESTS and os.path.exists(REAL_LOG_PATH),
    "set TRIKI_REAL_LOG_TESTS=1 with labeled real session logs to run",
)
class RealStampFireTests(unittest.TestCase):
    """STAMP->FIRE on the real log: real stamps fire 'lift', real twists never do,
    and the refractory keeps one stamp to roughly one fire."""

    @classmethod
    def setUpClass(cls):
        cls.rows = _load_real_gesture_rows(REAL_LOG_PATH)

    def test_real_stamp_slices_fire(self):
        starts = _label_run_starts(self.rows, "lift")
        self.assertGreaterEqual(len(starts), 10)
        fired = 0
        tested = 0
        for s in starts[:12]:
            sl = self.rows[max(0, s - 25): s + 25]
            tested += 1
            if fire_count(_run_real_slice(sl)) > 0:
                fired += 1
        # Validated 10/12 with the proven classifier 'lift' rule on a rolling
        # window (matches tmp/real_stamp_slice.py and tmp/engine_fire_probe_fast.py).
        self.assertGreaterEqual(fired, 9, f"only {fired}/{tested} real stamp slices fired")

    def test_real_twist_slices_never_fire(self):
        starts = _label_run_starts(self.rows, "rotate-cw") + _label_run_starts(self.rows, "rotate-ccw")
        starts.sort()
        false_fires = 0
        for s in starts[:8]:
            sl = self.rows[max(0, s - 25): s + 30]
            if fire_count(_run_real_slice(sl)) > 0:
                false_fires += 1
        self.assertEqual(false_fires, 0)

    def test_fire_respects_refractory(self):
        # Within one stamp slice the refractory keeps fires sparse (not one per
        # sample): a 0.35s debounce over a ~1s slice can fire at most a few times.
        starts = _label_run_starts(self.rows, "lift")
        sl = self.rows[max(0, starts[1] - 25): starts[1] + 25]
        labels = _run_real_slice(sl)
        if fire_count(labels) > 0:
            self.assertLessEqual(fire_count(labels), 4)


@unittest.skipUnless(
    RUN_REAL_LOG_TESTS and os.path.exists(REAL_LOG_PATH),
    "set TRIKI_REAL_LOG_TESTS=1 with labeled real session logs to run",
)
class RealLogRegressionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = _load_real_gesture_rows(REAL_LOG_PATH)
        # TURN/MOVE regression only; FIRE is covered by RealStampFireTests on short
        # slices. Disable the rolling classifier-window fire detector here so the
        # full-log replay (~30k samples) stays fast (no per-sample extract_features).
        engine = MotionControlEngine(fire_enabled=False)
        cls.labels = []
        for row in cls.rows:
            pred = engine.add_sample(
                row["t"], MotionSample(packet_id=0, values=tuple(int(x) for x in row["values"]))
            )
            cls.labels.append(None if pred is None else pred.label)
        cls.bursts = _detect_bursts(cls.rows)
        cls.truth = (
            ["forward"] * 10 + ["backward"] * 10 + ["air-stamp"] * 10 + ["table-stamp"] * 10
        )

    def _per_truth(self):
        agg = {}
        for k, burst in enumerate(self.bursts[:40]):
            n = len(burst)
            tr = sum(1 for i in burst if self.labels[i] in (TURN_RIGHT_LABEL, TURN_LEFT_LABEL)) / n
            mv = sum(1 for i in burst if self.labels[i] in (MOVE_FORWARD_LABEL, MOVE_BACKWARD_LABEL)) / n
            truth = self.truth[k]
            agg.setdefault(truth, {"turn": [], "move": [], "anymove": 0})
            agg[truth]["turn"].append(tr)
            agg[truth]["move"].append(mv)
            if mv > 0:
                agg[truth]["anymove"] += 1
        return agg

    def test_at_least_forty_labeled_bursts_present(self):
        self.assertGreaterEqual(len(self.bursts), 40)

    def test_move_never_false_fires_on_any_stir_or_stamp_burst(self):
        # The calm gate must keep forward/back MOVE at exactly zero across all 40
        # labeled stir/stamp bursts (measured 0/40).
        agg = self._per_truth()
        total_with_move = sum(agg[t]["anymove"] for t in agg)
        self.assertEqual(total_with_move, 0)

    def test_rest_is_quiet(self):
        # REST samples (classifier label 'still') must never TURN, very rarely
        # MOVE, and never spuriously FIRE.
        still = [i for i, row in enumerate(self.rows) if row.get("label") == "still"]
        self.assertGreater(len(still), 1000)
        turn_rest = sum(1 for i in still if self.labels[i] in (TURN_RIGHT_LABEL, TURN_LEFT_LABEL)) / len(still)
        move_rest = sum(1 for i in still if self.labels[i] in _ALL_MOVE_LABELS) / len(still)
        fire_rest = sum(1 for i in still if self.labels[i] == FIRE_LABEL)
        self.assertEqual(turn_rest, 0.0)
        self.assertLess(move_rest, 0.01)
        self.assertEqual(fire_rest, 0)


def _longest_move_run(labels):
    best = 0
    cur = 0
    for lb in labels:
        if lb in _ALL_MOVE_LABELS:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


@unittest.skipUnless(
    RUN_REAL_LOG_TESTS and os.path.exists(CYCLE_LOG_PATH),
    "set TRIKI_REAL_LOG_TESTS=1 with labeled real session logs to run",
)
class CyclingNoDirectionalMoveTests(unittest.TestCase):
    """REAL-LOG gate: the 10x4 circular-stir session (proven pure spin, no net
    translation) must NOT drive the body-frame tilt joystick. The calm gate (high
    spin/yaw during a stir) blocks it -- assert essentially zero sustained run and
    zero strafe."""

    @classmethod
    def setUpClass(cls):
        cls.rows = _load_real_gesture_rows(CYCLE_LOG_PATH)
        # MOVE/strafe regression only; disable the fire detector so the ~49k-sample
        # stir replay stays fast (fire on a stir is irrelevant to this gate).
        engine = MotionControlEngine(fire_enabled=False)
        cls.labels = []
        for row in cls.rows:
            pred = engine.add_sample(
                row["t"], MotionSample(packet_id=0, values=tuple(int(x) for x in row["values"]))
            )
            cls.labels.append(None if pred is None else pred.label)

    def test_log_has_many_samples(self):
        self.assertGreater(len(self.rows), 10000)

    def test_no_sustained_directional_move_on_a_stir(self):
        n = len(self.labels)
        move = sum(1 for lb in self.labels if lb in _ALL_MOVE_LABELS)
        frac = move / n
        self.assertLess(frac, 0.02, f"MOVE fired on {100 * frac:.3f}% of stir samples")
        self.assertLess(
            _longest_move_run(self.labels),
            25,
            "a stir produced a >0.5s sustained directional MOVE run (it must not)",
        )


if __name__ == "__main__":
    unittest.main()
