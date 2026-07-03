# Controlling Thymio robots wirelessly — without the RF dongle

Goal: drive a Thymio's **movement and LEDs** from the SoftEdIBO app, wirelessly,
reusing the gear we already have, ideally with **one USB cable to the PC**. The
air chambers we mounted on each Thymio already work over ESP-NOW (`EspRobot`);
this doc is about the Thymio's own wheeled base.

## ★ ACHIEVED: standalone dongle-free control (2026-07-03)

**A bare ESP32-C6 fully replaces the RF dongle** — it drives a Thymio over 802.15.4 with
**no dongle present at all**, proven from a cold power-cycle. The C6 forges Thymio Aseba
frames and, crucially, **keeps the link hot itself** by polling the robot the way the dongle
does. Reached by reverse-engineering the Aseba-over-802.15.4 protocol (the RF-module framing
is closed, but we replicate it verbatim from captures).

**Two things had to be true — both now solved:**
1. **The Thymio only listens while it is actively polled.** It opens its receive window in
   response to the dongle's ~10 Hz `GET_VARIABLES` poll; with nothing polling, it stops
   accepting frames (this is why unplugging the dongle killed control — its RF LED and the
   dongle's stop blinking together). So the C6 must poll continuously to hold the link open;
   a one-shot `tx` only lands while *something* (the dongle, or our own poller) already keeps
   the robot awake.
2. **The C6 radio must actually keep transmitting** — see the interrupt-leak gotcha below.

**How the C6 keeps the link hot (`thymio_link` in `rcp_c6`):** send
`{"cmd":"thymio_link","on":true,"ch":25}` once and the C6 transmits — on its own, in
`loop()` — the dongle's `GET_VARIABLES` poll plus the held motor/LED `SET_VARIABLES` every
100 ms (~10 Hz), sequence number incrementing. The PC just updates the held targets
(`{"cmd":"thymio_drive","left":L,"right":R}` / `thymio_leds`) — **instant**, no per-command
port-reset. On air, a second C6 sniffer showed the **Thymio ACKs every frame** (`02 00 <seq>`,
seq-matched), which proved the addressing/PAN/channel were right and reception was never the
problem — it was the poll cadence + a firmware radio leak + antenna.

### The protocol
The Thymio speaks **Aseba over IEEE 802.15.4**. An Aseba message is `[len][source][type]
[body]`, little-endian 16-bit. The message types:

| Aseba message | id | direction | purpose |
|---|---|---|---|
| SET_VARIABLES | 0xA00C | host→node | **write** variables (motors, LEDs, anything) |
| GET_VARIABLES | 0xA00B | host→node | **request** a variable range |
| VARIABLES | 0x9006 | node→host | the node's **reply** with variable values |

SET_VARIABLES body = `[dest_node][start_addr][value0][value1]…`. Writing motors =
SET_VARIABLES(start=0x56, [left, right]); reading the accelerometer = GET_VARIABLES(
start=0x62, count=3) then catch the node's VARIABLES reply.

### The on-air frame (captured + replicated)
```
61 88 | SEQ | 81 44 | 25 6a | 37 32 | 83 00 6a 25 32 37 11 | <Aseba message> | (FCS)
└FCF┘  seq  └PAN┘   └dst─┘  └src─┘  └── RF-module wrapper ─┘
```
- FCF `0x8861` = data frame, ACK-requested, PAN-compressed, 16-bit addrs.
- **PAN 0x4481**, dst = Thymio **0x6a25**, src = host **0x3237** (this network).
- RF-module wrapper `83 00 <dst> <src> 11` — the CC2533's proprietary header, copied
  verbatim from a capture (the only closed part; everything else is standard/Aseba).
- The radio appends the 2-byte FCS (send the PSDU without it). **SEQ must increment** or
  the Thymio drops it as a duplicate.

Spins the robot — SET_VARIABLES motor.right.target(0x57)=200:
`61 88 d7 81 44 25 6a 37 32 83 00 6a 25 32 37 11  01 00  0c a0  6a 25  57 00  c8 00`

### Thymio variable addresses (word offsets)

Confirmed live from a real Thymio-II over USB with `thymiodirect`
(`th.variable_offset(nid, name)` / `th.variable_size`), which agrees with the firmware's
empirically-found ones (motor 0x56/0x57, leds.top 0x65). See the memory
`thymio-sensors-and-sound` for the full 44-variable dump.

| variable | addr | size | | variable | addr | size |
|---|---|---|---|---|---|---|
| motor.left.target | **0x56** (86) | 1 | | acc[x,y,z] | **0x62** (98) | 3 |
| motor.right.target | **0x57** (87) | 1 | | mic.intensity | **0x79** (121) | 1 |
| leds.top r/g/b | **0x65** (101) | 3 | | acc._tap | 0x7e (126) | 1 |
| prox.horizontal[0..6] | 0x39 (57) | 7 | | button.center | 0x2c (44) | 1 |
| prox.ground.delta | 0x54 (84) | 2 | | temperature (×10 °C) | 0x76 (118) | 1 |

`sound.*` are **native functions**, not variables — triggered by bytecode (see Sound below).
`acc._tap` is set by the Thymio's default gesture program; **once you load your own bytecode
it stays 0** — for impact use raw `acc` instead (see below).

### Tooling (`scripts/`)
| script | what it does |
|---|---|
| **`thymio_link.py`** | **the reliable dongle-free drive.** Opens the C6 once, turns on the firmware `thymio_link` (the C6 polls at 10 Hz on its own), and sends instant `--left/--right/--stop/--led`, `--sound ID`/`--tone HZ DUR`, or a `--repl` jog (`f/b/l/r/s`, `snd`, `tone`). `--index`/`--addr` drive one of several Thymios. Any C6 (chip antenna is fine). |
| `thymio_move.py` | one-shot drive — only lands if the link is **already hot** (dongle driving, or a `thymio_link` running). |
| `thymio_tx.py` | send a raw frame hex (low-level replay/forge) |
| `thymio_sniff_capture.py` | raw `esp_ieee802154` promiscuous sniffer via the C6 (`--scan` finds the channel, `--debug` prints frames live) |
| `thymio_jog.py` | drive via the **RF dongle** (thymiodirect) — the working-today path + a traffic source to sniff |

### Reproduce it
1. Flash a XIAO **ESP32-C6** with `rcp_c6`
   (`pio run -d firmware/thymio_rcp -e rcp_c6 -t upload`) — it has `sniff*`, `tx`,
   `thymio_sound`, and the continuous `thymio_link`. (Chip antenna is fine — see the
   antenna note.)
2. Find the channel: drive the robot (`thymio_jog.py --drive 100 -100 --secs 180`) while
   sniffing (`thymio_sniff_capture.py --no-drive --debug --gateway <C6>`); `grep 8144` —
   the `"ch"` of a frame carrying PAN 0x4481 is the channel (was **25**).
3. Drive dongle-free, no dongle at all: `thymio_link.py --ch 25 --gateway <C6> --repl`
   (or `--left 150 --right 150`). The C6 keeps the link hot; `f/b/l/r/s` jog instantly.

Use the **stable `/dev/serial/by-id/...` path** for `--gateway` — `/dev/ttyACM*` numbers
shift whenever you plug/unplug another device (unplugging the dongle renumbered them and
looked like "it stopped working").

### Several Thymios on one C6
One C6 drives up to **4 Thymios**, each a **slot** addressed by its 802.15.4 short address
(e.g. `0x6a25`): the MAC dest is that address little-endian, the Aseba node id is the same
bytes big-endian (PAN/host/wrapper shared — verified the built frame is byte-identical to the
single-Thymio one). Firmware `thymio_link` holds a per-slot motor/LED state and polls every
active slot; `thymio_set {idx,addr}` registers a robot, `thymio_drive`/`thymio_leds`/
`thymio_sound` take an `idx`. In the app: Robot Config → each Thymio's `wireless_via: gateway`
+ **Address (C6)** → **Discover…**. That opens a **guided, dongle-free scan**
(`ThymioDiscoverDialog` + `thymio_discovery.discover_thymios`): the C6 sniffs the channel for
~20 s while you **power the Thymios on** (a Wireless Thymio announces itself at boot), and the
addresses appear **live, in first-seen order** — so turning robots on one at a time maps
address → robot. Pick one and it fills the field; no dongle, no hand-editing config. From the
CLI: `thymio_link.py --index N --addr <hex>`. (Slot 0 with no address rides a built-in default,
so the single-Thymio flow needs no address.)

> The scan is passive — it only sees a Thymio while it's *transmitting*. Powering one on
> during the scan should announce it; if nothing shows, drive it briefly (dongle or an already
> hot link) to make it talk. The sniffer pauses the link poller and zeroes motors first so the
> two don't fight for the radio.

### Movement, LEDs — proven; Sound — done; Sensors — planned
- **Proven:** full standalone control — SET_VARIABLES (motors, LEDs, any writable variable)
  over the C6's own continuous link, no dongle.
- **Sound — DONE (2026-07-03).** `sound.system`/`sound.freq`/`sound.play` are *native
  functions*, not variables, so we trigger them by loading a tiny Aseba program and running
  it: **SET_BYTECODE (`0xA001`) then RUN (`0xA003`)**. (⚠️ NOT `0xA000`/`0xA002` — those are
  GET_DESCRIPTION and **RESET**; an earlier note had them wrong.) The bytecode was captured
  from `thymiodirect`'s assembler on a real Thymio and is baked into the C6 firmware
  (`thPlaySystem`/`thPlayFreq`). Loading + running it leaves `motor.*.target` untouched, so a
  **driving robot keeps driving through a beep**. See "Sound" below.
- **Read sensors (accel, mic, prox, buttons…):** same link, other way — we already *send*
  `GET_VARIABLES` every poll; the next step is to catch + parse the node's `VARIABLES`
  (`0x9005`) reply on the C6 and forward it. Firmware, not new R&D — but note the full-space
  poll reply (128 words) exceeds one 802.15.4 frame, so poll a small range instead, e.g.
  `GET_VARIABLES(0x62, 24)` returns `acc`(0x62-0x64) + `mic.intensity`(0x79) in one frame.
- **Impact vs. touch (accelerometer) — poll raw `acc`, NOT the tap event.** The obvious idea
  (read the on-board `acc._tap` flag) does **not** work here: `acc._tap` is produced by the
  Thymio's *default gesture program*, which our loaded bytecode replaces — so it stays 0.
  Instead read raw `acc` (rest ≈ `[0,0,20]`, z = gravity): a knock ("pancada") is a large
  transient deviation, a gentle touch barely moves it. `acc` reads cleanly at ~99 Hz over USB;
  at the C6's 10 Hz a hard knock is still a clear deviation. Threshold is configurable.
- **Via the gateway:** the `thymio_link`/`tx`/`thymio_sound` commands work through the S3
  gateway to its own C6 (`{"target":"thymio",…}`). The boxed gateway C6's **chip antenna is
  plenty** (see the antenna note below) — no U.FL needed for a table/room study.

### Sound (how it works, and how to use it)
The Aseba native-call ABI: stash each argument's *value* into scratch RAM (`event.args`
@0x02, a 32-word array) and push its *address*, then `callnat _nf.<name>`. Wrap that in an
`init` event, `SET_BYTECODE` it (one message: `[dest][addr=0][words]`), then `RUN` it. The
exact bytecode (captured from `thymiodirect`'s assembler on a real Thymio-II) is baked into
the firmware — to play a different system sound only word[4] changes:

```
sound.system(N):  0003 ffff 0003 2000 000N 4002 2000 0002 c026 0000
sound.freq(f,d):  0003 ffff 0003 2000 000f 4002 2000 000d 4003 2000 0002 2000 0003 c02b 0000
```
`c026`/`c02b` = `callnat` with the native-function index (sound.system=0x26, sound.freq=0x2b
*on this firmware* — same firmware on every Thymio-II, so stable).

Use it:
- **CLI:** `thymio_link.py --sound 2` (system sound 0-7, -1 stops) or `--tone 700 30`
  (freq Hz + duration in 1/60 s); in the REPL, `snd 2` / `tone 700 30`.
- **Firmware cmd:** `{"target":"thymio","cmd":"thymio_sound","idx":0,"sys":2}` or
  `{…,"freq":700,"dur":30}`.
- **App / Python:** `robot.play_sound(system=2)` / `robot.play_sound(freq=700, duration_ms=500)`
  — on `ThymioRobot` and both links (`ThymioGatewayLink` → the C6; `ThymioLink`/`ThymioDongle`
  → the RF dongle via `th.run_asm`). `duration_ms` → the Thymio's 1/60 s unit automatically.

To re-derive the map/bytecode for a different firmware, connect a Thymio over USB and use
`thymiodirect`: `th.variable_offset/size(nid, name)`, `th.native_functions(nid)`,
`th.run_asm(nid, asm)` (Assembler → set_bytecode → run).

### Gotchas that cost a day
- **config→802.15.4-channel map is closed** (CC2533 firmware) — no formula, find it
  empirically. Landed on **ch25** (2475 MHz), nowhere near any estimate.
- **The Sonoff / OpenThread-RCP sniffer FILTERS OUT the Thymio's frames** (never showed
  PAN 0x4481). The **raw `esp_ieee802154` promiscuous** sniffer (our C6) sees them — use
  that.
