# `scripts/`

Developer/operator utilities for SoftEdIBO. Run them from the repo root with the project
venv active (they add the repo to `sys.path` themselves).

## Thymio wireless - control a Thymio over 802.15.4 (no dongle) 

Full protocol + reproduction notes: **[docs/THYMIO_WIRELESS_CONTROL.md](../docs/THYMIO_WIRELESS_CONTROL.md)**
(see the "* ACHIEVED: dongle-free control" section). Quick map:

| script | what it does |
|---|---|
| **`thymio_link.py`** | **the reliable dongle-free drive.** Turns on the C6's firmware `thymio_link` (the C6 polls the Thymio at 10 Hz on its own to hold the link hot) then sends instant `--left/--right/--stop/--led`, **`--sound ID`** (system sound 0-7) / **`--tone HZ DUR`**, or a `--repl` jog (`f/b/l/r/s`, `snd`, `tone`). **Several Thymios on one C6:** `--index` (0..3) + `--addr <hex>` per robot. No dongle at all; runs the leak-fixed `rcp_c6` (prefer a C6 with a proper U.FL antenna - the chip-antenna variant is flaky at range). |
| `thymio_move.py` | one-shot drive - only lands if the link is **already hot** (dongle driving, or a `thymio_link` running); otherwise the Thymio isn't listening. |
| `thymio_tx.py` | send a raw 802.15.4 frame hex (low-level replay/forge) via the C6's `tx` |
| `thymio_sniff_capture.py` | raw `esp_ieee802154` promiscuous sniffer via the C6 - `--scan` finds the channel, `--debug` prints frames live, `--ch N` locks one. (The **raw C6** sniffer sees the Thymio; the Sonoff/OpenThread one filters it out.) |
| `thymio_jog.py` | drive a Thymio via the **RF dongle** (thymiodirect) - the working-today path, and a traffic source to sniff |

**Sound:** `--sound 2` plays a built-in system sound (0-7, -1 stops); `--tone 700 30` plays a
700 Hz tone for 30/60 s. The C6 loads a tiny Aseba program (SET_BYTECODE + RUN) that calls the
`sound.*` native functions - motors keep their targets, so a driving robot beeps without
stopping. In the app / Python: `robot.play_sound(system=2)` or
`robot.play_sound(freq=700, duration_ms=500)`.

**Several Thymios:** one C6 drives up to 4, each a **slot** (`--index`) addressed by its
802.15.4 short **`--addr`** (hex, e.g. `6a25`). Discover the addresses in-app (Robot Config ->
Thymio -> **Discover...**: a dongle-free active scan - the C6 broadcasts LIST_NODES and every
powered robot on the network answers; power them on one at a time to map address -> robot,
listed in first-seen order), read a brand-new robot's address over its USB cable (Robot
Config -> Thymio -> **From cable...** - wireless Discover only sees robots already paired to
this network), or use `thymio_sniff_capture.py`. In the app, set each Thymio's
**Address (C6)** so `wireless_via: gateway` robots get distinct slots.

### 60-second quickstart (dongle-free)
```bash
# 1) flash a XIAO ESP32-C6 with the RCP (sniff + tx + sound; prefer the U.FL-antenna C6):
pio run -d firmware/thymio_rcp -e rcp_c6 -t upload
ls /dev/serial/by-id/                     # note the C6 port (Espressif)

# 2) find the Thymio's 802.15.4 channel (drive it in one terminal, sniff in another):
python scripts/thymio_jog.py --drive 100 -100 --secs 180        # robot spins (via dongle)
python scripts/thymio_sniff_capture.py --no-drive --debug --gateway /dev/ttyACM<C6>
#   look for a frame whose data contains 8144 (PAN 0x4481) -> its "ch" is the channel

# 3) drive it dongle-free, no dongle at all (was channel 25):
python scripts/thymio_link.py --ch 25 --gateway /dev/ttyACM<C6> --repl        # live jog: f/b/l/r/s
python scripts/thymio_link.py --ch 25 --gateway /dev/ttyACM<C6> --left 150 --right 150
python scripts/thymio_link.py --ch 25 --gateway /dev/ttyACM<C6> --stop
```
Prefer the stable `/dev/serial/by-id/...` path for `--gateway` - the `ttyACM*` numbers shift
when you plug/unplug other devices. If you re-pair the Thymio to a new network, only the
**channel** usually changes (set it with `--ch`); the PAN/addresses are read off the air and
rarely move.

## Other utilities
| script | purpose |
|---|---|
| `run.py` | launch the GUI app (sets the Qt env - `xcb` platform, shared GL contexts, WebEngine flags - then starts the app) |
| `build-firmware.sh` | build the bundled node/gateway firmware bins for OTA |
| `ota_c6_wifi.py` | one-command WiFi-OTA of the gateway's C6 (Thymio RCP) |
| `discover_nodes.py` | scan for ESP-NOW nodes via the gateway |
| `emergency-flash.sh` | cable-flash a bricked node through a second ESP as a serial bridge |
| `compile_ui.sh` | compile Qt Designer `.ui` files to `ui_*.py` |
| `fetch_blockly.sh` | vendor Blockly for the Behaviour Editor |
| `label_touches.py`, `train_touch_model.py` | touch-sensor dataset labelling + ML training |
