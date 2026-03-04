# ESP-NOW Gateway Firmware

ESP32-WROOM-32 firmware that bridges USB/serial (PC) ↔ ESP-NOW (nodes).

## Hardware

- **Board:** ESP32-WROOM-32 (DevKit)
- **Connection to PC:** USB via CH340/CP2102
- **Baud rate:** 115200

## Build & Flash

```bash
cd firmware/gateway
pio run --target upload
```

Requires [PlatformIO](https://platformio.org/). The ESP32 toolchain (~500 MB)
is downloaded automatically on first build and cached for subsequent builds.

## Serial Protocol (newline-terminated JSON)

**PC → Gateway:**
```json
{"target":"AA:BB:CC:DD:EE:01","cmd":"inflate","chamber":0,"value":255,"target":1000}
{"target":"FF:FF:FF:FF:FF:FF","cmd":"ping"}
```

**Gateway → PC:**
```json
{"source":"AA:BB:CC:DD:EE:01","type":"status","chamber":0,"pressure":2048}
{"status":"gateway_ready","mac":"AA:BB:CC:DD:EE:00"}
```

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

## Performance notes

- **`-O3`** compiler flag — maximum runtime optimization.
- Serial loop uses a **fixed char buffer** instead of Arduino `String` — zero heap
  allocation per received line, no fragmentation.
- `serializeJson` writes directly into a stack-allocated `char[256]` for outgoing
  ESP-NOW payloads.

## Important caveats

- ESP-NOW and WiFi share the same radio. The gateway runs in `WIFI_STA` mode
  **without** connecting to an AP (channel 1 by default). Nodes must be on the
  same WiFi channel.
- Maximum ESP-NOW payload: **250 bytes**. Keep JSON commands short.
- The `esp_now_peer_info_t.channel = 0` means "use current channel". If you
  change the WiFi channel, all peers must be re-added.
