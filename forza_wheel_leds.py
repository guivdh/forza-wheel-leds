"""
forza_wheel_leds.py
--------------------
Bridges Forza telemetry (UDP Data Out) to the Logitech G920/G29 RPM LEDs.

Supported games : Forza Horizon 5, Forza Horizon 6, Forza Motorsport (2023)
Supported wheels: Logitech G920, G29, G923 (any wheel with the Logitech Steering Wheel SDK)

Requirements:
  - Logitech G HUB installed and running
  - LogitechSteeringWheelEnginesWrapper.dll in the same folder as this script
  - Python 3.8+  (not needed if using the .exe release)

In-game setup (all supported Forza titles):
  Settings > HUD and Gameplay  (or Gameplay & HUD)
    Data Out             : ON
    Data Out IP Address  : 127.0.0.1
    Data Out IP Port     : 5607
"""

import ctypes
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

WHEEL_INDEX = 0       # 0 = first connected Logitech wheel

# ---------------------------------------------------------------------------
# LOGITECH STEERING WHEEL SDK  —  DLL BINDINGS
# ---------------------------------------------------------------------------

DLL_NAME = "LogitechSteeringWheelEnginesWrapper.dll"


def load_logitech_sdk() -> ctypes.CDLL:
    try:
        dll = ctypes.CDLL(DLL_NAME)
    except OSError:
        print(f"[ERROR] Could not load '{DLL_NAME}'.")
        print("        Place the DLL in the same folder as this script / .exe.")
        print("        Download the Logitech Steering Wheel SDK:")
        print("        https://www.logitechg.com/sdk/LogitechSteeringWheelSDK_8.75.30.zip")
        sys.exit(1)

    dll.LogiSteeringInitialize.restype  = ctypes.c_bool
    dll.LogiSteeringInitialize.argtypes = [ctypes.c_bool]

    dll.LogiSteeringShutdown.restype  = None
    dll.LogiSteeringShutdown.argtypes = []

    dll.LogiIsConnected.restype  = ctypes.c_bool
    dll.LogiIsConnected.argtypes = [ctypes.c_int]

    dll.LogiSetSteeringWheelRpmLeds.restype  = ctypes.c_bool
    dll.LogiSetSteeringWheelRpmLeds.argtypes = [
        ctypes.c_int,    # index
        ctypes.c_float,  # currentRPM
        ctypes.c_float,  # minRPM
        ctypes.c_float,  # maxRPM
    ]

    return dll


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
LED_OFF     = "off"        # all LEDs off (menu / no race)
LED_NORMAL  = "normal"     # progressive LEDs (normal RPM range)
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
# HELPERS
# ---------------------------------------------------------------------------

def leds_off(dll: ctypes.CDLL) -> None:
    """Turn all RPM LEDs off."""
    dll.LogiSetSteeringWheelRpmLeds(
        WHEEL_INDEX,
        ctypes.c_float(0.0),
        ctypes.c_float(1.0),
        ctypes.c_float(1.0),
    )


def leds_on(dll: ctypes.CDLL) -> None:
    """Turn all RPM LEDs fully on."""
    dll.LogiSetSteeringWheelRpmLeds(
        WHEEL_INDEX,
        ctypes.c_float(1.0),
        ctypes.c_float(0.0),
        ctypes.c_float(1.0),
    )


def apply_led_action(dll: ctypes.CDLL, action: str, current_rpm: float, min_rpm: float, max_rpm: float) -> None:
    """Apply a LED action (returned by compute_led_state) to the wheel."""
    if action == LED_OFF or action == LED_BLINK_OFF:
        leds_off(dll)
    elif action == LED_BLINK_ON:
        leds_on(dll)
    else:  # LED_NORMAL
        dll.LogiSetSteeringWheelRpmLeds(
            WHEEL_INDEX,
            ctypes.c_float(current_rpm),
            ctypes.c_float(min_rpm),
            ctypes.c_float(max_rpm),
        )


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 58)
    print("  forza-wheel-leds  |  Logitech G29 / G920 RPM LEDs")
    print("=" * 58)
    print(f"  Version        : 1.0.5")
    print(f"  Listening on   : {UDP_IP}:{UDP_PORT}")
    print(f"  LED min RPM    : {int(LED_MIN_RPM_RATIO * 100)} % of redline")
    print(f"  Blink at       : {int(BLINK_RPM_RATIO * 100)} % of redline  ({BLINK_HZ:.0f} Hz)")
    print(f"  Wheel index    : {WHEEL_INDEX}")
    print("=" * 58)
    print()

    # --- Logitech SDK ---
    dll = load_logitech_sdk()
    print("[OK]   Logitech DLL loaded.")

    if not dll.LogiSteeringInitialize(False):
        print("[WARN] LogiSteeringInitialize returned False.")
        print("       Make sure Logitech G HUB is installed and running.")
    else:
        print("[OK]   Logitech G HUB connection established.")

    time.sleep(0.5)  # Give the SDK a moment to enumerate devices

    if not dll.LogiIsConnected(WHEEL_INDEX):
        print(f"[WARN] No Logitech wheel detected at index {WHEEL_INDEX}.")
        print("       LEDs will activate once a wheel is connected.")
    else:
        print(f"[OK]   Wheel detected at index {WHEEL_INDEX}.")

    # --- UDP socket ---
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((UDP_IP, UDP_PORT))
    sock.settimeout(1.0)

    print()
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
                data, _ = sock.recvfrom(2048)
            except socket.timeout:
                continue

            packet = patch_and_parse(data)
            if packet is None:
                continue

            # Announce game change
            if packet["game"] != last_game:
                print(f"\n[INFO] Game detected: {packet['game']}")
                last_game = packet["game"]

            if packet["max_rpm"] <= 0:
                apply_led_action(dll, LED_OFF, 0, 0, 0)
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

            apply_led_action(dll, action, packet["current_rpm"], min_rpm, packet["max_rpm"])

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
        apply_led_action(dll, LED_OFF, 0, 0, 0)
        dll.LogiSteeringShutdown()
        sock.close()
        print("[INFO] LEDs off. Socket closed.")
        print()
        input("  Press Enter to close this window …")


if __name__ == "__main__":  # pragma: no cover
    main()
