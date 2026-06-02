# node_sensor Firmware

Touch-sensing board: 4× MLX90393 magnetometers (+1 optional 5th on a second I2C
bus). A magnet sits above each sensor inside the silicone; pressing the skin
moves the magnet and changes the measured field. The board streams the
**node_imu protocol** over ESP-NOW so it plugs straight into the SoftEdIBO PC
(`QuadrantDetector` / touch tracking).

Adapted from the thesis MLX90393 live-stream firmware. The offline **calibration
protocol (CSV) was intentionally dropped**: the SoftEdIBO runtime detects touch
with thresholds on the normalised values (`QuadrantDetector`), not a calibrated
model. The only "calibration" the runtime needs is the per-sensor baseline,
which this firmware computes automatically (see below).

## Hardware

- **Board:** ESP32-WROOM-32 (esp32dev) — kept on its own ESP for now.
- **Primary I2C** (sensors S0..S3): SDA = IO21, SCL = IO22, addrs `0x18 0x19 0x1A 0x1B`.
- **Secondary I2C** (optional S5): SDA = IO16, SCL = IO17, addr `0x1A`.
- MLX90393 config: gain 2×, OSR 2, filter 3 (≈28 Hz with 4 sensors).

Sensor order is significant: **S0→Q1 (top-left), S1→Q2 (top-right),
S2→Q3 (bottom-left), S3→Q4 (bottom-right)** — matches the PC `QuadrantDetector`
(which consumes the first 4 sensors; the optional 5th is appended after them).

## ESP-NOW protocol

**Boot** (broadcast):
```json
{"status":"node_imu_ready","sensors":4,"variant":"mlx90393"}
```

**Stream** (~28 Hz, to the gateway once it is known):
```json
{"type":"imu","mag":[mT,...],"adj":[0.0-1.0,...],"act":[active_idx,...]}
```
- `mag` — per-sensor field-change magnitude in mT (`|sample − baseline|`).
- `adj` — `mag / fullscale_mt`, clamped 0..1 (the value the PC prefers).
- `act` — indices of sensors whose `adj ≥ act_threshold`.

**Commands** (PC → board, via gateway):
```json
{"cmd":"ping"}                                  // -> {"type":"pong"}
{"cmd":"rebaseline"}                            // re-zero all sensors now
{"cmd":"configure","fullscale_mt":30,"act_threshold":0.2}
```

### Baseline (auto-zero)
Each sensor is auto-zeroed at boot by averaging the first 70 reads. Re-zero at
runtime with `{"cmd":"rebaseline"}` (e.g. after the silicone settles, or if the
board was touched during boot). Streaming pauses until the baseline is ready.

## Build & flash
```bash
cd firmware/node_sensor
pio run --target upload
```
Uses the shared `firmware/common/se_espnow.h` (added to the include path in
`platformio.ini`), the same ESP-NOW layer as the actuator nodes and the gateway.

---

## Planned / TODO — scale to ~12 sensors via I2C (not yet implemented)

A colleague flagged that **more I2C will be needed to support ~3× the sensors**
(i.e. ~12 instead of the current 4). This runs into a hard limit:

- The **MLX90393 has only 4 I2C addresses** per bus (`0x18–0x1B`, set by its 2
  address pins). The ESP32 has **2 hardware I2C controllers** (`Wire`, `Wire1`),
  so **8 sensors is the ceiling without extra hardware**.
- For ~12 sensors, add an **I2C multiplexer (TCA9548A)**: one bus fans out into
  8 channels, each carrying up to 4 MLX90393. Select the channel, then talk to
  the sensor at its address.

### Gaps to close when implementing
1. **Hardware:** add a TCA9548A on the primary bus (IO21/IO22). Each touch point
   = (mux channel, MLX address).
2. **Firmware (this file):** replace the fixed `PRIMARY_ADDR[4]` + single extra
   bus with a sensor table of `{mux_channel, address}`, select the channel
   before each `readData`, and let `streamCount` grow to 12. The `mag`/`adj`/
   `act` arrays already scale with `streamCount`.
3. **PC side:** `QuadrantDetector` is **hardcoded to exactly 4 sensors**
   (`src/hardware/quadrant_detector.py` — `raise ValueError` if ≠ 4). With 12
   sensors the 4-quadrant model no longer fits; route sensors to chambers via
   each skin's `touch.sensor_grid` / `sensor_to_chamber` map instead (the skin
   editor already supports per-sensor grids). Generalising the detector is OK —
   it was brought in from the thesis and is not yet validated.

The sensor count, layout and addressing should be confirmed with the colleague
before implementing.
