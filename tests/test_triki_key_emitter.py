import unittest
import ctypes
import threading
import time
from unittest.mock import patch

from triki_key_emitter import (
    DEFAULT_HOLD_MS,
    DEFAULT_VOLUME_TAP_REPEAT_MS,
    DEFAULT_KEYMAP,
    INPUT,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_SCANCODE,
    EV_KEY,
    EV_SYN,
    HoldKeyEmitter,
    KeyEmissionError,
    KeyOutputController,
    LazyKeyEmitter,
    LinuxUInputKeyEmitter,
    MacOSKeyEmitter,
    MAX_HOLD_MS,
    NullKeyEmitter,
    UI_DEV_CREATE,
    UI_DEV_DESTROY,
    UI_SET_EVBIT,
    UI_SET_KEYBIT,
    WindowsKeyEmitter,
    create_default_key_emitter,
    linux_evdev_code_for_key,
    macos_keycode_for_key,
    scancode_for_key,
    vk_for_key,
)


class FakeUser32:
    def __init__(self):
        self.calls = []
        self.inputs = []

    def SendInput(self, count, pointer, size):
        self.calls.append((count, size))
        item = ctypes.cast(pointer, ctypes.POINTER(INPUT)).contents
        self.inputs.append(INPUT.from_buffer_copy(item))
        return count


class FakeBinaryDevice:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def fileno(self):
        return 123

    def close(self):
        self.closed = True


class FakeQuartz:
    kCGHIDEventTap = 0
    kCGSessionEventTap = 1
    kCGEventSourceStateHIDSystemState = 1
    NSSystemDefined = 14

    def __init__(self):
        self.sources = []
        self.keyboard_events = []
        self.system_events = []
        self.posts = []

    def CGEventSourceCreate(self, state):
        source = ("source", state)
        self.sources.append(source)
        return source

    def CGEventCreateKeyboardEvent(self, source, keycode, key_down):
        event = ("keyboard", source, keycode, key_down)
        self.keyboard_events.append(event)
        return event

    def CGEventPost(self, tap, event):
        self.posts.append((tap, event))

    class NSEvent:
        @staticmethod
        def otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            event_type,
            location,
            modifier_flags,
            timestamp,
            window_number,
            context,
            subtype,
            data1,
            data2,
        ):
            return FakeMacSystemEvent(
                event_type,
                location,
                modifier_flags,
                timestamp,
                window_number,
                context,
                subtype,
                data1,
                data2,
            )


class FakeMacSystemEvent:
    def __init__(
        self,
        event_type,
        location,
        modifier_flags,
        timestamp,
        window_number,
        context,
        subtype,
        data1,
        data2,
    ):
        self.payload = (
            event_type,
            location,
            modifier_flags,
            timestamp,
            window_number,
            context,
            subtype,
            data1,
            data2,
        )

    def CGEvent(self):
        return ("system", self.payload)