- **Antenna — REASSESSED (2026-07-03): the chip antenna is plenty.** The bare-chip XIAO C6
  drives the robot solidly at **20 m through a closed door**, no U.FL. The earlier "chip
  antenna too weak / needs U.FL" was a **misdiagnosis** — the real culprits were the
  `intr_alloc` leak (radio silently dying) and not polling continuously; once those were fixed
  the chip antenna was fine. **No external antenna is needed** for a table/room study.
- **The radio silently died after ~20 `tx` — `E intr_alloc: No free interrupt inputs for
  ZB_MAC`.** `doTx` called `esp_ieee802154_enable()` on *every* tx without a matching
  `disable()`, leaking the ZB_MAC interrupt allocation; once exhausted the radio stopped
  transmitting **while `esp_ieee802154_transmit` still returned `ESP_OK`** (the lie that hid
  it for hours). Fix: enable the radio **once**, gated by `s_radioEnabled` (`radioUp()`/
  `radioDown()`). The continuous link also **paces frames** (waits for TX-done between them)
  — bursting 3-4 back-to-back drops them, same class of bug as the gateway's paced sends.
- **The Thymio only accepts frames while actively polled** — with the dongle unplugged you
  must keep the C6's `thymio_link` poll running, else its RX window closes.
- **`/dev/ttyACM*` numbers shift** when you plug/unplug devices (unplugging the dongle
  renumbered the ports and looked exactly like "control stopped working"). Use the stable
  `/dev/serial/by-id/...` path.

