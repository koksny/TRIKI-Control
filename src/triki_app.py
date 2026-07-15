from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import threading
import time
import webbrowser
from collections.abc import Sequence
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from triki_actions import (
    ActionBinding,
    ActionExecutor,
    ENGINE_MOTION,
    TrikiConfig,
    default_actions_for_profile,
    default_motion_settings_for_profile,
    engine_for_profile,
    labels_for_profile,
    load_config,
    normalize_gesture_label,
    normalize_hold_ms,
    normalize_lang,
    normalize_mouse_speed,
    normalize_profile_name,
    normalize_tilt_threshold,
    normalize_turn_threshold,
    normalize_turn_sensitivity,
    parse_macro_text,
    save_config,
)
from triki_assets import CAP_FRONT_DATA_URI, CAP_REVERSE_DATA_URI, CAP_SIDE_DATA_URI
from triki_calibration_server import ConnectionControl, EventBus, encode_sse, quiet_stream_errors
from triki_battery import battery_snapshot, normalize_battery_percent
from triki_key_emitter import (
    DEFAULT_HOLD_MS,
    HoldKeyEmitter,
    NullKeyEmitter,
    create_default_key_emitter,
)
from triki_live import LiveGestureDetector
from triki_motion_engine import MotionControlEngine
from triki_play import BleCommandBridge, play_button_hint, run_ble_stream
from triki_diagnostics import collect_diagnostics
from triki_metadata import APP_CREATOR, APP_LICENSE, APP_NAME, APP_VERSION, APP_WEBSITE
from triki_logging import SessionLogger


_DEFAULT_CONSOLE_STREAM = object()

# Control payloads (profiles/macros) are small JSON; bound the request body so a
# bogus or hostile Content-Length cannot make the handler read megabytes.
MAX_CONTROL_BODY_BYTES = 256 * 1024
# Cap how many profiles a single import may carry. merged_with_defaults() always
# injects the built-ins, so a normal export/import stays well under this limit.
MAX_IMPORT_PROFILES = 64
OUTPUT_ACTIVE_STATUSES = frozenset({"connected", "ready"})

# Default key-hold for the Motion engine. The engine re-emits the active intent
# every sample, so the hold only needs to BRIDGE the gap between samples to stay
# continuous -- it must NOT be long, or the key keeps firing after you stop (the
# turn-lag the maintainer felt: a 400 ms hold = the character kept turning ~0.4 s
# after the cap stopped). Real BLE delivery is bursty/jittery (measured inter-sample
# gap p90~60 ms, p99~115 ms), so 90 ms let ~1% of gaps drop the key MID-gesture ->
# an in-game stutter even though the app viz (driven by the raw samples) looked
# continuous. 120 ms bridges the p99 gap -> smooth holds, with only ~120 ms of
# over-travel when you stop (well under the 400 ms that felt laggy).
DEFAULT_MOTION_HOLD_MS = 120
APP_ICON_TRAY_ASSET = Path("assets") / "triki-control-icon-tray.png"


def apply_motion_profile_settings(engine, settings) -> None:
    if engine is None:
        return
    try:
        setter = getattr(engine, "set_turn_sensitivity", None)
        if setter is not None:
            setter(settings.turn_sensitivity)
        threshold_setter = getattr(engine, "set_turn_threshold", None)
        if threshold_setter is not None:
            threshold_setter(settings.turn_threshold)
    except Exception:
        pass


def apply_output_profile_settings(emitter, settings) -> None:
    try:
        setter = getattr(emitter, "set_mouse_speed", None)
        if setter is not None:
            setter(settings.mouse_speed)
    except Exception:
        pass


class AppSession:
    def __init__(
        self,
        *,
        config: TrikiConfig | None = None,
        config_path: Path | None = None,
        executor: ActionExecutor | None = None,
        logger=None,
    ) -> None:
        self.config_path = config_path
        self.logger = logger
        self.config = (
            config.merged_with_defaults()
            if config is not None
            else load_config(config_path) if config_path is not None
            else TrikiConfig().merged_with_defaults()
        )
        self.executor = executor if executor is not None else ActionExecutor()
        self._lock = threading.RLock()
        self.status = "idle"
        self.message = "Click Pair TRIKI to connect, then choose a profile."
        self._status_sequence = 0
        self._sample_count = 0
        self._gesture_count = 0
        self._action_count = 0
        self._action_revision = 1
        self._recent_events: list[dict] = []
        self._connection_log: list[dict] = []
        self._battery_percent: int | None = None
        self._battery_message = "Battery level unknown."
        # The LIVE body-frame Motion engine (set by the BLE wiring in main()) so
        # snapshot() can surface its body-frame tilt diagnostics (hd/he/tilt/fire).
        # None for the classifier path and in tests that drive an engine directly.
        # There is NO calibration anywhere -- neutral is auto-captured at connect
        # and re-centred when the cap is still, so the engine has no calibrate().
        self._motion_engine = None

    def set_motion_engine(self, engine) -> None:
        """Attach the live control engine so snapshot() can read its body-frame
        tilt diagnostics (hd/he/tilt/fire). Accepts any object (the classifier has
        no such diagnostics, which is fine -- the reads are null-safe)."""
        with self._lock:
            self._motion_engine = engine
            self._apply_motion_settings_locked()

    def _active_motion_settings_locked(self):
        return self.config.profile_settings.get(
            self.config.active_profile,
            default_motion_settings_for_profile(self.config.active_profile),
        )

    def _set_active_motion_settings_locked(
        self,
        *,
        turn_threshold=None,
        turn_sensitivity=None,
        mouse_speed=None,
    ) -> None:
        current = self._active_motion_settings_locked()
        self.config.profile_settings[self.config.active_profile] = type(current)(
            turn_threshold=(
                normalize_turn_threshold(turn_threshold, current.turn_threshold)
                if turn_threshold is not None
                else current.turn_threshold
            ),
            turn_sensitivity=(
                normalize_turn_sensitivity(turn_sensitivity)
                if turn_sensitivity is not None
                else current.turn_sensitivity
            ),
            mouse_speed=(
                normalize_mouse_speed(mouse_speed, current.mouse_speed)
                if mouse_speed is not None
                else current.mouse_speed
            ),
        )

    def _apply_motion_settings_locked(self) -> None:
        settings = self._active_motion_settings_locked()
        apply_motion_profile_settings(self._motion_engine, settings)
        apply_output_profile_settings(self.executor.key_emitter, settings)

    def set_status(self, status: str, message: str) -> dict:
        with self._lock:
            output_was_enabled = self.config.output_enabled
            if status not in OUTPUT_ACTIVE_STATUSES:
                self.config.output_enabled = False
                self._release_held_keys()
            self.status = status
            self.message = message
            self._status_sequence += 1
            self._connection_log.append(
                {
                    "sequence": self._status_sequence,
                    "status": status,
                    "message": message,
                    "timestamp": time.strftime("%H:%M:%S"),
                }
            )
            self._connection_log = self._connection_log[-80:]
            if self.logger is not None:
                self.logger.log("status", {"status": status, "message": message})
                if output_was_enabled and not self.config.output_enabled:
                    self.logger.log("output", {"enabled": False, "reason": status})
            if output_was_enabled and not self.config.output_enabled:
                self._save_config()
            return self.snapshot()

    def set_battery_level(self, percent: int | None, message: str = "") -> dict:
        with self._lock:
            self._battery_percent = normalize_battery_percent(percent)
            self._battery_message = message or (
                "Battery level unknown."
                if self._battery_percent is None
                else "Battery level read from BLE."
            )
            return self.snapshot()

    def set_output_enabled(self, enabled: bool) -> dict:
        with self._lock:
            self.config.output_enabled = enabled
            if not enabled:
                self._release_held_keys()
            if self.logger is not None:
                self.logger.log("output", {"enabled": enabled})
            self._save_config()
            return self.snapshot()

    def set_hold_ms(self, hold_ms: int) -> dict:
        with self._lock:
            value = normalize_hold_ms(hold_ms)
            self.config.hold_ms = value
            apply_hold = getattr(self.executor.key_emitter, "set_hold_ms", None)
            if apply_hold is not None:
                apply_hold(value)
            if self.logger is not None:
                self.logger.log("hold_ms", {"hold_ms": value})
            self._save_config()
            return self.snapshot()

    def set_tilt_threshold(self, threshold) -> dict:
        """Set the body-frame Motion engine's lean ENGAGE threshold (degrees).

        Updates the LIVE engine's ``tilt_on`` (and its hysteresis release
        ``tilt_off`` proportionally, preserving the tuned ~0.62 ratio) so the
        grown-up can make the cap touchier/stiffer with NO calibration. Clamped to
        a sane 3..30 deg band. Null-safe: a no-op snapshot if no Motion engine is
        attached (the classifier path has no tilt threshold)."""
        value = normalize_tilt_threshold(threshold)
        with self._lock:
            engine = self._motion_engine
        if engine is not None and hasattr(engine, "tilt_on"):
            try:
                ratio = 0.6
                old_on = float(getattr(engine, "tilt_on", 0.0) or 0.0)
                old_off = float(getattr(engine, "tilt_off", 0.0) or 0.0)
                if old_on > 0:
                    ratio = max(0.2, min(0.9, old_off / old_on))
                engine.tilt_on = value
                if hasattr(engine, "tilt_off"):
                    engine.tilt_off = round(value * ratio, 2)
            except Exception:
                pass
        with self._lock:
            if self.logger is not None:
                self.logger.log("tilt", {"threshold": value})
            return self.snapshot()

    def set_turn_threshold(self, value) -> dict:
        """Set the Motion engine's TURN engage threshold in raw gyro units.

        Lower values make a twist count sooner; this is profile-specific and
        independent from the lean/tilt threshold."""
        value = normalize_turn_threshold(value)
        with self._lock:
            self._set_active_motion_settings_locked(turn_threshold=value)
            self._apply_motion_settings_locked()
            if self.logger is not None:
                self.logger.log("turn_threshold", {"value": value, "profile": self.config.active_profile})
            self._save_config()
            return self.snapshot()

    def set_turn_sensitivity(self, value) -> dict:
        """Set the Motion engine's TURN sensitivity (0..100). Higher = the cap turns
        on a gentler twist. The maintainer found the default too touchy / too fast;
        this is the Advanced slider."""
        value = normalize_turn_sensitivity(value)
        with self._lock:
            self._set_active_motion_settings_locked(turn_sensitivity=value)
            self._apply_motion_settings_locked()
            if self.logger is not None:
                self.logger.log("turn_sensitivity", {"value": value, "profile": self.config.active_profile})
            self._save_config()
            return self.snapshot()

    def set_mouse_speed(self, value) -> dict:
        value = normalize_mouse_speed(value)
        with self._lock:
            self._set_active_motion_settings_locked(mouse_speed=value)
            self._apply_motion_settings_locked()
            if self.logger is not None:
                self.logger.log(
                    "mouse_speed",
                    {"value": value, "profile": self.config.active_profile},
                )
            self._save_config()
            return self.snapshot()

    def set_lang(self, lang: str) -> dict:
        """Persist the end-user UI language ('pl' default / 'en').

        Unknown values fall back to Polish via ``normalize_lang``. The setting is
        round-tripped through the config so it survives restarts; /debug stays
        English regardless."""
        with self._lock:
            self.config.lang = normalize_lang(lang)
            if self.logger is not None:
                self.logger.log("lang", {"lang": self.config.lang})
            self._save_config()
            return self.snapshot()

    def _release_held_keys(self) -> None:
        release = getattr(self.executor.key_emitter, "release_all", None)
        if release is not None:
            release()

    def update_action(self, gesture_label: str, binding: ActionBinding) -> dict:
        gesture_label = normalize_gesture_label(gesture_label)
        with self._lock:
            # A control is only bindable if the ACTIVE profile exposes it in
            # Advanced. Every profile now uses the same Motion/Game vocabulary, so
            # old classifier-only labels are rejected instead of lingering.
            allowed = labels_for_profile(self.config.active_profile)
            if gesture_label not in allowed:
                raise ValueError(
                    f"control {gesture_label!r} is not bindable in profile "
                    f"{self.config.active_profile!r}"
                )
            self.config.actions[gesture_label] = binding
            self.config.profiles[self.config.active_profile] = dict(self.config.actions)
            self._action_revision += 1
            self._save_config()
            return self.snapshot()

    def create_profile(self, name: str) -> dict:
        profile_name = normalize_profile_name(name)
        with self._lock:
            if profile_name in self.config.profiles:
                raise ValueError(f"profile already exists: {profile_name}")
            # A new custom profile starts from the same Motion/Game defaults as the
            # built-ins, so Advanced shows one consistent action table everywhere.
            self.config.profiles[profile_name] = default_actions_for_profile(profile_name)
            self.config.profile_settings[profile_name] = default_motion_settings_for_profile(profile_name)
            self.config.active_profile = profile_name
            self.config.actions = dict(self.config.profiles[profile_name])
            # Keep the derived control engine in sync with the active profile so a
            # later snapshot / BLE wiring never reads a stale engine.
            self.config.engine = engine_for_profile(profile_name)
            self._apply_motion_settings_locked()
            self._action_revision += 1
            self._save_config()
            return self.snapshot()

    def switch_profile(self, name: str) -> dict:
        profile_name = normalize_profile_name(name)
        with self._lock:
            if profile_name not in self.config.profiles:
                raise ValueError(f"unknown profile: {profile_name}")
            self._release_held_keys()
            self.config.active_profile = profile_name
            self.config.actions = dict(self.config.profiles[profile_name])
            self.config.engine = engine_for_profile(profile_name)
            self._apply_motion_settings_locked()
            self._action_revision += 1
            if self.logger is not None:
                self.logger.log("profile", {"active_profile": profile_name})
            self._save_config()
            return self.snapshot()

    def delete_profile(self, name: str) -> dict:
        profile_name = normalize_profile_name(name)
        with self._lock:
            if profile_name not in self.config.profiles:
                raise ValueError(f"unknown profile: {profile_name}")
            if len(self.config.profiles) == 1:
                raise ValueError("cannot delete the last profile")
            del self.config.profiles[profile_name]
            if self.config.active_profile == profile_name:
                self.config.active_profile = next(iter(self.config.profiles))
                self.config.actions = dict(self.config.profiles[self.config.active_profile])
                self.config.engine = engine_for_profile(self.config.active_profile)
            self.config.profile_settings.pop(profile_name, None)
            self._apply_motion_settings_locked()
            self._action_revision += 1
            self._save_config()
            return self.snapshot()

    def export_profiles(self) -> dict:
        with self._lock:
            config = self.config.merged_with_defaults()
            return {
                "version": config.version,
                "active_profile": config.active_profile,
                "profiles": {
                    name: {
                        gesture: binding.to_dict()
                        for gesture, binding in actions.items()
                    }
                    for name, actions in config.profiles.items()
                },
                "profile_settings": {
                    name: config.profile_settings[name].to_dict()
                    for name in config.profiles
                },
            }

    def import_profiles(self, data: dict, *, replace: bool = False) -> dict:
        if not isinstance(data, dict):
            raise ValueError("profile import requires a JSON object")
        incoming = TrikiConfig.from_dict(data).merged_with_defaults()
        if len(incoming.profiles) > MAX_IMPORT_PROFILES:
            raise ValueError(
                f"profile import exceeds the maximum of {MAX_IMPORT_PROFILES} profiles"
            )
        with self._lock:
            if replace:
                self._release_held_keys()
                self.config.profiles = {
                    name: dict(actions)
                    for name, actions in incoming.profiles.items()
                }
                self.config.profile_settings = dict(incoming.profile_settings)
            else:
                self.config.profiles.update(
                    {
                        name: dict(actions)
                        for name, actions in incoming.profiles.items()
                    }
                )
                self.config.profile_settings.update(incoming.profile_settings)
            self.config.active_profile = (
                incoming.active_profile
                if incoming.active_profile in self.config.profiles
                else next(iter(self.config.profiles))
            )
            self.config.actions = dict(self.config.profiles[self.config.active_profile])
            self.config.engine = engine_for_profile(self.config.active_profile)
            self._apply_motion_settings_locked()
            self._action_revision += 1
            self._save_config()
            return self.snapshot()

    def reset_all_profiles(self) -> dict:
        with self._lock:
            self._release_held_keys()
            output_enabled = self.config.output_enabled
            self.config = TrikiConfig(output_enabled=output_enabled).merged_with_defaults()
            self._apply_motion_settings_locked()
            self._action_revision += 1
            self._save_config()
            return self.snapshot()

    def reset_active_profile(self) -> dict:
        with self._lock:
            self.config.actions = default_actions_for_profile(self.config.active_profile)
            self.config.profiles[self.config.active_profile] = dict(self.config.actions)
            self.config.profile_settings[self.config.active_profile] = default_motion_settings_for_profile(
                self.config.active_profile
            )
            self._apply_motion_settings_locked()
            self._action_revision += 1
            self._save_config()
            return self.snapshot()

    def clear_events(self) -> dict:
        with self._lock:
            self._recent_events.clear()
            self._gesture_count = 0
            self._action_count = 0
            return self.snapshot()

    def test_key_output(self, key_name: str) -> dict:
        result = self.executor.execute(ActionBinding.key(key_name))
        with self._lock:
            event = {
                "type": "output-test",
                "gesture_label": "",
                "action_description": result.description,
                "action_emitted": result.emitted,
                "output_enabled": self.config.output_enabled,
                "output_reason": result.reason,
            }
            self._recent_events.append(event)
            self._recent_events = self._recent_events[-80:]
            self.message = f"Output test: {result.reason}"
            return self.snapshot()

    def record_sample(self) -> dict:
        with self._lock:
            self._sample_count += 1
            return {"type": "sample", "sample_count": self._sample_count}

    def record_prediction(self, elapsed_seconds: float, prediction) -> dict:
        gesture_label = normalize_gesture_label(prediction.label)
        with self._lock:
            binding = self.config.actions.get(gesture_label, ActionBinding.disabled())
            output_enabled = self.config.output_enabled
        # Execute OUTSIDE the lock: ActionBinding is frozen, so the captured
        # binding is race-safe, and a macro's time.sleep no longer stalls every
        # other session caller (set_status, snapshot, profile edits) while it runs.
        result = self.executor.execute(binding) if output_enabled else _blocked_result(binding)
        with self._lock:
            self._gesture_count += 1
            if result.emitted:
                self._action_count += 1
            event = {
                "type": "gesture",
                "elapsed_seconds": round(elapsed_seconds, 6),
                "gesture_label": gesture_label,
                "confidence": round(prediction.confidence, 3),
                "reason": prediction.reason,
                "action_description": result.description,
                "action_emitted": result.emitted,
                "output_enabled": output_enabled,
                "output_reason": result.reason,
                "features": {
                    "gyro_p99": round(prediction.features.gyro_p99, 3),
                    "accel_deviation_p99": round(prediction.features.accel_deviation_p99, 3),
                    "c_mean": round(prediction.features.c_mean, 3),
                    "c_abs_p99": round(prediction.features.c_abs_p99, 3),
                    "lateral_gyro_p99": round(prediction.features.lateral_gyro_p99, 3),
                    "lateral_accel_p99": round(prediction.features.lateral_accel_p99, 3),
                    "orientation_angle_degrees": round(prediction.features.orientation_angle_degrees, 3),
                },
            }
            self._recent_events.append(event)
            self._recent_events = self._recent_events[-80:]
            if self.logger is not None:
                self.logger.log("action", {
                    "gesture": gesture_label,
                    "binding": result.description,
                    "emitted": result.emitted,
                    "reason": result.reason,
                    "output_enabled": output_enabled,
                })
            return event

    def _motion_diagnostics(self) -> dict:
        # Live BODY-FRAME tilt diagnostics from the Motion engine for the tuning
        # panel and the on-screen viz: the signed d/e horizontal lean (hd/he), the
        # lean angle, the active direction, whether a stamp just fired, and the
        # engage threshold. NO heading / calibration (that machinery is gone). The
        # viz reads hd/he/tilt from here so it shares the engine's exact sign
        # convention (the structural half of the bug-#8 flip fix). Null-safe
        # defaults if no live engine is attached. Caller holds the lock.
        engine = self._motion_engine
        settings = self._active_motion_settings_locked()
        return {
            "hd": round(float(getattr(engine, "_last_hd", 0.0)), 1),
            "he": round(float(getattr(engine, "_last_he", 0.0)), 1),
            "tilt": round(float(getattr(engine, "_last_tilt", 0.0)), 1),
            "tilt_on": round(float(getattr(engine, "tilt_on", 7.6)), 1),
            "turn_threshold": round(float(getattr(engine, "turn_threshold", settings.turn_threshold)), 0),
            "turn_sensitivity": round(float(getattr(engine, "turn_sensitivity", settings.turn_sensitivity)), 0),
            "mouse_speed": settings.mouse_speed,
            "direction": str(getattr(engine, "_last_direction", "idle")),
            "fire": bool(getattr(engine, "_last_fire", False)),
        }

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "status": self.status,
                "message": self.message,
                "app_version": APP_VERSION,
                "config_path": str(self.config_path) if self.config_path is not None else "",
                "button_hint": play_button_hint(self.status, self.message),
                "output_enabled": self.config.output_enabled,
                "hold_ms": self.config.hold_ms,
                "default_hold_ms": DEFAULT_HOLD_MS,
                "sample_count": self._sample_count,
                "gesture_count": self._gesture_count,
                "action_count": self._action_count,
                "action_revision": self._action_revision,
                "active_profile": self.config.active_profile,
                "engine": self.config.engine,
                "lang": self.config.lang,
                "motion": self._motion_diagnostics(),
                "profiles": list(self.config.profiles.keys()),
                "battery": battery_snapshot(self._battery_percent, self._battery_message),
                # Every profile lists the same first-class Motion/Game controls.
                # Missing rows fall back to disabled so a half-populated map can
                # never crash the table.
                "actions": [
                    {
                        "gesture_label": label,
                        "display_name": display_name_for_label(label),
                        "binding": self.config.actions.get(label, ActionBinding.disabled()).to_dict(),
                        "description": self.config.actions.get(label, ActionBinding.disabled()).description,
                    }
                    for label in labels_for_profile(self.config.active_profile)
                ],
                "recent_events": list(reversed(self._recent_events[-30:])),
                "connection_log": list(reversed(self._connection_log[-30:])),
            }

    def _save_config(self) -> None:
        if self.config_path is not None:
            save_config(self.config_path, self.config)


