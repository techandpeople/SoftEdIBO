# Pressure model, fill control & over-/under-pressure safety

How chambers are driven and what stops them from bursting or imploding.
Applies to both actuator firmwares (`node_direct`, `node_multiplexed`), whose
shared fill supervisor lives in `firmware/common/coupled_fill.h`.

> **Note:** inflate/deflate **targeting** is done by the coupled-fill supervisor
> (`firmware/common/coupled_fill.h`) - open the group together, close each
> chamber the instant the shared line reaches its target while the pumps keep
> running, then one final settle and an isolated verify per chamber - because
> the chambers share one check-valve-less line and the gauges read the shared
> line while coupled. See `docs/COUPLED_FILL_CONTROL.md`. The pressure model,
> the vacuum blind spot and the caps below still apply. The older open-loop
> time-based `ms` fill (`firmware/common/fill_control.h`) is fully superseded:
> that header still exists but nothing includes it anymore.

## Pressure baseline - there is none in software

The XGZP6847A is read as a **gauge** sensor (`pressure.h`): it measures chamber
pressure **relative to atmosphere** by hardware (its reference port vents to
ambient). So:

- The zero is atmospheric **automatically and continuously** - no software tare
  is needed (unlike the magnet sensor, which does capture a baseline). A chamber
  at rest reads ~0 kPa regardless of whether it vents to atmosphere.
