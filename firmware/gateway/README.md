# ESP-NOW Gateway Firmware

Bridges USB/serial (PC) <-> ESP-NOW (nodes). The ESP-NOW / MAC / radio plumbing
is shared with the node firmwares via `firmware/common/se_espnow.h`.

## Two board variants

Both speak the **identical** serial protocol below; pick the one matching your
hardware. Each compiles only its own entry point (see `platformio.ini`).

| Variant | Board | Framework | Source | PlatformIO env | Output bin |
|---------|-------|-----------|--------|----------------|-----------|
| New | **Seeed XIAO ESP32-C6** (RISC-V), native USB-Serial/JTAG | ESP-IDF | `src/main.cpp` (cJSON, usb_serial_jtag) | `seeed_xiao_esp32c6` | `firmware.bin` |
| Old | **ESP32-WROOM-32** DevKit, USB-UART bridge (CH340/CP2102) | Arduino | `src/main_arduino.cpp` (ArduinoJson, Serial) | `esp32dev` | `firmware-esp32.bin` |

Baud rate: 115200 either way.

## Build & Flash

```bash
cd firmware/gateway
pio run -e seeed_xiao_esp32c6 --target upload   # new: XIAO ESP32-C6
pio run -e esp32dev          --target upload    # old: ESP32-WROOM-32
```

Requires [PlatformIO](https://platformio.org/). The C6 (RISC-V) needs ESP-IDF
5.x — the official `espressif32` 6.x ships Arduino core 2.x and does NOT support
it, so the C6 env pins the **pioarduino** platform fork (verified: IDF 5.5.4).
Native ESP-IDF also works (the `CMakeLists.txt` are shared):

```bash
cd firmware/gateway
idf.py set-target esp32c6 && idf.py build flash
```

> Flashing offsets differ: the C6 merged image has its bootloader at `0x0`,
> the WROOM at `0x1000` — but both merged `.bin` files are written at `0x0`
> (`esptool --chip esp32c6 …` vs `--chip esp32 …`). The setup wizard handles
> this automatically.

## Serial Protocol (newline-terminated JSON)

**PC => Gateway** — every command must include a `"target"` MAC:
```json
{"target":"AA:BB:CC:DD:EE:01","cmd":"set_max_pressure","chamber":0,"value":80}
{"target":"AA:BB:CC:DD:EE:01","cmd":"inflate","chamber":0,"delta":20}
{"target":"AA:BB:CC:DD:EE:01","cmd":"deflate","chamber":1,"delta":15}
{"target":"AA:BB:CC:DD:EE:01","cmd":"set_pressure","chamber":2,"value":75}
{"target":"AA:BB:CC:DD:EE:01","cmd":"hold","chamber":0}
{"target":"FF:FF:FF:FF:FF:FF","cmd":"ping"}
{"target":"AA:BB:CC:DD:EE:01","cmd":"debug"}
```

The gateway strips `"target"` before forwarding so nodes receive only the command fields.

**Gateway => PC** — every message from a node gets a `"source"` MAC added:
```json
{"source":"AA:BB:CC:DD:EE:01","type":"status","chamber":0,"pressure":75}
{"source":"AA:BB:CC:DD:EE:01","type":"pong"}
{"source":"AA:BB:CC:DD:EE:01","type":"debug","ch":[...],"tx_ok":1520,"tx_fail":3,"drop":0,"up":342}
{"status":"gateway_ready","mac":"AA:BB:CC:DD:EE:00"}
```

All `"pressure"` values are **0-100 %** of the node's configured maximum pressure.
The `"debug"` response is only available from nodes flashed with the debug firmware.

Maximum line length: **256 bytes** (`SERIAL_BUF_LEN` constant).

## Behaviour

- On boot sends `{"status":"gateway_ready","mac":"..."}` to serial.
- Reads serial line-by-line using a **fixed stack buffer** (no heap allocation).
- Forwards every JSON command from serial to the `target` MAC via ESP-NOW,
  stripping the `"target"` field before forwarding.
- Forwards every ESP-NOW message received from nodes to serial, adding a
  `"source"` field with the sender MAC.
- Broadcast address `FF:FF:FF:FF:FF:FF` is pre-registered as peer for scan/ping.
- Unknown sender MACs are dynamically added as peers on first send.
- **Fire-and-forget** delivery — no retry logic. ESP-NOW provides link-layer
  ACKs automatically; the app can resend if it doesn't see a pressure change.

## Performance notes

- The serial read loop uses a **fixed char buffer** — zero heap allocation per
  received line.
- ESP-NOW receives run in the WiFi task: the callback only copies the payload
  into a **FreeRTOS queue**; a dedicated task serialises (cJSON) and writes to
  USB, so the radio stack never blocks on serial I/O.
- JSON is handled with **cJSON** (bundled in ESP-IDF) — no external dependency.

## Important caveats

- ESP-NOW and WiFi share the same radio. The gateway runs in `WIFI_STA` mode
  **without** connecting to an AP (channel 1 by default). Nodes must be on the
  same WiFi channel.
- Maximum ESP-NOW payload: **250 bytes**. Keep JSON commands short.
- The `esp_now_peer_info_t.channel = 0` means "use current channel". If you
  change the WiFi channel, all peers must be re-added.