def _blocked_result(binding: ActionBinding):
    from triki_actions import ActionResult

    return ActionResult(False, binding.description, "output disabled")


def display_name_for_label(control_label: str) -> str:
    # Kid-facing English fallback row labels for the Advanced Action Mapping table
    # (the JS localizes these to PL/EN; this is the server-side default). Each label
    # is its own self-describing control -- no overloaded meanings:
    #   Motion (Game) controls -- TILT is first-class, four axes of its own:
    return {
        # Rotation-invariant body-frame Motion controls (the Game profile).
        "turn-left": "Turn Left (twist)",
        "turn-right": "Turn Right (twist)",
        "go": "Go forward (tilt)",
        "stamp": "Stamp (fire)",
        "flip": "Flip = Shift (run)",
        "scrub-straight": "Scrub slide (use/door)",
        # Legacy discrete classifier gestures (kept as defensive fallbacks).
        "rotate-cw": "Twist Right",
        "rotate-ccw": "Twist Left",
        "scrub-cw": "Stir Right",
        "scrub-ccw": "Stir Left",
        "back-forth": "Shake",
        "lift": "Stamp",
        "flip-over": "Flip",
    }.get(control_label, control_label)


# Backwards-compatible alias: the old name pointed at the same lookup.
display_name_for_gesture = display_name_for_label


def binding_from_payload(payload: dict) -> ActionBinding:
    action_type = str(payload.get("action_type", payload.get("type", "key"))).lower()
    if action_type == "disabled":
        return ActionBinding.disabled()
    if action_type in {"key", "media"}:
        key_name = str(payload.get("key_name", "")).strip()
        if not key_name:
            raise ValueError("A key is required for a key action.")
        return ActionBinding.key(key_name)
    if action_type == "macro":
        return parse_macro_text(str(payload.get("macro_text", "")))
    raise ValueError(f"unknown action type: {action_type}")


def build_about_payload(session: AppSession) -> dict:
    return {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "creator": APP_CREATOR,
        "website": APP_WEBSITE,
        "license": APP_LICENSE,
        "config_path": str(session.config_path) if session.config_path is not None else "",
        "session_log": str(session.logger.path) if getattr(session, "logger", None) is not None else "",
        "docs": [
            "README.md",
            "CREDITS.md",
            "LICENSE",
            "docs/build.md",
            "docs/controls.md",
            "docs/how-it-works.md",
            "docs/linux.md",
            "docs/macos.md",
            "docs/protocol.md",
        ],
    }


def is_allowed_control_origin(origin: str | None) -> bool:
    if not origin:
        return True
    parsed = urlparse(origin)
    return parsed.scheme in {"http", "https"} and is_loopback_host(parsed.hostname or "")


def handle_control(
    session: AppSession,
    action: str,
    payload: dict,
    *,
    bus: EventBus,
    connection_control: ConnectionControl,
    command_bridge: BleCommandBridge | None = None,
    show_window=None,
) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("control payload must be a JSON object")
    if action == "show":
        if show_window is not None:
            show_window()
        return session.snapshot()
    if action == "pairing":
        return connection_control.request_pairing(session, bus)
    if action == "quit":
        return session.set_output_enabled(False)
    if action == "led":
        if command_bridge is None:
            raise RuntimeError("TRIKI LED control is not available.")
        enabled = bool(payload.get("enabled"))
        command_bridge.set_led(enabled)
        return session.set_status(
            session.status,
            "TRIKI LED test on." if enabled else "TRIKI LED test off.",
        )
    if action == "output":
        enabled = bool(payload.get("enabled"))
        if enabled and session.status not in OUTPUT_ACTIVE_STATUSES:
            raise ValueError("Connect TRIKI before turning control on.")
        return session.set_output_enabled(enabled)
    if action == "hold":
        return session.set_hold_ms(payload.get("ms", 0))
    if action == "tilt":
        return session.set_tilt_threshold(payload.get("threshold"))
    if action == "turn-threshold":
        return session.set_turn_threshold(payload.get("value"))
    if action == "turn-sensitivity":
        return session.set_turn_sensitivity(payload.get("value"))
    if action == "mouse-speed":
        return session.set_mouse_speed(payload.get("value"))
    if action == "lang":
        return session.set_lang(str(payload.get("lang", "pl")))
    if action == "test-key":
        return session.test_key_output(str(payload.get("key", "right")))
    if action == "clear":
        return session.clear_events()
    if action == "action":
        gesture_label = str(payload.get("gesture_label", "")).strip()
        if not gesture_label:
            raise ValueError("A gesture label is required for an action.")
        return session.update_action(gesture_label, binding_from_payload(payload))
    if action == "profile":
        operation = str(payload.get("operation", "")).lower()
        if operation == "create":
            return session.create_profile(str(payload.get("name", "")))
        if operation == "switch":
            return session.switch_profile(str(payload.get("name", "")))
        if operation == "delete":
            return session.delete_profile(str(payload.get("name", "")))
        if operation == "import":
            data = payload.get("data", payload.get("config"))
            return session.import_profiles(data, replace=bool(payload.get("replace", False)))
        if operation == "reset":
            return session.reset_active_profile()
        if operation == "reset-all":
            return session.reset_all_profiles()
        raise ValueError(f"unknown profile operation: {operation}")
    raise ValueError(f"unknown control action: {action}")


class AppHttpHandler(BaseHTTPRequestHandler):
    server: AppHttpServer

    def log_message(self, format, *args) -> None:  # noqa: A002
        if self.path == "/favicon.ico":
            return
        write_console_line(
            "%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), format % args),
            stream=sys.stderr,
        )

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send_text(build_html(), "text/html; charset=utf-8")
            return
        if parsed.path == "/debug":
            self._send_text(build_debug_html(), "text/html; charset=utf-8")
            return
        if parsed.path == "/about":
            self._send_json(build_about_payload(self.server.session))
            return
        if parsed.path == "/diagnostics":
            self._send_json(collect_diagnostics(config_path=self.server.session.config_path))
            return
        if parsed.path == "/profiles/export":
            self._send_profile_export()
            return
        if parsed.path == "/state":
            self._send_json({"type": "state", "state": self.server.session.snapshot()})
            return
        if parsed.path == "/events":
            self._stream_events()
            return
        if parsed.path == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/control":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except (TypeError, ValueError):
            self._send_json({"error": "invalid Content-Length"}, HTTPStatus.BAD_REQUEST)
            return
        if length < 0:
            self._send_json({"error": "invalid Content-Length"}, HTTPStatus.BAD_REQUEST)
            return
        if length > MAX_CONTROL_BODY_BYTES:
            self.send_response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            self.send_header("Connection", "close")
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.close_connection = True
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._send_json(
                {"error": "control requests require application/json"},
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            )
            return
        if not is_allowed_control_origin(self.headers.get("Origin")):
            self._send_json(
                {"error": "control requests are only accepted from the local app"},
                HTTPStatus.FORBIDDEN,
            )
            return
        try:
            body = self.rfile.read(length).decode("utf-8") if length else "{}"
            payload = json.loads(body or "{}")
            action = parse_qs(parsed.query).get("action", [""])[0]
            state = handle_control(
                self.server.session,
                action,
                payload,
                bus=self.server.bus,
                connection_control=self.server.connection_control,
                command_bridge=self.server.command_bridge,
                show_window=self.server.show_window,
            )
        except Exception as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        self.server.bus.publish({"type": "state", "state": state})
        self._send_json({"state": state})
        if action == "quit" and self.server.quit_app is not None:
            timer = threading.Timer(0.05, self.server.quit_app)
            timer.daemon = True
            timer.start()

    def _stream_events(self) -> None:
        subscriber = self.server.bus.subscribe()
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(encode_sse({"type": "state", "state": self.server.session.snapshot()}).encode("utf-8"))
            self.wfile.flush()
            while True:
                item = subscriber.get()
                self.wfile.write(item.encode("utf-8"))
                self.wfile.flush()
        except quiet_stream_errors():
            return
        finally:
            self.server.bus.unsubscribe(subscriber)

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_profile_export(self) -> None:
        body = json.dumps(self.server.session.export_profiles(), indent=2).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="triki-profiles.json"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, body: str, content_type: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


class AppHttpServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        session: AppSession,
        bus: EventBus,
        connection_control: ConnectionControl,
        command_bridge: BleCommandBridge | None = None,
        show_window=None,
        quit_app=None,
    ) -> None:
        super().__init__(server_address, AppHttpHandler)
        self.session = session
        self.bus = bus
        self.connection_control = connection_control
        self.command_bridge = command_bridge or BleCommandBridge()
        self.show_window = show_window
        self.quit_app = quit_app


class TrayController:
    def __init__(
        self,
        window,
        *,
        url: str | None = None,
        on_quit=None,
        opener=urlopen,
        language: str = "en",
        pystray_module=None,
        image_module=None,
        image_draw_module=None,
    ) -> None:
        self.window = window
        self.url = url
        self.on_quit = on_quit
        self.opener = opener
        self.language = "pl" if language == "pl" else "en"
        self.pystray_module = pystray_module
        self.image_module = image_module
        self.image_draw_module = image_draw_module
        self.icon = None
        self._allow_close = False
        self._close_handler_attached = False

    def _text(self, key: str) -> str:
        strings = {
            "pl": {
                "open": "Otwórz TRIKI Control",
                "disable": "Wyłącz sterowanie",
                "pair": "Połącz TRIKI",
                "diagnostics": "Diagnostyka",
                "quit": "Zakończ",
                "hidden_title": "TRIKI Control nadal działa",
                "hidden_message": (
                    "Program nadal działa w tle. Aby zatrzymać sterowanie, kliknij ikonę "
                    "TRIKI obok zegara i wybierz „Wyłącz sterowanie”. Aby zamknąć program, "
                    "wróć do aplikacji i kliknij „Zakończ”."
                ),
            },
            "en": {
                "open": "Open TRIKI Control",
                "disable": "Disable control",
                "pair": "Pair TRIKI",
                "diagnostics": "Diagnostics",
                "quit": "Quit",
                "hidden_title": "TRIKI Control is still running",
                "hidden_message": (
                    "The app is still running in the background. To stop control, click "
                    "the TRIKI icon by the clock and choose 'Disable control'. To close "
                    "the app, return to it and click 'Quit'."
                ),
            },
        }
        return strings[self.language][key]

    def attach_close_handler(self) -> None:
        if self._close_handler_attached:
            return
        self.window.events.closing += self._on_window_closing
        self._close_handler_attached = True

    def start(self) -> bool:
        try:
            pystray_module = self.pystray_module
            image_module = self.image_module
            image_draw_module = self.image_draw_module
            if pystray_module is None:
                import pystray as pystray_module
            if image_module is None or image_draw_module is None:
                from PIL import Image as image_module
                from PIL import ImageDraw as image_draw_module
        except Exception:
            return False

        menu = pystray_module.Menu(
            pystray_module.MenuItem(self._text("open"), self.open_window, default=True),
            pystray_module.MenuItem(self._text("disable"), self.request_output_off),
            pystray_module.MenuItem(self._text("pair"), self.request_pairing),
            pystray_module.MenuItem(self._text("diagnostics"), self.open_diagnostics),
            pystray_module.MenuItem(self._text("quit"), self.quit),
        )
        self.icon = pystray_module.Icon(
            "TRIKI Control",
            create_tray_image(image_module, image_draw_module),
            "TRIKI Control",
            menu,
        )
        self.attach_close_handler()
        if hasattr(self.icon, "run_detached"):
            self.icon.run_detached()
        else:
            thread = threading.Thread(target=self.icon.run, daemon=True)
            thread.start()
        return True

    def _on_window_closing(self, *args) -> bool:
        if self._allow_close:
            return True
        self.window.hide()
        self.notify_hidden()
        return False

    def notify_hidden(self) -> None:
        if self.icon is None or not hasattr(self.icon, "notify"):
            return
        with contextlib.suppress(Exception):
            self.icon.notify(self._text("hidden_message"), self._text("hidden_title"))

    def open_window(self, *args) -> None:
        if self.url is not None and hasattr(self.window, "load_url"):
            self.window.load_url(app_url_for_path(self.url, "/"))
        self.window.show()

    def open_diagnostics(self, *args) -> None:
        if self.url is not None and hasattr(self.window, "load_url"):
            self.window.load_url(app_url_for_path(self.url, "/debug"))
        self.window.show()

    def request_pairing(self, *args) -> None:
        if self.url is None:
            return
        with contextlib.suppress(Exception):
            post_control_action(self.url, "pairing", opener=self.opener)

    def request_output_off(self, *args) -> None:
        if self.url is None:
            return
        with contextlib.suppress(Exception):
            post_control_action(
                self.url,
                "output",
                {"enabled": False},
                opener=self.opener,
            )

    def quit(self, *args) -> None:
        if self._allow_close:
            return
        self._allow_close = True
        self.request_output_off()
        if self.icon is not None:
            with contextlib.suppress(Exception):
                self.icon.stop()
        if self.on_quit is not None:
            self.on_quit()
        self.window.destroy()


def app_resource_path(relative_path: Path | str) -> Path:
    base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base_path / Path(relative_path)


def create_tray_image(image_module, image_draw_module):
    icon = _load_tray_icon(image_module)
    if icon is not None:
        return icon

    image = image_module.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = image_draw_module.Draw(image)
    draw.ellipse((6, 6, 58, 58), fill=(35, 134, 54, 255), outline=(46, 160, 67, 255), width=3)
    draw.text((21, 18), "T", fill=(255, 255, 255, 255))
    return image


def _load_tray_icon(image_module):
    try:
        with image_module.open(app_resource_path(APP_ICON_TRAY_ASSET)) as source:
            image = source.convert("RGBA")
        resampling = getattr(getattr(image_module, "Resampling", image_module), "LANCZOS", 1)
        if image.size != (64, 64):
            image = image.resize((64, 64), resampling)
        return image
    except Exception:
        return None


def app_url_for_path(base_url: str, path: str) -> str:
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def post_control_action(
    base_url: str,
    action: str,
    payload: dict | None = None,
    *,
    opener=urlopen,
    timeout: float = 2.0,
) -> dict:
    body = json.dumps({} if payload is None else payload).encode("utf-8")
    request = Request(
        app_url_for_path(base_url, f"/control?action={action}"),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw or "{}")