- **Blind spot:** `voltageToPressure` clamps readings to the sensor's physical
  range `[SENSOR_KPA_MIN, SENSOR_KPA_MAX]` - build flags, default 0..100 kPa -
  so the default sensor **cannot see below atmosphere**: any vacuum reads as the
  floor. The floor (`pressure::FLOOR_KPA`) is self-reported to the PC as
  `"kpa_min"` in the boot `*_ready` message and in `pong`, so the runtime knows
  below which pressure a deflate needs a **time budget** instead of the sensor.
  (The planned -40..+40 kPa sensors are just `-DSENSOR_KPA_MIN=-40
  -DSENSOR_KPA_MAX=40`; the closed loop then works below ambient too - see
  [Deep-vacuum valve lock](#deep-vacuum-valve-lock--the-40-kpa-sensor-readiness).)

## Inflate / deflate are both pump-driven

`recalcPumps()` drives the inflate (pressure) and deflate (vacuum) pumps from
the **actual open valves** - a pump only runs while a valve of its direction is
open (the direct board: one on/off pump per direction; the multiplexed board:
`ceil(open_valves/3)` of that role's 3 pumps). Deflate is *suction*, not passive
venting, so an unbounded deflate can pull a sealed chamber into vacuum.

### Wire protocol

`{"cmd":"inflate"|"deflate", "chamber":N, "delta":D, "ms":M, "duty":X}`

- `delta` (%) of the chamber's configured `[min_kpa, max_kpa]` range. The
  firmware turns it into an **absolute target** - `cachedKpa +/- delta`, clamped
  to the range - and hands that target to the coupled-fill engine. A command
  whose target is already met is a **no-op** (guarded in the firmware AND in
  `Skin._inflate`, so holding "+" at the cap cannot creep past max).
- `ms` is a **per-chamber open-time budget** (the engine's `capMs`), not an
  open-loop valve timer: targeting stays pressure-based, but a target the gauge
  cannot see (a deflate below the sensor floor) closes on this calibrated time
  instead, and any chamber closes once its budget is spent. Absent/0 -> the
  engine's `chamber_max_ms` (5 s) backstop. The PC computes it from measured
  curves: an inflate `ms` from the chamber's `FillProfile` (the per-combination
  curve when one was measured for exactly the co-active set, else the solo curve
  scaled by `max(1, active_chambers/pumps)` - `scale_fill_ms`); a deflate `ms`
  from the `DeflateProfile` only when the target sits below the measured plateau
  floor (+1 % margin) - see `Skin._inflate` / `Skin._deflate_ms`.
- `chamber: -1` actuates every chamber ("Inflate/Deflate All"); separate
  per-chamber frames arriving within the engine's grouping window batch into the
  same coupled round either way.
- `duty` (1-255, optional) is parsed and stored but currently **not** honoured
  on the engine path - the pumps run at full duty. Only the direct board's bench
  `test_run` drives a reduced PWM (used by the duty-curve calibration sweep).

## Safety nets

| Limit | Inflate | Deflate |
|---|---|---|
| Per-chamber time budget (sensor-independent, **always armed**) | `capMs` (the PC's `ms`) else `chamber_max_ms` = 5 s cumulative open | same - the real vacuum backstop |
| Round / sequence caps | `round_max_ms` 6 s (direct) / 8 s (multiplexed); `seq_max_ms` 25 s / 45 s | same |
| Pressure cutoff (sensor) | engine closes the chamber at its target, clamped to `max_kpa` | closes at its target, clamped to `min_kpa` - useless below the gauge floor |
| Absolute hard limit | `hard_max_kpa` = `HARD_MAX_KPA` = 100 kPa, single-sample, no debounce | none in the engine (gauge is blind); `HARD_MIN_KPA` = `pressure::VACUUM_HOLD_FLOOR_KPA` clamps `set_min_pressure` + the manual path - -100 kPa (inert) with the blind gauge, -40 kPa (valve-safe) with the vacuum sensor (see below) |
| Watchdog | `ACTUATION_TIMEOUT_MS` = 10 s | same |

Notes / honest gaps:

- The hard kPa limits are **deliberately effectively uncapped** (+/-100 kPa): the
  gauge has proven unreliable and must not gate fills, so over-pressure is
  bounded by TIME (the budgets and caps above), not by pressure. The per-chamber
  `max_kpa` (what `set_max_pressure` sets) clamps every target at request time
  and is the effective pressure cap while the sensor is healthy.
- On **deflate** the (default) sensor is blind below the floor by design, so the
  time budget - the PC's calibrated deflate `ms`, else the 5 s `chamber_max_ms`
  - is the real backstop, and it is **always** armed, even uncalibrated.
- `Skin` pushes `set_max_pressure` + `set_min_pressure` at construction **and
  re-pushes both before every actuation** (`Skin._push_limits`): ESP-NOW is
  fire-and-forget, so the one-shot push can be dropped and external tools can
  leave a stale limit on the node (observed: a 20 kPa chamber inflated to
  ~50 kPa via repeated "+"). End-to-end command ACKs are designed but not
  implemented - see `docs/ACK_RELIABILITY.md`.
- The PC never trusts the firmware's `pressure` % field (computed against
  whatever limits the node currently holds, which lag the PC config): it
  recomputes % from the reported `kpa` against the configured range
  (`src/hardware/units.py` `kpa_to_pct`).

## Symmetric time bound on every open valve (implemented)

The engine bounds every chamber's **cumulative open time** per sequence via
`capOf(i)` - the request's `capMs` when set, else `chamber_max_ms` - in **both**
directions, and a chamber that spent its budget counts as done at the isolated
verify (for a target below the gauge floor the budget IS the closing authority,
so it can never re-open in a loop). The PC caps its computed deflate budget at
`MAX_DEFLATE_MS` = 5 s (`src/hardware/fill_profile.py`), mirroring the
firmware's 5 s backstop.

## Driving a coupling sweep safely

Inflate via the `Skin` path (it attaches the calibrated time budget when the
chamber has a measured curve); each chamber is bounded by the table above.
Inflate to the chamber's configured `max_pressure` (firmware-capped, worst-case
deformation). See [TOUCH_COUPLING.md](TOUCH_COUPLING.md).

## Deep-vacuum valve lock & the -40 kPa sensor readiness

**The failure.** The FA0520E solenoid valves re-open against at most ~47 kPa of
differential pressure (~=350 mmHg, the part's rating). There is **no vent** in the
pneumatics: the only path back to atmosphere is *back through an idle pump* while
a valve of that direction is open (the "passive vent" - both valves open, pumps
off - that the fill calibration uses). So a chamber, or the shared vacuum
manifold behind the closed deflate valves, that is left **deeper than ~47 kPa of
vacuum traps a pressure the next valve cannot open against**: the valve stays
shut and the pump forces against a seat that will not move. This is the "motor
forcing for ~1 s, every now and then" symptom - it strikes after a real deflate,
when the next deflate (or a re-inflate of a vacuum-held chamber) tries to open a
valve against the trapped vacuum. (The `pump-recalc-1` fix stopped the *manual*
paths from dead-heading the manifold to that depth - see
[the memory trail]; it did **not** stop a *normal* deflate from legitimately
pulling the manifold deep and leaving it there.)

**Why the blind gauge can't fix it.** With the 0..100 kPa sensor the firmware
cannot see below atmosphere, so a vacuum deflate closes on **time** (`capMs`),
not pressure. A full-duty vacuum pump run for seconds pulls well past the valve's
~47 kPa limit and seals that in - the lock is essentially unavoidable while the
vacuum side is unmeasured and time-bounded.

**The fix the -40..+40 kPa sensors enable.** Once the vacuum side is *measured*,
the coupled-fill engine closes deflate **closed-loop at the target**, and the
target is capped at the sensor floor (-40 kPa) - which is **inside** the valve's
~47 kPa limit. So both the chamber and the manifold come to rest at <=40 kPa of
vacuum: the vacuum is **held** (wrinkles stay shrunk) *and* **every valve always
re-opens**. -40 kPa does triple duty - useful vacuum target, valve-open limit,
and sensor floor, all at the same value - which is also why the narrower -40..+40
sensor is the right part over a wider -100..+100 one (same ADC span over 2.5x
less range = 2.5x finer resolution, and nothing to measure below -40 anyway since
the valve can't operate there).

**What is prepared now (inert until the flag flips).**

- `pressure::VACUUM_HOLD_FLOOR_KPA` (`firmware/common/pressure.h`) derives the
  deepest holdable vacuum from the sensor floor `P_MIN`: **-100 kPa** (unchanged,
  a no-op sentinel) with the blind gauge, **-40 kPa** with the vacuum sensor,
  clamped to `VALVE_OPEN_LIMIT_KPA` = -42 kPa so it stays valve-safe even for a
  deeper sensor.
- `HARD_MIN_KPA` (direct) and `HARD_CHAMBER_MIN_KPA` (multiplexed) now reference
  it, so the `set_min_pressure` clamp and the manual vacuum cutoff become
  valve-safe automatically when the sensor changes - **no behaviour change on the
  current hardware** (both resolve to -100 kPa today).
- Flip the whole fleet by uncommenting the two `-DSENSOR_KPA_MIN=-40
  -DSENSOR_KPA_MAX=40` flags in `firmware/node_actuator/platformio.ini` (`[env]`,
  so every board variant picks them up). Both builds - current gauge and vacuum
  flag - are verified to compile.

**Deferred to when the sensors are in hand** (needs the bench): tune the
closed-loop-at-floor behaviour (the round gate can briefly overshoot past -40,
which the saturated gauge can't see), reflash both boards, bump the `fw` marker,
then remove the blind-gauge branch (`VACUUM_HOLD_FLOOR_KPA`'s `P_MIN >= 0` arm and
the `ms`-timed deflate fallback) so no dead single-sensor support is left behind.
