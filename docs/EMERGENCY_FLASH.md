# Emergency flash — recovering a node with a dead USB

The normal way to update a node is **OTA over ESP-NOW** (app → *Tools → Update
Nodes (OTA)*). That only works while the node still runs OTA-capable firmware.
If a node's USB-serial path dies **and** it is too bricked for OTA (bad flash,
wrong partition table, boot loop), you must write a known-good image over the
wires — but with no working USB on the board itself.

The trick: use a **second ESP32 as a USB-to-serial bridge** to the dead board's
UART0 pins, flash once, and from then on use OTA again.

Two front-ends, same operation:

- **GUI** — *Tools → Emergency Flash (dead USB)…* (`EmergencyFlashDialog`). Uses
  the same firmware images as the setup wizard, so dev flashes the local
  `firmware/*.bin` and a frozen nightly/release flashes the CI-built bundle.
- **CLI** — [`scripts/emergency-flash.sh`](../scripts/emergency-flash.sh), a
  headless dev fallback that flashes the local `firmware/*.bin`
  (build them first with `scripts/build-firmware.sh`).

## The bridge board

Must be a classic **ESP32-WROOM DevKit with a CP2102/CH340** USB-serial chip.
Boards with **native USB** (ESP32-S3 / C6 / XIAO) will **not** work as a bridge.
A plain USB-to-TTL dongle (FTDI/CP2102) works too and is simpler.

Holding the bridge's own ESP32 in reset (`EN→GND`) turns its USB-serial chip
into a transparent adapter for the target's UART0.

## Wiring — STRAIGHT-THROUGH (not crossed)

Because you are tapping the bridge's CP2102/CH340 (whose TX/RX sit on the
board's RX/TX nets), the labels are already "flipped" — so wire TX→TX, RX→RX:

| Bridge board pin | Target board pin |
|------------------|------------------|
| `EN` → `GND`     | (jumper on the bridge only; keeps its chip in reset) |
| `TX` (TX0/IO1)   | `TX` (TX0/IO1)   |
| `RX` (RX0/IO3)   | `RX` (RX0/IO3)   |
| `GND`            | `GND`            |
| `3V3` (or `5V`)  | `3V3` (or `5V`)  — never both; common GND |

> If it still won't sync, swap the two data wires (try crossed) and retry —
> boards vary.

## Download mode (on the TARGET)

The bridge can't drive the target's reset lines, so esptool runs with **no
auto-reset** and you enter download mode by hand:

1. **Hold** `BOOT` (= `IO0`).
2. **Tap** `EN`/`RST`.
3. **Release** `BOOT`.

Do this as flashing starts (while it prints `Connecting…`).

## Flash

GUI: pick the node type, plug in the bridge, click **Flash Node**.

CLI:

```bash
scripts/emergency-flash.sh -t direct                 # node_direct, release
scripts/emergency-flash.sh -t multiplexed -d         # multiplexed, debug
scripts/emergency-flash.sh -t magnet -p /dev/ttyUSB0 -b 115200
```

Start at **115200** baud; raise it only if that flashes reliably. The bundled
`.bin` files are **merged** images, written at `0x0`.

When it finishes: remove the `IO0`/`BOOT` jumper, tap `EN`/`RST`. The node now
runs OTA-capable firmware — update it wirelessly from here on.
