"""
Unit tests for forza_wheel_leds.py — targeting 100 % line coverage.

All tests run without a real HID device, UDP socket, or running game.
hidapi.dll is mocked via ctypes.CDLL.
"""

import configparser
import ctypes
import os
import socket
import struct
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch

# ---------------------------------------------------------------------------
# Helpers to build valid Forza UDP packets
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
        self.assertIsNone(fwl.patch_and_parse(b"\x00" * 325))

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

    def test_fh5_324_variant_parsed(self):
        # FH5 variant with 324 bytes (1 extra byte at end) — same structure
        pkt = _pack_packet(is_race_on=1, max_rpm=7500, current_rpm=4000,
                           gear=3, raw_size=323) + b"\x00"
        result = fwl.patch_and_parse(pkt)
        self.assertIsNotNone(result)
        self.assertEqual(result["game"], "FH5 / FH6")
        self.assertAlmostEqual(result["max_rpm"], 7500.0, places=0)

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
        result = fwl.rpm_to_bitmask(6800, 5600, 8000)
        self.assertGreater(result, 0x00)
        self.assertLess(result, fwl.ALL_LEDS_ON)

    def test_max_equals_min_returns_zero(self):
        self.assertEqual(fwl.rpm_to_bitmask(5000, 5000, 5000), 0x00)

    def test_just_above_min_gives_one_led(self):
        result = fwl.rpm_to_bitmask(5601, 5600, 8000)
        self.assertEqual(result, 0x01)


# ---------------------------------------------------------------------------
# Tests: _hidapi_dll_path
# ---------------------------------------------------------------------------

class TestHidapiDllPath(unittest.TestCase):

    def test_frozen_returns_meipass_path(self):
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "_MEIPASS", "/fake/meipass", create=True):
            path = fwl._hidapi_dll_path()
        self.assertEqual(path, "/fake/meipass/hidapi.dll")

    def test_script_beside_file(self):
        with patch.object(sys, "frozen", False, create=True), \
             patch("os.path.exists", return_value=True):
            path = fwl._hidapi_dll_path()
        self.assertTrue(path.endswith("hidapi.dll"))
        self.assertNotEqual(path, "hidapi.dll")

    def test_script_fallback_to_path(self):
        with patch.object(sys, "frozen", False, create=True), \
             patch("os.path.exists", return_value=False):
            path = fwl._hidapi_dll_path()
        self.assertEqual(path, "hidapi.dll")


# ---------------------------------------------------------------------------
# Tests: _config_path
# ---------------------------------------------------------------------------

class TestConfigPath(unittest.TestCase):

    def test_frozen_returns_beside_executable(self):
        with patch.object(sys, "frozen", True, create=True), \
             patch.object(sys, "executable", "/fake/app.exe", create=True):
            path = fwl._config_path()
        self.assertEqual(path, os.path.join("/fake", "config.ini"))

    def test_script_returns_beside_script(self):
        with patch.object(sys, "frozen", False, create=True):
            path = fwl._config_path()
        self.assertTrue(path.endswith("config.ini"))


# ---------------------------------------------------------------------------
# Tests: load_config
# ---------------------------------------------------------------------------

