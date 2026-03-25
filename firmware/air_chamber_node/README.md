# Air Chamber Node Firmware

ESP32-WROOM-32 firmware for nodes that control 3 inflatable air chambers
via solenoid valves and two DRV8833-driven pumps.

## Hardware (per node)

| Component | GPIO |
|-----------|------|
| Chamber 0 — inflate valve | 15 |
| Chamber 0 — deflate valve | 4  |
| Chamber 1 — inflate valve | 16 |
| Chamber 1 — deflate valve | 17 |
| Chamber 2 — inflate valve | 5  |
| Chamber 2 — deflate valve | 18 |
| Inflate pump — DRV8833 IN1 (PWM) | 32 |
| Inflate pump — DRV8833 IN2       | 33 |
| Deflate pump — DRV8833 IN1 (PWM) | 25 |
| Deflate pump — DRV8833 IN2       | 26 |
| Pressure sensor ch0 — XGZP6847 (ADC) | 34 |
| Pressure sensor ch1 — XGZP6847 (ADC) | 35 |
| Pressure sensor ch2 — XGZP6847 (ADC) | 36 |

Valves: `HIGH` = open, `LOW` = closed.
Pumps: PWM on IN1, IN2 kept LOW (forward only).

## Build & Flash

```bash
cd firmware/air_chamber_node
pio run --target upload
```

## Tuning Constants (`src/main.cpp`)

| Constant | Default | Description |
|----------|---------|-------------|
| `MAX_PRESSURE_ADC` | 1500 | ADC hard cap — absolute burst protection. Per-chamber limits can be set lower at runtime via `set_max_pressure`. |
| `MIN_PRESSURE_ADC` | 200  | ADC threshold treated as "empty" |
| `DEFAULT_INFLATE_DUTY` | 255 | Pump PWM duty (0–255) used for inflate/set_pressure commands |
| `PRESSURE_CHECK_MS` | 200 | Safety check interval (ms) |
| `STATUS_REPORT_MS` | 500 | Pressure status broadcast interval (ms) |
| `VALVE_SETTLE_MS` | 20 | Pause after valve toggle (ms) |
| `PUMP_PWM_FREQ` | 20000 | Pump PWM frequency (Hz) — above audible range |

Calibrate `MAX_PRESSURE_ADC` after measuring sensor output at your target maximum pressure.
This value is the absolute safety boundary enforced in hardware — no command can exceed it.

Additionally, the PC sends `set_max_pressure` per chamber on startup to set a lower
software limit. This limit is stored in RAM and defaults to `MAX_PRESSURE_ADC` on
boot. If the PC app crashes, the node continues to enforce the last received limit.

## ESP-NOW Protocol

All pressure values exchanged with the PC are in **percent (0–100)** of `MAX_PRESSURE_ADC`.
The node converts internally to ADC units; the PC never needs to know raw ADC values.

### Commands received from gateway

The gateway strips its own `"target"` field before forwarding.

| `cmd` | Fields | Description |
|-------|--------|-------------|
| `inflate` | `chamber` (0–2), `delta` (0–100, default 10) | Inflate by `delta`% relative to current pressure |
| `deflate` | `chamber` (0–2), `delta` (0–100, default 10) | Deflate by `delta`% relative to current pressure |
| `set_pressure` | `chamber` (0–2), `value` (0–100) | Inflate or deflate to an absolute target of `value`% |
| `set_max_pressure` | `chamber` (0–2), `value` (0–100, default 100) | Set per-chamber pressure ceiling. Capped to `MAX_PRESSURE_ADC`. Survives until reboot. |
| `hold` | `chamber` (0–2) | Stop pump and close both valves — freeze at current pressure |
| `ping` | — | Responds `{"type":"pong"}` |

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
`pressure` is current ADC reading expressed as `current_adc * 100 / MAX_PRESSURE_ADC`.

## Behaviour

- **Gateway discovery:** the MAC of the first ESP-NOW sender is stored as the
  gateway peer — no hardcoding needed.
- **Parallel operation:** multiple chambers can inflate/deflate simultaneously.
  The inflate pump runs at `max(duty)` of all active inflate chambers.
- **Pressure safety:** each chamber stops independently when its pressure
  target is reached or when the per-chamber `max_pressure_adc` ceiling is hit;
  the pump stops automatically when no chamber is active.
- **Per-chamber limits:** the app sends `set_max_pressure` on startup. The node
  enforces this limit even if the app disconnects or crashes.
- **Valve interlock:** switching from inflate to deflate (or vice versa) on
  the same chamber closes the current valve before opening the next one
  (`VALVE_SETTLE_MS` delay).

## Performance notes

- **`-O3`** compiler flag — maximum runtime optimization.
- **ADC multisampling:** `readPressure()` averages `ADC_SAMPLES` (4) readings
  per call, reducing noise on the XGZP6847 analog output.
- **Pressure check skipped** when no chamber is active — avoids redundant ADC
  reads on every loop iteration.
- **`snprintf` for status messages** — fixed-format JSON (`sendStatus`,
  `pong`) written directly into a stack buffer; no ArduinoJson heap allocation.
- **`static const char pong[]`** — constant response stored in flash, never
  copied to the heap.
- **LEDC channels:** inflate pump on channel `PUMP1_LEDC_CH` (0), deflate
  pump on `PUMP2_LEDC_CH` (1). PWM at 20 kHz — above audible range.

## Important caveats

- ESP-NOW and WiFi share the same radio. The node runs in `WIFI_STA` mode
  **without** connecting to an AP (channel 1 by default). The gateway must be
  on the same WiFi channel.
- Maximum ESP-NOW payload: **250 bytes**. Status messages are ~48 bytes.
- ADC pins 34, 35, 36 are **input-only** on the ESP32 (no pull-up/pull-down
  support). Ensure the sensor output is within 0–3.3 V.
- Calibrate `MAX_PRESSURE_ADC` / `MIN_PRESSURE_ADC` after measuring sensor
  output at known pressures with your voltage divider.