## Is this legal? — yes

Nothing here is illegal. This work reverse-engineers the wireless protocol of the **Thymio
robots used in this project** and builds our **own** 802.15.4 radio device to control them
without the official dongle. Concretely:

- **Authorized research on the institution's own equipment.** The Thymios and dongle belong
  to the university (LASIGE / the faculty), and are used **with authorization** for this
  research/education project — the researcher is a lawful user entitled to operate and study
  them for that purpose. This is authorized work on the institution's own devices over a
  private link between them, **not** access to any third party's hardware or network.
  Academic study and modification of equipment you are authorized to use is lawful — and the
  research/teaching context is, if anything, more protected, not less.
- **The protocol is built on open source.** Thymio/Aseba is **open-source hardware and
  software** (Mobsya, GPL). The Aseba message layer (SET_VARIABLES, GET_VARIABLES, variable
  addresses) comes straight from the public Aseba/`aseba-target-thymio2` sources. Only the
  CC2533 RF-module's on-air framing is closed, and we obtained it by **sniffing our own
  robot's traffic**, not by extracting or redistributing anyone's firmware.
- **Reverse-engineering for interoperability is a protected right in the EU.** The Software
  Directive (2009/24/EC, Art. 6) expressly permits reverse-engineering to achieve
  **interoperability** — which is exactly the purpose here (make our C6 interoperate with a
  robot we own). There is **no DRM / technical protection measure** being circumvented (the
  Thymio is open and unlocked), so anti-circumvention law doesn't apply either.
