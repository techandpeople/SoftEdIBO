# SoftEdIBO ESP-NOW Protocol

Three firmware flavors share this protocol:

- **`node_direct`** - 3 chambers, GPIO valves, onboard pumps (optionally folds
  in the magnet/touch module, auto-detected).
- **`node_multiplexed`** - up to 12 chambers, multiplexed valves/sensors,
  shared pump banks (no reservoir tanks in the current build - pumps push
  straight into the chambers).
- **`node_magnet_sensor`** - 4x MLX90393 magnet/touch board (+1 optional 5th
  sensor; separate firmware, no chambers / pumps; streams sensor data only).

The gateway is mostly a **transparent bridge** between the PC (USB serial) and
the nodes (ESP-NOW) - it rewrites the JSON envelope and forwards the payload
unchanged:

- **PC -> Gateway**: payload with `target` (MAC of the destination node)
- **Gateway -> Node**: same payload without `target`, sent over ESP-NOW
  (paced on TX-done via `se::sendPaced`, so command bursts don't overrun the
  radio TX queue; there is still no retransmit)
- **Node -> Gateway**: any JSON payload
- **Gateway -> PC**: same payload with `source` (sender MAC) added

Two envelopes are NOT relayed to ESP-NOW nodes:

- **No `target`** - the command is for the gateway itself (`get_ap`, `set_ap`,
  `ota_store_*`); see [`gateway/README.md`](gateway/README.md).
- **`target:"thymio"`** - forwarded over UART to the companion ESP32-C6 radio
  co-processor that speaks 802.15.4 to Thymio robots (`thymio_*` commands);
  its replies come back tagged `"source":"thymio"`. See
  [`../docs/THYMIO_WIRELESS_CONTROL.md`](../docs/THYMIO_WIRELESS_CONTROL.md).

The only messages the gateway emits on its own are the gateway-local replies
above and the lines listed at the bottom of this document.

---

## PC -> Gateway -> Node - commands

Each command is sent on the serial line as
`{"target":"<mac>", "cmd":"<name>", ...}`.

### Common to both actuator node types

