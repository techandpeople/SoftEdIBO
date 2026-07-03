# `scripts/`

Developer/operator utilities for SoftEdIBO. Run them from the repo root with the project
venv active (they add the repo to `sys.path` themselves).

## Thymio wireless — control a Thymio over 802.15.4 (no dongle) 🎯

Full protocol + reproduction notes: **[docs/THYMIO_WIRELESS_CONTROL.md](../docs/THYMIO_WIRELESS_CONTROL.md)**
(see the "★ ACHIEVED: dongle-free control" section). Quick map:

| script | what it does |
|---|---|
| **`thymio_link.py`** | **the reliable dongle-free drive.** Turns on the C6's firmware `thymio_link` (the C6 polls the Thymio at 10 Hz on its own to hold the link hot) then sends instant `--left/--right/--stop/--led` or a `--repl` jog. **Several Thymios on one C6:** `--index` (0..3) + `--addr <hex>` per robot. No dongle at all. Needs a **U.FL-antenna C6** running the leak-fixed `rcp_c6`. |
| `thymio_move.py` | one-shot drive — only lands if the link is **already hot** (dongle driving, or a `thymio_link` running); otherwise the Thymio isn't listening. |
| `thymio_tx.py` | send a raw 802.15.4 frame hex (low-level replay/forge) via the C6's `tx` |
| `thymio_sniff_capture.py` | raw `esp_ieee802154` promiscuous sniffer via the C6 — `--scan` finds the channel, `--debug` prints frames live, `--ch N` locks one. (The **raw C6** sniffer sees the Thymio; the Sonoff/OpenThread one filters it out.) |
| `thymio_jog.py` | drive a Thymio via the **RF dongle** (thymiodirect) — the working-today path, and a traffic source to sniff |

**Several Thymios:** one C6 drives up to 4, each a **slot** (`--index`) addressed by its
802.15.4 short **`--addr`** (hex, e.g. `6a25`). Discover the addresses in-app (Robot Config →
Thymio → **Discover…** button, which sniffs and lists them) or with `thymio_sniff_capture.py`.
In the app, set each Thymio's **Address (C6)** so `wireless_via: gateway` robots get distinct
slots.

### 60-second quickstart (dongle-free)
```bash
# 1) flash a XIAO ESP32-C6 (WITH its U.FL antenna) with the RCP (sniff + tx):
pio run -d firmware/thymio_rcp -e rcp_c6 -t upload
ls /dev/serial/by-id/                     # note the C6 port (Espressif)

# 2) find the Thymio's 802.15.4 channel (drive it in one terminal, sniff in another):
python scripts/thymio_jog.py --drive 100 -100 --secs 180        # robot spins (via dongle)
python scripts/thymio_sniff_capture.py --no-drive --debug --gateway /dev/ttyACM<C6>
#   look for a frame whose data contains 8144 (PAN 0x4481) → its "ch" is the channel

# 3) drive it dongle-free, no dongle at all (was channel 25):
python scripts/thymio_link.py --ch 25 --gateway /dev/ttyACM<C6> --repl        # live jog: f/b/l/r/s
python scripts/thymio_link.py --ch 25 --gateway /dev/ttyACM<C6> --left 150 --right 150
python scripts/thymio_link.py --ch 25 --gateway /dev/ttyACM<C6> --stop
```
Prefer the stable `/dev/serial/by-id/...` path for `--gateway` — the `ttyACM*` numbers shift
when you plug/unplug other devices. If you re-pair the Thymio to a new network, only the
**channel** usually changes (set it with `--ch`); the PAN/addresses are read off the air and
rarely move.

## Other utilities
| script | purpose |
|---|---|
| `run.py` | launch the GUI app (sets Qt/GL env, then `python -m src.main`) |
| `build-firmware.sh` | build the bundled node/gateway firmware bins for OTA |
| `ota_c6_wifi.py` | one-command WiFi-OTA of the gateway's C6 (Thymio RCP) |
| `discover_nodes.py` | scan for ESP-NOW nodes via the gateway |
| `emergency-flash.sh` | cable-flash a bricked node through a second ESP as a serial bridge |
| `compile_ui.sh` | compile Qt Designer `.ui` files to `ui_*.py` |
| `fetch_blockly.sh` | vendor Blockly for the Behaviour Editor |
| `label_touches.py`, `train_touch_model.py` | touch-sensor dataset labelling + ML training |
