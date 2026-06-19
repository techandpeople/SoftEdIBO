# SoftEdIBO ESP-NOW Protocol

Three firmware flavors share this protocol:

- **`node_direct`** — 3 chambers, GPIO valves, onboard pumps.
- **`node_multiplexed`** — up to 12 chambers, multiplexed valves/sensors,
  optional shared pressure/vacuum tanks.
- **`node_magnet_sensor`** — 4-sensor magnet sensor (separate firmware, no chambers / pumps;
  streams sensor data only).

The gateway is a **transparent bridge** between the PC (USB serial) and the
nodes (ESP-NOW). It has no command semantics of its own — it only rewrites the
JSON envelope:

- **PC → Gateway**: payload with `target` (MAC of the destination node)
- **Gateway → Node**: same payload without `target`, sent over ESP-NOW
- **Node → Gateway**: any JSON payload
- **Gateway → PC**: same payload with `source` (sender MAC) added

The only messages the gateway emits on its own are the boot lines listed at
the bottom of this document.

---

## PC → Gateway → Node — commands

Each command is sent on the serial line as
`{"target":"<mac>", "cmd":"<name>", ...}`.

### Common to both node types

| `cmd` | Fields | Notes |
|---|---|---|
| `ping` | — | `target:"FF:FF:FF:FF:FF:FF"` does a broadcast scan |
| `inflate` | `chamber`, `delta` (0–100 %) | |
| `deflate` | `chamber`, `delta` (0–100 %) | |
| `set_pressure` | `chamber`, `value` (0–100 %) | |
| `set_max_pressure` | `chamber`, `value` (kPa) | Stored on the chamber until reboot |
| `set_min_pressure` | `chamber`, `value` (kPa) | |
| `hold` | `chamber` | Closes valves, freezes the state machine |
| `debug` | — | Debug build only; reply: `{type:"debug",…}` |

Tank targets are part of `configure` (multiplexed only) — there is no separate
runtime `set_tank_pressure` command; re-send `configure` to change them.

### Direct-node only

| `cmd` | Fields | Notes |
|---|---|---|
| `set_led` | `color` ("#RRGGBB"), `pattern` ("off"/"solid"/"blink"/"pulse"), `period_ms`, `count`, `index` | WS2812 ring. `index` sets a single pixel (solid); omit it for the whole ring. `period_ms`/`count` apply to blink/pulse (count ≤ 0 = forever). |

### Sensor / magnet sensor-node only

| `cmd` | Fields | Notes |
|---|---|---|
| `rebaseline` | — | Re-zero (recapture the baseline of) all magnetic sensors |
| `configure` | `fullscale_mt`, `act_threshold` | Tune `adj` normalisation scale + activation level (both optional) |

### Multiplexed-node only

#### `configure`

Required before any other command. Without it, the node replies
`{type:"error", reason:"not_configured"}`. Re-send to change any field at
runtime (e.g. raise the pressure target mid-session).

Fields:

- `num_chambers`
- `pump_inflate_count`
- `pump_deflate_count`
- `tank_pressure_min_kpa` — lower safety bound of the pressure tank (kPa)
- `tank_pressure_max_kpa` — upper safety bound of the pressure tank (kPa)
- `tank_pressure_target_kpa` — operational set-point for the pressure tank (kPa, clamped into `[min, max]`)
- `tank_vacuum_min_kpa` — deepest allowed vacuum (most negative kPa)
- `tank_vacuum_max_kpa` — shallowest vacuum (least negative kPa, safety stop)
- `tank_vacuum_target_kpa` — operational set-point for the vacuum tank (kPa, typically negative, clamped into `[min, max]`)
- `pump_groups` — `{pressure:[i,…], vacuum:[i,…]}`, indices 1..6 of `PUMP1..PUMP6`
- `organ_channels` — `[c, …]` mux channels carrying organ+cover circuits; the
  index in this list becomes the `slot` in the node's `organ` broadcasts. Wire
  them to the highest channels (I13..I15) so the chamber autodetect (which
  claims low channels first) doesn't collide. Up to 4. Applying this also
  scrubs those channels from any autodetected chamber/tank assignment.

All tank fields are optional in the payload — omitted ones keep their current
firmware state. Targets default to 0.0 until set; the Python launcher fills
them with the YAML value or `(min + max) / 2` if absent.

---

## Node → Gateway → PC — replies and broadcasts

Each message arrives on the PC with a `source` field added by the gateway.

### Common to both node types

| `type` / `status` | Fields | When |
|---|---|---|
| `status:"node_*_ready"` | — | Once at boot, ESP-NOW broadcast to `FF:FF:FF:FF:FF:FF` |
| `status` | `chamber`, `pressure` (0–100 %) | Every 500 ms, one per chamber |
| `pong` | — | Reply to `ping` |
| `debug` | (per-node — see below) | Reply to `debug`, debug build only |

The boot announce is `{"status":"node_<type>_ready"}` (e.g.,
`node_direct_ready`, `node_multiplexed_ready`, `node_magnet_sensor_ready`). It is
broadcast on the ESP-NOW channel so the gateway can forward it before the
node knows the gateway's MAC.

### `organ` — organ network + silicone cover state (direct + multiplexed)

```json
{"type":"organ", "resistance_ohm": 952.4, "open": false}          // direct
{"type":"organ", "slot": 0, "resistance_ohm": 952.4, "open": false}  // multiplexed
```

