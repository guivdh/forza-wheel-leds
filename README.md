# forza-wheel-leds

Lights up the RPM LEDs on your **Logitech G29 / G920** steering wheel using live telemetry from **Forza Horizon 5**, **Forza Horizon 6**, and **Forza Motorsport (2023)**.

Forza does not natively drive the wheel LEDs — this tool bridges the gap.

![CI](https://github.com/guivdh/forza-wheel-leds/actions/workflows/build.yml/badge.svg)
![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)
![Windows](https://img.shields.io/badge/Windows-10%2F11-blue)
![Python](https://img.shields.io/badge/Python-3.8%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)

---

## How it works

```
Forza (UDP Data Out)  →  forza-wheel-leds  →  USB HID  →  G29 / G920 RPM LEDs
```

Forza broadcasts real-time telemetry over UDP (~60 packets/s).  
This tool reads `CurrentEngineRpm` and `EngineMaxRpm` from each packet and sends a direct **USB HID command** to the wheel to light the 5 LEDs accordingly.

No Logitech G HUB required. No DLL. No driver.

---

## Requirements

| Requirement | Notes |
|---|---|
| Windows 10 / 11 | Linux/macOS not officially supported |
| Logitech G29 or G920 | Plugged in via USB |
| Python 3.8+ | Only needed if running the `.py` script — not needed for the `.exe` release |

---

## Quick Start

### Option A — Pre-built .exe (no Python needed)

1. Download the latest `forza_wheel_leds_vX.X.X.zip` from [Releases](../../releases/latest) and extract it.
2. Configure Forza (see [In-game setup](#in-game-setup)).
3. Plug in your G29 or G920 via USB.
4. Double-click `forza_wheel_leds.exe`.

No installation required. No G HUB. No drivers.

### Option B — Python script

```bash
pip install hidapi
python forza_wheel_leds.py
```

---

## In-game Setup

> This must be done **once** per Forza title.

| Game | Settings path |
|---|---|
| Forza Horizon 5 | Settings → **HUD and Gameplay** → scroll to bottom |
| Forza Horizon 6 | Settings → **HUD and Gameplay** → scroll to bottom |
| Forza Motorsport 2023 | Settings → **Gameplay & HUD** → scroll to bottom |

Set the following values:

```
Data Out             : ON
Data Out IP Address  : 127.0.0.1
Data Out IP Port     : 5607
```

---

## Supported Wheels

| Wheel | LEDs | USB PID |
|---|---|---|
| Logitech G29 | 5 (green → yellow → red) | `046D:C24F` |
| Logitech G920 | 5 | `046D:C262` |

---

## Troubleshooting

**LEDs don't light up**
- Is the wheel plugged in via USB?
- Did you set Data Out to ON in Forza and use port `5607`?
- Make sure the wheel is in **PC mode** (not console mode).

**`[WARN] No supported Logitech wheel detected`**
- Plug in the wheel before launching the tool, or restart the tool after plugging in.

**Wrong port**
- Make sure the port in Forza's settings matches `UDP_PORT` in the script (default: `5607`).

---

## License

MIT — see [LICENSE](LICENSE).

---

## Under the hood

### 1. The UDP telemetry stream

When **Data Out** is enabled, Forza broadcasts one binary UDP datagram per physics tick (~60 Hz) to the configured IP:port. The packet is a flat C struct serialized in **little-endian** byte order.

FH5 and FH6 send **323 bytes** per packet. FM2023 sends **331 bytes** (8 extra bytes at the end for tire wear and track ID).

### 2. The 12-byte gap (FH4/FH5/FH6 quirk)

The packet is not a single contiguous struct. **Bytes 232–243 are padding** inserted by Playground Games — they contain no useful data. Before parsing, the script removes them:

```
Raw packet (323 bytes):
[  0 ────────────── 231 ][ 232 ─ 243 ][ 244 ──────────── 322 ]
        sled fields        12-byte gap      dash-only fields

After patch (311 bytes):
[  0 ────────────── 231 ][ 232 ──────────────────────────── 310 ]
        sled fields              dash-only fields (shifted)
```

```python
patched = data[:232] + data[244:323]   # skip bytes 232–243
```

### 3. Packet layout (key fields)

The 311-byte patched buffer maps to 85 little-endian fields. Here are the ones used by this script:

```
Offset  Size  Type   Field
──────  ────  ─────  ────────────────────────
0       4     s32    IsRaceOn       — 1 = driving, 0 = menus
                                     Note: 0 also in free roam (FH series)
                                     → script only checks max_rpm > 0
4       4     u32    TimestampMS    — millisecond counter (unused)
8       4     f32    EngineMaxRpm   — redline RPM of the current car
12      4     f32    EngineIdleRpm  — idle RPM (unused)
16      4     f32    CurrentEngineRpm — live RPM
...     ...   ...    (53 other fields: suspension, tyres, speed, position…)
244     4     f32    Speed          — m/s
252     4     f32    Power          — watts
256     4     f32    Torque         — N·m
...     ...   ...
296     1     u8     Gear           — 0 = reverse, 1–10 = forward gears
...
```

> Full struct definition: [`forza_wheel_leds.py` → `DASH_FORMAT`](forza_wheel_leds.py)

### 4. LED logic

From the RPM values the script computes a 5-bit bitmask and sends it directly to the wheel:

```python
min_rpm  = max_rpm * 0.70   # first LED lights at 70 % of redline
bitmask  = rpm_to_bitmask(current_rpm, min_rpm, max_rpm)
# e.g. 0b00111 = LEDs 1-2-3 on, 4-5 off
```

```
min_rpm ──────────────────────────── max_rpm
  ○ ○ ○ ○ ○  →  ● ○ ○ ○ ○  →  ● ● ● ● ●
```

When `currentRPM ≥ 97 % of max_rpm` (rev-limiter zone), all LEDs flash on/off at 10 Hz.

### 5. USB HID command

The G29 and G920 accept LED commands via a 7-byte HID output report (confirmed from the Linux kernel `hid-lg4ff.c`):

```
Byte 0: 0xF8   — extended command prefix
Byte 1: 0x12   — LED control sub-command
Byte 2: bitmask (bit 0 = LED 1 leftmost … bit 4 = LED 5 rightmost)
Byte 3-6: 0x00
```

This is sent directly via the `hid` Python library (HIDAPI wrapper), bypassing G HUB entirely.

```
forza_wheel_leds.py
  │
  ├── socket.recvfrom(2048)    — UDP listener (blocking, 1 s timeout)
  │
  └── hid.device().write([0x00, 0xF8, 0x12, bitmask, 0, 0, 0, 0])
          └── USB HID output report → G29 / G920 LEDs
```
