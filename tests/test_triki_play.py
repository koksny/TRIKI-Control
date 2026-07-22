import unittest
import asyncio
import json
import threading

from triki_calibration_server import ConnectionControl, EventBus
from triki_classifier import GesturePrediction, MotionFeatures
from triki_key_emitter import KeyOutputController, NullKeyEmitter
from triki_play import (
    BATTERY_LEVEL_UUID,
    MOTION_PUBLISH_INTERVAL_SECONDS,
    BleCommandBridge,
    LED_CHARACTERISTIC_UUID,
    PlaySession,
    build_html,
    build_motion_event,
    disconnect_client,
    handle_control,
    parse_args,
    play_button_hint,
    publish_battery_level,
    write_led_state,
)
from triki_protocol import MotionSample


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
        with self.assertRaisesRegex(RuntimeError, "TRIKI is not connected"):
            bridge.disconnect()

    def test_disconnect_client_closes_ble_connection(self):
        class FakeClient:
            def __init__(self):
                self.disconnects = 0

            async def disconnect(self):
                self.disconnects += 1

        client = FakeClient()
        asyncio.run(disconnect_client(client))

        self.assertEqual(client.disconnects, 1)

    def test_ble_command_bridge_disconnects_on_attached_event_loop(self):
        class FakeClient:
            def __init__(self):
                self.is_connected = True
                self.disconnects = 0

            async def disconnect(self):
                self.disconnects += 1
                self.is_connected = False

        loop = asyncio.new_event_loop()
        loop_thread = threading.Thread(target=loop.run_forever)
        loop_thread.start()
        client = FakeClient()
        bridge = BleCommandBridge()
        bridge.attach(client, loop)
        try:
            bridge.disconnect()
        finally:
            loop.call_soon_threadsafe(loop.stop)
            loop_thread.join(timeout=1.0)
            loop.close()

        self.assertEqual(client.disconnects, 1)
        self.assertFalse(client.is_connected)

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

    def test_html_key_dropdown_covers_default_keymap_and_modifiers(self):
        html = build_html()

        # The play-mode default keymap maps scrub-cw/scrub-ccw to page-down/page-up,
        # so the dropdown must offer those or those default rows render as
        # 'debug only' and risk an accidental overwrite.
        self.assertIn("'page-up'", html)
        self.assertIn("'page-down'", html)
        # Modifier keys enable Doom Fire/Use parity in play mode.
        self.assertIn("'ctrl'", html)

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


class LiveMotionEventTests(unittest.TestCase):
    def test_build_motion_event_carries_six_axes_and_derived_hints(self):
        sample = MotionSample(packet_id=7, values=(30, 40, 120, -50, 60, -2050))

        event = build_motion_event(sample.values, elapsed_seconds=1.23456)

        self.assertEqual(event["type"], "motion")
        # All six raw axes are streamed (a,b,c gyro / d,e,f accel).
        self.assertEqual(event["values"], [30, 40, 120, -50, 60, -2050])
        # rotation hint = gyro-c (values[2]) = the cap-spin axis.
        self.assertEqual(event["rotation"], 120)
        # energy = gyro vector magnitude sqrt(30^2 + 40^2 + 120^2) = 130.0.
        self.assertEqual(event["energy"], 130.0)
        self.assertEqual(event["elapsed_seconds"], round(1.23456, 4))

    def test_build_motion_event_is_jsonable_and_uses_plain_ints(self):
        # bleak hands us a numpy-free tuple, but guard against non-list/int leaks
        # that would break encode_sse(json.dumps).
        event = build_motion_event((1, 2, 3, 4, 5, 6), elapsed_seconds=0.0)

        self.assertIsInstance(event["values"], list)
        self.assertTrue(all(isinstance(v, int) for v in event["values"]))
        json.dumps(event)

    def test_motion_publish_is_throttled_independently_of_sample_gate(self):
        # Replicate the on_notify gate: a 'motion' event fires whenever
        # elapsed advances by >= MOTION_PUBLISH_INTERVAL_SECONDS, regardless of
        # the slower 0.25s sample/state gate. We mock the bus and the clock so
        # the test needs no hardware, BLE, or real time.
        published = []
        last_motion_publish = 0.0
        last_sample_publish = 0.0

        # ~15 Hz cap stream over one second of samples arriving at ~50 Hz.
        for step in range(50):
            elapsed = step * 0.02
            if elapsed - last_motion_publish >= MOTION_PUBLISH_INTERVAL_SECONDS:
                published.append(("motion", elapsed))
                last_motion_publish = elapsed
            if elapsed - last_sample_publish >= 0.25:
                published.append(("sample", elapsed))
                last_sample_publish = elapsed

        motion_times = [t for kind, t in published if kind == "motion"]
        sample_times = [t for kind, t in published if kind == "sample"]

        # Motion clearly outpaces the slower 0.25s sample/state gate: ~4
        # sample-gate firings vs ~12 motion firings over the same second.
        self.assertGreater(len(motion_times), len(sample_times))
        self.assertLessEqual(len(sample_times), 4)
        # Samples arriving at ~50 Hz (0.02s) gate-snap to every 4th sample
        # (0.08s) because 0.06s < 0.08s, so ~12 motion events fill ~1s without
        # the per-notify flood of one event per sample (which would be 50).
        self.assertGreaterEqual(len(motion_times), 12)
        self.assertLessEqual(len(motion_times), 15)
        self.assertLess(len(motion_times), 50)
        # Every gap between consecutive motion publishes respects the interval.
        gaps = [b - a for a, b in zip(motion_times, motion_times[1:])]
        for gap in gaps:
            self.assertGreaterEqual(gap + 1e-9, MOTION_PUBLISH_INTERVAL_SECONDS)

    def test_motion_interval_targets_about_15hz(self):
        # ~15 Hz keeps the WebView animation smooth without flooding SSE.
        self.assertAlmostEqual(1.0 / MOTION_PUBLISH_INTERVAL_SECONDS, 15.0, places=6)


if __name__ == "__main__":
    unittest.main()