class TestLoadConfig(unittest.TestCase):

    def _write_ini(self, content: str) -> str:
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".ini",
                                        delete=False)
        f.write(content)
        f.close()
        return f.name

    def test_defaults_when_file_absent(self):
        cfg = fwl.load_config("/nonexistent/config.ini")
        self.assertEqual(cfg["udp_port"],          fwl.UDP_PORT)
        self.assertAlmostEqual(cfg["led_min_rpm_ratio"], fwl.LED_MIN_RPM_RATIO)
        self.assertAlmostEqual(cfg["blink_rpm_ratio"],   fwl.BLINK_RPM_RATIO)
        self.assertAlmostEqual(cfg["blink_hz"],          fwl.BLINK_HZ)

    def test_reads_all_keys(self):
        ini = self._write_ini(
            "[settings]\n"
            "udp_port=1234\n"
            "led_min_rpm_ratio=0.65\n"
            "blink_rpm_ratio=0.95\n"
            "blink_hz=8\n"
        )
        try:
            cfg = fwl.load_config(ini)
        finally:
            os.unlink(ini)
        self.assertEqual(cfg["udp_port"], 1234)
        self.assertAlmostEqual(cfg["led_min_rpm_ratio"], 0.65)
        self.assertAlmostEqual(cfg["blink_rpm_ratio"],   0.95)
        self.assertAlmostEqual(cfg["blink_hz"],          8.0)

    def test_invalid_values_fall_back_to_defaults(self):
        ini = self._write_ini(
            "[settings]\n"
            "udp_port=notanumber\n"
            "blink_hz=bad\n"
        )
        try:
            cfg = fwl.load_config(ini)
        finally:
            os.unlink(ini)
        self.assertEqual(cfg["udp_port"], fwl.UDP_PORT)
        self.assertAlmostEqual(cfg["blink_hz"], fwl.BLINK_HZ)

    def test_missing_section_uses_defaults(self):
        ini = self._write_ini("[other]\nfoo=bar\n")
        try:
            cfg = fwl.load_config(ini)
        finally:
            os.unlink(ini)
        self.assertEqual(cfg["udp_port"], fwl.UDP_PORT)

    def test_forward_targets_parsed(self):
        ini = self._write_ini(
            "[settings]\n"
            "[forward]\n"
            "targets = 192.168.1.42:5607, 127.0.0.1:5608\n"
        )
        try:
            cfg = fwl.load_config(ini)
        finally:
            os.unlink(ini)
        self.assertEqual(cfg["forward_targets"],
                         [("192.168.1.42", 5607), ("127.0.0.1", 5608)])

    def test_forward_targets_empty(self):
        cfg = fwl.load_config("/nonexistent/config.ini")
        self.assertEqual(cfg["forward_targets"], [])

    def test_forward_targets_malformed_ignored(self):
        ini = self._write_ini(
            "[forward]\n"
            "targets = notavalidentry, 192.168.1.1:5607\n"
        )
        try:
            cfg = fwl.load_config(ini)
        finally:
            os.unlink(ini)
        self.assertEqual(cfg["forward_targets"], [("192.168.1.1", 5607)])

    def test_forward_targets_blank(self):
        ini = self._write_ini("[forward]\ntargets =\n")
        try:
            cfg = fwl.load_config(ini)
        finally:
            os.unlink(ini)
        self.assertEqual(cfg["forward_targets"], [])

    def test_forward_targets_trailing_comma(self):
        """Trailing comma produces an empty entry that should be skipped."""
        ini = self._write_ini("[forward]\ntargets = 192.168.1.1:5607,\n")
        try:
            cfg = fwl.load_config(ini)
        finally:
            os.unlink(ini)
        self.assertEqual(cfg["forward_targets"], [("192.168.1.1", 5607)])


# ---------------------------------------------------------------------------
# Tests: forward_packet
# ---------------------------------------------------------------------------

class TestForwardPacket(unittest.TestCase):

    def test_sends_to_all_targets(self):
        mock_sock = MagicMock()
        data = b"\x01\x02\x03"
        targets = [("192.168.1.42", 5607), ("127.0.0.1", 5608)]
        fwl.forward_packet(mock_sock, data, targets)
        mock_sock.sendto.assert_any_call(data, ("192.168.1.42", 5607))
        mock_sock.sendto.assert_any_call(data, ("127.0.0.1", 5608))
        self.assertEqual(mock_sock.sendto.call_count, 2)

    def test_empty_targets_sends_nothing(self):
        mock_sock = MagicMock()
        fwl.forward_packet(mock_sock, b"\x00", [])
        mock_sock.sendto.assert_not_called()

    def test_oserror_is_swallowed(self):
        mock_sock = MagicMock()
        mock_sock.sendto.side_effect = OSError("network unreachable")
        # must not raise
        fwl.forward_packet(mock_sock, b"\x00", [("10.0.0.1", 9999)])


# ---------------------------------------------------------------------------
# Tests: load_hidapi
# ---------------------------------------------------------------------------

class TestLoadHidapi(unittest.TestCase):

    def test_raises_oserror_when_dll_not_found(self):
        with patch("ctypes.CDLL", side_effect=OSError("not found")):
            with self.assertRaises(OSError):
                fwl.load_hidapi()

    def test_returns_lib_and_calls_hid_init(self):
        mock_lib = MagicMock()
        with patch("ctypes.CDLL", return_value=mock_lib):
            result = fwl.load_hidapi()
        self.assertIs(result, mock_lib)
        mock_lib.hid_init.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: open_wheel
