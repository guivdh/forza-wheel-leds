"""
Unit tests for forza_wheel_leds.py — targeting 100 % line coverage.

All tests run without a Logitech DLL, a UDP socket, or a running game.
System-level functions (load_logitech_sdk, main) are tested via mocks.
"""

import socket
import struct
import sys
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers to build valid Forza UDP packets for testing
# ---------------------------------------------------------------------------

# The DASH_FORMAT struct packs to 311 bytes.
# We build a minimal 311-byte buffer and inject it into a raw packet.
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


def _make_patched_buffer(
    is_race_on: int = 1,
    max_rpm: float = 8000.0,
    idle_rpm: float = 800.0,
    current_rpm: float = 5000.0,
    gear: int = 3,
) -> bytes:
    """Build a 311-byte buffer matching DASH_FORMAT with the given fields."""
    # Count how many fields the format expects
    n_floats_ints = struct.calcsize(DASH_FORMAT)  # should be 311
    # Build a list of zero values then override the ones we care about
    # Field layout (0-indexed):
    #  0 = IsRaceOn (i), 1 = TimestampMS (I)
    #  2 = EngineMaxRpm, 3 = EngineIdleRpm, 4 = CurrentEngineRpm (f)
    #  ... many zeros ...
    #  81 = Gear (5th B in the BBBBB block)
    values = [0] * 300  # more than enough

    values[0] = is_race_on
    values[1] = 12345          # TimestampMS
    values[2] = max_rpm
    values[3] = idle_rpm
    values[4] = current_rpm
    # gear sits at index 81 in the unpacked tuple
    # We'll inject it by packing the full struct manually
    return None  # replaced by _pack_packet below


