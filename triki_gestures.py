from __future__ import annotations


# The seven DISCRETE gesture labels produced by the hardware-tuned classifier
# (triki_classifier / triki_live). They are the bindable controls of the "Music"
# profile (twist=volume, stir=skip, stamp=play/pause, ...). The classifier is the
# only thing that emits these; do not overload them for other input modalities.
GESTURE_LABELS = (
    "rotate-cw",
    "rotate-ccw",
    "scrub-cw",
    "scrub-ccw",
    "back-forth",
    "lift",
    "flip-over",
)

# The body-frame Motion-engine controls (triki_motion_engine), the bindable
# controls of the "Game" profile. A round cap with a 6-axis IMU (gyro+accel) has
# ONE unobservable degree of freedom -- yaw/heading (rotation about gravity) -- so
# a screen-stable 4-way tilt is physically impossible. Every control below is
# ROTATION-INVARIANT: it needs no heading, so it works no matter how the cap is
# turned. (Tank-style: twist steers, tilt drives.)
#   turn-left / turn-right        TWIST the cap flat in place (gyro about vertical)
#   go                            TILT the cap any way past a threshold -> go forward
#                                 (tilt MAGNITUDE only; direction is unobservable)
#   stamp                         STAMP the cap straight down (vertical impact)
#   flip                          FLIP the cap upside down (orientation state)
#   scrub-straight                SLIDE the cap flat on the desk -> e.g. use/open door.
#                                 (There is only ONE scrub: a round 6-axis cap cannot
#                                 reliably tell a circle from a line, so the old
#                                 scrub-circular was dropped -- both collapse here.)
MOTION_LABELS = (
    "turn-left",
    "turn-right",
    "go",
    "stamp",
    "flip",
    "scrub-straight",
)

# Every bindable control label across BOTH engines. The two vocabularies are
# disjoint (no label means two different things), so a stored binding can be
# validated/normalised against this union without caring which engine made it.
ACTION_LABELS = GESTURE_LABELS + MOTION_LABELS

GESTURE_LABEL_ALIASES = {
    "swirl-cw": "scrub-cw",
    "swirl-ccw": "scrub-ccw",
    "shake": "back-forth",
    "slide-back-forth": "back-forth",
    "table-shake": "back-forth",
}


def normalize_gesture_label(label: str) -> str:
    normalized = label.strip().lower().replace("_", "-")
    return GESTURE_LABEL_ALIASES.get(normalized, normalized)
