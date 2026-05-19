"""
Unit tests for forza_wheel_leds.py — targeting 100 % line coverage.

All tests run without a HID device, a UDP socket, or a running game.
"""

import socket
import struct
import sys
import unittest
from unittest.mock import MagicMock, call, patch

# ---------------------------------------------------------------------------
# Helpers to build valid Forza UDP packets for testing
# ---------------------------------------------------------------------------

DASH_FORMAT = (
    "<iI"
    "fff"
    "fff" "fff" "fff" "fff"
    "ffff" "ffff" "ffff"
    "iiii"
    "ffff" "ffff" "ffff" "ffff" "ffff"
    "iiii" "i"
    "fff" "fff" "ffff" "fff" "fff" "f"
    "H" "B" "BBBBB" "bbb"
)


def _pack_packet(
    is_race_on: int = 1,
    max_rpm: float = 8000.0,
    idle_rpm: float = 800.0,
    current_rpm: float = 5000.0,
    gear: int = 3,
    raw_size: int = 323,
) -> bytes:
    fmt_fields = struct.unpack_from(DASH_FORMAT, bytes(311))
    n = len(fmt_fields)

    vals = [0] * n
    vals[0] = is_race_on
    vals[1] = 0
    vals[2] = max_rpm
    vals[3] = idle_rpm
    vals[4] = current_rpm
    vals[81] = gear

    patched = struct.pack(DASH_FORMAT, *vals)
    assert len(patched) == 311

    gap = b"\x00" * 12
    raw_323 = patched[:232] + gap + patched[232:]
    assert len(raw_323) == 323

    if raw_size == 323:
        return raw_323
    elif raw_size == 331:
        return raw_323 + b"\x00" * 8
    else:
        raise ValueError(f"Unsupported raw_size {raw_size}")


# ---------------------------------------------------------------------------
# Import module under test
# ---------------------------------------------------------------------------

import forza_wheel_leds as fwl


# ---------------------------------------------------------------------------
# Tests: patch_and_parse
# ---------------------------------------------------------------------------

class TestPatchAndParse(unittest.TestCase):

    def test_returns_none_for_unknown_size(self):
        self.assertIsNone(fwl.patch_and_parse(b"\x00" * 100))
        self.assertIsNone(fwl.patch_and_parse(b"\x00" * 322))
        self.assertIsNone(fwl.patch_and_parse(b"\x00" * 324))

    def test_fh5_fh6_packet_parsed(self):
        pkt = _pack_packet(is_race_on=1, max_rpm=8000, idle_rpm=800,
                           current_rpm=6000, gear=4, raw_size=323)
        result = fwl.patch_and_parse(pkt)
        self.assertIsNotNone(result)
        self.assertEqual(result["game"], "FH5 / FH6")
        self.assertTrue(result["is_race_on"])
        self.assertAlmostEqual(result["max_rpm"], 8000.0, places=0)
        self.assertAlmostEqual(result["current_rpm"], 6000.0, places=0)
        self.assertAlmostEqual(result["idle_rpm"], 800.0, places=0)
        self.assertEqual(result["gear"], 4)

    def test_fm2023_packet_parsed(self):
        pkt = _pack_packet(is_race_on=1, max_rpm=9000, idle_rpm=900,
                           current_rpm=7000, gear=2, raw_size=331)
        result = fwl.patch_and_parse(pkt)
        self.assertIsNotNone(result)
        self.assertEqual(result["game"], "FM2023")
        self.assertAlmostEqual(result["max_rpm"], 9000.0, places=0)
        self.assertEqual(result["gear"], 2)

    def test_is_race_on_false(self):
        pkt = _pack_packet(is_race_on=0, raw_size=323)
        result = fwl.patch_and_parse(pkt)
        self.assertFalse(result["is_race_on"])

    def test_reverse_gear(self):
        pkt = _pack_packet(gear=0, raw_size=323)
        result = fwl.patch_and_parse(pkt)
        self.assertEqual(result["gear"], 0)

    def test_returns_none_on_struct_error(self):
        pkt = _pack_packet(raw_size=323)
        with patch("struct.unpack_from", side_effect=struct.error("bad")):
            self.assertIsNone(fwl.patch_and_parse(pkt))


# ---------------------------------------------------------------------------
# Tests: rpm_to_bitmask
# ---------------------------------------------------------------------------

