from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from triki_gestures import (
    ACTION_LABELS,
    GESTURE_LABELS,
    MOTION_LABELS,
    normalize_gesture_label,
)
from triki_key_emitter import (
    DEFAULT_MOUSE_SPEED,
    MAX_HOLD_MS,
    KeyEmissionError,
    create_default_key_emitter,
    normalize_key_name,
    normalize_mouse_speed,
    validate_output_name,
)


_logger = logging.getLogger("triki")


# The app ships three built-in profile slots: "Game" (the default), "Music", and
# "Mouse". Every profile uses the same body-frame Motion engine settings and
# bindable controls as Game; Music and Mouse keep purpose-specific defaults.
GAME_PROFILE_NAME = "Game"
MUSIC_PROFILE_NAME = "Music"
MOUSE_PROFILE_NAME = "Mouse"
DEFAULT_PROFILE_NAME = GAME_PROFILE_NAME
BUILTIN_PROFILE_NAMES = (GAME_PROFILE_NAME, MUSIC_PROFILE_NAME, MOUSE_PROFILE_NAME)
# CONFIG_VERSION bumped 9->10 for the calibration-grounded control rebuild: the
# "Game" profile gains the full 10-control MOTION_LABELS set (turn, the two tilt
# axes, stamp, flip, scrub) on fresh WSAD-based defaults. Pre-v10 Game bindings are
# the old scheme, so the version-guarded built-in fold in merged_with_defaults()
# drops them and the fresh defaults win (a one-time reset of the Game binds).
# 12->13: scrub-circular dropped (a round cap can't tell a circle from a line); the
# single surviving scrub-straight is remapped to Space (use/door). The fold drops the
# now-dead scrub-circular bind from older configs.
# 13->14: Music and custom profiles now use the same Motion/Game action vocabulary,
# dropping the old classifier-only action rows from Advanced.
# 14->15: Music keeps the shared Motion rows, but restores media-key defaults.
# 15->16: Motion tuning gets the first per-profile settings pass.
# 16->17: profile threshold tuning moves to the turn/twist threshold.
# 17->18: profiles gain a cross-platform mouse movement speed while preserving
# every compatible Game/Music action override.
# 18->19: mouse movement mapped to turn controls gains an optional analog axis
# mode, enabled by default and saved independently for every profile.
# 19->20: Mouse becomes a built-in profile. The former Polish custom-profile name
# "Myszka" is folded into it so existing users keep their settings without seeing
# a duplicate tile.
CONFIG_VERSION = 20
# Action-map compatibility is narrower than the whole config schema. Later schema
# bumps for Motion tuning must not erase user key overrides from already-compatible
# Game/Doom configs.
GAME_ACTION_OVERRIDE_VERSION = 14
MUSIC_ACTION_OVERRIDE_VERSION = 15
MOUSE_ACTION_OVERRIDE_VERSION = 19
MAX_MACRO_DELAY_MS = 5000  # ceiling for a single macro delay step; legit macros use sub-second delays
MIN_MOTION_TILT_THRESHOLD = 3.0
MAX_MOTION_TILT_THRESHOLD = 30.0
DEFAULT_MOTION_TILT_THRESHOLD = 7.6
MIN_MOTION_TURN_THRESHOLD = 400.0
MAX_MOTION_TURN_THRESHOLD = 1600.0
DEFAULT_GAME_TURN_THRESHOLD = 1000.0
DEFAULT_MUSIC_TURN_THRESHOLD = 580.0
DEFAULT_GAME_TURN_SENSITIVITY = 50.0
DEFAULT_MUSIC_TURN_SENSITIVITY = 85.0

# Control engines. "classifier" is the old discrete LiveGestureDetector path kept
# for tooling/legacy code; "motion" is the body-frame continuous MotionControlEngine
# used by every app profile. The engine is a DERIVED property of the active profile
# (see engine_for_profile), so the stored value can never drift from the UI.
ENGINE_CLASSIFIER = "classifier"
ENGINE_MOTION = "motion"
VALID_ENGINES = (ENGINE_CLASSIFIER, ENGINE_MOTION)
DEFAULT_ENGINE = ENGINE_MOTION

