# Study Setup Guide

Practical setup notes for whoever builds and runs a SoftEdIBO study session.
For the application install/usage see [README.md](README.md); for the study
protocol see [docs/STUDY_PLAN.md](docs/STUDY_PLAN.md); for the touch-sensing
design see [docs/TOUCH_POSITION_TRACKING.md](docs/TOUCH_POSITION_TRACKING.md) and
[docs/TOUCH_ML.md](docs/TOUCH_ML.md).

---

## 1. Skins: shape (`skin_type`) and silicone (`skin_variant`)

Each skin is described by two independent fields in the settings file (set them
in the GUI: **Robot config -> skin -> "Skin type"** and **"Silicone"**). The app
uses the user copy, `~/.local/share/SoftEdIBO/config/settings.yaml`
(`%APPDATA%\SoftEdIBO\config\settings.yaml` on Windows), copied from the repo's
`config/settings.yaml` on first run:

- **`skin_type`** - the shape, which fixes the sensor layout and selects the
  touch-gesture model. Registered in
  [src/hardware/skin_geometry.py](src/hardware/skin_geometry.py).
- **`skin_variant`** - the silicone format (different chamber sizes per format).
  Orthogonal to the shape; fed to the touch ML as a feature. `natural` and
  `wrinkles` exist for every type; the organ-bearing variants depend on the
  type (`organ` for `tree_round`, `three_organ` for `turtle_square`,
  `organ_rectangle` / `organ_triangle` / `organ_ellipse` for `thymio` - see
  `VARIANTS_BY_TYPE` in [src/hardware/skin_geometry.py](src/hardware/skin_geometry.py)).

So e.g. a wrinkled turtle top is `skin_type: turtle_square`,
`skin_variant: wrinkles`.

---

## 2. Magnetic touch sensors per skin

The touch board (`node_magnet_sensor`) carries up to **4 MLX90393** 3-axis
magnetometers. A magnet sits above each sensor in the silicone; pressing the
skin moves the magnet closer and the reading rises. **How many sensors a skin
uses depends on the build** (what fits around the air tubes):

| skin_type        | sensors | What it can resolve                              |
|------------------|:-------:|--------------------------------------------------|
| `tree_round`     | 1       | tap / press / hold only (magnitude + timing)     |
| `turtle_side`    | 2       | the above **+ one axis** of direction            |
| `turtle_square`  | 4       | full quadrant **position + drag**                |
| `turtle_triangle`| 4       | full position                                    |
| `thymio`         | 4       | full position                                    |

> **Limitation (by design):** spatial quadrant position tracking only engages at
> **4 sensors** (see `Skin._setup_touch_tracking`,
> [src/hardware/skin.py](src/hardware/skin.py)). Fewer-sensor skins still get
> touch *reactions* and per-skin-type **gesture** ML, but no continuous position.

---

## 3. Magnet polarity

**Polarity is a hardware build choice, not a software setting** - there is no
polarity option in the app or firmware, and none is needed. The quadrant detector
([src/hardware/quadrant_detector.py](src/hardware/quadrant_detector.py), a port of
the thesis detector) decides which sensor/quadrant is touched purely from each
sensor's **magnitude** (uT): per-sensor threshold + hysteresis, dominant quadrant
= the strongest sensor. **It never uses the magnet's polarity (sign).**

**Is alternating polarity better?** Yes - marginally, and never worse, on
4-sensor skins. A neighbouring magnet of *opposite* polarity *subtracts* from a
sensor's own-magnet field instead of adding to it, so pressing one quadrant is
less likely to spuriously raise an adjacent sensor -> cleaner thresholds, better
multi-touch separation, less flicker at boundaries. (In SoftEdIBO each magnet
sits directly above its own well-separated sensor, so cross-talk is already low -
hence "better", not "required".)

**Decision for this project: magnets are NOT alternated** (all same polarity) -
the gain is small in this layout and not worth the build complexity. Alternating
`+ - / - +` on 4-sensor skins would only buy slightly cleaner multi-touch
boundaries, and what it buys is recoverable in software anyway (below).

**If you don't alternate, what you lose and how to recover it:**

- You lose a little cross-talk margin between adjacent quadrants on **4-sensor**
  skins only (fuzzier boundaries / multi-touch). Detecting *which single*
  quadrant still works - the own-magnet dominates each sensor by distance.
- Nothing is lost for gesture ML (uses magnitude/timing, polarity-agnostic) or
  for 1-2 sensor skins.
- **Recover it for free:** (1) **Re-zero** subtracts the static cross-talk (it
  re-baselines the sensors, so resting readings settle near 0); (2) tune
  **per-sensor thresholds + hysteresis** in the Touch Tuning panel (section 4) above
  the residual. This handles the normal case.
