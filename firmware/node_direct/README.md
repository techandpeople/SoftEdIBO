# node_direct firmware

ESP32-WROOM-32 firmware for a 3-chamber air controller with onboard pumps.
Valves are driven through a ULN2803A Darlington array; pumps via a single
DRV3297 motor driver.

## Hardware

| Signal | GPIO | Notes |
|--------|------|-------|
| PSENSOR1 | IO39 (J2_4, SENSOR_VN) | Chamber 0 pressure sensor (XGZP6847A) |
| PSENSOR2 | IO34 (J2_5) | Chamber 1 pressure sensor |
| PSENSOR3 | IO35 (J2_6) | Chamber 2 pressure sensor |
| PUMP1    | IO32 (J2_7) | Inflate pump (DRV3297, PWM) |
| PUMP2    | IO33 (J2_8) | Deflate pump (DRV3297, PWM) |
| VALVE1   | IO25 (J2_9, ULN2803A in)  | Chamber 0 inflate |
| VALVE2   | IO4  (J3_13, ULN2803A in) | Chamber 0 deflate |
| VALVE3   | IO16 (J3_12, ULN2803A in) | Chamber 1 inflate |
| VALVE4   | IO17 (J3_11, ULN2803A in) | Chamber 1 deflate |
| VALVE5   | IO18 (J3_9,  ULN2803A in) | Chamber 2 inflate |
| VALVE6   | IO19 (J3_8,  ULN2803A in) | Chamber 2 deflate |
| VALVE7   | IO26 (J2_10) | Spare (unused by firmware) |
| VALVE8   | IO27 (J2_11) | Spare (unused by firmware) |

Pin assignments verified from the schematic netlist.

## Build & Flash

```bash
pio run -e release --target upload   # production (no Serial)
pio run -e debug   --target upload   # development (Serial logs)
```

## Source layout

| File | Purpose |
|------|---------|
| `pins.h`      | GPIO assignments + chamber count |
| `pressure.h`  | XGZP6847A ADC → kPa conversion |
| `units.h`     | kPa ↔ percent helpers |
| `dbg.h`       | DBG_PRINT macros (no-op in release) |
| `cmd_queue.h` | Lock-free SPSC ring buffer |
| `chambers.h`  | Per-chamber state machine + valve/pump control |
| `commands.h`  | ESP-NOW JSON parsing + status broadcasts |
| `main.cpp`    | Arduino setup/loop + ESP-NOW callbacks |

## Protocol

JSON commands over ESP-NOW (gateway forwards):

| Command | Fields | Description |
|---------|--------|-------------|
| `inflate` | `chamber` (0–2), `delta` (0–100) | Inflate by delta % of `max_kpa` |
| `deflate` | `chamber` (0–2), `delta` (0–100) | Deflate by delta % of `max_kpa` |
| `set_pressure` | `chamber` (0–2), `value` (0–100) | Absolute target % |
| `set_max_pressure` | `chamber` (0–2), `value` (kPa) | Per-chamber safety cap |
| `hold` | `chamber` (0–2) | Stop pump, close both valves |
| `ping` | — | Responds `{"type":"pong"}` |
| `debug` | — | (debug build only) Full state snapshot |

Status broadcast every 500 ms for each chamber:
`{"type":"status","chamber":N,"pressure":pct}`

All pressures exchanged are 0–100 (% of each chamber's `max_kpa`). The gateway
must call `set_max_pressure` on connect so the chambers can't overshoot if the
PC crashes mid-session.