# End-user UI language. Polish is the default (the maintainer is Polish and the
# kid-facing UI ships PL-first); English is the alternate, persisted via the
# normal config round-trip. An absent/unknown value resolves to Polish so old
# configs load unchanged. The /debug diagnostics page stays English regardless.
LANG_PL = "pl"
LANG_EN = "en"
VALID_LANGS = (LANG_PL, LANG_EN)
DEFAULT_LANG = LANG_PL

# Every profile is wired to the body-frame Motion engine and continuous auto-hold.
# MOTION_PROFILE_NAME is retained as an ALIAS of the Game profile for any external
# importer that still references the old symbol.
MOTION_PROFILE_NAME = GAME_PROFILE_NAME


@dataclass(frozen=True)
class ActionStep:
    type: str
    key_name: str | None = None
    delay_ms: int = 0

    @classmethod
    def key(cls, key_name: str) -> ActionStep:
        return cls(type="key", key_name=normalize_action_key(key_name))

    @classmethod
    def delay(cls, delay_ms: int) -> ActionStep:
        delay_ms = int(delay_ms)
        if delay_ms < 0:
            raise ValueError("delay_ms must be non-negative")
        return cls(type="delay", delay_ms=min(delay_ms, MAX_MACRO_DELAY_MS))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActionStep:
        step_type = str(data.get("type", "")).strip().lower()
        if step_type == "key":
            return cls.key(str(data["key"]))
        if step_type == "delay":
            return cls.delay(int(data.get("ms", 0)))
        raise ValueError(f"unsupported macro step type: {step_type}")

    def to_dict(self) -> dict[str, Any]:
        if self.type == "key":
            return {"type": "key", "key": self.key_name}
        if self.type == "delay":
            return {"type": "delay", "ms": self.delay_ms}
        raise ValueError(f"unsupported macro step type: {self.type}")


@dataclass(frozen=True)
class ActionBinding:
    type: str
    key_name: str | None = None
    steps: tuple[ActionStep, ...] = ()

    @classmethod
    def disabled(cls) -> ActionBinding:
        return cls(type="disabled")

    @classmethod
    def key(cls, key_name: str) -> ActionBinding:
        return cls(type="key", key_name=normalize_action_key(key_name))

    @classmethod
    def macro(cls, steps: tuple[ActionStep, ...]) -> ActionBinding:
        if not steps:
            raise ValueError("macro requires at least one step")
        return cls(type="macro", steps=tuple(steps))

    @classmethod
    def from_dict(cls, data: dict[str, Any] | str | None) -> ActionBinding:
        if data is None:
            return cls.disabled()
        if isinstance(data, str):
            return cls.key(data)
        binding_type = str(data.get("type", "")).strip().lower()
        if binding_type == "disabled":
            return cls.disabled()
        if binding_type in {"key", "media"}:
            return cls.key(str(data["key"]))
        if binding_type == "macro":
            steps = tuple(ActionStep.from_dict(step) for step in data.get("steps", ()))
            return cls.macro(steps)
        raise ValueError(f"unsupported action type: {binding_type}")

    def to_dict(self) -> dict[str, Any]:
        if self.type == "disabled":
            return {"type": "disabled"}
        if self.type == "key":
            return {"type": "key", "key": self.key_name}
        if self.type == "macro":
            return {"type": "macro", "steps": [step.to_dict() for step in self.steps]}
        raise ValueError(f"unsupported action type: {self.type}")

    @property
    def description(self) -> str:
        if self.type == "disabled":
            return "disabled"
        if self.type == "key":
            return str(self.key_name)
        if self.type == "macro":
            return "macro: " + ", ".join(
                step.key_name if step.type == "key" else f"{step.delay_ms}ms"
                for step in self.steps
            )
        return self.type