`chamber: -1` fans an actuation command out to **every** chamber in one frame
(the PC's Inflate/Deflate-All), so a single dropped per-chamber frame can't
leave part of the skin un-actuated. Limit commands (`set_max_pressure` /
`set_min_pressure`) stay single-target.

Fills are driven by the shared **coupled-fill engine**
([`common/coupled_fill.h`](common/coupled_fill.h)): co-active same-direction
chambers open together, each closes progressively the moment the shared line
reaches its target, then one settle + isolated verify round. Safety is
time-based: per-chamber open cap 5 s (overridable per request via `ms`),
per-round 6 s / whole-sequence 25 s on the direct board and 8 s / 45 s on the
multiplexed board (`direct/chambers.h`, `multiplexed/chambers.h`), plus a 10 s
actuation watchdog.

`chamber: -1` on `inflate`/`deflate` carries the request's `ms`/`duty`/`timed`
to every chamber on both boards. A chamber index outside the board's valid
range (`0..2` on the direct board, `0..num_chambers-1` on the multiplexed
board) is rejected - it no longer wraps to `-1` (all); a rejected
`set_max_pressure`/`set_min_pressure` that carried a `seq` is NACKed with
`err:"bad_chamber"`, anything else is dropped silently.

**While emergency-stopped** (`stop` received, no `resume` yet) both boards drop
every actuation command (`inflate`, `deflate`, `set_pressure`, `hold`,
`hold_duty`, `valve_manual`, `pump_manual`, `vent`, `test_run`), so nothing is
queued to run on re-arm. `set_max_pressure`/`set_min_pressure` still apply
(and ack) because they only store a limit. `ping`, `tare`, LED commands,
`configure` (multiplexed setup / magnet tuning), `rebaseline` and the direct
board's `test_stop`/`status_rate` stay honoured.

| `cmd` | Fields | Notes |
|---|---|---|
| `ping` | - | `target:"FF:FF:FF:FF:FF:FF"` does a broadcast scan; reply carries `rgbw` + `kpa_min` |
| `inflate` | `chamber` (-1 = all), `delta` (0-100 %), `ms`?, `duty`?, `timed`? | Target = current + `delta` % of the [min, max] range, clamped to max; no-op if already at/above target. `ms>0` bounds that chamber's total valve-open time - the closing authority for a target the gauge can't see; 0/omitted keeps the engine's 5 s cap. `timed:1` = the board has NO pressure sensor populated: run fully open-loop on `ms` (required) and ignore the floating gauge entirely (no below-target guard, no cutoff, no measure) |
| `deflate` | `chamber` (-1 = all), `delta` (0-100 %), `ms`?, `duty`?, `timed`? | Mirror of `inflate` (target clamped to min). A target below the sensor floor (`kpa_min`) can only close on `ms` (calibrated deflate curve) or the engine's time cap; once the chamber's reading has bottomed out at the floor the pull continues on time with the vacuum pump dropped to the shared 180 PWM floor (full again the moment any open deflate valve is still visible / opened by hand). `timed:1` as on `inflate` - without it a sensorless board's below-target guard drops the deflate outright (a blind chamber never reduces: it stays at full duty for its `ms`) |
| `set_pressure` | `chamber` (-1 = all), `value` (0-100 %), `duty`? | Routes through the engine as an inflate or deflate to the absolute target |
| `set_max_pressure` | `chamber`, `value` (kPa), `seq`? | Stored on the chamber until reboot. With an optional `seq` the node confirms it (see `ack` below) so the PC can retransmit a dropped safety limit instead of clamping to a stale ceiling |
| `set_min_pressure` | `chamber`, `value` (kPa), `seq`? | Same confirmed-delivery option as `set_max_pressure` |
| `hold` | `chamber` (-1 = all) | Closes valves, drops the chamber from both engines (and from any `hold_duty`) |
| `hold_duty` | `chamber` (-1 = all), `duty` (0 or 70-255), `kpa`?, `dir`?, `timed`?, `off`? | Leak-compensating regulated hold on the chamber's own gauge, from the losses it measures in real time. `dir` 0 (default) = pressure hold: INFLATE valve + pressure pump; `dir` 1 = vacuum hold: the mirror with the DEFLATE valve and the vacuum pump. CLOSED FIRST: the hold starts with the valve closed measuring the chamber's closed-valve loss (kPa/s); a tight chamber stays closed and unpumped; only a chamber that has drifted 0.25 kPa below its target opens (a fresh hold waits up to 600 ms for its first loss window first). CONTINUOUS DUTY: once open the valve STAYS open and the shared pump PWM is the regulator, `ff + kp * predicted deficit +- 4 dither`, servoed down to the 70 PWM RUN floor (`pump_duty::RUN_MIN`) - not the 180 START floor (`pump_duty::MIN`), which only the KICK on every pump start uses (80 ms, then a 2 PWM/ms descent). `ff` is the predicted equilibrium duty: a least-squares line `slope = a*duty + b` over the last 16 windows of 200 ms (mean duty, reading slope) gives the pump gain `a` and the open-line loss `b`; `ff = -b/a` is where delivery balances loss (trusted with >= 4 PWM^2 of duty spread and a positive gain; the dither keeps it identifiable at equilibrium; `kp = 2 s^-1 / a` clamped 5..30, default 15 PWM/kPa). Until trusted, `ff` walks by a bounded integral of the predicted deficit (40 PWM/s, <= 12 per window), primed by `duty` when given, else predicted from the measured closed loss (run floor + 20 PWM per kPa/s). The deficit acted on is the reading extrapolated 80 ms ahead. A running pump that stops delivering under the start floor (reading falls 600 ms while air is needed) raises the side's learned run floor by 12 and re-kicks. THE ONLY PULSE LEFT: the valve closes (2-tick debounce, 160 ms minimum open) only when the pump sits on the run floor (predicted `ff` there, or the command there 400 ms with the reading not falling) and the chamber is at / heading past its target - or when it is 1 kPa past it with the command on the floor. A leaky chamber then re-opens 0.25 kPa below target at the run floor duty (the physical limit of a pump that cannot trickle slower); a tight one stays closed. One target level per line: chambers whose targets differ by more than 0.5 kPa never share it - a chamber at another level waits while the line's chambers still need air and gets the line once they sit at their targets (a starving line admits everyone). A vacuum target below the gauge floor (blind 0..100 sensor) is held by timed re-pulls instead: 120 ms at `duty` (>= 180) every 2.5 s, plus an immediate pull whenever the chamber leaks up into the visible band. `duty` 0 = no seed (the node predicts its own); otherwise it seeds the servo (or is the re-pull duty). `timed:1` (or no `kpa`) keeps the valve open at `duty` (sensorless boards). Pumps off whenever no held valve of their side is open. PC must re-send ~2 s as keepalive (6 s dead-man drops the hold). `off:1` drops it. Any inflate/deflate/set_pressure/hold on the chamber supersedes it; fill engines/manual/bench runs suspend it and it resumes after |
| `stop` | - | Emergency stop: latch every pump off + valve closed; actuation commands are dropped until `resume` (limits still apply - see above) |
| `resume` | - | Re-arm after `stop` |
| `tare` | - | Zero the pressure sensors: capture each chamber's current reading (8-sample average) as its ambient zero and persist it in NVS. Non-actuating, honoured while stopped. The PC vents every chamber first (`vent`, Skin Config -> Zero Sensors). The direct board acks it (`{"type":"ack","cmd":"tare"}`); the multiplexed board does not, and bypasses the `configure` requirement for it |
| `valve_manual` | `chamber`, `side` (0 = inflate, 1 = deflate), `open` (0/1) | Dev/bench override, bypasses the engine; 5 s dead-man auto-off + hard-limit cutoff |
| `pump_manual` | `pump` (0 = inflate/pressure, 1 = deflate/vacuum), `on` (0/1) | Dev/bench override, same safety nets |
| `vent` | `chamber` (-1 = all), `open` (0/1) | Bench vent: BOTH valves of the chamber open with the pumps kept off it (excluded from the pump recalc), so it equalises to atmosphere - neutralises an inflated or vacuumed chamber and drops any `hold_duty` on it. Manual-override rules: 5 s dead-man (PC keeps it alive), a `valve_manual` open on either side or any actuation ends it |
| `set_led` | `color` ("#RRGGBB"), `pattern` ("off"/"solid"/"blink"/"pulse"/"comet"/"fade"), `color2`?, `period_ms`, `count`, `fade_ms`?, `angle`?, `index`?, `ring`? | WS2812/SK6812 ring(s). `index` sets a single pixel; omit it for the whole ring. `period_ms`/`count` apply to blink/pulse/comet/fade (count <= 0 = forever). `"fade"` cross-fades `color` <-> `color2` (default black) on the node, one full cycle per `period_ms`. Every change cross-fades over `fade_ms` (default 250 ms; 0 snaps). `angle` (0-360 deg) rotates the comet start. `ring` (0..2) selects one of the multiplexed board's three rings; omit/-1 = all (ignored by the direct board) |
| `set_led_halves` | `colors` (["#RRGGBB", ...], up to 8), `pattern`, `period_ms`, `count`, `fade_ms`?, `angle`?, `ring`? | Splits the ring into `len(colors)` equal contiguous arcs in ONE frame (e.g. half purple / half yellow), rendered from loop(). `pattern`/`period_ms` animate the whole split together; `"comet"` paints one comet per colour. `angle` rotates the split. Prefer this over a burst of per-pixel `set_led` frames - those call `strip.show()` once per pixel in the receive task and reset the node |
| `debug` | - | Debug build only; reply: `{type:"debug",...}` |

`duty` (1-255) is an optional pump PWM value: it is parsed and stored, but the
shared-manifold pump recalc currently runs the pumps at full duty - only the
bench `test_run` honors it (duty-curve calibration sweeps).

### Direct-node only

| `cmd` | Fields | Notes |
|---|---|---|
| `test_run` | `dir` (0 = inflate, 1 = deflate), `chamber`? (-1 = all), `duty`? | Continuous bench run: latches one pump + its valve(s) open, ignoring pressure. Re-send ~1 Hz as keepalive - a 3 s dead-man stops the run if the link drops |
| `test_stop` | - | End the bench run (honoured even while `stop`ped) |
| `status_rate` | `ms`, `ttl` | Fast telemetry: lower the `status` cadence to `ms` (floor 20 ms) for a `ttl`-bounded window; auto-reverts to 500 ms when the ttl lapses. `ms<=0` / `ttl<=0` reverts immediately |
| `rebaseline` / `configure` | (see magnet table below) | The direct board folds the magnet/touch module in (auto-detected; no-op when no sensors are wired) |

### Magnet/touch commands (node_magnet_sensor + direct board's folded-in module)

| `cmd` | Fields | Notes |
|---|---|---|
| `rebaseline` | - | Re-zero (recapture the baseline of) all magnetic sensors |
| `configure` | `act_threshold_ut`, `adaptive_baseline`, `baseline_tau_ms`, `stream_vec`, `osr`, `filter` | Set the uT activation threshold; opt-in adaptive baseline (tracks slow drift, frozen per-sensor while active); `stream_vec` toggles the 3-axis `vec` rows in the stream (RAM-only - re-send after a node reboot). `osr` (0-3) / `filter` (0-7) set the MLX90393 oversampling / digital filter (Adafruit enum indices; applied from the main loop, then the baseline is re-captured - keep hands off the skin). All optional. Legacy `fullscale_mt`/`act_threshold` (fraction) still accepted |

### Multiplexed-node only

#### `configure`

Required before any actuation command (`ping`, `stop`/`resume`, `debug`,
`tare` and `hold_duty` work regardless). Without it, the node replies
`{type:"error", reason:"not_configured"}`. Re-send to change any field at
runtime.

Fields (all optional - omitted ones keep their current firmware state):

- `num_chambers`
- `pump_inflate_count`
- `pump_deflate_count`
- `pump_groups` - `{pressure:[i,...], vacuum:[i,...]}`, indices 1..6 of
  `PUMP1..PUMP6`; overrides the boot default (first half pressure, second half
  vacuum). Wins over the count fields when both are present.
- `organ_channels` - `[c, ...]` mux channels carrying organ+cover circuits; the
  index in this list becomes the `slot` in the node's `organ` broadcasts. Wire
  them to the highest channels (I13..I15) so the chamber autodetect (which
  claims low channels first) doesn't collide. Up to 4. Applying this also
  scrubs those channels from any autodetected chamber/tank assignment.
- `tank_pressure_min/max/target_kpa`, `tank_vacuum_min/max/target_kpa` -
  legacy reservoir-tank bounds. Still parsed and stored, but the current
  no-reservoir build has no tank sensors and nothing consumes them (the pumps
  push straight into the chambers); the PC no longer sends them.

---

## Node -> Gateway -> PC - replies and broadcasts

Each message arrives on the PC with a `source` field added by the gateway.

### Common to both actuator node types

| `type` / `status` | Fields | When |
|---|---|---|
| `status:"node_*_ready"` | `fw`, `rgbw`, `kpa_min` | Once at boot, ESP-NOW broadcast to `FF:FF:FF:FF:FF:FF` |
| `status` | `kpa[]` (kPa, 0.1 resolution), `st[]` (actuation: 0 idle, 1 inflating, 2 deflating), `vi[]`/`vd[]` (actual inflate/deflate valve output, 0/1) - parallel arrays indexed by chamber | ONE batched frame per node every 500 ms covering all chambers (3 on the direct board, `num_chambers` on the multiplexed board). No `pressure` % - the PC recomputes it from `kpa` against the configured range. The direct board also emits one the instant a chamber's state or valve output changes, sends faster during a `status_rate` window, and keeps reporting while stopped. The multiplexed board refreshes idle chamber pressure every `PRESSURE_CHECK_MS` (200 ms) so `kpa` stays live, but sends **no** status while emergency-stopped, unconfigured, not ready or in the PCA error state |
| `pong` | `rgbw`, `kpa_min` | Reply to `ping` |
| `ack` | `cmd`, `seq`?, `chamber`?, `ok`?, `err`? | Confirms a command was **applied**. **Both boards** ack `set_max_pressure`/`set_min_pressure` when the PC tagged them with a `seq`, echoing `seq`+`chamber`+`ok` (`ok:false`+`err`, e.g. `"bad_chamber"`, is a NACK) so the PC can retransmit a dropped safety limit - see [`../docs/ACK_RELIABILITY.md`](../docs/ACK_RELIABILITY.md). The direct board additionally acks `stop`/`resume`/`tare`/`test_run`/`test_stop`/`status_rate` (no `seq`) |
| `pumps` | `inf`, `def` (live pump PWM duty, 0-255) | Direct board only: with every status batch + the instant a duty changes |
| `seq` | `inf_ph`/`inf_mask`, `def_ph`/`def_mask`, `ch[]` (`k`, `mx`) | Direct board only: engine-phase diagnostic after an Inflate/Deflate-All |
| `dbg` | `ev` (`"rx"`/`"eng"`/`"dry"`), ... | Debug build only: valve-state-at-command, engine round/measure trace, dry-pump warnings |
| `debug` | (per-node - see below) | Reply to `debug`, debug build only |

The boot announce is `{"status":"node_<type>_ready", ...}` (e.g.,
`node_direct_ready`, `node_multiplexed_ready`, `node_magnet_sensor_ready`). It
is broadcast on the ESP-NOW channel so the gateway can forward it before the
node knows the gateway's MAC. Actuator boards add:

- `fw` - build marker string, to confirm from the PC log which firmware
  actually booted;
- `rgbw` - whether the LED ring build is RGBW (`-DLED_RGBW`), so the OTA
  picker auto-selects the right bin;
- `kpa_min` - the gauge floor (lowest pressure the sensor can see; build flag
  `SENSOR_KPA_MIN`), so the PC knows when a deflate needs a time budget.
  `rgbw`/`kpa_min` are repeated in every `pong` for PCs that missed the boot.

### `organ` - organ network + silicone cover state (direct + multiplexed)

```json
{"type":"organ", "resistance_ohm": 952.4, "open": false}          // direct
{"type":"organ", "slot": 0, "resistance_ohm": 952.4, "open": false}  // multiplexed
```

An ADC line measures the parallel resistance of all plugged-in organs of one
circuit; the silicone cover closes the circuit's return path, so an open
circuit means the cover is off (`open: true`, `resistance_ohm: -1`). Sent on
change (+/-25 ohm hysteresis, 3-sample debounce on the open/closed flip - the
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
| `error` | `reason` | On error (see list below) |

`error.reason` values: `pca9685_address_conflict`, `not_configured`.

(The old `tank_status` broadcast is gone - the current no-reservoir build has
no tank sensors.)

### magnet sensor-node only

#### `magnet` - live sample (every reading)

| Field | Shape |
|---|---|
| `mag` | `[m1, ...]` - N per-sensor magnitudes (uT), baseline-subtracted |
| `act` | `[idx, ...]` - indices of sensors whose `mag >= act_threshold_ut` |
| `vec` | `[[dx,dy,dz], ...]` - per-sensor 3-axis deltas in uT: one decimal (0.1 uT) for components below 100 uT, whole uT above; only when 3-axis streaming is on (`MAG_VECTOR` build or `configure stream_vec`). The announce then carries `"vec":1` |

Cadence: ~28 Hz (35 ms) on the standalone board; 100 ms (~10 Hz) on the direct
actuator board, so the stream can't crowd out actuation commands. The magnet
module is shared: `firmware/common/se_magnet.h`, used by both the standalone
`node_magnet_sensor` board and the direct actuator board (which folds it in,
auto-detected).

The PC decides what's touched based on the skin's configured layout
(`skin.touch.sensor_grid` paired with `magnet_geometry`). The firmware does
**not** emit `predicted_quadrant` / `active_quadrants` - those are computed on
the PC.

Register `controller.on_magnet(cb)` to receive each message (the gateway adds
`"source":"<MAC>"`).

> **Sizing**: stay under ~230 bytes total (ESP-NOW packet limit 250). Drop
> `device_id` and `ts_ms` - `source` is added by the gateway and the PC
> stamps on receipt.

#### Boot announce (self-describing)

The magnet sensor firmware broadcasts its configuration at the end of
`setup()` (and re-broadcasts every 2 s until the gateway is known, so a
late-connecting PC still captures it):

```json
{"status":"node_magnet_sensor_ready", "sensors": 4, "variant": "mlx90393", "fw": "magvec-1"}
```

`fw` is the magnet module's build marker; `"vec":1` is added when 3-axis
streaming is on (from boot on the `vector` env / `-DMAG_VECTOR` build, or after
`configure stream_vec:true`). A `ping` to the standalone board gets a bare
`{"type":"pong"}` (no `rgbw`/`kpa_min` - it has no LEDs or chambers). `ESP32Controller` caches
the payload on receipt (including the optional `magnets`/`geometry` keys,
which the current firmware does not emit yet); read it later via
`controller.magnet_geometry`.

#### Linking a skin to a magnet sensor node

A skin's YAML may opt into touch sensing by adding a `touch` block referencing
the magnet sensor node's MAC (see the annotated example in
`config/settings.yaml`):

```yaml
skins:
  - skin_id: belly
    chambers: [...]
    grid: {cols: 8, rows: 4}
    chamber_grid: [[...rows of chamber-index-or-(-1)...]]
    touch:
      node_mac: "BB:CC:DD:EE:FF:00"
      sensor_count: 4
      grid: {cols: 8, rows: 4}          # optional separate sensor-grid dims
      sensor_grid: [[...rows of sensor-index-or-(-1)...]]
      sensor_to_chamber: {"0": 0, "1": 0, "2": 1}   # optional touch routing
```

Skins without a `touch` block remain pure pneumatic (the existing case);
nothing else changes.

### `debug` reply payloads

**node_direct** (debug build only):

- `num_chambers`
- `ch[]` - array, one entry per chamber, each with
  `s` (state code), `kpa`, `tgt` (target kPa), `min`, `max`
- `tx_ok`, `tx_fail` - ESP-NOW send counters
- `drop` - commands dropped by the queue
- `up` - uptime in seconds

**node_multiplexed** (debug build only):

- `ready`, `configured` - booleans
- `num_chambers`
- `p_tank`, `v_tank` - mux channel indices assigned by autodetect

### OTA firmware update (all ESP-NOW nodes)

Every node includes the shared receiver [`common/se_ota.h`](common/se_ota.h);
the PC side is `src/hardware/node_ota_updater.py` (ESP-NOW) and
`src/hardware/wifi_ota_updater.py` (WiFi), driven from Tools -> Update Nodes
(OTA)... The gateway relays these unchanged.

| Direction | Payload | Notes |
|---|---|---|
| PC -> node | `{"cmd":"ota_begin","size":N,"md5":"<hex>","chunk":96}` | Starts an image; node replies `ota_ready` or `ota_error` (`begin_failed`) |
| PC -> node | `{"cmd":"ota_data","seq":S,"data":"<base64>"}` | One chunk, `seq` 0,1,2,... `CHUNK_SIZE` = 96 raw bytes (the gateway->node relay drops payloads over ~190 B). Node writes only the expected `seq`, re-acks older ones, silently drops future ones |
| PC -> node | `{"cmd":"ota_end"}` | Node verifies the MD5, arms an RTC "boot done" flag and reboots |
| PC -> node | `{"cmd":"ota_wifi","ssid":"...","pass":"...","url":"http://.../fw"}` | Fast path (S3 gateway with PSRAM): node leaves ESP-NOW, joins the gateway SoftAP and HTTP-pulls the image staged via `ota_store_*`. The gateway injects its own `ssid`/`pass` when absent |
| node -> PC | `{"type":"ota_ready"}` | Reply to `ota_begin` |
| node -> PC | `{"type":"ota_ack","seq":S}` | Chunk `S` written (or already held) |
| node -> PC | `{"type":"ota_wifi_start"}` | `ota_wifi` accepted; the node is going off the air |
| node -> PC | `{"type":"ota_done"}` | Broadcast 4x by the **new** firmware after it boots - the only success signal (both paths) |
| node -> PC | `{"type":"ota_error","reason":"..."}` | `begin_failed`, `not_active`, `bad_data`, `write_failed`, `verify_failed`, `bad_wifi_args` |

A failed WiFi join/download reboots the node back into its current firmware.
See [`gateway/README.md`](gateway/README.md) for the gateway-local
`ota_store_*` staging commands.

### Non-JSON node output

If a node sends a payload that is not valid JSON, the gateway forwards it
wrapped as `{"source":"<mac>", "raw":"<bytes>"}`.

### Local Serial output (NOT forwarded by gateway)

Printed on the node's USB Serial only - only visible if you connect a USB
cable to the node and open `pio device monitor`.

| Payload | Node | Build |
|---|---|---|
| `{"status":"node_*_ready"}` | both | Always (mirrors the ESP-NOW broadcast) |
| `{"error":"esp_now_init_failed"}` | both | Always, if `esp_now_init()` fails |
| `TODO: ...` autodetect lines | node_multiplexed | Always |
| `VALVE ...` / `PUMPS ...` / `RX ...` lines | both | Debug only |

---

## Gateway internal messages

Emitted on the serial line by the gateway itself, no `source` field.

| Payload | When |
|---|---|
| `{"status":"gateway_ready", "mac":"<own_mac>", "ap":"<ssid>"}` | Boot succeeded (`ap` on the SoftAP build) |
| `{"error":"esp_now_init_failed"}` | `esp_now_init()` failed at boot |
| `{"type":"error", "reason":"bad_cmd_json", "len":N, "raw":"..."}` | A PC line failed to parse (usually USB byte loss) - reported instead of silently dropped |

Gateway-local command replies (`ap_config`, `ap_set`, `ota_store_*`) are
documented in [`gateway/README.md`](gateway/README.md).

---

## Source files

- Shared ESP-NOW/MAC/radio layer - [`firmware/common/se_espnow.h`](common/se_espnow.h)
- Shared coupled-fill supervisor - [`firmware/common/coupled_fill.h`](common/coupled_fill.h)
- Shared magnet/touch module - [`firmware/common/se_magnet.h`](common/se_magnet.h)
- Shared OTA receiver (ESP-NOW + WiFi pull) - [`firmware/common/se_ota.h`](common/se_ota.h)
- Gateway dispatch - [`firmware/gateway/src/main.cpp`](gateway/src/main.cpp)
- node_direct command parser/handler - [`firmware/node_actuator/src/direct/commands.h`](node_actuator/src/direct/commands.h)
- node_multiplexed command parser - [`firmware/node_actuator/src/multiplexed/main.cpp`](node_actuator/src/multiplexed/main.cpp) (`parseAndQueue`, `processCommand`)
- node_magnet_sensor / touch board (MLX90393) - [`firmware/node_magnet_sensor/src/main.cpp`](node_magnet_sensor/src/main.cpp)