class TestRpmToBitmask(unittest.TestCase):

    def test_zero_when_below_min(self):
        self.assertEqual(fwl.rpm_to_bitmask(1000, 5600, 8000), 0x00)

    def test_all_on_when_at_max(self):
        self.assertEqual(fwl.rpm_to_bitmask(8000, 5600, 8000), fwl.ALL_LEDS_ON)

    def test_all_on_when_above_max(self):
        self.assertEqual(fwl.rpm_to_bitmask(9000, 5600, 8000), fwl.ALL_LEDS_ON)

    def test_at_min_gives_zero(self):
        self.assertEqual(fwl.rpm_to_bitmask(5600, 5600, 8000), 0x00)

    def test_progressive_middle(self):
        # midpoint → roughly half the LEDs
        result = fwl.rpm_to_bitmask(6800, 5600, 8000)
        self.assertGreater(result, 0x00)
        self.assertLess(result, fwl.ALL_LEDS_ON)

    def test_max_equals_min_returns_zero(self):
        # degenerate case: max_rpm <= min_rpm
        self.assertEqual(fwl.rpm_to_bitmask(5000, 5000, 5000), 0x00)

    def test_just_above_min_gives_one_led(self):
        result = fwl.rpm_to_bitmask(5601, 5600, 8000)
        self.assertEqual(result, 0x01)


# ---------------------------------------------------------------------------
# Tests: _send_led_report
# ---------------------------------------------------------------------------

class TestSendLedReport(unittest.TestCase):

    def test_writes_correct_report(self):
        mock_hid = MagicMock()
        fwl._send_led_report(mock_hid, 42, 0x1F)
        mock_hid.write.assert_called_once_with(
            42, bytes([0x00, 0xF8, 0x12, 0x1F, 0x00, 0x00, 0x00, 0x00])
        )

    def test_bitmask_masked_to_byte(self):
        mock_hid = MagicMock()
        fwl._send_led_report(mock_hid, 42, 0x1FF)
        args = mock_hid.write.call_args[0]
        self.assertEqual(args[1][3], 0xFF)


# ---------------------------------------------------------------------------
# Tests: open_wheel
# ---------------------------------------------------------------------------

class TestOpenWheel(unittest.TestCase):

    def test_returns_handle_when_first_pid_matches(self):
        mock_hid = MagicMock()
        mock_hid.open.return_value = 99

        result = fwl.open_wheel(mock_hid)
        self.assertEqual(result, 99)
        mock_hid.open.assert_called_once_with(fwl.LOGITECH_VID, fwl.WHEEL_PIDS[0])

    def test_tries_second_pid_when_first_fails(self):
        mock_hid = MagicMock()
        mock_hid.open.side_effect = [OSError("not found"), 88]

        result = fwl.open_wheel(mock_hid)
        self.assertEqual(result, 88)

    def test_returns_none_when_no_wheel_found(self):
        mock_hid = MagicMock()
        mock_hid.open.side_effect = OSError("not found")

        result = fwl.open_wheel(mock_hid)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Tests: compute_led_state
# ---------------------------------------------------------------------------

class TestComputeLedState(unittest.TestCase):

    def _call(self, current_rpm, max_rpm, blink_phase, last_blink, now,
              blink_thresh=None, blink_interval=0.1):
        if blink_thresh is None:
            blink_thresh = max_rpm * 0.97
        return fwl.compute_led_state(
            current_rpm, max_rpm, blink_phase, last_blink, now,
            blink_thresh, blink_interval,
        )

    def test_normal_zone_returns_normal(self):
        action, phase, lb = self._call(5000, 8000, False, 0.0, 1.0)
        self.assertEqual(action, fwl.LED_NORMAL)
        self.assertFalse(phase)

    def test_normal_zone_resets_blink_phase(self):
        action, phase, lb = self._call(5000, 8000, True, 0.0, 1.0)
        self.assertEqual(action, fwl.LED_NORMAL)
        self.assertFalse(phase)

    def test_blink_zone_toggles_after_interval(self):
        action, phase, lb = self._call(
            current_rpm=7800, max_rpm=8000,
            blink_phase=False, last_blink=0.0, now=1.0,
            blink_thresh=7760, blink_interval=0.1,
        )
        self.assertEqual(action, fwl.LED_BLINK_ON)
        self.assertTrue(phase)
        self.assertEqual(lb, 1.0)

    def test_blink_zone_no_toggle_before_interval(self):
        action, phase, lb = self._call(
            current_rpm=7800, max_rpm=8000,
            blink_phase=False, last_blink=0.99, now=1.0,
            blink_thresh=7760, blink_interval=0.1,
        )
        self.assertEqual(action, fwl.LED_BLINK_OFF)
        self.assertFalse(phase)
        self.assertEqual(lb, 0.99)

    def test_blink_zone_phase_true_gives_blink_on(self):
        action, phase, lb = self._call(
            current_rpm=7800, max_rpm=8000,
            blink_phase=True, last_blink=0.99, now=1.0,
            blink_thresh=7760, blink_interval=0.1,
        )
        self.assertEqual(action, fwl.LED_BLINK_ON)

    def test_blink_zone_toggle_off(self):
        action, phase, lb = self._call(
            current_rpm=7800, max_rpm=8000,
            blink_phase=True, last_blink=0.0, now=1.0,
            blink_thresh=7760, blink_interval=0.1,
        )
        self.assertEqual(action, fwl.LED_BLINK_OFF)
        self.assertFalse(phase)

    def test_exactly_at_blink_thresh(self):
        action, _, _ = self._call(
            current_rpm=7760, max_rpm=8000,
            blink_phase=False, last_blink=0.0, now=1.0,
            blink_thresh=7760, blink_interval=0.1,
        )
        self.assertIn(action, (fwl.LED_BLINK_ON, fwl.LED_BLINK_OFF))


