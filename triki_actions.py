from __future__ import annotations

import json
import logging
import shutil
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
    MAX_HOLD_MS,
    KeyEmissionError,
    create_default_key_emitter,
    normalize_key_name,
    vk_for_key,
)


_logger = logging.getLogger("triki")


# The app ships EXACTLY two built-in profiles: "Game" (the default) and "Music".
# "Game" runs the body-frame Motion engine and auto-holds continuously; "Music"
# runs the discrete classifier for one-shot media controls.
GAME_PROFILE_NAME = "Game"
MUSIC_PROFILE_NAME = "Music"
DEFAULT_PROFILE_NAME = GAME_PROFILE_NAME
BUILTIN_PROFILE_NAMES = (GAME_PROFILE_NAME, MUSIC_PROFILE_NAME)
# CONFIG_VERSION bumped 9->10 for the calibration-grounded control rebuild: the
# "Game" profile gains the full 10-control MOTION_LABELS set (turn, the two tilt
# axes, stamp, flip, scrub) on fresh WSAD-based defaults. Pre-v10 Game bindings are
# the old scheme, so the version-guarded built-in fold in merged_with_defaults()
# drops them and the fresh defaults win (a one-time reset of the Game binds).
# 12->13: scrub-circular dropped (a round cap can't tell a circle from a line); the
# single surviving scrub-straight is remapped to Space (use/door). The fold drops the
# now-dead scrub-circular bind from older configs.
CONFIG_VERSION = 13
MAX_MACRO_DELAY_MS = 5000  # ceiling for a single macro delay step; legit macros use sub-second delays

# Control engines. "classifier" = the discrete LiveGestureDetector (used by the
# "Music" profile for one-shot twist/stamp media controls). "motion" = the
# body-frame continuous MotionControlEngine (used by the "Game" profile for
# tilt/twist/stamp movement). The engine is a DERIVED property of the active
# profile (see engine_for_profile): Game -> motion, everything else -> classifier,
# so the stored value can never drift from the active profile.
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

# The "Game" profile is the one (and only) preset wired to the body-frame Motion
# engine. Activating it implies engine == "motion" and continuous auto-hold; the
# "Music" profile (and any other name) uses the discrete classifier with hold==0.
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


