# node_actuator Firmware

Pneumatic actuator boards. One PlatformIO project hosts **both** board variants,
selected at build time via `build_src_filter`:

| Variant | Sources | `node_type` | Description |
|---------|---------|-------------|-------------|
| `direct` | `src/direct/` | `node_direct` | 3 chambers, GPIO valves via ULN2803A, onboard pumps via DRV3297 |
| `multiplexed` | `src/multiplexed/` | `node_multiplexed` | Up to 12 chambers, muxed valves/sensors (2× PCA9685 + 74HC4067), optional shared reservoir tanks + pumps |

Each variant has a release env and a `*_debug` env (adds Serial logs + the
`debug` command, `-DDEBUG_BUILD`).

## Build & flash

```bash
cd firmware/node_actuator
pio run -e direct             --target upload   # node_direct, release
pio run -e direct_debug       --target upload   # node_direct, debug
pio run -e multiplexed        --target upload   # node_multiplexed, release
pio run -e multiplexed_debug  --target upload   # node_multiplexed, debug
```

`pio run` (no `-e`) builds all four.

## Shared code

ESP-NOW/MAC/radio plumbing and the common helpers come from `firmware/common`
(added to the include path in `platformio.ini`):

- `se_espnow.h` — ESP-NOW init, peers, send, gateway-MAC tracking (also used by
  the gateway and node_sensor).
- `units.h`, `pressure.h`, `dbg.h`, `cmd_queue.h` — shared by both variants.

Variant-specific modules (`pins.h`, `chambers.h`, `commands.h`, `mux.h`,
`pca_valves.h`, `pumps.h`, `config.h`) live under each `src/<variant>/` folder.

See [../PROTOCOL.md](../PROTOCOL.md) for the ESP-NOW command/status protocol.
