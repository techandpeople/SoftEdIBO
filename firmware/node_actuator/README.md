# node_actuator Firmware

Pneumatic actuator boards. One PlatformIO project hosts **both** board variants,
selected at build time via `build_src_filter`:

| Variant | Sources | `node_type` | Description |
|---------|---------|-------------|-------------|
| `direct` | `src/direct/` | `node_direct` | 3 chambers, GPIO valves via ULN2803A, onboard pumps via DRV3297; optional folded-in MLX90393 magnet/touch module (auto-detected) |
| `multiplexed` | `src/multiplexed/` | `node_multiplexed` | Up to 12 chambers, muxed valves/sensors (2x PCA9685 + 74HC4067), shared pump banks (no reservoir tanks in the current build), 4 LED rings |

Each variant has a release env, a `*_debug` env (adds Serial logs + the
`debug` command, `-DDEBUG_BUILD`), and `*_rgbw` / `*_rgbw_debug` envs for
SK6812 RGBW rings (`-DLED_RGBW` - the node self-reports `rgbw` in ready/pong
so the OTA picker selects the right bin).

## Build & flash

```bash
cd firmware/node_actuator
pio run -e direct             --target upload   # node_direct, release
pio run -e direct_debug       --target upload   # node_direct, debug
pio run -e multiplexed        --target upload   # node_multiplexed, release
pio run -e multiplexed_debug  --target upload   # node_multiplexed, debug
pio run -e direct_rgbw        --target upload   # RGBW-ring variants (also
pio run -e multiplexed_rgbw   --target upload   #  *_rgbw_debug)
```

`pio run` (no `-e`) builds all eight.

## Shared code

ESP-NOW/MAC/radio plumbing and the common helpers come from `firmware/common`
(added to the include path in `platformio.ini`):

- `se_espnow.h` - ESP-NOW init, peers, paced send, gateway-MAC tracking (also
  used by the gateway and node_magnet_sensor).
- `coupled_fill.h` - shared coupled-line fill supervisor (the engine both
  boards drive their fills through) so the two boards can't drift.
- `se_ota.h` - OTA receiver (ESP-NOW chunk stream + WiFi pull).
- `se_magnet.h` - MLX90393 magnet/touch module (direct board folds it in).
- `units.h`, `pressure.h`, `dbg.h`, `cmd_queue.h` - shared by both variants.

Variant-specific modules (`pins.h`, `chambers.h`, `commands.h`, `leds.h`,
`organ.h`, `magnet.h`, `mux.h`, `pca_valves.h`, `pumps.h`, `config.h`) live
under each `src/<variant>/` folder.

See [../PROTOCOL.md](../PROTOCOL.md) for the ESP-NOW command/status protocol.

## Fill / deflate safety

`inflate`/`deflate` take `{chamber, delta, ms, duty}` and route through the
shared coupled-fill engine (`coupled_fill.h`): co-active same-direction
chambers open together, each closes progressively at its own target, then one
settle + isolated verify. `ms>0` is that chamber's valve-open time budget -
the closing authority for a target the gauge can't see (a deflate below the
sensor floor, timed from the PC's calibrated deflate curve).

Safety is **time-based** (the gauge is unreliable and blind below its floor):
per-chamber cumulative open cap 5 s (`chamber_max_ms`, overridden per request
by `ms`), per-round cap 6 s, whole-sequence cap 25 s, a 10 s actuation
watchdog (`ACTUATION_TIMEOUT_MS`), and a 5 s dead-man on the manual/bench
paths. `HARD_MAX/MIN_KPA` (+/-100) is effectively uncapped - a single-sample
inflate cut kept only for a sane gauge. Full model:
[../../docs/PRESSURE_AND_FILL_SAFETY.md](../../docs/PRESSURE_AND_FILL_SAFETY.md).
