from __future__ import annotations

import contextlib
import ctypes
import platform
import struct
import threading
import time
from dataclasses import dataclass
from typing import Callable

from triki_gestures import normalize_gesture_label


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
    # ',' / '.' -- the classic Doom-family strafe-left / strafe-right keys (also
    # used by the orientation-free Motion engine's tilt-direction strafe). VK_OEM
    # comma/period.
    ",": 0xBC,
    ".": 0xBE,
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
    ",": 0x33,
    ".": 0x34,
}

KEY_NAME_TO_VK.update({chr(code): code - 32 for code in range(ord("a"), ord("z") + 1)})
KEY_NAME_TO_VK.update({str(digit): ord(str(digit)) for digit in range(10)})
KEY_NAME_TO_VK.update({f"f{index}": 0x6F + index for index in range(1, 13)})
# Modifier keys. Games (notably Doom-family shooters) bind Fire/Run/Strafe to
# Ctrl/Shift/Alt, so they must be emittable. Generic names map to the left
# variant; explicit left/right names are also accepted.
KEY_NAME_TO_VK.update(
    {
        "ctrl": 0xA2,
        "control": 0xA2,
        "lctrl": 0xA2,
        "rctrl": 0xA3,
        "shift": 0xA0,
        "lshift": 0xA0,
        "rshift": 0xA1,
        "alt": 0xA4,
        "lalt": 0xA4,
        "ralt": 0xA5,
    }
)
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
KEY_NAME_TO_SCANCODE.update(
    {
        "ctrl": 0x1D,
        "control": 0x1D,
        "lctrl": 0x1D,
        "shift": 0x2A,
        "lshift": 0x2A,
        "rshift": 0x36,
        "alt": 0x38,
        "lalt": 0x38,
    }
)
EXTENDED_KEYS = {0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0xA3, 0xA5}
EXTENDED_SCANCODES = {0x48, 0x49, 0x4B, 0x4D, 0x50, 0x51}
MOUSE_MOVE_DIRECTIONS = {
    "mouse-move-left": (-1, 0),
    "mouse-move-right": (1, 0),
    "mouse-move-up": (0, -1),
    "mouse-move-down": (0, 1),
}
MOUSE_BUTTON_NAMES = {
    "mouse-left-button",
    "mouse-right-button",
    "mouse-middle-button",
}
MOUSE_ACTION_NAMES = frozenset((*MOUSE_MOVE_DIRECTIONS, *MOUSE_BUTTON_NAMES))
DEFAULT_MOUSE_SPEED = 12
MIN_MOUSE_SPEED = 1
MAX_MOUSE_SPEED = 50
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
WINDOWS_MOUSE_BUTTON_FLAGS = {
    "mouse-left-button": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "mouse-right-button": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "mouse-middle-button": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}
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
EV_REL = 0x02
SYN_REPORT = 0
REL_X = 0x00
REL_Y = 0x01
BTN_LEFT = 0x110
BTN_RIGHT = 0x111
BTN_MIDDLE = 0x112
LINUX_MOUSE_BUTTON_CODES = {
    "mouse-left-button": BTN_LEFT,
    "mouse-right-button": BTN_RIGHT,
    "mouse-middle-button": BTN_MIDDLE,
}
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_RELBIT = 0x40045566
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
    ",": 43,
    ".": 47,
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
    "shift": 56,
    "lshift": 56,
    "rshift": 60,
    "alt": 58,
    "lalt": 58,
    "ralt": 61,
    "ctrl": 59,
    "control": 59,
    "lctrl": 59,
    "rctrl": 62,
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


def validate_output_name(output_name: str) -> str:
    normalized = normalize_key_name(output_name)
    if normalized not in MOUSE_ACTION_NAMES:
        vk_for_key(normalized)
    return normalized