- **The radio use is within license-free rules.** We transmit standard IEEE 802.15.4 in the
  **2.4 GHz ISM band** — the same license-free band Wi-Fi, BLE, Zigbee and Thread share —
  using a **certified radio module** (the ESP32-C6 is FCC/CE-certified) at its normal low
  power. Talking to our own robot in this band is ordinary permitted ISM use; we are not
  jamming, not exceeding power limits, and not interfering with others.
- **Impersonating the dongle's address is not fraud.** The MAC/PAN addresses we reuse are on
  **our own private link between our own two devices** — there is no third party, no
  protected system, and nothing deceptive.

**The one thing that would change the analysis:** selling a dongle-replacement as a
*commercial product* would bring radio-equipment certification duties (CE/EMC/RED, FCC) for
the finished product. That is a product-compliance matter, not a legality-of-the-research
matter — and it's out of scope for this internal research tool.

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

## Updating the C6 (WiFi-OTA)

Once boxed, the C6 has no USB to the PC — it updates over **WiFi-OTA** from the S3's SoftAP:
**Tools → Update Nodes (OTA)… → the "C6 (Thymio RCP)" row**, or `scripts/ota_c6_wifi.py`. The
PC streams the image to the S3 over USB, the S3 buffers it in PSRAM and serves it at `/fw`,
and the C6 joins the AP and pulls it — no box opening, no buttons.

