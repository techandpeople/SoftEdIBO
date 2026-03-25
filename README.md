# SoftEdIBO

Soft-based robot platform for inclusive, embodied interaction.
Developed at [LASIGE](https://www.lasige.pt/), Faculdade de Ciências, Universidade de Lisboa.

SoftEdIBO controls soft robots equipped with inflatable air chambers.
Participants interact by touching the robots, which respond through inflation and deflation.
The system supports multiple robot types (Turtle, Tree, Thymio) and activity modes.

---

## Hardware requirements

| Component | Quantity | Notes |
|-----------|----------|-------|
| ESP32-WROOM-32 (gateway) | 1 | Connected to PC via USB |
| ESP32-WROOM-32 (air chamber nodes) | 1 per skin | Up to 3 chambers per node |
| DRV8833 H-bridge | 2 per node | Inflate and deflate pumps |
| XGZP6847 pressure sensor | 1 per chamber | Analog output (0–3.3 V) |
| Solenoid valves | 2 per chamber | Inflate + deflate |

Flash the [gateway firmware](firmware/gateway/) to the USB-connected ESP32
and the [air chamber node firmware](firmware/air_chamber_node/) to each sensor/valve node.

---

## Architecture

```
PC ──USB──► Gateway (ESP32) ──ESP-NOW──► Node(s) (ESP32)
                                            │
                                     3 air chambers each
                                     (inflate/deflate valves + pressure sensors)
```

**Software layers:**

```
SessionPanel
  └── Activity (GroupTouch, Simulation, …)
        └── Robot (Turtle / Tree / Thymio / Simulated)
              └── Skin  (1 ESP32 node, 1–3 chambers)
                    └── AirChamber  (pressure 0–100 %)
```

- **Activity** decides which robots participate and can replace real robots with simulated ones (`SimulationActivity`).
- **Skin** is the basic tactile unit. Multiple skins can share one ESP32 node (up to 3 chambers total).
- **Pressure** is expressed as **0–100 %** of the maximum pressure configured on each node.
- **Per-chamber max pressure** can be set in `settings.yaml` and is enforced both in the app and on the ESP32 node (hardware safety — survives app crashes).

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

### Firmware

```bash
# Gateway
cd firmware/gateway && pio run --target upload

# Air chamber node
cd firmware/air_chamber_node && pio run --target upload
```

Requires [PlatformIO](https://platformio.org/).

### Key source paths

| Path | Description |
|------|-------------|
| `src/hardware/skin.py` | Skin model — groups 1–3 AirChambers on one ESP32 node |
| `src/hardware/air_chamber.py` | AirChamber model — pressure 0–100 %, configurable max |
| `src/hardware/esp32_controller.py` | Real hardware controller (via ESP-NOW gateway) |
| `src/hardware/simulated_controller.py` | Mock controller for simulation mode |
| `src/robots/` | TurtleRobot, TreeRobot, ThymioRobot, SimulatedRobot |
| `src/activities/` | Activity registry + GroupTouch + SimulationActivity |
| `src/gui/monitor/` | Live pressure monitor widgets |
| `config/settings.yaml` | Robot and hardware configuration |
| `firmware/gateway/` | Gateway ESP32 firmware |
| `firmware/air_chamber_node/` | Air chamber node ESP32 firmware |
