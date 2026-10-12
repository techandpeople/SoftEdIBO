"""Touch-gesture taxonomy - the label set + operational definitions.

Single source of truth shared by live labeling (observer panel), the rule
baseline, and the training script. Definitions are expressed in terms of
**contact duration and pulse count**, not metric coordinates, because the touch
hardware is sparse (4 magnetic sensors per skin) and per-skin-type models make
the indices stable. Thresholds are tunable constants - refine them once real
labelled segments exist.

A touch is described on four independent **axes**, and the gesture name is
derived from their combination (:func:`derive_gesture`):

* **form**     - its shape in time: short, press, rest, repeated regular,
  repeated irregular (rules on duration and the gaps between pulses);
* **movement** - static, slide (one direction) or rub (back and forth). The
  only axis that needs a trained model to be reliable; rules give a fallback;
* **force**    - light, medium, strong (peak signal against the skin's
  activation threshold);
* **extent**   - one point, several zones, the whole skin (sensors active at
  once).

Derived gestures:

* ``tap``          - short, static;
* ``hit``          - very short, static, strong;
* ``press``        - one sustained press held steady;
* ``rest``         - a hand left resting on the skin for seconds;
* ``compressions`` - a *regular* bout of repeated presses (chest compressions
  / a heartbeat rhythm). The study's active gesture: it is what the child does
  to advance a behaviour phase;
* ``pats``         - repeated presses with no steady rhythm;
* ``stroke``       - a touch that travels across the skin in one direction;
* ``rub``          - a touch that travels back and forth;
* ``squeeze``      - several zones held while the chamber pressure rises. Only
  derived when a pressure rise is supplied with the touch.

Out of reach with a handful of magnet sensors per skin, and deliberately not
classes: pinch, poke, tickle, scratch.

Keeping the axes (and the continuous descriptors behind them) with every
touch means the gesture names can be redefined later without re-running a
session.
"""

from __future__ import annotations

# Class labels.
TAP = "tap"
PRESS = "press"
REST = "rest"
COMPRESSIONS = "compressions"
PATS = "pats"
STROKE = "stroke"
RUB = "rub"
SQUEEZE = "squeeze"
HIT = "hit"
UNKNOWN = "unknown"

GESTURE_CLASSES: tuple[str, ...] = (
    TAP, PRESS, REST, COMPRESSIONS, PATS, STROKE, RUB, SQUEEZE, HIT)

# Operational definitions (also used as button tooltips / docs).
DEFINITIONS: dict[str, str] = {
    TAP:          "One short press - a quick touch, then release.",
    PRESS:        "One sustained press, held steady on the skin.",
    REST:         "A hand left resting on the skin for several seconds.",
    COMPRESSIONS: "A rhythmic bout of repeated presses in a row (like chest "
                  "compressions or a heartbeat). Press-release several times "
                  "without long pauses; the run is grouped into one gesture.",
    PATS:         "A few quick presses in a row with no steady rhythm.",
    STROKE:       "A touch that slides across the skin in one direction.",
    RUB:          "A touch that slides back and forth across the skin.",
    SQUEEZE:      "Several zones held at once, hard enough to raise the "
                  "pressure inside the skin.",
    HIT:          "A very short, strong strike.",
    UNKNOWN:      "Not classifiable / noise.",
}

# How each gesture reads in a sentence ("after <label> 3 times").
LABELS: dict[str, str] = {
    TAP: "a tap", PRESS: "a press", REST: "a resting hand",
    COMPRESSIONS: "compressions", PATS: "pats", STROKE: "a stroke",
    RUB: "a rub", SQUEEZE: "a squeeze", HIT: "a hit",
}

# --- Axes -----------------------------------------------------------------
FORM_SHORT = "short"
FORM_PRESS = "press"
FORM_REST = "rest"
FORM_REGULAR = "repeated_regular"
FORM_IRREGULAR = "repeated_irregular"
FORMS = (FORM_SHORT, FORM_PRESS, FORM_REST, FORM_REGULAR, FORM_IRREGULAR)

MOVE_STATIC = "static"
MOVE_SLIDE = "slide"
MOVE_RUB = "rub"
MOVEMENTS = (MOVE_STATIC, MOVE_SLIDE, MOVE_RUB)

FORCE_LIGHT = "light"
FORCE_MEDIUM = "medium"
FORCE_STRONG = "strong"
FORCES = (FORCE_LIGHT, FORCE_MEDIUM, FORCE_STRONG)

EXTENT_POINT = "point"
EXTENT_MULTI = "multi"
EXTENT_WHOLE = "whole"
EXTENTS = (EXTENT_POINT, EXTENT_MULTI, EXTENT_WHOLE)

