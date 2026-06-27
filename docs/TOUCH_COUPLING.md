# Touch ↔ chamber coupling (actuation contamination)

## The problem

On a skin, a small magnet sits in the silicone above each MLX90393 touch sensor;
pressing the skin moves the magnet and changes the field (that's how touch is
sensed). But **inflating a chamber also deforms the silicone and shifts the
magnet** — so chamber actuation can masquerade as a touch. The boot-time
magnetic baseline is static, so once a chamber inflates under a sensor its `adj`
climbs and may cross `act_threshold` → a *false touch*.

This matters during a session because chambers are actively driven while we read
touch. Before doing anything about it, **measure whether it actually bites** for
a given skin (the sensors may sit away from actuated chambers).

## Three independent pieces

### 1. Measure: which chamber moves which sensor

`src/core/touch_coupling.py` (pure, Qt-free, tested) turns a *sweep* into a
coupling matrix `[chamber × sensor]` of how much each chamber moves each sensor's
`adj`, plus a derived chamber↔sensor map:

- `build_coupling(samples, sensor_count, …) -> CouplingMatrix`
- `build_coupling_from_recording(path, sensor_count, …)` — reads a stream JSONL
- `CouplingMatrix.mapping(threshold)` → `chamber -> [sensors it moves]`
- `CouplingMatrix.sensor_primary_chamber(threshold)` → `sensor -> strongest chamber`
  (with 4-sensor skins, that's per-quadrant)

**Sensor-lag handling.** Chamber `status` broadcasts arrive ~every 500 ms and the
gauge pressure sensor lags, while `magnet` samples stream at ~28 Hz. Instead of
guessing the lag, the analyzer measures only at **steady state**: any sample
within `settle_ms` (default 800 ms) of a change in the active-chamber
classification is dropped, so transitions never smear the means.

### Collection procedure (no special tooling)

1. Power the magnet node + the actuator node; let the magnetic baseline settle
   at boot **without touching**.
2. **Record** a session (the recorder logs chamber `status` + `magnet`).
3. Inflate **one chamber at a time**, hold a few seconds (dwell), deflate to
   rest, then the next. Inflation is bounded safely (see
   [PRESSURE_AND_FILL_SAFETY.md](PRESSURE_AND_FILL_SAFETY.md)).
4. Run `build_coupling_from_recording(path, sensor_count)` → matrix + `mapping()`.

If a chamber moves no sensor above threshold, there is no contamination to
correct — the measurement self-validates the problem.

### 2. Mitigate at the source: adaptive baseline (firmware, opt-in)

`node_magnet_sensor` can keep tracking slow drift after boot, so a slowly
inflating chamber is absorbed while fast touches still register:
`{"cmd":"configure","adaptive_baseline":true,"baseline_tau_ms":2000}`. The
baseline is **frozen per-sensor while that sensor is active**, so a real touch is
never averaged away. The EWMA step uses the *real* elapsed time between samples,
so it is immune to ESP-NOW / gateway jitter. Off by default — enable only once a
sweep confirms contamination. Toggle live in the Touch tuning panel.

### 3. Pressure-informed compensation (implemented, PC-side)

Subtracts each chamber's expected per-sensor offset at runtime, using the full
``[chamber x sensor]`` matrix (one chamber can move several sensors irregularly):
for each sensor, ``residual = mag - Σ_chamber coupling[c][s] x (level_c / ref)``,
then the active-sensor set is rederived from the residual. A real press still
stands out; the actuation offset is removed.

- **Core:** `src/core/touch_compensation.py` (`TouchCompensator`, pure/tested) —
  offset subtraction, `act` recompute (``threshold_ut``), and an opt-in
  ``suppress_pct`` fallback (blank a sensor while a strongly-coupled chamber is
  at/above a level — the "ignore touch while inflated/vacuum" last resort).
- **Live wiring:** `src/hardware/touch_source.py` `CompensatedMagnetSource` wraps
  the raw controller; the Skin exposes it as `skin.touch_source` and `skin.on_magnet`.
  Detection consumers (activities, gesture ML, the skin's QuadrantDetector +
  TouchEventRouter) read the compensated stream via `subscribe_skin_magnet`; the
  recorder / live monitor / this calibration tool keep using the raw controller
  (they need uncompensated uT). The per-chamber level comes from the Skin's own
  AirChamber model, so it works regardless of fill mode.
- **Calibration:** Tools → **Calibrate Touch Coupling…**
  (`src/gui/touch_calibration_dialog.py`): a sweep (inflate each chamber alone,
  dwell, deflate) collects `mag` + `status`, builds the matrix in uT via
  `build_coupling`, and stores it as `touch.coupling`; the tuning block
  `touch.compensation` (`enabled`, `threshold_ut`, `suppress_pct`) is written
  alongside. Settings helpers + sample→config core in
  `src/hardware/touch_calibration.py` (tested).

The matrix is measured in **`mag` (uT)** — the same field the PC detection path
uses (it ignores the firmware `adj`). Enabled per skin; off by default, so the
detection path is byte-for-byte identical until you calibrate and enable.

## Existing related mapping

`touch.sensor_to_chamber` (`TouchEventRouter`) is the *routing* direction —
which chamber a sensor **touch** actuates — hand-authored, defaulting to 1:1. It
is the inverse use of the same physical colocation, but it is not a measured
influence map; the coupling matrix above is.
