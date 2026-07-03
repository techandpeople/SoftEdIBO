# node_magnet_sensor Firmware

Touch-sensing board: 4× MLX90393 magnetometers (+1 optional 5th on a second I2C
bus). A magnet sits above each sensor inside the silicone; pressing the skin
moves the magnet and changes the measured field. The board streams the
**node_magnet_sensor protocol** over ESP-NOW so it plugs straight into the SoftEdIBO PC
(`QuadrantDetector` / touch tracking).

Adapted from the thesis MLX90393 live-stream firmware. The offline **calibration
protocol (CSV) was intentionally dropped**: the SoftEdIBO runtime detects touch
with thresholds on the raw µT magnitudes (`QuadrantDetector`), not a calibrated
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
{"status":"node_magnet_sensor_ready","sensors":4,"variant":"mlx90393"}
```

**Stream** (~28 Hz, to the gateway once it is known):
```json
{"type":"magnet","mag":[uT,...],"act":[active_idx,...]}
```
- `mag` — per-sensor field-change magnitude in µT (`|sample − baseline|`).
- `act` — indices of sensors whose `mag ≥ act_threshold_ut` (the value the PC prefers).

**3-axis streaming:** the stream message can additionally carry
`"vec":[[dx,dy,dz],...]` — the per-sensor baseline-subtracted field delta in
whole µT — and the boot announce then gains `"vec":1`. `mag`/`act` are
unchanged, so the PC pipeline is unaffected; the direction information enables
vector touch compensation and richer offline analysis (`docs/TOUCH_COUPLING.md`).
Two ways to turn it on: the `[env:vector]` build (`-DMAG_VECTOR`) enables it
from boot (flash `[env:release]` to revert), or
`{"cmd":"configure","stream_vec":true}` toggles it at runtime on any build
(RAM-only — cleared by a reboot; the PC re-sends it when it needs vectors).

**Shared module:** all sensing/streaming logic lives in
`firmware/common/se_magnet.h`, which the direct actuator board
(`node_actuator` `[env:direct*]`) folds in too — one implementation, two
boards. This file only wires the buses, command dispatch and OTA.

**Commands** (PC → board, via gateway):
```json
{"cmd":"ping"}                                  // -> {"type":"pong"}
{"cmd":"rebaseline"}                            // re-zero all sensors now
{"cmd":"configure","act_threshold_ut":100}      // µT at/above which a sensor is "active"
{"cmd":"configure","adaptive_baseline":true,"baseline_tau_ms":2000}
```
Legacy `configure` fields (`fullscale_mt`, `act_threshold` as a 0..1 fraction) are
still accepted, converted to `act_threshold_ut = act_threshold × fullscale_mt`.

### Baseline (auto-zero)
Each sensor is auto-zeroed at boot by averaging the first 70 reads. Re-zero at
runtime with `{"cmd":"rebaseline"}` (e.g. after the silicone settles, or if the
board was touched during boot). Streaming pauses until the baseline is ready.

**Adaptive baseline (opt-in).** `{"cmd":"configure","adaptive_baseline":true,
"baseline_tau_ms":2000}` makes the baseline keep tracking slow drift after boot —
e.g. a chamber inflating under the magnet — so it isn't read as a touch. Frozen
per-sensor while that sensor is active (real touches survive); EWMA uses the real
elapsed time, so it's immune to ESP-NOW jitter. Off by default — only enable once
a coupling sweep confirms actuation contamination. See `docs/TOUCH_COUPLING.md`.

## Build & flash
```bash
cd firmware/node_magnet_sensor
pio run --target upload                # default (scalar protocol)
pio run -e vector --target upload      # + 3-axis "vec" streaming
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
   before each `readData`, and let `streamCount` grow to 12. The `mag`/`act`
   arrays already scale with `streamCount`.
3. **PC side:** `QuadrantDetector` is **hardcoded to exactly 4 sensors**
   (`src/hardware/quadrant_detector.py` — `raise ValueError` if ≠ 4). With 12
   sensors the 4-quadrant model no longer fits; route sensors to chambers via
   each skin's `touch.sensor_grid` / `sensor_to_chamber` map instead (the skin
   editor already supports per-sensor grids). Generalising the detector is OK —
   it was brought in from the thesis and is not yet validated.

The sensor count, layout and addressing should be confirmed with the colleague
before implementing.
