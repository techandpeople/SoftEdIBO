# Study Setup Guide

Practical setup notes for whoever builds and runs a SoftEdIBO study session.
For the application install/usage see [README.md](README.md); for the study
protocol see [docs/STUDY_PLAN.md](docs/STUDY_PLAN.md); for the touch-sensing
design see [docs/TOUCH_POSITION_TRACKING.md](docs/TOUCH_POSITION_TRACKING.md) and
[docs/TOUCH_ML.md](docs/TOUCH_ML.md).

---

## 1. Skins: shape (`skin_type`) and silicone (`skin_variant`)

Each skin is described by two independent fields in `config/settings.yaml`
(set them in the GUI: **Robot config → skin → "Skin type"** and **"Silicone"**):

- **`skin_type`** — the shape, which fixes the sensor layout and selects the
  touch-gesture model. Registered in
  [src/hardware/skin_geometry.py](src/hardware/skin_geometry.py).
- **`skin_variant`** — the silicone format (different chamber sizes per format).
  Orthogonal to the shape; fed to the touch ML as a feature. One of:
  `natural`, `wrinkles`, `organ`.

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

**Polarity is a hardware build choice, not a software setting** — there is no
polarity option in the app or firmware, and none is needed. The quadrant detector
([src/hardware/quadrant_detector.py](src/hardware/quadrant_detector.py), a port of
the thesis detector) decides which sensor/quadrant is touched purely from each
sensor's **magnitude** (μT): per-sensor threshold + hysteresis, dominant quadrant
= the strongest sensor. **It never uses the magnet's polarity (sign).**

**Is alternating polarity better?** Yes — marginally, and never worse, on
4-sensor skins. A neighbouring magnet of *opposite* polarity *subtracts* from a
sensor's own-magnet field instead of adding to it, so pressing one quadrant is
less likely to spuriously raise an adjacent sensor → cleaner thresholds, better
multi-touch separation, less flicker at boundaries. (In SoftEdIBO each magnet
sits directly above its own well-separated sensor, so cross-talk is already low —
hence "better", not "required".)

**Decision for this project: magnets are NOT alternated** (all same polarity) —
the gain is small in this layout and not worth the build complexity. Alternating
`+ − / − +` on 4-sensor skins would only buy slightly cleaner multi-touch
boundaries, and what it buys is recoverable in software anyway (below).

**If you don't alternate, what you lose and how to recover it:**

- You lose a little cross-talk margin between adjacent quadrants on **4-sensor**
  skins only (fuzzier boundaries / multi-touch). Detecting *which single*
  quadrant still works — the own-magnet dominates each sensor by distance.
- Nothing is lost for gesture ML (uses magnitude/timing, polarity-agnostic) or
  for 1–2 sensor skins.
- **Recover it for free:** (1) **Re-zero** subtracts the static cross-talk (the
  firmware already sends baseline-subtracted `adj`); (2) tune **per-sensor
  thresholds + hysteresis** in the Touch Tuning panel (§4) above the residual.
  This handles the normal case.
- **Optional, for maximum separation:** a cross-talk "unmixing" calibration —
  press each quadrant alone, record the 4×4 response, invert it to separate the
  quadrants regardless of polarity (the linear/KNN idea from the thesis). Not
  implemented; add it only if dense 4-sensor multi-touch ever needs it.

> Polarity is never a software setting either way; if you ever wanted the
> software to *exploit* the sign, that would need a firmware change to stream a
> signed axis instead of magnitude — not necessary (the thesis validated the
> magnitude approach).

---

## 4. Calibrating touch thresholds

Touch detection uses **absolute magnitude thresholds (μT)**, so each build needs
a quick threshold tune (no ML, no dataset):

1. Open the monitor for the skin (4-sensor skins show the **Touch Tuning** panel).
2. **Re-zero** at rest so resting readings settle near 0.
3. Press each quadrant and note the peak; set each per-sensor threshold **above**
   the resting noise and **below** the touched peak (default 100 μT is
   conservative). Add hysteresis to stop flickering at the boundary.

---

## 5. Recording & training touch gestures

1. During a session, enable recording — the stream is saved to a `.jsonl` under
   the recordings folder, **tagged with each skin's `skin_type` and
   `skin_variant`** in its header (no manual tagging needed later).
2. **Tools → Touch Gestures**: *Add recording…* (skin type is read from the
   recording), label each touch (auto-filled from the live tags tapped during the
   session), optionally **Group** multi-touch gestures (e.g. a triple tap), then
   **Train**. One model is trained per `skin_type`; the silicone variant is used
   as a feature. See [docs/TOUCH_ML.md](docs/TOUCH_ML.md).

---

## 6. Chamber fill times

**Tools → Calibrate Fill Times…** measures, per chamber, how long it takes to
inflate from empty to its max (using the pressure sensor as ground truth) and
saves it as `fill_time_ms` in `config/settings.yaml`. Run it once per build /
after silicone changes:

1. Connect the gateway and power the nodes (do this **outside** a running
   session). On a reservoir node (`has_reservoirs: true`), let the tanks charge
   to their target first — fill times are measured against a charged tank.
2. Open the dialog, **Calibrate all** (or per chamber). Each chamber deflates,
   then inflates while timed; the measured time appears per row. The list
   **scrolls** when there are many chambers.
3. **Save**.

You can also calibrate just one skin from **Configure Skin → Calibrate Fill**
(same dialog, scoped to that skin's chambers) — handy after editing a single
skin without re-running everything.

A hard **5 s ceiling** and the firmware `HARD_MAX` pressure cutoff always apply,
so a stuck/unplugged sensor can't run a pump indefinitely.

**At runtime**, a chamber with a `fill_time_ms` inflates **by time** so it doesn't
depend on the laggy multiplexed pressure sensor. The window is the calibrated
`fill_time` scaled by both the requested fill % **and** the node's concurrent
load — `fill_time × requested% × max(1, active_chambers ÷ pumps)` — because the
pumps (or the shared reservoir tank) are shared per node, so chambers inflating
together fill each other slower. A lone chamber (or up to `pump_count` at once)
keeps its measured time; the PC recomputes this automatically as chambers start
and stop. The firmware then **maintains the level against slow leaks**: while idle
it keeps reading pressure and tops the chamber back up if it droops — but only on
a *drop*, so a child pressing the skin (which *raises* pressure) never triggers a
top-up. Chambers without a `fill_time_ms` keep the classic pressure-target
behaviour.

**Reservoirs** aren't calibrated (a tank isn't a chamber, so it never appears in
the dialog). The firmware keeps each tank at its target pressure on its own, and —
so a refill never disturbs a fill measurement — the tank pump **pauses while any
chamber on that node is inflating/deflating**, topping the tank back up once the
chambers settle.

When you start a session on real hardware, if any selected chamber has no
calibrated fill time you're prompted to **calibrate now** (or start anyway with
the pressure-based fallback). Calibrating there rebuilds the robots so the new
times take effect — just start the session again.
