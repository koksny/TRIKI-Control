import json
import asyncio
import unittest

from triki_control.calibration import CalibrationAction, CalibrationSession
from triki_control.calibration_server import (
    build_arg_parser,
    build_html,
    ConnectionControl,
    disconnect_message,
    encode_sse,
    EventBus,
    gatt_characteristic_missing_message,
    handle_pairing_request,
    handle_control,
    is_quiet_404,
    iter_connection_targets,
    session_exception_status_and_message,
)
from triki_control.calibration_server import iter_connect_modes, should_reconnect_after_session
from triki_control.calibration_server import quiet_stream_errors


class CalibrationServerTests(unittest.TestCase):
    def test_encode_sse_formats_json_data_event(self):
        payload = encode_sse({"type": "state", "ok": True})

        self.assertEqual(payload, 'data: {"type":"state","ok":true}\n\n')

    def test_control_next_advances_session(self):
        session = CalibrationSession(
            actions=[
                CalibrationAction("still", "Still", "Still."),
                CalibrationAction("rotate-cw", "Rotate CW", "Rotate."),
            ]
        )

        result = handle_control(session, "next", {})

        self.assertEqual(result["current"]["label"], "rotate-cw")

    def test_control_select_changes_session_index(self):
        session = CalibrationSession(
            actions=[
                CalibrationAction("still", "Still", "Still."),
                CalibrationAction("rotate-cw", "Rotate CW", "Rotate."),
            ]
        )

        result = handle_control(session, "select", {"index": 1})

        self.assertEqual(result["current_index"], 1)

    def test_control_unknown_action_reports_error(self):
        session = CalibrationSession()

        with self.assertRaises(ValueError):
            handle_control(session, "bad-action", {})

    def test_pairing_request_prompts_for_physical_button_once(self):
        session = CalibrationSession()
        bus = EventBus()
        control = ConnectionControl(manual_pairing=True)

        result = handle_pairing_request(session, bus, control)

        self.assertEqual(result["status"], "pairing")
        self.assertIn("Press the TRIKI pairing button now", result["message"])
        self.assertTrue(control.is_pairing_requested())

    def test_manual_connection_control_waits_until_pairing_request(self):
        async def scenario():
            session = CalibrationSession()
            bus = EventBus()
            control = ConnectionControl(manual_pairing=True)

            wait_task = asyncio.create_task(control.wait_for_pairing_request(session, bus))
            await asyncio.sleep(0.05)

            self.assertFalse(wait_task.done())
            self.assertEqual(session.status, "waiting")
            self.assertIn("Press pairing now", session.message)

            control.request_pairing(session, bus)
            await asyncio.wait_for(wait_task, timeout=1.0)

        asyncio.run(scenario())

    def test_connection_control_can_auto_reconnect_after_first_pairing_request(self):
        async def scenario():
            session = CalibrationSession()
            bus = EventBus()
            control = ConnectionControl(manual_pairing=True, auto_after_first_pairing=True)

            first_wait = asyncio.create_task(control.wait_for_pairing_request(session, bus))
            await asyncio.sleep(0.05)
            self.assertFalse(first_wait.done())

            control.request_pairing(session, bus)
            await asyncio.wait_for(first_wait, timeout=1.0)

            second_wait = asyncio.create_task(control.wait_for_pairing_request(session, bus))
            await asyncio.wait_for(second_wait, timeout=1.0)
            self.assertEqual(session.status, "pairing")

        asyncio.run(scenario())

    def test_html_contains_event_source_and_step_controls(self):
        html = build_html()

        self.assertIn("new EventSource('/events')", html)
        self.assertIn("data-action=\"next\"", html)
        self.assertIn("data-action=\"pairing\"", html)
        self.assertIn("Press pairing now", html)
        self.assertIn("TRIKI Calibration", html)
        self.assertIn('id="button-hint"', html)
        self.assertIn('id="connection-log"', html)
        self.assertIn("gyro=${event.features.gyro_p99}", html)
        self.assertIn("accel=${event.features.accel_deviation_p99}", html)
        self.assertIn("latG=${event.features.lateral_gyro_p99}", html)
        self.assertIn("latA=${event.features.lateral_accel_p99}", html)
        self.assertIn("loop=${event.features.lateral_accel_area_norm}", html)
        self.assertIn("line=${event.features.lateral_accel_pca_ratio}", html)
        self.assertIn("cAbs=${event.features.c_abs_p99}", html)
        self.assertIn("fDrop=${event.features.f_abs_drop_delta}", html)
        self.assertIn("fPost=${event.features.f_abs_peak_after_drop_delta}", html)
        self.assertIn("fAfter=${event.features.f_abs_post_peak_sample_count}", html)

    def test_html_defaults_to_dark_theme_with_toggle(self):
        html = build_html()

        self.assertIn('data-theme="dark"', html)
        self.assertIn('id="theme-toggle"', html)
        self.assertIn("localStorage.setItem('triki-theme'", html)

    def test_favicon_is_handled_without_visible_console_error(self):
        self.assertTrue(is_quiet_404("/favicon.ico"))
        self.assertFalse(is_quiet_404("/missing"))

    def test_calibrator_reconnects_after_successful_disconnected_session(self):
        self.assertTrue(should_reconnect_after_session(0, reconnect_forever=True))

    def test_calibrator_can_stop_reconnecting_when_disabled(self):
        self.assertFalse(should_reconnect_after_session(0, reconnect_forever=False))

    def test_hybrid_connect_mode_tries_cached_before_scan(self):
        self.assertEqual(iter_connect_modes("hybrid"), ("cached", "scan"))

    def test_single_connect_mode_is_preserved(self):
        self.assertEqual(iter_connect_modes("scan"), ("scan",))

    def test_cached_auto_connection_strategy_only_uses_cached_services(self):
        self.assertEqual(
            iter_connection_targets("cached", "auto"),
            (("cached", "nus-cached"),),
        )

    def test_hybrid_auto_connection_strategy_rediscovery_happens_after_scan(self):
        self.assertEqual(
            iter_connection_targets("hybrid", "auto"),
            (
                ("cached", "nus-cached"),
                ("scan", "nus-cached"),
                ("scan", "nus-uncached"),
            ),
        )

    def test_missing_uart_characteristic_message_explains_stale_gatt_cache(self):
        message = gatt_characteristic_missing_message("nus-cached", "cached")

        self.assertIn("GATT_PROFILE_STALE", message)
        self.assertIn("mode=cached", message)
        self.assertIn("do not press", message.lower())

    def test_pre_uart_disconnect_is_not_reported_as_generic_session_error(self):
        status, message = session_exception_status_and_message(
            OSError(-2147023673, "Operation canceled"),
            "nus-cached",
            "cached",
        )

        self.assertEqual(status, "retrying")
        self.assertIn("GATT_DROPPED_BEFORE_UART_READY", message)
        self.assertIn("Windows may still show Connected", message)
        self.assertIn("UART_READY", message)
        self.assertNotIn("SESSION_ERROR", message)

    def test_disconnect_message_includes_session_uptime(self):
        message = disconnect_message("Lift / Enter", 5.4)

        self.assertIn("TRIKI disconnected during Lift / Enter", message)
        self.assertIn("after 5.4s", message)

    def test_default_connection_strategy_uses_cached_then_scan_rediscovery(self):
        args = build_arg_parser().parse_args([])

        self.assertEqual(args.connect_mode, "scan")
        self.assertFalse(args.auto_reconnect)
        self.assertEqual(args.scan_seconds, 10.0)
        self.assertEqual(args.gatt_timeout_seconds, 12.0)
        self.assertEqual(args.window_seconds, 0.75)
        self.assertEqual(args.repeat_seconds, 0.55)
        self.assertEqual(args.warmup_seconds, 0.2)
        self.assertEqual(args.confirm_windows, 1)

    def test_sse_disconnects_are_quiet_for_aborted_browser_connections(self):
        self.assertIn(ConnectionAbortedError, quiet_stream_errors())


if __name__ == "__main__":
    unittest.main()
