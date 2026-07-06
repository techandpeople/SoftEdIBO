"""Touch-gesture taxonomy — the label set + operational definitions.

Single source of truth shared by live labeling (observer panel), the rule
baseline, and the training script. Definitions are expressed in terms of
**contact duration and pulse count**, not metric coordinates, because the touch
hardware is sparse (4 magnetic sensors per skin) and per-skin-type models make
the indices stable. Thresholds are tunable constants — refine them once real
labelled segments exist.

The set is deliberately small and well separated for a 4-magnet skin:

* ``tap``          — one quick press (disambiguation / selection);
* ``press``        — one sustained press held steady;
* ``compressions`` — a *rhythmic bout* of repeated presses in a row (chest
  compressions / a heartbeat rhythm). This is the study's active gesture: it is
  what the child does to advance a behaviour phase (see the ``compressions``
  ``gesture_count`` transitions in the study specs).

``tap`` and ``press`` carry a single press→release pulse; ``compressions`` is
many pulses merged into one gesture, so the three separate cleanly on
``n_pulses`` + duration — signals the sparse magnet array measures reliably.
"""

from __future__ import annotations

# Class labels.
TAP = "tap"
PRESS = "press"
COMPRESSIONS = "compressions"
UNKNOWN = "unknown"

GESTURE_CLASSES: tuple[str, ...] = (TAP, PRESS, COMPRESSIONS)

# Operational definitions (also used as button tooltips / docs).
DEFINITIONS: dict[str, str] = {
    TAP:          "One short press — a quick touch, then release.",
    PRESS:        "One sustained press, held steady on the skin.",
    COMPRESSIONS: "A rhythmic bout of repeated presses in a row (like chest "
                  "compressions or a heartbeat). Press–release several times "
                  "without long pauses; the run is grouped into one gesture.",
    UNKNOWN:      "Not classifiable / noise.",
}

# Tunable thresholds for the rule baseline (and as priors for ML). Durations in
# milliseconds. Refine against real data.
TAP_MAX_MS = 250          # at/under this and single-pulse → tap
PRESS_MIN_MS = 600        # at/over this and single-pulse → press

# A merged gesture with at least this many press→release pulses is a
# compressions bout (rhythmic repeated pressing), not a single tap/press.
COMPRESSIONS_MIN_PULSES = 3

# How long the skin must stay untouched before a run of presses is closed off as
# one gesture. It must bridge the quiet gap BETWEEN presses within a bout (a
# ~60–120/min compression cadence leaves ~300–700 ms of silence per cycle) so
# the whole bout merges into one ``compressions`` gesture — yet stay short enough
# that two deliberately separate touches don't merge into one. 1200 ms covers
# slow (massage-rate) bouts; raise it toward a few seconds if the compression
# cadence is slower, at the cost of more latency before ANY gesture is reported.
# Used by BOTH guided capture and live inference (train/serve parity), so the
# merged segment the model learns matches the one inference feeds it.
BOUT_GAP_MS = 1200

# Slide (drag) detection — shared by src.ml.touch_motion (capture summary) and
# the live motion-trail arrow (src.gui.sensor_grid_view), so both judge "is
# this really a slide?" identically.
SLIDE_MIN_TRAVEL_MM = 12.0   # start→end travel below this is a static touch
# With 3-axis vec data: mean |z|-fraction of the deltas at/above this means the
# magnet was pushed straight DOWN (tap/press) — a real slide drags it laterally
# (x/y shear). Only applied when the node streams ``vec``.
SLIDE_MAX_Z_FRAC = 0.9

# Straightness = net displacement / total path length (0..1). A finger drags in
# a roughly straight line (near 1); chamber inflation shifts the magnet so the
# touch centroid wanders/circles between coupled sensors (near 0). Below this,
# there is no single travel direction — not a slide (kills the "arrow going in
# circles while inflating" artefact).
SLIDE_MIN_STRAIGHTNESS = 0.6

# Master switch for the live slide/direction visuals (the motion-trail arrow on
# the sensor grid + the travel direction in the capture summary). Temporarily
# OFF: the direction read wasn't reliable enough yet and distracts during ML
# gesture capture. The detection code and its tests stay in place — flip this to
# True to bring the feature back. Does NOT affect any ML feature (those live in
# touch_features, independent of this).
SLIDE_DETECTION_ENABLED = False
