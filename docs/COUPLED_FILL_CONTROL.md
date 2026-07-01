# Coupled-fill control (inflate / deflate)

How the actuator firmware drives several chambers that share one pneumatic line,
and the `millis()` underflow bug that made it look like nothing inflated.

Applies to both actuator firmwares (`node_direct`, `node_multiplexed`). The
board-agnostic supervisor lives in `firmware/common/coupled_fill.h`; each board's
`chambers.h` supplies the actuation (valves/pumps) and a per-board tuning.

## The hardware problem

Every chamber of one direction taps a **single shared manifold** with **no check
valves**:

- `node_direct` — 3 chambers, 1 inflate pump + 1 vacuum pump.
- `node_multiplexed` — up to 9 chambers, **3 inflate + 3 vacuum pumps on a common
  manifold** (the pumps are interchangeable; only how many run matters).

Because there are no check valves, while **two or more valves of the same
direction are open the chambers equalise**, and each chamber's in-line gauge reads
the **shared line**, not its own chamber. So per-chamber closed-loop cutoff fires
on the wrong pressure — one chamber's gauge momentarily seeing the line trips the
others' cutoff. This is why "inflate the whole skin" never settled correctly: the
only trustworthy reading is taken when a chamber is **isolated** (its valve closed
and the line settled, or it is the only open valve of its direction).

## The algorithm — "actuate coupled, measure isolated"

One `coupled_fill::Engine` drives **one direction**; a board owns two (inflate,
deflate), independent because the two manifolds are independent. State machine:

1. **GROUPING** — a chamber requested in this direction starts a short coalescing
   window (`group_window_ms`) so sibling per-chamber commands are batched and
   **open together**.
2. **FILLING** — open every chamber in the group at once + run the pumps. While
   coupled, the line is the only trustworthy reading, so the round ends when the
   line first reaches the **lowest target among the open chambers** (that chamber
   is done). Spikes are rejected (see below).
3. **SETTLING** — every valve is now shut, so after `settle_ms` each gauge reads
   its **own** chamber. Drop the chambers that really reached target (±`tol_frac`
   of range); whatever is still short re-opens for the next, higher round. A round
   that opened a single chamber was never coupled, so its fill reading is already
   trustworthy and the settle is skipped.

The set shrinks each round; the last chamber opens **alone**, so its gauge reads
itself → precise. A reading taken while coupled is never trusted. **No chamber is
ever abandoned**: a chamber leaves the pending set only when an isolated measure
confirms its target or a safety cap (`chamber_max_ms`, `seq_max_ms`) expires.

## Dynamic grouping — why it must coalesce per-chamber frames

The runtime (`Skin`) inflates a whole skin by sending **one frame per chamber**
(`src/hardware/skin.py`), not a single `chamber == -1` broadcast (only the test
dialog uses `-1`). The old firmware treated each per-chamber frame as an
independent closed-loop fill over **coupled** sensors → wrong cutoff. The engine
therefore groups by direction: any time ≥ 2 same-direction chambers are co-active
(whether from `-1` or from separate frames arriving within `group_window_ms`),
they run as one coupled round. A single chamber is the same machine with a
one-element set.

## Pumps

- **Direct** (`chambers::recalcPumps`): one pump per direction, driven on/off from
  the **actual open valves** (the `valveOpen` mirror), never from chamber state —
  so a pump can never run without an open flow path.
