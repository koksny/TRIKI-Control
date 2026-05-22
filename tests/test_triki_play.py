import unittest
import asyncio

from triki_calibration_server import ConnectionControl, EventBus
from triki_classifier import GesturePrediction, MotionFeatures
from triki_key_emitter import KeyOutputController, NullKeyEmitter
from triki_play import (
    BATTERY_LEVEL_UUID,
    BleCommandBridge,
    LED_CHARACTERISTIC_UUID,
    PlaySession,
    build_html,
    handle_control,
    parse_args,
    play_button_hint,
    publish_battery_level,
    write_led_state,
)


def prediction(label: str, confidence: float = 0.88) -> GesturePrediction:
    return GesturePrediction(
        label=label,
        confidence=confidence,
        reason="test prediction",
        features=MotionFeatures(
            sample_count=20,
            duration_seconds=0.5,
            gyro_p90=1000.0,
            gyro_p99=2000.0,
            accel_deviation_p99=300.0,
            accel_delta=400.0,
            orientation_angle_degrees=5.0,
            c_mean=700.0,
            c_positive_fraction=0.6,
            c_negative_fraction=0.0,
            c_sign_runs=1,
            c_sequence="+",
            gyro_peak_count=1,
            accel_peak_count=0,
            c_abs_p99=2200.0,
            lateral_gyro_p99=1200.0,
            lateral_accel_p99=800.0,
        ),
    )


