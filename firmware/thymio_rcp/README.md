# Thymio RCP bring-up

Minimal **host ↔ radio-co-processor (RCP)** UART link test, the first step toward
controlling Thymio robots wirelessly without the Thymio RF dongle. Background and
the full plan are in [docs/THYMIO_WIRELESS_CONTROL.md](../../docs/THYMIO_WIRELESS_CONTROL.md).

- **host_s3** → XIAO **ESP32-S3** (the board with the USB cable to the PC).
  Sends `PING <n>` over UART, prints the reply.
- **rcp_c6** → XIAO **ESP32-C6**. Replies `PONG <n>`. **Transport-agnostic**: it
  services *both* its own USB and the inter-board UART and replies on whichever
  the line came in on — so the same binary works flashed-solo-on-USB (type
  `PING 1` in the C6's monitor, get `PONG 1`, no S3 needed) and wired to the S3.
- **c6_radio** → XIAO **ESP32-C6 standalone** (no S3, no wiring): brings up the
  802.15.4 radio in promiscuous mode and prints raw frames, hopping channels
  11..26. The first hands-on step of the Thymio protocol R&D — flash it over the
  C6's own USB while you wait to solder the inter-board link.

A `PONG` proves the C6 received the line *and ran code on it* (a shorted wire
would echo back `PING`).

## Wiring (4 wires)

| S3 (host) | ↔ | C6 (RCP) |
|-----------|---|----------|
| D6/TX (GPIO43) | → | D7/RX (GPIO17) |
| D7/RX (GPIO44) | ← | D6/TX (GPIO16) |
| GND | — | GND (common ground, required) |
| 5V (VBUS) | → | 5V (VBUS) — one USB cable powers both |

Both boards are 3.3 V logic, so the UART connects directly (no level shifter).
Do **not** tie the two `3V3` pads together — power flows 5V → 5V only.

## Run

```bash
cd firmware/thymio_rcp
pio run -e rcp_c6  -t upload       # flash the C6 first (over its own USB)
pio run -e host_s3 -t upload       # then the S3
pio device monitor                 # watch the S3: expect "PING n -> ... OK"
```

Expected on the S3 monitor:

```
[host] PING 0 -> "PONG 0"  OK
[host] PING 1 -> "PONG 1"  OK
```

`NO REPLY` ⇒ check the wiring (TX↔RX crossed? common GND? C6 powered & flashed?).

### Solo, no soldering — C6 radio sniffer

```bash
pio run -e c6_radio -t upload      # flash the C6 alone over its own USB
pio device monitor                 # watch raw 802.15.4 frames, hopping ch 11..26
```

Expected (frames appear once a Wireless Thymio is powered nearby):

```
[c6 802154] ch=15 — no frames
[c6 802154] ch=15 rssi= -58 lqi=255 len= 12 | 61 88 ...
```

Each board flashes over its own USB and runs independently — you can test the C6
radio and the S3 host separately before the inter-board link is soldered.