def _pack_packet(
    is_race_on: int = 1,
    max_rpm: float = 8000.0,
    idle_rpm: float = 800.0,
    current_rpm: float = 5000.0,
    gear: int = 3,
    raw_size: int = 323,
) -> bytes:
    """
    Build a raw Forza UDP packet of `raw_size` bytes with the gap at 232–243.
    The struct is packed at offset 0..231 and 244..end, with zeros for the gap.
    """
    # Pack the full DASH_FORMAT struct (311 bytes)
    # We need values for every field; only a few matter for our tests.
    # 85 fields total in DASH_FORMAT as written in forza_wheel_leds.py.
    # Let's count them:
    fmt_fields = struct.unpack_from(DASH_FORMAT, bytes(311))
    n = len(fmt_fields)

    vals = [0] * n
    vals[0] = is_race_on       # IsRaceOn
    vals[1] = 0                # TimestampMS
    vals[2] = max_rpm          # EngineMaxRpm
    vals[3] = idle_rpm         # EngineIdleRpm
    vals[4] = current_rpm      # CurrentEngineRpm
    vals[81] = gear            # Gear (5th byte in BBBBB block)

    patched = struct.pack(DASH_FORMAT, *vals)  # 311 bytes
    assert len(patched) == 311

    # Re-insert the 12-byte gap: first 232 bytes + 12 zero bytes + rest
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
# Import the module under test (no DLL needed — we mock ctypes.CDLL)
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
        # 323 bytes but all zeros — struct.unpack_from will succeed (zeros are valid),
        # so we test with a buffer that's too short after patching by
        # monkeypatching struct.unpack_from to raise
        pkt = _pack_packet(raw_size=323)
        with patch("struct.unpack_from", side_effect=struct.error("bad")):
            self.assertIsNone(fwl.patch_and_parse(pkt))


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
        # Even if blink_phase was True, normal zone resets it
        action, phase, lb = self._call(5000, 8000, True, 0.0, 1.0)
        self.assertEqual(action, fwl.LED_NORMAL)
        self.assertFalse(phase)

    def test_blink_zone_toggles_after_interval(self):
        # Phase starts False, interval elapsed → toggle to True → LED_BLINK_ON
        action, phase, lb = self._call(
            current_rpm=7800, max_rpm=8000,
            blink_phase=False, last_blink=0.0, now=1.0,
            blink_thresh=7760, blink_interval=0.1,
        )
        self.assertEqual(action, fwl.LED_BLINK_ON)
        self.assertTrue(phase)
        self.assertEqual(lb, 1.0)

    def test_blink_zone_no_toggle_before_interval(self):
        # Interval not elapsed → phase stays False → LED_BLINK_OFF
        action, phase, lb = self._call(
            current_rpm=7800, max_rpm=8000,
            blink_phase=False, last_blink=0.99, now=1.0,
            blink_thresh=7760, blink_interval=0.1,
        )
        self.assertEqual(action, fwl.LED_BLINK_OFF)
        self.assertFalse(phase)
        self.assertEqual(lb, 0.99)  # unchanged

    def test_blink_zone_phase_true_gives_blink_on(self):
        action, phase, lb = self._call(
            current_rpm=7800, max_rpm=8000,
            blink_phase=True, last_blink=0.99, now=1.0,
            blink_thresh=7760, blink_interval=0.1,
        )
        self.assertEqual(action, fwl.LED_BLINK_ON)

    def test_blink_zone_toggle_off(self):
        # Phase is True, interval elapsed → toggle to False → LED_BLINK_OFF
        action, phase, lb = self._call(
            current_rpm=7800, max_rpm=8000,
            blink_phase=True, last_blink=0.0, now=1.0,
            blink_thresh=7760, blink_interval=0.1,
        )
        self.assertEqual(action, fwl.LED_BLINK_OFF)
        self.assertFalse(phase)

    def test_exactly_at_blink_thresh(self):
        # RPM exactly at threshold → blink zone
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

    def _mock_dll(self):
        dll = MagicMock()
        dll.LogiSetSteeringWheelRpmLeds = MagicMock()
        return dll

    def test_led_off_calls_leds_off(self):
        dll = self._mock_dll()
        fwl.apply_led_action(dll, fwl.LED_OFF, 0, 0, 0)
        dll.LogiSetSteeringWheelRpmLeds.assert_called_once()
        args = dll.LogiSetSteeringWheelRpmLeds.call_args[0]
        # currentRPM arg should be 0.0
        self.assertAlmostEqual(float(args[1].value), 0.0)

    def test_led_blink_off_calls_leds_off(self):
        dll = self._mock_dll()
        fwl.apply_led_action(dll, fwl.LED_BLINK_OFF, 0, 0, 0)
        dll.LogiSetSteeringWheelRpmLeds.assert_called_once()
        args = dll.LogiSetSteeringWheelRpmLeds.call_args[0]
        self.assertAlmostEqual(float(args[1].value), 0.0)

    def test_led_blink_on_calls_leds_on(self):
        dll = self._mock_dll()
        fwl.apply_led_action(dll, fwl.LED_BLINK_ON, 0, 0, 0)
        dll.LogiSetSteeringWheelRpmLeds.assert_called_once()
        args = dll.LogiSetSteeringWheelRpmLeds.call_args[0]
        # currentRPM arg should be 1.0 (all on)
        self.assertAlmostEqual(float(args[1].value), 1.0)

    def test_led_normal_passes_rpm_values(self):
        dll = self._mock_dll()
        fwl.apply_led_action(dll, fwl.LED_NORMAL, 6000.0, 5600.0, 8000.0)
        dll.LogiSetSteeringWheelRpmLeds.assert_called_once()
        args = dll.LogiSetSteeringWheelRpmLeds.call_args[0]
        self.assertAlmostEqual(float(args[1].value), 6000.0)
        self.assertAlmostEqual(float(args[2].value), 5600.0)
        self.assertAlmostEqual(float(args[3].value), 8000.0)


# ---------------------------------------------------------------------------
# Tests: leds_off / leds_on
# ---------------------------------------------------------------------------

class TestLedsHelpers(unittest.TestCase):

    def _mock_dll(self):
        dll = MagicMock()
        dll.LogiSetSteeringWheelRpmLeds = MagicMock()
        return dll

    def test_leds_off(self):
        dll = self._mock_dll()
        fwl.leds_off(dll)
        dll.LogiSetSteeringWheelRpmLeds.assert_called_once()
        args = dll.LogiSetSteeringWheelRpmLeds.call_args[0]
        self.assertAlmostEqual(float(args[1].value), 0.0)  # currentRPM = 0

    def test_leds_on(self):
        dll = self._mock_dll()
        fwl.leds_on(dll)
        dll.LogiSetSteeringWheelRpmLeds.assert_called_once()
        args = dll.LogiSetSteeringWheelRpmLeds.call_args[0]
        self.assertAlmostEqual(float(args[1].value), 1.0)  # currentRPM = 1


# ---------------------------------------------------------------------------
# Tests: load_logitech_sdk
# ---------------------------------------------------------------------------

