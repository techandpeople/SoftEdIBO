# ESP-NOW Gateway Firmware

Bridges USB/serial (PC) <-> ESP-NOW (nodes). The ESP-NOW / MAC / radio plumbing
is shared with the node firmwares via `firmware/common/se_espnow.h`.

## Three board variants

All speak the **identical** serial protocol below; pick the one matching your
hardware. Each compiles only its own entry point (see `platformio.ini`).

| Variant | Board | Framework | Source | PlatformIO env | Output bin |
|---------|-------|-----------|--------|----------------|-----------|
| Preferred | **Seeed XIAO ESP32-S3** (Xtensa, dual-core), native USB-Serial/JTAG | ESP-IDF | `src/main.cpp` (cJSON, usb_serial_jtag) | `seeed_xiao_esp32s3` | `firmware.bin` |
| Compact | **Seeed XIAO ESP32-C6** (RISC-V), native USB-Serial/JTAG | ESP-IDF | `src/main.cpp` (cJSON, usb_serial_jtag) | `seeed_xiao_esp32c6` | `firmware.bin` |
| Old | **ESP32-WROOM-32** DevKit, USB-UART bridge (CH340/CP2102) | Arduino | `src/main_arduino.cpp` (ArduinoJson, Serial) | `esp32dev` | `firmware-esp32.bin` |

Baud rate: 115200 either way. The S3 and C6 share the **same** ESP-IDF source
(both have the native USB-Serial/JTAG peripheral); the S3 is preferred because
its dual-core Xtensa and extra RAM leave headroom to run a SoftAP for the
Thymios alongside ESP-NOW.

## Build & Flash

```bash
cd firmware/gateway
pio run -e seeed_xiao_esp32s3    --target upload  # preferred: XIAO ESP32-S3
pio run -e seeed_xiao_esp32s3_ap --target upload  # S3 + WiFi AP for Thymios
pio run -e seeed_xiao_esp32c6    --target upload  # compact:   XIAO ESP32-C6
pio run -e esp32dev             --target upload   # old:       ESP32-WROOM-32
```

### Optional WiFi access point (`-DGATEWAY_AP`)

The `seeed_xiao_esp32s3_ap` env builds the gateway with a **SoftAP** so WiFi
clients (e.g. Thymio robots) can associate **while ESP-NOW keeps running** — the
two share one 2.4 GHz radio, so the AP and the nodes must be on the **same
channel** (default 1). On the dual-core S3 the ESP-NOW bridge runs on the second
core so AP traffic never stalls forwarding to the PC. Defaults: SSID `SoftEdIBO`,
password `softedibo`, channel 1, up to 8 clients — override at build time:

```bash
pio run -e seeed_xiao_esp32s3_ap \
  -a '-DGATEWAY_AP_SSID="MyNet" -DGATEWAY_AP_PASS="secret12" -DGATEWAY_AP_CHANNEL=6'
```

A SoftAP build announces itself with an extra `"ap"` field on boot:
`{"status":"gateway_ready","mac":"…","ap":"SoftEdIBO"}`. In the desktop app the
setup wizard exposes this as a **"Run as WiFi access point for Thymio robots"**
checkbox on the gateway-flash page (enabled only for boards with an AP build).

The SSID/password can be changed at **runtime** (no reflash) via two
gateway-local commands — JSON lines **without** a `"target"`, so the gateway
handles them itself instead of forwarding over ESP-NOW. New values persist in
NVS. The desktop app exposes this as **Tools → Gateway WiFi AP…**.

```jsonc
// PC => gateway
{"cmd":"get_ap"}
{"cmd":"set_ap","ssid":"MyNet","pass":"secret12"}   // omit "pass" to keep current
// gateway => PC
{"type":"ap_config","ssid":"SoftEdIBO","channel":1,"secured":true}
{"type":"ap_set","ok":true,"ssid":"MyNet"}
{"type":"ap_set","ok":false,"reason":"bad_password"}   // or "empty_ssid"
{"type":"error","reason":"ap_not_supported"}           // non-AP build
```

Requires [PlatformIO](https://platformio.org/). The XIAO C6 (RISC-V) and S3
(Xtensa) need ESP-IDF 5.x — the official `espressif32` 6.x ships Arduino core
2.x and does NOT support them with the espidf framework, so both XIAO envs pin
the **pioarduino** platform fork (verified: IDF 5.5.4). Native ESP-IDF also
works (the `CMakeLists.txt` are shared):

```bash
cd firmware/gateway
idf.py set-target esp32s3 && idf.py build flash   # or: set-target esp32c6
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
{"source":"AA:BB:CC:DD:EE:01","type":"status","chamber":0,"pressure":75,"kpa":6.00}
{"source":"AA:BB:CC:DD:EE:01","type":"pong"}
{"source":"AA:BB:CC:DD:EE:01","type":"debug","ch":[...],"tx_ok":1520,"tx_fail":3,"drop":0,"up":342}
{"status":"gateway_ready","mac":"AA:BB:CC:DD:EE:00"}
```

All `"pressure"` values are **0-100 %** of the node's configured maximum pressure.
The `"debug"` response is only available from nodes flashed with the debug firmware.

Maximum line length: **512 bytes** (`SERIAL_BUF_LEN` constant). It was raised
from 256 for OTA: an `ota_data` line carries a base64 chunk *plus* the `"target"`
MAC, which overflows 256.

## OTA firmware update (over ESP-NOW)

Nodes can be reflashed wirelessly through the gateway — the PC streams the image
as ordinary JSON commands, the gateway relays them unchanged, and the node
writes flash via `firmware/common/se_ota.h`. Driven PC-side by
`src/hardware/node_ota_updater.py`; the gateway itself needs no OTA-specific code
(only the larger line buffer above).

```jsonc
// PC => node
{"target":"AA:..","cmd":"ota_begin","size":768929,"md5":"<hex>","chunk":144}
{"target":"AA:..","cmd":"ota_data","seq":0,"data":"<base64 of 144 bytes>"}
{"target":"AA:..","cmd":"ota_end"}
// node => PC
{"source":"AA:..","type":"ota_ready"}
{"source":"AA:..","type":"ota_ack","seq":0}
{"source":"AA:..","type":"ota_done"}          // node then reboots
{"source":"AA:..","type":"ota_error","reason":"verify_failed"}
```

Chunk = 144 raw bytes (→ 192 base64 chars, comfortably under the 250-byte
ESP-NOW limit). The node tolerates a sliding window (re-ACKs duplicates, drops
out-of-order future chunks); the PC retransmits on a per-sequence timeout and
verifies the image with the MD5 from `ota_begin`. Nodes need an OTA partition
table (`default.csv`); see each node's `platformio.ini`.

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
  same WiFi channel. The `-DGATEWAY_AP` build adds a SoftAP (`WIFI_APSTA`) on
  that same channel — the single radio is time-shared between AP traffic and
  ESP-NOW, so heavy client traffic competes for airtime with the nodes.
- Maximum ESP-NOW payload: **250 bytes**. Keep JSON commands short.
- The `esp_now_peer_info_t.channel = 0` means "use current channel". If you
  change the WiFi channel, all peers must be re-added.
