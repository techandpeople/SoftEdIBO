# ESP-NOW Gateway Firmware

ESP32 firmware for the gateway node that bridges serial (USB) communication
with ESP-NOW wireless protocol.

## Role
- Connected to PC via USB/serial
- Receives JSON commands from PC and forwards to remote ESP32 nodes via ESP-NOW
- Receives ESP-NOW messages from remote nodes and forwards to PC via serial

## Serial Protocol (JSON)

**PC -> Gateway (commands):**
```json
{"target": "AA:BB:CC:DD:EE:01", "cmd": "inflate", "chamber": 0, "value": 255}
```

**Gateway -> PC (status):**
```json
{"source": "AA:BB:CC:DD:EE:01", "type": "status", "chamber": 0, "pressure": 128}
```

## Setup
- Platform: ESP32 (Arduino or PlatformIO)
- Baud rate: 115200