def _normalize_action_map(actions: dict[str, ActionBinding]) -> dict[str, ActionBinding]:
    # Keep any binding whose label is a real control label in EITHER vocabulary
    # (discrete GESTURE_LABELS or first-class MOTION_LABELS). The per-profile fold
    # in merged_with_defaults() then narrows each stored body to the labels that
    # profile's engine actually produces, so a stale cross-vocabulary row is dropped
    # rather than lingering as a dead bind.
    normalized_actions: dict[str, ActionBinding] = {}
    for gesture, binding in actions.items():
        canonical = normalize_gesture_label(str(gesture))
        if canonical in ACTION_LABELS:
            normalized_actions[canonical] = binding
    return normalized_actions


@dataclass(frozen=True)
class ActionResult:
    emitted: bool
    description: str
    reason: str


def normalize_turn_sensitivity(value, fallback: float = DEFAULT_GAME_TURN_SENSITIVITY) -> float:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        return fallback


def normalize_tilt_threshold(value, fallback: float = DEFAULT_MOTION_TILT_THRESHOLD) -> float:
    try:
        return round(max(MIN_MOTION_TILT_THRESHOLD, min(MAX_MOTION_TILT_THRESHOLD, float(value))), 1)
    except (TypeError, ValueError):
        return fallback


def normalize_turn_threshold(value, fallback: float = DEFAULT_GAME_TURN_THRESHOLD) -> float:
    try:
        return round(max(MIN_MOTION_TURN_THRESHOLD, min(MAX_MOTION_TURN_THRESHOLD, float(value))), 0)
    except (TypeError, ValueError):
        return fallback


def normalize_mouse_axis_enabled(value, fallback: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
        return fallback
    if isinstance(value, (int, float)):
        return bool(value)
    return fallback


def can_preserve_builtin_action_overrides(profile_name: str, version: int) -> bool:
    minimum = {
        GAME_PROFILE_NAME: GAME_ACTION_OVERRIDE_VERSION,
        MUSIC_PROFILE_NAME: MUSIC_ACTION_OVERRIDE_VERSION,
        MOUSE_PROFILE_NAME: MOUSE_ACTION_OVERRIDE_VERSION,
    }.get(normalize_profile_name(profile_name))
    return minimum is not None and version >= minimum


@dataclass(frozen=True)
class MotionProfileSettings:
    turn_threshold: float = DEFAULT_GAME_TURN_THRESHOLD
    turn_sensitivity: float = DEFAULT_GAME_TURN_SENSITIVITY
    mouse_speed: int = DEFAULT_MOUSE_SPEED
    mouse_axis_enabled: bool = True

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any] | MotionProfileSettings,
        *,
        fallback: MotionProfileSettings | None = None,
    ) -> MotionProfileSettings:
        fallback = fallback or cls()
        if isinstance(data, MotionProfileSettings):
            return cls(
                turn_threshold=normalize_turn_threshold(data.turn_threshold, fallback.turn_threshold),
                turn_sensitivity=normalize_turn_sensitivity(data.turn_sensitivity, fallback.turn_sensitivity),
                mouse_speed=normalize_mouse_speed(data.mouse_speed, fallback.mouse_speed),
                mouse_axis_enabled=normalize_mouse_axis_enabled(
                    data.mouse_axis_enabled,
                    fallback.mouse_axis_enabled,
                ),
            )
        if not isinstance(data, dict):
            return fallback
        return cls(
            turn_threshold=normalize_turn_threshold(
                data.get("turn_threshold", fallback.turn_threshold),
                fallback.turn_threshold,
            ),
            turn_sensitivity=normalize_turn_sensitivity(
                data.get("turn_sensitivity", fallback.turn_sensitivity),
                fallback.turn_sensitivity,
            ),
            mouse_speed=normalize_mouse_speed(
                data.get("mouse_speed", fallback.mouse_speed),
                fallback.mouse_speed,
            ),
            mouse_axis_enabled=normalize_mouse_axis_enabled(
                data.get("mouse_axis_enabled", fallback.mouse_axis_enabled),
                fallback.mouse_axis_enabled,
            ),
        )

    def to_dict(self) -> dict[str, float | int | bool]:
        return {
            "turn_threshold": normalize_turn_threshold(self.turn_threshold),
            "turn_sensitivity": normalize_turn_sensitivity(self.turn_sensitivity),
            "mouse_speed": normalize_mouse_speed(self.mouse_speed),
            "mouse_axis_enabled": normalize_mouse_axis_enabled(self.mouse_axis_enabled),
        }


