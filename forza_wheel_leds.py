"""
forza_wheel_leds.py
--------------------
Bridges Forza telemetry (UDP Data Out) to the Logitech G29/G920 RPM LEDs.

Supported games : Forza Horizon 5, Forza Horizon 6, Forza Motorsport (2023)
Supported wheels: Logitech G29, G920 (direct USB HID — no G HUB required)

Requirements:
  - Python 3.8+  (not needed if using the .exe release)
  - hidapi.dll bundled in the .exe release (Windows inbox-independent)

In-game setup (all supported Forza titles):
  Settings > HUD and Gameplay  (or Gameplay & HUD)
    Data Out             : ON
    Data Out IP Address  : 127.0.0.1
    Data Out IP Port     : 5607
"""

import ctypes
import ctypes.util
import os
import socket
import struct
import sys
import time

# ---------------------------------------------------------------------------
# USER CONFIGURATION
# ---------------------------------------------------------------------------

UDP_PORT = 5607       # Must match the port set in-game
UDP_IP   = "0.0.0.0" # Listen on all interfaces (127.0.0.1 also works)

# Fraction of max RPM at which the FIRST LED lights up.
#   0.50 → first LED at 50 % of redline  (recommended — all 5 LEDs visible)
#   0.70 → first LED at 70 % of redline  (shift-indicator feel)
LED_MIN_RPM_RATIO = 0.50

# Rev-limiter blink: LEDs flash when RPM exceeds this fraction of max RPM.
BLINK_RPM_RATIO = 0.97

# Blink frequency in Hz (on/off cycles per second).
BLINK_HZ = 10.0

# ---------------------------------------------------------------------------
# LOGITECH G29 / G920  —  DIRECT USB HID via hidapi.dll (ctypes)
# ---------------------------------------------------------------------------

LOGITECH_VID = 0x046D
WHEEL_PIDS = [
    0xC24F,  # G29 (PC / PS3 mode)
    0xC262,  # G920
]

NUM_LEDS     = 5
ALL_LEDS_ON  = (1 << NUM_LEDS) - 1   # 0x1F
ALL_LEDS_OFF = 0x00


def _hidapi_dll_path() -> str:
    """
    Resolve path to hidapi.dll.
    - PyInstaller .exe: DLL is extracted to sys._MEIPASS
    - Script mode: look next to the script, then rely on PATH
    """
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "hidapi.dll")  # type: ignore[attr-defined]
    # Script mode: look next to the script first
    beside = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hidapi.dll")
    if os.path.exists(beside):
        return beside
    return "hidapi.dll"  # fall back to PATH


def load_hidapi() -> ctypes.CDLL:
    """Load hidapi.dll and declare the function signatures we need."""
    path = _hidapi_dll_path()
    try:
        lib = ctypes.CDLL(path)
    except OSError as exc:
        raise OSError(f"Cannot load hidapi.dll: {exc}") from exc

    # hid_init() → int
    lib.hid_init.restype  = ctypes.c_int
    lib.hid_init.argtypes = []

    # hid_exit() → int
    lib.hid_exit.restype  = ctypes.c_int
    lib.hid_exit.argtypes = []

    # hid_open(vendor_id, product_id, serial_number=NULL) → hid_device*
    lib.hid_open.restype  = ctypes.c_void_p
    lib.hid_open.argtypes = [ctypes.c_ushort, ctypes.c_ushort, ctypes.c_wchar_p]

    # hid_close(device) → void
    lib.hid_close.restype  = None
    lib.hid_close.argtypes = [ctypes.c_void_p]

    # hid_write(device, data, length) → int
    lib.hid_write.restype  = ctypes.c_int
    lib.hid_write.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t]

    lib.hid_init()
    return lib


def open_wheel(lib: ctypes.CDLL):
    """
    Try to open the first recognised Logitech wheel.
    Returns a non-NULL c_void_p handle, or None if no wheel found.
    """
    for pid in WHEEL_PIDS:
        handle = lib.hid_open(LOGITECH_VID, pid, None)
        if handle:
            return handle
    return None


def _send_led_report(lib: ctypes.CDLL, handle, bitmask: int) -> None:
    """Write the 8-byte LED control output report (report-ID 0x00 + 7 bytes)."""
    report = bytes([0x00, 0xF8, 0x12, bitmask & 0xFF, 0x00, 0x00, 0x00, 0x00])
    lib.hid_write(handle, report, len(report))


# ---------------------------------------------------------------------------
# RPM → LED BITMASK
# ---------------------------------------------------------------------------

def rpm_to_bitmask(current_rpm: float, min_rpm: float, max_rpm: float) -> int:
    """
    Convert RPM to a 5-bit LED bitmask.
    LEDs light progressively from left (bit 0) to right (bit 4).
    """
    if max_rpm <= min_rpm:
        return ALL_LEDS_OFF
    if current_rpm <= min_rpm:
        return ALL_LEDS_OFF
    if current_rpm >= max_rpm:
        return ALL_LEDS_ON
    ratio = (current_rpm - min_rpm) / (max_rpm - min_rpm)
    n_lit = max(1, round(ratio * NUM_LEDS))
    return (1 << n_lit) - 1