def activate_existing_instance(
    base_url: str,
    *,
    opener=urlopen,
    timeout: float = 0.75,
) -> bool:
    try:
        post_control_action(base_url, "show", opener=opener, timeout=timeout)
        return True
    except Exception:
        return False


def build_html() -> str:
    return """<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TRIKI Control</title>
  <style>
    :root {
      color-scheme: dark;
      /* Neon arcade palette: deep space backdrop, electric accents */
      --bg: #0a0a14;
      --bg2: #11122a;
      --panel: rgba(22, 18, 46, 0.72);
      --panel-solid: #161230;
      --soft: rgba(38, 30, 72, 0.66);
      --line: rgba(124, 92, 255, 0.30);
      --line-strong: rgba(124, 92, 255, 0.6);
      --text: #f4f1ff;
      --muted: #9a90c8;
      --cyan: #1ff0ff;
      --magenta: #ff2bd6;
      --violet: #9b5cff;
      --lime: #5dff8f;
      --gold: #ffd23f;
      --red: #ff4d6d;
      --glow-cyan: rgba(31, 240, 255, 0.55);
      --glow-magenta: rgba(255, 43, 214, 0.55);
      --glow-lime: rgba(93, 255, 143, 0.6);
      --r: 18px;
      --font: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    }
    * { box-sizing: border-box; }
    [hidden] { display: none !important; }
    html, body {
      height: 100vh;
      overflow: hidden;
    }
    body {
      margin: 0;
      font-family: var(--font);
      color: var(--text);
      background:
        radial-gradient(900px 600px at 12% -8%, rgba(255, 43, 214, 0.16), transparent 60%),
        radial-gradient(900px 700px at 100% 0%, rgba(31, 240, 255, 0.16), transparent 60%),
        radial-gradient(700px 700px at 50% 120%, rgba(155, 92, 255, 0.20), transparent 60%),
        linear-gradient(160deg, var(--bg), var(--bg2));
      -webkit-font-smoothing: antialiased;
    }
    /* animated arcade grid floor */
    body::before {
      content: "";
      position: fixed;
      inset: -40% -10% -10% -10%;
      z-index: 0;
      background-image:
        linear-gradient(rgba(124, 92, 255, 0.14) 1px, transparent 1px),
        linear-gradient(90deg, rgba(124, 92, 255, 0.10) 1px, transparent 1px);
      background-size: 46px 46px;
      transform: perspective(420px) rotateX(62deg);
      transform-origin: 50% 100%;
      mask-image: linear-gradient(to top, #000 0%, transparent 72%);
      -webkit-mask-image: linear-gradient(to top, #000 0%, transparent 72%);
      animation: floor 7s linear infinite;
      pointer-events: none;
      opacity: 0.8;
    }
    @keyframes floor { to { background-position: 0 46px, 46px 0; } }

    /* DESIGN STAGE: the UI is authored at a fixed 1020x820 box and SCALED to fit the
       window (see the fit() handler in the script). It shrinks onto a small monitor or
       grows on a big one while keeping the exact desktop layout, instead of reflowing. */
    #fit-stage {
      width: 1020px;
      height: 820px;
      position: absolute;
      top: 0;
      left: 50%;
      margin-left: -510px;           /* center the 1020-wide design box */
      transform-origin: top center;  /* JS sets transform: scale(s) -> stays centered */
      z-index: 1;
    }
    main {
      width: 100%;
      height: 100%;
      position: relative;
      z-index: 1;
      padding: 14px 18px 10px;
      display: grid;
      grid-template-rows: auto minmax(0, 1fr) auto;
      gap: 12px;
      overflow: hidden;
    }
    h1, h2, h3 { margin: 0; line-height: 1.1; }

    /* ---------- HEADER ---------- */
    .app-header {
      display: grid;
      grid-template-columns: auto 1fr auto;
      align-items: center;
      gap: 16px;
    }
    .brand { display: flex; align-items: center; gap: 14px; min-width: 0; }
    .brand-mark {
      width: 50px; height: 50px; border-radius: 14px; flex: 0 0 auto;
      display: grid; place-items: center;
      background: linear-gradient(135deg, var(--magenta), var(--violet) 55%, var(--cyan));
      box-shadow: 0 0 24px var(--glow-magenta), inset 0 0 12px rgba(255,255,255,0.25);
      font-weight: 900; font-size: 26px; color: #fff;
      text-shadow: 0 2px 6px rgba(0,0,0,0.5);
    }
    h1#app-title {
      font-size: 30px; font-weight: 900; letter-spacing: 1px;
      background: linear-gradient(90deg, var(--cyan), var(--magenta));
      -webkit-background-clip: text; background-clip: text; color: transparent;
      filter: drop-shadow(0 0 10px rgba(31,240,255,0.25));
    }
    .step-rail { display: flex; align-items: center; gap: 8px; justify-self: center; }
    .step-dot {
      display: flex; align-items: center; gap: 7px;
      padding: 7px 13px; border-radius: 999px;
      background: var(--soft); border: 1px solid var(--line);
      color: var(--muted); font-weight: 800; font-size: 13px; white-space: nowrap;
      transition: all .25s ease;
    }
    .step-dot .num {
      width: 22px; height: 22px; border-radius: 50%; display: grid; place-items: center;
      background: rgba(255,255,255,0.08); font-size: 12px;
    }
    .step-dot.done { color: var(--lime); border-color: var(--glow-lime); box-shadow: 0 0 14px rgba(93,255,143,0.25); }
    .step-dot.done .num { background: var(--lime); color: #04210f; }
    .step-dot.active { color: var(--text); border-color: var(--cyan); box-shadow: 0 0 16px var(--glow-cyan); }
    .step-dot.active .num { background: var(--cyan); color: #042; }

    .header-actions { display: flex; align-items: center; gap: 10px; }
    .lang-pill { min-width: 44px; font-weight: 900; letter-spacing: 1px; }
    .lang-pill.active { border-color: var(--cyan); color: var(--cyan); box-shadow: 0 0 14px var(--glow-cyan); background: rgba(31,240,255,0.08); }
    .led-button.quit-button {
      color: #fff; border-color: rgba(255,77,109,0.7); background: rgba(255,77,109,0.14);
    }
    .led-button.quit-button:hover { border-color: var(--red); color: #fff; box-shadow: 0 0 14px rgba(255,77,109,.35); }
    .battery-indicator {
      display: inline-flex; align-items: center; gap: 8px; color: var(--muted);
      font-size: 13px; font-weight: 700; white-space: nowrap; min-height: 24px;
    }
    .battery-icon {
      position: relative; width: 38px; height: 18px; border: 2px solid var(--line-strong);
      border-radius: 4px; padding: 2px;
    }
    .battery-icon::after {
      content: ""; position: absolute; right: -6px; top: 4px; width: 4px; height: 8px;
      border-radius: 0 2px 2px 0; background: var(--line-strong);
    }
    .battery-fill { display: block; height: 100%; width: 0%; border-radius: 2px;
      background: var(--lime); box-shadow: 0 0 8px var(--glow-lime); transition: width .2s ease, background .2s ease; }
    .battery-indicator.ok .battery-fill { background: var(--lime); box-shadow: 0 0 8px var(--glow-lime); }
    .battery-indicator.medium .battery-fill { background: var(--gold); box-shadow: 0 0 8px rgba(255,210,63,.6); }
    .battery-indicator.low .battery-fill, .battery-indicator.critical .battery-fill { background: var(--red); box-shadow: 0 0 8px rgba(255,77,109,.6); }
    .battery-indicator.unknown .battery-icon, .battery-indicator.unavailable .battery-icon { opacity: 0.55; }

    /* ---------- STAGE GRID ---------- */
    .stage-grid {
      display: grid;
      grid-template-columns: minmax(0, 1.05fr) minmax(320px, 0.95fr);
      gap: 16px;
      min-height: 0;
    }
    .card {
      border: 1px solid var(--line);
      background: var(--panel);
      -webkit-backdrop-filter: blur(8px);
      backdrop-filter: blur(8px);
      border-radius: var(--r);
      box-shadow: 0 18px 50px rgba(0,0,0,0.45), inset 0 1px 0 rgba(255,255,255,0.04);
      min-height: 0;
    }
    .card-title {
      display: flex; align-items: center; gap: 10px;
      font-size: 13px; font-weight: 900; letter-spacing: 2px; color: var(--muted);
      text-transform: uppercase;
    }
    .card-title .badge {
      margin-left: auto; font-size: 11px; letter-spacing: 1px;
      padding: 3px 10px; border-radius: 999px; border: 1px solid var(--line);
      color: var(--lime); background: rgba(93,255,143,0.08);
    }

    /* ---------- HERO LIVE CAP ---------- */
    .hero { display: grid; grid-template-rows: auto 1fr auto auto; gap: 12px; padding: 16px; }
    .cap-viewport {
      position: relative;
      display: grid; place-items: center;
      min-height: 0;
      perspective: 900px;
      overflow: hidden;
      border-radius: 14px;
      background:
        radial-gradient(circle at 50% 42%, rgba(31,240,255,0.10), transparent 58%),
        radial-gradient(circle at 50% 42%, rgba(255,43,214,0.10), transparent 70%),
        #0c0a1c;
      border: 1px solid var(--line);
    }
    .motion-ring {
      position: absolute; width: min(78%, 360px); aspect-ratio: 1; border-radius: 50%;
      border: 2px solid rgba(124,92,255,0.22);
      box-shadow: inset 0 0 50px rgba(124,92,255,0.10);
      transition: border-color .15s ease, box-shadow .15s ease;
    }
    .motion-ring.hot {
      border-color: var(--cyan);
      box-shadow: inset 0 0 60px rgba(31,240,255,0.22), 0 0 30px var(--glow-cyan);
    }
    .motion-ring.ring2 { width: min(92%, 440px); border-style: dashed; opacity: .5; animation: spinRing 16s linear infinite; }
    @keyframes spinRing { to { transform: rotate(360deg); } }

    .cap-model {
      --cap-x: 0px; --cap-y: 0px; --cap-turn: 0deg; --cap-tilt-x: 0deg; --cap-tilt-y: 0deg;
      --cap-scale: 1;
      position: relative; z-index: 3;
      width: clamp(160px, 24vw, 250px); aspect-ratio: 1;
      transform-style: preserve-3d;
      transform: translate3d(var(--cap-x), var(--cap-y), 0)
        scale(var(--cap-scale))
        rotateX(var(--cap-tilt-x)) rotateY(var(--cap-tilt-y)) rotateZ(var(--cap-turn));
      filter: drop-shadow(0 18px 26px rgba(0,0,0,0.55))
              drop-shadow(0 0 var(--glow-size, 6px) var(--glow-color, rgba(31,240,255,0.0)));
      will-change: transform, filter;
    }
    .cap-face {
      position: absolute; inset: 0; width: 100%; height: 100%;
      object-fit: contain; opacity: 0; transition: opacity 160ms ease; pointer-events: none;
      -webkit-user-drag: none; user-select: none;
    }
    .cap-face.front { opacity: 1; }
    .cap-face.side { top: 47%; height: 54%; transform: scale(1.06); }
    .cap-model.is-tilted .front { opacity: 0.45; }
    .cap-model.is-tilted .side { opacity: 0.95; }
    .cap-model.is-reverse .front, .cap-model.is-reverse .side { opacity: 0; }
    .cap-model.is-reverse .reverse { opacity: 1; }
    /* spin marker rides the rim so the kid can SEE rotation */
    .cap-spin-marker {
      position: absolute; z-index: 5; top: 8%; left: 50%;
      width: 16px; height: 16px; border-radius: 50%;
      background: var(--cyan); border: 3px solid #0c0a1c;
      box-shadow: 0 0 14px var(--glow-cyan);
      transform: translate(-50%, -50%);
    }
    .cap-model.is-reverse .cap-spin-marker { opacity: 0; }
    .cap-shadow {
      position: absolute; z-index: 2; bottom: 12%;
      width: clamp(150px, 20vw, 230px); height: clamp(22px, 3vw, 40px);
      border-radius: 50%; background: rgba(0,0,0,0.5); filter: blur(16px); opacity: .7;
    }

    /* ---------- MASCOT FACE (grafted: blinking eyes + reactive mouth, layered on the front cap) ---------- */
    .mascot-face {
      position: absolute; z-index: 6; inset: 0;
      pointer-events: none; transition: opacity .18s ease;
    }
    .cap-model.is-reverse .mascot-face, .cap-model.is-tilted .mascot-face { opacity: 0; }
    .mascot-eyes {
      position: absolute; left: 50%; top: 44%; transform: translate(-50%, -50%);
      display: flex; gap: clamp(18px, 4.5vw, 34px);
    }
    .mascot-eyes .eye {
      width: clamp(13px, 3.2vw, 22px); height: clamp(13px, 3.2vw, 22px);
      border-radius: 50%; background: #14102b;
      box-shadow: 0 0 0 3px rgba(255,255,255,0.85), 0 2px 6px rgba(0,0,0,0.5);
      transition: transform .12s ease, height .08s ease;
    }
    .mascot-face.blink .eye { height: 3px; }
    .mascot-face.excited .eye { transform: scale(1.18); }
    .mascot-mouth {
      position: absolute; left: 50%; top: 60%; transform: translateX(-50%);
      width: clamp(26px, 6vw, 44px); height: clamp(13px, 3vw, 22px);
      border: 4px solid #14102b; border-top: 0;
      border-radius: 0 0 60px 60px;
      box-shadow: 0 2px 4px rgba(0,0,0,0.35);
      transition: all .14s ease; background: rgba(255,255,255,0.10);
    }
    .mascot-face.surprised .mascot-mouth {
      width: clamp(20px, 4.5vw, 30px); height: clamp(20px, 4.5vw, 30px);
      border-radius: 50%; border-top: 4px solid #14102b;
    }
    .mascot-face.flat .mascot-mouth { height: 0; border-radius: 0; }

    /* directional arrows light up per axis */
    .motion-arrow, .turn-arrow {
      position: absolute; z-index: 4; opacity: 0.14; pointer-events: none;
      transition: opacity .12s ease, filter .12s ease, background .12s ease, border-color .12s ease;
    }
    .motion-arrow {
      width: 38px; height: 54px; background: var(--muted);
      clip-path: polygon(50% 0, 100% 58%, 70% 58%, 70% 100%, 30% 100%, 30% 58%, 0 58%);
    }
    .motion-arrow.active { background: var(--lime); opacity: 1; filter: drop-shadow(0 0 12px var(--glow-lime)); }
    .arrow-forward { top: 5%; transform: rotate(0deg); }
    .arrow-backward { bottom: 5%; transform: rotate(180deg); }
    .arrow-left { left: 6%; transform: rotate(-90deg); }
    .arrow-right { right: 6%; transform: rotate(90deg); }
    .arrow-up { right: 16%; top: 24%; width: 26px; height: 40px; }
    .arrow-down { right: 16%; bottom: 24%; width: 26px; height: 40px; transform: rotate(180deg); }
    .turn-arrow {
      width: min(72%, 340px); aspect-ratio: 1; border: 4px solid transparent;
      border-top-color: var(--muted); border-radius: 50%;
    }
    .turn-arrow.cw { transform: rotate(46deg); }
    .turn-arrow.ccw { transform: rotate(226deg); }
    .turn-arrow.active { border-top-color: var(--magenta); opacity: .9; filter: drop-shadow(0 0 12px var(--glow-magenta)); }

    /* BIG current-gesture callout */
    .gesture-callout {
      position: absolute; z-index: 7; top: 14px; left: 50%; transform: translateX(-50%);
      pointer-events: none; text-align: center; width: 92%;
    }
    .gesture-callout .word {
      display: inline-block;
      font-size: clamp(24px, 4vw, 42px); font-weight: 900; letter-spacing: 1px;
      padding: 6px 22px; border-radius: 14px;
      color: #fff; opacity: 0; transform: scale(0.7) rotate(-3deg);
      text-shadow: 0 0 18px currentColor;
      transition: opacity .12s ease, transform .12s ease;
      white-space: nowrap;
    }
    .gesture-callout.show .word { opacity: 1; transform: scale(1) rotate(-2deg); }
    .gesture-callout.pop .word { animation: pop .42s cubic-bezier(.2,1.6,.4,1); }
    @keyframes pop { 0% { transform: scale(0.55) rotate(-8deg);} 60% { transform: scale(1.14) rotate(3deg);} 100% { transform: scale(1) rotate(-2deg);} }

    .neutral-hint {
      position: absolute; z-index: 6; bottom: 14px; left: 50%; transform: translateX(-50%);
      color: var(--muted); font-size: 13px; font-weight: 700; letter-spacing: .5px;
      opacity: .8; pointer-events: none; transition: opacity .2s ease;
    }
    .gesture-callout.show ~ .neutral-hint { opacity: 0; }

    /* literal 6-axis readout = the cap's axes, proof it works */
    .axis-readout { display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px; }
    .axis {
      background: var(--soft); border: 1px solid var(--line); border-radius: 12px;
      padding: 7px 6px 8px; text-align: center;
    }
    .axis .lab { font-size: 10px; font-weight: 900; letter-spacing: 1px; color: var(--muted); }
    .axis .val {
      font-size: 14px; font-weight: 800; font-variant-numeric: tabular-nums;
      margin: 2px 0 6px; color: var(--text);
    }
    .axis .bar { position: relative; height: 6px; border-radius: 4px; background: rgba(255,255,255,0.08); overflow: hidden; }
    .axis .bar i {
      position: absolute; left: 50%; top: 0; bottom: 0; width: 2px; border-radius: 4px;
      background: var(--cyan); box-shadow: 0 0 8px var(--glow-cyan);
      transition: left .06s linear, width .06s linear, background .1s ease;
    }
    .axis.gyro .bar i { background: var(--magenta); box-shadow: 0 0 8px var(--glow-magenta); }
    .axis.accel .bar i { background: var(--cyan); box-shadow: 0 0 8px var(--glow-cyan); }
    .axis-legend { display: flex; gap: 16px; justify-content: center; font-size: 11px; color: var(--muted); font-weight: 700; }
    .axis-legend b { color: var(--magenta); }
    .axis-legend em { color: var(--cyan); font-style: normal; }

    /* energy meter */
    .energy-wrap { display: flex; align-items: center; gap: 10px; }
    .energy-wrap .lab { font-size: 11px; font-weight: 900; letter-spacing: 1px; color: var(--muted); }
    .energy { flex: 1; height: 10px; border-radius: 6px; background: rgba(255,255,255,0.08); overflow: hidden; }
    .energy i {
      display: block; height: 100%; width: 0%;
      background: linear-gradient(90deg, var(--lime), var(--cyan) 50%, var(--magenta));
      box-shadow: 0 0 12px var(--glow-cyan); transition: width .08s linear;
    }

    /* ---------- RIGHT COLUMN: the simple flow ---------- */
    .flow { display: grid; grid-template-rows: auto auto auto 1fr; gap: 12px; min-height: 0; }

    /* (1) Connect */
    .connect-card { padding: 14px; display: grid; gap: 10px; }
    .pair-button {
      width: 100%; min-height: 60px; border: 0; border-radius: 16px;
      font-size: 22px; font-weight: 900; letter-spacing: .5px; color: #04121a; cursor: pointer;
      background: linear-gradient(135deg, var(--cyan), #4af);
      box-shadow: 0 0 30px var(--glow-cyan), inset 0 -3px 0 rgba(0,0,0,0.2);
      display: flex; align-items: center; justify-content: center; gap: 12px;
      transition: transform .12s ease, box-shadow .2s ease, background .25s ease;
    }
    .pair-button:hover { transform: translateY(-2px); }
    .pair-button:active { transform: translateY(1px) scale(.99); }
    .pair-button.connected {
      color: #042713;
      background: linear-gradient(135deg, var(--lime), #2fe0a0);
      box-shadow: 0 0 30px var(--glow-lime), inset 0 -3px 0 rgba(0,0,0,0.2);
    }
    @keyframes pulse { 0%,100% { box-shadow: 0 0 22px var(--glow-cyan);} 50% { box-shadow: 0 0 44px var(--glow-cyan);} }
    .pair-button:not(.connected) { animation: pulse 1.8s ease-in-out infinite; }

    /* (2) Game tiles */
    .games-card { padding: 14px; display: grid; grid-template-rows: auto 1fr; gap: 10px; min-height: 0; }
    .game-grid {
      display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px;
      overflow: auto; align-content: start;
      /* top/side padding so a SELECTED tile's lift (-2px) + border + glow in the top
         row isn't clipped by this scroll container (looked "cut by the KROK 2 header"). */
      padding: 8px 4px 4px 2px;
    }
    .game-tile {
      position: relative; border: 2px solid var(--line); border-radius: 16px;
      background: linear-gradient(160deg, rgba(255,255,255,0.05), rgba(0,0,0,0.18));
      padding: 12px 8px 10px; cursor: pointer; text-align: center; color: var(--text);
      transition: transform .14s ease, border-color .18s ease, box-shadow .2s ease, background .2s ease;
      display: grid; gap: 5px; place-items: center; min-height: 96px; font-family: var(--font);
    }
    .game-tile:hover { transform: translateY(-3px); border-color: var(--line-strong); }
    .game-tile .emoji { font-size: 36px; line-height: 1; filter: drop-shadow(0 3px 6px rgba(0,0,0,.4)); }
    .game-tile .name { font-weight: 900; font-size: 14px; }
    .game-tile .desc { font-size: 11px; color: var(--muted); font-weight: 600; }
    .game-tile.selected {
      border-color: var(--magenta);
      background: linear-gradient(160deg, rgba(255,43,214,0.18), rgba(124,92,255,0.10));
      box-shadow: 0 0 26px var(--glow-magenta);
      transform: translateY(-2px);
    }
    .game-tile.selected::after {
      content: "\\2713"; position: absolute; top: 6px; right: 9px;
      width: 22px; height: 22px; border-radius: 50%; display: grid; place-items: center;
      background: var(--magenta); color: #fff; font-size: 13px; font-weight: 900;
      box-shadow: 0 0 12px var(--glow-magenta);
    }

    /* (3) big toggles row. The Game profile auto-holds internally, so step 3 is
       the lone Output ON/OFF (no game-mode rocker). */
    .toggles { display: grid; grid-template-columns: 1.2fr 1fr; gap: 12px; }
    .toggles.toggles-single { grid-template-columns: 1fr; }
    .power-card {
      padding: 12px; display: grid; gap: 8px; place-items: center; text-align: center;
      border-radius: var(--r); border: 1px solid var(--line); background: var(--panel);
    }
    .power-card .cap-title { font-size: 12px; font-weight: 900; letter-spacing: 2px; color: var(--muted); }
    .power-btn {
      width: 100%; min-height: 64px; border: 0; border-radius: 18px; cursor: pointer;
      font-size: 24px; font-weight: 900; letter-spacing: 2px; color: #fff;
      display: flex; align-items: center; justify-content: center; gap: 12px;
      background: linear-gradient(135deg, #3a3358, #221d3e);
      box-shadow: inset 0 0 0 2px rgba(255,255,255,0.06);
      transition: all .2s ease;
    }
    .power-btn:disabled { opacity: .45; cursor: not-allowed; }
    .power-btn .dot { width: 16px; height: 16px; border-radius: 50%; background: var(--red); box-shadow: 0 0 10px var(--red); transition: all .2s ease; }
    .power-btn.on {
      background: linear-gradient(135deg, var(--lime), #2fe0a0);
      color: #04210f; box-shadow: 0 0 34px var(--glow-lime);
    }
    .power-btn.on .dot { background: #04210f; box-shadow: none; }

    /* ---------- LED + advanced bar ---------- */
    .util-bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; align-content: start; }
    .led-button {
      min-height: 46px; padding: 0 18px; border-radius: 14px; cursor: pointer; font-weight: 900; font-size: 14px;
      border: 1px solid var(--line); background: var(--soft); color: var(--text);
      display: inline-flex; align-items: center; gap: 8px; transition: all .15s ease; font-family: var(--font);
    }
    .led-button:hover { border-color: var(--line-strong); }
    .led-button.active { border-color: var(--gold); color: var(--gold); box-shadow: 0 0 18px rgba(255,210,63,0.4); background: rgba(255,210,63,0.1); }
    .led-button:disabled { opacity: .45; cursor: not-allowed; }
    .util-spacer { flex: 1; }

    /* ---------- ADVANCED (contained, scrollable overlay/drawer) ----------
       The grown-up settings live in a FIXED overlay, NOT as a grid child, so
       opening them can never collapse the main stage grid or bleed past the
       viewport. The opener is a normal button in the util-bar; the body is a
       scrollable panel that closes via its X, a backdrop click, or Esc. */
    .adv-open-btn {
      min-height: 46px; padding: 0 18px; border-radius: 14px; cursor: pointer;
      font-weight: 900; font-size: 14px; font-family: var(--font);
      border: 1px solid var(--line); background: var(--soft); color: var(--text);
      display: inline-flex; align-items: center; gap: 8px; transition: all .15s ease;
    }
    .adv-open-btn:hover { border-color: var(--line-strong); }
    .adv-backdrop {
      position: fixed; inset: 0; z-index: 50;
      background: rgba(0, 0, 0, 0.55);
      -webkit-backdrop-filter: blur(2px); backdrop-filter: blur(2px);
      display: none; padding: 24px;
    }
    .adv-backdrop.open { display: grid; place-items: center; }
    .adv-panel {
      width: 100%; max-width: min(900px, 94vw); max-height: 88vh;
      display: flex; flex-direction: column; min-height: 0;
      border: 1px solid var(--line-strong); border-radius: 16px;
      background: var(--panel-solid);
      box-shadow: 0 30px 80px rgba(0,0,0,0.6), inset 0 1px 0 rgba(255,255,255,0.05);
    }
    .adv-panel-header {
      position: sticky; top: 0; z-index: 1; flex: 0 0 auto;
      display: flex; align-items: center; gap: 12px;
      padding: 14px 16px; border-bottom: 1px solid var(--line);
      background: var(--panel-solid); border-radius: 16px 16px 0 0;
    }
    .adv-panel-header h2 {
      font-size: 14px; font-weight: 900; letter-spacing: 1px; color: var(--text);
      text-transform: uppercase; margin: 0;
    }
    .adv-close-btn {
      margin-left: auto; min-height: 34px; min-width: 34px; padding: 0 10px;
      border-radius: 10px; border: 1px solid var(--line); background: var(--soft);
      color: var(--text); font-size: 18px; font-weight: 900; cursor: pointer; line-height: 1;
    }
    .adv-close-btn:hover { border-color: var(--line-strong); color: var(--red); }
    .adv-body {
      flex: 1 1 auto; min-height: 0; overflow-y: auto;
      padding: 14px;
    }
    .adv-section { border: 1px solid var(--line); border-radius: 14px; padding: 14px; margin-bottom: 14px; background: rgba(0,0,0,0.18); }
    .adv-section:last-child { margin-bottom: 0; }
    .adv-section > h2 { font-size: 12px; letter-spacing: 2px; text-transform: uppercase; color: var(--cyan); margin-bottom: 12px; }
    .row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
    .row label { font-size: 13px; color: var(--muted); font-weight: 700; }
    .profile-controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }

    /* Body-frame Motion settings: profile-specific TURN tuning plus a live
       body-frame tilt read. No calibrate, no heading. */
    .motion-help { color: var(--muted); font-size: 12px; line-height: 1.4; margin-bottom: 10px; }
    .motion-help strong { color: var(--cyan); }
    .tilt-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-bottom: 10px; }
    .tilt-controls label { font-size: 13px; color: var(--muted); font-weight: 700; }
    .tilt-controls input[type="number"] { width: 90px; }
    .tilt-hint { color: var(--muted); font-size: 12px; flex: 1 1 220px; }
    .motion-live { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; font-size: 11px; color: var(--muted); }
    .motion-live code { color: var(--lime); font-weight: 700; font-variant-numeric: tabular-nums; margin-right: 4px; }

    button, select, input {
      min-height: 38px; border: 1px solid var(--line); border-radius: 10px;
      background: var(--soft); color: var(--text); padding: 8px 12px; font-size: 14px; font-family: var(--font);
    }
    button { cursor: pointer; }
    button:disabled { cursor: not-allowed; opacity: 0.5; }
    select option { background: var(--panel-solid); }
    .actions { display: grid; gap: 8px; }
    .action-row {
      border: 1px solid var(--line); background: rgba(255,255,255,0.03); border-radius: 12px; padding: 10px;
      display: grid; grid-template-columns: 150px 1fr; gap: 10px; align-items: center;
    }
    .action-row .gesture-name { display: grid; gap: 2px; }
    .action-row .gesture-name strong { font-size: 14px; }
    .action-row .gesture-name small { color: var(--muted); font-size: 11px; }
    .mapping-controls { display: grid; grid-template-columns: 190px minmax(210px, 1fr) auto auto; gap: 8px; align-items: center; }
    .mapping-controls .macro-input { grid-column: 1 / -1; }
    .record-key.recording { border-color: var(--lime); color: var(--lime); }
    .danger { color: var(--red); border-color: rgba(255,77,109,0.5); }

    /* ---------- FOOTER ---------- */
    .status-footer {
      display: flex; justify-content: space-between; align-items: baseline; gap: 16px;
      color: var(--muted); font-size: 13px; padding: 0 4px 2px;
    }
    .status-footer p { margin: 0; line-height: 1.35; }
    .footer-left { text-align: left; }
    .footer-right { text-align: right; }

    /* about dialog */
    dialog#about-dialog {
      border: 1px solid var(--line-strong); border-radius: 16px; background: var(--panel-solid);
      color: var(--text); padding: 20px; width: min(440px, calc(100vw - 36px));
    }
    dialog::backdrop { background: rgba(5,4,16,0.7); }
    .about-body { display: grid; gap: 8px; }
    .about-body p { margin: 0; color: var(--muted); line-height: 1.45; overflow-wrap: anywhere; }
    .about-actions { display: flex; justify-content: flex-end; margin-top: 14px; }

    /* The main screen scales as one fixed stage. The Advanced overlay is the one
       exception: its controls reflow so the modal remains usable in a narrow window. */
    @media (max-width: 760px) {
      .adv-backdrop { padding: 12px; }
      .adv-panel { max-width: calc(100vw - 24px); }
      .adv-body, .adv-section { padding: 10px; }
      .action-row { grid-template-columns: 1fr; }
      .mapping-controls { grid-template-columns: 1fr; }
      .mapping-controls > * { width: 100%; min-width: 0; }
    }
  </style>
</head>
<body>
  <div id="fit-stage">
  <main>
    <!-- ===================== HEADER ===================== -->
    <section class="app-header">
      <div class="brand">
        <div class="brand-mark" aria-hidden="true">T</div>
        <h1 id="app-title">TRIKI Control</h1>
      </div>

      <nav class="step-rail" aria-label="Steps">
        <div class="step-dot" id="step-1"><span class="num">1</span> <span data-i18n="step.connect">Connect</span></div>
        <div class="step-dot" id="step-2"><span class="num">2</span> <span data-i18n="step.pickGame">Pick game</span></div>
        <div class="step-dot" id="step-3"><span class="num">3</span> <span data-i18n="step.turnOn">Turn ON</span></div>
        <div class="step-dot" id="step-4"><span class="num">4</span> <span data-i18n="step.play">Play!</span></div>
      </nav>

      <div class="header-actions">
        <button class="lang-pill led-button" id="lang-toggle" type="button" title="Language">PL</button>
        <button class="about-button led-button" id="about-button" type="button" title="About" data-i18n="header.about">About</button>
        <div class="battery-indicator unknown" id="battery-indicator" aria-label="Battery status" title="Battery level unknown.">
          <span class="battery-icon" aria-hidden="true"><span class="battery-fill" id="battery-fill"></span></span>
          <span class="battery-label" id="battery-label" data-i18n="battery.unknown">Battery --</span>
        </div>
      </div>
    </section>

    <!-- ===================== STAGE ===================== -->
    <div class="stage-grid">

      <!-- ---------- HERO: live cap ---------- -->
      <section class="card hero" aria-label="Live TRIKI motion">
        <div class="card-title"><span data-i18n="hero.title">Your cap, live</span> <span class="badge" id="live-badge">LIVE</span></div>

        <div class="cap-viewport">
          <div class="motion-ring ring2" aria-hidden="true"></div>
          <div class="motion-ring" id="motion-ring" aria-hidden="true"></div>

          <div class="turn-arrow cw" data-arrow="turn-cw" aria-hidden="true"></div>
          <div class="turn-arrow ccw" data-arrow="turn-ccw" aria-hidden="true"></div>
          <div class="motion-arrow arrow-forward" data-arrow="pedal-forward" aria-hidden="true"></div>
          <div class="motion-arrow arrow-backward" data-arrow="pedal-backward" aria-hidden="true"></div>
          <div class="motion-arrow arrow-left" data-arrow="side-left" aria-hidden="true"></div>
          <div class="motion-arrow arrow-right" data-arrow="side-right" aria-hidden="true"></div>
          <div class="motion-arrow arrow-down" data-arrow="vertical-down" aria-hidden="true"></div>
          <div class="motion-arrow arrow-up" data-arrow="vertical-up" aria-hidden="true"></div>

          <div class="cap-shadow" aria-hidden="true"></div>
          <div class="cap-model" id="live-cap">
            <img class="cap-face front" src="__CAP_FRONT__" alt="TRIKI cap">
            <img class="cap-face side" src="__CAP_SIDE__" alt="">
            <img class="cap-face reverse" src="__CAP_REVERSE__" alt="">
            <div class="mascot-face flat" id="mascot-face" aria-hidden="true">
              <div class="mascot-eyes"><span class="eye" id="eye-l"></span><span class="eye" id="eye-r"></span></div>
              <div class="mascot-mouth"></div>
            </div>
            <div class="cap-spin-marker" aria-hidden="true"></div>
          </div>

          <div class="gesture-callout" id="gesture-callout"><span class="word" id="gesture-word">SPIN!</span></div>
          <div class="neutral-hint" id="neutral-hint" data-i18n="hero.neutralHint">Spin, tilt or stamp your cap!</div>
        </div>

        <!-- energy -->
        <div class="energy-wrap">
          <span class="lab" data-i18n="hero.power">POWER</span>
          <div class="energy"><i id="energy-fill"></i></div>
        </div>

        <!-- literal 6-axis readout -->
        <div>
          <div class="axis-readout" id="axis-readout">
            <div class="axis gyro"><div class="lab">SPIN X</div><div class="val" id="ax-0">0</div><div class="bar"><i id="bar-0"></i></div></div>
            <div class="axis gyro"><div class="lab">SPIN Y</div><div class="val" id="ax-1">0</div><div class="bar"><i id="bar-1"></i></div></div>
            <div class="axis gyro"><div class="lab">TURN Z</div><div class="val" id="ax-2">0</div><div class="bar"><i id="bar-2"></i></div></div>
            <div class="axis accel"><div class="lab">TILT X</div><div class="val" id="ax-3">0</div><div class="bar"><i id="bar-3"></i></div></div>
            <div class="axis accel"><div class="lab">TILT Y</div><div class="val" id="ax-4">0</div><div class="bar"><i id="bar-4"></i></div></div>
            <div class="axis accel"><div class="lab">FLIP Z</div><div class="val" id="ax-5">0</div><div class="bar"><i id="bar-5"></i></div></div>
          </div>
          <div class="axis-legend" style="margin-top:8px;"><span><b>&#9632;</b> gyro (spin)</span><span><em>&#9632;</em> accel (tilt)</span></div>
        </div>
      </section>

      <!-- ---------- FLOW: the simple steps ---------- -->
      <aside class="flow">

        <!-- (1) CONNECT -->
        <section class="card connect-card">
          <div class="card-title"><span data-i18n="connect.step">Step 1 &mdash; Connect</span></div>
          <button class="pair-button" data-action="pairing" type="button" data-i18n="connect.pair">Pair TRIKI</button>
        </section>

        <!-- (2) PICK A GAME -->
        <section class="card games-card">
          <div class="card-title"><span data-i18n="games.step">Step 2 &mdash; Pick your game</span> <span class="badge" id="profile-badge">&mdash;</span></div>
          <div class="game-grid" id="game-grid"><!-- tiles injected by JS --></div>
        </section>

        <!-- (3) ON/OFF -- the only step-3 control. The Game profile auto-holds
             keys internally (no game-mode toggle, no hold rocker): the flow is
             Connect -> Pick game -> Output ON. -->
        <div class="toggles toggles-single">
          <section class="power-card">
            <div class="cap-title" data-i18n="power.step">Step 3 &mdash; Control</div>
            <button class="power-btn" id="power-btn" type="button" aria-pressed="false" disabled>
              <span class="dot"></span><span id="power-label">CONTROL OFF</span>
            </button>
          </section>
        </div>

        <!-- LED + Advanced + explicit app exit -->
        <div class="util-bar">
          <button class="led-button" id="led-test" type="button" title="Hold to light the TRIKI LED" disabled data-i18n="led.test">Test light</button>
          <span class="util-spacer"></span>
          <button class="adv-open-btn" id="advanced-open" type="button" data-i18n="advanced.open">Advanced</button>
          <button class="quit-button led-button" id="quit-button" type="button" title="Quit">
            <span aria-hidden="true">&#x23FB;</span><span data-i18n="header.quit">Quit</span>
          </button>
        </div>
      </aside>
    </div>

    <!-- ===================== ADVANCED (contained, scrollable overlay) =====================
         Rendered OUTSIDE .stage-grid as a fixed backdrop + scrollable panel so it
         can never collapse the main grid or spill past the viewport. Opened by the
         util-bar "Advanced" button; closes via X / backdrop / Esc. -->
    <div class="adv-backdrop" id="advanced-backdrop" role="dialog" aria-modal="true" aria-labelledby="advanced-title" hidden>
      <div class="adv-panel">
        <div class="adv-panel-header">
          <h2 id="advanced-title" data-i18n="advanced.title">Advanced settings</h2>
          <button class="adv-close-btn" id="advanced-close" type="button" aria-label="Close" title="Close">&times;</button>
        </div>
        <div class="adv-body">

          <!-- Profiles management -->
          <section class="adv-section">
            <h2 data-i18n="adv.profiles">Profiles</h2>
            <div class="profile-controls">
              <label for="profile-select" data-i18n="adv.profile">Profile</label>
              <select id="profile-select" aria-label="Profile"></select>
              <input id="new-profile-name" placeholder="New profile" data-i18n-ph="adv.newProfile">
              <button id="create-profile" type="button" data-i18n="adv.new">New</button>
              <button id="delete-profile" type="button" data-i18n="adv.delete">Delete</button>
              <button id="reset-profile" type="button" data-i18n="adv.reset">Reset</button>
              <button id="export-profiles" type="button" data-i18n="adv.export">Export</button>
              <button id="import-profiles" type="button" data-i18n="adv.import">Import</button>
              <button id="reset-all-profiles" class="danger" type="button" data-i18n="adv.resetAll">Reset All</button>
              <input id="import-profile-file" type="file" accept="application/json" hidden>
            </div>
          </section>

          <!-- profile-specific TURN settings (body-frame Motion engine). NO
               calibrate button, NO heading -- neutral is auto-captured at connect
               and re-centred when the cap is still. The live tilt readout is
               diagnostic only; profile tuning here applies to twist/turn. Hidden
               only for legacy non-motion states. -->
          <section class="adv-section tilt-section" id="tilt-section" hidden>
            <h2 data-i18n="adv.tilt">Tilt control</h2>
            <p class="motion-help">
              <span data-i18n="motion.activeEngine">Active engine:</span> <strong id="engine-name">motion</strong>.
              <span data-i18n="tilt.help">Twist the cap in place to turn; lean it past the threshold and hold to walk. Neutral is wherever the cap lies when you connect &mdash; no calibration. Re-centre by leaving the cap still for ~1.5 s. Strafe (sideways lean) is best-effort &mdash; remap it in the rows below.</span>
            </p>
            <div class="tilt-controls">
              <label for="turn-threshold" data-i18n="turn.threshold">Turn threshold</label>
              <input type="range" id="turn-threshold" min="400" max="1600" step="10" value="1000" style="flex:1 1 160px;"> <code id="turn-threshold-val">1000</code>
              <span class="tilt-hint" data-i18n="turn.thresholdHint">Saved per profile. Lower = the twist engages sooner.</span>
            </div>
            <div class="tilt-controls">
              <label for="turn-sensitivity" data-i18n="turn.sensitivity">Turn sensitivity</label>
              <input type="range" id="turn-sensitivity" min="0" max="100" step="1" value="50" style="flex:1 1 160px;"> <code id="turn-sensitivity-val">50</code>
              <span class="tilt-hint" data-i18n="turn.sensitivityHint">Saved per profile. Higher = gentler twist pickup.</span>
            </div>
            <div class="tilt-controls">
              <label for="mouse-speed" data-i18n="mouse.speed">Mouse speed</label>
              <input type="range" id="mouse-speed" min="1" max="50" step="1" value="12" style="flex:1 1 160px;"> <code id="mouse-speed-val">12</code>
              <span class="tilt-hint" data-i18n="mouse.speedHint">Saved per profile. Controls movement distance for mapped mouse directions.</span>
            </div>
            <div class="motion-live" id="motion-live">
              <span data-i18n="tilt.live.fwd">Forward/back</span> <code id="motion-he">0</code>
              <span data-i18n="tilt.live.side">Side</span> <code id="motion-hd">0</code>
              <span data-i18n="tilt.live.lean">Lean</span> <code id="motion-tilt">0&deg;</code>
              <span data-i18n="tilt.live.dir">Direction</span> <code id="motion-direction">idle</code>
            </div>
          </section>

          <!-- Per-gesture mapping table -->
          <section class="adv-section">
            <h2 data-i18n="adv.actionMapping">Action Mapping</h2>
            <div class="actions" id="actions"><!-- rows injected by JS --></div>
          </section>
        </div>
      </div>
    </div>

    <!-- ===================== FOOTER ===================== -->
    <section class="status-footer">
      <p class="footer-left" id="message">Waiting for state</p>
      <p class="footer-right" id="hint">Pairing button: wait.</p>
    </section>

    <dialog id="about-dialog">
      <div class="about-body">
        <h2>TRIKI Control v__APP_VERSION__</h2>
        <p id="about-version">Version __APP_VERSION__</p>
        <p id="about-credits">Created by Wojciech 'Koksny' Górny, Koksny.com.</p>
        <p id="about-license">Open source under the MIT License.</p>
        <p id="about-config">Config path will appear here.</p>
        <p data-i18n="about.tagline">Spin the cap. Play the game. Have fun!</p>
      </div>
      <div class="about-actions">
        <button id="about-close" type="button" data-i18n="about.close">Close</button>
      </div>
    </dialog>
  </main>
  </div>
  <script>
    // ---- SCALE-TO-FIT: the UI is authored at a fixed 1020x820 design box (#fit-stage)
    // and scaled to fit the window, so it shrinks onto a small monitor or grows on a big
    // one while keeping the exact desktop layout (no reflow). window.innerWidth/Height
    // are NOT affected by a child transform, so there is no resize feedback loop. ----
    (function () {
      var DW = 1020, DH = 820;
      // Lift the overlays OUT of the scaled stage so they render at full window size
      // (readable settings on a small monitor) instead of shrinking with the game view.
      // Same element IDs -> all other JS that looks them up still works.
      ['advanced-backdrop', 'about-dialog'].forEach(function (id) {
        var el = document.getElementById(id);
        if (el && el.parentElement !== document.body) document.body.appendChild(el);
      });
      function fit() {
        var stage = document.getElementById('fit-stage');
        if (!stage) return;
        var s = Math.min(window.innerWidth / DW, window.innerHeight / DH);
        s = Math.max(0.3, Math.min(s, 3));
        stage.style.transform = 'scale(' + s + ')';
      }
      window.addEventListener('resize', fit);
      window.addEventListener('orientationchange', fit);
      if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', fit);
      } else {
        fit();
      }
    })();
    let state = null;
    let renderedActionRevision = null;
    let renderedProfileSignature = null;
    let activeRecorder = null;

    /* =========================================================
       i18n: Polish DEFAULT + English toggle. Every user-facing string carries a
       data-i18n (textContent) or data-i18n-ph (placeholder) key; setLang() walks
       them. The chosen language is persisted server-side via control('lang',...)
       and arrives back on state.lang. /debug stays English (separate page).
       ========================================================= */
    let lang = 'pl';
    const I18N = {
      pl: {
        'step.connect': 'Połącz', 'step.pickGame': 'Wybierz grę', 'step.turnOn': 'Włącz', 'step.play': 'Graj!',
        'header.about': 'O programie', 'header.quit': 'Zakończ',
        'quit.confirm': 'Zakończyć TRIKI Control i całkowicie wyłączyć sterowanie?',
        'battery.unknown': 'Bateria --', 'battery.level': 'Bateria', 'battery.titleUnknown': 'Poziom baterii jest nieznany.',
        'hero.title': 'Twój kapsel na żywo', 'hero.neutralHint': 'Kręć, przechylaj lub stempluj kapsel!',
        'hero.power': 'MOC',
        'connect.step': 'Krok 1 \\u2014 Połącz', 'connect.pair': 'Połącz TRIKI', 'connect.connected': 'Połączono',
        'games.step': 'Krok 2 \\u2014 Wybierz grę',
        'power.step': 'Krok 3 \\u2014 Sterowanie',
        'power.turnOn': 'WŁĄCZ STEROWANIE', 'power.turnOff': 'WYŁĄCZ STEROWANIE',
        'power.offline': 'STEROWANIE WYŁĄCZONE',
        'led.test': 'Test światła',
        'advanced.open': 'Zaawansowane', 'advanced.title': 'Ustawienia zaawansowane',
        'adv.profiles': 'Profile', 'adv.profile': 'Profil', 'adv.newProfile': 'Nowy profil',
        'adv.new': 'Nowy', 'adv.delete': 'Usuń', 'adv.reset': 'Reset', 'adv.export': 'Eksport',
        'adv.import': 'Import', 'adv.resetAll': 'Resetuj wszystko',
        'adv.actionMapping': 'Mapowanie akcji',
        'adv.tilt': 'Sterowanie obrotem',
        'motion.activeEngine': 'Aktywny silnik:',
        'tilt.help': 'Obróć kapsel płasko w miejscu, aby skręcić; przechyl go i przytrzymaj, aby iść do przodu. Suwaki poniżej dotyczą obrotu, a nie progu przechyłu. Neutral to po prostu jak kapsel leży po połączeniu \\u2014 bez kalibracji.',
        'turn.threshold': 'Próg obrotu', 'turn.thresholdHint': 'Zapisywane osobno dla profilu. Niżej = obrót łapie szybciej.',
        'turn.sensitivity': 'Czułość obrotu', 'turn.sensitivityHint': 'Zapisywane osobno dla profilu. Wyżej = bardziej wybacza wolny lub niedoskonały obrót.',
        'mouse.speed': 'Szybkość myszy', 'mouse.speedHint': 'Zapisywane osobno dla profilu. Określa odległość ruchu dla przypisanych kierunków myszy.',
        'action.target': 'Klawisz / media / mysz', 'action.macro': 'Makro', 'action.disabled': 'Wyłączone',
        'action.record': 'Nagraj klawisz', 'action.pressKey': 'Naciśnij klawisz', 'action.save': 'Zapisz',
        'tilt.live.fwd': 'Przód/tył', 'tilt.live.side': 'Bok', 'tilt.live.lean': 'Przechył', 'tilt.live.dir': 'Kierunek',
        'about.tagline': 'Kręć kapslem. Graj. Baw się dobrze!', 'about.close': 'Zamknij'
      },
      en: {
        'step.connect': 'Connect', 'step.pickGame': 'Pick game', 'step.turnOn': 'Turn ON', 'step.play': 'Play!',
        'header.about': 'About', 'header.quit': 'Quit',
        'quit.confirm': 'Quit TRIKI Control and completely disable control?',
        'battery.unknown': 'Battery --', 'battery.level': 'Battery', 'battery.titleUnknown': 'Battery level is unknown.',
        'hero.title': 'Your cap, live', 'hero.neutralHint': 'Spin, tilt or stamp your cap!',
        'hero.power': 'POWER',
        'connect.step': 'Step 1 \\u2014 Connect', 'connect.pair': 'Pair TRIKI', 'connect.connected': 'Connected',
        'games.step': 'Step 2 \\u2014 Pick your game',
        'power.step': 'Step 3 \\u2014 Control',
        'power.turnOn': 'TURN CONTROL ON', 'power.turnOff': 'TURN CONTROL OFF',
        'power.offline': 'CONTROL OFF',
        'led.test': 'Test light',
        'advanced.open': 'Advanced', 'advanced.title': 'Advanced settings',
        'adv.profiles': 'Profiles', 'adv.profile': 'Profile', 'adv.newProfile': 'New profile',
        'adv.new': 'New', 'adv.delete': 'Delete', 'adv.reset': 'Reset', 'adv.export': 'Export',
        'adv.import': 'Import', 'adv.resetAll': 'Reset All',
        'adv.actionMapping': 'Action Mapping',
        'adv.tilt': 'Turn control',
        'motion.activeEngine': 'Active engine:',
        'tilt.help': "Twist the cap in place to turn; lean it and hold to walk. The sliders below tune turn/twist, not the lean threshold. Neutral is wherever the cap lies when you connect \\u2014 no calibration.",
        'turn.threshold': 'Turn threshold', 'turn.thresholdHint': 'Saved per profile. Lower = twist engages sooner.',
        'turn.sensitivity': 'Turn sensitivity', 'turn.sensitivityHint': 'Saved per profile. Higher = more forgiving slow or imperfect twists.',
        'mouse.speed': 'Mouse speed', 'mouse.speedHint': 'Saved per profile. Controls movement distance for mapped mouse directions.',
        'action.target': 'Key / Media / Mouse', 'action.macro': 'Macro', 'action.disabled': 'Disabled',
        'action.record': 'Record key', 'action.pressKey': 'Press a key', 'action.save': 'Save',
        'tilt.live.fwd': 'Forward/back', 'tilt.live.side': 'Side', 'tilt.live.lean': 'Lean', 'tilt.live.dir': 'Direction',
        'about.tagline': 'Spin the cap. Play the game. Have fun!', 'about.close': 'Close'
      }
    };
    const STATUS_TEXT = {
      pl: {
        idle: ['Kliknij „Połącz TRIKI”, aby zacząć.', 'Nie naciskaj jeszcze przycisku na kapslu.'],
        waiting: ['Kliknij „Połącz TRIKI”, aby zacząć.', 'Nie naciskaj jeszcze przycisku na kapslu.'],
        pairing: ['Szukanie TRIKI. Naciśnij teraz fizyczny przycisk raz.', 'Naciśnij raz i puść, potem trzymaj TRIKI blisko komputera.'],
        connecting: ['Łączenie z TRIKI...', 'Nie naciskaj przycisku podczas łączenia.'],
        connected: ['Połączono. Uruchamiam sterowanie...', 'Nie naciskaj przycisku parowania podczas połączenia.'],
        ready: ['TRIKI jest połączone i gotowe.', 'Wybierz profil, potem włącz sterowanie.'],
        retrying: ['Połączenie nie jest gotowe. Próbuję ponownie...', 'Trzymaj TRIKI blisko komputera i poczekaj.'],
        reconnecting: ['Połączenie zostało przerwane. Próbuję ponownie...', 'Trzymaj TRIKI blisko komputera i poczekaj.'],
        disconnected: ['TRIKI zostało rozłączone. Sterowanie jest wyłączone.', 'Kliknij „Połącz TRIKI”, aby połączyć ponownie.'],
        error: ['Nie udało się połączyć z TRIKI.', 'Kliknij „Połącz TRIKI”, aby spróbować ponownie.']
      },
      en: {
        idle: ['Click Pair TRIKI to begin.', 'Do not press the cap button yet.'],
        waiting: ['Click Pair TRIKI to begin.', 'Do not press the cap button yet.'],
        pairing: ['Looking for TRIKI. Press its physical button once now.', 'Press once and release, then keep TRIKI close to the computer.'],
        connecting: ['Connecting to TRIKI...', 'Do not press the button while connecting.'],
        connected: ['Connected. Starting control...', 'Do not press the pairing button while connected.'],
        ready: ['TRIKI is connected and ready.', 'Choose a profile, then turn control on.'],
        retrying: ['The connection is not ready. Trying again...', 'Keep TRIKI close to the computer and wait.'],
        reconnecting: ['The connection was interrupted. Trying again...', 'Keep TRIKI close to the computer and wait.'],
        disconnected: ['TRIKI disconnected. Control is off.', 'Click Pair TRIKI to connect again.'],
        error: ['Could not connect to TRIKI.', 'Click Pair TRIKI to try again.']
      }
    };
    function T(key, vars) {
      const table = I18N[lang] || I18N.pl;
      let value = (key in table) ? table[key] : (I18N.pl[key] != null ? I18N.pl[key] : key);
      if (vars) for (const name in vars) value = value.split('{' + name + '}').join(vars[name]);
      return value;
    }
    function setLang(next) {
      lang = (next === 'en') ? 'en' : 'pl';
      document.documentElement.setAttribute('lang', lang);
      document.querySelectorAll('[data-i18n]').forEach(el => {
        el.textContent = T(el.getAttribute('data-i18n'));
      });
      document.querySelectorAll('[data-i18n-ph]').forEach(el => {
        el.setAttribute('placeholder', T(el.getAttribute('data-i18n-ph')));
      });
      const pill = document.getElementById('lang-toggle');
      if (pill) {
        pill.textContent = (lang === 'pl') ? 'PL' : 'EN';
        pill.classList.toggle('active', true);
      }
      const quitButton = document.getElementById('quit-button');
      if (quitButton) quitButton.title = T('header.quit');
      // Re-render the language-dependent dynamic bits (tiles, tilt block, the
      // localized Advanced action-row names).
      if (state) {
        renderedProfileSignature = null;
        renderedActionRevision = null;
        renderTilt();
        renderProfiles();
        renderActions();
        renderPower(state.status === 'connected' || state.status === 'ready');
        renderStatus();
        renderBattery(state.battery);
      }
    }

    const keyChoices = [
      ['', 'Disabled'],
      ['left', 'Left Arrow'],
      ['right', 'Right Arrow'],
      ['up', 'Up Arrow'],
      ['down', 'Down Arrow'],
      ['enter', 'Enter'],
      ['space', 'Space'],
      ['escape', 'Escape'],
      ['ctrl', 'Ctrl'],
      ['shift', 'Shift'],
      ['alt', 'Alt'],
      ['tab', 'Tab'],
      ['backspace', 'Backspace'],
      ['page-up', 'Page Up'],
      ['page-down', 'Page Down'],
      ['=', 'Equals'],
      [',', 'Comma ( , )'],
      ['.', 'Period ( . )'],
      ['w', 'W'],
      ['a', 'A'],
      ['s', 'S'],
      ['d', 'D'],
      ['volume-up', 'Volume Up'],
      ['volume-down', 'Volume Down'],
      ['volume-mute', 'Volume Mute'],
      ['media-play-pause', 'Play/Pause'],
      ['media-next', 'Media Next'],
      ['media-prev', 'Media Previous']
    ];
    const mouseChoices = {
      pl: [
        ['mouse-left-button', 'Lewy przycisk myszy'],
        ['mouse-right-button', 'Prawy przycisk myszy'],
        ['mouse-middle-button', 'Środkowy przycisk myszy'],
        ['mouse-move-left', 'Ruch myszy w lewo'],
        ['mouse-move-right', 'Ruch myszy w prawo'],
        ['mouse-move-up', 'Ruch myszy w górę'],
        ['mouse-move-down', 'Ruch myszy w dół']
      ],
      en: [
        ['mouse-left-button', 'Left mouse button'],
        ['mouse-right-button', 'Right mouse button'],
        ['mouse-middle-button', 'Middle mouse button'],
        ['mouse-move-left', 'Move mouse left'],
        ['mouse-move-right', 'Move mouse right'],
        ['mouse-move-up', 'Move mouse up'],
        ['mouse-move-down', 'Move mouse down']
      ]
    };

    // Kid-friendly names + emoji per real profile, localized PL/EN. Keys are the
    // EXACT server profile names (control('profile',{operation:'switch',name})).
    // EXACTLY two built-in slots. Both use the same Game/Motion action rows;
    // Music keeps media-key defaults on those shared rows.
    const GAME_META = {
      en: {
        'Game': { emoji: '\\uD83C\\uDFAE', name: 'Game', desc: 'Tilt, twist, stamp' },
        'Music': { emoji: '\\uD83C\\uDFB5', name: 'Music', desc: 'Media keys' }
      },
      pl: {
        'Game': { emoji: '\\uD83C\\uDFAE', name: 'Gra', desc: 'Przechył, obrót, stempel' },
        'Music': { emoji: '\\uD83C\\uDFB5', name: 'Muzyka', desc: 'Sterowanie muzyka' }
      }
    };
    function gameMetaFor(name) {
      const table = GAME_META[lang] || GAME_META.pl;
      return table[name];
    }
    // Big on-screen callout word per emitted control. The Motion engine emits the
    // first-class labels used by every profile; legacy discrete labels remain here
    // only as defensive fallbacks for old events.
    const calloutWords = {
      // Motion (Game) -- rotation-invariant tank scheme
      'turn-right': 'TURN RIGHT!',
      'turn-left': 'TURN LEFT!',
      'go': 'GO!',
      'stamp': 'STAMP!',
      'flip': 'FLIP!',
      'scrub-straight': 'SLIDE!',
      // Discrete (Music)
      'rotate-cw': 'TURN RIGHT!',
      'rotate-ccw': 'TURN LEFT!',
      'scrub-cw': 'NEXT!',
      'scrub-ccw': 'PREV!',
      'back-forth': 'PLAY!',
      'lift': 'STAMP!',
      'flip-over': 'MUTE!'
    };
    const calloutColors = {
      // Motion (Game) -- rotation-invariant tank scheme
      'turn-right': 'var(--magenta)',
      'turn-left': 'var(--violet)',
      'go': 'var(--cyan)',
      'stamp': 'var(--gold)',
      'flip': 'var(--red)',
      'scrub-straight': 'var(--lime)',
      // Discrete (Music)
      'rotate-cw': 'var(--magenta)',
      'rotate-ccw': 'var(--violet)',
      'scrub-cw': 'var(--cyan)',
      'scrub-ccw': 'var(--cyan)',
      'back-forth': 'var(--lime)',
      'lift': 'var(--gold)',
      'flip-over': 'var(--red)'
    };
    // Localized (PL default / EN) Advanced row names per control label. Falls back
    // to the server-sent display_name, then the raw label. ASCII-only Polish to
    // match the rest of the embedded UI strings.
    const CONTROL_LABELS = {
      pl: {
        'turn-left': 'Skręt w lewo (obrót)', 'turn-right': 'Skręt w prawo (obrót)',
        'go': 'Naprzód (przechył)',
        'stamp': 'Stempel (strzał)', 'flip': 'Do góry dnem = Shift (bieg)',
        'scrub-straight': 'Przesuw po stole (użyj/drzwi)',
        'rotate-cw': 'Obrót w prawo', 'rotate-ccw': 'Obrót w lewo',
        'scrub-cw': 'Mieszanie w prawo', 'scrub-ccw': 'Mieszanie w lewo',
        'back-forth': 'Potrząśnięcie', 'lift': 'Stempel', 'flip-over': 'Obrót kapsla'
      },
      en: {
        'turn-left': 'Turn left (twist)', 'turn-right': 'Turn right (twist)',
        'go': 'Go forward (tilt)',
        'stamp': 'Stamp (fire)', 'flip': 'Flip upside down = Shift (run)',
        'scrub-straight': 'Scrub slide (use/door)',
        'rotate-cw': 'Twist right', 'rotate-ccw': 'Twist left',
        'scrub-cw': 'Stir right', 'scrub-ccw': 'Stir left',
        'back-forth': 'Shake', 'lift': 'Stamp', 'flip-over': 'Flip'
      }
    };
    function controlLabel(item) {
      const table = CONTROL_LABELS[lang] || CONTROL_LABELS.pl;
      return table[item.gesture_label] || item.display_name || item.gesture_label;
    }

    function setState(next) {
      state = next;
      // Apply the persisted UI language (PL default) before painting text. Only
      // re-walk the DOM when it actually changes to avoid clobbering inputs.
      const nextLang = (state && state.lang) === 'en' ? 'en' : 'pl';
      if (nextLang !== lang) setLang(nextLang);
      document.getElementById('app-title').textContent = 'TRIKI Control';
      renderStatus();
      renderBattery(state.battery);
      const pairButton = document.querySelector('.pair-button');
      const ledButton = document.getElementById('led-test');
      const isConnected = state.status === 'connected' || state.status === 'ready';
      pairButton.classList.toggle('connected', isConnected);
      pairButton.textContent = isConnected ? T('connect.connected') : T('connect.pair');
      ledButton.disabled = !isConnected;
      if (!isConnected && ledHeld) {
        ledHeld = false;
        ledButton.classList.remove('active');
      }
      renderTilt();
      renderPower(isConnected);
      const nextProfileSignature = profileSignature();
      if (nextProfileSignature !== renderedProfileSignature) renderProfiles();
      if (state.action_revision !== renderedActionRevision) renderActions();
      refreshSteps(isConnected);
    }

    function renderStatus() {
      if (!state) return;
      const table = STATUS_TEXT[lang] || STATUS_TEXT.pl;
      const copy = table[state.status];
      document.getElementById('message').textContent = copy ? copy[0] : state.message;
      document.getElementById('hint').textContent = copy ? copy[1] : (state.button_hint || '');
    }

    function renderTilt() {
      // Surface profile-specific TURN tuning when engine == 'motion'. NO calibrate,
      // NO heading -- the tilt values below are a live diagnostic read.
      // Legacy classifier states hide this block.
      const isMotion = (state && state.engine) === 'motion';
      const section = document.getElementById('tilt-section');
      const engineName = document.getElementById('engine-name');
      if (section) section.hidden = !isMotion;
      if (engineName) engineName.textContent = (state && state.engine) ? state.engine : 'motion';
      const m = (state && state.motion) || {};
      const setText = (id, value) => { const el = document.getElementById(id); if (el) el.textContent = value; };
      setText('motion-he', Number.isFinite(m.he) ? m.he : 0);
      setText('motion-hd', Number.isFinite(m.hd) ? m.hd : 0);
      setText('motion-tilt', (Number.isFinite(m.tilt) ? m.tilt : 0) + '\\u00B0');
      setText('motion-direction', m.direction || 'idle');
      // Seed the profile-specific TURN threshold unless the grown-up is mid-edit
      // (don't clobber a value being dragged).
      const thr = document.getElementById('turn-threshold');
      const thv = document.getElementById('turn-threshold-val');
      if (thr && document.activeElement !== thr && Number.isFinite(m.turn_threshold) && m.turn_threshold > 0) {
        thr.value = m.turn_threshold;
      }
      if (thv && Number.isFinite(m.turn_threshold) && m.turn_threshold > 0) thv.textContent = m.turn_threshold;
      const ts = document.getElementById('turn-sensitivity');
      const tsv = document.getElementById('turn-sensitivity-val');
      if (ts && document.activeElement !== ts && Number.isFinite(m.turn_sensitivity)) {
        ts.value = m.turn_sensitivity;
        if (tsv) tsv.textContent = m.turn_sensitivity;
      }
      const mouseSpeed = document.getElementById('mouse-speed');
      const mouseSpeedValue = document.getElementById('mouse-speed-val');
      if (mouseSpeed && document.activeElement !== mouseSpeed && Number.isFinite(m.mouse_speed)) {
        mouseSpeed.value = m.mouse_speed;
        if (mouseSpeedValue) mouseSpeedValue.textContent = m.mouse_speed;
      }
    }

    function renderPower(isConnected) {
      const on = !!state.output_enabled;
      const btn = document.getElementById('power-btn');
      btn.classList.toggle('on', on);
      btn.setAttribute('aria-pressed', String(on));
      btn.disabled = !isConnected && !on;
      const label = on ? T('power.turnOff') : isConnected ? T('power.turnOn') : T('power.offline');
      btn.setAttribute('aria-label', label);
      btn.title = label;
      document.getElementById('power-label').textContent = label;
    }

    function refreshSteps(isConnected) {
      const s1 = document.getElementById('step-1');
      const s2 = document.getElementById('step-2');
      const s3 = document.getElementById('step-3');
      const s4 = document.getElementById('step-4');
      [s1, s2, s3, s4].forEach(s => s.classList.remove('active', 'done'));
      const hasGame = !!(state && state.active_profile);
      const output = !!(state && state.output_enabled);
      if (isConnected) s1.classList.add('done'); else s1.classList.add('active');
      if (isConnected) (hasGame ? s2.classList.add('done') : s2.classList.add('active'));
      if (isConnected && hasGame) (output ? s3.classList.add('done') : s3.classList.add('active'));
      if (isConnected && hasGame && output) s4.classList.add('active');
    }

    function renderBattery(battery) {
      const root = document.getElementById('battery-indicator');
      const fill = document.getElementById('battery-fill');
      const label = document.getElementById('battery-label');
      const rawPercent = battery && Number.isFinite(battery.percent) ? battery.percent : null;
      const percent = rawPercent === null ? null : Math.max(0, Math.min(100, rawPercent));
      const status = battery && battery.status ? battery.status : percent === null ? 'unknown' : 'ok';
      const text = percent === null ? T('battery.unknown') : `${percent}%`;
      root.className = `battery-indicator ${status}`;
      fill.style.width = percent === null ? '18%' : `${percent}%`;
      label.textContent = text;
      root.title = percent === null ? T('battery.titleUnknown') : `${T('battery.level')}: ${percent}%`;
      root.setAttribute('aria-label', percent === null ? text : `${T('battery.level')} ${text}`);
    }

    function profileNames() {
      return state.profiles && state.profiles.length ? state.profiles : ['Game'];
    }

    function profileSignature() {
      return `${state.active_profile || ''}|${profileNames().join('|')}`;
    }

    function renderProfiles() {
      const select = document.getElementById('profile-select');
      select.innerHTML = '';
      for (const name of profileNames()) {
        const option = document.createElement('option');
        option.value = name;
        option.textContent = name;
        select.appendChild(option);
      }
      const active = state.active_profile || profileNames()[0];
      select.value = active;
      renderGameTiles(active);
      const meta = gameMetaFor(active);
      document.getElementById('profile-badge').textContent = meta ? meta.name : active;
      renderedProfileSignature = profileSignature();
    }

    function renderGameTiles(active) {
      const grid = document.getElementById('game-grid');
      grid.innerHTML = '';
      for (const name of profileNames()) {
        const meta = gameMetaFor(name) || { emoji: '\\uD83C\\uDFAE', name, desc: '' };
        const tile = document.createElement('button');
        tile.type = 'button';
        tile.className = 'game-tile' + (name === active ? ' selected' : '');
        tile.dataset.game = name;
        tile.innerHTML =
          '<div class="emoji">' + meta.emoji + '</div>' +
          '<div class="name">' + escapeHtml(meta.name) + '</div>' +
          '<div class="desc">' + escapeHtml(meta.desc) + '</div>';
        tile.addEventListener('click', () => {
          if (name === (state && state.active_profile)) return;
          control('profile', { operation: 'switch', name });
        });
        grid.appendChild(tile);
      }
    }

    function allKeyChoices() {
      const choices = [...keyChoices, ...(mouseChoices[lang] || mouseChoices.pl)];
      for (let code = 65; code <= 90; code += 1) {
        const key = String.fromCharCode(code).toLowerCase();
        if (!choices.some(choice => choice[0] === key)) choices.push([key, String.fromCharCode(code)]);
      }
      for (let digit = 0; digit <= 9; digit += 1) {
        choices.push([String(digit), String(digit)]);
      }
      for (let index = 1; index <= 12; index += 1) {
        choices.push([`f${index}`, `F${index}`]);
      }
      return choices;
    }

    function renderActions() {
      const root = document.getElementById('actions');
      root.innerHTML = '';
      for (const item of state.actions || []) {
        const row = document.createElement('div');
        row.className = 'action-row';
        row.dataset.gesture = item.gesture_label;
        row.innerHTML = `
          <div class="gesture-name"><strong>${escapeHtml(controlLabel(item))}</strong><small>${escapeHtml(item.gesture_label)}</small></div>
          <div class="mapping-controls">
            <select class="action-type">
              <option value="key">${escapeHtml(T('action.target'))}</option>
              <option value="macro">${escapeHtml(T('action.macro'))}</option>
              <option value="disabled">${escapeHtml(T('action.disabled'))}</option>
            </select>
            <select class="key-select"></select>
            <button class="record-key" type="button">${escapeHtml(T('action.record'))}</button>
            <button class="apply-action" type="button">${escapeHtml(T('action.save'))}</button>
            <input class="macro-input" placeholder="left, 100ms, enter">
          </div>`;
        const typeSelect = row.querySelector('.action-type');
        const keySelect = row.querySelector('.key-select');
        const recordButton = row.querySelector('.record-key');
        const macroInput = row.querySelector('.macro-input');
        for (const choice of allKeyChoices()) {
          const option = document.createElement('option');
          option.value = choice[0];
          option.textContent = choice[0] ? choice[1] : T('action.disabled');
          keySelect.appendChild(option);
        }
        const binding = item.binding || { type: 'disabled' };
        typeSelect.value = binding.type === 'macro' ? 'macro' : binding.type === 'disabled' ? 'disabled' : 'key';
        keySelect.value = binding.key || '';
        macroInput.value = binding.type === 'macro' ? macroToText(binding.steps || []) : '';
        const syncFields = () => {
          keySelect.disabled = typeSelect.value !== 'key';
          recordButton.disabled = typeSelect.value !== 'key';
          macroInput.disabled = typeSelect.value !== 'macro';
          macroInput.hidden = typeSelect.value !== 'macro';
        };
        typeSelect.addEventListener('change', syncFields);
        recordButton.addEventListener('click', () => startKeyRecording(recordButton, keySelect));
        row.querySelector('.apply-action').addEventListener('click', () => {
          saveAction(item.gesture_label, typeSelect.value, keySelect.value, macroInput.value);
        });
        syncFields();
        root.appendChild(row);
      }
      renderedActionRevision = state.action_revision;
    }

    function saveAction(gestureLabel, actionType, keyName, macroText) {
      control('action', {
        gesture_label: gestureLabel,
        action_type: actionType,
        key_name: keyName,
        macro_text: macroText
      });
    }

    function macroToText(steps) {
      return steps.map(step => step.type === 'delay' ? `${step.ms}ms` : step.key).join(', ');
    }

    function startKeyRecording(button, select) {
      if (activeRecorder) activeRecorder.button.classList.remove('recording');
      button.classList.add('recording');
      button.textContent = T('action.pressKey');
      activeRecorder = { button, select };
      const finish = (event) => {
        event.preventDefault();
        event.stopPropagation();
        const keyName = keyNameFromEvent(event);
        ensureSelectOption(select, keyName, keyName.toUpperCase());
        select.value = keyName;
        button.classList.remove('recording');
        button.textContent = T('action.record');
        activeRecorder = null;
        document.removeEventListener('keydown', finish, true);
      };
      document.addEventListener('keydown', finish, true);
    }

    function ensureSelectOption(select, value, label) {
      if ([...select.options].some(option => option.value === value)) return;
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      select.appendChild(option);
    }

    function keyNameFromEvent(event) {
      const map = {
        ArrowLeft: 'left',
        ArrowRight: 'right',
        ArrowUp: 'up',
        ArrowDown: 'down',
        Enter: 'enter',
        Escape: 'escape',
        ' ': 'space',
        Spacebar: 'space',
        Tab: 'tab',
        Backspace: 'backspace',
        PageUp: 'page-up',
        PageDown: 'page-down',
        Control: 'ctrl',
        Shift: 'shift',
        Alt: 'alt',
        '=': '='
      };
      if (map[event.key]) return map[event.key];
      if (/^F([1-9]|1[0-2])$/.test(event.key)) return event.key.toLowerCase();
      if (/^[a-zA-Z0-9]$/.test(event.key)) return event.key.toLowerCase();
      return event.key.toLowerCase();
    }

    async function control(action, payload = {}) {
      try {
        const response = await fetch('/control?action=' + encodeURIComponent(action), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const data = await response.json();
        if (data.error) throw new Error(data.error);
        if (data.state) setState(data.state);
      } catch (error) {
        document.getElementById('message').textContent = error.message || String(error);
      }
    }

    for (const button of document.querySelectorAll('[data-action]')) {
      button.addEventListener('click', () => control(button.dataset.action));
    }
    const ledButton = document.getElementById('led-test');
    let ledHeld = false;
    ledButton.addEventListener('pointerdown', event => {
      if (ledButton.disabled) return;
      event.preventDefault();
      if (ledButton.setPointerCapture) ledButton.setPointerCapture(event.pointerId);
      ledHeld = true;
      ledButton.classList.add('active');
      control('led', { enabled: true });
    });
    async function releaseLed() {
      if (!ledHeld) return;
      ledHeld = false;
      ledButton.classList.remove('active');
      await control('led', { enabled: false });
    }
    ledButton.addEventListener('pointerup', releaseLed);
    ledButton.addEventListener('pointercancel', releaseLed);
    ledButton.addEventListener('lostpointercapture', releaseLed);
    window.addEventListener('blur', releaseLed);

    // Step 3 ON/OFF -> output control
    document.getElementById('power-btn').addEventListener('click', () => {
      if (!state) return;
      control('output', { enabled: !state.output_enabled });
    });

    document.getElementById('profile-select').addEventListener('change', event => {
      control('profile', { operation: 'switch', name: event.target.value });
    });
    document.getElementById('create-profile').addEventListener('click', () => {
      const input = document.getElementById('new-profile-name');
      control('profile', { operation: 'create', name: input.value });
      input.value = '';
    });
    document.getElementById('delete-profile').addEventListener('click', () => {
      if (state) control('profile', { operation: 'delete', name: state.active_profile });
    });
    document.getElementById('reset-profile').addEventListener('click', () => {
      control('profile', { operation: 'reset' });
    });
    document.getElementById('export-profiles').addEventListener('click', () => {
      window.location.href = '/profiles/export';
    });
    document.getElementById('import-profiles').addEventListener('click', () => {
      document.getElementById('import-profile-file').click();
    });
    document.getElementById('import-profile-file').addEventListener('change', async event => {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      try {
        const data = JSON.parse(await file.text());
        await control('profile', { operation: 'import', data });
      } catch (error) {
        document.getElementById('message').textContent = error.message || String(error);
      } finally {
        event.target.value = '';
      }
    });
    document.getElementById('reset-all-profiles').addEventListener('click', () => {
      if (window.confirm('Reset all profiles?')) control('profile', { operation: 'reset-all' });
    });
    // Turn threshold (raw gyro units). Saved per profile; lower values engage a
    // twist sooner. This is not the lean/tilt threshold.
    const turnThresholdInput = document.getElementById('turn-threshold');
    if (turnThresholdInput) {
      const thv = document.getElementById('turn-threshold-val');
      turnThresholdInput.addEventListener('input', () => {
        if (thv) thv.textContent = turnThresholdInput.value;
      });
      turnThresholdInput.addEventListener('change', () => {
        control('turn-threshold', { value: parseFloat(turnThresholdInput.value) });
      });
    }
    // Turn sensitivity slider (0..100): lower = the cap must twist more to turn.
    const turnSensInput = document.getElementById('turn-sensitivity');
    if (turnSensInput) {
      const tsv = document.getElementById('turn-sensitivity-val');
      turnSensInput.addEventListener('input', () => { if (tsv) tsv.textContent = turnSensInput.value; });
      turnSensInput.addEventListener('change', () => {
        control('turn-sensitivity', { value: parseFloat(turnSensInput.value) });
      });
    }
    const mouseSpeedInput = document.getElementById('mouse-speed');
    if (mouseSpeedInput) {
      const mouseSpeedValue = document.getElementById('mouse-speed-val');
      mouseSpeedInput.addEventListener('input', () => {
        if (mouseSpeedValue) mouseSpeedValue.textContent = mouseSpeedInput.value;
      });
      mouseSpeedInput.addEventListener('change', () => {
        control('mouse-speed', { value: parseInt(mouseSpeedInput.value, 10) });
      });
    }
    document.getElementById('quit-button').addEventListener('click', () => {
      if (window.confirm(T('quit.confirm'))) control('quit');
    });
    document.getElementById('about-button').addEventListener('click', async () => {
      const dialog = document.getElementById('about-dialog');
      try {
        const response = await fetch('/about');
        const about = await response.json();
        document.getElementById('about-version').textContent = `${about.app_name} v${about.app_version}`;
        document.getElementById('about-credits').textContent = `Created by ${about.creator} (${about.website}).`;
        document.getElementById('about-license').textContent = `Open source under the ${about.license}.`;
        document.getElementById('about-config').textContent = about.config_path ? `Config: ${about.config_path}` : 'Config: default app data path';
      } catch (error) {
        document.getElementById('about-config').textContent = error.message || String(error);
      }
      if (dialog.showModal) dialog.showModal();
      else dialog.setAttribute('open', 'open');
    });
    document.getElementById('about-close').addEventListener('click', () => {
      const dialog = document.getElementById('about-dialog');
      if (dialog.close) dialog.close();
      else dialog.removeAttribute('open');
    });

    // Language toggle (PL default <-> EN). Persisted server-side; the returned
    // state.lang drives setLang on the next setState, but we also flip instantly.
    document.getElementById('lang-toggle').addEventListener('click', () => {
      const next = lang === 'pl' ? 'en' : 'pl';
      setLang(next);
      control('lang', { lang: next });
    });

    // Advanced overlay: open via the util-bar button, close via X / backdrop / Esc.
    // It lives OUTSIDE .stage-grid as a fixed backdrop so it can never collapse the
    // main grid or spill past the viewport; its body scrolls internally.
    const advBackdrop = document.getElementById('advanced-backdrop');
    function openAdvanced() {
      advBackdrop.hidden = false;
      advBackdrop.classList.add('open');
    }
    function closeAdvanced() {
      advBackdrop.classList.remove('open');
      advBackdrop.hidden = true;
    }
    document.getElementById('advanced-open').addEventListener('click', openAdvanced);
    document.getElementById('advanced-close').addEventListener('click', closeAdvanced);
    advBackdrop.addEventListener('click', event => {
      if (event.target === advBackdrop) closeAdvanced();
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && advBackdrop.classList.contains('open')) closeAdvanced();
    });

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
      }[ch]));
    }

    /* =========================================================
       LIVE CAP ANIMATION (driven by the additive SSE 'motion' event)
       Derives -1..1 visual axes client-side from raw ints, then maps to CSS
       transforms. Index map: values[0,1,2]=gyro a,b,c ; values[3,4,5]=accel d,e,f.
       ========================================================= */
    const GYRO_NORM = 3500;
    const ACCEL_NORM = 2600;
    const SMOOTH = 0.30;
    const REST_F = -2050;
    const FACE_REVERSE_F_THRESHOLD = 1200;
    let motionTarget = [0, 0, 0, 0, 0, REST_F];
    let motionEnergy = 0;
    const vis = {
      turn: 0, tiltX: 0, tiltY: 0, stamp: 0, slide: 0, energy: 0,
      spinAngle: 0,
      faceSignal: REST_F, faceDown: false, tiltVisible: false
    };
    let faceLatchUntil = 0;

    function clamp(v, lo, hi) { return v < lo ? lo : v > hi ? hi : v; }
    function norm(v, scale) { return clamp(v / scale, -1, 1); }
    function dz(v, d) { return Math.abs(v) < d ? 0 : v; }
    function lerp(a, b, t) { return a + (b - a) * t; }

    // SSE 'motion' handler: store the latest raw axes + energy for the render loop.
    function onMotion(values, energy, rotation) {
      if (!Array.isArray(values) || values.length < 6) return;
      // Drop corrupt/dropped BLE packets (|accel| not near gravity ~2050). A junk
      // packet has accel ~0, which would otherwise jerk the cap / flash a false
      // flip. Keep the last good pose instead.
      const am = Math.sqrt(values[3] * values[3] + values[4] * values[4] + values[5] * values[5]);
      if (am < 800 || am > 8000) return;
      motionTarget = values.map(Number);
      motionEnergy = Number.isFinite(energy) ? Number(energy) : Math.sqrt(
        values[0] * values[0] + values[1] * values[1] + values[2] * values[2]);
    }

    const ARROW_CACHE = {};
    function setArrow(name, active) {
      const el = ARROW_CACHE[name] || (ARROW_CACHE[name] = document.querySelector('[data-arrow="' + name + '"]'));
      if (el) el.classList.toggle('active', !!active);
    }

    function updateFace(values) {
      const f = Number(values[5]);
      if (!isFinite(f)) return;
      vis.faceSignal += (f - vis.faceSignal) * 0.18;
      // Non-latching: the cap shows reversed ONLY while it is actually upside down,
      // and returns to normal as soon as it tips back toward upright (f < ~0). The
      // old code only un-reversed at f <= -1200, so after a single flip it stayed
      // reversed forever -- the bug the maintainer saw.
      if (vis.faceSignal >= FACE_REVERSE_F_THRESHOLD) vis.faceDown = true;
      else if (vis.faceSignal < 0) vis.faceDown = false;
    }

    function updateAxisReadout(values) {
      const scales = [GYRO_NORM, GYRO_NORM, GYRO_NORM, ACCEL_NORM, ACCEL_NORM, ACCEL_NORM];
      for (let i = 0; i < 6; i += 1) {
        const v = values[i];
        document.getElementById('ax-' + i).textContent = (v >= 0 ? '+' : '') + v;
        let n = clamp(v / scales[i], -1, 1);
        if (i === 5) n = clamp((v - REST_F) / ACCEL_NORM, -1, 1);
        const bar = document.getElementById('bar-' + i);
        const w = Math.abs(n) * 50;
        if (n >= 0) { bar.style.left = '50%'; bar.style.width = w + '%'; }
        else { bar.style.left = (50 - w) + '%'; bar.style.width = w + '%'; }
      }
    }

    const mascotFace = document.getElementById('mascot-face');
    let mascotExpressionUntil = 0;
    function setMascotExpression(kind, ms) {
      mascotFace.classList.remove('excited', 'surprised');
      if (kind) mascotFace.classList.add(kind);
      mascotExpressionUntil = (typeof performance !== 'undefined' ? performance.now() : Date.now()) + (ms || 800);
    }

    function renderCap(values, dt) {
      const turnRaw = dz(norm(values[2], GYRO_NORM), 0.035);
      const tiltXraw = dz(norm(values[3], ACCEL_NORM), 0.02);
      const tiltYraw = dz(norm(values[4], ACCEL_NORM), 0.02);
      const stampRaw = dz(norm((values[5] - REST_F), ACCEL_NORM), 0.05);
      const slideRaw = dz(norm(values[0], GYRO_NORM), 0.03);

      vis.turn = lerp(vis.turn, turnRaw, SMOOTH);
      vis.tiltX = lerp(vis.tiltX, tiltXraw, SMOOTH);
      vis.tiltY = lerp(vis.tiltY, tiltYraw, SMOOTH);
      vis.stamp = lerp(vis.stamp, stampRaw, SMOOTH);
      vis.slide = lerp(vis.slide, slideRaw, SMOOTH);

      const energyTarget = clamp(motionEnergy / 4200, 0, 1);
      vis.energy = lerp(vis.energy, energyTarget, 0.2);

      // The on-screen cap shows the cap's ROTATIONAL STATE, not its velocity. We
      // INTEGRATE the (deadzoned) gyro turn rate into a heading and HOLD it: while
      // the real cap is turning the angle advances; the moment it stops, the angle
      // STAYS where it is (the cap keeps its rotated pose) instead of springing back
      // on its own. The deadzone (|turnRaw| past 0.035) gates out gyro bias/noise so
      // a still cap never drifts. Gyro gives only angular velocity (a round cap +
      // no magnetometer cannot yield absolute heading), so this is a relative hold:
      // it reflects how far YOU last turned it and holds until you turn it again.
      const spinGain = 760;
      if (Math.abs(turnRaw) > 0.035) {
        vis.spinAngle = (vis.spinAngle + vis.turn * spinGain * dt) % 360;
      }
      // else: HOLD -- do not decay. Nobody is touching the cap, so it must not move.

      updateFace(values);

      // ---- VIZ FLIP FIX (bug #8): drive the cap rotation AND the directional
      // arrows from the SAME body-frame hd/he sign convention the engine decodes,
      // so a physical forward lean tilts the on-screen cap forward and lights the
      // forward arrow (they can never disagree). Anchor = the axis-readout truth
      // labels: vis.tiltX = norm(values[3]) = TILT X = d-axis (hd);
      // vis.tiltY = norm(values[4]) = TILT Y = e-axis (he).
      //   forward = he < 0  (engine MOVE_FORWARD) -> fwdSig = -vis.tiltY (fwd +)
      //   strafe-right = hd > 0 (engine strafe-right) -> strafeSig = vis.tiltX (right +)
      const fwdSig = -vis.tiltY;      // +ve when the cap leans FORWARD (he<0)
      const strafeSig = vis.tiltX;    // +ve when the cap leans RIGHT  (hd>0)

      const cap = document.getElementById('live-cap');
      cap.style.setProperty('--cap-turn', vis.spinAngle.toFixed(2) + 'deg');
      // rotateX: a forward lean (fwdSig>0) tips the cap's top toward the forward
      // arrow. rotateY: a right lean (strafeSig>0) banks the cap to the right.
      cap.style.setProperty('--cap-tilt-x', (fwdSig * 26).toFixed(2) + 'deg');
      cap.style.setProperty('--cap-tilt-y', (strafeSig * 26).toFixed(2) + 'deg');
      cap.style.setProperty('--cap-x', ((vis.slide * 26) + (strafeSig * 12)).toFixed(2) + 'px');
      cap.style.setProperty('--cap-y', (vis.stamp * 26).toFixed(2) + 'px');
      cap.style.setProperty('--cap-scale', (1 + vis.energy * 0.06).toFixed(3));
      cap.style.setProperty('--glow-size', (6 + vis.energy * 30).toFixed(1) + 'px');
      cap.style.setProperty('--glow-color', 'rgba(31,240,255,' + (0.05 + vis.energy * 0.6).toFixed(2) + ')');

      const tiltMag = Math.abs(vis.tiltX) + Math.abs(vis.tiltY);
      vis.tiltVisible = vis.tiltVisible ? tiltMag > 0.12 : tiltMag > 0.28;
      cap.classList.toggle('is-tilted', vis.tiltVisible);
      const now = (typeof performance !== 'undefined' ? performance.now() : Date.now());
      // Reverse the cap ONLY for the real upside-down orientation (no stamp/gesture
      // latch -- a stamp must never flip the on-screen cap).
      cap.classList.toggle('is-reverse', vis.faceDown);

      setArrow('turn-cw', vis.turn > 0.1);
      setArrow('turn-ccw', vis.turn < -0.1);
      // Arrows share fwdSig/strafeSig so they match the engine key mapping exactly:
      // forward lean -> forward arrow; right lean -> right arrow.
      setArrow('side-right', strafeSig > 0.12 || vis.slide > 0.12);
      setArrow('side-left', strafeSig < -0.12 || vis.slide < -0.12);
      setArrow('pedal-forward', fwdSig > 0.12);
      setArrow('pedal-backward', fwdSig < -0.12);
      setArrow('vertical-up', vis.stamp > 0.12);
      setArrow('vertical-down', vis.stamp < -0.12);

      document.getElementById('motion-ring').classList.toggle('hot', vis.energy > 0.28);
      document.getElementById('energy-fill').style.width = (vis.energy * 100).toFixed(0) + '%';

      if (now > mascotExpressionUntil) mascotFace.classList.remove('excited', 'surprised');

      updateAxisReadout(values);
    }

    /* BIG gesture callout (fired from SSE 'gesture') */
    let calloutTimer = null;
    function triggerCallout(label) {
      const word = calloutWords[label];
      if (!word) return;
      const el = document.getElementById('gesture-callout');
      const w = document.getElementById('gesture-word');
      w.textContent = word;
      w.style.color = calloutColors[label] || 'var(--cyan)';
      el.classList.add('show');
      el.classList.remove('pop'); void el.offsetWidth; el.classList.add('pop');
      clearTimeout(calloutTimer);
      calloutTimer = setTimeout(() => el.classList.remove('show'), 950);
    }

    function pulseGesture(label) {
      triggerCallout(label);
      // STAMP and FLIP get the "surprised" mascot beat; every turn/tilt/scrub gets
      // "excited". Covers both vocabularies.
      if (label === 'stamp' || label === 'flip' || label === 'lift' || label === 'flip-over') {
        const now = (typeof performance !== 'undefined' ? performance.now() : Date.now());
        faceLatchUntil = now + 750;
        setMascotExpression('surprised', 800);
      } else {
        setMascotExpression('excited', 700);
      }
    }

    /* Mascot eye blink loop */
    function blinkLoop() {
      mascotFace.classList.add('blink');
      setTimeout(() => mascotFace.classList.remove('blink'), 140);
      setTimeout(blinkLoop, 2400 + Math.random() * 2600);
    }
    blinkLoop();

    /* MAIN ANIMATION LOOP: dual heartbeat (setInterval) + rAF so the cap keeps
       animating even when backgrounded / throttled inside the embedded WebView. */
    let lastTime = null;
    function tick() {
      const now = (typeof performance !== 'undefined' ? performance.now() : Date.now());
      if (lastTime === null) lastTime = now;
      const dt = Math.min(0.05, Math.max(0, (now - lastTime) / 1000));
      if (dt <= 0) return;
      lastTime = now;
      renderCap(motionTarget, dt);
    }
    setInterval(tick, 30);
    (function raf() { tick(); requestAnimationFrame(raf); })();

    // Paint Polish defaults on first load (before any SSE state arrives) so the
    // very first frame shows translated PL copy, not the English HTML literals or
    // raw keys. The persisted choice (state.lang) takes over on the first setState.
    setLang('pl');

    const events = new EventSource('/events');
    events.onmessage = (message) => {
      const payload = JSON.parse(message.data);
      if (payload.type === 'state') setState(payload.state);
      if (payload.type === 'gesture') {
        if (state) {
          state.recent_events = [payload, ...state.recent_events].slice(0, 30);
          state.gesture_count += 1;
          if (payload.action_emitted) state.action_count += 1;
        }
        pulseGesture(payload.gesture_label);
      }
      if (payload.type === 'motion') onMotion(payload.values, payload.energy, payload.rotation);
      if (payload.type === 'sample' && state) state.sample_count = payload.sample_count;
    };
  </script>
</body>
</html>""".replace("__APP_VERSION__", APP_VERSION).replace("__CAP_FRONT__", CAP_FRONT_DATA_URI).replace("__CAP_SIDE__", CAP_SIDE_DATA_URI).replace("__CAP_REVERSE__", CAP_REVERSE_DATA_URI)


