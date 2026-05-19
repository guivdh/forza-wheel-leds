"""
forza_wheel_leds.py
--------------------
Bridges Forza telemetry (UDP Data Out) to the Logitech G29/G920 RPM LEDs.

Supported games : Forza Horizon 5, Forza Horizon 6, Forza Motorsport (2023)
Supported wheels: Logitech G29, G920 (direct USB HID — no G HUB required)

Requirements:
  - Python 3.8+  (not needed if using the .exe release)
  - hidapi package  (pip install hidapi)  — bundled in the .exe release

In-game setup (all supported Forza titles):
  Settings > HUD and Gameplay  (or Gameplay & HUD)
    Data Out             : ON
    Data Out IP Address  : 127.0.0.1
    Data Out IP Port     : 5607
"""

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
#   0.70 → first LED at 70 % of redline  (shift-indicator feel — recommended)
#   0.50 → first LED at 50 %             (wider spread, always visible)
LED_MIN_RPM_RATIO = 0.70

# Rev-limiter blink: LEDs flash when RPM exceeds this fraction of max RPM.
#   0.97 → blink starts at 97 % of redline (recommended)
#   1.00 → disable blinking
BLINK_RPM_RATIO = 0.97

# Blink frequency in Hz (on/off cycles per second).
BLINK_HZ = 10.0

# ---------------------------------------------------------------------------
# LOGITECH G29 / G920  —  DIRECT USB HID
# ---------------------------------------------------------------------------

# Logitech USB Vendor ID
LOGITECH_VID = 0x046D

# Known wheel Product IDs (G29 PC/PS3 mode, G920)
WHEEL_PIDS = [
    0xC24F,  # G29 (PC / PS3 mode)
    0xC262,  # G920
]

# HID output report: extended command 0xF8, sub-command 0x12 = LED control.
# Byte layout (7 bytes, prepended with report-ID 0x00 by hidapi):
#   [0] 0xF8  extended command prefix
#   [1] 0x12  LED sub-command
#   [2] bitmask  (bit 0 = LED1 leftmost/green … bit 4 = LED5 rightmost/red)
#   [3..6] 0x00
_HID_LED_PREFIX = [0xF8, 0x12]
_HID_LED_SUFFIX = [0x00, 0x00, 0x00, 0x00]

# Total of 5 LEDs on the G29 (and G920 maps them to its own 5-LED arc).
NUM_LEDS = 5
ALL_LEDS_ON  = (1 << NUM_LEDS) - 1   # 0x1F
ALL_LEDS_OFF = 0x00


def open_wheel(hid_module):
    """
    Try to open the first recognised Logitech wheel via USB HID.
    Returns an open device handle (int) or None if no supported wheel is found.

    Uses the cython-hidapi API: hid.open(vid, pid) returns an integer handle.
    """
    for pid in WHEEL_PIDS:
        try:
            handle = hid_module.open(LOGITECH_VID, pid)
            return handle
        except OSError:
            continue
    return None


def _send_led_report(hid_module, handle, bitmask: int) -> None:
    """Write a 7-byte LED control output report to the HID device."""
    # cython-hidapi: hid.write(handle, data) — data is bytes, first byte is report ID.
    report = bytes([0x00, 0xF8, 0x12, bitmask & 0xFF, 0x00, 0x00, 0x00, 0x00])
    hid_module.write(handle, report)


def rpm_to_bitmask(current_rpm: float, min_rpm: float, max_rpm: float) -> int:
    """
    Convert an RPM value to a 5-bit LED bitmask.

    LEDs light up progressively from left (bit 0) to right (bit 4) as
    current_rpm rises from min_rpm to max_rpm.

    Returns 0x00 if current_rpm < min_rpm, 0x1F if current_rpm >= max_rpm.
    """
    if max_rpm <= min_rpm:
        return ALL_LEDS_OFF
    if current_rpm <= min_rpm:
        return ALL_LEDS_OFF
    if current_rpm >= max_rpm:
        return ALL_LEDS_ON

    ratio = (current_rpm - min_rpm) / (max_rpm - min_rpm)
    # Number of LEDs to light: 1..NUM_LEDS
    n_lit = max(1, round(ratio * NUM_LEDS))
    return (1 << n_lit) - 1


# ---------------------------------------------------------------------------
# FORZA PACKET PARSING
# ---------------------------------------------------------------------------

# FH5 / FH6 raw packet = 323 bytes.
# Bytes 232–243 are a 12-byte padding gap specific to FH4/FH5/FH6.
# After skipping that gap we get 311 bytes that map to DASH_FORMAT below.
#
# FM2023 raw packet = 331 bytes.
# It carries the same gap, plus 8 extra bytes at the tail
# (TireWear x4 floats + TrackOrdinal s32).  We ignore the tail for now
# and apply the same gap fix so the same parser works for both titles.

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

