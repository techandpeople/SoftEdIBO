# Air Chamber Node Firmware

ESP32-WROOM-32 firmware for nodes that control 3 inflatable air chambers
via solenoid valves and two DRV8833-driven pumps.

## Hardware (per node)

| Component | GPIO |
|-----------|------|
| Chamber 0 — inflate valve | 22 |
| Chamber 0 — deflate valve | 23 |
| Chamber 1 — inflate valve | 21 |
| Chamber 1 — deflate valve | 13 |
| Chamber 2 — inflate valve | 14 |
| Chamber 2 — deflate valve | 33 |
| Inflate pump — DRV8833 ch A (PWM) | 25 |
| Deflate pump — DRV8833 ch B (PWM) | 26 |
| Pressure sensor ch0 — XGZP6847A (ADC) | 34 |
| Pressure sensor ch1 — XGZP6847A (ADC) | 35 |
| Pressure sensor ch2 — XGZP6847A (ADC) | 32 |

Pin definitions are in [`src/pins.h`](src/pins.h).

Valves: `HIGH` = open, `LOW` = closed.
Pumps: DRV8833 H-bridge, single PWM pin per channel (other pin tied on PCB).

## Build & Flash

Two build environments — **release** (production) and **debug** (development):

```bash
# Production — no Serial output, no debug overhead
pio run -e release --target upload

# Development — Serial logs, tx counters, "debug" command via ESP-NOW
pio run -e debug --target upload
```

The CI pipeline automatically selects the right environment:
- **Nightly** (push to `master`) → `debug` build
- **Stable release** (tag `v*`) → `release` build

## Pressure sensing

Pressure is read from XGZP6847A sensors using the datasheet transfer function
(see [`src/pressure.h`](src/pressure.h)):

```
P(kPa) = ((V_out / 3.3) - 0.05) * 100 / 0.9
```

All internal calculations use **kPa**. The ESP-NOW protocol exchanges values
as **percent (0–100)** of `MAX_KPA` (default 8.0 kPa). The PC never sees raw
ADC or kPa values.

## Tuning Constants (`src/main.cpp`)

| Constant | Default | Description |
|----------|---------|-------------|
| `MAX_KPA` | 8.0 | Hard safety cap in kPa — absolute burst protection |
| `DEFAULT_INFLATE_DUTY` | 255 | Pump PWM duty (0–255) for inflate/set_pressure |
| `PRESSURE_CHECK_MS` | 200 | Safety check interval (ms) |
| `STATUS_REPORT_MS` | 500 | Pressure status broadcast interval (ms) |
| `VALVE_SETTLE_MS` | 20 | Delay between closing one valve and opening the other (ms) |
| `PUMP_PWM_FREQ` | 20000 | Pump PWM frequency (Hz) — above audible range |

Per-chamber software limits can be set at runtime via `set_max_pressure`
(clamped to `MAX_KPA`). These survive until reboot.

## ESP-NOW Protocol

### Commands received from gateway

The gateway strips its own `"target"` field before forwarding.

| `cmd` | Fields | Description |
|-------|--------|-------------|
| `inflate` | `chamber` (0–2), `delta` (0–100, default 10) | Inflate by `delta`% relative to current pressure |
| `deflate` | `chamber` (0–2), `delta` (0–100, default 10) | Deflate by `delta`% relative to current pressure |
| `set_pressure` | `chamber` (0–2), `value` (0–100) | Inflate or deflate to an absolute target of `value`% |
| `set_max_pressure` | `chamber` (0–2), `value` (0–100, default 100) | Set per-chamber pressure ceiling (clamped to `MAX_KPA`) |
| `hold` | `chamber` (0–2) | Stop pump and close both valves — freeze at current pressure |
| `ping` | — | Responds `{"type":"pong"}` |
| `debug` | — | **(debug build only)** Responds with full node state snapshot |

#### Examples
```json
{"cmd":"set_max_pressure","chamber":0,"value":80}
{"cmd":"inflate","chamber":0,"delta":20}
{"cmd":"deflate","chamber":1,"delta":15}
{"cmd":"set_pressure","chamber":2,"value":75}
{"cmd":"hold","chamber":0}
```

### Status sent to gateway

Broadcast every `STATUS_REPORT_MS` (500 ms) for all 3 chambers:
```json
{"type":"status","chamber":0,"pressure":75}
```

### Debug response (debug build only)

```json
{"type":"debug","ch":[{"s":3,"kpa":2.4,"tgt":4.0,"max":8.0},...],"tx_ok":1520,"tx_fail":3,"drop":0,"up":342}
```

`s` = chamber state (0=idle, 1=pre-inflate, 2=pre-deflate, 3=inflating, 4=deflating),
`drop` = commands dropped due to queue overflow, `up` = uptime in seconds.

## Architecture

### Command queue

Commands are **queued** from the ESP-NOW callback (WiFi task) and processed in
`loop()` (Arduino task). This avoids blocking the WiFi stack — critical when
5+ nodes share the same gateway. The queue is a lock-free single-producer /
single-consumer ring buffer (16 slots).

### Non-blocking valve settle

When switching direction (inflate ↔ deflate) on a chamber, the old valve closes
and a settle timer starts. The new valve only opens after `VALVE_SETTLE_MS`
elapses in `loop()` — no `delay()` calls anywhere. Both valves are **never open
simultaneously** on the same chamber.

### Pump sharing

Multiple chambers can inflate/deflate simultaneously. The inflate pump runs at
`max(duty)` of all actively inflating chambers. The deflate pump runs at full
duty if any chamber is deflating. Pumps stop automatically when no chamber is
active.

## Debug vs Release builds

| Feature | Release | Debug |
|---------|---------|-------|
| `Serial.begin()` | not called | initialized at 115200 |
| Serial log output | compiled out | state transitions, safety stops, commands |
| `onSent` callback | not registered | tracks `tx_ok` / `tx_fail` |
| `{"cmd":"debug"}` | unknown command (ignored) | returns full state snapshot |
| Queue drop counter | not tracked | counted in `cmdDropped` |
| Compiler flags | `-O2 -DNDEBUG` | `-O0 -g -DDEBUG_BUILD` |

## Performance notes

- **Non-blocking loop** — no `delay()` anywhere; valve settle uses timestamps.
- **Cached ADC readings** — pressure read once per `PRESSURE_CHECK_MS`, reused
  by both safety checks and status broadcasts.
- **Unified pump recalc** — single `recalcPumps()` iterates chambers once for
  both inflate and deflate pumps.
- **`snprintf` for status messages** — fixed-format JSON written directly into
  stack buffers; no ArduinoJson heap allocation for outgoing packets.
- **LEDC channels:** inflate pump on channel 0, deflate pump on channel 1.
  PWM at 20 kHz — above audible range.

## Important caveats

- ESP-NOW and WiFi share the same radio. The node runs in `WIFI_STA` mode
  **without** connecting to an AP (channel 1 by default). The gateway must be
  on the same WiFi channel.
- Maximum ESP-NOW payload: **250 bytes**. Status messages are ~48 bytes.
- ADC pins 34, 35, 32 are **input-only** on the ESP32 (no pull-up/pull-down
  support). Ensure the sensor output is within 0–3.3 V.