An ADC line measures the parallel resistance of all plugged-in organs of one
circuit; the silicone cover closes the circuit's return path, so an open
circuit means the cover is off (`open: true`, `resistance_ohm: -1`). Sent on
change (±25 Ω hysteresis, 3-sample debounce on the open/closed flip — the
cover rests by gravity) and re-sent every 2 s as a heartbeat.

- **Direct node**: a single circuit on `ORGAN_SENSE_PIN` (IO36). No `slot`
  field (treated as `slot 0`).
- **Multiplexed node**: one circuit per `organ_channels` entry (see
  `configure`); `slot` = index in that list. Lets one node serve several
  independent patients (e.g. one per Tree branch).

On the PC, `ESP32Controller.on_organ(cb)` delivers `(resistance_ohm, slot)`
(`inf` when open); `src/hardware/organ_sensor.py` follows one slot and splits
it into cover / resistance event streams for activities.

### Multiplexed-node only

| `type` | Fields | When |
|---|---|---|
| `tank_status` | `kind`, `pressure` (0–100 %) | Every 500 ms, one per tank |
| `error` | `reason` | On error (see list below) |

`error.reason` values: `pca9685_address_conflict`, `not_configured`.

### magnet sensor-node only

#### `magnet` — live sample (every reading)

| Field | Shape |
|---|---|
| `raw` | `[[x,y,z], …]` — N entries, one per sensor |
| `mag` | `[m1, …]` — N magnitudes |
| `adj` | `[a1, …]` — N baseline-adjusted values |

The PC decides what's touched based on the skin's configured layout
(`skin.touch.sensor_grid` paired with `imu_geometry`). The firmware does **not**
emit `predicted_quadrant` / `active_quadrants` — those are computed on the PC.

Register `controller.on_imu(cb)` to receive each message (the gateway adds
`"source":"<MAC>"`).

> **Sizing**: stay under ~230 bytes total (ESP-NOW packet limit 250). Drop
> `device_id` and `ts_ms` — `source` is added by the gateway and the PC
> stamps on receipt.

#### Boot announce (self-describing)

The magnet sensor firmware broadcasts its configuration once at the end of `setup()`,
so the PC can adapt to different sensor / magnet variants without per-board
knowledge:

```json
{"status":"node_magnet_sensor_ready",
 "sensors": N,
 "magnets": M,
 "variant": "label",
 "geometry": {"sensors":[[x,y], …], "magnets":[[x,y], …]}}
```

Coordinates can be normalised `[0, 1]` or in mm — document which on the
firmware side. `ESP32Controller` caches the payload on receipt; read it
later via `controller.imu_geometry`.

#### Linking a skin to an magnet sensor node

A skin's YAML may opt into touch sensing by adding a `touch` block referencing
the magnet sensor node's MAC:

```yaml
skins:
  - skin_id: belly
    chambers: [...]
    grid: {cols: 8, rows: 4}
    chamber_grid: [[...rows of chamber-index-or-(-1)...]]
    touch:
      node_mac: "BB:CC:DD:EE:FF:00"
      sensor_count: 4
      sensor_grid: [[...rows of sensor-index-or-(-1)...]]
```

Skins without a `touch` block remain pure pneumatic (the existing case);
nothing else changes.

### `debug` reply payloads

**node_direct** (debug build only):

- `num_chambers`
- `ch[]` — array, one entry per chamber, each with
  `s` (state code), `kpa`, `tgt` (target kPa), `min`, `max`
- `tx_ok`, `tx_fail` — ESP-NOW send counters
- `drop` — commands dropped by the queue
- `up` — uptime in seconds

**node_multiplexed** (debug build only):

- `ready`, `configured` — booleans
- `num_chambers`
- `p_tank`, `v_tank` — mux channel indices assigned by autodetect

### Non-JSON node output

If a node sends a payload that is not valid JSON, the gateway forwards it
wrapped as `{"source":"<mac>", "raw":"<bytes>"}`.

### Local Serial output (NOT forwarded by gateway)

Printed on the node's USB Serial only — only visible if you connect a USB
cable to the node and open `pio device monitor`.

| Payload | Node | Build |
|---|---|---|
| `{"status":"node_*_ready"}` | both | Always (mirrors the ESP-NOW broadcast) |
| `{"error":"esp_now_init_failed"}` | both | Always, if `esp_now_init()` fails |
| `TODO: …` autodetect lines | node_multiplexed | Always |
| `VALVE …` / `PUMPS …` / `RX …` lines | both | Debug only |

---

## Gateway internal messages

Emitted on the serial line by the gateway itself, no `source` field.

| Payload | When |
|---|---|
| `{"status":"gateway_ready", "mac":"<own_mac>"}` | Boot succeeded |
| `{"error":"esp_now_init_failed"}` | `esp_now_init()` failed in `setup()` |

---

## Source files

- Shared ESP-NOW/MAC/radio layer — [`firmware/common/se_espnow.h`](common/se_espnow.h)
- Gateway dispatch — [`firmware/gateway/src/main.cpp`](gateway/src/main.cpp)
- node_direct command parser/handler — [`firmware/node_actuator/src/direct/commands.h`](node_actuator/src/direct/commands.h)
- node_multiplexed command parser — [`firmware/node_actuator/src/multiplexed/main.cpp`](node_actuator/src/multiplexed/main.cpp) (`parseAndQueue`, `processCommand`)
- node_magnet_sensor / touch board (MLX90393) — [`firmware/node_magnet_sensor/src/main.cpp`](node_magnet_sensor/src/main.cpp)
