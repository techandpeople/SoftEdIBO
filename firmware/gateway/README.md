# SoftEdIBO Gateway Firmware

Bridges USB/serial (PC) <-> ESP-NOW (nodes). The ESP-NOW / MAC / radio plumbing
is shared with the node firmwares via `firmware/common/se_espnow.h`.

## One build — Seeed XIAO ESP32-S3

The gateway is a **Seeed XIAO ESP32-S3** (Xtensa dual-core, native USB-Serial/JTAG),
ESP-IDF, `src/main.cpp` (cJSON). One build (`seeed_xiao_esp32s3` → `firmware-s3.bin`)
does everything: ESP-NOW chamber control **+** a SoftAP for the Thymios / WiFi-OTA
(`-DGATEWAY_AP`) **+** a UART link to a companion XIAO ESP32-C6 that speaks 802.15.4 to
Thymio robots (`-DGATEWAY_THYMIO`, see `docs/THYMIO_WIRELESS_CONTROL.md`). The dual-core
headroom keeps the ESP-NOW bridge responsive while WiFi clients are associated (the bridge
runs on the second core); PSRAM buffers the WiFi-OTA image. Baud rate 115200.

## Build & Flash

```bash
cd firmware/gateway
pio run -e seeed_xiao_esp32s3 --target upload
```

### WiFi access point (`-DGATEWAY_AP`, always on)

The gateway runs a **SoftAP** so WiFi clients (e.g. Thymio robots) can associate **while
ESP-NOW keeps running** — the two share one 2.4 GHz radio, so the AP and the nodes are on
the **same channel** (default 1). Defaults: SSID `SoftEdIBO`, password `softedibo`,
channel 1, up to 8 clients — override at build time:

```bash
pio run -e seeed_xiao_esp32s3 \
  -a '-DGATEWAY_AP_SSID="MyNet" -DGATEWAY_AP_PASS="secret12" -DGATEWAY_AP_CHANNEL=6'
```

The gateway announces itself with an `"ap"` field on boot:
`{"status":"gateway_ready","mac":"…","ap":"SoftEdIBO"}`.

The SSID/password can be changed at **runtime** (no reflash) via two
gateway-local commands — JSON lines **without** a `"target"`, so the gateway
handles them itself instead of forwarding over ESP-NOW. New values persist in
NVS. The desktop app exposes this as **Tools → Gateway WiFi AP…**.

```jsonc
// PC => gateway
{"cmd":"get_ap"}
{"cmd":"set_ap","ssid":"MyNet","pass":"secret12","channel":6}   // pass/channel optional (keep current)
// gateway => PC
{"type":"ap_config","ssid":"SoftEdIBO","channel":1,"secured":true}
{"type":"ap_set","ok":true,"ssid":"MyNet"}
{"type":"ap_set","ok":false,"reason":"bad_password"}   // or "empty_ssid"
{"type":"error","reason":"ap_not_supported"}           // non-AP build
```

This SoftAP also powers the **fast WiFi firmware update** (S3 only — it buffers
the image in PSRAM). In the desktop app's **Tools → Update Nodes (OTA)…** pick the
*WiFi* transport: the PC streams the image to the gateway over the **USB cable**
(`ota_store_*` gateway-local commands), the gateway buffers it in PSRAM and serves
it over HTTP (`http://<ap-ip>/fw`, with an `x-MD5` header), and the node joins the
AP to download it in seconds. The **PC never joins the WiFi** — it stays on the
cable. The node side is `firmware/common/se_ota.h` (the `ota_wifi` command); the
gateway proxy is in `src/main.cpp`.

```jsonc
// PC => gateway (no "target"): stage the image, then trigger the node
{"cmd":"ota_store_begin","size":806976,"md5":"…"}
{"type":"ota_store_ready"}                       // or {"type":"ota_store_error","reason":"no_psram"}
{"cmd":"ota_store_data","data":"<base64>"}        // ×N
{"type":"ota_store_ack","len":8192}               // cumulative, every 4 KB stored — the PC
                                                  // windows its sends on these (USB flow control)
{"cmd":"ota_store_end"}
{"type":"ota_stored","ok":true,"size":806976,"url":"http://192.168.4.1/fw"}
// staging errors ("reason"): bad_size, no_psram, httpd_failed, not_storing,
// size_mismatch (+got/want), md5_mismatch; ap_not_supported on a non-AP build
```

