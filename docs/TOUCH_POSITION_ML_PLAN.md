# Plan — Finer touch localization from the existing MLX90393 array

> Status: **plan** (not implemented). Goal: extract far more spatial
> information from the 4 (up to 5) MLX90393 sensors than the current
> 4-quadrant scheme — a grid of virtual touch points ("matrix"), plus a
> per-touch force estimate — using ML for the inverse mapping.
> Companion docs: [TOUCH_POSITION_TRACKING.md](TOUCH_POSITION_TRACKING.md)
> (current quadrant path), [TOUCH_ML.md](TOUCH_ML.md) (gesture ML),
> [TOUCH_COUPLING.md](TOUCH_COUPLING.md) (actuation contamination).

## Why the sensors are worth more than 4 quadrants

The current `QuadrantDetector` **throws away almost everything** the sensors
measure: it thresholds 4 scalar magnitudes into 4 booleans. What the hardware
actually provides per stream tick (~28 Hz):

- **3-axis field deltas** per sensor (`vec` rows — already implemented in
  `firmware/common/se_magnet.h`, runtime-switchable, no reflash needed):
  4 sensors × 3 axes = a **12-dimensional continuous signature** per touch.
- Good SNR: a touch reads ~100–300 µT near a sensor while MLX90393 noise at
  GAIN_2X/OSR_2/FILTER_3 is a few µT — so even the *weak* response of the
  distant sensors (a few % of the near-sensor signal) is measurable.
- The magnet grid in the silicone means a press at point P displaces a
  *local* set of magnets; each press location produces a distinct **pattern of
  ratios and directions** across the 12 dims. Magnitude scales with press
  depth, but the *normalized* signature is approximately force-invariant —
  so position and force separate cleanly:
  - position ≈ f(unit-normalized 12-D signature)  ← learned inverse map
  - force    ≈ g(signature magnitude)             ← monotonic, per position

This is a standard "sparse magnetometer array + deformable magnet layer"
localization problem; an ML regressor/classifier on the signature is the
practical way to invert it (a physical dipole-superposition fit is possible
but fragile with an uncalibrated magnet grid).

**Why not reed switches:** reeds are binary, one component + wire per point,
no force signal, no gesture dynamics, mechanical bounce. One reed = one point;
one MLX90393 array = potentially dozens of virtual points *plus* force *plus*
the gesture stream the study already uses. The cost of the MLX90393s is only
wasted if we keep using them as 4 binary switches — which is exactly what this
plan fixes.

## Phases

### Phase 0 — Feasibility bench (decides everything, ~1 day of work)

No GUI, no new firmware. A CLI script + analysis:

1. `{"cmd":"configure","stream_vec":true}` on the touch node (runtime toggle).
2. `scripts/touch_position_bench.py`: print/tape an M×N grid overlay on a real
   skin (start 4×4); the script prompts "press cell (r,c)" × K reps (K≈8–10),
   auto-segments the press peaks (reuse `touch_segmenter`), stores the
   peak-window mean 12-D signature per rep → JSONL dataset.
3. Analysis in the same script: unit-normalize signatures, then
   - grouped cross-validated kNN/RandomForest **cell classification** accuracy
     for grid sizes 2×2 → 4×4 (+ confusion heatmap);
   - continuous **(x,y) regression** error in cm;
   - a **non-ML baseline**: weighted centroid of the 4 magnitudes using
     `skin_geometry` sensor coordinates. ML must beat it to earn its place
     (same philosophy as the gesture rule baseline).

**Gate:** adopt the finest grid with ≥~90 % accuracy. If only 2×2 survives,
stop here (quadrants were the honest resolution) and consider Phase 4.

### Phase 1 — Position model + runtime integration

- `src/ml/touch_position.py`: `TouchPositionModel` per `skin_type` (variant as
  one-hot, like gesture models), trained from bench/guided-capture data,
  stored `models/touch_pos_<skin_type>.joblib`. Lazy sklearn; inert without
  the `ml` extra (falls back to quadrants) — same contract as
  `touch_classifier`.
- `MLPositionTracker` returning the **same state dict** as
  `TouchPositionTracker`, extended with `"cell": (row, col)` and
  `"xy": (x, y)`; plugged in via the existing seam
  `MagnetSensorProfile.build_position_tracker` when a model exists (config
  `touch.position: "ml"`), else the current quadrant pair. Consumers of
  `skin.get_touch_position()` keep working unchanged.