@dataclass
class TrikiConfig:
    actions: dict[str, ActionBinding] = field(default_factory=dict)
    output_enabled: bool = False
    version: int = CONFIG_VERSION
    profiles: dict[str, dict[str, ActionBinding]] = field(default_factory=dict)
    active_profile: str = DEFAULT_PROFILE_NAME
    hold_ms: int = 0
    engine: str = DEFAULT_ENGINE
    # UI language for the end-user app: 'pl' (default) or 'en'. Persisted so the
    # maintainer's choice survives restarts. Optional like ``engine`` -- old
    # configs that carry no ``lang`` field load unchanged (default Polish) and
    # CONFIG_VERSION is NOT bumped. /debug stays English regardless.
    lang: str = DEFAULT_LANG

    def merged_with_defaults(self) -> TrikiConfig:
        # UNCONDITIONAL 2-built-in collapse (runs regardless of stored version).
        # The app ships EXACTLY two BUILT-IN profiles, {Game, Music}, which always
        # exist with their new defaults. The nine LEGACY built-ins (Default, WASD
        # Game, Doom, Doom Motion, Doom / Steering, Presentation, 'Which Sausage,
        # Mate?', Experimental Pointer; and 'Media', which folds onto Music) are
        # DROPPED by name -- the maintainer wants them gone ("if I see 3 Doom
        # profiles again..."). Any OTHER stored profile is a user-created custom
        # one and is KEPT (so the management controls keep working); user OVERRIDES
        # to Game/Music are folded onto the fresh defaults so Advanced edits persist.
        merged_profiles = default_profile_map()  # the two built-ins, fresh
        for stored_name, stored_actions in self.profiles.items():
            normalized = normalize_profile_name(stored_name)
            body = _normalize_action_map(stored_actions)
            if normalized in BUILTIN_PROFILE_NAMES:
                # User overrides onto a surviving built-in -- but only for a
                # CURRENT-version config, and only rows in THAT built-in's own
                # vocabulary (Game=MOTION_LABELS, Music=GESTURE_LABELS). A pre-vN
                # built-in body is the OLD scheme/defaults, so it is dropped and the
                # fresh defaults win (one-time reset on the version bump).
                if self.version >= CONFIG_VERSION:
                    merged_profiles[normalized].update(
                        _only_labels(body, labels_for_profile(normalized))
                    )
            elif normalized in _LEGACY_BUILTIN_NAMES:
                # A retired built-in: 'Media' carries its (discrete) overrides onto
                # Music; every other legacy built-in is dropped entirely.
                if remap_legacy_profile_name(normalized) == MUSIC_PROFILE_NAME:
                    merged_profiles[MUSIC_PROFILE_NAME].update(
                        _only_labels(body, GESTURE_LABELS)
                    )
            else:
                # A genuine user-created custom profile (runs the discrete
                # classifier): keep it, merged over the discrete default so every
                # gesture row is populated.
                custom = default_action_map()
                custom.update(_only_labels(body, GESTURE_LABELS))
                merged_profiles[normalized] = custom

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
        # merge -- but ONLY for a current-version config. In a v8 config ``actions``
        # mirrors the active profile's live edits; in a LEGACY config it is the body
        # of a dropped legacy profile (e.g. the old 'Default') and must be discarded
        # so the fresh built-in defaults win.
        if (
            self.actions
            and self.version >= CONFIG_VERSION
            and active_profile in merged_profiles
        ):
            merged_profiles[active_profile].update(
                _only_labels(
                    _normalize_action_map(self.actions),
                    labels_for_profile(active_profile),
                )
            )

        actions = dict(merged_profiles[active_profile])
        # hold_ms is auto-managed: the Game profile (Motion engine) auto-holds
        # continuously (no toggle); Music (classifier) uses one-shot taps (0).
        # hold_ms is auto-managed for the Motion (Game) profile; a PRE-version config
        # is reset to 0 so the app re-seeds the current default (old configs carried a
        # 400 ms hold that caused the turn-lag).
        hold_ms = (
            normalize_hold_ms(self.hold_ms)
            if engine_for_profile(active_profile) == ENGINE_MOTION
            and self.hold_ms
            and self.version >= CONFIG_VERSION
            else 0
        )
        # The control engine is a DERIVED property of the active profile: Game ->
        # motion, everything else -> classifier. Deriving it (rather than trusting
        # a possibly-stale stored value) keeps engine and active_profile from ever
        # drifting apart.
        return TrikiConfig(
            actions=actions,
            output_enabled=self.output_enabled,
            version=CONFIG_VERSION,
            profiles=merged_profiles,
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
        return cls(
            actions=actions,
            output_enabled=bool(data.get("output_enabled", False)),
            version=int(data.get("version", 1)),
            profiles=profiles,
            active_profile=normalize_profile_name(str(data.get("active_profile", DEFAULT_PROFILE_NAME))),
            hold_ms=normalize_hold_ms(data.get("hold_ms", 0)),
            engine=normalize_engine(data.get("engine")),
            lang=normalize_lang(data.get("lang")),
        ).merged_with_defaults()


class ActionExecutor:
    def __init__(self, *, key_emitter=None, sleep=None) -> None:
        self.key_emitter = key_emitter if key_emitter is not None else create_default_key_emitter()
        self.sleep = sleep if sleep is not None else time.sleep

    def execute(self, binding: ActionBinding) -> ActionResult:
        try:
            if binding.type == "disabled":
                return ActionResult(False, "disabled", "action disabled")
            if binding.type == "key":
                assert binding.key_name is not None
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
    """The discrete-classifier default map -- the safety fallback for any profile
    that is NOT a built-in (i.e. a user-created custom profile, which runs the
    classifier). Keyed by the seven GESTURE_LABELS, on Doom-default movement keys
    so a stray classifier gesture still does something sane; fully editable."""
    return dict(_classifier_default_map())


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
    # The single "Music" profile, run by the discrete classifier (NOT the Motion
    # engine) so twist=volume and stamp=play/pause work as reliable one-shots.
    #   rotate-cw/ccw -> volume up/down   (TWIST)
    #   scrub-cw/ccw  -> media next/prev  (HIDDEN in Advanced: round-cap scrub is
    #                    unreliable spin; kept here only so a stray scrub maps to a
    #                    harmless media step, never shown as a bindable row)
    #   back-forth    -> media play/pause (shake)
    #   lift          -> media play/pause (STAMP)
    #   flip-over     -> volume mute
    return {
        "rotate-cw": ActionBinding.key("volume-up"),
        "rotate-ccw": ActionBinding.key("volume-down"),
        "scrub-cw": ActionBinding.key("media-next"),
        "scrub-ccw": ActionBinding.key("media-prev"),
        "back-forth": ActionBinding.key("media-play-pause"),
        "lift": ActionBinding.key("media-play-pause"),
        "flip-over": ActionBinding.key("volume-mute"),
    }


def default_profile_map() -> dict[str, dict[str, ActionBinding]]:
    """The complete built-in profile set: EXACTLY two -- Game (default) and
    Music. Every other historical preset (Default, WASD Game, Presentation,
    'Which Sausage, Mate?', Doom, Doom Motion, Doom / Steering, Experimental
    Pointer) is gone."""
    return {
        GAME_PROFILE_NAME: _game_action_map(),
        MUSIC_PROFILE_NAME: _music_action_map(),
    }


def default_actions_for_profile(profile_name: str) -> dict[str, ActionBinding]:
    # Built-ins get their own map (Game=motion labels, Music=gesture labels); any
    # other (custom) profile runs the classifier, so it falls back to the discrete
    # default map -- never the Game/motion vocabulary.
    normalized = normalize_profile_name(profile_name)
    return dict(default_profile_map().get(normalized, default_action_map()))


def labels_for_profile(profile_name: str) -> tuple[str, ...]:
    """The bindable control labels a profile's engine actually produces -- the
    exact rows Advanced should list for it. MOTION_LABELS for the Game (Motion)
    profile; GESTURE_LABELS for Music and every discrete-classifier profile. Also
    used to filter a stored body onto the right vocabulary during migration."""
    return MOTION_LABELS if engine_for_profile(profile_name) == ENGINE_MOTION else GESTURE_LABELS


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
    vk_for_key(normalized)
    return normalized


def normalize_profile_name(name: str) -> str:
    cleaned = " ".join(name.strip().split())
    return cleaned[:40] if cleaned else DEFAULT_PROFILE_NAME


def normalize_hold_ms(hold_ms: Any) -> int:
    try:
        value = int(hold_ms)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_HOLD_MS, value))


