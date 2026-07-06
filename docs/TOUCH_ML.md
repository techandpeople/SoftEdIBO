# Touch-gesture ML (exploratory)

Pipeline to classify *how* a child touches a skin — three well-separated
classes, **tap / press / compressions** (`src/ml/gesture_taxonomy.py`) — on top
of the existing presence/location touch sensing:

- **tap** — one quick press (disambiguation / selection);
- **press** — one sustained press held steady;
- **compressions** — a *rhythmic bout* of repeated presses in a row (chest
  compressions / a heartbeat rhythm). This is the study's active gesture: it is
  offered as a `gesture_count` kind, so a behaviour authored in the visual editor
  can advance a phase when the child performs it (`{"gesture_count": {"kind":
  "compressions"}}`).

The set is deliberately small: `tap`/`press` carry one press→release pulse and
`compressions` is many pulses merged into one gesture, so all three separate on
**pulse count + duration** — signals a sparse magnet array measures reliably. It
is still **exploratory**: the hardware is sparse (at most 4 quadrant sensors per
skin; some types carry only 1 or 2), so a rule baseline is kept as a comparison
and ML only earns its place if it beats it on real labelled data. No video —
only sensor streams.

## Design in one paragraph

Models are **per skin type** (`skin.skin_type`). Within a type the sensor index
is stable (sensor *i* is always the same physical spot), so features are
**coordinate-free / index-based** — they never need the (unreliable) sensor
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
| `src/data/stream_recorder.py` | records all gateway messages of a session → JSONL |
| `src/ml/gesture_taxonomy.py` | label set + operational definitions + thresholds |
| `src/ml/touch_segmenter.py` | magnet stream → press→release `TouchSegment`s |
| `src/ml/touch_features.py` | coordinate-free feature vector per segment |
| `src/ml/rule_baseline.py` | rules-only classifier (training comparison) |
| `src/ml/touch_classifier.py` | per-type model load + inference (lazy sklearn) |
| `src/ml/labeling.py` | segment + align live tags + CSV import/export (shared) |
| `src/ml/training.py` | shared training core (segment → match → fit), lazy sklearn |
| `src/gui/train_touch_dialog.py` | **Tools → Touch Gestures…** in-app label + train |
| `scripts/label_touches.py` | CLI front-end for `src/ml/labeling.py` |
| `scripts/train_touch_model.py` | CLI front-end for `src/ml/training.py` |

`numpy` / `scikit-learn` / `joblib` are the optional `ml` extra — needed **only**
to train and to run a trained model. The app, recording, segmentation and
feature extraction run without them; the classifier is inert (returns `unknown`)
when absent. Install with `pip install -e '.[ml]'`.

## Workflow — all in the app

**Tools → Touch Gestures…** does the whole flow without a terminal:

1. **Record.** Run sessions with "Record sensor streams" ticked (default). Each
   writes a JSONL into the recordings folder (configurable in **Settings →
   Recordings**; default `<data>/recordings/<session_id>.jsonl`).
2. **Label live.** During the session, tap the gesture class in the observer
   panel while a child performs it — logged as a `gesture_label` event in the
   session's event log.
3. **Label & edit in-app.** In the Touch Gestures dialog: **Add recording** →
   each touch segment is listed, its skin type/variant read from the recording
   header (per-source maps stamped by the recorder) and its label pre-filled
   from the nearest live tag; correct any gesture from the dropdown, and
   **Group** the presses of a compressions bout into one gesture
   (a shared `group_id`). **Import/Export CSV** to hand-edit or share datasets
   (`skin_type,skin_variant,source,start_ms,end_ms,label,group_id`).
4. **Train.** Click **Train models** — one model per `skin_type`, with a
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
  merged segment — matching how it is labelled and trained. The gap must bridge
  the quiet between presses in a bout, so it costs ~`BOUT_GAP_MS` of latency
  before any gesture is reported (fine for the study's phase triggers).
- When the node streams 3-axis `vec` deltas (see `docs/TOUCH_COUPLING.md`),
  segments carry them and three direction features are extracted
  (`vec_present`, `vec_dir_consistency`, `vec_z_frac`) — pressing holds one
  direction, sliding rotates it. All-zero on scalar-only data, so old
  recordings stay valid.

## Honest limits

- At most 4 sensors per skin (1–2 on some types) cap what any method recovers —
  the taxonomy's classes are realistic; fine "quality" (gentle vs firm) is not.
- Cross-validation is grouped by recording (proxy for participant) until
  per-segment participant labels are added.
- Classes are a starting point; refine the taxonomy once real segments exist.