@dataclass
class TrikiConfig:
    actions: dict[str, ActionBinding] = field(default_factory=dict)
    output_enabled: bool = False
    version: int = CONFIG_VERSION
    profiles: dict[str, dict[str, ActionBinding]] = field(default_factory=dict)
    profile_settings: dict[str, MotionProfileSettings] = field(default_factory=dict)
    active_profile: str = DEFAULT_PROFILE_NAME
    hold_ms: int = 0
    engine: str = DEFAULT_ENGINE
    # UI language for the end-user app: 'pl' (default) or 'en'. Persisted so the
    # maintainer's choice survives restarts. Optional like ``engine`` -- old
    # configs that carry no ``lang`` field load unchanged (default Polish) and
    # CONFIG_VERSION is NOT bumped. /debug stays English regardless.
    lang: str = DEFAULT_LANG

    def merged_with_defaults(self) -> TrikiConfig:
        # Unconditional built-in collapse (runs regardless of stored version).
        # The app ships exactly three built-ins, {Game, Music, Mouse}, which always
        # exist with their new defaults. The nine LEGACY built-ins (Default, WASD
        # Game, Doom, Doom Motion, Doom / Steering, Presentation, 'Which Sausage,
        # Mate?', Experimental Pointer, Media) are DROPPED by name -- the maintainer
        # wants them gone ("if I see 3 Doom profiles again..."). Any OTHER stored
        # profile is a user-created custom one and is KEPT (so the management
        # controls keep working); user overrides to built-ins are folded onto the
        # fresh defaults so Advanced edits persist.
        merged_profiles = default_profile_map()
        for stored_name, stored_actions in self.profiles.items():
            normalized = normalize_profile_name(stored_name)
            body = _normalize_action_map(stored_actions)
            if normalized in BUILTIN_PROFILE_NAMES:
                # User overrides onto a surviving built-in, but only after that
                # profile's action vocabulary/defaults became compatible with the
                # current Game/Motion rows. Later non-action schema bumps must not
                # erase a player's Doom key overrides.
                if can_preserve_builtin_action_overrides(normalized, self.version):
                    merged_profiles[normalized].update(
                        _only_labels(body, labels_for_profile(normalized))
                    )
            elif normalized in _LEGACY_BUILTIN_NAMES:
                # A retired built-in: all legacy bodies are dropped. The active name
                # may still remap (Media->Music), but the old discrete rows no longer
                # survive into Advanced.
                pass
            else:
                # A genuine user-created custom profile: keep it, merged over the
                # same Motion/Game default so every profile has identical bindable
                # controls.
                custom = default_action_map()
                custom.update(_only_labels(body, labels_for_profile(normalized)))
                merged_profiles[normalized] = custom

        merged_profile_settings = {
            name: default_motion_settings_for_profile(name)
            for name in merged_profiles
        }
        for stored_name, stored_settings in self.profile_settings.items():
            normalized = normalize_profile_name(stored_name)
            if normalized in merged_profiles:
                merged_profile_settings[normalized] = MotionProfileSettings.from_dict(
                    stored_settings,
                    fallback=merged_profile_settings[normalized],
                )
        for name in merged_profiles:
            merged_profile_settings.setdefault(name, default_motion_settings_for_profile(name))

        # Resolve the active profile: a name that still exists after the collapse
        # (a built-in or a surviving custom profile) stays active as-is; a dropped
        # legacy built-in name is remapped (Media->Music, everything else->Game).
        stored_active = normalize_profile_name(self.active_profile)
        if stored_active in merged_profiles:
            active_profile = stored_active
        else:
            active_profile = remap_legacy_profile_name(self.active_profile)
        if active_profile not in merged_profiles:
            active_profile = (
                DEFAULT_PROFILE_NAME if DEFAULT_PROFILE_NAME in merged_profiles else next(iter(merged_profiles))
            )
        # The top-level ``actions`` carries the live (possibly just-edited) bindings
        # of the active profile; fold them on so an in-flight edit survives the
        # merge, but only after that profile's action vocabulary is compatible
        # with the current rows. Older legacy bodies (e.g. old 'Default') are
        # discarded so fresh built-in defaults win.
        if (
            self.actions
            and can_preserve_builtin_action_overrides(active_profile, self.version)
            and active_profile in merged_profiles
        ):
            merged_profiles[active_profile].update(
                _only_labels(
                    _normalize_action_map(self.actions),
                    labels_for_profile(active_profile),
                )
            )

        actions = dict(merged_profiles[active_profile])
        # hold_ms is auto-managed: every profile uses the Motion engine and
        # auto-holds continuously (no toggle).
        # hold_ms is auto-managed for Motion profiles; a PRE-version config
        # is reset to 0 so the app re-seeds the current default (old configs carried a
        # 400 ms hold that caused the turn-lag).
        hold_ms = (
            normalize_hold_ms(self.hold_ms)
            if engine_for_profile(active_profile) == ENGINE_MOTION
            and self.hold_ms
            and self.version >= CONFIG_VERSION
            else 0
        )
        # The control engine is a DERIVED property of the active profile: every
        # profile resolves to motion. Deriving it (rather than trusting
        # a possibly-stale stored value) keeps engine and active_profile from ever
        # drifting apart.
        return TrikiConfig(
            actions=actions,
            output_enabled=self.output_enabled,
            version=CONFIG_VERSION,
            profiles=merged_profiles,
            profile_settings=merged_profile_settings,
            active_profile=active_profile,
            hold_ms=hold_ms,
            engine=engine_for_profile(active_profile),
            lang=normalize_lang(self.lang),
        )

    def to_dict(self) -> dict[str, Any]:
        merged = self.merged_with_defaults()
        return {
            "version": merged.version,
            "output_enabled": merged.output_enabled,
            "hold_ms": merged.hold_ms,
            "engine": merged.engine,
            "lang": merged.lang,
            "active_profile": merged.active_profile,
            "actions": {
                gesture: binding.to_dict()
                for gesture, binding in merged.actions.items()
            },
            "profiles": {
                name: {
                    gesture: binding.to_dict()
                    for gesture, binding in actions.items()
                }
                for name, actions in merged.profiles.items()
            },
            "profile_settings": {
                name: settings.to_dict()
                for name, settings in merged.profile_settings.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrikiConfig:
        if not isinstance(data, dict):
            raise ValueError("config must be a JSON object")
        actions = {
            normalize_gesture_label(str(gesture)): ActionBinding.from_dict(binding)
            for gesture, binding in data.get("actions", {}).items()
        }
        profiles = {
            normalize_profile_name(str(name)): {
                normalize_gesture_label(str(gesture)): ActionBinding.from_dict(binding)
                for gesture, binding in profile_actions.items()
            }
            for name, profile_actions in data.get("profiles", {}).items()
        }
        profile_settings = {
            normalize_profile_name(str(name)): MotionProfileSettings.from_dict(settings)
            for name, settings in data.get("profile_settings", {}).items()
        }
        return cls(
            actions=actions,
            output_enabled=bool(data.get("output_enabled", False)),
            version=int(data.get("version", 1)),
            profiles=profiles,
            profile_settings=profile_settings,
            active_profile=normalize_profile_name(str(data.get("active_profile", DEFAULT_PROFILE_NAME))),
            hold_ms=normalize_hold_ms(data.get("hold_ms", 0)),
            engine=normalize_engine(data.get("engine")),
            lang=normalize_lang(data.get("lang")),
        ).merged_with_defaults()


class ActionExecutor:
    def __init__(self, *, key_emitter=None, sleep=None) -> None:
        self.key_emitter = key_emitter if key_emitter is not None else create_default_key_emitter()
        self.sleep = sleep if sleep is not None else time.sleep

    def execute(self, binding: ActionBinding, *, mouse_strength: float | None = None) -> ActionResult:
        try:
            if binding.type == "disabled":
                return ActionResult(False, "disabled", "action disabled")
            if binding.type == "key":
                assert binding.key_name is not None
                scaled_press = getattr(self.key_emitter, "press_key_scaled", None)
                if mouse_strength is not None and scaled_press is not None:
                    scaled_press(binding.key_name, mouse_strength)
                else:
                    self.key_emitter.press_key(binding.key_name)
                return ActionResult(True, binding.key_name, "key emitted")
            if binding.type == "macro":
                for step in binding.steps:
                    if step.type == "key":
                        assert step.key_name is not None
                        self.key_emitter.press_key(step.key_name)
                    elif step.type == "delay":
                        self.sleep(step.delay_ms / 1000.0)
                    else:
                        raise ValueError(f"unsupported macro step type: {step.type}")
                return ActionResult(True, binding.description, "macro emitted")
            raise ValueError(f"unsupported action type: {binding.type}")
        except KeyEmissionError as exc:
            return ActionResult(False, binding.description, f"output error: {exc}")


def default_action_map() -> dict[str, ActionBinding]:
    """Universal profile fallback: same bindable controls/defaults as Game."""
    return dict(_game_action_map())


def _classifier_default_map() -> dict[str, ActionBinding]:
    # Generic discrete-gesture fallback (custom profiles + unknown names). Keyed by
    # the classifier's seven GESTURE_LABELS.
    return {
        "rotate-cw": ActionBinding.key("right"),
        "rotate-ccw": ActionBinding.key("left"),
        "scrub-cw": ActionBinding.key("up"),
        "scrub-ccw": ActionBinding.key("down"),
        "back-forth": ActionBinding.key("space"),
        "lift": ActionBinding.key("ctrl"),
        "flip-over": ActionBinding.key("."),
    }


def _game_action_map() -> dict[str, ActionBinding]:
    # The single "Game" profile, run by the body-frame Motion engine. ROTATION-
    # INVARIANT tank-style controls (a round 6-axis cap has no observable heading):
    #   turn-left  -> left   TWIST the cap flat = steer left
    #   turn-right -> right  TWIST the cap flat = steer right
    #   go         -> up     TILT the cap any way = drive forward (throttle)
    #   stamp      -> ctrl   vertical STAMP = fire (Doom default)
    #   flip       -> shift  cap UPSIDE DOWN holds Shift (run)
    #   scrub-straight -> space    flat SLIDE on the desk = use / open door (Doom
    #                    default). The ONLY scrub (circular was dropped). Mapped to
    #                    "use" (not menu/Esc) on purpose: a stray slide then just bumps
    #                    "use" -- harmless in-game -- instead of yanking you to the menu.
    # All editable in Advanced.
    return {
        "turn-left": ActionBinding.key("left"),
        "turn-right": ActionBinding.key("right"),
        "go": ActionBinding.key("w"),
        "stamp": ActionBinding.key("enter"),
        "flip": ActionBinding.key("shift"),
        "scrub-straight": ActionBinding.key("space"),
    }


def _music_action_map() -> dict[str, ActionBinding]:
    # Same Motion rows as Game, but media-player defaults so the Music slot remains
    # useful out of the box after the v14 Advanced-table cleanup.
    return {
        "turn-left": ActionBinding.key("volume-down"),
        "turn-right": ActionBinding.key("volume-up"),
        "go": ActionBinding.key("media-prev"),
        "stamp": ActionBinding.key("media-play-pause"),
        "flip": ActionBinding.key("volume-mute"),
        "scrub-straight": ActionBinding.key("media-next"),
    }


def _mouse_action_map() -> dict[str, ActionBinding]:
    actions = _game_action_map()
    actions["turn-left"] = ActionBinding.key("mouse-move-left")
    actions["turn-right"] = ActionBinding.key("mouse-move-right")
    return actions


def default_profile_map() -> dict[str, dict[str, ActionBinding]]:
    """The complete built-in profile set: Game (default), Music, and Mouse.
    Every other historical preset (Default, WASD Game, Presentation,
    'Which Sausage, Mate?', Doom, Doom Motion, Doom / Steering, Experimental
    Pointer) is gone."""
    return {
        GAME_PROFILE_NAME: _game_action_map(),
        MUSIC_PROFILE_NAME: _music_action_map(),
        MOUSE_PROFILE_NAME: _mouse_action_map(),
    }


def default_actions_for_profile(profile_name: str) -> dict[str, ActionBinding]:
    normalized = normalize_profile_name(profile_name)
    return dict(default_profile_map().get(normalized, default_action_map()))


def default_motion_settings_for_profile(profile_name: str) -> MotionProfileSettings:
    normalized = normalize_profile_name(profile_name)
    if normalized == MUSIC_PROFILE_NAME:
        return MotionProfileSettings(
            turn_threshold=DEFAULT_MUSIC_TURN_THRESHOLD,
            turn_sensitivity=DEFAULT_MUSIC_TURN_SENSITIVITY,
            mouse_speed=DEFAULT_MOUSE_SPEED,
        )
    return MotionProfileSettings(
        turn_threshold=DEFAULT_GAME_TURN_THRESHOLD,
        turn_sensitivity=DEFAULT_GAME_TURN_SENSITIVITY,
        mouse_speed=DEFAULT_MOUSE_SPEED,
    )


def labels_for_profile(profile_name: str) -> tuple[str, ...]:
    """The bindable control labels Advanced should list for any profile."""
    return MOTION_LABELS


def _only_labels(
    body: dict[str, ActionBinding], allowed: tuple[str, ...]
) -> dict[str, ActionBinding]:
    """Keep only the rows whose label is in ``allowed`` -- used to narrow a stored
    profile body onto the vocabulary its engine produces (drops cross-vocabulary or
    pre-v9 dead rows)."""
    allowed_set = set(allowed)
    return {label: binding for label, binding in body.items() if label in allowed_set}


def parse_macro_text(text: str) -> ActionBinding:
    steps: list[ActionStep] = []
    for raw_part in text.split(","):
        part = raw_part.strip()
        if not part:
            continue
        lowered = part.lower()
        if lowered.endswith("ms"):
            number = lowered[:-2].strip()
            try:
                steps.append(ActionStep.delay(int(number)))
            except ValueError:
                raise ValueError(f"invalid macro delay: {part!r}")
        else:
            steps.append(ActionStep.key(part))
    if not steps:
        raise ValueError('enter at least one key or delay, e.g. "left, 100ms, enter"')
    return ActionBinding.macro(tuple(steps))


def normalize_action_key(key_name: str) -> str:
    normalized = normalize_key_name(key_name)
    return validate_output_name(normalized)


def normalize_profile_name(name: str) -> str:
    cleaned = " ".join(name.strip().split())
    if cleaned.lower() == "myszka":
        return MOUSE_PROFILE_NAME
    return cleaned[:40] if cleaned else DEFAULT_PROFILE_NAME


def normalize_hold_ms(hold_ms: Any) -> int:
    try:
        value = int(hold_ms)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_HOLD_MS, value))


