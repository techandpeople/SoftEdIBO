# Air Chamber Node Firmware

ESP32 firmware for remote nodes that control air chambers (inflate/deflate valves).

## Role
- Receives commands from the gateway via ESP-NOW
- Controls air chamber valves (inflate/deflate)
- Reports pressure sensor readings back to the gateway

## ESP-NOW Message Format
Each node is identified by its MAC address. Commands and status messages
use the same JSON format as the gateway serial protocol.

## Hardware
- 1 ESP32 per 3 air chambers
- Each chamber: 1 solenoid valve (inflate) + 1 solenoid valve (deflate)
- Optional: pressure sensor per chamber

## Setup
- Platform: ESP32 (Arduino or PlatformIO)
- Register gateway MAC address as ESP-NOW peer on boot
