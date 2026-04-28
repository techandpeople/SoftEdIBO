# node_reservoir firmware

ESP32-WROOM-32 firmware for the multi-chamber reservoir node:

- Up to 12 chambers (inflate/deflate valves via 2x PCA9685 + 3x ULN2803A)
- Shared pressure and vacuum tanks
- 6 pumps (3x DRV3297)
- 16-channel sensor mux (74HC4067)

## Build

```bash
pio run -e release
pio run -e debug
```

## Boot autodetect

On boot, firmware runs four steps:

1. I2C scan (`0x40..0x4F`) for two PCA9685 chips.
2. Sensor scan on mux channels `I0..I15` and logs detected channels.
3. Pump to tank mapping by pulsing each pump with all chamber valves closed.
4. Idle-safe state until a `configure` command arrives.

If two distinct PCA9685 addresses are not found, firmware keeps outputs disabled and returns:

```json
{"type":"error","reason":"pca9685_address_conflict"}
```

for chamber/tank commands.

## Runtime commands

Shared with direct nodes:

- `inflate`
- `deflate`
- `set_pressure`
- `set_max_pressure`
- `hold`
- `ping`

Reservoir-specific:

```json
{"cmd":"configure","num_chambers":12,
 "pump_inflate_count":3,"pump_deflate_count":3,
 "tank_pressure_max_kpa":50.0,"tank_vacuum_max_kpa":50.0,
 "pump_groups":{"pressure":[1,3,5],"vacuum":[2,4,6]}}
```

```json
{"cmd":"set_tank_pressure","kind":"pressure","value":30.0}
```

## Status messages

Every 500 ms:

```json
{"type":"status","chamber":N,"pressure":pct}
{"type":"tank_status","kind":"pressure","pressure":pct}
{"type":"tank_status","kind":"vacuum","pressure":pct}
```
