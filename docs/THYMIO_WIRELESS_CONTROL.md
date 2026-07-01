# Controlling Thymio robots wirelessly — without the RF dongle

Goal: drive a Thymio's **movement and LEDs** from the SoftEdIBO app, wirelessly,
reusing the gear we already have, ideally with **one USB cable to the PC**. The
air chambers we mounted on each Thymio already work over ESP-NOW (`EspRobot`);
this doc is about the Thymio's own wheeled base, which is still a stub
(`ThymioRobot.connect` / `send_command` are `TODO` — see `src/robots/thymio/`).

## How a Thymio can be reached at all

A stock **Thymio II has no Wi-Fi and no Bluetooth**. Its only two control links are:

1. **micro-USB** — appears as a USB-CDC serial port and speaks **Aseba** (the PC /
   Thymio Suite TDM talks to it this way).
2. **Wireless module** — only on the *Wireless Thymio*: a **2.4 GHz IEEE 802.15.4**
   radio that pairs with the matching **Thymio RF dongle**. This is a proprietary
   Aseba-over-802.15.4 protocol — **not** Wi-Fi, BLE, ESP-NOW, Thread or Zigbee.

So our ESP-NOW/Wi-Fi gateway **cannot** reach the Thymio's mainboard by radio: the
ESP-NOW path only reaches the air-chamber node we added. Wireless control of the
Thymio means one of: keep the RF dongle, tether over USB, or **put a 802.15.4
radio of our own on the link**.

## Why the C6, not the S3

802.15.4 is silicon: a chip either has the radio or it doesn't. Verified against
this repo's own ESP-IDF toolchain (`components/soc/<chip>/include/soc/soc_caps.h`):

| Chip | Wi-Fi | BLE | 802.15.4 | Notes |
|------|:----:|:---:|:--------:|-------|
| ESP32-S3 | ✓ | ✓ | **✗** | `SOC_IEEE802154_SUPPORTED` absent — radio not in silicon |
| ESP32-C6 | ✓ | ✓ | **✓** | `SOC_IEEE802154_SUPPORTED=1` (Wi-Fi 6 + BLE 5 + 802.15.4) |
| ESP32-H2 | ✗ | ✓ | ✓ | 802.15.4 + BLE, **no Wi-Fi** |
| ESP32-C5 | ✓ | ✓ | ✓ | dual-band Wi-Fi 6 + BLE + 802.15.4, supports PSRAM |

The "ESP32-S3 supports Thread/Zigbee" claims online refer to the S3 acting as a
**gateway host** paired with a *separate* 802.15.4 radio co-processor (RCP) — it
does not have the radio itself. Only **C6 / H2 / C5 / C61** do.

## Chosen architecture: host (S3) + RCP (C6) over UART, one USB