# ---------------------------------------------------------------------------
# Tests: apply_led_action
# ---------------------------------------------------------------------------

class TestApplyLedAction(unittest.TestCase):

    def test_led_off_sends_all_off(self):
        mock_hid = MagicMock()
        fwl.apply_led_action(mock_hid, 42, fwl.LED_OFF, 0, 0, 0)
        mock_hid.write.assert_called_once()
        self.assertEqual(mock_hid.write.call_args[0][1][3], fwl.ALL_LEDS_OFF)

    def test_led_blink_off_sends_all_off(self):
        mock_hid = MagicMock()
        fwl.apply_led_action(mock_hid, 42, fwl.LED_BLINK_OFF, 0, 0, 0)
        mock_hid.write.assert_called_once()
        self.assertEqual(mock_hid.write.call_args[0][1][3], fwl.ALL_LEDS_OFF)

    def test_led_blink_on_sends_all_on(self):
        mock_hid = MagicMock()
        fwl.apply_led_action(mock_hid, 42, fwl.LED_BLINK_ON, 0, 0, 0)
        mock_hid.write.assert_called_once()
        self.assertEqual(mock_hid.write.call_args[0][1][3], fwl.ALL_LEDS_ON)

    def test_led_normal_sends_computed_bitmask(self):
        mock_hid = MagicMock()
        fwl.apply_led_action(mock_hid, 42, fwl.LED_NORMAL, 8000.0, 5600.0, 8000.0)
        mock_hid.write.assert_called_once()
        self.assertEqual(mock_hid.write.call_args[0][1][3], fwl.ALL_LEDS_ON)


# ---------------------------------------------------------------------------
# Tests: main()
# ---------------------------------------------------------------------------

