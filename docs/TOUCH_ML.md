# Touch types (rules first, a trained model on top)

How a child touches a skin is described on four independent **axes**, and the
gesture name is derived from their combination
(`src/ml/gesture_taxonomy.py`, `src/ml/touch_axes.py`):

| Axis | Values | How it is read |
|---|---|---|
| form | short, press, rest, repeated regular, repeated irregular | duration and the gaps between pulses |
| movement | static, slide, rub | the order sensors activated in (and the 3-axis data when streamed) |
| force | light, medium, strong | peak signal against the skin's activation threshold |
| extent | point, multi, whole | sensors active at once |

| Gesture | Combination |
|---|---|
| tap | short, static |
| hit | very short, static, strong |
| press | sustained, static |
| rest | held for seconds |
| compressions | repeated, regular |
| pats | repeated, irregular |
| stroke | slide |
| rub | back and forth |
| squeeze | several zones held while the chamber pressure rises |

Pinch, poke, tickle and scratch are out of reach with a handful of magnet
sensors per skin and are deliberately not classes.

**Rules name every gesture**, so the app recognises touches before anything
is trained. **A trained model then names the gesture instead**, wherever one
exists for the skin type: capture labelled examples in Tools -> Touch
Gestures ("Guided live capture") and train. The report shows the model's
cross-validated accuracy next to the rules' on the same touches, so you can
see whether it earns its place. A model saved by an older version is refused
at load time and the rules are used until it is retrained.

Two things the model does not decide:

- **Squeeze.** It is told apart from a plain multi-zone press by the rise of
  the skin's chamber pressure during the touch, which the model never sees.
  That evidence outranks the model. Chambers the robot is inflating or
  deflating at that moment are ignored (they change by themselves), and the
  pressure only updates every 500 ms, so a squeeze must last about a second.
- **The axes.** They always come from the rules (the movement is taken from
  the model's gesture), and each logged touch keeps what the rules would have
  called it (`rule_gesture`) and who named it (`named_by`).

Every recognised touch is logged (`gesture` event) with its axes and the
continuous descriptors behind them, so the names can be redefined later
without re-running a session. `gesture_count` accepts every gesture as a kind.

Training data caveat: a model learns the people who trained it. An hour of
one adult's touches is enough to get it working, not to trust it on children;
capture from several people before relying on the accuracy figure.

## Design in one paragraph

Models are **per skin type** (`skin.skin_type`). Within a type the sensor index
is stable (sensor *i* is always the same physical spot), so features are
**coordinate-free / index-based** - they never need the (unreliable) sensor
coordinates. The silicone **variant** (`skin.skin_variant`) is orthogonal to
the shape, so it is fed to the per-type model as a one-hot feature block
(`variant_features` in `touch_features.py`) instead of splitting the model
further. The geometry registry (`src/hardware/skin_geometry.py`) holds each
type's shape + sensor coordinates as editable constants, used by the GUI to draw
the skin; the ML core does not depend on the coordinates (only the variant list
is shared from it).

## Modules

| File | Role |
|---|---|
| `src/hardware/skin_geometry.py` | hardcoded geometry registry keyed by `skin_type` |
| `src/data/stream_recorder.py` | records all gateway messages of a session -> JSONL |
| `src/ml/gesture_taxonomy.py` | label set + operational definitions + thresholds |
| `src/ml/touch_segmenter.py` | magnet stream -> press->release `TouchSegment`s |
| `src/ml/touch_features.py` | coordinate-free feature vector per segment |
| `src/ml/rule_baseline.py` | rules-only classifier (training comparison) |
| `src/ml/touch_classifier.py` | per-type model load + inference (lazy sklearn) |
| `src/ml/labeling.py` | segment + align live tags + CSV import/export (shared) |
| `src/ml/training.py` | shared training core (segment -> match -> fit), lazy sklearn |
| `src/gui/train_touch_dialog.py` | **Tools -> Touch Gestures...** in-app label + train |
| `scripts/label_touches.py` | CLI front-end for `src/ml/labeling.py` |
| `scripts/train_touch_model.py` | CLI front-end for `src/ml/training.py` |

`numpy` / `scikit-learn` / `joblib` are the optional `ml` extra - needed **only**
to train and to run a trained model. The app, recording, segmentation and
feature extraction run without them; the classifier is inert (returns `unknown`)
when absent. Install with `pip install -e '.[ml]'`.

## Workflow - all in the app

**Tools -> Touch Gestures...** does the whole flow without a terminal:

1. **Record.** Run sessions with "Record sensor streams" ticked (default). Each
   writes a JSONL into the recordings folder (configurable in **Settings ->
   Recordings**; default `<data>/recordings/<session_id>.jsonl`).
2. **Label live.** During the session, tap the gesture class in the observer
   panel while a child performs it - logged as a `gesture_label` event in the
   session's event log.
3. **Label & edit in-app.** In the Touch Gestures dialog: **Add recording** ->
   each touch segment is listed, its skin type/variant read from the recording
   header (per-source maps stamped by the recorder) and its label pre-filled
   from the nearest live tag; correct any gesture from the dropdown, and
   **Group** the presses of a compressions bout into one gesture
   (a shared `group_id`). **Import/Export CSV** to hand-edit or share datasets
   (`skin_type,skin_variant,source,start_ms,end_ms,label,group_id`).
4. **Train.** Click **Train models** - one model per `skin_type`, with a
   rule-baseline comparison and report, written to `models/touch_<skin_type>.joblib`.
5. **Infer.** `LiveTouchClassifier` (in `touch_classifier.py`) loads the model
   for a skin's type and emits `gesture` events live; inert until a model exists.

A CLI exists for scripted/batch use and mirrors the dialog
(`scripts/label_touches.py`, `scripts/train_touch_model.py`); both share
`src/ml/labeling.py` and `src/ml/training.py`. Without the `ml` extra installed,
training says so instead of failing.

## Train/serve parity & 3-axis features

- Recordings carry the **compensated** magnet stream alongside raw whenever
  pressure compensation is enabled (flagged `compensated`); training prefers it,
  because live inference consumes the compensated stream (`subscribe_skin_magnet`).
  The coupling calibration, conversely, always ignores compensated lines.
- Live inference groups a run of presses with a `PulseMerger`
  (`BOUT_GAP_MS` = 1200 ms) so a `compressions` bout reaches the model as one
  merged segment - matching how it is labelled and trained. The gap must bridge
  the quiet between presses in a bout, so it costs ~`BOUT_GAP_MS` of latency
  before any gesture is reported (fine for the study's phase triggers).
- When the node streams 3-axis `vec` deltas (see `docs/TOUCH_COUPLING.md`),
  segments carry them and three direction features are extracted
  (`vec_present`, `vec_dir_consistency`, `vec_z_frac`) - pressing holds one
  direction, sliding rotates it. All-zero on scalar-only data, so old
  recordings stay valid.

## Honest limits

- At most 4 sensors per skin (1-2 on some types) cap what any method recovers -
  the taxonomy's classes are realistic; fine "quality" (gentle vs firm) is not.
- Cross-validation is grouped by recording (proxy for participant) until
  per-segment participant labels are added.
- Classes are a starting point; refine the taxonomy once real segments exist.