We keep the **S3 as the host** (USB to the PC, ESP-NOW to the air nodes, SoftAP,
Wi-Fi-OTA — all the things it's already best at, with its PSRAM headroom) and add
a **C6 as a dedicated 802.15.4 radio co-processor (RCP)** wired to the S3 over
UART. Only the S3 has the USB cable. The PC sees one device; the S3 forwards
Thymio-targeted traffic to the C6 over the inter-board UART.

This is exactly Espressif's own **ESP Thread Border Router / Zigbee Gateway**
topology (ESP32-S3 host + ESP32-H2 RCP over UART, single USB-C). We reuse the
*hardware topology*, not the Thread stack — above 802.15.4 the C6 runs the
**Thymio** protocol, not Thread/Zigbee.

Why two chips instead of a single C6 doing both ESP-NOW and 802.15.4: the C6 *can*
(Wi-Fi + 802.15.4 coexist on its one shared 2.4 GHz antenna), but our ESP-NOW
traffic is already airtime-sensitive — bursty multi-chamber sends have dropped
frames before (fixed with paced sends). Giving the Thymio radio its own chip and
antenna removes that contention and leaves the working air-node path untouched.

```
        ┌─────────────────┐                 ┌─────────────────┐
  USB ──┤ XIAO ESP32-S3   │                 │ XIAO ESP32-C6   │
  (PC)  │  host           │                 │  RCP (802.15.4) │
        │ D6/TX (43) ─────┼──────────────►──┤ D7/RX (17)      │
        │ D7/RX (44) ──◄──┼─────────────────┤ D6/TX (16)      │
        │ GND ────────────┼─────────────────┤ GND             │
        │ 5V (VBUS) ──────┼─────────────────┤ 5V (VBUS)       │
        └─────────────────┘                 └─────────────────┘
          one USB cable                       no USB to the PC
```

### Wiring (4 wires)

| S3 (host) | ↔ | C6 (RCP) | Purpose |
|-----------|---|----------|---------|
| D6 / TX (GPIO43) | → | D7 / RX (GPIO17) | UART data |
| D7 / RX (GPIO44) | ← | D6 / TX (GPIO16) | UART data (crossed) |
| GND | — | GND | common ground (**required**) |
| 5V (VBUS) | → | 5V (VBUS) | power the C6 from the single USB |

- Cross **TX↔RX**; tying TX↔TX is the classic mistake.
- Both boards are **3.3 V logic** → direct UART, no level shifter.
- Power flows **5V → 5V** (each board's own LDO regulates 3V3). Do **not** tie the
  two `3V3` pads together.
- Optional (host reflashes the RCP itself): two more GPIO from the S3 to the C6's
  `RST` and a strap/`BOOT` pin. Skip for the prototype — flash the C6 over its own
  USB.

## Plan

- **Phase 0 — bring-up (done, untested on hardware):** prove the S3↔C6 UART link
  with a PING/PONG echo. Firmware: [`firmware/thymio_rcp/`](../firmware/thymio_rcp/).
  Validate the 4-wire connection before anything else.
- **Phase 1 — Thymio 802.15.4 protocol (R&D):** make the C6 talk to a Wireless
  Thymio by impersonating the RF dongle. The Thymio dongle firmware is
  open-source (Aseba/Mobsya), so the link layer (channel, pairing/network id,
  frame format carrying Aseba messages) is derivable; port it onto the C6's
  `esp_ieee802154` raw TX/RX. First milestone: set `motor.left.target` /
  `motor.right.target` and see a wheel move.
- **Phase 2 — PC integration:** the S3 host forwards `target:"thymio"` lines to the
  C6 over UART (a third route alongside the existing gateway-local vs target-MAC
  split). Fill in `ThymioRobot.connect` / `send_command` so `set_motors` /
  `set_leds` reach the C6 path. Optionally add movement verbs to the activity
  catalog/editor later (note: the scripted-activity runtime is per-skin, while
  movement is per-robot — a new addressing model).

**The C6 RCP firmware is transport-agnostic:** it services *both* its own USB and
the inter-board UART and replies on whichever channel a command arrived on (rather
than detecting USB presence — no host simply means no bytes, so a disconnected USB
never stalls it). One binary therefore works flashed-solo-on-USB for bench testing
*and* wired to the S3 in production — no rebuild, no mode jumper. The bring-up
`rcp_c6` env already demonstrates this with PING/PONG.

## Phase 1 — protocol notes (in progress)

From the open-source Thymio firmware (`Mobsya/aseba-target-thymio2`, `rf.c`/`rf.h`)
and the Mobsya/Aseba docs:

- The Wireless Thymio runs **Aseba over IEEE 802.15.4** (2.4 GHz), backward-compatible
  with the wired Aseba protocol.
- **The Thymio's main MCU does not do the 802.15.4 framing itself.** It drives an
  **external RF module over I²C** (addr `0x42`): registers for PAN/network id
  (`REG_PANID_L/H`), channel (`REG_CTRL = ((ch+1)<<3)+2`), FIFO TX/RX, pairing
  (`REG_PAIRING`), node id. The actual over-the-air frame is built inside the RF
  module's own firmware. **So the Thymio is already a host+RCP split (MCU ↔ I²C ↔
  RF module) — the same shape as our S3 ↔ UART ↔ C6.**
- **Logical channels 0/1/2 (default 1)** are the RF module's encoding, *not* raw
  802.15.4 channel numbers (11–26). The real channel is decided in the RF module
  firmware — **not yet located** (the source wiki is unmaintained; `electronics-thymio2-rf`
  is hardware only). Reference: Rétornaz et al., *Seamless Multi-Robot Programming…
  Wireless Thymio II*, 2013.
- There is a **pairing + shared network/PAN id** scheme: dongle and robot must share
  the network id and be on the same channel.

**Decode strategy — sniffer-first.** Rather than fully reverse the RF firmware on
paper, capture real frames with the `c6_radio` env (it hops 11–26, so it finds the
channel regardless of the 0/1/2 mapping) and correlate the bytes with the Aseba
message structure. The unknowns the capture resolves directly: the actual 802.15.4
channel, PAN id, addressing, and how an Aseba "set variable" (e.g. `motor.left.target`)
lands in the payload.

## Open questions / risks

- **The R&D risk is Phase 1.** The border-router precedent proves the S3↔C6
  wiring/topology, *not* that matching the Thymio's 802.15.4 link layer is quick.
  Reverse/port effort is unknown until we read the open dongle firmware.
- **Wireless Thymio required** — a plain wired Thymio II has no 802.15.4 module.
- Channel/region: the Thymio dongle exposes channel/network settings; the C6 must
  match them.

## Capture playbook (Phase 1 reverse-engineering)

With a borrowed dongle you can capture the exact frames that move the Thymio and
map Aseba commands → 802.15.4 bytes. Setup: dongle in the PC (Thymio Suite / TDM
running, Thymio **paired** and powered), and a second C6 flashed with `c6_radio`
sitting next to the robot.

1. **Find the channel.** Power everything and watch `c6_radio` hop 11..26. Jog the
   robot (`python scripts/thymio_jog.py --repl`, press `f`) and note which
   channel shows bursts. (Thymio logical channel 0/1/2 maps to one of these.)
2. **Lock the channel** for a clean capture: set `FIXED_CHANNEL` to that number in
   `c6_radio` and reflash.
3. **Baseline.** Robot idle, capture ~10 s — these are beacons / keep-alives.
4. **Stimulus, one variable at a time** (keep the robot still so sensor traffic
   stays quiet):
   - `scripts/thymio_jog.py --drive 200 200 --secs 1`   (both motors +)
   - `scripts/thymio_jog.py --drive 0 0 --secs 1`        (stop)
   - `scripts/thymio_jog.py --drive -200 -200 --secs 1`  (both motors −)
   - `scripts/thymio_jog.py --leds 0 32 0`               (top LED green)
5. **Diff** the frames across stimuli: the bytes that change between "200 200" and
   "−200 −200" encode `motor.left/right.target`; the LED one isolates `leds.top`.
   An Aseba "set variables" message carries a variable id + the value(s).
6. **Record the MAC header** (first bytes): PAN id, source/dest addresses,
   sequence number — needed to forge a frame the Thymio accepts.

Output: enough to implement the C6 TX side — build the same set-variable frame and
have the Thymio act on it, i.e. impersonate the dongle.

## References

- ESP Thread Border Router / Zigbee Gateway (S3 host + H2 RCP, single USB-C):
  <https://www.espressif.com/en/dev-board/esp-thread-border-routerzigbee-gateway-en>
- RCP interface (UART, default 460800): ESP Thread BR docs.
- XIAO pinouts: ESP32-S3 (D6/TX=GPIO43, D7/RX=GPIO44), ESP32-C6 (D6/TX=GPIO16,
  D7/RX=GPIO17) — Seeed Studio wiki.
- 802.15.4 silicon support: `framework-espidf/components/soc/<chip>/include/soc/soc_caps.h`.