# ---------------------------------------------------------------------------

class TestOpenWheel(unittest.TestCase):

    def test_returns_handle_when_first_pid_matches(self):
        lib = MagicMock()
        lib.hid_open.return_value = 0xDEAD
        result = fwl.open_wheel(lib)
        self.assertEqual(result, 0xDEAD)
        lib.hid_open.assert_called_once_with(fwl.LOGITECH_VID, fwl.WHEEL_PIDS[0], None)

    def test_tries_second_pid_when_first_returns_null(self):
        lib = MagicMock()
        lib.hid_open.side_effect = [None, 0xBEEF]
        result = fwl.open_wheel(lib)
        self.assertEqual(result, 0xBEEF)

    def test_returns_none_when_all_pids_fail(self):
        lib = MagicMock()
        lib.hid_open.return_value = None
        result = fwl.open_wheel(lib)
        self.assertIsNone(result)


def _device_list(*usage_pages):
    """Build a real hid_device_info linked list; returns (nodes, head address)."""
    nodes = [fwl.HidDeviceInfo() for _ in usage_pages]
    for i, (node, page) in enumerate(zip(nodes, usage_pages)):
        node.path = f"path-{i}".encode()
        node.usage_page = page
        if i + 1 < len(nodes):
            node.next = ctypes.pointer(nodes[i + 1])
    return nodes, ctypes.addressof(nodes[0])


class TestOpenWheelInterfaceSelection(unittest.TestCase):
    """G29 with G HUB exposes vendor interfaces before the game controller (#4)."""

    def test_opens_game_controller_interface_by_path(self):
        nodes, head = _device_list(0xFF00, 0xFF00, 0x01)
        lib = MagicMock()
        lib.hid_enumerate.return_value = head
        lib.hid_open_path.return_value = 0xCAFE
        result = fwl.open_wheel(lib)
        self.assertEqual(result, 0xCAFE)
        lib.hid_open_path.assert_called_once_with(b"path-2")
        lib.hid_open.assert_not_called()
        lib.hid_free_enumeration.assert_called_once_with(head)

    def test_falls_back_to_hid_open_without_controller_interface(self):
        nodes, head = _device_list(0xFF00)
        lib = MagicMock()
        lib.hid_enumerate.return_value = head
        lib.hid_open.return_value = 0xDEAD
        result = fwl.open_wheel(lib)
        self.assertEqual(result, 0xDEAD)
        lib.hid_open_path.assert_not_called()
        lib.hid_free_enumeration.assert_called_once_with(head)

    def test_falls_back_to_hid_open_when_open_path_fails(self):
        nodes, head = _device_list(0x01)
        lib = MagicMock()
        lib.hid_enumerate.return_value = head
        lib.hid_open_path.return_value = None
        lib.hid_open.return_value = 0xBEEF
        result = fwl.open_wheel(lib)
        self.assertEqual(result, 0xBEEF)

    def test_falls_back_to_hid_open_when_enumeration_empty(self):
        lib = MagicMock()
        lib.hid_enumerate.return_value = None
        lib.hid_open.return_value = 0xDEAD
        self.assertEqual(fwl.open_wheel(lib), 0xDEAD)
        lib.hid_free_enumeration.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: _send_led_report
# ---------------------------------------------------------------------------

class TestSendLedReport(unittest.TestCase):

    def test_writes_correct_report(self):
        lib = MagicMock()
        fwl._send_led_report(lib, 0xDEAD, 0x1F)
        lib.hid_write.assert_called_once_with(
            0xDEAD,
            bytes([0x00, 0xF8, 0x12, 0x1F, 0x00, 0x00, 0x00, 0x00]),
            8,
        )

    def test_bitmask_masked_to_byte(self):
        lib = MagicMock()
        fwl._send_led_report(lib, 1, 0x1FF)
        report = lib.hid_write.call_args[0][1]
        self.assertEqual(report[3], 0xFF)


# ---------------------------------------------------------------------------
# Tests: compute_led_state
# ---------------------------------------------------------------------------