# The movement each gesture is an example of: what the movement model learns
# from a labelled capture (every other axis is rule-based).
MOVEMENT_OF: dict[str, str] = {
    **dict.fromkeys(GESTURE_CLASSES, MOVE_STATIC),
    STROKE: MOVE_SLIDE, RUB: MOVE_RUB,
}

# Tunable thresholds for the rule baseline (and as priors for ML). Durations in
# milliseconds. Refine against real data.
TAP_MAX_MS = 250          # at/under this and single-pulse -> tap
PRESS_MIN_MS = 600        # at/over this and single-pulse -> press

# A merged gesture with at least this many press->release pulses is a
# compressions bout (rhythmic repeated pressing), not a single tap/press.
COMPRESSIONS_MIN_PULSES = 3

# Form axis. A single press held at least this long is a resting hand.
REST_MIN_MS = 3000
# Two or more pulses merged into one gesture are a repeated touch; it is
# "regular" when the gaps between pulses vary by at most this much
# (coefficient of variation: standard deviation / mean).
REPEATED_MIN_PULSES = 2
REGULAR_MAX_CV = 0.25

# Force axis: the touch's peak against the skin's activation threshold.
FORCE_MEDIUM_RATIO = 2.0
FORCE_STRONG_RATIO = 4.0

# A short, strong touch at/under this duration is a hit, not a tap.
HIT_MAX_MS = 150
# Chamber pressure rise (kPa) during a multi-zone hold that makes it a squeeze.
SQUEEZE_MIN_KPA = 0.5

# How long the skin must stay untouched before a run of presses is closed off as
# one gesture. It must bridge the quiet gap BETWEEN presses within a bout (a
# ~60-120/min compression cadence leaves ~300-700 ms of silence per cycle) so
# the whole bout merges into one ``compressions`` gesture - yet stay short enough
# that two deliberately separate touches don't merge into one. 1200 ms covers
# slow (massage-rate) bouts; raise it toward a few seconds if the compression
# cadence is slower, at the cost of more latency before ANY gesture is reported.
# Used by BOTH guided capture and live inference (train/serve parity), so the
# merged segment the model learns matches the one inference feeds it.
BOUT_GAP_MS = 1200

# Slide (drag) detection - shared by src.ml.touch_motion (capture summary) and
# the live motion-trail arrow (src.gui.sensor_grid_view), so both judge "is
# this really a slide?" identically.
SLIDE_MIN_TRAVEL_MM = 12.0   # start->end travel below this is a static touch
# With 3-axis vec data: mean |z|-fraction of the deltas at/above this means the
# magnet was pushed straight DOWN (tap/press) - a real slide drags it laterally
# (x/y shear). Only applied when the node streams ``vec``.
SLIDE_MAX_Z_FRAC = 0.9

# Straightness = net displacement / total path length (0..1). A finger drags in
# a roughly straight line (near 1); chamber inflation shifts the magnet so the
# touch centroid wanders/circles between coupled sensors (near 0). Below this,
# there is no single travel direction - not a slide (kills the "arrow going in
# circles while inflating" artefact).
SLIDE_MIN_STRAIGHTNESS = 0.6

# Master switch for the live slide/direction visuals (the motion-trail arrow on
# the sensor grid + the travel direction in the capture summary). Temporarily
# OFF: the direction read wasn't reliable enough yet and distracts during ML
# gesture capture. The detection code and its tests stay in place - flip this to
# True to bring the feature back. Does NOT affect any ML feature (those live in
# touch_features, independent of this).
SLIDE_DETECTION_ENABLED = False


def derive_gesture(form: str, movement: str, force: str, extent: str,
                   n_pulses: int = 1, duration_ms: float = 0.0,
                   pressure_rise_kpa: float | None = None) -> str:
    """Name the gesture a combination of axis values describes.

    ``pressure_rise_kpa`` is how much the chamber pressure rose during the
    touch, when known; a squeeze is only derived when it is supplied."""
    if movement == MOVE_RUB:
        return RUB
    if movement == MOVE_SLIDE:
        return STROKE
    if (extent in (EXTENT_MULTI, EXTENT_WHOLE)
            and form in (FORM_PRESS, FORM_REST)
            and pressure_rise_kpa is not None
            and pressure_rise_kpa >= SQUEEZE_MIN_KPA):
        return SQUEEZE
    if form == FORM_REGULAR and n_pulses >= COMPRESSIONS_MIN_PULSES:
        return COMPRESSIONS
    if form in (FORM_REGULAR, FORM_IRREGULAR):
        return PATS
    if form == FORM_SHORT:
        strong_strike = force == FORCE_STRONG and duration_ms <= HIT_MAX_MS
        return HIT if strong_strike else TAP
    if form == FORM_REST:
        return REST
    if form == FORM_PRESS:
        return PRESS
    return UNKNOWN
