# Pressure model, fill control & over-/under-pressure safety

How chambers are driven and what stops them from bursting or imploding.
Applies to both actuator firmwares (`node_direct`, `node_multiplexed`), whose
shared fill supervisor lives in `firmware/common/coupled_fill.h`.

> **Note:** inflate/deflate **targeting** is done by the coupled-fill supervisor
> (`firmware/common/coupled_fill.h`) — open the group together, close each
> chamber the instant the shared line reaches its target while the pumps keep
> running, then one final settle and an isolated verify per chamber — because
> the chambers share one check-valve-less line and the gauges read the shared
> line while coupled. See `docs/COUPLED_FILL_CONTROL.md`. The pressure model,
> the vacuum blind spot and the caps below still apply. The older open-loop
> time-based `ms` fill (`firmware/common/fill_control.h`) is fully superseded:
> that header still exists but nothing includes it anymore.

## Pressure baseline — there is none in software

The XGZP6847A is read as a **gauge** sensor (`pressure.h`): it measures chamber
pressure **relative to atmosphere** by hardware (its reference port vents to
ambient). So:

- The zero is atmospheric **automatically and continuously** — no software tare
  is needed (unlike the magnet sensor, which does capture a baseline). A chamber
  at rest reads ~0 kPa regardless of whether it vents to atmosphere.
- **Blind spot:** `voltageToPressure` clamps readings to the sensor's physical
  range `[SENSOR_KPA_MIN, SENSOR_KPA_MAX]` — build flags, default 0..100 kPa —
  so the default sensor **cannot see below atmosphere**: any vacuum reads as the
  floor. The floor (`pressure::FLOOR_KPA`) is self-reported to the PC as
  `"kpa_min"` in the boot `*_ready` message and in `pong`, so the runtime knows
  below which pressure a deflate needs a **time budget** instead of the sensor.
  (The planned −40..+40 kPa sensors are just `-DSENSOR_KPA_MIN=-40
  -DSENSOR_KPA_MAX=40`; the closed loop then works below ambient too.)

## Inflate / deflate are both pump-driven

`recalcPumps()` drives the inflate (pressure) and deflate (vacuum) pumps from
the **actual open valves** — a pump only runs while a valve of its direction is
open (the direct board: one on/off pump per direction; the multiplexed board:
`ceil(open_valves/3)` of that role's 3 pumps). Deflate is *suction*, not passive
venting, so an unbounded deflate can pull a sealed chamber into vacuum.

### Wire protocol

`{"cmd":"inflate"|"deflate", "chamber":N, "delta":D, "ms":M, "duty":X}`

- `delta` (%) of the chamber's configured `[min_kpa, max_kpa]` range. The
  firmware turns it into an **absolute target** — `cachedKpa ± delta`, clamped
  to the range — and hands that target to the coupled-fill engine. A command
  whose target is already met is a **no-op** (guarded in the firmware AND in
  `Skin._inflate`, so holding "+" at the cap cannot creep past max).
- `ms` is a **per-chamber open-time budget** (the engine's `capMs`), not an
  open-loop valve timer: targeting stays pressure-based, but a target the gauge
  cannot see (a deflate below the sensor floor) closes on this calibrated time
  instead, and any chamber closes once its budget is spent. Absent/0 → the
  engine's `chamber_max_ms` (5 s) backstop. The PC computes it from measured
  curves: an inflate `ms` from the chamber's `FillProfile` (the per-combination
  curve when one was measured for exactly the co-active set, else the solo curve
  scaled by `max(1, active_chambers/pumps)` — `scale_fill_ms`); a deflate `ms`
  from the `DeflateProfile` only when the target sits below the measured plateau
  floor (+1 % margin) — see `Skin._inflate` / `Skin._deflate_ms`.
- `chamber: -1` actuates every chamber ("Inflate/Deflate All"); separate
  per-chamber frames arriving within the engine's grouping window batch into the
  same coupled round either way.
- `duty` (1-255, optional) is parsed and stored but currently **not** honoured
  on the engine path — the pumps run at full duty. Only the direct board's bench
  `test_run` drives a reduced PWM (used by the duty-curve calibration sweep).

## Safety nets

| Limit | Inflate | Deflate |
|---|---|---|
| Per-chamber time budget (sensor-independent, **always armed**) | `capMs` (the PC's `ms`) else `chamber_max_ms` = 5 s cumulative open | same — the real vacuum backstop |
| Round / sequence caps | `round_max_ms` 6 s (direct) / 8 s (multiplexed); `seq_max_ms` 25 s / 45 s | same |
| Pressure cutoff (sensor) | engine closes the chamber at its target, clamped to `max_kpa` | closes at its target, clamped to `min_kpa` — useless below the gauge floor |
| Absolute hard limit | `hard_max_kpa` = `HARD_MAX_KPA` = 100 kPa, single-sample, no debounce | none in the engine (gauge is blind); `HARD_MIN_KPA` = −100 kPa clamps `set_min_pressure` and the manual path |
| Watchdog | `ACTUATION_TIMEOUT_MS` = 10 s | same |

Notes / honest gaps:

- The hard kPa limits are **deliberately effectively uncapped** (±100 kPa): the
  gauge has proven unreliable and must not gate fills, so over-pressure is
  bounded by TIME (the budgets and caps above), not by pressure. The per-chamber
  `max_kpa` (what `set_max_pressure` sets) clamps every target at request time
  and is the effective pressure cap while the sensor is healthy.
- On **deflate** the (default) sensor is blind below the floor by design, so the
  time budget — the PC's calibrated deflate `ms`, else the 5 s `chamber_max_ms`
  — is the real backstop, and it is **always** armed, even uncalibrated.
- `Skin` pushes `set_max_pressure` + `set_min_pressure` at construction **and
  re-pushes both before every actuation** (`Skin._push_limits`): ESP-NOW is
  fire-and-forget, so the one-shot push can be dropped and external tools can
  leave a stale limit on the node (observed: a 20 kPa chamber inflated to
  ~50 kPa via repeated "+"). End-to-end command ACKs are designed but not
  implemented — see `docs/ACK_RELIABILITY.md`.
- The PC never trusts the firmware's `pressure` % field (computed against
  whatever limits the node currently holds, which lag the PC config): it
  recomputes % from the reported `kpa` against the configured range
  (`src/hardware/units.py` `kpa_to_pct`).

## Symmetric time bound on every open valve (implemented)

The engine bounds every chamber's **cumulative open time** per sequence via
`capOf(i)` — the request's `capMs` when set, else `chamber_max_ms` — in **both**
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