def normalize_engine(engine: Any) -> str:
    """Coerce a stored/selected engine value to a known engine name.

    Unknown / missing values fall back to the Motion engine used by every profile.
    """
    text = str(engine).strip().lower() if engine is not None else ""
    return text if text in VALID_ENGINES else DEFAULT_ENGINE


def normalize_lang(lang: Any) -> str:
    """Coerce a stored/selected UI language to a known value.

    Unknown / missing values fall back to Polish (the default), so old configs
    that carry no ``lang`` field keep the PL-first behaviour.
    """
    text = str(lang).strip().lower() if lang is not None else ""
    return text if text in VALID_LANGS else DEFAULT_LANG


def engine_for_profile(profile_name: str) -> str:
    """The control engine implied by selecting ``profile_name``.

    Every profile runs the body-frame Motion engine (tilt/twist/stamp movement +
    auto-hold). Deriving the engine from the active profile means the stored engine
    value can never drift from the active profile.
    """
    return ENGINE_MOTION


# Fixed remap from legacy profile names to the built-in profile set. The old media
# preset name still selects Music; everything else (WASD Game, Doom, Doom Motion,
# Doom / Steering, Default, Presentation, 'Which Sausage, Mate?', Experimental
# Pointer, and any unknown name) folds onto Game.
_LEGACY_PROFILE_REMAP = {
    "media": MUSIC_PROFILE_NAME,
    "music": MUSIC_PROFILE_NAME,
}

