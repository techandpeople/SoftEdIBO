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
| `MAX_PRESSURE_ADC` | 1500 | ADC limit — burst protection |
| `MIN_PRESSURE_ADC` | 200  | ADC threshold for "empty" |
| `DEFAULT_INFLATE_DUTY` | 255 | Pump PWM when `value` omitted |
| `PRESSURE_CHECK_MS` | 200 | Safety check interval (ms) |
| `STATUS_REPORT_MS` | 500 | Status broadcast interval (ms) |
| `VALVE_SETTLE_MS` | 20 | Pause after valve toggle (ms) |
| `PUMP_PWM_FREQ` | 20000 | Pump PWM frequency (Hz) |

Adjust `MAX_PRESSURE_ADC` and `MIN_PRESSURE_ADC` after measuring sensor
output at known pressures with your specific voltage divider circuit.

## ESP-NOW Protocol

Commands received from gateway (gateway strips its own `"target"` field):

| `cmd` | Fields | Description |
|-------|--------|-------------|
| `inflate` | `chamber` (0-2), `value` (0-255), `target` (ADC) | Inflate until `target` pressure |
| `deflate` | `chamber` (0-2), `target` (ADC) | Deflate until `target` pressure |
| `stop` | `chamber` (optional) | Stop one or all chambers |
| `ping` | — | Responds `{"type":"pong"}` |

Status sent to gateway every `STATUS_REPORT_MS`:
```json
{"type":"status","chamber":0,"pressure":2048}
```

## Behaviour

- **Gateway discovery:** the MAC of the first ESP-NOW sender is stored as the
  gateway peer — no hardcoding needed.
- **Parallel operation:** multiple chambers can inflate/deflate simultaneously.
  The inflate pump runs at `max(duty)` of all active inflate chambers.
- **Pressure safety:** each chamber stops independently when its pressure
  target is reached; the pump stops automatically when no chamber is active.
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