class KeyEmitterTests(unittest.TestCase):
    def test_send_input_structure_matches_64_bit_windows_layout(self):
        self.assertEqual(ctypes.sizeof(INPUT), 40)

    def test_default_keymap_freezes_usable_gestures(self):
        self.assertEqual(DEFAULT_KEYMAP["rotate-cw"], "right")
        self.assertEqual(DEFAULT_KEYMAP["rotate-ccw"], "left")
        self.assertEqual(DEFAULT_KEYMAP["lift"], "enter")
        self.assertEqual(DEFAULT_KEYMAP["flip-over"], "space")
        self.assertEqual(DEFAULT_KEYMAP["back-forth"], "escape")
        self.assertEqual(DEFAULT_KEYMAP["scrub-cw"], "page-down")
        self.assertEqual(DEFAULT_KEYMAP["scrub-ccw"], "page-up")
        self.assertNotIn("shake", DEFAULT_KEYMAP)
        self.assertNotIn("swirl-cw", DEFAULT_KEYMAP)

    def test_vk_for_key_accepts_game_keys(self):
        self.assertEqual(vk_for_key("right"), 0x27)
        self.assertEqual(vk_for_key("left"), 0x25)
        self.assertEqual(vk_for_key("enter"), 0x0D)
        self.assertEqual(vk_for_key("space"), 0x20)
        self.assertEqual(vk_for_key("escape"), 0x1B)
        self.assertEqual(vk_for_key("page-up"), 0x21)
        self.assertEqual(vk_for_key("page-down"), 0x22)
        self.assertEqual(vk_for_key("="), 0xBB)

    def test_scancode_for_key_accepts_game_keys(self):
        self.assertEqual(scancode_for_key("right"), 0x4D)
        self.assertEqual(scancode_for_key("left"), 0x4B)
        self.assertEqual(scancode_for_key("up"), 0x48)
        self.assertEqual(scancode_for_key("down"), 0x50)
        self.assertEqual(scancode_for_key("space"), 0x39)
        self.assertEqual(scancode_for_key("escape"), 0x01)
        self.assertEqual(scancode_for_key("page-up"), 0x49)
        self.assertEqual(scancode_for_key("page-down"), 0x51)
        self.assertEqual(scancode_for_key("="), 0x0D)

    def test_scancode_for_key_accepts_wasd_digits_and_function_keys(self):
        self.assertEqual(scancode_for_key("w"), 0x11)
        self.assertEqual(scancode_for_key("a"), 0x1E)
        self.assertEqual(scancode_for_key("s"), 0x1F)
        self.assertEqual(scancode_for_key("d"), 0x20)
        self.assertEqual(scancode_for_key("1"), 0x02)
        self.assertEqual(scancode_for_key("0"), 0x0B)
        self.assertEqual(scancode_for_key("f5"), 0x3F)

    def test_windows_emitter_sends_key_down_and_up(self):
        user32 = FakeUser32()
        emitter = WindowsKeyEmitter(user32=user32)

        emitter.press_key("space")

        self.assertEqual(len(user32.calls), 2)

    def test_windows_emitter_uses_scancodes_for_game_keyboard_keys(self):
        user32 = FakeUser32()
        emitter = WindowsKeyEmitter(user32=user32)

        emitter.press_key("right")

        down = user32.inputs[0].union.ki
        up = user32.inputs[1].union.ki
        self.assertEqual(down.wVk, 0)
        self.assertEqual(down.wScan, 0x4D)
        self.assertEqual(down.dwFlags, KEYEVENTF_SCANCODE | KEYEVENTF_EXTENDEDKEY)
        self.assertEqual(up.wVk, 0)
        self.assertEqual(up.wScan, 0x4D)
        self.assertEqual(up.dwFlags, KEYEVENTF_SCANCODE | KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP)

    def test_windows_emitter_uses_scancodes_for_letter_keys(self):
        user32 = FakeUser32()
        emitter = WindowsKeyEmitter(user32=user32)

        emitter.press_key("d")

        down = user32.inputs[0].union.ki
        self.assertEqual(down.wVk, 0)
        self.assertEqual(down.wScan, 0x20)
        self.assertEqual(down.dwFlags, KEYEVENTF_SCANCODE)

    def test_windows_emitter_uses_extended_scancodes_for_page_keys(self):
        user32 = FakeUser32()
        emitter = WindowsKeyEmitter(user32=user32)

        emitter.press_key("page-down")

        down = user32.inputs[0].union.ki
        up = user32.inputs[1].union.ki
        self.assertEqual(down.wVk, 0)
        self.assertEqual(down.wScan, 0x51)
        self.assertEqual(down.dwFlags, KEYEVENTF_SCANCODE | KEYEVENTF_EXTENDEDKEY)
        self.assertEqual(up.dwFlags, KEYEVENTF_SCANCODE | KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP)

    def test_windows_emitter_keeps_media_keys_as_virtual_keys(self):
        user32 = FakeUser32()
        emitter = WindowsKeyEmitter(user32=user32)

        emitter.press_key("volume-up")

        down = user32.inputs[0].union.ki
        self.assertEqual(down.wVk, 0xAF)
        self.assertEqual(down.wScan, 0)
        self.assertEqual(down.dwFlags, 0)

    def test_default_emitter_uses_lazy_linux_uinput_backend(self):
        calls = []
        fake = NullKeyEmitter()

        emitter = create_default_key_emitter(
            system_name="Linux",
            linux_factory=lambda: calls.append("created") or fake,
        )

        self.assertIsInstance(emitter, LazyKeyEmitter)
        self.assertEqual(calls, [])

        emitter.press_key("space")

        self.assertEqual(calls, ["created"])
        self.assertEqual(fake.pressed, ["space"])

    def test_default_emitter_uses_lazy_macos_quartz_backend(self):
        calls = []
        fake = NullKeyEmitter()

        emitter = create_default_key_emitter(
            system_name="Darwin",
            macos_factory=lambda: calls.append("created") or fake,
        )

        self.assertIsInstance(emitter, LazyKeyEmitter)
        self.assertEqual(calls, [])

        emitter.press_key("space")

        self.assertEqual(calls, ["created"])
        self.assertEqual(fake.pressed, ["space"])

    def test_default_emitter_reports_unsupported_platform(self):
        emitter = create_default_key_emitter(system_name="FreeBSD")

        with self.assertRaises(KeyEmissionError) as context:
            emitter.press_key("space")

        self.assertIn("unsupported platform", str(context.exception))

    def test_macos_key_codes_cover_game_keys(self):
        self.assertEqual(macos_keycode_for_key("left"), 123)
        self.assertEqual(macos_keycode_for_key("right"), 124)
        self.assertEqual(macos_keycode_for_key("space"), 49)
        self.assertEqual(macos_keycode_for_key("enter"), 36)
        self.assertEqual(macos_keycode_for_key("page-up"), 116)
        self.assertEqual(macos_keycode_for_key("page-down"), 121)
        self.assertEqual(macos_keycode_for_key("="), 24)
        self.assertEqual(macos_keycode_for_key("w"), 13)
        self.assertEqual(macos_keycode_for_key("f5"), 96)

    def test_macos_emitter_posts_key_down_and_up_events(self):
        quartz = FakeQuartz()
        sleeps = []
        emitter = MacOSKeyEmitter(
            quartz_module=quartz,
            accessibility_checker=lambda: True,
            sleep=sleeps.append,
        )

        emitter.press_key("right")

        self.assertEqual(quartz.sources, [("source", 1)])
        self.assertEqual(
            quartz.keyboard_events,
            [
                ("keyboard", ("source", 1), 124, True),
                ("keyboard", ("source", 1), 124, False),
            ],
        )
        self.assertEqual(
            quartz.posts,
            [
                (1, ("keyboard", ("source", 1), 124, True)),
                (1, ("keyboard", ("source", 1), 124, False)),
            ],
        )
        self.assertEqual(sleeps, [0.025])

    def test_macos_emitter_reports_accessibility_permission(self):
        prompts = []
        emitter = MacOSKeyEmitter(
            quartz_module=FakeQuartz(),
            accessibility_checker=lambda: False,
            accessibility_prompter=lambda: prompts.append("prompted") or False,
        )

        with self.assertRaises(KeyEmissionError) as context:
            emitter.press_key("space")

        self.assertIn("Accessibility", str(context.exception))
        self.assertEqual(prompts, ["prompted"])

    def test_macos_emitter_posts_media_key_system_events(self):
        quartz = FakeQuartz()
        sleeps = []
        emitter = MacOSKeyEmitter(
            quartz_module=quartz,
            accessibility_checker=lambda: True,
            sleep=sleeps.append,
        )

        emitter.press_key("volume-up")

        self.assertEqual(len(quartz.posts), 2)
        self.assertEqual(quartz.posts[0][0], 1)
        self.assertEqual(quartz.posts[1][0], 1)
        down = quartz.posts[0][1]
        up = quartz.posts[1][1]
        self.assertEqual(down[0], "system")
        self.assertEqual(up[0], "system")
        self.assertNotEqual(down[1][7], up[1][7])
        self.assertEqual(sleeps, [0.025])

    def test_linux_evdev_key_codes_cover_game_and_media_keys(self):
        self.assertEqual(linux_evdev_code_for_key("left"), 105)
        self.assertEqual(linux_evdev_code_for_key("right"), 106)
        self.assertEqual(linux_evdev_code_for_key("space"), 57)
        self.assertEqual(linux_evdev_code_for_key("="), 13)
        self.assertEqual(linux_evdev_code_for_key("volume-up"), 115)
        self.assertEqual(linux_evdev_code_for_key("media-play-pause"), 164)

    def test_linux_uinput_emitter_configures_device_and_writes_press_events(self):
        fake_device = FakeBinaryDevice()
        ioctl_calls = []

        def fake_open(path, mode, buffering=0):
            self.assertEqual(str(path), "/tmp/uinput")
            self.assertEqual(mode, "wb")
            self.assertEqual(buffering, 0)
            return fake_device

        def fake_ioctl(fd, request, value=0):
            ioctl_calls.append((fd, request, value))
            return 0

        emitter = LinuxUInputKeyEmitter(
            device_path="/tmp/uinput",
            opener=fake_open,
            ioctl=fake_ioctl,
            sleep=lambda _seconds: None,
        )
        emitter.press_key("right")
        emitter.close()

        self.assertIn((123, UI_SET_EVBIT, EV_KEY), ioctl_calls)
        self.assertIn((123, UI_SET_EVBIT, EV_SYN), ioctl_calls)
        self.assertIn((123, UI_SET_KEYBIT, 106), ioctl_calls)
        self.assertIn((123, UI_DEV_CREATE, 0), ioctl_calls)
        self.assertIn((123, UI_DEV_DESTROY, 0), ioctl_calls)
        self.assertTrue(fake_device.closed)
        payload = b"".join(fake_device.writes)
        self.assertIn((EV_KEY).to_bytes(2, "little"), payload)
        self.assertIn((106).to_bytes(2, "little"), payload)

    def test_output_controller_blocks_keys_until_enabled(self):
        emitter = NullKeyEmitter()
        controller = KeyOutputController(emitter=emitter)

        result = controller.handle_gesture("rotate-cw")

        self.assertFalse(result.emitted)
        self.assertEqual(result.key_name, "right")
        self.assertEqual(emitter.pressed, [])

    def test_output_controller_defaults_to_platform_key_emitter_factory(self):
        emitter = NullKeyEmitter()

        with patch("triki_key_emitter.create_default_key_emitter", return_value=emitter) as factory:
            controller = KeyOutputController(enabled=True)
            result = controller.handle_gesture("lift")

        factory.assert_called_once_with()
        self.assertTrue(result.emitted)
        self.assertEqual(emitter.pressed, ["enter"])

    def test_output_controller_emits_when_enabled(self):
        emitter = NullKeyEmitter()
        controller = KeyOutputController(emitter=emitter, enabled=True)

        result = controller.handle_gesture("lift")

        self.assertTrue(result.emitted)
        self.assertEqual(result.key_name, "enter")
        self.assertEqual(emitter.pressed, ["enter"])

    def test_scrub_defaults_to_page_keys(self):
        emitter = NullKeyEmitter()
        controller = KeyOutputController(emitter=emitter, enabled=True)

        result = controller.handle_gesture("scrub-cw")

        self.assertTrue(result.emitted)
        self.assertEqual(result.key_name, "page-down")
        self.assertEqual(emitter.pressed, ["page-down"])

    def test_output_controller_accepts_legacy_swirl_and_shake_aliases(self):
        emitter = NullKeyEmitter()
        controller = KeyOutputController(emitter=emitter, enabled=True)

        scrub = controller.handle_gesture("swirl-cw")
        back = controller.handle_gesture("shake")

        self.assertTrue(scrub.emitted)
        self.assertEqual(scrub.gesture_label, "scrub-cw")
        self.assertTrue(back.emitted)
        self.assertEqual(back.gesture_label, "back-forth")
        self.assertEqual(emitter.pressed, ["page-down", "escape"])

    def test_mapping_rejects_unknown_key(self):
        controller = KeyOutputController(emitter=NullKeyEmitter())

        with self.assertRaises(ValueError):
            controller.set_mapping("scrub-cw", "fire")

    def test_output_controller_accepts_letter_keys_for_game_profiles(self):
        emitter = NullKeyEmitter()
        controller = KeyOutputController(emitter=emitter, enabled=True)
        controller.set_mapping("rotate-cw", "d")

        result = controller.handle_gesture("rotate-cw")

        self.assertTrue(result.emitted)
        self.assertEqual(result.key_name, "d")
        self.assertEqual(emitter.pressed, ["d"])


