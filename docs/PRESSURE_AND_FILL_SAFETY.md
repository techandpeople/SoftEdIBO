# Pressure model, fill control & over-/under-pressure safety

How chambers are driven and what stops them from bursting or imploding.
Applies to both actuator firmwares (`node_direct`, `node_multiplexed`), whose
shared policy lives in `firmware/common/fill_control.h`.

> **Note:** inflate/deflate **targeting** is now done by the coupled-fill
> supervisor (`firmware/common/coupled_fill.h`) — open the group together, fill to
> the lowest target, close, measure each chamber isolated, repeat — because the
> chambers share one check-valve-less line and the gauges read the shared line
> while coupled. See `docs/COUPLED_FILL_CONTROL.md`. The pressure model, the
> vacuum blind spot and the hard caps below still apply; the time-based `ms` fill
> described here is superseded as the primary path on the actuator boards.

## Pressure baseline — there is none in software

The XGZP6847A is read as a **gauge** sensor (`pressure.h`: `P_MIN = 0`): it
measures chamber pressure **relative to atmosphere** by hardware (its reference
port vents to ambient). So:

- The zero is atmospheric **automatically and continuously** — no software tare
  is needed (unlike the magnet sensor, which does capture a baseline). A chamber
  at rest reads ~0 % regardless of whether it vents to atmosphere.
- **Blind spot:** `voltageToPressure` clamps negatives to 0 and the range is
  0..100 kPa, so the firmware **cannot see below atmosphere** — any vacuum reads
  as 0. There is therefore **no closed-loop control on the vacuum side**; a
  deflate target `< 0` is never reached in the reading and only time bounds it.

## Inflate / deflate are both pump-driven

`recalcPumps()` drives an **inflate pump** and a **deflate (vacuum) pump** — both
are active. Deflate is *suction*, not passive venting, so an unbounded deflate
can pull a sealed chamber into vacuum.

### Wire protocol

`{"cmd":"inflate"|"deflate", "chamber":N, "delta":D, "ms":M}`

- `delta` (%) is the PC's unit: it drives `AirChamber.target_pressure`, the GUI,
  and is the input that the PC turns into `ms` (`effective_fill_ms`).
- `ms` selects time-based actuation. The firmware branches on `ms > 0`:
  - **inflate, ms > 0** (calibrated): valve open for `ms` (clamped to
    `MAX_FILL_MS`), toward `max_kpa`; `delta` is ignored in this branch.
  - **inflate, ms == 0** (uncalibrated): pressure-based to the `delta` target.
  - **deflate**: always time-bounded (see below); `delta` sets the pressure
    target as a secondary cutoff.

Time-based fill exists because the pressure sensor is laggy — calibrated fill
runs open-loop on a measured `fill_time_ms` instead of chasing the sensor.

## Safety nets

| Limit | Inflate | Deflate |
|---|---|---|
| Time cap (sensor-independent) | `MAX_FILL_MS` (5 s) when time-based | **`MAX_DEFLATE_MS` (5 s), always armed** |
| Pressure cutoff (sensor) | stop at `max_kpa` (auto loop) | stop at `min_kpa` (auto loop) — useless below atmosphere |
| Absolute hard limit | `HARD_MAX_KPA` = 12 kPa | `HARD_MIN_KPA` = −12 kPa |
| Watchdog | `ACTUATION_TIMEOUT_MS` = 10 s | same |

Notes / honest gaps:

- The automatic over-/under-pressure cutoff is in `main.cpp` (read +
  `kpa >= max_kpa` / `kpa <= min_kpa`) and is **sensor-dependent**. The absolute
  `HARD_MAX`/`HARD_MIN` are wired into the *manual/dev* safety tick only; in the
  automatic path the per-chamber `max_kpa` (clamped ≤ 12) is the effective cap.
- A faulty/laggy sensor is the residual over-pressure risk on **inflate**, caught
  only by the 10 s watchdog. On **deflate** the sensor is blind to vacuum by
  design, so `MAX_DEFLATE_MS` (not pressure) is the real backstop — which is why
  the deflate deadline is **always** armed, even uncalibrated.
- `Skin` sends both `set_max_pressure` and `set_min_pressure` to the firmware on
  construction, so the caps survive a PC crash mid-session.

## Symmetric deflate time cap (implemented)

`fill_control.h` adds `MAX_DEFLATE_MS`, `deflateUntil(ms=0)` (always returns a
deadline; 0 → the hard cap) and `deflateTimeTick`. `beginDeflate(n, target, ms)`
arms `fill_until_ms` on every call (reusing that field — a chamber is INFLATING
xor DEFLATING), and each node calls `deflateTimeTick` in its loop. The PC may
send `ms` on `deflate` (clamped to the cap); absent, the hard cap applies.

## Driving a coupling sweep safely

Inflate via the `Skin` path (it already picks time-based vs pressure-based per
calibration); each chamber is bounded by the table above. Inflate to the
chamber's configured `max_pressure` (firmware-capped, worst-case deformation).
See [TOUCH_COUPLING.md](TOUCH_COUPLING.md).
