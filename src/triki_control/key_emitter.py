from __future__ import annotations

import ctypes
import platform
import struct
import time
from dataclasses import dataclass
from typing import Callable

from triki_control.gestures import normalize_gesture_label


DEFAULT_KEYMAP: dict[str, str | None] = {
    "rotate-cw": "right",
    "rotate-ccw": "left",
    "lift": "enter",
    "flip-over": "space",
    "back-forth": "escape",
    "scrub-cw": "page-down",
    "scrub-ccw": "page-up",
}

KEY_NAME_TO_VK = {
    "left": 0x25,
    "right": 0x27,
    "enter": 0x0D,
    "return": 0x0D,
    "space": 0x20,
    "escape": 0x1B,
    "esc": 0x1B,
    "up": 0x26,
    "down": 0x28,
    "page-up": 0x21,
    "page-down": 0x22,
    "tab": 0x09,
    "backspace": 0x08,
    "=": 0xBB,
    "volume-mute": 0xAD,
    "volume-down": 0xAE,
    "volume-up": 0xAF,
    "media-next": 0xB0,
    "media-prev": 0xB1,
    "media-stop": 0xB2,
    "media-play-pause": 0xB3,
}
KEY_NAME_TO_SCANCODE = {
    "escape": 0x01,
    "esc": 0x01,
    "enter": 0x1C,
    "return": 0x1C,
    "space": 0x39,
    "tab": 0x0F,
    "backspace": 0x0E,
    "up": 0x48,
    "left": 0x4B,
    "right": 0x4D,
    "down": 0x50,
    "page-up": 0x49,
    "page-down": 0x51,
    "=": 0x0D,
}

KEY_NAME_TO_VK.update({chr(code): code - 32 for code in range(ord("a"), ord("z") + 1)})
KEY_NAME_TO_VK.update({str(digit): ord(str(digit)) for digit in range(10)})
KEY_NAME_TO_VK.update({f"f{index}": 0x6F + index for index in range(1, 13)})
KEY_NAME_TO_SCANCODE.update(
    {
        "q": 0x10,
        "w": 0x11,
        "e": 0x12,
        "r": 0x13,
        "t": 0x14,
        "y": 0x15,
        "u": 0x16,
        "i": 0x17,
        "o": 0x18,
        "p": 0x19,
        "a": 0x1E,
        "s": 0x1F,
        "d": 0x20,
        "f": 0x21,
        "g": 0x22,
        "h": 0x23,
        "j": 0x24,
        "k": 0x25,
        "l": 0x26,
        "z": 0x2C,
        "x": 0x2D,
        "c": 0x2E,
        "v": 0x2F,
        "b": 0x30,
        "n": 0x31,
        "m": 0x32,
        "1": 0x02,
        "2": 0x03,
        "3": 0x04,
        "4": 0x05,
        "5": 0x06,
        "6": 0x07,
        "7": 0x08,
        "8": 0x09,
        "9": 0x0A,
        "0": 0x0B,
        "f1": 0x3B,
        "f2": 0x3C,
        "f3": 0x3D,
        "f4": 0x3E,
        "f5": 0x3F,
        "f6": 0x40,
        "f7": 0x41,
        "f8": 0x42,
        "f9": 0x43,
        "f10": 0x44,
        "f11": 0x57,
        "f12": 0x58,
    }
)
EXTENDED_KEYS = {0x21, 0x22, 0x25, 0x26, 0x27, 0x28}
EXTENDED_SCANCODES = {0x48, 0x49, 0x4B, 0x4D, 0x50, 0x51}
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_SCANCODE = 0x0008
WORD = ctypes.c_uint16
DWORD = ctypes.c_uint32
LONG = ctypes.c_int32
UINT = ctypes.c_uint
ULONG_PTR = ctypes.c_size_t
EV_SYN = 0x00
EV_KEY = 0x01
SYN_REPORT = 0
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
BUS_USB = 0x03
LINUX_INPUT_EVENT_FORMAT = "@llHHi"
LINUX_UINPUT_USER_DEV_FORMAT = "80sHHHHI" + "i" * 256
MACOS_KEY_NAME_TO_KEYCODE = {
    "a": 0,
    "s": 1,
    "d": 2,
    "f": 3,
    "h": 4,
    "g": 5,
    "z": 6,
    "x": 7,
    "c": 8,
    "v": 9,
    "b": 11,
    "q": 12,
    "w": 13,
    "e": 14,
    "r": 15,
    "y": 16,
    "t": 17,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "6": 22,
    "5": 23,
    "=": 24,
    "9": 25,
    "7": 26,
    "8": 28,
    "0": 29,
    "o": 31,
    "u": 32,
    "i": 34,
    "p": 35,
    "enter": 36,
    "return": 36,
    "l": 37,
    "j": 38,
    "k": 40,
    "n": 45,
    "m": 46,
    "tab": 48,
    "space": 49,
    "backspace": 51,
    "escape": 53,
    "esc": 53,
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
    "f6": 97,
    "f7": 98,
    "f8": 100,
    "f9": 101,
    "f10": 109,
    "f11": 103,
    "f12": 111,
    "page-up": 116,
    "page-down": 121,
    "left": 123,
    "right": 124,
    "down": 125,
    "up": 126,
}
MACOS_MEDIA_KEY_TYPES = {
    "volume-up": 0,
    "volume-down": 1,
    "volume-mute": 7,
    "media-play-pause": 16,
    "media-next": 17,
    "media-prev": 18,
}
MACOS_MEDIA_KEY_DOWN_FLAGS = 0xA00
MACOS_MEDIA_KEY_UP_FLAGS = 0xB00
MACOS_SYSTEM_DEFINED_SUBTYPE_AUX_CONTROL_BUTTONS = 8
MACOS_KEY_PRESS_SECONDS = 0.025
MACOS_APPLICATION_SERVICES_PATH = (
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
)
MACOS_CORE_FOUNDATION_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"