class ModifierKeyTests(unittest.TestCase):
    def test_vk_for_modifier_keys(self):
        self.assertEqual(vk_for_key("ctrl"), 0xA2)
        self.assertEqual(vk_for_key("control"), 0xA2)
        self.assertEqual(vk_for_key("shift"), 0xA0)
        self.assertEqual(vk_for_key("alt"), 0xA4)

    def test_scancode_for_modifier_keys(self):
        self.assertEqual(scancode_for_key("ctrl"), 0x1D)
        self.assertEqual(scancode_for_key("shift"), 0x2A)
        self.assertEqual(scancode_for_key("alt"), 0x38)

    def test_windows_emitter_uses_scancode_for_ctrl(self):
        user32 = FakeUser32()
        emitter = WindowsKeyEmitter(user32=user32)

        emitter.press_key("ctrl")

        down = user32.inputs[0].union.ki
        up = user32.inputs[1].union.ki
        self.assertEqual(down.wScan, 0x1D)
        self.assertEqual(down.dwFlags, KEYEVENTF_SCANCODE)
        self.assertEqual(up.dwFlags, KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP)

    def test_windows_emitter_marks_right_modifiers_as_extended_vk(self):
        for name, vk in (("rctrl", 0xA3), ("ralt", 0xA5)):
            user32 = FakeUser32()
            emitter = WindowsKeyEmitter(user32=user32)
            emitter.press_key(name)
            down = user32.inputs[0].union.ki
            up = user32.inputs[1].union.ki
            self.assertEqual(down.wVk, vk)
            self.assertEqual(down.wScan, 0)
            self.assertEqual(down.dwFlags, KEYEVENTF_EXTENDEDKEY)
            self.assertEqual(up.dwFlags, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP)

    def test_windows_emitter_keeps_right_shift_on_scancode_path(self):
        user32 = FakeUser32()
        emitter = WindowsKeyEmitter(user32=user32)

        emitter.press_key("rshift")

        down = user32.inputs[0].union.ki
        self.assertEqual(down.wScan, 0x36)
        self.assertEqual(down.dwFlags, KEYEVENTF_SCANCODE)

    def test_linux_and_macos_modifier_codes(self):
        self.assertEqual(linux_evdev_code_for_key("ctrl"), 29)
        self.assertEqual(macos_keycode_for_key("ctrl"), 59)