class TestComputeLedState(unittest.TestCase):

    def _call(self, current_rpm, max_rpm, blink_phase, last_blink, now,
              blink_thresh=None, blink_interval=0.1):
        if blink_thresh is None:
            blink_thresh = max_rpm * fwl.BLINK_RPM_RATIO
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
        action, phase, lb = self._call(7800, 8000, False, 0.0, 1.0,
                                       blink_thresh=7760, blink_interval=0.1)
        self.assertEqual(action, fwl.LED_BLINK_ON)
        self.assertTrue(phase)

    def test_blink_zone_no_toggle_before_interval(self):
        action, phase, lb = self._call(7800, 8000, False, 0.99, 1.0,
                                       blink_thresh=7760, blink_interval=0.1)
        self.assertEqual(action, fwl.LED_BLINK_OFF)
        self.assertFalse(phase)

    def test_blink_zone_phase_true_gives_blink_on(self):
        action, _, _ = self._call(7800, 8000, True, 0.99, 1.0,
                                  blink_thresh=7760, blink_interval=0.1)
        self.assertEqual(action, fwl.LED_BLINK_ON)

    def test_blink_zone_toggle_off(self):
        action, phase, lb = self._call(7800, 8000, True, 0.0, 1.0,
                                       blink_thresh=7760, blink_interval=0.1)
        self.assertEqual(action, fwl.LED_BLINK_OFF)
        self.assertFalse(phase)

    def test_exactly_at_blink_thresh(self):
        action, _, _ = self._call(7760, 8000, False, 0.0, 1.0,
                                  blink_thresh=7760, blink_interval=0.1)
        self.assertIn(action, (fwl.LED_BLINK_ON, fwl.LED_BLINK_OFF))


# ---------------------------------------------------------------------------
# Tests: apply_led_action
# ---------------------------------------------------------------------------

class TestApplyLedAction(unittest.TestCase):

    def test_led_off_sends_all_off(self):
        lib = MagicMock()
        fwl.apply_led_action(lib, 1, fwl.LED_OFF, 0, 0, 0)
        report = lib.hid_write.call_args[0][1]
        self.assertEqual(report[3], fwl.ALL_LEDS_OFF)

    def test_led_blink_off_sends_all_off(self):
        lib = MagicMock()
        fwl.apply_led_action(lib, 1, fwl.LED_BLINK_OFF, 0, 0, 0)
        report = lib.hid_write.call_args[0][1]
        self.assertEqual(report[3], fwl.ALL_LEDS_OFF)

    def test_led_blink_on_sends_all_on(self):
        lib = MagicMock()
        fwl.apply_led_action(lib, 1, fwl.LED_BLINK_ON, 0, 0, 0)
        report = lib.hid_write.call_args[0][1]
        self.assertEqual(report[3], fwl.ALL_LEDS_ON)

    def test_led_normal_sends_computed_bitmask(self):
        lib = MagicMock()
        fwl.apply_led_action(lib, 1, fwl.LED_NORMAL, 8000.0, 5600.0, 8000.0)
        report = lib.hid_write.call_args[0][1]
        self.assertEqual(report[3], fwl.ALL_LEDS_ON)


# ---------------------------------------------------------------------------
# Tests: main()
# ---------------------------------------------------------------------------