# ---------------------------------------------------------------------------
# FORZA PACKET PARSING
# ---------------------------------------------------------------------------

DASH_FORMAT = (
    "<iI"        # [0]  IsRaceOn (s32), TimestampMS (u32)
    "fff"        # [2]  EngineMaxRpm, EngineIdleRpm, CurrentEngineRpm
    "fff"        # [5]  AccelerationX/Y/Z
    "fff"        # [8]  VelocityX/Y/Z
    "fff"        # [11] AngularVelocityX/Y/Z
    "fff"        # [14] Yaw, Pitch, Roll
    "ffff"       # [17] NormalizedSuspensionTravel FL/FR/RL/RR
    "ffff"       # [21] TireSlipRatio FL/FR/RL/RR
    "ffff"       # [25] WheelRotationSpeed FL/FR/RL/RR
    "iiii"       # [29] WheelOnRumbleStrip FL/FR/RL/RR
    "ffff"       # [33] WheelInPuddleDepth FL/FR/RL/RR
    "ffff"       # [37] SurfaceRumble FL/FR/RL/RR
    "ffff"       # [41] TireSlipAngle FL/FR/RL/RR
    "ffff"       # [45] TireCombinedSlip FL/FR/RL/RR
    "ffff"       # [49] SuspensionTravelMeters FL/FR/RL/RR
    "iiii"       # [53] CarOrdinal, CarClass, CarPerformanceIndex, DrivetrainType
    "i"          # [57] NumCylinders
    "fff"        # [58] PositionX/Y/Z
    "fff"        # [61] Speed, Power, Torque
    "ffff"       # [64] TireTemp FL/FR/RL/RR
    "fff"        # [68] Boost, Fuel, DistanceTraveled
    "fff"        # [71] BestLap, LastLap, CurrentLap
    "f"          # [74] CurrentRaceTime
    "H"          # [75] LapNumber (u16)
    "B"          # [76] RacePosition (u8)
    "BBBBB"      # [77] Accel, Brake, Clutch, HandBrake, Gear (u8)
    "bbb"        # [82] Steer, NormalizedDrivingLine, NormalizedAIBrakeDifference (s8)
)

IDX_IS_RACE_ON      = 0
IDX_ENGINE_MAX_RPM  = 2
IDX_ENGINE_IDLE_RPM = 3
IDX_CURRENT_RPM     = 4
IDX_GEAR            = 81

SIZE_FH5_FH6  = 323
SIZE_FH5_FH6B = 324   # FH5 variant (+1 byte at end, same structure)
SIZE_FM2023   = 331

GAME_LABELS = {
    SIZE_FH5_FH6:  "FH5 / FH6",
    SIZE_FH5_FH6B: "FH5 / FH6",
    SIZE_FM2023:   "FM2023",
}


def patch_and_parse(data: bytes):
    """
    Remove the 12-byte FH4/FH5/FH6 gap (bytes 232–243), unpack the struct.
    Returns None if the packet size is not recognised.
    """
    size = len(data)
    if size not in (SIZE_FH5_FH6, SIZE_FH5_FH6B, SIZE_FM2023):
        return None

    patched = data[:232] + data[244:323]

    try:
        vals = struct.unpack_from(DASH_FORMAT, patched)
    except struct.error:
        return None

    return {
        "game":        GAME_LABELS[size],
        "is_race_on":  bool(vals[IDX_IS_RACE_ON]),
        "current_rpm": float(vals[IDX_CURRENT_RPM]),
        "max_rpm":     float(vals[IDX_ENGINE_MAX_RPM]),
        "idle_rpm":    float(vals[IDX_ENGINE_IDLE_RPM]),
        "gear":        int(vals[IDX_GEAR]),
    }


# ---------------------------------------------------------------------------
# LED STATE LOGIC  (pure — no side effects, fully testable)
# ---------------------------------------------------------------------------

LED_OFF       = "off"
LED_NORMAL    = "normal"
LED_BLINK_ON  = "blink_on"
LED_BLINK_OFF = "blink_off"


def compute_led_state(
    current_rpm: float,
    max_rpm: float,
    blink_phase: bool,
    last_blink: float,
    now: float,
    blink_thresh: float,
    blink_interval: float,
) -> tuple:
    if current_rpm >= blink_thresh:
        if now - last_blink >= blink_interval:
            blink_phase = not blink_phase
            last_blink  = now
        action = LED_BLINK_ON if blink_phase else LED_BLINK_OFF
    else:
        blink_phase = False
        action      = LED_NORMAL

    return action, blink_phase, last_blink


# ---------------------------------------------------------------------------
# HID LED APPLICATION
# ---------------------------------------------------------------------------

