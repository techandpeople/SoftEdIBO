# Thymio RCP (XIAO ESP32-C6)

The **radio co-processor (RCP)** firmware that drives Thymio robots wirelessly
**without the Thymio RF dongle**. Background and the full plan are in
[docs/THYMIO_WIRELESS_CONTROL.md](../../docs/THYMIO_WIRELESS_CONTROL.md).

One build - **`rcp_c6`**, flashed onto a XIAO **ESP32-C6**. It is
**transport-agnostic**: it services *both* its own USB-Serial/JTAG port and the
inter-board UART to the S3 host, and replies on whichever channel a line arrived
on (no host simply means no bytes, so a disconnected USB never stalls it). The
same binary therefore works flashed-solo-on-USB for bench testing *and* wired to
the S3 gateway in production - no rebuild, no mode jumper. The S3 gateway forwards
`{"target":"thymio",...}` lines to it over UART and tags its replies `"source":"thymio"`.

What it does (all over the C6's own 802.15.4 radio - see the docs for the protocol):

- **`thymio_link`** - *be* the dongle: poll the Thymio ~10 Hz to hold its receive
  window open and assert the held motor/LED targets (`thymio_drive` / `thymio_leds` /
  `thymio_sound`), for up to 4 robots addressed by slot (`thymio_set`).
- **sensors** - the same link poll also reads `acc` / `mic` / `ground` and forwards
  them as `{"type":"thymio_sensors",...}` (raw reply frames via `thymio_rx_debug`).
- **`thymio_discover`** - broadcast Aseba `LIST_NODES`; report each replying robot's
  address as `{"type":"thymio_found","addr":"6a25"}`.
- **`sniff_start` / `sniff_ch` / `sniff_stop`** - promiscuous 802.15.4 capture (raw
  frames as `{"type":"frame",...}`), the Phase-1 protocol reverse-engineering.
- **`tx`** - transmit a raw frame hex (replay / forge).
- **`ota_wifi`** - WiFi-OTA self-update from the S3's SoftAP; flashed once over USB,
  every update after comes over the air.
- **`ping`** / bare `PING <n>` - bring-up check: a `{"type":"pong"}` / `PONG <n>`
  proves the C6 received the line *and ran code on it* (a shorted wire would only
  echo `PING` back).

## Wiring (4 wires)

| S3 (host) | <-> | C6 (RCP) |
|-----------|---|----------|
| D6/TX (GPIO43) | -> | D7/RX (GPIO17) |
| D7/RX (GPIO44) | <- | D6/TX (GPIO16) |
| GND | - | GND (common ground, required) |
| 5V (VBUS) | -> | 5V (VBUS) - one USB cable powers both |

Both boards are 3.3 V logic, so the UART connects directly (no level shifter).
Do **not** tie the two `3V3` pads together - power flows 5V -> 5V only.

## Build & flash

```bash
cd firmware/thymio_rcp
pio run -e rcp_c6 -t upload        # first flash over the C6's own USB
pio device monitor                 # 115200 baud
```

After this first USB flash the C6 needs no USB again - updates go over WiFi-OTA
from the S3's SoftAP (Tools -> Update Nodes (OTA)... -> the "C6 (Thymio RCP)" row, or
`scripts/ota_c6_wifi.py`).

## Bench test over USB (no S3, no soldering)

The one build runs standalone on the C6's USB, so you can exercise it before the
inter-board link is soldered. In the monitor (type directly - no `"target"` wrapper):

```
PING 1                  -> PONG 1                            (bring-up echo)
{"cmd":"ping"}          -> {"type":"pong","src":"c6"}
{"cmd":"sniff_start"}   -> {"type":"frame",...}  once a Wireless Thymio is powered nearby
```

A `PONG` proves the C6 received the line *and ran code on it*. A spare C6 on USB
running this same build is also how you sniff or drive a Thymio directly, reading
its replies over its own USB - see the docs.
