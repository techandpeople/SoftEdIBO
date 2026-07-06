# Touch ↔ chamber coupling (actuation contamination)

## The problem

On a skin, a small magnet sits in the silicone above each MLX90393 touch sensor;
pressing the skin moves the magnet and changes the field (that's how touch is
sensed). But **inflating a chamber also deforms the silicone and shifts the
magnet** — so chamber actuation can masquerade as a touch. The boot-time
magnetic baseline is static, so once a chamber inflates under a sensor its `mag`
climbs and may cross the activation threshold → a *false touch*.

This matters during a session because chambers are actively driven while we read
touch. Before doing anything about it, **measure whether it actually bites** for
a given skin (the sensors may sit away from actuated chambers).

## Three independent pieces

### 1. Measure: which chamber moves which sensor, at which level

`src/core/touch_coupling.py` (pure, Qt-free, tested) turns a *sweep* into
per-chamber **level→offset curves** `[chamber × level × sensor]` — how much each
chamber moves each sensor's `mag` (µT) at each inflation level it held — plus a
derived chamber↔sensor map. A single-level sweep yields the legacy one-point
matrix; a staircase sweep (e.g. 25/50/75/100 %) captures the nonlinear silicone
response:

- `build_coupling(samples, sensor_count, …) -> CouplingMatrix` (`.curves`,
  `.deltas` = the strongest-level view)
- `build_coupling_from_recording(path, sensor_count, …)` — reads a stream JSONL
- `CouplingMatrix.mapping(threshold)` → `chamber -> [sensors it moves]`
- `CouplingMatrix.sensor_primary_chamber(threshold)` → `sensor -> strongest chamber`
  (with 4-sensor skins, that's per-quadrant)

When the samples carry the firmware's 3-axis `vec` deltas, each curve point
also records the per-sensor offset **vectors**, enabling vector compensation
(below). The magnet module is shared (`firmware/common/se_magnet.h`) between
the standalone `node_magnet_sensor` board and the direct actuator board, so
both support it two ways: the `MAG_VECTOR` build flag (`pio run -e vector` on
`node_magnet_sensor`; reflash `-e release` to revert) turns it on from boot, or
`{"cmd":"configure","stream_vec":true}` toggles it at runtime on any build
(RAM-only — the calibration dialog sends it when a sweep starts, and the Skin
re-sends it on build whenever its stored curves carry vectors). The ready
announce gains `"vec":1` while it is on.

**Sensor-lag handling.** Chamber `status` broadcasts lag the true pressure,
while `magnet` samples stream at ~28 Hz. Instead of guessing the lag, the
analyzer measures only at **steady state**: any sample within `settle_ms`
(default 800 ms) of a change in the active-chamber classification *or a level
step* is dropped, so transitions never smear the means. Active samples are
grouped into 10 %-wide level bins — one curve point per bin, at the bin's mean
measured level.

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

Subtracts each chamber's expected per-sensor offset at runtime (one chamber can
move several sensors irregularly): for each sensor, the expected offset at the
current level is read off the chamber's coupling **curve** (piecewise-linear,
through the origin; a one-point curve reproduces the legacy
``delta x level/ref`` scaling), and the active-sensor set is rederived from the
residual. A real press still stands out; the actuation offset is removed.

Three robustness layers on top of the plain subtraction:

- **Margin** (``margin_frac``): a sensor's activation threshold grows by that
  fraction of the correction applied to it — big corrections carry
  proportionally bigger calibration error, so they can't flip a sensor active
  on their own.
- **Transition guard** (``guard_ms``, ``guard_level_eps``): the calibration
  only measures steady state, so for ``guard_ms`` after a chamber's level moves
  the compensator hardens the coupled sensors' thresholds by that chamber's
  strongest measured offset (worst case while pressure readings lag / the pump
  vibrates). Mirrors the analyzer's own settle window, live.
- **Vector mode** (automatic): touch and actuation displace the magnet along
  *different axes*; scalar magnitudes therefore under- or over-compensate (they
  only add up when collinear). When both the calibration and the live stream
  carry 3-axis ``vec`` deltas, the compensator subtracts the offset **vector**
  and takes the residual's norm — the physically correct model. Falls back to
  scalar per reading whenever either side lacks vectors.

- **Core:** `src/core/touch_compensation.py` (`ChamberCoupling` +
  `TransitionGuard` + `TouchCompensator`, pure/tested) — curve interpolation,
  offset subtraction, `act` recompute (``threshold_ut`` + margin + guard), and
  an opt-in ``suppress_pct`` fallback (blank a sensor while a strongly-coupled
  chamber is at/above a level — the "ignore touch while inflated/vacuum" last
  resort).
- **Live wiring:** `src/hardware/touch_source.py` `CompensatedMagnetSource` wraps
  the raw controller; the Skin exposes it as `skin.touch_source` and `skin.on_magnet`.
  Detection consumers (activities, gesture ML, the skin's QuadrantDetector +
  TouchEventRouter) read the compensated stream via `subscribe_skin_magnet`; the
  live monitor and this calibration tool keep using the raw controller (they
  need uncompensated uT). The stream recorder captures the raw stream *and*,
  when compensation is on, the compensated one (extra lines flagged
  `compensated` — the gesture ML trains on them; see docs/TOUCH_ML.md); the
  coupling analyzer skips flagged lines, so such recordings still calibrate on
  raw uT. The per-chamber level comes from the Skin's own AirChamber model, so
  it works regardless of fill mode.
- **Calibration:** Tools → **Calibrate Touch Coupling…**
  (`src/gui/touch_calibration_dialog.py`): the sweep sequence is a pure
  `SweepProgram` (rest → per chamber an ascending staircase of levels → deflate;
  "Levels per chamber" picks the staircase, 1 = the legacy full-inflation-only
  sweep). The dialog executes it against the gateway, holds **fast telemetry**
  on the chamber node so level bins track the staircase closely, and collects
  `mag` (+`vec` when streamed) + `status`. Chamber levels are recomputed from
  each status's measured `kpa` against the skin's *configured* min/max
  (`units.kpa_to_pct`) — the firmware's own `pressure` % is computed against
  the limits the node currently holds, which lag the PC config — falling back
  to the firmware % only when no `kpa` is present. The curves are built via
  `build_coupling` and stored as `touch.coupling` (`curves`, plus the
  legacy `deltas` for older readers); the tuning block `touch.compensation`
  (`enabled`, `threshold_ut`, `margin_frac`, `guard_ms`, `suppress_pct`) is
  written alongside. When a sweep yields no curves, `sweep_diagnostics` is
  appended to the preview, separating the two failure modes: no magnet samples
  at all (the touch node never streamed) vs. per-chamber peak levels that never
  reached `ACTIVE_MIN` (20 %), so nothing classified as inflated (stale kPa
  limits, pumps not running). Settings helpers + sample→config core in
  `src/hardware/touch_calibration.py` (tested).

The curves are measured in **`mag` (uT)** — the same field the PC detection path
uses. Enabled per skin; off by default, so the detection path is byte-for-byte
identical until you calibrate and enable. Configs saved before the curve upgrade
(single `deltas` matrix, no margin/guard keys) load and behave exactly as before.

## Existing related mapping

`touch.sensor_to_chamber` (`TouchEventRouter`) is the *routing* direction —
which chamber a sensor **touch** actuates — hand-authored, defaulting to 1:1. It
is the inverse use of the same physical colocation, but it is not a measured
influence map; the coupling matrix above is.