def build_debug_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>TRIKI Diagnostics</title>
  <style>
    :root { color-scheme: dark; --bg: #101216; --panel: #171b22; --line: #333d4d; --text: #f2f6fb; --muted: #a9b5c5; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
    main { max-width: 1120px; margin: 0 auto; padding: 18px; display: grid; gap: 14px; }
    h1, h2 { margin: 0; }
    h1 { font-size: 22px; }
    h2 { font-size: 15px; }
    section { border: 1px solid var(--line); background: var(--panel); border-radius: 8px; padding: 14px; display: grid; gap: 10px; }
    .grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
    .metric { color: var(--muted); }
    .metric strong { display: block; color: var(--text); font-size: 20px; }
    pre { margin: 0; overflow: auto; color: var(--text); background: #0b0d10; border: 1px solid var(--line); border-radius: 6px; padding: 10px; max-height: 320px; }
    @media (max-width: 760px) { .grid { grid-template-columns: 1fr 1fr; } }
  </style>
</head>
<body>
  <main>
    <h1>TRIKI Diagnostics</h1>
    <section>
      <h2>Live State</h2>
      <div class="grid">
        <div class="metric">Status<strong id="status">-</strong></div>
        <div class="metric">Samples<strong id="samples">0</strong></div>
        <div class="metric">Gestures<strong id="gestures">0</strong></div>
        <div class="metric">Actions<strong id="actions">0</strong></div>
      </div>
      <pre id="summary">{}</pre>
    </section>
    <section>
      <h2>connection_log</h2>
      <pre id="connection-log">[]</pre>
    </section>
    <section>
      <h2>recent_events</h2>
      <pre id="recent-events">[]</pre>
    </section>
  </main>
  <script>
    function render(state) {
      document.getElementById('status').textContent = state.status || '-';
      document.getElementById('samples').textContent = state.sample_count || 0;
      document.getElementById('gestures').textContent = state.gesture_count || 0;
      document.getElementById('actions').textContent = state.action_count || 0;
      document.getElementById('summary').textContent = JSON.stringify({
        message: state.message,
        button_hint: state.button_hint,
        output_enabled: state.output_enabled,
        active_profile: state.active_profile,
        profiles: state.profiles
      }, null, 2);
      document.getElementById('connection-log').textContent = JSON.stringify(state.connection_log || [], null, 2);
      document.getElementById('recent-events').textContent = JSON.stringify(state.recent_events || [], null, 2);
    }
    fetch('/state').then(response => response.json()).then(payload => render(payload.state));
    const events = new EventSource('/events');
    events.onmessage = message => {
      const payload = JSON.parse(message.data);
      if (payload.type === 'state') render(payload.state);
      if (payload.type === 'gesture') fetch('/state').then(response => response.json()).then(next => render(next.state));
    };
  </script>
</body>
</html>"""


def is_loopback_host(host: str) -> bool:
    normalized = host.strip("[]").strip().lower()
    return normalized in {"", "127.0.0.1", "::1", "localhost"}


def browser_url_for(host: str, port: int) -> str:
    display_host = host.strip("[]")
    if display_host in {"", "0.0.0.0", "::"}:
        display_host = "127.0.0.1"
    elif ":" in display_host and not display_host.startswith("["):
        display_host = f"[{display_host}]"
    return f"http://{display_host}:{port}/"


def schedule_browser_open(url: str, delay_seconds: float, opener=webbrowser.open) -> threading.Timer:
    def open_url() -> None:
        with contextlib.suppress(Exception):
            opener(url)

    timer = threading.Timer(max(0.0, delay_seconds), open_url)
    timer.daemon = True
    timer.start()
    return timer


def run_webview_window(
    url: str,
    *,
    webview_module=None,
    enable_tray: bool = True,
    on_show_window=None,
    on_quit_app=None,
    on_quit=None,
    language: str = "en",
) -> None:
    if webview_module is None:
        import webview as webview_module

    window = webview_module.create_window(
        "TRIKI Control",
        url,
        width=1020,
        height=820,
        resizable=True,        # the UI now scales-to-fit (see #fit-stage), so the
        min_size=(360, 480),   # window can shrink onto a small monitor or stretch.
    )
    if window is not None:
        controller = TrayController(
            window,
            url=url,
            on_quit=on_quit,
            language=language,
        )
        if on_show_window is not None:
            on_show_window(controller.open_window)
        if on_quit_app is not None:
            on_quit_app(controller.quit)
        if enable_tray:
            controller.start()
    webview_module.start()


def write_console_line(message: str, stream=_DEFAULT_CONSOLE_STREAM) -> None:
    output = sys.stdout if stream is _DEFAULT_CONSOLE_STREAM else stream
    if output is None:
        return
    with contextlib.suppress(Exception):
        output.write(f"{message}\n")
        output.flush()


def write_log_line(path: Path | None, message: str) -> None:
    if path is None:
        return
    with contextlib.suppress(Exception):
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{timestamp} {message}\n")


def default_config_path() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
        return base / "TRIKI" / "config.json"
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "triki" / "config.json"


def default_log_path() -> Path:
    return default_config_path().with_name("app.log")


def build_arg_parser(*, default_ui: str = "browser") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the TRIKI background app and local config UI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--config-path", type=Path, default=default_config_path())
    parser.add_argument("--log-path", type=Path, default=default_log_path())
    parser.add_argument("--ui", choices=("webview", "browser", "none"), default=default_ui)
    parser.add_argument("--open-browser", dest="ui", action="store_const", const="browser")
    parser.add_argument("--no-open-browser", dest="ui", action="store_const", const="none")
    parser.add_argument("--no-tray", action="store_true")
    parser.add_argument("--open-delay-seconds", type=float, default=0.75)
    parser.add_argument("--scan-seconds", type=float, default=10.0)
    parser.add_argument("--connect-attempts", type=int, default=5)
    parser.add_argument("--connect-mode", choices=("cached", "scan", "hybrid"), default="scan")
    parser.add_argument("--gatt-profile", choices=("auto", "nus-cached", "nus-uncached"), default="auto")
    parser.add_argument("--retry-delay-seconds", type=float, default=1.0)
    parser.add_argument("--reconnect-delay-seconds", type=float, default=0.25)
    parser.add_argument("--gatt-timeout-seconds", type=float, default=12.0)
    parser.add_argument("--auto-reconnect", action="store_true")
    parser.add_argument("--no-reconnect", action="store_true")
    parser.add_argument("--output-enabled", action="store_true")
    parser.add_argument(
        "--hold-ms",
        type=int,
        default=None,
        help=(
            "Hold each emitted key for N ms, auto-extending while a gesture "
            "repeats (needed for movement in games like Doom). 0 disables; "
            "omit to use the saved config value."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--session-log",
        default=None,
        help="Write the full session diagnostics log (JSONL) to this path. Default: <config dir>/sessions/session-<timestamp>.jsonl.",
    )
    parser.add_argument("--no-session-log", action="store_true", help="Disable session diagnostics logging.")
    parser.add_argument("--window-seconds", type=float, default=0.4)
    parser.add_argument("--min-samples", type=int, default=6)
    parser.add_argument("--min-confidence", type=float, default=0.55)
    parser.add_argument("--repeat-seconds", type=float, default=0.3)
    parser.add_argument("--warmup-seconds", type=float, default=0.05)
    parser.add_argument("--confirm-windows", type=int, default=1)
    return parser


def parse_args(argv: Sequence[str] | None = None, *, default_ui: str = "browser") -> argparse.Namespace:
    return build_arg_parser(default_ui=default_ui).parse_args(argv)


def _build_classifier(args: argparse.Namespace, *, observer=None) -> LiveGestureDetector:
    return LiveGestureDetector(
        window_seconds=args.window_seconds,
        min_samples=args.min_samples,
        min_confidence=args.min_confidence,
        repeat_seconds=args.repeat_seconds,
        warmup_seconds=args.warmup_seconds,
        confirm_windows=args.confirm_windows,
        suppress_labels=(
            "still",
            "unknown",
            "tap-single",
            "tap-double",
            "toss-catch",
            "rock-edge",
            "slide-back-forth",
            "twist-cw-ccw-cw-ccw",
            "twist-ccw-cw-ccw-cw",
        ),
        observer=observer,
    )


def build_detector(
    config: TrikiConfig,
    args: argparse.Namespace,
    *,
    observer=None,
):
    """Pick the per-sample control engine implied by ``config.engine`` at call time.

    Returns a drop-in for the ``detector`` parameter of ``run_ble_stream`` /
    ``run_ble_session``: the body-frame, continuous :class:`MotionControlEngine`
    for normal app profiles, or the legacy discrete :class:`LiveGestureDetector`
    if a caller deliberately supplies a classifier config. Both expose the same
    ``add_sample(elapsed_seconds, sample) -> GesturePrediction | None`` / ``reset()``
    contract. NOTE: the live app does NOT use this directly for the stream -- it
    uses :class:`ProfileEngineRouter` so a runtime profile switch re-routes input
    live; this helper is kept for tests and callers that want the single engine for
    a fixed config.
    """
    if config.engine == ENGINE_MOTION:
        engine = MotionControlEngine(observer=observer)
        apply_motion_profile_settings(
            engine,
            config.profile_settings.get(
                config.active_profile,
                default_motion_settings_for_profile(config.active_profile),
            ),
        )
        return engine
    return _build_classifier(args, observer=observer)


class ProfileEngineRouter:
    """Route each BLE sample to the engine the ACTIVE profile uses, so a runtime
    profile switch takes effect immediately -- the live engine is NOT frozen at
    stream start.

    Normal app profiles all resolve to the body-frame :class:`MotionControlEngine`,
    but the router still keeps the legacy classifier branch available for tooling
    and tests. It reads ``session.config.engine`` per sample, feeds ONLY the active
    engine, and resets the newly-active engine on an engine switch so it starts
    clean (Motion re-seeds its neutral; the classifier empties its window). It
    exposes the same ``add_sample`` / ``reset`` contract as either engine, so the
    BLE loop is untouched, and ``.motion`` stays available for the live tilt
    diagnostics.
    """

    def __init__(self, session: "AppSession", motion, classifier) -> None:
        self._session = session
        self.motion = motion
        self.classifier = classifier
        self._last_engine: str | None = None

    def add_sample(self, elapsed_seconds: float, sample):
        engine_name = self._session.config.engine
        active = self.motion if engine_name == ENGINE_MOTION else self.classifier
        if engine_name != self._last_engine:
            # First sample of the stream OR the first after a profile switch: start
            # the now-active engine clean so its neutral/window is never stale.
            active.reset()
            self._last_engine = engine_name
        return active.add_sample(elapsed_seconds, sample)

    def reset(self) -> None:
        self.motion.reset()
        self.classifier.reset()
        self._last_engine = None


def build_engine_router(
    session: "AppSession",
    args: argparse.Namespace,
    *,
    observer=None,
) -> ProfileEngineRouter:
    """Build BOTH engines + a profile-following router for the BLE loop, so a runtime
    profile switch re-routes input live instead of silently breaking it. The session-
    log ``observer`` is attached to both engines; since only the active one is fed,
    only it logs -- no double-logging."""
    motion = MotionControlEngine(observer=observer)
    apply_motion_profile_settings(
        motion,
        session.config.profile_settings.get(
            session.config.active_profile,
            default_motion_settings_for_profile(session.config.active_profile),
        ),
    )
    return ProfileEngineRouter(
        session,
        motion,
        _build_classifier(args, observer=observer),
    )


def main(argv: Sequence[str] | None = None, *, default_ui: str = "browser") -> int:
    args = parse_args(argv, default_ui=default_ui)
    write_log_line(
        args.log_path,
        (
            "START "
            f"host={args.host} port={args.port} "
            f"ui={args.ui} frozen={getattr(sys, 'frozen', False)}"
        ),
    )
    if not is_loopback_host(args.host):
        warning = (
            f"WARNING: --host {args.host} is not loopback. The /control endpoint "
            "injects keyboard and mouse input and has no authentication, so non-browser "
            "clients that can reach this address can control this PC. "
            "Use 127.0.0.1 unless you fully trust your network."
        )
        write_console_line(warning, stream=sys.stderr)
        write_log_line(args.log_path, warning)
    url = browser_url_for(args.host, args.port)
    if activate_existing_instance(url):
        write_log_line(args.log_path, f"EXISTING_INSTANCE_ACTIVATED url={url}")
        return 0
    config = load_config(args.config_path)
    # Output is deliberately session-scoped. A previous crash, logout or forced
    # shutdown must never make the next launch start injecting input by itself.
    config.output_enabled = bool(args.output_enabled)
    if args.hold_ms is not None:
        config.hold_ms = normalize_hold_ms(args.hold_ms)
    elif config.engine == ENGINE_MOTION and config.hold_ms <= 0:
        # Motion mode RE-EMITS the active intent every sample; with hold_ms == 0
        # that would tap-and-release each sample and stutter exactly like the old
        # discrete path. A non-zero hold makes HoldKeyEmitter keep the bound key
        # held continuously while the lean/twist persists. Only seed this when the
        # user did not pass --hold-ms (an explicit 0 is still honoured).
        config.hold_ms = normalize_hold_ms(DEFAULT_MOTION_HOLD_MS)
    session_logger = None
    if not args.no_session_log:
        try:
            session_log_path = (
                Path(args.session_log)
                if args.session_log
                else default_config_path().parent / "sessions" / ("session-" + time.strftime("%Y%m%d-%H%M%S") + ".jsonl")
            )
            session_logger = SessionLogger(session_log_path)
            write_console_line(f"SESSION_LOG {session_log_path}")
            write_log_line(args.log_path, f"SESSION_LOG {session_log_path}")
        except Exception as exc:
            write_log_line(args.log_path, f"SESSION_LOG_DISABLED {type(exc).__name__}: {exc}")
            session_logger = None
    base_emitter = NullKeyEmitter() if args.dry_run else create_default_key_emitter()
    hold_emitter = HoldKeyEmitter(
        base_emitter,
        hold_ms=config.hold_ms,
        observer=(lambda rec: session_logger.log("key", rec)) if session_logger else None,
    )
    session = AppSession(
        config=config,
        config_path=args.config_path,
        executor=ActionExecutor(key_emitter=hold_emitter),
        logger=session_logger,
    )
    bus = EventBus()
    connection_control = ConnectionControl(
        manual_pairing=not args.auto_reconnect,
        auto_after_first_pairing=True,
    )
    command_bridge = BleCommandBridge()
    # A profile-FOLLOWING router (not a single fixed engine): switching Game <-> Music
    # at runtime re-routes input live instead of silently dropping it (the two engines
    # speak disjoint vocabularies -- see ProfileEngineRouter).
    detector = build_engine_router(
        session,
        args,
        observer=(lambda rec: session_logger.log("gesture", rec)) if session_logger else None,
    )
    # Hand the live MOTION engine to the session so snapshot() can surface its body-
    # frame tilt diagnostics (hd/he/tilt/fire) and the profile-specific turn tuning.
    session.set_motion_engine(detector.motion)
    if session_logger is not None:
        session_logger.log("session_start", {
            "app_version": APP_VERSION,
            "platform": sys.platform,
            "frozen": getattr(sys, "frozen", False),
            "active_profile": config.active_profile,
            "engine": config.engine,
            "hold_ms": config.hold_ms,
            "output_enabled": config.output_enabled,
            "actions": {gesture: binding.description for gesture, binding in config.actions.items()},
            "detector": {
                "window_seconds": args.window_seconds,
                "min_samples": args.min_samples,
                "min_confidence": args.min_confidence,
                "repeat_seconds": args.repeat_seconds,
                "warmup_seconds": args.warmup_seconds,
                "confirm_windows": args.confirm_windows,
            },
        })
    server = AppHttpServer((args.host, args.port), session, bus, connection_control, command_bridge)

    def stop_background_work() -> None:
        connection_control.request_shutdown()
        session.set_output_enabled(False)

    def shutdown_app_server() -> None:
        stop_background_work()
        server.shutdown()

    write_log_line(args.log_path, f"SERVER_READY url={url}")
    thread = threading.Thread(
        target=lambda: asyncio.run(
            run_ble_stream(
                session,
                bus,
                connection_control=connection_control,
                scan_seconds=args.scan_seconds,
                connect_attempts=args.connect_attempts,
                connect_mode=args.connect_mode,
                gatt_profile=args.gatt_profile,
                retry_delay_seconds=args.retry_delay_seconds,
                reconnect_forever=not args.no_reconnect,
                reconnect_delay_seconds=args.reconnect_delay_seconds,
                gatt_timeout_seconds=args.gatt_timeout_seconds,
                detector=detector,
                command_bridge=command_bridge,
            )
        ),
        daemon=True,
    )
    thread.start()
    write_log_line(args.log_path, "BLE_THREAD_STARTED")

    if args.ui == "webview":
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        write_console_line(f"OPEN {url}")
        write_log_line(args.log_path, f"WEBVIEW_START url={url}")
        try:
            run_webview_window(
                url,
                enable_tray=not args.no_tray,
                on_show_window=lambda show_window: setattr(server, "show_window", show_window),
                on_quit_app=lambda quit_app: setattr(server, "quit_app", quit_app),
                on_quit=stop_background_work,
                language=config.lang,
            )
        except Exception as exc:
            write_log_line(args.log_path, f"WEBVIEW_ERROR {type(exc).__name__}: {exc}")
            raise
        finally:
            stop_background_work()
            server.shutdown()
            server.server_close()
            thread.join(timeout=1.0)
            with contextlib.suppress(Exception):
                hold_emitter.close()
            if session_logger is not None:
                with contextlib.suppress(Exception):
                    session_logger.close()
            write_log_line(args.log_path, "SERVER_CLOSED")
        return 0

    if args.ui == "browser":
        server.show_window = lambda: schedule_browser_open(url, 0.0)
        schedule_browser_open(url, args.open_delay_seconds)
        write_log_line(args.log_path, f"BROWSER_OPEN_SCHEDULED delay={args.open_delay_seconds}")
    server.quit_app = shutdown_app_server
    write_console_line(f"OPEN {url}")
    write_log_line(args.log_path, f"SERVE_FOREVER url={url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 130
    finally:
        stop_background_work()
        server.server_close()
        thread.join(timeout=1.0)
        with contextlib.suppress(Exception):
            hold_emitter.close()
        if session_logger is not None:
            with contextlib.suppress(Exception):
                session_logger.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        write_log_line(default_log_path(), f"FATAL {type(exc).__name__}: {exc}")
        raise