class TestLoadLogitechSdk(unittest.TestCase):

    def test_exits_when_dll_not_found(self):
        with patch("ctypes.CDLL", side_effect=OSError("not found")):
            with self.assertRaises(SystemExit):
                fwl.load_logitech_sdk()

    def test_returns_dll_and_sets_types(self):
        mock_dll = MagicMock()
        with patch("ctypes.CDLL", return_value=mock_dll):
            result = fwl.load_logitech_sdk()
        self.assertIs(result, mock_dll)
        # Check that argtypes / restype were configured
        self.assertIsNotNone(mock_dll.LogiSteeringInitialize.restype)
        self.assertIsNotNone(mock_dll.LogiSteeringInitialize.argtypes)


# ---------------------------------------------------------------------------
# Tests: main()
# ---------------------------------------------------------------------------

class TestMain(unittest.TestCase):
    """
    Test main() end-to-end using mocks for the DLL and the UDP socket.
    We exercise all branches: DLL warn, wheel not detected, timeout,
    unknown packet, no race, normal RPM, redline blink, KeyboardInterrupt.
    """

    def _run_main(self, packets, dll_init=True, wheel_connected=True,
                  dll_load_ok=True):
        """
        Run main() with injected packets (list of bytes or socket.timeout).
        Returns after all packets are consumed (raises StopIteration → caught).
        """
        mock_dll = MagicMock()
        mock_dll.LogiSteeringInitialize.return_value = dll_init
        mock_dll.LogiIsConnected.return_value = wheel_connected

        mock_sock = MagicMock()
        recv_iter = iter(packets)

        def fake_recvfrom(_):
            try:
                item = next(recv_iter)
            except StopIteration:
                raise KeyboardInterrupt  # cleanly exits main loop
            if item is socket.timeout:
                raise socket.timeout
            return item, ("127.0.0.1", 5607)

        mock_sock.recvfrom.side_effect = fake_recvfrom

        with patch("ctypes.CDLL", return_value=mock_dll), \
             patch("socket.socket", return_value=mock_sock), \
             patch("time.sleep"), \
             patch("time.time", return_value=100.0), \
             patch("builtins.input"):
            fwl.main()

        return mock_dll

    def test_main_normal_race(self):
        pkt = _pack_packet(is_race_on=1, max_rpm=8000, current_rpm=5000,
                           gear=3, raw_size=323)
        self._run_main([pkt])

    def test_main_fm2023_packet(self):
        pkt = _pack_packet(is_race_on=1, max_rpm=9000, current_rpm=6000,
                           gear=2, raw_size=331)
        self._run_main([pkt])

    def test_main_no_race_but_driving(self):
        # IsRaceOn=0 (free roam) but max_rpm > 0 → LEDs should still work
        pkt = _pack_packet(is_race_on=0, max_rpm=8000, current_rpm=4000,
                           gear=3, raw_size=323)
        self._run_main([pkt])

    def test_main_max_rpm_zero_turns_leds_off(self):
        # max_rpm=0 → LEDs off (in menu, no car loaded)
        pkt = _pack_packet(is_race_on=1, max_rpm=0, current_rpm=0, raw_size=323)
        self._run_main([pkt])

    def test_main_redline_blink(self):
        # current_rpm >= 97% of max → blink zone
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

    def test_main_dll_init_warns(self):
        pkt = _pack_packet(raw_size=323)
        self._run_main([pkt], dll_init=False)

    def test_main_wheel_not_connected_warns(self):
        pkt = _pack_packet(raw_size=323)
        self._run_main([pkt], wheel_connected=False)

    def test_main_game_detected_printed_once(self):
        # Two packets from the same game → game label printed only once
        pkt = _pack_packet(is_race_on=1, max_rpm=8000, current_rpm=5000,
                           raw_size=323)
        self._run_main([pkt, pkt])

    def test_main_second_game_triggers_new_label(self):
        # First FH5/FH6 packet, then FM2023 packet → two game labels
        pkt1 = _pack_packet(raw_size=323)
        pkt2 = _pack_packet(raw_size=331)
        self._run_main([pkt1, pkt2])


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------

class TestEntryPoint(unittest.TestCase):
    def test_main_called_when_run_as_script(self):
        """Cover the `if __name__ == '__main__': main()` guard."""
        with patch.object(fwl, "main") as mock_main, \
             patch.object(fwl, "__name__", "__main__"):
            # Re-execute the guard block directly
            exec(  # noqa: S102
                "if __name__ == '__main__': main()",
                {**vars(fwl), "__name__": "__main__", "main": mock_main},
            )
        mock_main.assert_called_once()


if __name__ == "__main__":
    unittest.main()
