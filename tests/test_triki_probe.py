import inspect
import unittest

from triki_control.probe import (
    BATTERY_SERVICE_UUID,
    GATT_PROFILES,
    NUS_SERVICE_UUID,
    TRIKI_ADDRESS,
    activation_prompt_for_mode,
    bleak_client_kwargs_for_profile,
    device_not_found_message,
    format_service_dump,
    iter_gatt_profiles,
    make_cached_triki_device,
    movement_prompt_for_label,
    rssi_feedback,
    setup_disconnect_message,
)


class TrikiProbeTests(unittest.TestCase):
    def test_cached_device_uses_known_paired_address(self):
        device = make_cached_triki_device()

        self.assertEqual(device.address, TRIKI_ADDRESS)
        self.assertEqual(device.name, "Triki 308531776")

    def test_cached_mode_prompt_tells_operator_not_to_press_button(self):
        self.assertIn("NIE NACISKAJ", activation_prompt_for_mode("cached"))

    def test_scan_mode_prompt_identifies_pairing_button_flow(self):
        self.assertIn("SCAN", activation_prompt_for_mode("scan"))

    def test_probe_signature_accepts_connect_mode(self):
        from triki_control.probe import probe

        self.assertIn("connect_mode", inspect.signature(probe).parameters)

    def test_auto_gatt_profile_tries_cached_then_uncached_nus(self):
        self.assertEqual(
            tuple(iter_gatt_profiles("auto")),
            GATT_PROFILES,
        )

    def test_bleak_client_kwargs_for_cached_nus_profile(self):
        self.assertEqual(
            bleak_client_kwargs_for_profile("nus-cached"),
            {
                "services": [NUS_SERVICE_UUID, BATTERY_SERVICE_UUID],
                "winrt": {"use_cached_services": True},
            },
        )

    def test_bleak_client_kwargs_for_uncached_nus_profile(self):
        self.assertEqual(
            bleak_client_kwargs_for_profile("nus-uncached"),
            {
                "services": [NUS_SERVICE_UUID, BATTERY_SERVICE_UUID],
                "winrt": {"use_cached_services": False},
            },
        )

    def test_movement_prompt_for_still_tells_operator_to_hold_still(self):
        self.assertIn("KEEP_STILL_NOW", movement_prompt_for_label("still"))

    def test_movement_prompt_for_gesture_tells_operator_to_start(self):
        prompt = movement_prompt_for_label("rotate-cw")

        self.assertIn("START_MOVING_NOW", prompt)
        self.assertIn("rotate-cw", prompt)

    def test_service_dump_formats_missing_services(self):
        self.assertEqual(format_service_dump(None), "services=<unavailable>")

    def test_cached_device_not_found_message_is_retry_friendly(self):
        message = device_not_found_message("cached")

        self.assertIn("CACHED_DEVICE_NOT_READY", message)
        self.assertIn("retry", message.lower())

    def test_rssi_feedback_warns_when_signal_is_weak(self):
        message = rssi_feedback(-86)

        self.assertIsNotNone(message)
        self.assertIn("RSSI_WEAK", message)

    def test_rssi_feedback_is_quiet_for_healthy_signal(self):
        self.assertIsNone(rssi_feedback(-60))

    def test_setup_disconnect_message_identifies_pre_uart_abort(self):
        message = setup_disconnect_message("nus-cached", OSError(995, "cancelled"))

        self.assertIn("DISCONNECTED_BEFORE_UART_READY", message)
        self.assertIn("nus-cached", message)

    def test_probe_signature_accepts_retry_delay(self):
        from triki_control.probe import probe

        self.assertIn("retry_delay_seconds", inspect.signature(probe).parameters)


if __name__ == "__main__":
    unittest.main()