class RecordingEmitter:
    def __init__(self):
        self._lock = threading.Lock()
        self.pressed = []
        self.downs = []
        self.ups = []
        self.closed = False

    def press_key(self, key_name):
        with self._lock:
            self.pressed.append(key_name)

    def key_down(self, key_name):
        with self._lock:
            self.downs.append(key_name)

    def key_up(self, key_name):
        with self._lock:
            self.ups.append(key_name)

    def close(self):
        self.closed = True

    def snapshot(self):
        with self._lock:
            return list(self.pressed), list(self.downs), list(self.ups)


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class HoldKeyEmitterTests(unittest.TestCase):
    def test_zero_hold_is_an_instant_tap(self):
        base = RecordingEmitter()
        emitter = HoldKeyEmitter(base, hold_ms=0)

        emitter.press_key("up")

        self.assertEqual(base.pressed, ["up"])
        self.assertEqual(base.downs, [])
        self.assertEqual(base.ups, [])

    def test_clamps_hold_ms_to_max(self):
        emitter = HoldKeyEmitter(RecordingEmitter(), hold_ms=999999)
        self.assertEqual(emitter.hold_ms, MAX_HOLD_MS)

    def test_hold_presses_down_immediately_then_releases(self):
        base = RecordingEmitter()
        emitter = HoldKeyEmitter(base, hold_ms=40)

        emitter.press_key("up")

        self.assertEqual(base.downs, ["up"])  # down is immediate, non-blocking
        self.assertEqual(base.pressed, [])  # never a tap while holding
        emitter.set_hold_ms(0)  # deterministic release, no timer race
        self.assertEqual(base.ups, ["up"])
        emitter.close()

    def test_repeat_extends_hold_without_new_key_down(self):
        base = RecordingEmitter()
        emitter = HoldKeyEmitter(base, hold_ms=60)

        emitter.press_key("up")
        emitter.press_key("up")
        emitter.press_key("up")

        # A sustained gesture re-fires, but the key must stay logically down:
        # exactly one key_down and exactly one key_up on release.
        self.assertEqual(base.downs, ["up"])
        self.assertEqual(base.ups, [])
        emitter.set_hold_ms(0)  # deterministic release, no timer race
        self.assertEqual(base.ups, ["up"])
        emitter.close()

    def test_volume_keys_repeat_as_rate_limited_taps_during_hold(self):
        clock = {"t": 100.0}
        base = RecordingEmitter()
        emitter = HoldKeyEmitter(
            base,
            hold_ms=120,
            volume_tap_repeat_ms=80,
            monotonic=lambda: clock["t"],
        )

        emitter.press_key("volume-up")
        clock["t"] += 0.02
        emitter.press_key("volume-up")
        clock["t"] += 0.06
        emitter.press_key("volume-up")

        self.assertEqual(base.pressed, ["volume-up", "volume-up"])
        self.assertEqual(base.downs, [])
        self.assertEqual(base.ups, [])
        self.assertEqual(DEFAULT_VOLUME_TAP_REPEAT_MS, 80)
        emitter.close()

    def test_non_volume_media_keys_still_do_not_repeat_while_held(self):
        base = RecordingEmitter()
        emitter = HoldKeyEmitter(base, hold_ms=120)

        emitter.press_key("media-next")
        emitter.press_key("media-next")

        self.assertEqual(base.pressed, [])
        self.assertEqual(base.downs, ["media-next"])
        emitter.set_hold_ms(0)
        self.assertEqual(base.ups, ["media-next"])
        emitter.close()

    def test_distinct_keys_held_concurrently(self):
        base = RecordingEmitter()
        emitter = HoldKeyEmitter(base, hold_ms=40)

        emitter.press_key("up")
        emitter.press_key("ctrl")

        self.assertEqual(sorted(base.downs), ["ctrl", "up"])
        emitter.set_hold_ms(0)  # deterministic release, no timer race
        self.assertEqual(sorted(base.ups), ["ctrl", "up"])
        emitter.close()

    def test_timed_worker_eventually_releases(self):
        # Timing-sensitive: keeps coverage of the real auto-release path driven
        # by the background worker's deadline (rather than a forced set_hold_ms).
        base = RecordingEmitter()
        emitter = HoldKeyEmitter(base, hold_ms=40)

        emitter.press_key("up")

        self.assertEqual(base.downs, ["up"])
        self.assertTrue(_wait_until(lambda: base.snapshot()[2] == ["up"]))
        self.assertEqual(base.pressed, [])
        emitter.close()

    def test_set_hold_ms_zero_releases_held_keys(self):
        base = RecordingEmitter()
        emitter = HoldKeyEmitter(base, hold_ms=5000)

        emitter.press_key("up")
        self.assertEqual(base.downs, ["up"])
        emitter.set_hold_ms(0)

        self.assertEqual(base.ups, ["up"])
        # Now behaves as a plain tap.
        emitter.press_key("down")
        self.assertEqual(base.pressed, ["down"])
        emitter.close()

    def test_set_hold_ms_reenables_hold_from_zero(self):
        base = RecordingEmitter()
        emitter = HoldKeyEmitter(base, hold_ms=0)

        emitter.press_key("up")
        self.assertEqual(base.pressed, ["up"])  # zero hold is a plain tap
        self.assertEqual(base.downs, [])

        emitter.set_hold_ms(400)
        emitter.press_key("down")
        self.assertEqual(base.downs, ["down"])  # hold path re-enabled
        emitter.close()

    def test_set_hold_ms_lower_releases_on_new_deadline(self):
        base = RecordingEmitter()
        emitter = HoldKeyEmitter(base, hold_ms=5000)

        emitter.press_key("up")
        self.assertEqual(base.downs, ["up"])
        self.assertEqual(base.ups, [])

        # Shortening the hold takes effect on the next deadline refresh: a repeat
        # of the held gesture re-arms the key on the new (much sooner) deadline,
        # so the worker releases it shortly after — still exactly one down/up.
        emitter.set_hold_ms(40)
        emitter.press_key("up")
        self.assertEqual(base.downs, ["up"])
        self.assertTrue(_wait_until(lambda: base.snapshot()[2] == ["up"]))
        emitter.close()

    def test_close_releases_held_keys_and_base(self):
        base = RecordingEmitter()
        emitter = HoldKeyEmitter(base, hold_ms=5000)

        emitter.press_key("up")
        emitter.close()

        self.assertEqual(base.ups, ["up"])
        self.assertTrue(base.closed)

    def test_observer_reports_tap_down_extend_and_up(self):
        base = RecordingEmitter()
        events = []
        emitter = HoldKeyEmitter(base, hold_ms=0, observer=lambda rec: events.append(rec))

        emitter.press_key("up")  # hold_ms == 0 -> instant tap
        self.assertEqual(events[-1]["action"], "tap")
        self.assertEqual(events[-1]["key"], "up")

        emitter.set_hold_ms(400)
        emitter.press_key("up")  # new hold -> down
        self.assertEqual(events[-1]["action"], "down")
        self.assertEqual(events[-1]["hold_ms"], 400)
        emitter.press_key("up")  # same key again -> extend
        self.assertEqual(events[-1]["action"], "extend")

        emitter.set_hold_ms(0)  # releases the held key -> up
        self.assertTrue(any(e["action"] == "up" and e["key"] == "up" for e in events))
        emitter.close()

    def test_close_stops_worker_thread(self):
        base = RecordingEmitter()
        emitter = HoldKeyEmitter(base, hold_ms=5000)

        # Identify THIS emitter's worker by diffing the live 'triki-hold' set, so
        # the assertion is unaffected by parked workers other tests may leave.
        before = {t for t in threading.enumerate() if t.name == "triki-hold"}
        emitter.press_key("up")
        spawned = [
            t for t in threading.enumerate() if t.name == "triki-hold" and t not in before
        ]
        self.assertEqual(len(spawned), 1)
        worker = spawned[0]

        emitter.close()

        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