- **Multiplexed** (`chambers::recalcPumps` + `pumps::setRoleActiveCount`): the
  manifold is shared, so the number of running pumps scales with open valves —
  `ceil(open_valves_of_dir / VALVES_PER_PUMP)`, capped at the pumps of that role
  (3). This keeps the per-chamber fill speed roughly constant as more valves open
  **and** stops a spare pump dead-heading the manifold (the "pump running dry /
  forcing" failure): a pump only spins when open valves can take its air.

## Spike rejection

A pump-start surge on the shared line used to trip the cutoff the instant a round
opened ("one pulse, then stop"). Three layers now stop that without re-introducing
overshoot:

1. **Median-of-three** gauge read on the control path (`readKpaMedian`) — kills a
   single-sample spike.
2. **`min_round` gate** — ignore the cutoff for `min_round_single_ms` (1-chamber
   round, tight) or `min_round_ms` (2+ chambers, rides out the bigger manifold
   kick) after a round opens.
3. **Consecutive-read debounce** (`cutoff_debounce`) — the cutoff must persist
   across N control ticks.

`hard_max_kpa` (inflate) bypasses all three as the safety backstop.

## Per-board tuning (`coupled_fill::Tuning`)

The multiplexed board is slower (I2C PCA9685 valve writes + a per-read 16-channel
mux scan), so its windows are looser.

| field                 | direct | multiplexed | meaning                                              |
|-----------------------|-------:|------------:|------------------------------------------------------|
| `group_window_ms`     |     50 |         120 | coalesce sibling per-chamber requests                |
| `min_round_single_ms` |     40 |          60 | 1-chamber round cutoff gate                          |
| `min_round_ms`        |    220 |         350 | 2+-chamber round cutoff gate (manifold kick)         |
| `settle_ms`           |    150 |         300 | settle closed before measuring isolated              |
| `round_max_ms`        |   6000 |        8000 | per-round cap (leak / can't reach)                   |
| `seq_max_ms`          |  25000 |       45000 | whole-sequence safety cap                            |
| `chamber_max_ms`      |   5000 |        5000 | per-chamber cumulative-open cap (stuck gauge/vacuum) |
| `cutoff_debounce`     |      3 |           2 | consecutive over-target reads to end a round         |
| `tol_frac`            |   0.10 |        0.10 | reached tolerance, fraction of range                 |

## The `millis()` underflow bug (the finding)

**Symptom.** After the redesign, `inflate all` opened the round once and then
nothing happened — the chambers barely moved and needed re-clicking. The ESP-NOW
debug trace showed, per click:

```
dbg ev:rx  cmd:inflate ch:-1 open_inf:0
seq  inf_ph:1 inf_mask:7              # GROUPING, all 3 chambers pending
dbg ev:eng dir:0 code:0 mask:7        # EV_ROUND_OPEN — opened all 3 together
... then forever: st:0, vi:0, pumps inf:0, and NO code:1/2/3, no reboot, no stop
```

So the round opened (valves set, pump told to run) but within the same instant
everything was off again, with **no** round-end event and **no** external `stop`.

**Root cause.** `actuationWatchdog()` compared `now - since_ms >= TIMEOUT` in
**unsigned** arithmetic. `now` is captured once at the top of `loop()`. When a
round opens, `engOpen()` sets `since_ms = millis()` — a hair **later** than `now`.
So on the round-open tick `now < since_ms`, and the unsigned subtraction
**underflowed** to ~4×10⁹, which is `>= 10000` → the watchdog force-stopped the
chamber the instant it opened (`holdChamber()` closes the valve, cuts the pump,
and silently drops the engine to IDLE — hence no `code:1`). The pump therefore
never actually ran and the chambers never filled.

**Fix.** Signed difference, both boards:

```cpp
if ((int32_t)(now - state[i].since_ms) >= (int32_t)ACTUATION_TIMEOUT_MS) { ... }
```

The small future offset becomes a small **negative**, so the watchdog only fires
on a real timeout. The engine already used signed `(int32_t)(now - phaseMs)`
everywhere; the watchdog was the one raw-unsigned holdout.

**Same bug, more places.** The pattern recurs anywhere a timestamp is set with
`millis()` *during the command drain or a control tick* (both run AFTER `loop()`
caches `now`) and is then compared `now - ts >= window` unsigned. All fixed to
signed:

- `actuationWatchdog` — `since_ms` set in `engOpen` (control tick).
- `manualSafetyTick` — `manualPumpTs` / `manualValveTs` set in the command drain.
  This is the **"a manually-opened valve in Test Actuators sometimes closes
  itself"** bug: the dead-man underflowed and shut the valve the instant it
  opened. "Sometimes", because it only triggers when a millisecond ticks over
  between `now = millis()` and `setManualValve` (otherwise `manualValveTs == now`,
  diff 0). (Note: a manual valve still auto-closes after `MANUAL_MAX_ON_MS` = 5 s
  by design — the dead-man — unless the dialog keeps it alive.)
- multiplexed manual override (`manualTs`) and the bench-test keepalive
  (`testHeartbeatMs`) — same drain-set-then-compare pattern.

The periodic throttles (`lastStatusMs`, `lastPressureMs`, `lastChamberMs`, …) are
**not** affected: they are set to `now` when they fire, so they are always `<=
now` (no underflow except at the 49.7-day `millis()` rollover).

**Lesson.** Any `millis()` difference against a timestamp that may be later than
the loop's cached `now` must be **signed**: `(int32_t)(now - then) >= window`.
Cache one `now` per loop. The ESP-NOW trace pattern — `ev:eng code:0` (open) with
no following `code:1` (end) — is what pinned the first one; keep that diagnostic.

## Diagnostics (`-DDEBUG_BUILD`)

Wireless debugging over ESP-NOW is the only way to catch these on a battery node:

- `dbg ev:rx` — the command + which valves were open the instant it arrived.
- `dbg ev:eng` — round/measure trace; `code` is `coupled_fill::Event`
  (0 open, 1 end, 2 measure, 3 done, 4 abort), `mask` the affected chambers.
- `dbg ev:dry` — a pump running with no open valve, or more pumps than the open
  valves should need (`recalcPumps` makes both impossible — fires only on a
  regression).
- `seq` — both engines' phase + pending masks + per-chamber kpa/max.

**`fw` marker.** The boot `node_*_ready` message carries `"fw":"…"`. **Bump it
whenever the actuator logic changes** so a flash can be confirmed from the log —
not bumping it (it sat at `round-min2`) cost a debug cycle of not knowing whether
the OTA had taken. Current marker: `coupled-fill-1`.

**Flashing.** OTA flashes the **prebuilt merged** `firmware/node_actuator/firmware-*.bin`.
Rebuild them with `scripts/build-firmware.sh` — building into `.pio/` alone does
**not** update what the OTA/wizard sends.

## Safety backstops (unchanged in spirit)

- `hard_max_kpa` — single-sample inflate cut, no debounce.
- `chamber_max_ms` — per-chamber cumulative-open cap; also the only backstop for a
  deflate into vacuum, which the gauge cannot see (clamps below atmosphere to 0).
- `seq_max_ms` — whole-sequence cap; aborts and closes any open valves.
- `actuationWatchdog` — last-resort per-chamber timeout (now signed).
- Emergency stop, manual dead-man and the continuous bench test bypass the engine
  and are unchanged.