def normalize_engine(engine: Any) -> str:
    """Coerce a stored/selected engine value to a known engine name.

    Unknown / missing values fall back to the classifier so old configs (which
    carry no ``engine`` field at all) keep their existing behaviour and never
    accidentally activate Motion mode.
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

    The "Game" profile runs the body-frame Motion engine (tilt/twist/stamp
    movement + auto-hold); every other profile -- "Music" and any unknown name --
    uses the discrete classifier. Deriving the engine from the active profile
    means the stored engine value can never drift from the active profile.
    """
    return ENGINE_MOTION if normalize_profile_name(profile_name) == GAME_PROFILE_NAME else ENGINE_CLASSIFIER


# Fixed remap from any legacy profile name to the 2-profile world. The old media
# preset folds onto Music; everything else (WASD Game, Doom, Doom Motion, Doom /
# Steering, Default, Presentation, 'Which Sausage, Mate?', Experimental Pointer,
# and any unknown name) folds onto Game.
_LEGACY_PROFILE_REMAP = {
    "media": MUSIC_PROFILE_NAME,
    "music": MUSIC_PROFILE_NAME,
}

# The retired built-in profile names. A stored profile with one of these names is
# treated as an OLD built-in and dropped on load (Media's overrides fold onto
# Music); any other non-built-in name is a user-created custom profile and is kept.
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
    """Map any stored/active profile name into {Game, Music}. A name that is
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
    with path.open("w", encoding="utf-8") as file:
        json.dump(config.to_dict(), file, indent=2, sort_keys=True)
        file.write("\n")


# Sanity (checked once at import, after every binding helper is defined): each
# built-in's default map must be keyed exactly by the vocabulary its engine
# produces, so the Advanced rows, the engine output and the bindings can never
# drift apart.
assert set(_game_action_map()) == set(MOTION_LABELS)
assert set(_music_action_map()) == set(GESTURE_LABELS)
assert set(_classifier_default_map()) == set(GESTURE_LABELS)