class TestMain(unittest.TestCase):

    def _make_lib(self, wheel_found=True):
        lib = MagicMock()
        lib.hid_open.return_value = 0xDEAD if wheel_found else None
        return lib

    def _run_main(self, packets, wheel_found=True, dll_load_ok=True):
        lib = self._make_lib(wheel_found)
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

        if dll_load_ok:
            load_patch = patch.object(fwl, "load_hidapi", return_value=lib)
        else:
            load_patch = patch.object(fwl, "load_hidapi",
                                      side_effect=OSError("dll missing"))

        with load_patch, \
             patch("socket.socket", return_value=mock_sock), \
             patch("time.time", return_value=100.0), \
             patch.object(fwl, "_config_path", return_value="/fake/config.ini"), \
             patch("os.path.exists", return_value=False), \
             patch("builtins.input"):
            fwl.main()

        return lib

    def test_main_config_file_present_label(self):
        """Cover the cfg_source = 'config.ini' branch (os.path.exists returns True)."""
        pkt = _pack_packet(is_race_on=1, max_rpm=8000, current_rpm=5000,
                           raw_size=323)
        lib = self._make_lib(wheel_found=True)
        mock_sock = MagicMock()
        recv_iter = iter([pkt])

        def fake_recvfrom(_):
            try:
                return next(recv_iter), ("127.0.0.1", 5607)
            except StopIteration:
                raise KeyboardInterrupt

        mock_sock.recvfrom.side_effect = fake_recvfrom

        with patch.object(fwl, "load_hidapi", return_value=lib), \
             patch("socket.socket", return_value=mock_sock), \
             patch("time.time", return_value=100.0), \
             patch.object(fwl, "_config_path", return_value="/fake/config.ini"), \
             patch("os.path.exists", return_value=True), \
             patch("builtins.input"):
            fwl.main()

    def test_main_dll_not_found_exits(self):
        with self.assertRaises(SystemExit):
            self._run_main([], dll_load_ok=False)

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

    def test_main_forward_targets_displayed_and_called(self):
        """Forwarding targets in config are shown in banner and packets are forwarded."""
        pkt = _pack_packet(is_race_on=1, max_rpm=8000, current_rpm=5000,
                           raw_size=323)
        lib = self._make_lib(wheel_found=True)
        mock_sock = MagicMock()
        recv_iter = iter([pkt])

        def fake_recvfrom(_):
            try:
                return next(recv_iter), ("127.0.0.1", 5607)
            except StopIteration:
                raise KeyboardInterrupt

        mock_sock.recvfrom.side_effect = fake_recvfrom

        fake_cfg = {
            "udp_port": 5607,
            "led_min_rpm_ratio": fwl.LED_MIN_RPM_RATIO,
            "blink_rpm_ratio": fwl.BLINK_RPM_RATIO,
            "blink_hz": fwl.BLINK_HZ,
            "forward_targets": [("192.168.1.42", 5607)],
        }

        with patch.object(fwl, "load_hidapi", return_value=lib), \
             patch("socket.socket", return_value=mock_sock), \
             patch("time.time", return_value=100.0), \
             patch.object(fwl, "_config_path", return_value="/fake/config.ini"), \
             patch.object(fwl, "load_config", return_value=fake_cfg), \
             patch("os.path.exists", return_value=True), \
             patch("builtins.input"):
            fwl.main()

        mock_sock.sendto.assert_called_with(pkt, ("192.168.1.42", 5607))

    def test_main_wheel_reconnects_during_loop(self):
        """handle starts None, wheel found on first packet retry."""
        pkt = _pack_packet(is_race_on=1, max_rpm=8000, current_rpm=5000,
                           raw_size=323)
        lib = MagicMock()
        # Startup: all PIDs return None → open_wheel returns None
        # Loop retry: first PID returns a valid handle
        lib.hid_open.side_effect = [None] * len(fwl.WHEEL_PIDS) + [0xDEAD]

        mock_sock = MagicMock()
        recv_iter = iter([pkt])

        def fake_recvfrom(_):
            try:
                return next(recv_iter), ("127.0.0.1", 5607)
            except StopIteration:
                raise KeyboardInterrupt

        mock_sock.recvfrom.side_effect = fake_recvfrom

        with patch.object(fwl, "load_hidapi", return_value=lib), \
             patch("socket.socket", return_value=mock_sock), \
             patch("time.time", return_value=100.0), \
             patch.object(fwl, "_config_path", return_value="/fake/config.ini"), \
             patch("os.path.exists", return_value=False), \
             patch("builtins.input"):
            fwl.main()

        lib.hid_write.assert_called()

    def test_main_finally_handles_errors(self):
        """Cover except-pass branches in finally when lib/sock calls raise."""
        lib = MagicMock()
        lib.hid_open.return_value = 0xDEAD
        lib.hid_write.side_effect = Exception("write error")
        lib.hid_close.side_effect = Exception("close error")
        lib.hid_exit.side_effect  = Exception("exit error")

        mock_sock = MagicMock()
        mock_sock.recvfrom.side_effect = KeyboardInterrupt
        mock_sock.close.side_effect = Exception("close error")

        with patch.object(fwl, "load_hidapi", return_value=lib), \
             patch("socket.socket", return_value=mock_sock), \
             patch("time.time", return_value=100.0), \
             patch.object(fwl, "_config_path", return_value="/fake/config.ini"), \
             patch("os.path.exists", return_value=False), \
             patch("builtins.input"):
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