# The retired built-in profile names. A stored profile with one of these names is
# treated as an OLD built-in and dropped on load; any other non-built-in name is a
# user-created custom profile and is kept.
_LEGACY_BUILTIN_NAMES = frozenset(
    {
        "Default",
        "WASD Game",
        "Media",
        "Presentation",
        "Which Sausage, Mate?",
        "Doom",
        "Doom Motion",
        "Doom / Steering",
        "Experimental Pointer",
    }
)


def remap_legacy_profile_name(name: Any) -> str:
    """Map any stored/active profile name into {Game, Music, Mouse}. A name that is
    already a built-in is returned as-is; 'Media' (case-insensitive) -> Music;
    everything else -> Game."""
    normalized = normalize_profile_name(str(name)) if name is not None else DEFAULT_PROFILE_NAME
    if normalized in BUILTIN_PROFILE_NAMES:
        return normalized
    return _LEGACY_PROFILE_REMAP.get(normalized.strip().lower(), GAME_PROFILE_NAME)


def _maps_cleanly_to_builtin(stored_name: str, target: str) -> bool:
    """True when ``stored_name`` is a name whose USER OVERRIDES should be folded
    onto the built-in ``target``: an exact built-in name, or an explicit alias in
    the remap table (e.g. 'Media' -> Music). A generic legacy name (which the
    fallback maps to Game) returns False, so old bodies are dropped rather than
    dumped onto Game."""
    normalized = normalize_profile_name(str(stored_name))
    if normalized in BUILTIN_PROFILE_NAMES:
        return True
    return _LEGACY_PROFILE_REMAP.get(normalized.strip().lower()) == target


