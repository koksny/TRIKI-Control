from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from triki_gestures import GESTURE_LABELS, normalize_gesture_label
from triki_key_emitter import (
    KeyEmissionError,
    create_default_key_emitter,
    normalize_key_name,
    vk_for_key,
)


DEFAULT_PROFILE_NAME = "Default"
CONFIG_VERSION = 5


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
        if delay_ms < 0:
            raise ValueError("delay_ms must be non-negative")
        return cls(type="delay", delay_ms=delay_ms)

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
    normalized_actions: dict[str, ActionBinding] = {}
    for gesture, binding in actions.items():
        canonical = normalize_gesture_label(str(gesture))
        if canonical in GESTURE_LABELS:
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

    def merged_with_defaults(self) -> TrikiConfig:
        active_profile = normalize_profile_name(self.active_profile)
        profiles = {
            normalize_profile_name(name): _normalize_action_map(actions)
            for name, actions in self.profiles.items()
            if normalize_profile_name(name)
        }
        if not profiles:
            if self.actions:
                profiles[active_profile] = _normalize_action_map(self.actions)
                if self.version < CONFIG_VERSION:
                    migrated_profiles = default_profile_map()
                    migrated_profiles[DEFAULT_PROFILE_NAME].update(_normalize_action_map(self.actions))
                    profiles = migrated_profiles
                    active_profile = DEFAULT_PROFILE_NAME
            else:
                profiles = default_profile_map()
                if active_profile not in profiles:
                    active_profile = DEFAULT_PROFILE_NAME
        elif self.version < CONFIG_VERSION:
            migrated_profiles = default_profile_map()
            for name, profile_actions in profiles.items():
                if name in migrated_profiles:
                    migrated_profiles[name].update(
                        _custom_profile_overrides_since_config_4(name, profile_actions)
                    )
                else:
                    migrated_profiles[name] = profile_actions
            profiles = migrated_profiles

        if active_profile not in profiles:
            if self.actions:
                profiles[active_profile] = _normalize_action_map(self.actions)
            else:
                active_profile = DEFAULT_PROFILE_NAME if DEFAULT_PROFILE_NAME in profiles else next(iter(profiles))

        merged_profiles: dict[str, dict[str, ActionBinding]] = {}
        for name, profile_actions in profiles.items():
            merged_actions = default_actions_for_profile(name)
            merged_actions.update(profile_actions)
            merged_profiles[name] = merged_actions

        actions = dict(merged_profiles[active_profile])
        return TrikiConfig(
            actions=actions,
            output_enabled=self.output_enabled,
            version=max(CONFIG_VERSION, self.version),
            profiles=merged_profiles,
            active_profile=active_profile,
        )

    def to_dict(self) -> dict[str, Any]:
        merged = self.merged_with_defaults()
        return {
            "version": merged.version,
            "output_enabled": merged.output_enabled,
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
    return {
        "rotate-cw": ActionBinding.key("right"),
        "rotate-ccw": ActionBinding.key("left"),
        "scrub-cw": ActionBinding.key("page-down"),
        "scrub-ccw": ActionBinding.key("page-up"),
        "back-forth": ActionBinding.key("escape"),
        "lift": ActionBinding.key("enter"),
        "flip-over": ActionBinding.key("space"),
    }


def default_profile_map() -> dict[str, dict[str, ActionBinding]]:
    return {
        "Default": default_action_map(),
        "WASD Game": {
            "rotate-cw": ActionBinding.key("d"),
            "rotate-ccw": ActionBinding.key("a"),
            "scrub-cw": ActionBinding.key("w"),
            "scrub-ccw": ActionBinding.key("s"),
            "back-forth": ActionBinding.key("escape"),
            "lift": ActionBinding.key("w"),
            "flip-over": ActionBinding.key("space"),
        },
        "Media": {
            "rotate-cw": ActionBinding.key("volume-up"),
            "rotate-ccw": ActionBinding.key("volume-down"),
            "scrub-cw": ActionBinding.key("media-next"),
            "scrub-ccw": ActionBinding.key("media-prev"),
            "back-forth": ActionBinding.key("media-play-pause"),
            "lift": ActionBinding.key("media-play-pause"),
            "flip-over": ActionBinding.key("volume-mute"),
        },
        "Presentation": {
            "rotate-cw": ActionBinding.key("right"),
            "rotate-ccw": ActionBinding.key("left"),
            "scrub-cw": ActionBinding.disabled(),
            "scrub-ccw": ActionBinding.disabled(),
            "back-forth": ActionBinding.key("escape"),
            "lift": ActionBinding.key("space"),
            "flip-over": ActionBinding.key("escape"),
        },
        "Which Sausage, Mate?": {
            "rotate-cw": ActionBinding.key("right"),
            "rotate-ccw": ActionBinding.key("left"),
            "scrub-cw": ActionBinding.key("="),
            "scrub-ccw": ActionBinding.key("z"),
            "back-forth": ActionBinding.key("backspace"),
            "lift": ActionBinding.key("enter"),
            "flip-over": ActionBinding.key("space"),
        },
    }


def _config_4_default_action_map() -> dict[str, ActionBinding]:
    return {
        "rotate-cw": ActionBinding.key("right"),
        "rotate-ccw": ActionBinding.key("left"),
        "scrub-cw": ActionBinding.key("right"),
        "scrub-ccw": ActionBinding.key("left"),
        "back-forth": ActionBinding.key("escape"),
        "lift": ActionBinding.key("enter"),
        "flip-over": ActionBinding.key("space"),
    }


def _config_4_default_profile_map() -> dict[str, dict[str, ActionBinding]]:
    return {
        "Default": _config_4_default_action_map(),
        "WASD Game": {
            "rotate-cw": ActionBinding.key("d"),
            "rotate-ccw": ActionBinding.key("a"),
            "scrub-cw": ActionBinding.key("d"),
            "scrub-ccw": ActionBinding.key("a"),
            "back-forth": ActionBinding.key("escape"),
            "lift": ActionBinding.key("w"),
            "flip-over": ActionBinding.key("space"),
        },
        "Media": {
            "rotate-cw": ActionBinding.key("volume-up"),
            "rotate-ccw": ActionBinding.key("volume-down"),
            "scrub-cw": ActionBinding.key("media-next"),
            "scrub-ccw": ActionBinding.key("media-prev"),
            "back-forth": ActionBinding.key("media-play-pause"),
            "lift": ActionBinding.key("media-play-pause"),
            "flip-over": ActionBinding.key("volume-mute"),
        },
        "Presentation": {
            "rotate-cw": ActionBinding.key("right"),
            "rotate-ccw": ActionBinding.key("left"),
            "scrub-cw": ActionBinding.key("right"),
            "scrub-ccw": ActionBinding.key("left"),
            "back-forth": ActionBinding.key("escape"),
            "lift": ActionBinding.key("space"),
            "flip-over": ActionBinding.key("escape"),
        },
    }


def _custom_profile_overrides_since_config_4(
    profile_name: str,
    profile_actions: dict[str, ActionBinding],
) -> dict[str, ActionBinding]:
    legacy_defaults = _config_4_default_profile_map().get(normalize_profile_name(profile_name))
    if legacy_defaults is None:
        return profile_actions
    return {
        gesture: binding
        for gesture, binding in profile_actions.items()
        if legacy_defaults.get(gesture) != binding
    }


def default_actions_for_profile(profile_name: str) -> dict[str, ActionBinding]:
    normalized = normalize_profile_name(profile_name)
    return dict(default_profile_map().get(normalized, default_action_map()))


def parse_macro_text(text: str) -> ActionBinding:
    steps: list[ActionStep] = []
    for raw_part in text.split(","):
        part = raw_part.strip()
        if not part:
            continue
        lowered = part.lower()
        if lowered.endswith("ms"):
            steps.append(ActionStep.delay(int(lowered[:-2].strip())))
        else:
            steps.append(ActionStep.key(part))
    return ActionBinding.macro(tuple(steps))


def normalize_action_key(key_name: str) -> str:
    normalized = normalize_key_name(key_name)
    vk_for_key(normalized)
    return normalized


def normalize_profile_name(name: str) -> str:
    cleaned = " ".join(name.strip().split())
    return cleaned[:40] if cleaned else DEFAULT_PROFILE_NAME


def load_config(path: Path) -> TrikiConfig:
    if not path.exists():
        return TrikiConfig().merged_with_defaults()
    with path.open(encoding="utf-8") as file:
        return TrikiConfig.from_dict(json.load(file))


def save_config(path: Path, config: TrikiConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(config.to_dict(), file, indent=2, sort_keys=True)
        file.write("\n")