class KeyEmissionError(RuntimeError):
    pass


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", WORD),
        ("wScan", WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", LONG),
        ("dy", LONG),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", DWORD),
        ("wParamL", WORD),
        ("wParamH", WORD),
    ]


class INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _fields_ = [
        ("type", DWORD),
        ("union", INPUTUNION),
    ]


def normalize_key_name(key_name: str) -> str:
    return key_name.strip().lower().replace("_", "-")


def vk_for_key(key_name: str) -> int:
    normalized = normalize_key_name(key_name)
    if normalized not in KEY_NAME_TO_VK:
        raise ValueError(f"unsupported key name: {key_name}")
    return KEY_NAME_TO_VK[normalized]


def scancode_for_key(key_name: str) -> int:
    normalized = normalize_key_name(key_name)
    if normalized not in KEY_NAME_TO_SCANCODE:
        raise ValueError(f"unsupported scancode key: {key_name}")
    return KEY_NAME_TO_SCANCODE[normalized]


LINUX_KEY_NAME_TO_EVDEV = {
    "escape": 1,
    "esc": 1,
    "1": 2,
    "2": 3,
    "3": 4,
    "4": 5,
    "5": 6,
    "6": 7,
    "7": 8,
    "8": 9,
    "9": 10,
    "0": 11,
    "=": 13,
    "backspace": 14,
    "tab": 15,
    "q": 16,
    "w": 17,
    "e": 18,
    "r": 19,
    "t": 20,
    "y": 21,
    "u": 22,
    "i": 23,
    "o": 24,
    "p": 25,
    "a": 30,
    "s": 31,
    "d": 32,
    "f": 33,
    "g": 34,
    "h": 35,
    "j": 36,
    "k": 37,
    "l": 38,
    "enter": 28,
    "return": 28,
    "z": 44,
    "x": 45,
    "c": 46,
    "v": 47,
    "b": 48,
    "n": 49,
    "m": 50,
    "space": 57,
    "f1": 59,
    "f2": 60,
    "f3": 61,
    "f4": 62,
    "f5": 63,
    "f6": 64,
    "f7": 65,
    "f8": 66,
    "f9": 67,
    "f10": 68,
    "f11": 87,
    "f12": 88,
    "up": 103,
    "page-up": 104,
    "left": 105,
    "right": 106,
    "down": 108,
    "page-down": 109,
    "volume-mute": 113,
    "volume-down": 114,
    "volume-up": 115,
    "media-next": 163,
    "media-play-pause": 164,
    "media-prev": 165,
    "media-stop": 166,
}


def linux_evdev_code_for_key(key_name: str) -> int:
    normalized = normalize_key_name(key_name)
    if normalized not in LINUX_KEY_NAME_TO_EVDEV:
        raise ValueError(f"unsupported Linux key name: {key_name}")
    return LINUX_KEY_NAME_TO_EVDEV[normalized]


