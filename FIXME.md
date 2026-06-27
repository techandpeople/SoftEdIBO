# FIXME — Actuator (pumps + valves) logic, end to end

Status snapshot of how an inflate/deflate (and the manual pump/valve controls)
travels from a user action all the way to the GPIO pins on the **`node_direct`**
board. **The actuator chain has never been successfully tested end-to-end**, so
this documents the *current code as it stands* (not any diff/regression) — every
layer is examined fresh as a candidate for "why nothing actuates correctly."
File references are `path:line`.

Scope: the **direct** actuator board (`firmware/node_actuator/src/direct/`). The
multiplexed board (`firmware/node_actuator/src/multiplexed/`) mirrors the same
protocol and the same shared fill policy; it is referenced only where it helps
confirm a pin/convention.

---

## STATUS (2026-06-26)

Pumps physically work; the **control logic** was the problem. Confirmed root
cause + fixes applied (all PC-side except a comment):

| # | Problem | Status |
|---|---------|--------|
| A | **STOP ALL didn't stop** — only stopped after the 5 s safety timeout (the symptom actually observed). `_stop_all` sent `stop` then `resume` immediately, discarding the firmware's continuous "stay-off" enforcement; if the single `stop` ESP-NOW frame dropped, nothing cut the actuator. | **FIXED** — `_stop_all` now latches (sends `stop` ×3, no auto-resume) and re-arms lazily on the next action / on close. `src/gui/test_actuators_dialog.py` |
| B | **Per-slot Inflate/Deflate barely moved** — sent `value=255`, which the node ignores (reads `delta`, falls back to 10 %). | **FIXED** — now `delta=100`. `test_actuators_dialog.py` |
| C | **Fill-time calibration always timed out at 5 s** — same `value=255` bug in the calibrator's inflate, so the chamber stalled at ~10 % and never reached the 95 % target. | **FIXED** — now `delta=100`. `src/gui/fill_calibration_dialog.py:180` |
| D | Misleading pump-button UI; stale `pins.h` pump comment. | **FIXED** — removed `_update_pump_button`; `pins.h` comment now matches the array (PUMP1=IO32). |

Hardware confirmed by the user: `PUMP_PINS = {32,33}` correct; pumps work;
deflate uses the **PUMP2 vacuum** pump (partial negative pressure — compresses
the skin for the *wrinkles* effect, not full vacuum).

**Next step chosen: validate these fixes on hardware first** (see §10), *then*
build the time-based calibrated control redesign (§11).

---

## 0. The layers (who talks to whom)

```
USER (GUI button / activity script)
   │
   ▼
[PC] Skin / ESP32Controller / TestActuatorsDialog   (Python)
   │   builds a command dict  {target, cmd, chamber, …}
   ▼
[PC] ESPNowGateway.send()                            src/hardware/espnow_gateway.py
   │   json.dumps(...) + "\n"  → USB serial
   ▼
[Gateway ESP32] processLine()                        firmware/gateway/src/main.cpp (IDF, XIAO-C6)
   │                                                  or main_arduino.cpp (WROOM)
   │   parse JSON, strip "target", esp_now_send(mac, payload)
   ▼
[Node ESP32] onReceived()                            firmware/node_actuator/src/direct/main.cpp:51
   │   commands::parseAndQueue()  → ring buffer
   ▼
[Node] loop(): cmd_queue::pop → commands::process()  direct/commands.h:83
   │   mutates chambers::state[n]  +  recalcPumps()
   ▼
[Node] chambers:: setValve() / ledcWrite()           direct/chambers.h
   ▼
GPIO  →  ULN2803A (valves)   /   DRV3297 (pumps)      direct/pins.h
```

There are **two independent control philosophies** living on the node at once
(this matters — see §7):

1. **Closed-loop state machine** — `inflate` / `deflate` / `set_pressure` /
   `hold`. Each chamber has a `state` (IDLE/INFLATING/DEFLATING) and a target;
   `recalcPumps()` derives the pump duty from the set of chamber states. Valves
   are opened/closed by `beginInflate`/`beginDeflate`/`stop`.
2. **Manual / raw layer** — `pump_manual` / `valve_manual`. Writes the pump LEDC
   duty and the valve GPIO **directly**, bypassing the state machine, tracked in
   a *separate* set of arrays (`manualPumpOn[]`, `manualValveOn[]`).