**AP credentials live on the gateway, in one place (2026-07-03).** The SoftAP name/password
are stored on the S3 (NVS) and are edited in the **same OTA dialog** (AP network + New
password + **Save AP**) — the separate *Tools → Gateway WiFi AP…* dialog was removed. The PC
no longer sends credentials with the update: it sends `ota_wifi` **without** ssid/pass and the
**gateway injects its own stored ones while forwarding** (`apInjectCreds`), so a renamed AP
can't silently break OTA and the password never leaves the gateway. (The OTA dialog still
auto-fills the SSID from the gateway via `get_ap`; an explicit ssid/pass passed by a script
overrides the injection.)

> **If a C6 WiFi-OTA fails at ~99% with `wifi`:** staging (PC→S3) finished but the C6 couldn't
> join the S3's AP. Usual causes: (a) the gateway landed on a different `/dev/ttyACM*` and the
> app talked to the wrong device — the box can present two USB serials (the S3 **and** a
> Thymio-II if one is plugged); pin the gateway to its `/dev/serial/by-id/...` path; (b) the
> C6's 802.15.4 radio was left busy (a prior Test Drive/Discover) — the firmware now quiets the
> poller/sniffer/radio before WiFi, but that fix has to be flashed first (power-cycle the box
> for a clean radio and OTA immediately). The node ESP-NOW flood also contends for the S3
> radio — quiet/unplug the other nodes for the C6 update if it's flaky.

> The old USB↔C6-UART flashing bridge (the `c6_bridge` gateway command + the BOOT/RST
> auto-press wires) and `scripts/flash_c6_via_s3.sh` were **removed** — WiFi-OTA replaced
> them. First-ever flash of a bare C6 is still over its own USB (`pio run -e rcp_c6 -t
> upload`); everything after is WiFi-OTA.

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
- **Phase 2 — gateway forwarding + app routing (DONE):** the S3 gateway keeps its
  ESP-NOW chamber control and adds a UART link to the C6 as a third route (the one
  gateway build `seeed_xiao_esp32s3` = `-DGATEWAY_AP -DGATEWAY_THYMIO`). PC lines
  `{"target":"thymio",...}` are forwarded to the C6 over UART; the C6's replies come
  back tagged `{"source":"thymio",...}`. **The app drives the Thymio through this path**
  via `ThymioGatewayLink` (Robot Config → `wireless_via: gateway`, per-robot `channel` +
  `address`); the RF dongle (`ThymioLink`) stays as an alternative transport.