def macos_keycode_for_key(key_name: str) -> int:
    normalized = normalize_key_name(key_name)
    if normalized not in MACOS_KEY_NAME_TO_KEYCODE:
        raise ValueError(f"unsupported macOS key name: {key_name}")
    return MACOS_KEY_NAME_TO_KEYCODE[normalized]


def is_macos_accessibility_trusted() -> bool:
    return _check_macos_accessibility_trust(prompt=False)


def request_macos_accessibility_trust() -> bool:
    return _check_macos_accessibility_trust(prompt=True)


def _check_macos_accessibility_trust(*, prompt: bool) -> bool:
    try:
        application_services = ctypes.CDLL(MACOS_APPLICATION_SERVICES_PATH)
        if prompt:
            return _axis_process_trusted_with_prompt(application_services)
        return _axis_process_trusted(application_services)
    except Exception:
        return False


def _axis_process_trusted(application_services) -> bool:
    application_services.AXIsProcessTrusted.restype = ctypes.c_bool
    return bool(application_services.AXIsProcessTrusted())


def _axis_process_trusted_with_prompt(application_services) -> bool:
    try:
        trusted_with_options = application_services.AXIsProcessTrustedWithOptions
    except AttributeError:
        return _axis_process_trusted(application_services)

    core_foundation = ctypes.CDLL(MACOS_CORE_FOUNDATION_PATH)
    options = _create_macos_accessibility_prompt_options(application_services, core_foundation)
    if not options:
        return _axis_process_trusted(application_services)

    try:
        trusted_with_options.argtypes = (ctypes.c_void_p,)
        trusted_with_options.restype = ctypes.c_bool
        return bool(trusted_with_options(options))
    finally:
        core_foundation.CFRelease.argtypes = (ctypes.c_void_p,)
        core_foundation.CFRelease(options)


def _create_macos_accessibility_prompt_options(application_services, core_foundation):
    prompt_key = ctypes.c_void_p.in_dll(application_services, "kAXTrustedCheckOptionPrompt")
    true_value = ctypes.c_void_p.in_dll(core_foundation, "kCFBooleanTrue")
    keys = (ctypes.c_void_p * 1)(prompt_key.value)
    values = (ctypes.c_void_p * 1)(true_value.value)

    core_foundation.CFDictionaryCreate.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_long,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )
    core_foundation.CFDictionaryCreate.restype = ctypes.c_void_p
    return core_foundation.CFDictionaryCreate(None, keys, values, 1, None, None)


class LazyKeyEmitter:
    def __init__(self, factory: Callable[[], object]) -> None:
        self._factory = factory
        self._emitter = None

    def press_key(self, key_name: str) -> None:
        if self._emitter is None:
            self._emitter = self._factory()
        self._emitter.press_key(key_name)

    def close(self) -> None:
        if self._emitter is not None:
            close = getattr(self._emitter, "close", None)
            if close is not None:
                close()


class UnavailableKeyEmitter:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def press_key(self, key_name: str) -> None:
        vk_for_key(key_name)
        raise KeyEmissionError(self.reason)


class WindowsKeyEmitter:
    def __init__(self, user32=None) -> None:
        if user32 is None:
            self.user32 = ctypes.WinDLL("user32", use_last_error=True)
            self.user32.SendInput.argtypes = (
                UINT,
                ctypes.POINTER(INPUT),
                ctypes.c_int,
            )
            self.user32.SendInput.restype = UINT
        else:
            self.user32 = user32

    def press_key(self, key_name: str) -> None:
        normalized = normalize_key_name(key_name)
        if normalized in KEY_NAME_TO_SCANCODE:
            scancode = scancode_for_key(normalized)
            self._send_scancode(scancode, key_up=False)
            self._send_scancode(scancode, key_up=True)
            return
        vk = vk_for_key(normalized)
        self._send_vk(vk, key_up=False)
        self._send_vk(vk, key_up=True)

    def _send_vk(self, vk: int, *, key_up: bool) -> None:
        flags = KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_KEYS else 0
        if key_up:
            flags |= KEYEVENTF_KEYUP
        self._send_keyboard_input(vk=vk, scancode=0, flags=flags)

    def _send_scancode(self, scancode: int, *, key_up: bool) -> None:
        flags = KEYEVENTF_SCANCODE
        if scancode in EXTENDED_SCANCODES:
            flags |= KEYEVENTF_EXTENDEDKEY
        if key_up:
            flags |= KEYEVENTF_KEYUP
        self._send_keyboard_input(vk=0, scancode=scancode, flags=flags)

    def _send_keyboard_input(self, *, vk: int, scancode: int, flags: int) -> None:
        item = INPUT(
            type=INPUT_KEYBOARD,
            union=INPUTUNION(
                ki=KEYBDINPUT(
                    wVk=vk,
                    wScan=scancode,
                    dwFlags=flags,
                    time=0,
                    dwExtraInfo=0,
                )
            ),
        )
        sent = self.user32.SendInput(1, ctypes.byref(item), ctypes.sizeof(INPUT))
        if sent != 1:
            last_error = ctypes.get_last_error()
            detail = f" last_error={last_error}" if last_error else ""
            raise KeyEmissionError(f"SendInput failed for vk=0x{vk:02x}{detail}")