Both layers write the **same** hardware (PUMP1/PUMP2 LEDC channels, the valve
pins). Nothing reconciles them. Whoever wrote last wins.

---

## 1. Entry point A — "Test Actuators" dialog (manual dev path)

File: `src/gui/test_actuators_dialog.py`. Opened from the Skin config dialog
(`src/gui/skin_config_dialog.py:895`). Commands go **straight to the gateway**,
bypassing the Skin/robot layer, so they act on the live (possibly unsaved) node.

The dialog shows, per chamber/slot:
- **Inflate / Deflate** (per slot)  → state-machine commands
- **Inflate Valve / Deflate Valve** toggles  → `valve_manual` (raw)
- a pressure ADC label

…plus a global **Pump Control** group:
- **Inflate Pump / Deflate Pump** toggles  → `pump_manual` (raw)
- **STOP ALL**  → `stop` + `resume`

### Button → wire command

| UI action | method | wire command sent | notes |
|---|---|---|---|
| "Inflate" (slot) | `_inflate_slot` `:201` | `{cmd:"inflate", chamber:slot, value:255}` | **⚠ `value` is ignored by firmware** — see §6.1 |
| "Deflate" (slot) | `_deflate_slot` `:205` | `{cmd:"deflate", chamber:slot}` | no delta → firmware default 10% |
| "Inflate All / Deflate All" | `_inflate_slots`/`_deflate_slots` `:209` | loops the per-slot command | |
| "Inflate/Deflate Valve" toggle | `_toggle_valve` `:217` | `{cmd:"valve_manual", chamber, side, open}` | side 0=inflate,1=deflate |
| "Inflate/Deflate Pump" toggle | `_toggle_pump` `:235` | `{cmd:"pump_manual", pump, on}` | pump 0=inflate,1=deflate |
| "STOP ALL" | `_stop_all` `:261` | `{cmd:"stop"}` then `{cmd:"resume"}` | `stop`=firmware `emergencyStopAll` (pumps off, valves closed, chambers→IDLE); `resume` re-arms |
| LED ring | `_send_led` `:190` | `{cmd:"set_led", …}` | not an actuator |

Note: pressing per-slot **Inflate/Deflate also flips the global pump button UI to
ON** (`_inflate_slot:203`, `_deflate_slot:207`) — but that is a *UI guess*. The
firmware drives the pump from chamber state, and auto-stops the pump when the
target/time/HARD_MAX is reached; the button is never corrected back to OFF, so
the displayed pump state desyncs from reality.

---

## 2. Entry point B — runtime / activity path (closed loop)

The "normal" path used during a session/activity:

```
Activity / GUI monitor / robot
   skin.inflate(local_idx, delta)            src/hardware/skin.py:226
   skin.deflate(local_idx, delta)            src/hardware/skin.py:232
   skin.set_pressure(local_idx, value)       src/hardware/skin.py:238
        │  Skin._apply()                      src/hardware/skin.py:282
        │    updates the PC-side AirChamber model (target/state)
        │    chooses time-based vs pressure-based fill (calibration)
        ▼
   ESP32Controller.inflate/deflate/set_pressure   src/hardware/esp32_controller.py:43
        ▼
   gateway.send(mac, "inflate", chamber=slot, delta=…, [ms=…])
```

Callers of `skin.inflate/deflate/set_pressure`:
- `src/activities/scripted_activity.py:439` (behaviour engine, via `set_pressure`)
- `src/activities/organ_swap.py:580,407,335,614`
- `src/gui/monitor/chamber_widget.py:57,59` (live monitor +/- buttons)
- `src/gui/robot_config_dialog.py:99,104`
- `src/robots/esp_robot.py:151-174`, `src/robots/simulated_robot.py:172-176`

Key difference from path A: path B sends `delta=` (the firmware key the node
actually reads) and, for **calibrated** chambers, an `ms=` time-based fill
window scaled by concurrent load (`Skin._apply:301-307`,
`src/hardware/fill_scaling.py`). Path A (the test dialog) never sends `ms`.

---

## 3. PC → gateway transport

`ESPNowGateway.send()` `src/hardware/espnow_gateway.py:79`:
- builds `{"target": mac, "cmd": command, **kwargs}`, `json.dumps`, appends `\n`,
  writes to the serial port.