The gateway MD5-checks the staged image before serving it, and injects its own
AP `ssid`/`pass` into any forwarded `ota_wifi` command that doesn't carry one —
so the PC never needs to know the AP credentials and a renamed AP can't break
OTA.

Requires [PlatformIO](https://platformio.org/). The XIAO ESP32-S3 needs ESP-IDF 5.x — the
official `espressif32` 6.x ships Arduino core 2.x, so the env pins the **pioarduino**
platform fork (verified: IDF 5.5.4). Native ESP-IDF also works (shared `CMakeLists.txt`):

```bash
cd firmware/gateway
idf.py set-target esp32s3 && idf.py build flash
```

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
Two `"target"` values are special:

- **no `"target"`** — gateway-local command (`get_ap`, `set_ap`, `ota_store_*` above);
- **`"target":"thymio"`** — forwarded over UART to the companion C6 radio
  co-processor (`thymio_*` commands, see `docs/THYMIO_WIRELESS_CONTROL.md`);
  the C6's replies come back tagged `{"source":"thymio",...}`.

**Gateway => PC** — every message from a node gets a `"source"` MAC added:
```json
{"source":"AA:BB:CC:DD:EE:01","type":"status","chamber":0,"pressure":75,"kpa":6.00,"st":0,"vi":0,"vd":0}
{"source":"AA:BB:CC:DD:EE:01","type":"pong","rgbw":true,"kpa_min":0}
{"source":"AA:BB:CC:DD:EE:01","type":"debug","ch":[...],"tx_ok":1520,"tx_fail":3,"drop":0,"up":342}
{"status":"gateway_ready","mac":"AA:BB:CC:DD:EE:00","ap":"SoftEdIBO"}
```

A PC line that fails to parse (usually USB byte loss) is reported back as
`{"type":"error","reason":"bad_cmd_json","len":N,"raw":"…"}` instead of being
silently dropped, so a swallowed command (e.g. a missed `stop`) is visible.

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
{"target":"AA:..","cmd":"ota_begin","size":768929,"md5":"<hex>","chunk":96}
{"target":"AA:..","cmd":"ota_data","seq":0,"data":"<base64 of 96 bytes>"}
{"target":"AA:..","cmd":"ota_end"}
// node => PC
{"source":"AA:..","type":"ota_ready"}
{"source":"AA:..","type":"ota_ack","seq":0}
{"source":"AA:..","type":"ota_done"}          // node then reboots
{"source":"AA:..","type":"ota_error","reason":"verify_failed"}
```

Chunk = 96 raw bytes (→ 128 base64 chars; the gateway→node relay drops
payloads over ~190 bytes, so chunks stay well under that — see
`node_ota_updater.CHUNK_SIZE`). The node tolerates a sliding window (re-ACKs
duplicates, drops out-of-order future chunks); the PC retransmits on a
per-sequence timeout, verifies the image with the MD5 from `ota_begin`, and
only treats the update as done when the freshly-booted new firmware broadcasts
`ota_done`. Nodes need an OTA partition table (`default.csv`); see each node's
`platformio.ini`.

## Behaviour

- On boot sends `{"status":"gateway_ready","mac":"..."}` to serial.
- Reads serial line-by-line using a **fixed stack buffer** (no heap allocation).
- Forwards every JSON command from serial to the `target` MAC via ESP-NOW,
  stripping the `"target"` field before forwarding.
- Forwards every ESP-NOW message received from nodes to serial, adding a
  `"source"` field with the sender MAC.
- Broadcast address `FF:FF:FF:FF:FF:FF` is pre-registered as peer for scan/ping.
- Unknown sender MACs are dynamically added as peers on first send.
- PC→node forwards are **paced** (`se::sendPaced` waits for the previous
  frame's TX-done before sending the next), so a burst of back-to-back
  commands (e.g. a batch inflate) can't overrun the radio's tiny TX queue and
  silently drop frames.
- Still **fire-and-forget** — no retransmit. ESP-NOW provides link-layer
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