class LinuxUInputKeyEmitter:
    def __init__(
        self,
        *,
        device_path: str = "/dev/uinput",
        opener=None,
        ioctl=None,
        sleep=None,
        supported_keys: tuple[str, ...] | None = None,
    ) -> None:
        self.device_path = device_path
        self.sleep = sleep if sleep is not None else time.sleep
        if ioctl is None:
            try:
                import fcntl
            except ImportError as exc:
                raise KeyEmissionError("Linux uinput output requires fcntl support.") from exc
            ioctl = fcntl.ioctl
        self.ioctl = ioctl
        self._created = False
        self._device = None
        open_device = opener if opener is not None else open
        try:
            self._device = open_device(self.device_path, "wb", buffering=0)
            self._configure_device(supported_keys)
        except OSError as exc:
            raise KeyEmissionError(
                f"Linux uinput output requires access to {self.device_path}: {exc}"
            ) from exc

    def _configure_device(self, supported_keys: tuple[str, ...] | None) -> None:
        assert self._device is not None
        keys = supported_keys if supported_keys is not None else tuple(LINUX_KEY_NAME_TO_EVDEV)
        fd = self._device.fileno()
        self.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        self.ioctl(fd, UI_SET_EVBIT, EV_SYN)
        for key_name in keys:
            self.ioctl(fd, UI_SET_KEYBIT, linux_evdev_code_for_key(key_name))
        user_dev = struct.pack(
            LINUX_UINPUT_USER_DEV_FORMAT,
            b"TRIKI Control Virtual Keyboard",
            BUS_USB,
            0x5452,
            0x494B,
            1,
            0,
            *([0] * 256),
        )
        self._device.write(user_dev)
        self.ioctl(fd, UI_DEV_CREATE, 0)
        self._created = True
        self.sleep(0.05)

    def press_key(self, key_name: str) -> None:
        code = linux_evdev_code_for_key(key_name)
        self._write_event(EV_KEY, code, 1)
        self._write_event(EV_SYN, SYN_REPORT, 0)
        self._write_event(EV_KEY, code, 0)
        self._write_event(EV_SYN, SYN_REPORT, 0)

    def _write_event(self, event_type: int, code: int, value: int) -> None:
        assert self._device is not None
        payload = struct.pack(LINUX_INPUT_EVENT_FORMAT, 0, 0, event_type, code, value)
        self._device.write(payload)

    def close(self) -> None:
        if self._device is None:
            return
        try:
            if self._created:
                self.ioctl(self._device.fileno(), UI_DEV_DESTROY, 0)
        finally:
            self._created = False
            self._device.close()
            self._device = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class MacOSKeyEmitter:
    def __init__(
        self,
        *,
        quartz_module=None,
        accessibility_checker: Callable[[], bool] = is_macos_accessibility_trusted,
        accessibility_prompter: Callable[[], bool] = request_macos_accessibility_trust,
        sleep=None,
        key_press_seconds: float = MACOS_KEY_PRESS_SECONDS,
    ) -> None:
        if quartz_module is None:
            try:
                import Quartz as quartz_module
            except ImportError as exc:
                raise KeyEmissionError(
                    "macOS key output requires PyObjC Quartz support."
                ) from exc
        self.quartz = quartz_module
        self.accessibility_checker = accessibility_checker
        self.accessibility_prompter = accessibility_prompter
        self.sleep = sleep if sleep is not None else time.sleep
        self.key_press_seconds = key_press_seconds
        self.event_tap = getattr(self.quartz, "kCGSessionEventTap", self.quartz.kCGHIDEventTap)
        self.event_source = self.quartz.CGEventSourceCreate(
            self.quartz.kCGEventSourceStateHIDSystemState
        )
        if self.event_source is None:
            raise KeyEmissionError("macOS CGEvent source creation failed.")

    def press_key(self, key_name: str) -> None:
        normalized = normalize_key_name(key_name)
        self._require_accessibility_permission()
        if normalized in MACOS_MEDIA_KEY_TYPES:
            self._send_media_key(MACOS_MEDIA_KEY_TYPES[normalized], key_down=True)
            self.sleep(self.key_press_seconds)
            self._send_media_key(MACOS_MEDIA_KEY_TYPES[normalized], key_down=False)
            return
        keycode = macos_keycode_for_key(normalized)
        self._send_keyboard_key(keycode, key_down=True)
        self.sleep(self.key_press_seconds)
        self._send_keyboard_key(keycode, key_down=False)

    def _require_accessibility_permission(self) -> None:
        if self.accessibility_checker():
            return
        self.accessibility_prompter()
        raise KeyEmissionError(
            "macOS key output requires Accessibility permission for TRIKI Control. "
            "Grant it in System Settings > Privacy & Security > Accessibility, then restart TRIKI Control."
        )

    def _send_keyboard_key(self, keycode: int, *, key_down: bool) -> None:
        event = self.quartz.CGEventCreateKeyboardEvent(self.event_source, keycode, key_down)
        if event is None:
            raise KeyEmissionError(f"macOS CGEvent creation failed for keycode={keycode}")
        self.quartz.CGEventPost(self.event_tap, event)

    def _send_media_key(self, key_type: int, *, key_down: bool) -> None:
        flags = MACOS_MEDIA_KEY_DOWN_FLAGS if key_down else MACOS_MEDIA_KEY_UP_FLAGS
        data1 = (key_type << 16) | flags
        event = self.quartz.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            self.quartz.NSSystemDefined,
            (0, 0),
            flags,
            0,
            0,
            None,
            MACOS_SYSTEM_DEFINED_SUBTYPE_AUX_CONTROL_BUTTONS,
            data1,
            -1,
        )
        cg_event = event.CGEvent()
        self.quartz.CGEventPost(self.event_tap, cg_event)


