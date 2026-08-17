# Plan - Finer touch localization from the existing MLX90393 array

> Status: **plan** (not implemented). Goal: extract far more spatial
> information from the 4 (up to 5) MLX90393 sensors than the current
> 4-quadrant scheme - a grid of virtual touch points ("matrix"), plus a
> per-touch force estimate - using ML for the inverse mapping.
> Companion docs: [TOUCH_POSITION_TRACKING.md](TOUCH_POSITION_TRACKING.md)
> (current quadrant path), [TOUCH_ML.md](TOUCH_ML.md) (gesture ML),
> [TOUCH_COUPLING.md](TOUCH_COUPLING.md) (actuation contamination).

## Why the sensors are worth more than 4 quadrants

The current `QuadrantDetector` **throws away almost everything** the sensors
measure: it thresholds 4 scalar magnitudes into 4 booleans. What the hardware
actually provides per stream tick (~28 Hz):

- **3-axis field deltas** per sensor (`vec` rows - already implemented in
  `firmware/common/se_magnet.h`, runtime-switchable, no reflash needed):
  4 sensors x 3 axes = a **12-dimensional continuous signature** per touch.
- Good SNR: a touch reads ~100-300 uT near a sensor while MLX90393 noise at
  GAIN_2X/OSR_2/FILTER_3 is a few uT - so even the *weak* response of the
  distant sensors (a few % of the near-sensor signal) is measurable.
- The magnet grid in the silicone means a press at point P displaces a
  *local* set of magnets; each press location produces a distinct **pattern of
  ratios and directions** across the 12 dims. Magnitude scales with press
  depth, but the *normalized* signature is approximately force-invariant -
  so position and force separate cleanly:
  - position ~= f(unit-normalized 12-D signature)  <- learned inverse map
  - force    ~= g(signature magnitude)             <- monotonic, per position

This is a standard "sparse magnetometer array + deformable magnet layer"
localization problem; an ML regressor/classifier on the signature is the
practical way to invert it (a physical dipole-superposition fit is possible
but fragile with an uncalibrated magnet grid).

**Why not reed switches:** reeds are binary, one component + wire per point,
no force signal, no gesture dynamics, mechanical bounce. One reed = one point;
one MLX90393 array = potentially dozens of virtual points *plus* force *plus*
the gesture stream the study already uses. The cost of the MLX90393s is only
wasted if we keep using them as 4 binary switches - which is exactly what this
plan fixes.

## Phases

### Phase 0 - Feasibility bench (decides everything) - **IMPLEMENTED**

No new firmware. In-app (the app ships as a PyInstaller build, so every tool
must live in the GUI): **Tools -> Touch Position Bench...**
(`src/gui/position_bench_dialog.py` + `src/ml/position_bench.py` core;
`scripts/touch_position_bench.py` is the headless dev mirror, same core).

1. The dialog locks onto the streaming touch node, turns on
   `{"cmd":"configure","stream_vec":true}` (runtime toggle) and re-zeros.
2. Tape/print an MxN grid on the skin (start 4x4); the dialog prompts each
   cell on a drawn target grid, K presses each (default 10), with redo/skip,
   periodic re-zero, and an **ETA from the user's own pace** (median
   inter-press interval). Presses are detected PC-side from the *continuous*
   energy sum with a low threshold - the firmware `act` threshold would miss
   presses far from every sensor.
3. A **pattern mode** captures named free-form touches instead of the grid
   (`two_fingers`, `both_hands`, `squeeze`, ...) for the multi-touch question.
4. The report (auto-saved beside the dataset in the recordings folder):
   - cross-validated kNN/RandomForest **cell classification** on the
     unit-normalized 3-axis signatures, vs **magnitude-only** features;
   - merged **2x2** accuracy vs the **dominant-sensor baseline** (~= today's
     quadrant logic) - ML must beat it to earn its place;
   - continuous **(x,y) regression** error in cells (and mm when the cell
     size is given); per-cell recall grid + worst confusions.

