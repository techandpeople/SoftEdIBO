# Touch-gesture ML (exploratory)

Pipeline to classify *how* a child touches a skin (tap / press / stroke /
squeeze), on top of the existing presence/location touch sensing. It is
**exploratory**: the hardware is sparse (4 magnetic sensors per skin), so a
rule baseline is kept as a comparison and ML only earns its place if it beats it
on real labelled data. No video — only sensor streams.

## Design in one paragraph

Models are **per skin type** (`skin.skin_type`). Within a type the sensor index
is stable (sensor *i* is always the same physical spot), so features are
**coordinate-free / index-based** — they never need the (unreliable) sensor
coordinates. The geometry registry (`src/hardware/skin_geometry.py`) holds each
type's shape + sensor coordinates as editable constants, used by the GUI to draw
the skin; the ML core does not depend on them.

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

**Tools → Touch Gestures (label & train)…** does the whole flow without a
terminal:

1. **Record.** Run sessions with "Record sensor streams" ticked (default). Each
   writes a JSONL into the recordings folder (configurable in **Settings →
   Recordings**; default `<data>/recordings/<session_id>.jsonl`).
2. **Label live.** During the session, tap the gesture class in the observer
   panel while a child performs it — logged as a `gesture_label` event.
3. **Label & edit in-app.** In the Touch Gestures dialog: **Add recording** →
   each touch segment is listed and auto-filled from the live tags; pick the
   `skin_type`, correct any gesture from the dropdown. **Import/Export CSV** to
   hand-edit or share datasets (`skin_type,source,start_ms,end_ms,label`).
4. **Train.** Click **Train models** — one model per `skin_type`, with a
   rule-baseline comparison and report, written to `models/touch_<skin_type>.joblib`.

A CLI exists for scripted/batch use and mirrors the dialog
(`scripts/label_touches.py`, `scripts/train_touch_model.py`); both share
`src/ml/labeling.py` and `src/ml/training.py`. Without the `ml` extra installed,
training says so instead of failing.
5. **Infer.** `LiveTouchClassifier` (in `touch_classifier.py`) loads the model
   for a skin's type and emits `gesture` events live; inert until a model exists.

## Honest limits

- 4 sensors per skin cap what any method recovers — tap/press/stroke/squeeze are
  realistic; fine "quality" (gentle vs firm) is not.
- Cross-validation is grouped by recording (proxy for participant) until
  per-segment participant labels are added.
- Classes are a starting point; refine the taxonomy once real segments exist.
