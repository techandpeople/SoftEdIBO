# SoftEdIBO

Soft-based robot platform for inclusive, embodied interaction.
Developed at [LASIGE](https://www.lasige.pt/), Faculdade de Ciencias, Universidade de Lisboa.

SoftEdIBO controls soft robots equipped with inflatable air chambers.
Participants interact by touching the robots, which respond through inflation and deflation.
The system supports multiple robot types (Turtle, Tree, Thymio) and activity modes.

> **Running a study?** See [SETUP.md](SETUP.md) for skin/sensor setup, magnet
> polarity, touch calibration, and the gesture-training workflow.

---

## Hardware requirements

| Component | Quantity | Notes |
|-----------|----------|-------|
| Seeed XIAO ESP32-S3 (gateway) | 1 | Connected to PC via USB; also runs a WiFi SoftAP (WiFi-OTA) + a UART link to a companion XIAO ESP32-C6 for dongle-free Thymio control |
| Seeed XIAO ESP32-C6 (Thymio RCP) | 0-1 | Optional — impersonates the Thymio RF dongle over 802.15.4 (drive wheels/LEDs/sound, no dongle). See [docs/THYMIO_WIRELESS_CONTROL.md](docs/THYMIO_WIRELESS_CONTROL.md) |
| ESP32-WROOM-32 (`node_direct`) | 1 per direct board | 3 chambers, direct ADC sensors, onboard pumps |
| ESP32-WROOM-32 (`node_multiplexed`) | 1 per multiplexed board | Up to 12 chambers (runtime configurable) |
| DRV3297 motor driver | 1 (`node_direct`) / up to 3 (`node_multiplexed`) | Drives pumps with PWM |
| Air pump | 2 (`node_direct`) / up to 6 (`node_multiplexed`) | Inflate/deflate supply — pumps push directly into the chambers |
| XGZP6847A pressure sensor | 1 per chamber | Analog output (0-3.3 V) |
| Solenoid valves | 2 per chamber | Inflate + deflate via ULN2803A |

Flash the [gateway firmware](firmware/gateway/) to the USB-connected board — one
build, a **Seeed XIAO ESP32-S3** (ESP-IDF), which does everything: ESP-NOW chamber
control + a WiFi SoftAP (for WiFi-OTA) + a UART link to a companion C6 for Thymios.
For each node, choose the matching firmware target:

| Firmware | Path | When to use |
|----------|------|-------------|
| `node_actuator` env `direct` / `direct_debug` | [firmware/node_actuator/](firmware/node_actuator/) | Direct board (fixed 3 chambers) |
| `node_actuator` env `multiplexed` / `multiplexed_debug` | [firmware/node_actuator/](firmware/node_actuator/) | Multiplexed board (default 12 chambers, runtime configurable) |
| `node_magnet_sensor` | [firmware/node_magnet_sensor/](firmware/node_magnet_sensor/) | 4× MLX90393 magnetic touch board (`node_magnet_sensor` protocol) |

---

## Architecture

```
PC --USB--> Gateway (XIAO ESP32-S3) --ESP-NOW--> node_direct      (3 chambers, direct GPIO valves + own pumps)
                                    │        └-> node_multiplexed (up to 12 chambers)
                                    └--UART--> C6 --802.15.4--> Thymio  (dongle-free wheels/LEDs/sound; optional)
```

**Software layers:**

```
SessionPanel
  +-- Activity (declarative behaviours run by ScriptedActivity)
        +-- Robot (Turtle / Tree / Thymio / Simulated)
              +-- Node(s)  (ESP32, identified by MAC + node_type + max_slots)
              +-- Skin(s)  (logical grouping of 1-3 chambers from one node of this robot)
                    +-- AirChamber  (local index 0-2, pressure 0-100 %)
```

- **Node** is a physical ESP32. Its `node_type` (`node_direct` or `node_multiplexed`) determines which firmware to flash.
- **Skin** groups 1-3 chambers, all on the same node (a skin never spans MACs). Activities address chambers by local skin index (0, 1, 2) — no knowledge of node topology required.
- **Pressure** is expressed as **0-100 %** of the maximum pressure configured on each node.
- **Per-chamber max pressure** is set in `settings.yaml` and enforced both in the app and on the ESP32 (hardware safety — survives app crashes).
- **Pressure sensing** uses the XGZP6847A datasheet transfer function (see [pressure.h](firmware/common/pressure.h)).

**Touch sensing (optional).** A skin may reference a `node_magnet_sensor` (4-sensor magnet sensor/touch board) via its `touch:` block — see [firmware/PROTOCOL.md](firmware/PROTOCOL.md). The activity-time view (`SkinGridView`) overlays a pulsing yellow outline on the active sensor cells so the operator sees where each touch lands relative to the chamber regions.

**Skin geometry by type.** Each skin sets a `skin_type` (e.g. `turtle_square`, `tree_round`, `thymio`) whose **shape and sensor coordinates** are hardcoded in [`src/hardware/skin_geometry.py`](src/hardware/skin_geometry.py). The skin dialog offers only the current robot's types and draws the real outline/aspect (square, rectangle, round, triangle, Thymio "D") — editor and activity view share the masks in [`src/gui/skin_shapes.py`](src/gui/skin_shapes.py). The legacy paint-grid editor still applies to skins without a `skin_type`.

**Sensor stream recording + touch-gesture ML.** A session can record every sensor message to `data/recordings/<id>.jsonl` (toggle in the setup dialog — no video). Those recordings, plus gestures tagged live in the observer panel, feed a **per-`skin_type`, coordinate-free** touch-gesture classifier (tap / press / stroke / squeeze). `scikit-learn` is the optional `ml` extra; the classifier is inert without a trained model. See [docs/TOUCH_ML.md](docs/TOUCH_ML.md).

**Activities are declarative behaviours.** No activities are hard-coded: every activity is a behaviour spec authored in the block editor (**Tools → Activity Editor…**), stored in the DB (or imported from JSON examples in `config/examples/behaviours/`) and interpreted at session time by `ScriptedActivity`. See [docs/ACTIVITIES.md](docs/ACTIVITIES.md).

---

## Installation

### Linux (x86-64)

```bash
curl -fsSL https://raw.githubusercontent.com/techandpeople/SoftEdIBO/master/install.sh | bash
```

This will download the latest release, install it to `~/.local/opt/SoftEdIBO/`, create a `softedibo` command in `~/.local/bin/`, and add a desktop entry to the application menu.

**Nightly build** (latest commit on `master`):

```bash
curl -fsSL https://raw.githubusercontent.com/techandpeople/SoftEdIBO/master/install.sh | bash -s -- --nightly
```

**Uninstall:**

```bash
softedibo --uninstall
```

> **First time with USB?** After installing, run:
> ```bash
> sudo usermod -aG dialout $USER
> ```
> Then log out and back in. The installer does this automatically if needed.

---

### Windows (x64)

1. Download **`SoftEdIBO-windows-x64.zip`** from the [latest release](https://github.com/techandpeople/SoftEdIBO/releases/latest)
2. Extract and run **`SoftEdIBO.exe`**

> **USB driver:** install the [CH340](https://www.wch-ic.com/downloads/CH341SER_EXE.html) or [CP210x](https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers) driver if your device is not detected.

---

## Usage

On first launch, a setup wizard guides you through flashing the firmware to the ESP32 nodes.

### Configuration (`config/settings.yaml`)

Robots are configured per type (`turtles:` / `trees:` / `thymios:`), each with its own `nodes:` (ESP32s by MAC) and `skins:` on top. A skin lists its chambers — each a `{mac, slot}` pair on a single node — with optional per-chamber `max_pressure` / `min_pressure` safety limits in kPa. It is all editable from the GUI (Robots panel); the commented examples in [config/settings.yaml](config/settings.yaml) show the full shape (skin types, touch blocks, Thymio entries).

```yaml
robots:
  turtles:
    - id: turtle_1
      nodes:
        - mac: "AA:BB:CC:DD:EE:01"
          node_type: "node_multiplexed"
          max_slots: 12
      skins:
        - skin_id: shell_top
          name: Shell Top
          skin_type: turtle_square
          chambers:
            - {mac: "AA:BB:CC:DD:EE:01", slot: 0, max_pressure: 8.0}
            - {mac: "AA:BB:CC:DD:EE:01", slot: 1, max_pressure: 6.0}
  trees: []
  thymios: []
```

- Multiple skins can share the same node MAC; each skin groups up to 3 of that node's slots.
- `max_pressure` is sent to the ESP32 node on startup as kPa. The gateway forwards it unchanged; the node enforces it independently — even if the app crashes, chambers will not exceed their configured limit.
- If `max_pressure` is omitted, chambers default to 8.0 kPa.

---

## Development

### Python application

```bash
# Qt platform libs (Debian/Ubuntu). Without libxcb-cursor0 the app aborts with
# "Could not load the Qt platform plugin xcb" — the app forces xcb (see run.py).
sudo apt-get install -y libgl1 libegl1 libglib2.0-0 libdbus-1-3 \
  libxcb1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
  libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 \
  libxcb-xkb1 libxkbcommon-x11-0 libfontconfig1

git clone https://github.com/techandpeople/SoftEdIBO.git
cd SoftEdIBO
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
./scripts/compile_ui.sh     # generates src/gui/ui_*.py — not in git
python scripts/run.py
```

Requires Python 3.12+. Re-run `./scripts/compile_ui.sh` after editing any
`src/gui/ui/*.ui` file, or the app fails with
`ModuleNotFoundError: No module named 'src.gui.ui_...'`.

**Debug mode** — shows all log levels on the console (DEBUG+):

```bash
python scripts/run.py --debug
```

Without `--debug`, only warnings and errors are shown on the console. All log
levels are always written to `softedibo.log` in the per-user state directory
(`~/.local/state/SoftEdIBO/` on Linux, `%LOCALAPPDATA%\SoftEdIBO\` on Windows;
rotating, 2 MB x 3 backups).

### Firmware

```bash
# Gateway — one build, Seeed XIAO ESP32-S3 (ESP-IDF)
cd firmware/gateway && pio run -e seeed_xiao_esp32s3 --target upload

# Actuator nodes (one project, env per variant)
cd firmware/node_actuator && pio run -e direct      --target upload
cd firmware/node_actuator && pio run -e multiplexed --target upload

# Sensor node (4x MLX90393 touch board)
cd firmware/node_magnet_sensor && pio run -e release --target upload

# Thymio radio co-processor (optional) — Seeed XIAO ESP32-C6
cd firmware/thymio_rcp && pio run -e rcp_c6 --target upload
```

Prebuilt binaries for all of the above are produced by `scripts/build-firmware.sh`.

Requires [PlatformIO](https://platformio.org/).

> **Updating nodes later:** once a node has been cable-flashed once with the
> current partition table, you can reflash it wirelessly via **Tools → Update
> Nodes (OTA)…** in the app — it streams the bundled firmware to the node over
> ESP-NOW through the connected gateway (no cable, no WiFi). See
> [firmware/gateway/README.md](firmware/gateway/README.md#ota-firmware-update-over-esp-now).

The CI pipeline automatically selects the firmware environment:
- **Nightly** (push to `master`) → node debug build
- **Stable release** (tag `v*`) → node release build

### Debug builds

| Layer | Production | Development |
|-------|-----------|-------------|
| **Python app** | `run.py` — warnings only on console | `run.py --debug` — all levels on console |
| **Node firmware** | `release` env — no Serial, no debug overhead | `debug` env — Serial logs, tx counters, `{"cmd":"debug"}` |
| **Gateway firmware** | Single build, no debug overhead | Same (transparent bridge, nothing to gate) |

### Key source paths

| Path | Description |
|------|-------------|
| `src/hardware/skin.py` | Skin model — groups 1-3 AirChambers on one ESP32 node; `skin_type` + `geometry` |
| `src/hardware/skin_geometry.py` | Hardcoded skin-geometry registry (shape + sensor coords) keyed by `skin_type` |
| `src/hardware/air_chamber.py` | AirChamber model — pressure 0-100 %, configurable max |
| `src/hardware/esp32_controller.py` | Real hardware controller (via the SoftEdIBO gateway) |
| `src/hardware/simulated_controller.py` | Mock controller for simulation mode |
| `src/hardware/touch_profiles.py` | Touch-sensor technology seam (magnet today, capacitive skeleton) — see [docs/TOUCH_SENSORS.md](docs/TOUCH_SENSORS.md) |
| `src/data/stream_recorder.py` | Per-session JSONL recorder of all gateway sensor messages |
| `src/data/export.py` | Session CSV export with robot/participant attribution |
| `src/ml/` | Touch-gesture pipeline (segmenter, features, classifier) — see [docs/TOUCH_ML.md](docs/TOUCH_ML.md) |
| `src/gui/skin_shapes.py` | Shared skin-outline masks (round/triangle/thymio) + aspect ratio |
| `src/robots/` | TurtleRobot, TreeRobot, ThymioRobot, SimulatedRobot |
| `src/activities/` | Activity registry + declarative behaviour engine (`ScriptedActivity`, verb catalogue) |
| `src/gui/monitor/` | Live pressure monitor widgets |
| `scripts/label_touches.py` / `scripts/train_touch_model.py` | Offline touch-gesture labelling + training (`.[ml]` extra) |
| `src/log.py` | Centralized logging setup (console + rotating file) |
| `config/settings.yaml` | Robot and hardware configuration |
| `firmware/gateway/` | Gateway firmware (ESP-IDF, Seeed XIAO ESP32-S3) |
| `firmware/thymio_rcp/` | Thymio radio co-processor (XIAO ESP32-C6) — dongle-free 802.15.4 control; see [docs/THYMIO_WIRELESS_CONTROL.md](docs/THYMIO_WIRELESS_CONTROL.md) |
| `firmware/common/` | Shared firmware headers (`se_espnow.h`, units/pressure/dbg/cmd_queue) |
| `firmware/node_actuator/` | Actuator nodes — `direct` + `multiplexed` variants (build-flag envs) |
| `firmware/node_magnet_sensor/` | Sensor node — 4× MLX90393 touch board (`node_magnet_sensor` protocol) |
| `firmware/node_actuator/src/direct/pins.h` | node_direct pin definitions |
| `firmware/node_actuator/src/multiplexed/pins.h` | node_multiplexed pin definitions |