**Gate:** adopt the finest grid with >=~90 % accuracy. If only 2x2 survives,
stop here (quadrants were the honest resolution) and consider Phase 4.

### Phase 1 - Position model + runtime integration

- `src/ml/touch_position.py`: `TouchPositionModel` per `skin_type` (variant as
  one-hot, like gesture models), trained from bench/guided-capture data,
  stored `models/touch_pos_<skin_type>.joblib`. Lazy sklearn; inert without
  the `ml` extra (falls back to quadrants) - same contract as
  `touch_classifier`.
- `MLPositionTracker` returning the **same state dict** as
  `TouchPositionTracker`, extended with `"cell": (row, col)` and
  `"xy": (x, y)`; plugged in via the existing seam
  `MagnetSensorProfile.build_position_tracker` when a model exists (config
  `touch.position: "ml"`), else the current quadrant pair. Consumers of
  `skin.get_touch_position()` keep working unchanged.
- `"force"`: signature magnitude mapped through the calibration reps'
  magnitude range -> 0-1 pseudo-force (the roadmap's touch-pressure feature).

### Phase 2 - Guided calibration in the app

- **Tools -> Calibrate Touch Position...** dialog (Qt Designer `.ui`, whatsThis
  on every control), modeled on the gesture **Guided live capture**: draws the
  skin from `skin_geometry`, highlights the target cell, counts K presses,
  auto-advances; trains at the end and shows accuracy + confusion; saves the
  model and writes the chosen grid size to the skin's `touch:` block.
- Per-type model reuse across skins of the same `skin_type` (template pattern,
  like `fill_profiles_by_type`); a per-skin recalibration only when the build
  differs.

### Phase 3 - Consumers

- `SkinGridView`: draw the estimated touch point/cell (dot or cell highlight)
  instead of only the per-sensor yellow flash.
- Behavior engine: a `touch_zone` condition/wait block that accepts a cell of
  the calibrated grid (editor dropdown built from the skin's grid size); the
  old quadrant zone names stay as coarse aliases so existing behaviours load.
- Gesture ML (optional, later): feed the cell trajectory as features -
  unlocks motion gestures (slide, circular rub) that the index-based features
  cannot see.

### Phase 4 - only if Phase 0 tops out below the needed resolution

**No extra sensors fit in the build (user-confirmed 2026-08)**, so the levers
are everything *around* the fixed 4-sensor decoder. Framing: the magnet sheet
is the *encoder* and it is entirely free to redesign - improving resolution
without sensors = a better code + more amplitude + a quieter read.

The bench report is the *diagnostic* that picks the lever, not just the
verdict:

| Failure mode in the report | Limit | Lever |
|---|---|---|
| Cells fail with LOW peak energy | SNR / amplitude | levers 2 + 3 |
| Cells confused DESPITE strong peaks (neighbour confusions) | encoding | lever 1 |

1. **The code - magnet sheet (near-zero cost, next casting):**
   **alternating polarity** in the 4x4 matrix (N/S checkerboard) - with all
   magnets alike, adjacent press points give *similar* signatures; alternated,
   they give near-opposite signs and the class distance jumps. Optionally
   also diversify orientations (some magnets lying at 90 deg) for even more
   unique per-zone fingerprints.
2. **The mechanics - displacement headroom:** a thicker/softer under-layer
   beneath the magnets (lower-shore silicone, or small voids/dimples under
   each magnet) so presses move them further; bonding the sheet to the top
   skin adds the X/Y component (see the build section).
3. **The sensor operating point - firmware only:** raise MLX90393 OSR/filter
   (currently GAIN_2X/OSR_2/FILTER_3) to push noise sub-uT, trading stream
   rate - position does not need 28 Hz, a clean ~15 Hz serves.
4. **The time dimension - software only, data already captured:** the
   signature uses the peak window, but the press *transient* (how the 12-D
   vector grows/rotates) is extra information; bench datasets store the FULL
   segments, so temporal features can be evaluated offline on existing data,
   no recapture.

Exploratory note (not a phase): **active sensing** - a short known chamber
pulse perturbs the magnets in a touch-dependent way; the response could
disambiguate position. Clever but slow and complex; revisit only if all four
levers stall.

If a future board revision ever happens anyway, a denser array of cheap
3-axis parts (e.g. TLV493D) flips the geometry into the regime where the
model-based method wins (see below) - but that is a new PCB, last resort.

## The physical build, and whether it carries the signal

Current construction (2026-08): a **4x4 magnet matrix** embedded in its own
silicone sheet; under the magnets only the thin silicone they sit in, then the
rigid plastic chassis (sensors below it); the chamber skin lies **on top**,
currently not bonded to the magnet sheet.

- **Rigid backing is not a blocker for presses.** The magnet can only move by
  compressing the thin under-layer, but the field gradient near a small
  magnet is steep (dipole: dB/dz ~= 3B/r - at r = 5 mm and B = 1 mT, a 0.1 mm
  approach ~= 60 uT), and today's quadrant detection already reads 100-300 uT
  on this very construction. Sub-mm compression is plenty. A press between
  magnets also *tilts* the neighbours, and a tilt rotates the field - which
  the 3-axis stream sees even when |B| barely changes.
- **The open question is separability, not visibility** - whether presses at
  different points give *distinct* signatures at the 4 sensors. That is a
  property of this build (magnet pitch, layer stiffness) and exactly what the
  bench measures. Run it before any construction change.
- **Bonding the magnet sheet to the chamber skin** (user idea) would couple
  tangential finger motion into X/Y magnet displacement - precisely the
  signal slides and squeezes need, and the 3-axis sensors are built for it.
  Do it **reversibly first** (silicone-glue dots / thin double-sided tape at
  a few points) and run the bench + a slide pattern capture glued vs unglued.
  Caution: bonding also couples **chamber inflation** into the magnets much
  more strongly - recalibrate touch coupling (TOUCH_COUPLING.md) after any
  bonding, and expect the compensation to matter more.

## Multi-touch (two fingers, two hands, several participants)

With 4 sensors, continuously tracking two independent touch points is
underdetermined - not planned. What *is* realistic, and matters for the
study:

- **Broad/bimanual presses as pattern classes.** Two-hands chest compression
  is not two points, it is one wide press: high total energy spread over many
  sensors. Capture it in the bench's pattern mode; classify like `squeeze`.
  The compressions gesture then keys off pulse rhythm exactly as today,
  whether performed with one finger or both hands.
- **A "distributed touch" state instead of a wrong point.** A single-point
  position model can flag signatures that fit no single-press manifold
  (low confidence / novelty) and report `distributed` rather than inventing
  a location. Consumers (activities) treat it as "touched, location wide".
- **Several participants on different skins** is a non-issue - each skin has
  its own node and stream.

## Gesture taxonomy v2 - squeeze, intensity, slide

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
| `intensity` | Signature magnitude **normalized by the expected magnitude at that position** (raw `peak_mag` confounds a strong far touch with a gentle near one - the position model decouples them) | Position Phases 0-1 |
| `slide` | Per-sample (x,y) trajectory inside the segment: path length >> net displacement != 0, plus the existing `vec_dir_consistency` (pressing holds one field direction, sliding rotates it) | Position Phases 0-1 |
| `squeeze` | Many sensors active at once with high combined energy and spread-out (often lateral/opposing) vec directions - vs a press's single localized peak | Nothing new - testable on today's stream |

### Track G (runs after position Phase 1; G0-squeeze can start today)

1. **G0 - separability bench.** Extend guided capture
   (`GestureCaptureSession` already does prompt-N-reps-per-class) with the
   candidate classes and attributes; measure grouped-CV confusion per
   `skin_type`. Extend the **rule baseline** first (squeeze: active-sensor
   spread + energy; slide: trajectory length; intensity: normalized force
   thresholds) - ML keeps having to beat it. Gate each class/attribute
   individually: whatever doesn't separate on real data is dropped, not
   shipped.
2. **G1 - features.** Add to `touch_features.py`: normalized force (peak +
   mean), trajectory features (path length, net displacement, direction bin,
   speed), spread features (active count, energy entropy across sensors).
   All computed from the `TouchSegment`'s existing `mags`/`vecs`/`times_ms` -
   position applied per sample via the Phase-1 model. Zero for old recordings,
   so existing datasets stay valid.
3. **G2 - taxonomy + training.** Extend `gesture_taxonomy.py` (classes +
   attribute enums + operational definitions), the train dialog's label
   dropdowns, and the rule baseline. Same per-`skin_type` models, same
   `models/touch_<type>.joblib` flow. Attributes are predicted as secondary
   outputs (or simple calibrated thresholds), not extra classes.
4. **G3 - behaviour engine.** New classes appear as `gesture_count` kinds
   automatically (the study wiring already counts kinds); add an optional
   `min_intensity` filter and a `direction` filter for `slide` in the editor
   block. Latency stays ~`BOUT_GAP_MS` after the gesture ends - fine for
   phase triggers.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Chamber inflation contaminates the signature | Vector compensation + `TransitionGuard` already exist ([TOUCH_COUPLING.md](TOUCH_COUPLING.md)); additionally ignore/flag position while a chamber under the skin is actuating |
| Baseline drift between calibration and use | `rebaseline` between activity phases; adaptive baseline stays usable (it freezes only above-threshold sensors) |
| Force/position entanglement | Unit-normalize the signature for position; magnitude becomes the force channel |
| Multi-touch superposition | No two-point tracking; broad/bimanual presses become pattern classes and anything else degrades to a `distributed` state (see the multi-touch section) |
| Calibration burden | ~16 cells x 10 reps ~= 10 min per skin *type*, not per skin |

## Why learned inverse map, not analytic magnet localization

The "proper" algorithm for this problem is model-based: express each sensor
reading as the superposed dipole fields of all 16 magnets, then solve the
nonlinear inverse problem (Levenberg-Marquardt style) for the magnet
displacements on every frame. That is the heavy-math route, and it is
deliberately **not** what this plan does, because on this hardware it is the
fragile route: it needs per-magnet moments and exact positions, sensor
calibration, a deformation model for silicone (not free space), and it
degrades badly when magnets move together under a finger. The learned inverse
map sidesteps all of it - the calibration presses *sample the real physics of
the real build*, classifier/regressor interpolate between samples, and the
cross-validated accuracy is an honest measure of whether it works. The trade:
it only answers within the calibrated region and needs recapture when the
build changes - acceptable, since models are per skin *type*.

Three clarifications that keep this decision honest:

- **The information ceiling is shared.** 12 measured dims bound every
  algorithm equally: press locations whose signatures coincide are
  unresolvable for LM inversion and for ML alike. The bench measures that
  ceiling directly, so "a smarter algorithm" cannot beat what the bench says
  the data carries.
- **When model-based would win:** with the geometry inverted - many sensors,
  few magnets (e.g. a dense cheap-sensor matrix tracking 1-4 magnets), the
  dipole fit becomes overdetermined and then it generalizes without per-skin
  calibration and handles multi-touch by model selection. Revisit if Phase 4
  ever builds that hardware. Note the classic magnet-tracking literature
  works in exactly that regime (1 magnet, 12+ sensor channels); ours is the
  opposite (16 magnets, 12 channels, ~96 unknowns) and needs a silicone
  deformation model to even be regularizable.
- **Middle road (Phase 1 upgrade path):** a Gaussian-Process regressor with a
  physics-informed prior/kernel (dipole decay) - still trained on the bench
  data, but smoother interpolation and *calibrated uncertainty*, which is
  what the multi-touch `distributed` state needs. More math where it pays,
  none where it breaks.

Bench datasets double as the referee: any future physics-based attempt scores
against the same labelled presses, same gate.

## Cost of trying

Phase 0 is taping a grid on one skin and ~15 min of guided pressing in
Tools -> Touch Position Bench... - zero firmware changes. Everything after it is
gated on measured numbers.