def normalize_mouse_speed(value, fallback: int = DEFAULT_MOUSE_SPEED) -> int:
    try:
        return max(MIN_MOUSE_SPEED, min(MAX_MOUSE_SPEED, int(round(float(value)))))
    except (TypeError, ValueError):
        return fallback


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
    ",": 51,
    ".": 52,
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
    "ctrl": 29,
    "control": 29,
    "lctrl": 29,
    "rctrl": 97,
    "shift": 42,
    "lshift": 42,
    "rshift": 54,
    "alt": 56,
    "lalt": 56,
    "ralt": 100,
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

    def _ensure(self):
        if self._emitter is None:
            self._emitter = self._factory()
        return self._emitter

    def press_key(self, key_name: str) -> None:
        self._ensure().press_key(key_name)

    def key_down(self, key_name: str) -> None:
        self._ensure().key_down(key_name)

    def key_up(self, key_name: str) -> None:
        self._ensure().key_up(key_name)

    def move_pointer(self, dx: int, dy: int) -> None:
        self._ensure().move_pointer(dx, dy)

    def close(self) -> None:
        if self._emitter is not None:
            close = getattr(self._emitter, "close", None)
            if close is not None:
                close()


class UnavailableKeyEmitter:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def press_key(self, key_name: str) -> None:
        validate_output_name(key_name)
        raise KeyEmissionError(self.reason)

    def key_down(self, key_name: str) -> None:
        self.press_key(key_name)

    def key_up(self, key_name: str) -> None:
        self.press_key(key_name)

    def move_pointer(self, dx: int, dy: int) -> None:
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
        if normalized in MOUSE_MOVE_DIRECTIONS:
            dx, dy = MOUSE_MOVE_DIRECTIONS[normalized]
            self.move_pointer(dx * DEFAULT_MOUSE_SPEED, dy * DEFAULT_MOUSE_SPEED)
            return
        self.key_down(key_name)
        self.key_up(key_name)

    def key_down(self, key_name: str) -> None:
        self._dispatch(key_name, key_up=False)

    def key_up(self, key_name: str) -> None:
        self._dispatch(key_name, key_up=True)

    def _dispatch(self, key_name: str, *, key_up: bool) -> None:
        normalized = normalize_key_name(key_name)
        if normalized in WINDOWS_MOUSE_BUTTON_FLAGS:
            down_flag, up_flag = WINDOWS_MOUSE_BUTTON_FLAGS[normalized]
            self._send_mouse_input(flags=up_flag if key_up else down_flag)
            return
        if normalized in KEY_NAME_TO_SCANCODE:
            self._send_scancode(scancode_for_key(normalized), key_up=key_up)
            return
        self._send_vk(vk_for_key(normalized), key_up=key_up)

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

    def move_pointer(self, dx: int, dy: int) -> None:
        self._send_mouse_input(dx=int(dx), dy=int(dy), flags=MOUSEEVENTF_MOVE)

    def _send_mouse_input(
        self,
        *,
        dx: int = 0,
        dy: int = 0,
        mouse_data: int = 0,
        flags: int,
    ) -> None:
        item = INPUT(
            type=INPUT_MOUSE,
            union=INPUTUNION(
                mi=MOUSEINPUT(
                    dx=dx,
                    dy=dy,
                    mouseData=mouse_data,
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
            raise KeyEmissionError(f"SendInput failed for mouse flags=0x{flags:04x}{detail}")


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
        self.ioctl(fd, UI_SET_EVBIT, EV_REL)
        for key_name in keys:
            self.ioctl(fd, UI_SET_KEYBIT, linux_evdev_code_for_key(key_name))
        for code in LINUX_MOUSE_BUTTON_CODES.values():
            self.ioctl(fd, UI_SET_KEYBIT, code)
        self.ioctl(fd, UI_SET_RELBIT, REL_X)
        self.ioctl(fd, UI_SET_RELBIT, REL_Y)
        user_dev = struct.pack(
            LINUX_UINPUT_USER_DEV_FORMAT,
            b"TRIKI Control Virtual Input",
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
        normalized = normalize_key_name(key_name)
        if normalized in MOUSE_MOVE_DIRECTIONS:
            dx, dy = MOUSE_MOVE_DIRECTIONS[normalized]
            self.move_pointer(dx * DEFAULT_MOUSE_SPEED, dy * DEFAULT_MOUSE_SPEED)
            return
        self.key_down(key_name)
        self.key_up(key_name)

    def key_down(self, key_name: str) -> None:
        normalized = normalize_key_name(key_name)
        code = LINUX_MOUSE_BUTTON_CODES.get(normalized)
        if code is None:
            code = linux_evdev_code_for_key(normalized)
        self._write_event(EV_KEY, code, 1)
        self._write_event(EV_SYN, SYN_REPORT, 0)

    def key_up(self, key_name: str) -> None:
        normalized = normalize_key_name(key_name)
        code = LINUX_MOUSE_BUTTON_CODES.get(normalized)
        if code is None:
            code = linux_evdev_code_for_key(normalized)
        self._write_event(EV_KEY, code, 0)
        self._write_event(EV_SYN, SYN_REPORT, 0)

    def move_pointer(self, dx: int, dy: int) -> None:
        if dx:
            self._write_event(EV_REL, REL_X, int(dx))
        if dy:
            self._write_event(EV_REL, REL_Y, int(dy))
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
        if normalized in MOUSE_MOVE_DIRECTIONS:
            dx, dy = MOUSE_MOVE_DIRECTIONS[normalized]
            self._move_pointer_unchecked(dx * DEFAULT_MOUSE_SPEED, dy * DEFAULT_MOUSE_SPEED)
            return
        if normalized in MOUSE_BUTTON_NAMES:
            self._send_mouse_button(normalized, key_down=True)
            self.sleep(self.key_press_seconds)
            self._send_mouse_button(normalized, key_down=False)
            return
        if normalized in MACOS_MEDIA_KEY_TYPES:
            self._send_media_key(MACOS_MEDIA_KEY_TYPES[normalized], key_down=True)
            self.sleep(self.key_press_seconds)
            self._send_media_key(MACOS_MEDIA_KEY_TYPES[normalized], key_down=False)
            return
        keycode = macos_keycode_for_key(normalized)
        self._send_keyboard_key(keycode, key_down=True)
        self.sleep(self.key_press_seconds)
        self._send_keyboard_key(keycode, key_down=False)

    def key_down(self, key_name: str) -> None:
        self._set_key(key_name, key_down=True)

    def key_up(self, key_name: str) -> None:
        self._set_key(key_name, key_down=False)

    def _set_key(self, key_name: str, *, key_down: bool) -> None:
        normalized = normalize_key_name(key_name)
        self._require_accessibility_permission()
        if normalized in MOUSE_BUTTON_NAMES:
            self._send_mouse_button(normalized, key_down=key_down)
            return
        if normalized in MACOS_MEDIA_KEY_TYPES:
            self._send_media_key(MACOS_MEDIA_KEY_TYPES[normalized], key_down=key_down)
            return
        self._send_keyboard_key(macos_keycode_for_key(normalized), key_down=key_down)

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

    def move_pointer(self, dx: int, dy: int) -> None:
        self._require_accessibility_permission()
        self._move_pointer_unchecked(dx, dy)

    def _move_pointer_unchecked(self, dx: int, dy: int) -> None:
        x, y = self._mouse_position()
        event = self.quartz.CGEventCreateMouseEvent(
            self.event_source,
            self.quartz.kCGEventMouseMoved,
            (x + int(dx), y + int(dy)),
            self.quartz.kCGMouseButtonLeft,
        )
        if event is None:
            raise KeyEmissionError("macOS mouse movement event creation failed.")
        self.quartz.CGEventPost(self.event_tap, event)

    def _send_mouse_button(self, button_name: str, *, key_down: bool) -> None:
        event_names = {
            "mouse-left-button": (
                "kCGMouseButtonLeft",
                "kCGEventLeftMouseDown",
                "kCGEventLeftMouseUp",
            ),
            "mouse-right-button": (
                "kCGMouseButtonRight",
                "kCGEventRightMouseDown",
                "kCGEventRightMouseUp",
            ),
            "mouse-middle-button": (
                "kCGMouseButtonCenter",
                "kCGEventOtherMouseDown",
                "kCGEventOtherMouseUp",
            ),
        }
        button_attr, down_attr, up_attr = event_names[button_name]
        event = self.quartz.CGEventCreateMouseEvent(
            self.event_source,
            getattr(self.quartz, down_attr if key_down else up_attr),
            self._mouse_position(),
            getattr(self.quartz, button_attr),
        )
        if event is None:
            raise KeyEmissionError(f"macOS mouse button event creation failed for {button_name}")
        self.quartz.CGEventPost(self.event_tap, event)

    def _mouse_position(self) -> tuple[float, float]:
        current = self.quartz.CGEventCreate(None)
        if current is None:
            raise KeyEmissionError("macOS cursor position event creation failed.")
        point = self.quartz.CGEventGetLocation(current)
        if hasattr(point, "x") and hasattr(point, "y"):
            return float(point.x), float(point.y)
        return float(point[0]), float(point[1])

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
        self.downs: list[str] = []
        self.ups: list[str] = []
        self.pointer_moves: list[tuple[int, int]] = []

    def press_key(self, key_name: str) -> None:
        normalized = validate_output_name(key_name)
        if normalized in MOUSE_MOVE_DIRECTIONS:
            dx, dy = MOUSE_MOVE_DIRECTIONS[normalized]
            self.move_pointer(dx * DEFAULT_MOUSE_SPEED, dy * DEFAULT_MOUSE_SPEED)
            return
        self.pressed.append(normalized)

    def key_down(self, key_name: str) -> None:
        self.downs.append(validate_output_name(key_name))

    def key_up(self, key_name: str) -> None:
        self.ups.append(validate_output_name(key_name))

    def move_pointer(self, dx: int, dy: int) -> None:
        self.pointer_moves.append((int(dx), int(dy)))


DEFAULT_HOLD_MS = 400
MAX_HOLD_MS = 2000
DEFAULT_VOLUME_TAP_REPEAT_MS = 80
REPEAT_TAP_KEYS = {"volume-up", "volume-down"}


class HoldKeyEmitter:
    """Wraps a base emitter and turns each ``press_key`` into a timed key hold.

    The TRIKI gesture vocabulary is discrete: a sustained motion (twisting,
    stirring, sliding) re-fires the same gesture every ``repeat_seconds``. A
    plain down+up tap is too short to move a player in keyboard-driven games
    like Doom. With ``hold_ms`` > 0 the first press sends a key-down and a
    background worker releases the key ``hold_ms`` later; a repeat of the same
    gesture before the deadline just extends it. Continuous gesturing therefore
    becomes a continuous key hold that auto-releases shortly after you stop.

    ``hold_ms`` == 0 restores the original instantaneous tap behaviour, so the
    wrapper is safe to leave permanently in place and toggle at runtime.
    """

    def __init__(
        self,
        base,
        *,
        hold_ms: int = 0,
        max_hold_ms: int = MAX_HOLD_MS,
        volume_tap_repeat_ms: int = DEFAULT_VOLUME_TAP_REPEAT_MS,
        mouse_speed: int = DEFAULT_MOUSE_SPEED,
        monotonic: Callable[[], float] = time.monotonic,
        observer=None,
    ) -> None:
        self._base = base
        self._observer = observer
        self._max_hold_ms = max(0, int(max_hold_ms))
        self._volume_tap_repeat_seconds = max(0.0, int(volume_tap_repeat_ms) / 1000.0)
        self._mouse_speed = normalize_mouse_speed(mouse_speed)
        self._monotonic = monotonic
        self._hold_seconds = self._clamp_seconds(hold_ms)
        self._cond = threading.Condition()
        self._deadlines: dict[str, float] = {}
        self._last_repeat_tap: dict[str, float] = {}
        self._worker: threading.Thread | None = None
        self._stop = False

    def _clamp_seconds(self, hold_ms) -> float:
        value = max(0, min(self._max_hold_ms, int(hold_ms)))
        return value / 1000.0

    def _notify(self, action: str, key: str, **extra) -> None:
        if self._observer is None:
            return
        try:
            self._observer({"t": round(self._monotonic(), 4), "action": action, "key": key, **extra})
        except Exception:
            pass

    @property
    def hold_ms(self) -> int:
        return int(round(self._hold_seconds * 1000))

    def set_hold_ms(self, hold_ms: int) -> None:
        with self._cond:
            self._hold_seconds = self._clamp_seconds(hold_ms)
            if self._hold_seconds <= 0 and self._deadlines:
                self._release_all_locked()
            self._cond.notify_all()

    @property
    def mouse_speed(self) -> int:
        return self._mouse_speed

    def set_mouse_speed(self, mouse_speed: int) -> None:
        with self._cond:
            self._mouse_speed = normalize_mouse_speed(mouse_speed)

    def press_key(self, key_name: str) -> None:
        key = normalize_key_name(key_name)
        with self._cond:
            if key in MOUSE_MOVE_DIRECTIONS:
                unit_x, unit_y = MOUSE_MOVE_DIRECTIONS[key]
                dx = unit_x * self._mouse_speed
                dy = unit_y * self._mouse_speed
                self._base.move_pointer(dx, dy)
                self._notify("move", key, dx=dx, dy=dy)
                return
            hold_seconds = self._hold_seconds
            if hold_seconds <= 0:
                self._base.press_key(key)
                self._notify("tap", key)
                return
            if key in REPEAT_TAP_KEYS:
                now = self._monotonic()
                last = self._last_repeat_tap.get(key)
                if last is None or (now - last) >= (self._volume_tap_repeat_seconds - 1e-9):
                    self._last_repeat_tap[key] = now
                    self._base.press_key(key)
                    self._notify("tap", key, repeat_ms=int(round(self._volume_tap_repeat_seconds * 1000)))
                else:
                    self._notify("skip", key, repeat_ms=int(round(self._volume_tap_repeat_seconds * 1000)))
                return
            is_new = key not in self._deadlines
            self._deadlines[key] = self._monotonic() + hold_seconds
            if is_new:
                self._base.key_down(key)
                self._notify("down", key, hold_ms=int(round(hold_seconds * 1000)))
            else:
                self._notify("extend", key, hold_ms=int(round(hold_seconds * 1000)))
            self._ensure_worker_locked()
            self._cond.notify_all()

    # Pass-through primitives so this wrapper can itself be wrapped if needed.
    def key_down(self, key_name: str) -> None:
        self._base.key_down(key_name)

    def key_up(self, key_name: str) -> None:
        self._base.key_up(key_name)

    def _ensure_worker_locked(self) -> None:
        if self._worker is None:
            self._worker = threading.Thread(
                target=self._run, name="triki-hold", daemon=True
            )
            self._worker.start()

    def _run(self) -> None:
        with self._cond:
            while not self._stop:
                if not self._deadlines:
                    self._cond.wait()
                    continue
                now = self._monotonic()
                due = [key for key, deadline in self._deadlines.items() if deadline <= now]
                for key in due:
                    del self._deadlines[key]
                    with contextlib.suppress(Exception):
                        self._base.key_up(key)
                    self._notify("up", key, reason="timeout")
                if self._deadlines:
                    wait_for = max(0.0, min(self._deadlines.values()) - self._monotonic())
                    self._cond.wait(timeout=max(wait_for, 0.005))

    def _release_all_locked(self) -> None:
        for key in list(self._deadlines):
            with contextlib.suppress(Exception):
                self._base.key_up(key)
            self._notify("up", key, reason="flush")
        self._deadlines.clear()

    def release_all(self) -> None:
        with self._cond:
            if self._deadlines:
                self._release_all_locked()
                self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            # Release is performed HERE (synchronously, under _cond) rather than
            # delegated to the daemon worker: the worker is daemon=True and may be
            # killed at interpreter shutdown before draining, which would leave keys
            # physically held. Do not move the release into _run() without making the
            # worker non-daemon and joining it.
            self._stop = True
            self._release_all_locked()
            self._cond.notify_all()
            worker = self._worker
        if worker is not None:
            worker.join(timeout=1.0)
        base_close = getattr(self._base, "close", None)
        if base_close is not None:
            with contextlib.suppress(Exception):
                base_close()


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
            key_name = validate_output_name(key_name)
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
