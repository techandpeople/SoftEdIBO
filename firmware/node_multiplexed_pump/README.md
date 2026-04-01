# Mux Chamber Node Firmware

ESP32-WROOM-32 firmware for nodes that control **N inflatable air chambers**
via a 74HC595 shift-register valve chain and a 74HC4051 analog sensor mux.
Supports up to 8 chambers per node with a single sensor mux chip.
Daisy-chain more 74HC595 chips for more than 4 chambers per shift register.

Use this firmware when you need more than 3 chambers on a single ESP32.

## Hardware (per node)

### Valve control — 74HC595 shift register

Bit layout per chamber `i`: `bit(i*2)` = inflate valve, `bit(i*2+1)` = deflate valve.

| Signal | GPIO |
|--------|------|
| SER (data in) | 23 |
| SRCLK (shift clock) | 22 |
| RCLK (latch) | 21 |

Daisy-chain additional 74HC595 chips (Q7' → SER of next chip) for more than 4 chambers.
Each chip provides 4 chamber valve pairs (8 bits = 4 × inflate + deflate).

### Sensor mux — 74HC4051 analog multiplexer

| Signal | GPIO |
|--------|------|
| S0 (select bit 0) | 13 |
| S1 (select bit 1) | 12 |
| S2 (select bit 2) | 14 |
| SIG (ADC1 input) | 34 |

S0/S1/S2 select one of 8 XGZP6847A sensors. SIG is read by a single ADC1 pin.
A brief settle delay (`SMUX_SETTLE_US = 10 µs`) is applied after channel switching.

For more than 8 chambers, add a second 74HC4051 on a different ADC1 pin sharing the
same S0/S1/S2 lines — extend the firmware accordingly.

### Pumps — DRV8833 (standard mode only)

Not present in **reservoir mode**.

| Signal | GPIO |
|--------|------|
| Inflate pump (DRV8833 ch A, PWM) | 25 |
| Deflate pump (DRV8833 ch B, PWM) | 26 |

Pin definitions are in [`src/pins.h`](src/pins.h).

Valves: `HIGH` = open, `LOW` = closed.

## NUM_CHAMBERS

`NUM_CHAMBERS` is a **compile-time constant** set via PlatformIO build flags
(e.g. `-DNUM_CHAMBERS=8`). It determines how many chambers the firmware manages.

- Default in `platformio.ini`: `8` (can be overridden per build).
- Maximum: `8` with a single 74HC4051 sensor mux (enforced by `static_assert`).
- For fewer chambers, reduce `NUM_CHAMBERS` to eliminate unnecessary ADC reads.

## Build & Flash

Four build environments — two standard (own pumps) and two reservoir mode (no onboard pumps):

| Environment | Pumps | Serial | When to use |
|-------------|-------|--------|-------------|
| `release`     | yes | no  | Production — mux node with own pumps |
| `debug`       | yes | yes | Development — mux node with own pumps |
| `release_res` | no  | no  | Production — fed by central reservoir node |
| `debug_res`   | no  | yes | Development — fed by central reservoir node |

```bash
# Production — standard mux node (own pumps)
pio run -e release --target upload

# Development — standard mux node (own pumps) + Serial logs
pio run -e debug --target upload

# Production — reservoir mode (no onboard pumps)
pio run -e release_res --target upload

# Development — reservoir mode + Serial logs
pio run -e debug_res --target upload
```

### Adjusting NUM_CHAMBERS

Edit `platformio.ini` and change `-DNUM_CHAMBERS=8` to the desired count:

```ini
[env:release]
build_flags = -O2 -DNDEBUG -DNUM_CHAMBERS=5
```

In **reservoir mode** (`-DRESERVOIR_MODE`), pump GPIO pins are not initialised and
`recalcPumps()` is a no-op. Air is supplied and removed by the central reservoir node.

## Pressure sensing

Pressure is read from XGZP6847A sensors using the datasheet transfer function
(see [`src/pressure.h`](src/pressure.h)):

```
P(kPa) = ((V_out / 3.3) - 0.05) * 100 / 0.9
```

All internal calculations use **kPa**. The ESP-NOW protocol exchanges values
as **percent (0–100)** of `max_kpa` per chamber. The PC never sees raw ADC or kPa values.

## Tuning Constants (`src/main.cpp`)

| Constant | Default | Description |
|----------|---------|-------------|
| `DEFAULT_MAX_KPA` | 8.0 | Default per-chamber safety cap in kPa |
| `HARD_MAX_KPA` | 12.0 | Absolute hard limit (cannot be exceeded by `set_max_pressure`) |
| `DEFAULT_INFLATE_DUTY` | 255 | Pump PWM duty (0–255) for inflate/set_pressure |
| `PRESSURE_CHECK_MS` | 200 | Safety check and ADC read interval (ms) |
| `STATUS_REPORT_MS` | 500 | Pressure status broadcast interval (ms) |
| `VALVE_SETTLE_MS` | 20 | Delay between closing one valve and opening the other (ms) |
| `SMUX_SETTLE_US` | 10 | 74HC4051 channel-select settle time (µs) |
| `PUMP_PWM_FREQ` | 20000 | Pump PWM frequency (Hz) — above audible range |

Per-chamber software limits can be set at runtime via `set_max_pressure`
(clamped to `HARD_MAX_KPA`). These survive until reboot.

## ESP-NOW Protocol

### Commands received from gateway

| `cmd` | Fields | Description |
|-------|--------|-------------|
| `inflate` | `chamber` (0–N-1), `delta` (0–100, default 10) | Inflate by `delta`% relative to current pressure |
| `deflate` | `chamber` (0–N-1), `delta` (0–100, default 10) | Deflate by `delta`% relative to current pressure |
| `set_pressure` | `chamber` (0–N-1), `value` (0–100) | Inflate or deflate to an absolute target of `value`% |
| `set_max_pressure` | `chamber` (0–N-1), `value` (kPa, ≤ 12.0) | Set per-chamber pressure ceiling |
| `hold` | `chamber` (0–N-1) | Stop pump and close both valves |
| `ping` | — | Responds `{"type":"pong"}` |
| `debug` | — | **(debug build only)** Full node state snapshot |

#### Examples
```json
{"cmd":"set_max_pressure","chamber":0,"value":6.5}
{"cmd":"inflate","chamber":3,"delta":20}
{"cmd":"deflate","chamber":7,"delta":15}
{"cmd":"set_pressure","chamber":2,"value":75}
{"cmd":"hold","chamber":0}
```

### Status sent to gateway

Broadcast every `STATUS_REPORT_MS` (500 ms) for all `NUM_CHAMBERS` chambers:
```json
{"type":"status","chamber":3,"pressure":75}
```

### Debug response (debug build only)

```json
{"type":"debug","num_chambers":8,"defaults":{"max_kpa":8.0,"hard_max_kpa":12.0},"reservoir_mode":false,"ch":[{"s":0,"st":"IDLE","kpa":0.000,"pct":0,"tgt_kpa":0.000,"tgt_pct":0,"max_kpa":8.0},...], "tx_ok":1520,"tx_fail":3,"drop":0,"up":342}
```

`s` = chamber state (0=idle, 1=pre-inflate, 2=pre-deflate, 3=inflating, 4=deflating).

## Architecture

### Valve control via shift register

`flushShiftReg()` writes `srState` (uint32_t, up to 32 valve bits = 16 chambers) to
the shift register chain MSB-first. Called every time a valve opens or closes.

### Sensor reading loop

All `NUM_CHAMBERS` sensors are read sequentially in each `PRESSURE_CHECK_MS` tick:
mux selects channel → settle → ADC read → next channel. Results cached in `cachedKpa[]`.

### Command queue

Commands are **queued** from the ESP-NOW callback (WiFi task) and processed in
`loop()` (Arduino task). The queue is a lock-free single-producer / single-consumer
ring buffer (16 slots).

### Non-blocking valve settle

When switching direction (inflate ↔ deflate) on a chamber, the old valve closes
and a settle timer starts. The new valve only opens after `VALVE_SETTLE_MS`
elapses in `loop()` — no `delay()` calls anywhere.

### Pump sharing (standard mode)

Multiple chambers can inflate/deflate simultaneously. The inflate pump runs at
`max(duty)` of all actively inflating chambers. The deflate pump runs at full
duty if any chamber is deflating. Pumps stop automatically when no chamber is active.

## Debug vs Release builds

| Feature | Release | Debug |
|---------|---------|-------|
| `Serial.begin()` | not called | initialized at 115200 |
| Serial log output | compiled out | state transitions, safety stops, commands |
| `onSent` callback | not registered | tracks `tx_ok` / `tx_fail` |
| `{"cmd":"debug"}` | ignored | returns full state snapshot |
| Queue drop counter | not tracked | counted in `cmdDropped` |
| Compiler flags | `-O2 -DNDEBUG` | `-O0 -g -DDEBUG_BUILD` |

## Important caveats

- ESP-NOW and WiFi share the same radio. The node runs in `WIFI_STA` mode
  **without** connecting to an AP (channel 1 by default). The gateway must be
  on the same WiFi channel.
- Maximum ESP-NOW payload: **250 bytes**. Status messages are ~48 bytes.
  Debug responses for 8 chambers are ~400 bytes — split if needed.
- ADC pin 34 is **input-only** on the ESP32 (no pull-up/pull-down support).
  Ensure the XGZP6847A output is within 0–3.3 V.
- All ADC readings go through the single `SMUX_SIG_PIN`. Read one chamber at a
  time; do not overlap reads.