- Returns `False` (drops the command) if not connected.
- The read thread (`_read_loop` `:202`) accumulates bytes, splits on `\n`,
  **discards the first partial fragment**, and dispatches each complete JSON line
  to callbacks. Incoming `type:"error"` lines are now logged
  (`_dispatch_line:190`, recently added).

---

## 4. Gateway firmware (relay)

Two interchangeable builds, same protocol:
- IDF / XIAO-C6: `firmware/gateway/src/main.cpp` — `processLine()` `:269`
- Arduino / WROOM: `firmware/gateway/src/main_arduino.cpp` — `processLine()` `:58`

Logic:
1. Parse the JSON line. **If unparseable** → emit
   `{"type":"error","reason":"bad_cmd_json","len":…,"raw":…}` back to the PC
   (recently added in both — `main.cpp:271`, `main_arduino.cpp:60`) instead of
   silently dropping. This is the diagnostic for serial byte-loss / truncation.
2. If a `"target"` MAC is present → `ensurePeer(mac)`, strip `"target"`,
   `esp_now_send` the remaining JSON to the node (payload must be ≤250 B).
3. If **no** `"target"` → treat as a gateway-local command (`get_ap`/`set_ap`).

Node → PC direction: `onReceived`/`rxTask` wraps the node's JSON, adds
`"source": mac`, writes the line to USB.

---

## 5. Node firmware — `node_direct`

### 5.1 Receive + queue
`main.cpp:51 onReceived` → `commands::parseAndQueue()` `commands.h:171`:
parses JSON, maps `cmd` string → a `cmd_queue::Cmd`, and `push()`es it onto a
16-slot lock-free ring (`firmware/common/cmd_queue.h`). `set_led`, `rebaseline`,
`configure` are handled **inline** (not queued).

`parseAndQueue` field mapping (the authoritative protocol):

| `cmd` | type | reads | → Cmd fields |
|---|---|---|---|
| `inflate` | CMD_INFLATE | `chamber`, `delta`(def 10), `ms`(def 0) | `chamber`,`param`,`fill_ms` |
| `deflate` | CMD_DEFLATE | `chamber`, `delta`(def 10), `ms`(def 0) | `chamber`,`param`,`fill_ms` |
| `set_pressure` | CMD_SET_PRESSURE | `chamber`, `value` | `chamber`,`param` |
| `set_max_pressure` | CMD_SET_MAX | `chamber`, `value`(kPa) | `chamber`,`param_kpa` |
| `set_min_pressure` | CMD_SET_MIN | `chamber`, `value`(kPa) | `chamber`,`param_kpa` |
| `hold` | CMD_HOLD | `chamber` | `chamber` |
| `stop` / `resume` | CMD_STOP/RESUME | — | latch |
| `valve_manual` | CMD_VALVE_MANUAL | `chamber`, `side`(0/1), `open`(0/1) | `chamber`,`param`,`cfg_chambers` |
| `pump_manual` | CMD_PUMP_MANUAL | `pump`(0/1), `on`(0/1) | `param`,`cfg_chambers` |
| `ping` | CMD_PING | — | → pong |

> ⚠ `inflate` reads **`delta`**, NOT `value`. A payload with only `value=…` falls
> back to `delta=10`. This directly affects the Test dialog — see §6.1.

### 5.2 Process the command
`loop()` drains the queue → `commands::process()` `commands.h:83`:

- `CMD_STOP`  → `chambers::stopped=true; emergencyStopAll(); sendAck; sendPumps`
- `CMD_RESUME`→ `chambers::stopped=false; sendAck`
- **If `chambers::stopped`, every other command is dropped** (`commands.h:95`).
- `CMD_INFLATE` `:103`:
  - `ms>0` → `beginInflate(n, duty=255, target=max_kpa, fill_ms)` (time-based).
  - else → `delta = (max_kpa-min_kpa)*param/100`, `target = cachedKpa + delta`
    (capped at max), `beginInflate(n, 255, target)` (pressure-based).
- `CMD_DEFLATE` `:116`: `target = cachedKpa - delta` (floored at min_kpa),
  `beginDeflate(n, target, fill_ms)`.