def apply_led_action(lib: ctypes.CDLL, handle, action: str,
                     current_rpm: float, min_rpm: float, max_rpm: float) -> None:
    if action == LED_OFF or action == LED_BLINK_OFF:
        _send_led_report(lib, handle, ALL_LEDS_OFF)
    elif action == LED_BLINK_ON:
        _send_led_report(lib, handle, ALL_LEDS_ON)
    else:  # LED_NORMAL
        _send_led_report(lib, handle, rpm_to_bitmask(current_rpm, min_rpm, max_rpm))


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 58)
    print("  forza-wheel-leds  |  Logitech G29 / G920 RPM LEDs")
    print("=" * 58)
    print(f"  Version        : 1.2.3")
    print(f"  Listening on   : {UDP_IP}:{UDP_PORT}")
    print(f"  LED min RPM    : {int(LED_MIN_RPM_RATIO * 100)} % of redline")
    print(f"  Blink at       : {int(BLINK_RPM_RATIO * 100)} % of redline  ({BLINK_HZ:.0f} Hz)")
    print("=" * 58)
    print()

    # --- Load hidapi.dll ---
    try:
        lib = load_hidapi()
    except OSError as exc:
        print(f"[ERROR] {exc}")
        print()
        print("        The .exe release bundles hidapi.dll automatically.")
        print("        If running the .py script, place hidapi.dll next to it.")
        print("        Download from: https://github.com/libusb/hidapi/releases")
        print()
        input("  Press Enter to close this window …")
        sys.exit(1)

    # --- Open wheel ---
    handle = open_wheel(lib)
    if handle is None:
        print("[WARN] No supported Logitech wheel detected (G29 / G920).")
        print("       Make sure the wheel is plugged in via USB.")
        print("       LEDs will activate once the wheel is connected.")
        print()
    else:
        print("[OK]   Logitech wheel connected via USB HID.")
        print()

    # --- UDP socket ---
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_IP, UDP_PORT))
    sock.settimeout(1.0)

    print(f"[OK]   UDP socket bound to {UDP_IP}:{UDP_PORT}")
    print()
    print("  In-game setup (do once per Forza title):")
    print("    Settings > HUD and Gameplay > Data Out : ON")
    print(f"    Data Out IP Address : 127.0.0.1")
    print(f"    Data Out IP Port    : {UDP_PORT}")
    print()
    print("  Waiting for Forza telemetry …")
    print("  Close this window (or press Ctrl+C) to stop.")
    print()

    last_game      = ""
    blink_phase    = False
    last_blink     = 0.0
    blink_interval = 1.0 / BLINK_HZ

    try:
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue

            packet = patch_and_parse(data)
            if packet is None:
                # Log first packet in detail to help diagnose unknown format
                hex_preview = data[:32].hex(" ")
                print(f"\n[UDP] Unknown packet — size {len(data)} bytes")
                print(f"      From  : {addr[0]}:{addr[1]}")
                print(f"      Bytes : {hex_preview} …")
                print(f"      (copy the above and send it for diagnosis)")
                print()
                continue

            if packet["game"] != last_game:
                print(f"\n[INFO] Game detected: {packet['game']}")
                last_game = packet["game"]

            if handle is None:
                handle = open_wheel(lib)
                if handle is not None:
                    print("\n[OK]   Logitech wheel connected via USB HID.")

            if packet["max_rpm"] <= 0:
                if handle is not None:
                    apply_led_action(lib, handle, LED_OFF, 0, 0, 0)
                print("  In menu — LEDs off …                   ", end="\r")
                continue

            min_rpm      = packet["max_rpm"] * LED_MIN_RPM_RATIO
            blink_thresh = packet["max_rpm"] * BLINK_RPM_RATIO

            action, blink_phase, last_blink = compute_led_state(
                current_rpm    = packet["current_rpm"],
                max_rpm        = packet["max_rpm"],
                blink_phase    = blink_phase,
                last_blink     = last_blink,
                now            = time.time(),
                blink_thresh   = blink_thresh,
                blink_interval = blink_interval,
            )

            if handle is not None:
                apply_led_action(lib, handle, action,
                                 packet["current_rpm"], min_rpm, packet["max_rpm"])

            blink_str = " *** REDLINE ***" if action in (LED_BLINK_ON, LED_BLINK_OFF) else ""
            gear_str  = "R" if packet["gear"] == 0 else str(packet["gear"])
            print(
                f"  RPM {packet['current_rpm']:6.0f} / {packet['max_rpm']:.0f}"
                f"  |  Gear {gear_str}"
                f"  |  {packet['game']}{blink_str}   ",
                end="\r",
            )

    except KeyboardInterrupt:
        print("\n[INFO] Shutting down …")
    finally:
        try:
            if handle is not None:
                _send_led_report(lib, handle, ALL_LEDS_OFF)
                lib.hid_close(handle)
            lib.hid_exit()
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
        print("[INFO] LEDs off. Socket closed.")
        print()
        input("  Press Enter to close this window …")


if __name__ == "__main__":  # pragma: no cover
    try:
        main()
    except Exception as exc:
        print(f"\n[FATAL] Unexpected error: {exc}")
        import traceback
        traceback.print_exc()
        print()
        input("  Press Enter to close this window …")