# Field indices in the unpacked tuple
IDX_IS_RACE_ON      = 0
IDX_ENGINE_MAX_RPM  = 2
IDX_ENGINE_IDLE_RPM = 3
IDX_CURRENT_RPM     = 4
IDX_GEAR            = 81  # 5th entry in the BBBBB block (0-indexed)

# Raw packet sizes used for game detection
SIZE_FH5_FH6 = 323
SIZE_FM2023  = 331

GAME_LABELS = {
    SIZE_FH5_FH6: "FH5 / FH6",
    SIZE_FM2023:  "FM2023",
}


def patch_and_parse(data: bytes):
    """
    Remove the 12-byte FH4/FH5/FH6 gap (bytes 232–243),
    unpack the struct, and return a named dict of the fields we care about.
    Returns None if the packet size is not recognised.
    """
    size = len(data)
    if size not in (SIZE_FH5_FH6, SIZE_FM2023):
        return None

    patched = data[:232] + data[244:323]  # always 311 bytes after patch

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

# LED actions returned by compute_led_state()
LED_OFF       = "off"        # all LEDs off (menu / no race)
LED_NORMAL    = "normal"     # progressive LEDs (normal RPM range)
LED_BLINK_ON  = "blink_on"  # all LEDs on  (rev-limiter blink phase)
LED_BLINK_OFF = "blink_off" # all LEDs off (rev-limiter blink phase)


def compute_led_state(
    current_rpm: float,
    max_rpm: float,
    blink_phase: bool,
    last_blink: float,
    now: float,
    blink_thresh: float,
    blink_interval: float,
) -> tuple:
    """
    Pure function: given the current telemetry and blink state, return
    (action, new_blink_phase, new_last_blink).

    Parameters
    ----------
    current_rpm    : current engine RPM
    max_rpm        : engine max (redline) RPM
    blink_phase    : current blink phase (True = LEDs on)
    last_blink     : timestamp of last blink toggle
    now            : current timestamp
    blink_thresh   : RPM threshold above which blinking starts
    blink_interval : seconds between blink toggles (= 1 / BLINK_HZ)

    Returns
    -------
    (action, new_blink_phase, new_last_blink)
    """
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

def apply_led_action(hid_module, handle, action: str, current_rpm: float, min_rpm: float, max_rpm: float) -> None:
    """Apply a LED action (returned by compute_led_state) to the wheel via HID."""
    if action == LED_OFF or action == LED_BLINK_OFF:
        _send_led_report(hid_module, handle, ALL_LEDS_OFF)
    elif action == LED_BLINK_ON:
        _send_led_report(hid_module, handle, ALL_LEDS_ON)
    else:  # LED_NORMAL
        bitmask = rpm_to_bitmask(current_rpm, min_rpm, max_rpm)
        _send_led_report(hid_module, handle, bitmask)


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 58)
    print("  forza-wheel-leds  |  Logitech G29 / G920 RPM LEDs")
    print("=" * 58)
    print(f"  Version        : 1.1.0")
    print(f"  Listening on   : {UDP_IP}:{UDP_PORT}")
    print(f"  LED min RPM    : {int(LED_MIN_RPM_RATIO * 100)} % of redline")
    print(f"  Blink at       : {int(BLINK_RPM_RATIO * 100)} % of redline  ({BLINK_HZ:.0f} Hz)")
    print("=" * 58)
    print()

    # --- HID wheel ---
    try:
        import hid as hid_module
    except ImportError:
        print("[ERROR] 'hidapi' package not found.")
        print("        Install it with:  pip install hidapi")
        print()
        input("  Press Enter to close this window …")
        sys.exit(1)

    handle = open_wheel(hid_module)
    if handle is None:
        print("[WARN] No supported Logitech wheel detected (G29 / G920).")
        print("       Make sure the wheel is plugged in via USB.")
        print("       LEDs will not work until the wheel is connected.")
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
                print(f"  [UDP] Packet received from {addr[0]}:{addr[1]} "
                      f"— size {len(data)} bytes (unrecognised format)   ", end="\r")
                continue

            # Announce game change
            if packet["game"] != last_game:
                print(f"\n[INFO] Game detected: {packet['game']}")
                last_game = packet["game"]

            if handle is None:
                handle = open_wheel(hid_module)
                if handle is not None:
                    print("\n[OK]   Logitech wheel connected via USB HID.")

            if packet["max_rpm"] <= 0:
                if handle is not None:
                    apply_led_action(hid_module, handle, LED_OFF, 0, 0, 0)
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
                apply_led_action(hid_module, handle, action, packet["current_rpm"], min_rpm, packet["max_rpm"])

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
                _send_led_report(hid_module, handle, ALL_LEDS_OFF)
                hid_module.close(handle)
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
