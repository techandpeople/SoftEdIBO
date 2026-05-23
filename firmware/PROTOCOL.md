# SoftEdIBO ESP-NOW Protocol

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

The boot announce is `{"status":"node_direct_ready"}` or
`{"status":"node_multiplexed_ready"}`. It is broadcast on the ESP-NOW channel
so the gateway can forward it before the node knows the gateway's MAC.

### Multiplexed-node only

| `type` | Fields | When |
|---|---|---|
| `tank_status` | `kind`, `pressure` (0–100 %) | Every 500 ms, one per tank |
| `error` | `reason` | On error (see list below) |

`error.reason` values: `pca9685_address_conflict`, `not_configured`.

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

- Gateway dispatch — [`firmware/gateway/src/main.cpp`](gateway/src/main.cpp)
- node_direct command parser/handler — [`firmware/node_direct/src/commands.h`](node_direct/src/commands.h)
- node_multiplexed command parser — [`firmware/node_multiplexed/src/main.cpp`](node_multiplexed/src/main.cpp) (`parseAndQueue`, `processCommand`)