class PlayModeTests(unittest.TestCase):
    def test_play_session_records_debug_gesture_until_output_enabled(self):
        emitter = NullKeyEmitter()
        output = KeyOutputController(emitter=emitter, enabled=False)
        session = PlaySession(output=output)

        event = session.record_prediction(1.25, prediction("rotate-cw"))

        self.assertEqual(event["gesture_label"], "rotate-cw")
        self.assertEqual(event["key_name"], "right")
        self.assertFalse(event["key_emitted"])
        self.assertEqual(event["output_reason"], "output disabled")
        self.assertEqual(emitter.pressed, [])
        self.assertEqual(session.snapshot()["key_count"], 0)

    def test_play_session_emits_key_when_output_enabled(self):
        emitter = NullKeyEmitter()
        output = KeyOutputController(emitter=emitter, enabled=True)
        session = PlaySession(output=output)

        event = session.record_prediction(2.0, prediction("lift"))

        self.assertTrue(event["key_emitted"])
        self.assertEqual(event["key_name"], "enter")
        self.assertEqual(emitter.pressed, ["enter"])
        self.assertEqual(session.snapshot()["key_count"], 1)

    def test_scrub_uses_page_keys_in_play_mode_by_default(self):
        emitter = NullKeyEmitter()
        output = KeyOutputController(emitter=emitter, enabled=True)
        session = PlaySession(output=output)

        event = session.record_prediction(3.0, prediction("scrub-cw"))

        self.assertTrue(event["key_emitted"])
        self.assertEqual(event["key_name"], "page-down")
        self.assertEqual(event["output_reason"], "key emitted")
        self.assertEqual(emitter.pressed, ["page-down"])

    def test_handle_control_toggles_output_and_updates_mapping(self):
        emitter = NullKeyEmitter()
        output = KeyOutputController(emitter=emitter)
        session = PlaySession(output=output)
        bus = EventBus()
        control = ConnectionControl(manual_pairing=True)

        state = handle_control(
            session,
            "output",
            {"enabled": True},
            bus=bus,
            connection_control=control,
        )
        self.assertTrue(state["output_enabled"])

        state = handle_control(
            session,
            "map",
            {"gesture_label": "scrub-cw", "key_name": "up"},
            bus=bus,
            connection_control=control,
        )

        keymap = {item["gesture_label"]: item["key_name"] for item in state["keymap"]}
        self.assertEqual(keymap["scrub-cw"], "up")

    def test_pairing_control_prompts_for_physical_button(self):
        session = PlaySession(output=KeyOutputController(emitter=NullKeyEmitter()))
        bus = EventBus()
        control = ConnectionControl(manual_pairing=True)

        state = handle_control(
            session,
            "pairing",
            {},
            bus=bus,
            connection_control=control,
        )

        self.assertEqual(state["status"], "pairing")
        self.assertIn("Press the TRIKI pairing button now", state["message"])
        self.assertTrue(control.is_pairing_requested())

    def test_battery_level_read_publishes_session_state(self):
        class FakeClient:
            def __init__(self):
                self.uuid = None

            async def read_gatt_char(self, uuid):
                self.uuid = uuid
                return bytes([64])

        class BatterySession:
            def __init__(self):
                self.calls = []

            def set_battery_level(self, percent, message=""):
                self.calls.append((percent, message))
                return {"battery": {"percent": percent, "message": message}}

        class CapturingBus:
            def __init__(self):
                self.payloads = []

            def publish(self, payload):
                self.payloads.append(payload)

        client = FakeClient()
        session = BatterySession()
        bus = CapturingBus()

        asyncio.run(publish_battery_level(session, bus, client, timeout_seconds=0.1))

        self.assertEqual(client.uuid, BATTERY_LEVEL_UUID)
        self.assertEqual(session.calls, [(64, "Battery level read from BLE.")])
        self.assertEqual(bus.payloads[-1]["type"], "state")
        self.assertEqual(bus.payloads[-1]["state"]["battery"]["percent"], 64)

    def test_battery_level_read_failure_marks_battery_unavailable(self):
        class FailingClient:
            async def read_gatt_char(self, uuid):
                raise RuntimeError("missing characteristic")

        class BatterySession:
            def __init__(self):
                self.calls = []

            def set_battery_level(self, percent, message=""):
                self.calls.append((percent, message))
                return {"battery": {"percent": percent, "message": message}}

        class CapturingBus:
            def __init__(self):
                self.payloads = []

            def publish(self, payload):
                self.payloads.append(payload)

        session = BatterySession()
        bus = CapturingBus()

        asyncio.run(publish_battery_level(session, bus, FailingClient(), timeout_seconds=0.1))

        self.assertIsNone(session.calls[-1][0])
        self.assertIn("Battery level unavailable", session.calls[-1][1])
        self.assertEqual(bus.payloads[-1]["state"]["battery"]["percent"], None)

    def test_led_write_uses_triki_led_characteristic_and_hold_payloads(self):
        class FakeClient:
            def __init__(self):
                self.writes = []

            async def write_gatt_char(self, uuid, payload, *, response):
                self.writes.append((uuid, bytes(payload), response))

        client = FakeClient()

        asyncio.run(write_led_state(client, True))
        asyncio.run(write_led_state(client, False))

        self.assertEqual(
            client.writes,
            [
                (LED_CHARACTERISTIC_UUID, b"\x01", True),
                (LED_CHARACTERISTIC_UUID, b"\x00", True),
            ],
        )

    def test_ble_command_bridge_reports_not_connected_before_writing_led(self):
        bridge = BleCommandBridge()

        with self.assertRaisesRegex(RuntimeError, "TRIKI is not connected"):
            bridge.set_led(True)

    def test_button_hint_explains_when_to_press_pairing_button(self):
        self.assertIn("Click Press pairing now", play_button_hint("waiting", ""))
        self.assertIn("Press the physical TRIKI button", play_button_hint("pairing", ""))
        self.assertIn("Do not press", play_button_hint("ready", "UART_READY"))

    def test_html_contains_play_controls_and_focus_guidance(self):
        html = build_html()

        self.assertIn("TRIKI Play Mode", html)
        self.assertIn("new EventSource('/events')", html)
        self.assertIn("data-action=\"pairing\"", html)
        self.assertIn("Enable key output", html)
        self.assertIn("Focus the game window after enabling output", html)
        self.assertIn("/control?action=", html)

    def test_parse_args_defaults_to_safe_play_mode(self):
        args = parse_args([])

        self.assertEqual(args.port, 8765)
        self.assertFalse(args.output_enabled)
        self.assertFalse(args.auto_reconnect)
        self.assertEqual(args.connect_mode, "scan")
        self.assertEqual(args.window_seconds, 0.4)
        self.assertEqual(args.min_samples, 6)
        self.assertEqual(args.repeat_seconds, 0.3)
        self.assertEqual(args.warmup_seconds, 0.05)


if __name__ == "__main__":
    unittest.main()
