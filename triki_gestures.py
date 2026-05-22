from __future__ import annotations


GESTURE_LABELS = (
    "rotate-cw",
    "rotate-ccw",
    "scrub-cw",
    "scrub-ccw",
    "back-forth",
    "lift",
    "flip-over",
)

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