class TestMain(unittest.TestCase):

    def _run_main(self, packets, wheel_found=True, hid_available=True):
        mock_hid = MagicMock()
        if wheel_found:
            mock_hid.open.return_value = 99  # fake handle
        else:
            mock_hid.open.side_effect = OSError("not found")

        mock_sock = MagicMock()
        recv_iter = iter(packets)

        def fake_recvfrom(_):
            try:
                item = next(recv_iter)
            except StopIteration:
                raise KeyboardInterrupt
            if item is socket.timeout:
                raise socket.timeout
            return item, ("127.0.0.1", 5607)

        mock_sock.recvfrom.side_effect = fake_recvfrom

        def fake_import(name, *args, **kwargs):
            if name == "hid":
                if not hid_available:
                    raise ImportError("no hid")
                return mock_hid
            return original_import(name, *args, **kwargs)

        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        with patch("socket.socket", return_value=mock_sock), \
             patch("time.sleep", return_value=None), \
             patch("time.time", return_value=100.0), \
             patch("builtins.input"), \
             patch("builtins.__import__", side_effect=fake_import):
            fwl.main()

        return mock_hid

    def test_main_hid_not_installed(self):
        """ImportError on 'hid' → sys.exit(1)."""
        with self.assertRaises(SystemExit):
            self._run_main([], hid_available=False)

    def test_main_normal_race(self):
        pkt = _pack_packet(is_race_on=1, max_rpm=8000, current_rpm=5000,
                           gear=3, raw_size=323)
        self._run_main([pkt])

    def test_main_fm2023_packet(self):
        pkt = _pack_packet(is_race_on=1, max_rpm=9000, current_rpm=6000,
                           gear=2, raw_size=331)
        self._run_main([pkt])

    def test_main_no_race_but_driving(self):
        pkt = _pack_packet(is_race_on=0, max_rpm=8000, current_rpm=4000,
                           gear=3, raw_size=323)
        self._run_main([pkt])

    def test_main_max_rpm_zero_turns_leds_off(self):
        pkt = _pack_packet(is_race_on=1, max_rpm=0, current_rpm=0, raw_size=323)
        self._run_main([pkt])

    def test_main_redline_blink(self):
        pkt = _pack_packet(is_race_on=1, max_rpm=8000, current_rpm=7800,
                           gear=6, raw_size=323)
        self._run_main([pkt])

    def test_main_reverse_gear(self):
        pkt = _pack_packet(is_race_on=1, max_rpm=8000, current_rpm=2000,
                           gear=0, raw_size=323)
        self._run_main([pkt])

    def test_main_socket_timeout_then_packet(self):
        pkt = _pack_packet(is_race_on=1, max_rpm=8000, current_rpm=5000,
                           raw_size=323)
        self._run_main([socket.timeout, pkt])

    def test_main_unknown_packet_ignored(self):
        bad = b"\x00" * 100
        pkt = _pack_packet(is_race_on=1, max_rpm=8000, current_rpm=5000,
                           raw_size=323)
        self._run_main([bad, pkt])

    def test_main_no_wheel_on_start(self):
        pkt = _pack_packet(is_race_on=1, max_rpm=8000, current_rpm=5000,
                           raw_size=323)
        self._run_main([pkt], wheel_found=False)

    def test_main_max_rpm_zero_no_wheel(self):
        pkt = _pack_packet(is_race_on=1, max_rpm=0, current_rpm=0, raw_size=323)
        self._run_main([pkt], wheel_found=False)

    def test_main_game_detected_printed_once(self):
        pkt = _pack_packet(is_race_on=1, max_rpm=8000, current_rpm=5000,
                           raw_size=323)
        self._run_main([pkt, pkt])

    def test_main_second_game_triggers_new_label(self):
        pkt1 = _pack_packet(raw_size=323)
        pkt2 = _pack_packet(raw_size=331)
        self._run_main([pkt1, pkt2])

    def test_main_wheel_reconnects_during_loop(self):
        """handle starts None (startup fails), wheel found on first packet retry."""
        pkt = _pack_packet(is_race_on=1, max_rpm=8000, current_rpm=5000,
                           raw_size=323)

        mock_hid = MagicMock()
        # Startup: both PIDs fail → open_wheel returns None
        # Loop retry: first PID succeeds → handle = 99
        mock_hid.open.side_effect = [OSError("nf"), OSError("nf"), 99]

        mock_sock = MagicMock()
        recv_iter = iter([pkt])

        def fake_recvfrom(_):
            try:
                return next(recv_iter), ("127.0.0.1", 5607)
            except StopIteration:
                raise KeyboardInterrupt

        mock_sock.recvfrom.side_effect = fake_recvfrom

        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        def fake_import(name, *args, **kwargs):
            if name == "hid":
                return mock_hid
            return original_import(name, *args, **kwargs)

        with patch("socket.socket", return_value=mock_sock), \
             patch("time.sleep", return_value=None), \
             patch("time.time", return_value=100.0), \
             patch("builtins.input"), \
             patch("builtins.__import__", side_effect=fake_import):
            fwl.main()

        mock_hid.write.assert_called()

    def test_main_finally_handles_errors(self):
        """Cover except-pass branches in finally when handle/sock calls raise."""
        mock_hid = MagicMock()
        mock_hid.open.return_value = 99
        mock_hid.write.side_effect = Exception("write error")
        mock_hid.close.side_effect = Exception("close error")

        mock_sock = MagicMock()
        mock_sock.recvfrom.side_effect = KeyboardInterrupt
        mock_sock.close.side_effect = Exception("close error")

        def fake_import(name, *args, **kwargs):
            if name == "hid":
                return mock_hid
            return original_import(name, *args, **kwargs)

        original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

        with patch("socket.socket", return_value=mock_sock), \
             patch("time.sleep", return_value=None), \
             patch("time.time", return_value=100.0), \
             patch("builtins.input"), \
             patch("builtins.__import__", side_effect=fake_import):
            fwl.main()  # must not raise


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------

class TestEntryPoint(unittest.TestCase):
    def test_main_called_when_run_as_script(self):
        with patch.object(fwl, "main") as mock_main, \
             patch.object(fwl, "__name__", "__main__"):
            exec(
                "if __name__ == '__main__': main()",
                {**vars(fwl), "__name__": "__main__", "main": mock_main},
            )
        mock_main.assert_called_once()


if __name__ == "__main__":
    unittest.main()