- **Optional, for maximum separation:** a cross-talk "unmixing" calibration -
  press each quadrant alone, record the 4x4 response, invert it to separate the
  quadrants regardless of polarity (the linear/KNN idea from the thesis). Not
  implemented; add it only if dense 4-sensor multi-touch ever needs it.

> Polarity is never a software setting either way; if you ever wanted the
> software to *exploit* the sign, that would need a firmware change to stream a
> signed axis instead of magnitude - not necessary (the thesis validated the
> magnitude approach).

---

## 4. Calibrating touch thresholds

Touch detection uses **absolute magnitude thresholds (uT)**, so each build needs
a quick threshold tune (no ML, no dataset):

1. Open the monitor for the skin (4-sensor skins show the **Touch Tuning** panel).
2. **Re-zero** at rest so resting readings settle near 0.
3. Press each quadrant and note the peak; set each per-sensor threshold **above**
   the resting noise and **below** the touched peak (default 100 uT is
   conservative). Add hysteresis to stop flickering at the boundary.

---

## 5. Recording & training touch gestures

1. During a session, enable recording - the stream is saved to a `.jsonl` under
   the recordings folder, **tagged with each skin's `skin_type` and
   `skin_variant`** in its header (no manual tagging needed later).
2. **Tools -> Touch Gestures**: *Add recording...* (skin type is read from the
   recording), label each touch (auto-filled from the live tags tapped during the
   session), optionally **Group** multi-touch gestures (e.g. a triple tap), then
   **Train**. One model is trained per `skin_type`; the silicone variant is used
   as a feature. See [docs/TOUCH_ML.md](docs/TOUCH_ML.md).

---

## 6. Chamber fill calibration

Chambers are **targeted by pressure**: the firmware's coupled-fill engine opens
the co-active chambers together and closes each one the moment its gauge reaches
the target (see [docs/COUPLED_FILL_CONTROL.md](docs/COUPLED_FILL_CONTROL.md) and
[docs/PRESSURE_AND_FILL_SAFETY.md](docs/PRESSURE_AND_FILL_SAFETY.md)). The
calibration curves do not replace that loop - they give each request a
**time budget** (`ms`, the engine's per-chamber open-time cap), which is the
closing authority only where the gauge cannot see (a deflate below the sensor
floor, e.g. wrinkles) and a safety bound everywhere else.

**Tools -> Calibrate Fill Times...** (or **Configure Skin -> Calibrate Fill**,
scoped to one skin) measures, per chamber:

- **Calibrate all** - the **time->pressure fill curve**: each chamber sweeps from
  empty in one continuous pass while the node streams pressure at a fast cadence
  (**Detail**). Saved as the chamber's `fill_profile`; also ranks which chamber
  fills first. An inflate's `ms` is interpolated from it and scaled by the
  node's concurrent load, `x max(1, active_chambers / pumps)`.
- **Duty curves** - fill speed at several pump PWM duties, so the activity
  editor's "over (ms)" slow-fill can pick a duty from measured data (note: the
  firmware engine path does not apply a requested `duty` yet - see FIXME.md).
  **Min power PWM** sets the duty that power level 1 maps to (level 5 = 255;
  diaphragm pumps stall below ~180).
- **Deflate curves** - the falling vacuum curve down to the sensor floor, which
  times the deflates the gauge cannot supervise.
- **Hold/leak curves** - the pump PWM that balances each chamber's leak at
  several levels (hold curve) plus the natural pressure decay (leak curve).
  Needs pressure sensors on the node.

Run it once per build / after silicone changes, with the gateway connected,
outside a running session and with hands clear; then **Save** (or **Apply** to
keep the dialog open). **Save as skin-type template** stores the curves under
`fill_profiles_by_type` (keyed by `skin_type` + `skin_variant`), so every skin of
that type inherits them unless it has its own override. Between sweeps the
chambers vent to ambient with the firmware `vent` command (both valves open,
pumps off).

> Continuous calibration currently needs the direct board (`test_run` /
> `status_rate` are not ported to the multiplexed board yet), so Calibrate Fill
> is disabled for `node_multiplexed` chambers.

**Leak compensation** is the `hold_duty` regulated hold: after every
inflate/deflate settles at a positive level, the app automatically asks the
node to hold it there - the inflate valve reopens whenever the gauge droops
below the level and the shared pump is servoed (never below 180 PWM), seeded
from the hold curve. Any new actuation on the chamber drops the hold first.
Vacuum poses (wrinkles) and sensorless boards are not auto-held.

Safety always applies regardless of calibration: a 5 s per-chamber open cap
(when no `ms` is sent), per-round / per-sequence caps, a 10 s actuation watchdog,
and the per-chamber `max_pressure` clamp.

When you start a session on real hardware, if any selected chamber has no
calibrated fill curve you're prompted to **calibrate now** (or start anyway with
plain pressure targeting and the 5 s cap). Calibrating there rebuilds the robots
so the new curves take effect - just start the session again.