def create_default_key_emitter(
    *,
    system_name: str | None = None,
    linux_factory: Callable[[], object] | None = None,
    macos_factory: Callable[[], object] | None = None,
):
    system = system_name if system_name is not None else platform.system()
    if system == "Windows":
        return WindowsKeyEmitter()
    if system == "Linux":
        factory = linux_factory if linux_factory is not None else LinuxUInputKeyEmitter
        return LazyKeyEmitter(factory)
    if system == "Darwin":
        factory = macos_factory if macos_factory is not None else MacOSKeyEmitter
        return LazyKeyEmitter(factory)
    return UnavailableKeyEmitter(f"unsupported platform for key output: {system or 'unknown'}")


class NullKeyEmitter:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    def press_key(self, key_name: str) -> None:
        vk_for_key(key_name)
        self.pressed.append(normalize_key_name(key_name))


@dataclass(frozen=True)
class KeyOutputResult:
    gesture_label: str
    key_name: str | None
    emitted: bool
    reason: str


class KeyOutputController:
    def __init__(
        self,
        *,
        keymap: dict[str, str | None] | None = None,
        emitter=None,
        enabled: bool = False,
    ) -> None:
        self.keymap = _normalized_keymap(DEFAULT_KEYMAP if keymap is None else keymap)
        self.emitter = emitter if emitter is not None else create_default_key_emitter()
        self.enabled = enabled

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled

    def set_mapping(self, gesture_label: str, key_name: str | None) -> None:
        gesture_label = normalize_gesture_label(gesture_label)
        if key_name is not None:
            vk_for_key(key_name)
            key_name = normalize_key_name(key_name)
        self.keymap[gesture_label] = key_name

    def handle_gesture(self, gesture_label: str) -> KeyOutputResult:
        gesture_label = normalize_gesture_label(gesture_label)
        key_name = self.keymap.get(gesture_label)
        if key_name is None:
            return KeyOutputResult(gesture_label, None, False, "gesture is unmapped")
        if not self.enabled:
            return KeyOutputResult(gesture_label, key_name, False, "output disabled")
        self.emitter.press_key(key_name)
        return KeyOutputResult(gesture_label, key_name, True, "key emitted")


def _normalized_keymap(keymap: dict[str, str | None]) -> dict[str, str | None]:
    return {
        normalize_gesture_label(gesture): normalize_key_name(key) if key is not None else None
        for gesture, key in keymap.items()
    }
