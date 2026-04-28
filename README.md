# SoftEdIBO

Soft-based robot platform for inclusive, embodied interaction.
Developed at [LASIGE](https://www.lasige.pt/), Faculdade de Ciencias, Universidade de Lisboa.

SoftEdIBO controls soft robots equipped with inflatable air chambers.
Participants interact by touching the robots, which respond through inflation and deflation.
The system supports multiple robot types (Turtle, Tree, Thymio) and activity modes.

---

## Hardware requirements

| Component | Quantity | Notes |
|-----------|----------|-------|
| ESP32-WROOM-32 (gateway) | 1 | Connected to PC via USB |
| ESP32-WROOM-32 (`node_direct`) | 1 per direct board | 3 chambers, direct ADC sensors, onboard pumps |
| ESP32-WROOM-32 (`node_reservoir`) | 1 per reservoir board | Up to 12 chambers + shared pressure/vacuum tanks |
| DRV3297 motor driver | 1 (`node_direct`) / 3 (`node_reservoir`) | Drives pumps with PWM |
| Air pump | 2 (`node_direct`) / 6 (`node_reservoir`) | Inflate/deflate supply |
| XGZP6847A pressure sensor | 1 per chamber | Analog output (0-3.3 V) |
| Solenoid valves | 2 per chamber | Inflate + deflate via ULN2803A |

Flash the [gateway firmware](firmware/gateway/) to the USB-connected ESP32.
For each node, choose one of the two firmware targets:

| Firmware | Path | When to use |
|----------|------|-------------|
| `node_direct` (`release` / `debug`) | [firmware/node_direct/](firmware/node_direct/) | Direct board (fixed 3 chambers) |
| `node_reservoir` (`release` / `debug`) | [firmware/node_reservoir/](firmware/node_reservoir/) | Reservoir board (default 12 chambers, runtime configurable) |

---

## Architecture

```
PC --USB--> Gateway (ESP32) --ESP-NOW--> node_direct    (3 chambers, direct GPIO valves + own pumps)
                                     └-> node_reservoir (12 chambers + shared pressure/vacuum tanks)
```

**Software layers:**

```
SessionPanel
  +-- Activity (GroupTouch, Simulation, ...)
        +-- Robot (Turtle / Tree / Thymio / Simulated)
              +-- Node(s)  (ESP32, identified by MAC + node_type + max_slots)
                +-- Reservoir(s)  (auto-derived from node_reservoir using slots N and N+1)
              +-- Skin(s)  (logical grouping of 1-3 chambers from any node of this robot)
                    +-- AirChamber  (local index 0-2, pressure 0-100 %)
```

          - **Node** is a physical ESP32. Its `node_type` (`node_direct` or `node_reservoir`) determines which firmware to flash.
- **Skin** groups 1-3 chambers. Chambers can come from different nodes of the same robot. Activities address chambers by local skin index (0, 1, 2) — no knowledge of node topology required.
          - **Reservoir** is an optional per-robot shared air tank (pressure or vacuum). For `node_reservoir`, pressure and vacuum reservoirs are internal to the same MAC.
- **Pressure** is expressed as **0-100 %** of the maximum pressure configured on each node.
- **Per-chamber max pressure** is set in `settings.yaml` and enforced both in the app and on the ESP32 (hardware safety — survives app crashes).
          - **Pressure sensing** uses the XGZP6847A datasheet transfer function (see [pressure.h](firmware/node_direct/src/pressure.h)).

---

## Installation

### Linux (x86-64)

```bash
curl -fsSL https://raw.githubusercontent.com/techandpeople/SoftEdIBO/master/install.sh | bash
```

This will download the latest release, install it to `/opt/SoftEdIBO/`, create a `softedibo` command in your PATH, and add a desktop entry to the application menu.

**Nightly build** (latest commit on `master`):

```bash
curl -fsSL https://raw.githubusercontent.com/techandpeople/SoftEdIBO/master/install.sh | bash -s -- --nightly
```

**Uninstall:**

```bash
softedibo --uninstall 2>/dev/null || /opt/SoftEdIBO/install.sh --uninstall
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

Robots are configured as a flat list of skins per type. Each skin maps to an ESP32 node (by MAC address) and specifies which chamber slots it uses. An optional `max_pressure` field sets per-chamber safety limits (in % of the hardware maximum).

```yaml
robots:
  turtles:
    - id: turtle_1
      skins:
        - skin_id: shell_top
          name: Shell Top
          mac: "AA:BB:CC:DD:EE:01"
          slots: [0, 1, 2]
          max_pressure:       # optional — defaults to 100%
            0: 80             # chamber 0 capped at 80%
            1: 60             # chamber 1 capped at 60%
  trees: []
  thymios: []
```

- Multiple skins can share the same MAC (up to 3 slots total per node).
- `max_pressure` is sent to the ESP32 node on startup. The node enforces it independently — even if the app crashes, chambers will not exceed their configured limit.
- If `max_pressure` is omitted, all chambers default to 100% (the full hardware range).

---

## Development

### Python application

```bash
git clone https://github.com/techandpeople/SoftEdIBO.git
cd SoftEdIBO
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/run.py
```

Requires Python 3.12+.

**Debug mode** — shows all log levels on the console (DEBUG+):

```bash
python scripts/run.py --debug
```

Without `--debug`, only warnings and errors are shown on the console. All log
levels are always written to `data/softedibo.log` (rotating, 2 MB x 3 backups).

### Firmware

```bash
# Gateway
cd firmware/gateway && pio run --target upload

# Direct node
cd firmware/node_direct && pio run -e release --target upload

# Reservoir node
cd firmware/node_reservoir && pio run -e release --target upload
```

Requires [PlatformIO](https://platformio.org/).

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
| `src/hardware/skin.py` | Skin model — groups 1-3 AirChambers on one ESP32 node |
| `src/hardware/air_chamber.py` | AirChamber model — pressure 0-100 %, configurable max |
| `src/hardware/esp32_controller.py` | Real hardware controller (via ESP-NOW gateway) |
| `src/hardware/simulated_controller.py` | Mock controller for simulation mode |
| `src/robots/` | TurtleRobot, TreeRobot, ThymioRobot, SimulatedRobot |
| `src/activities/` | Activity registry + GroupTouch + SimulationActivity |
| `src/gui/monitor/` | Live pressure monitor widgets |
| `src/log.py` | Centralized logging setup (console + rotating file) |
| `config/settings.yaml` | Robot and hardware configuration |
| `firmware/gateway/` | Gateway ESP32 firmware |
| `firmware/node_direct/` | node_direct firmware (3 chambers, GPIO valves + own pumps) |
| `firmware/node_reservoir/` | node_reservoir firmware (shared tanks + runtime sizing) |
| `firmware/node_direct/src/pins.h` | node_direct pin definitions |
| `firmware/node_reservoir/src/pins.h` | node_reservoir pin definitions |