- `"force"`: signature magnitude mapped through the calibration reps'
  magnitude range → 0–1 pseudo-force (the roadmap's touch-pressure feature).

### Phase 2 — Guided calibration in the app

- **Tools → Calibrate Touch Position…** dialog (Qt Designer `.ui`, whatsThis
  on every control), modeled on the gesture **Guided live capture**: draws the
  skin from `skin_geometry`, highlights the target cell, counts K presses,
  auto-advances; trains at the end and shows accuracy + confusion; saves the
  model and writes the chosen grid size to the skin's `touch:` block.
- Per-type model reuse across skins of the same `skin_type` (template pattern,
  like `fill_profiles_by_type`); a per-skin recalibration only when the build
  differs.

### Phase 3 — Consumers

- `SkinGridView`: draw the estimated touch point/cell (dot or cell highlight)
  instead of only the per-sensor yellow flash.
- Behavior engine: a `touch_zone` condition/wait block that accepts a cell of
  the calibrated grid (editor dropdown built from the skin's grid size); the
  old quadrant zone names stay as coarse aliases so existing behaviours load.
- Gesture ML (optional, later): feed the cell trajectory as features —
  unlocks motion gestures (slide, circular rub) that the index-based features
  cannot see.

### Phase 4 — only if Phase 0 tops out below the needed resolution

Hardware densification, kept **inert until decided** (project rule):
- populate the existing optional 5th sensor slot (center) — firmware already
  supports it (`MAX_SENSORS = 5`);
- or a denser array of cheaper 3-axis parts (e.g. TLV493D, ~1/6 the price of
  an MLX90393) as a real matrix — new board revision, so last resort;
- magnet-layer tweaks (denser grid, alternating polarity) are cheap physical
  levers that improve separability without touching electronics.

## Gesture taxonomy v2 — squeeze, intensity, slide

The gesture set ({tap, press, compressions}, [TOUCH_ML.md](TOUCH_ML.md)) is
small because pulse count + duration were the only reliable signals. The
position/force channels above unlock a richer set. Design rule: **base classes
stay few; the new dimensions become orthogonal attributes**, so classes don't
multiply and each needs its own labelled data only once.

- **Base classes v2:** `tap`, `press`, `compressions`, **`slide`** (contact
  point travels), **`squeeze`** (whole-hand grasp).
- **Attributes on any gesture:** `intensity` (gentle/normal/strong) and, for
  `slide`, a coarse `direction`.

What each new signal rests on:

| Signal | Physical basis | Depends on |
|---|---|---|
| `intensity` | Signature magnitude **normalized by the expected magnitude at that position** (raw `peak_mag` confounds a strong far touch with a gentle near one — the position model decouples them) | Position Phases 0–1 |
| `slide` | Per-sample (x,y) trajectory inside the segment: path length ≫ net displacement ≠ 0, plus the existing `vec_dir_consistency` (pressing holds one field direction, sliding rotates it) | Position Phases 0–1 |
| `squeeze` | Many sensors active at once with high combined energy and spread-out (often lateral/opposing) vec directions — vs a press's single localized peak | Nothing new — testable on today's stream |

### Track G (runs after position Phase 1; G0-squeeze can start today)

1. **G0 — separability bench.** Extend guided capture
   (`GestureCaptureSession` already does prompt-N-reps-per-class) with the
   candidate classes and attributes; measure grouped-CV confusion per
   `skin_type`. Extend the **rule baseline** first (squeeze: active-sensor
   spread + energy; slide: trajectory length; intensity: normalized force
   thresholds) — ML keeps having to beat it. Gate each class/attribute
   individually: whatever doesn't separate on real data is dropped, not
   shipped.
2. **G1 — features.** Add to `touch_features.py`: normalized force (peak +
   mean), trajectory features (path length, net displacement, direction bin,
   speed), spread features (active count, energy entropy across sensors).
   All computed from the `TouchSegment`'s existing `mags`/`vecs`/`times_ms` —
   position applied per sample via the Phase-1 model. Zero for old recordings,
   so existing datasets stay valid.
3. **G2 — taxonomy + training.** Extend `gesture_taxonomy.py` (classes +
   attribute enums + operational definitions), the train dialog's label
   dropdowns, and the rule baseline. Same per-`skin_type` models, same
   `models/touch_<type>.joblib` flow. Attributes are predicted as secondary
   outputs (or simple calibrated thresholds), not extra classes.
4. **G3 — behaviour engine.** New classes appear as `gesture_count` kinds
   automatically (the study wiring already counts kinds); add an optional
   `min_intensity` filter and a `direction` filter for `slide` in the editor
   block. Latency stays ~`BOUT_GAP_MS` after the gesture ends — fine for
   phase triggers.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Chamber inflation contaminates the signature | Vector compensation + `TransitionGuard` already exist ([TOUCH_COUPLING.md](TOUCH_COUPLING.md)); additionally ignore/flag position while a chamber under the skin is actuating |
| Baseline drift between calibration and use | `rebaseline` between activity phases; adaptive baseline stays usable (it freezes only above-threshold sensors) |
| Force/position entanglement | Unit-normalize the signature for position; magnitude becomes the force channel |
| Multi-touch superposition | Out of scope: single-touch localization only (matches the study's activities) |
| Calibration burden | ~16 cells × 10 reps ≈ 10 min per skin *type*, not per skin |

## Cost of trying

Phase 0 is one script and an afternoon with one skin, zero firmware changes,
zero GUI changes. Everything after it is gated on measured numbers.