def load_config(path: Path) -> TrikiConfig:
    if not path.exists():
        return TrikiConfig().merged_with_defaults()
    try:
        with path.open(encoding="utf-8") as file:
            data = json.load(file)
        return TrikiConfig.from_dict(data)
    except (json.JSONDecodeError, ValueError, TypeError, AttributeError, KeyError, OSError) as exc:
        backup = None
        try:
            backup = path.with_suffix(path.suffix + ".bak")
            shutil.copyfile(path, backup)
        except OSError:
            backup = None
        _logger.warning(
            "Could not read config %s (%s: %s); falling back to defaults%s",
            path, type(exc).__name__, exc,
            f" (backed up to {backup})" if backup else "",
        )
        return TrikiConfig().merged_with_defaults()


def save_config(path: Path, config: TrikiConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as file:
        json.dump(config.to_dict(), file, indent=2, sort_keys=True)
        file.write("\n")
        file.flush()
        os.fsync(file.fileno())
        temporary_path = Path(file.name)
    os.replace(temporary_path, path)


# Sanity (checked once at import, after every binding helper is defined): each
# built-in's default map must be keyed exactly by the vocabulary its engine
# produces, so the Advanced rows, the engine output and the bindings can never
# drift apart.
assert set(_game_action_map()) == set(MOTION_LABELS)
assert set(_music_action_map()) == set(MOTION_LABELS)
assert set(_classifier_default_map()) == set(GESTURE_LABELS)