- **Movement authoring (later, optional):** add movement verbs to the activity
  catalog/editor (note: the scripted-activity runtime is per-skin, while movement is
  per-robot — a new addressing model).

**Works today (dongle, no Suite) — confirmed on hardware:** `ThymioRobot` drives
motors/LEDs through an injected `ThymioLink` that uses **thymiodirect** straight to the
RF dongle's serial port — no Thymio Device Manager / Thymio Suite (the desktop Suite is
no longer distributed for Linux). Opt-in via config `wireless: true` (Robot Config →
"Drive wheels wirelessly"); the dongle is auto-detected (or set `dongle_port`).
`pip install thymiodirect`. `scripts/thymio_jog.py` is a standalone smoke-test/jog and
the command source for the capture playbook below. (Was tdmclient→TDM; switched to
thymiodirect when the desktop Suite/TDM became unavailable.)

**Several Thymios on one dongle:** the wireless Aseba network carries many robots over a
single dongle/radio, each a distinct **node id** on the same channel + network id. So
the serial connection lives in one shared **`ThymioDongle`** (owned by the main window),
and each robot's `ThymioLink` is a thin handle bound to its node id — `set_motors`/
`set_leds` relay to that node. Set a distinct **Wireless node id** per Thymio in Robot
Config (`node_id` in settings; `0` = auto-bind the first node, fine for a single robot).
Discover the ids with `scripts/thymio_jog.py --list`, and drive one with `--node N`. All
three Thymios pair to the **same** dongle network but with **different node ids** (set in
the Thymio Suite network configurator on Windows — the Linux Suite is gone). thymiodirect
has no public `disconnect`, so `ThymioDongle.close()` stops its (non-daemon) asyncio +
serial threads explicitly, else the app hangs on exit.

**Several Thymios on one dongle.** A single RF dongle relays to many Thymios at once —
each is one **node id** on the same channel + network id. So the serial connection lives
in `ThymioDongle` (owns the one thymiodirect `Thymio`, discovers all nodes, relays
reads/writes by node id, thread-safe); each `ThymioLink` is a thin handle bound to one
node id on it. The window builds **one shared `ThymioDongle`** for all wireless Thymios
and closes it on exit (thymiodirect has no public `disconnect`, and its threads are
non-daemon — `ThymioDongle.close()` stops the asyncio loop + serial reader so the app
doesn't hang). Per robot set **Wireless node id** in Robot Config (`node_id`, `0`/blank =
bind the first node — fine for a single Thymio); give each robot a **distinct** id.
Pairing must put them on the **same network, distinct node ids** (Thymio Suite → Wireless
Network Configurator, Windows/Mac only). Discover/verify ids with
`python scripts/thymio_jog.py --list`, then jog one with `--node <id>`.

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
  firmware. Reference: Rétornaz et al., *Seamless Multi-Robot Programming… Wireless
  Thymio II*, 2013.
- **RF chip = Texas Instruments CC2533** (2.4 GHz IEEE 802.15.4 SoC, 8051 core +
  radio), confirmed from the BOM in `Mobsya/electronics-thymio2-rf` (`thymiorf/` +
  `usb-dongle/` are hardware only). The module and dongle share this chip. **Standard
  802.15.4 PHY ⇒ the C6's `esp_ieee802154` can see and send its frames.** The CC2533
  firmware (the actual over-air link protocol) is **not public**, so the frame format
  is reverse-engineered by sniffing, not from source.
- There is a **pairing + shared network/PAN id** scheme: dongle and robot must share
  the network id and be on the same channel.

**Decode strategy — sniffer-first.** Rather than fully reverse the RF firmware on
paper, capture real frames with the C6's **sniff mode** (`rcp_c6`'s `sniff_start`, via
`scripts/thymio_sniff_capture.py` — it hops 11–26, so it finds the channel regardless of
the 0/1/2 mapping) and correlate the bytes with the Aseba message structure. The unknowns
the capture resolves directly: the actual 802.15.4 channel, PAN id, addressing, and how an
Aseba "set variable" (e.g. `motor.left.target`) lands in the payload.