- `CMD_SET_PRESSURE` `:122`: convert % → kPa, then inflate-or-deflate toward it.
- `CMD_SET_MAX/MIN`: update the per-chamber ceiling/floor.
- `CMD_HOLD`: `stop(n) + recalcPumps()`.
- `CMD_VALVE_MANUAL` `:154`: `setManualValve(chamber, side, open)`.
- `CMD_PUMP_MANUAL` `:160`: `setManualPump(pump, on)`.

### 5.3 Chamber state machine + actuation
File: `firmware/node_actuator/src/direct/chambers.h`.

- `beginInflate()` `:101`: clamp target, close deflate valve, set state=INFLATING,
  open inflate valve, `recalcPumps()`.
- `beginDeflate()` `:160`: close inflate valve, set state=DEFLATING, open deflate
  valve, **always arm a time deadline** (`deflateUntil`, default cap 5 s — the
  gauge sensor is blind below atmosphere so pressure can't stop a vacuum runaway),
  `recalcPumps()`.
- `stop(n)` `:81`: close both valves, reset the Chamber struct (keeps max/min).
- `recalcPumps()` `:60`: scan all chambers →
  - `PUMP1` (LEDC ch 0, the **inflate** pump) = max duty across INFLATING chambers.
  - `PUMP2` (LEDC ch 1, the **deflate** pump) = 255 if ANY chamber DEFLATING else 0.
  - Pumps are **shared across all 3 chambers** (one inflate pump, one vacuum pump).

`loop()` (`main.cpp:93`) runs these every iteration:
- pressure read every 200 ms; closed-loop cutoff: INFLATING stops at
  `target_kpa`/`max_kpa`, DEFLATING stops at `target_kpa` (`main.cpp:118-135`).
- `fillTimeTick` / `deflateTimeTick` — time-window cutoffs (shared policy in
  `firmware/common/fill_control.h`).
- `maintainTick` — idle leak top-up of a held chamber.
- `manualSafetyTick` `chambers.h:228` — dead-man auto-off of manual pump/valve
  after `MANUAL_MAX_ON_MS = 5000`, plus HARD_MAX/HARD_MIN cutoffs.
- `actuationWatchdog` — force-stop any chamber stuck actuating > 10 s.
- status + `sendPumps()` broadcast every 500 ms.

### 5.4 Manual layer
`setManualPump(idx,on)` `:204`: writes `ledcWrite(PUMP1/PUMP2, on?255:0)` directly
and timestamps it. `setManualValve(ch,side,open)` `:211`: closes the opposite side
of that chamber, then `setValve()` directly, tracked in `manualValveOn[]`.

`manualSafetyTick` only ever turns manual actuators **off** (dead-man + hard
limit). It does **not** re-assert them, and nothing stops `recalcPumps()` from
overwriting a manually-set pump duty (see §7).

### 5.5 Hardware mapping
`firmware/node_actuator/src/direct/pins.h`:
- `PSENSOR_PINS[3] = {39,34,35}` — XGZP6847A gauge sensors per chamber.
- `PUMP_PINS[2] = {32,33}` via DRV3297. `chambers.h:294` attaches
  `PUMP_PINS[0]→PUMP1_LEDC_CH(0)`, `PUMP_PINS[1]→PUMP2_LEDC_CH(1)`.
- `VALVE_PINS[6] = {25,4,16,17,18,19}` via ULN2803A. Chamber *i*:
  inflate = `VALVE_PINS[i*2]`, deflate = `VALVE_PINS[i*2+1]`.
- Valve HIGH = open, pump LEDC duty 0–255.

---

## 6. ⚠ Current-code discrepancies / candidate faults

Examined as current code (never-worked bring-up), ordered roughly by how likely
each is to make the actuators misbehave.

### 6.1 Test dialog "Inflate" sends `value=255`, which the firmware ignores
`src/gui/test_actuators_dialog.py:202`

```python
self._gateway.send(self._mac, "inflate", chamber=slot, value=255)
```

The node's `inflate` reads **`delta`** (`commands.h:180`), not `value`. With no
`delta` in the payload it defaults to **10**, so each "Inflate" click only raises
the chamber by ~10 % of its (max−min) span above the *current* reading, then
auto-stops. The intended `255` ("full") never reaches the node. This alone makes
the per-slot Inflate look like "barely does anything / doesn't work."
`_deflate_slot` (`:206`) sends no delta either → 10 % per click.
**Fix:** send `delta=100` (or use `set_pressure`, or a time-based `ms=` fill).

### 6.2 Pressure-based inflate depends on a working, sane pressure sensor
`commands.h:110-112`. Without `ms`, inflate is closed-loop:
`target = cachedKpa[n] + delta` (capped at `max_kpa`), and `loop()` stops as soon
as `kpa >= target || kpa >= max_kpa` (`main.cpp:124`). Consequences if the
XGZP6847A reading is wrong (unplugged, mis-wired, wrong pin, noisy):
- reads **high/full** → `target` is already ≤ current → the cutoff fires almost
  immediately → valve barely opens, pump barely runs → "nothing happens";
- `max_kpa` default is only **8 kPa** (`chambers.h:14`); a sensor that floats near
  that ceiling will refuse to inflate at all.

`PSENSOR_PINS = {39,34,35}` are input-only ADC1 pins (`pins.h:15`); confirm the
sensors actually read plausible kPa at rest (the Test dialog pressure label / the
500 ms `status` broadcast shows the % — verify it sits near 0 % when deflated).
The calibrated **time-based** path (`ms=`) sidesteps this, but the Test dialog
never uses it; only the activity path with a calibrated `fill_time_ms` does.

### 6.3 Pump pin mapping — array probably right, comment is stale; bench-verify
`firmware/node_actuator/src/direct/pins.h:21`

```c
constexpr int PUMP_PINS[2] = {32, 33};
//                           PUMP1=IO33 (J2_8), PUMP2=IO32 (J2_7)   ← STALE: contradicts the array
```

`chambers.h:294` binds `PUMP_PINS[0]→PUMP1_LEDC_CH(0)` and
`PUMP_PINS[1]→PUMP2_LEDC_CH(1)`, and in the direct firmware **PUMP1 = inflate**,
**PUMP2 = vacuum/deflate** (`recalcPumps` `chambers.h:60`). So with `{32,33}`:
inflate→IO32, vacuum→IO33. The **multiplexed** board's pin file (marked "verified
from the schematic") declares `PUMP1=IO32, PUMP2=IO33` (`multiplexed/pins.h:38`),
so the direct array agrees with that convention — it's the **direct comment that
is stale** (it claims PUMP1=IO33). Mapping is likely correct; still, since the
board was never proven, **bench-confirm which physical pump (pressure vs vacuum)
sits on IO32 vs IO33** and then fix the comment. A swap here = inflate runs the
vacuum pump and vice-versa.

### 6.4 The DRV3297 is driven by PWM only — confirm its enable/sleep is asserted
Both boards drive each pump with a **single PWM line** and nothing else
(`chambers.h:287-298`, `multiplexed/pumps.h:21-27` — "Each pump has a single PWM
input"). The firmware never toggles an enable / nSLEEP / nFAULT line. If the
DRV3297's enable is **not** hardwired active on the PCB, the motor never spins no
matter what the PWM does — a classic "never worked at all." This is by-design in
firmware (no enable GPIO is defined), so it must be guaranteed in hardware:
**verify on the board/schematic that the DRV3297 enable is tied active** (or, if
it needs a GPIO, that GPIO is currently undriven and must be added).

### 6.5 Two control layers write the same hardware with no arbiter
See §7 — the per-slot closed-loop Inflate/Deflate and the manual pump/valve
toggles fight over the same LEDC channels and valve pins. In the Test dialog this
makes it easy to leave the hardware in a contradictory state and conclude "it
doesn't work."

### 6.6 Pump-button UI is a guess, not hardware truth
`_inflate_slot:203` / `_deflate_slot:207` flip the global pump button to "ON" when
you fire a per-slot inflate/deflate, but the firmware auto-stops the pump (target
/ time / HARD_MAX) and the button is never corrected. So the UI can show "Pump:
ON" while the pump is actually off (and vice-versa). The real pump duty is in the
500 ms `{"type":"pumps","inf":…,"def":…}` broadcast (`commands.h:51`) — trust that
over the button.

### Diagnostics available in the current firmware
- `sendAck()` / `sendPumps()` + the `type:"pumps"` 500 ms broadcast
  (`commands.h:41-58`, `main.cpp:111,161`) — shows the real pump LEDC duty.
- Gateway emits `bad_cmd_json` on unparseable PC lines
  (`gateway/src/main.cpp:271`, `main_arduino.cpp:60`); PC logs `type:"error"`
  (`espnow_gateway.py:190`). Watch them in Robot panel → **Serial Monitor**.

---

## 7. ⚠ Structural issue — the two control layers fight

The closed-loop layer (`recalcPumps` / `setValve` via the state machine) and the
manual layer (`setManualPump` / `setManualValve`) both write the same PUMP1/PUMP2
LEDC channels and the same valve pins, with **no coordination**:

- Manual pump ON, then a chamber finishes inflating → `recalcPumps()` writes the
  inflate pump duty to 0, silently overriding the manual ON. (And vice-versa: a
  chamber starting to inflate re-spins a pump you manually turned off.)
- `manualValveOn[]` tracking is separate from chamber `state`; `beginInflate`/
  `stop` move valves without updating the manual arrays, and `setManualValve`
  moves valves without updating chamber state.
- In the Test dialog you can simultaneously hold a manual valve open, toggle a
  manual pump, AND fire the per-slot closed-loop Inflate — three commands driving
  the same hardware to contradictory states within the same chamber.

This is not a regression from the recent diff, but it makes the Test dialog
behaviour hard to predict and is worth deciding on: e.g. have the manual layer
and the state machine share one arbiter, or make the dialog mode-exclusive
(manual XOR closed-loop), so a test session can't leave the two layers fighting.

---

## 8. How to bisect the chain (PC → gateway → node → hardware)

Isolate which layer fails, top to bottom, using Robot panel → **Serial Monitor**
(stays on top) to watch `tx`/`rx`.

1. **PC → gateway:** fire a per-slot **Inflate**. `tx` should show
   `{"cmd":"inflate","chamber":N,"value":255}`. If you instead see a
   `{"type":"error","reason":"bad_cmd_json",…}` in `rx`, the serial line is being
   garbled/truncated. (Note the `value` key — confirms §6.1: the node treats it as
   delta=10.)
2. **Gateway → node:** does the node ever reply? The node broadcasts `status`
   every 500 ms once it knows the gateway. No `rx` from the node at all ⇒ flash the
   **gateway** first (see memory: "no nodes found" is usually a dead gateway),
   wrong MAC, or node not powered/flashed.
3. **Node logic:** watch the `{"type":"pumps","inf":D,"def":D}` broadcast. After an
   Inflate, `inf` (inflate-pump duty) should rise; after Deflate, `def` should hit
   255. If `inf`/`def` stay 0 ⇒ the command isn't producing actuation (chamber
   never entered INFLATING — e.g. §6.2 pressure cutoff fired immediately, or the
   node is latched `stopped`).
4. **Node → hardware:** `inf`/`def` rises in the broadcast but the pump doesn't
   spin ⇒ hardware: DRV3297 enable not asserted (§6.4), wrong pump pin (§6.3), or
   power. Pump spins but it's the **wrong** one ⇒ pin/inflate-vacuum swap (§6.3).
5. **Valves:** fire a manual **Inflate Valve** toggle
   (`tx {"cmd":"valve_manual","chamber":N,"side":0,"open":1}`) and listen/feel for
   the solenoid; it dead-man auto-closes after 5 s (`MANUAL_MAX_ON_MS`). No click ⇒
   valve pin/ULN2803A wiring or power.
6. **Manual pump:** `tx {"cmd":"pump_manual","pump":0,"on":1}` → `rx pumps.inf`
   should jump to 255 and drop after 5 s. This drives the pump with zero
   dependence on the pressure sensor or chamber state — the cleanest pump test.

---

## 9. Quick checklist to fix

- [ ] **Bench-confirm DRV3297 enable is asserted** and that IO32/IO33 map to the
      expected physical pumps (§6.3, §6.4). Fix the stale `pins.h` comment.
- [ ] Make the Test dialog inflate actually inflate: send `delta=100` (or
      `set_pressure`) instead of the ignored `value=255` (§6.1).
- [ ] Sanity-check the pressure sensors read ~0 % when deflated (§6.2); a bad
      reading makes pressure-based inflate self-cancel.
- [ ] Decide whether the manual and closed-loop layers should be reconciled or
      made mutually exclusive in the Test dialog (§6.5 / §7).
- [ ] Use the `pumps`/`ack`/`bad_cmd_json` diagnostics (§8) to localise the
      failing layer before changing code.

> Most of §9 is now done — see the STATUS table at the top. §8 (bisection) and
> §10 (validation) are the live checklists.

---

## 10. Validation plan (do this first)

The fixes A–C in STATUS are PC-side; the STOP fix also relies on firmware that
already has `stop`/`resume`/`emergencyStopAll` + the continuous enforcement. To
also see the `ack`/`pumps`/`bad_cmd_json` diagnostics, flash **current** firmware.

1. **Rebuild + flash current firmware** (gateway + the direct node):
   ```bash
   cd firmware/gateway      && pio run -e <your_env> -t upload
   cd firmware/node_actuator && pio run -e direct_debug -t upload   # debug => Serial logs
   ```
   (Or OTA via Tools → Update Nodes, after `scripts/build-firmware.sh` rebuilds
   the bundled `firmware/node_*/firmware-*.bin`.)
2. Run the app, connect the gateway, open **Robot panel → Serial Monitor**.
3. **STOP test (the reported bug):** open Test Actuators, toggle **Inflate Pump**
   ON (or fire a Deflate). Click **STOP ALL**. Expect, immediately (not after 5 s):
   - `tx {"target":…,"cmd":"stop"}` (×3)
   - `rx {"type":"ack","cmd":"stop"}`
   - `rx {"type":"pumps","inf":0,"def":0}` and the pump audibly stops.
   Then fire any Inflate/Deflate → expect a single `tx …"cmd":"resume"` first
   (lazy re-arm), then the action.
4. **Inflate/Deflate test:** per-slot Inflate now sends `delta=100` → the pressure
   label / `status` % should climb toward max; Deflate should bring it down.
5. **Calibration test:** Tools → Calibrate Fill Times → Calibrate one chamber.
   It should now reach the target and record a real `… ms` instead of `≥5000 ms`.
6. If STOP still lags: check the `ack` — **no `ack` ⇒ the `stop` frame isn't
   reaching the node** (ESP-NOW/serial loss or wrong MAC), not a logic bug.

---

## 11. Agreed redesign — time-based calibrated control (after validation)

Decisions captured from the user (2026-06-26). This replaces the closed-loop /
heuristic-scaling approach with measured, time-based actuation.

- **Time-based, not pressure-loop.** Inflate/deflate run for a *calibrated time*
  to hit a target; the gauge sensor is reference-for-calibration + occasional
  resync, not the live control loop (it is laggy / blind below atmosphere).
- **Calibrate inflate AND deflate (vacuum).** PUMP2 (vacuum) deflates to a
  *partial* negative pressure — enough to compress the skin into the *wrinkles*
  look, not full vacuum. So deflate needs its own calibrated time per target,
  mirroring inflate. (Maps onto the existing `min_kpa` negative-floor concept.)
- **Measure every concurrency combination.** Pumps are shared, so fill time
  depends on how many chambers actuate together. Calibrate each non-empty subset
  of chambers: for 3 chambers → {0},{1},{2},{0,1},{0,2},{1,2},{0,1,2} (2ⁿ−1).
  This *replaces* the `effective_fill_ms` `max(1, active/pumps)` heuristic
  (`src/hardware/fill_scaling.py`) with a measured lookup keyed by the active set.
- **Runtime "% full" = % of calibrated ms elapsed** (elapsed_ms / total_ms for
  the current target & active-set), **not** the sensor reading…
  - …**plus an optional, configurable idle resync:** when a chamber has been idle
    for more than **X seconds** (X configurable; the whole behaviour toggleable),
    read the pressure sensor and correct the displayed/assumed fill level — to
    catch leaks that the open-loop time model can't see.

### Work breakdown (proposed, for after HW validation)
1. Calibration UI/core: extend `FillTimeCalibrator` + the dialog to (a) drive all
   2ⁿ−1 active-set combinations, (b) also calibrate deflate/vacuum, storing a
   lookup (per active-set → fill_ms and deflate_ms) in settings.
2. Calibration measurement: optionally characterise the pressure-vs-time curve
   (sample while filling) so partial targets map to a time, not just the endpoint.
3. Runtime: replace `effective_fill_ms` with the measured active-set lookup;
   track elapsed-ms → % full in the Skin/AirChamber model.
4. Idle resync: add the configurable idle-timeout sensor recheck + the on/off
   setting; surface both in the settings/skin config UI.
5. Fix the two control layers fighting (§7) — make the Test dialog mode-exclusive
   or give the manual + state-machine layers one arbiter.