## Open questions / risks

- **The R&D risk is Phase 1.** The border-router precedent proves the S3↔C6
  wiring/topology, *not* that matching the Thymio's 802.15.4 link layer is quick.
  Reverse/port effort is unknown until we read the open dongle firmware.
- **Wireless Thymio required** — a plain wired Thymio II has no 802.15.4 module.
- Channel/region: the Thymio dongle exposes channel/network settings; the C6 must
  match them.

## Capture playbook (Phase 1 reverse-engineering)

With the borrowed dongle you can capture the exact frames that move the Thymio and map
Aseba commands → 802.15.4 bytes. **No second C6 / no separate sniffer firmware:** the
boxed C6 already runs the sniff-capable RCP, so we sniff *through the gateway*. Its
promiscuous 802.15.4 frames come out `{"type":"frame",...}` over the S3's USB, and
`scripts/thymio_sniff_capture.py` logs them **on the same timeline** as the moves it
drives through the dongle — the correlation is done for you.

Setup: **(a)** OTA the sniff-capable RCP onto the C6 (Tools → Update Nodes (OTA)… → the
"C6 (Thymio RCP)" row, WiFi — or `scripts/ota_c6_wifi.py`); **(b)** dongle in the PC,
Thymio **paired** + powered; **(c)** close the main app (it holds the gateway port).

1. **Find the channel.** Hop while driving — frames cluster on the Thymio's channel:
   `python scripts/thymio_sniff_capture.py --out cap_hop.jsonl`
   Inspect `cap_hop.jsonl`: which `ch` has `frame`s around each `cmd`?
2. **Lock the channel** for a clean capture (no reflash — it's a command):
   `python scripts/thymio_sniff_capture.py --ch <N> --out cap.jsonl`
   The script drives a scripted sequence (baseline → LED r/g/b → forward/back/spin,
   each held `--secs`) and timestamps every command next to the frames.
3. **Diff** the frames per command: bytes that change between `forward` (150 150) and
   `backward` (−150 −150) encode `motor.left/right.target`; the `led_*` steps isolate
   `leds.top`. An Aseba "set variables" message carries a variable id + the value(s) —
   e.g. look for `0x96` (150) or its little-endian 16-bit form in the payload.
4. **MAC header** (first bytes of each frame): FCF, sequence number, PAN id, src/dst
   addresses — needed to forge a frame the Thymio accepts. Frames are capped to 96
   payload bytes over the relay (the true length is in `len`); bump `SNIFF_MAX_BYTES`
   (C6) + the S3 relay `line[]` if you need full 127-byte frames.
5. **`--no-drive`** logs frames while you jog by hand; **`sniff_ch`/`sniff_stop`** are
   plain gateway commands (`{"target":"thymio","cmd":"sniff_stop"}`) if driving the C6
   directly. The RCP disables the radio on `sniff_stop` and before any `ota_wifi`, so
   WiFi and 802.15.4 never fight over the shared antenna.

(Sniffing lives in the one `rcp_c6` build now — the boxed C6 sniffs on command, so no
separate sniffer firmware and the WiFi-OTA route stays intact. A spare C6 on USB running
`rcp_c6` sniffs the same way, read directly over its USB.)

Output: enough to implement the C6 TX side — build the same set-variable frame and
have the Thymio act on it, i.e. impersonate the dongle.

## References

- ESP Thread Border Router / Zigbee Gateway (S3 host + H2 RCP, single USB-C):
  <https://www.espressif.com/en/dev-board/esp-thread-border-routerzigbee-gateway-en>
- RCP interface (UART, default 460800): ESP Thread BR docs.
- XIAO pinouts: ESP32-S3 (D6/TX=GPIO43, D7/RX=GPIO44), ESP32-C6 (D6/TX=GPIO16,
  D7/RX=GPIO17) — Seeed Studio wiki.
- 802.15.4 silicon support: `framework-espidf/components/soc/<chip>/include/soc/soc_caps.h`.
