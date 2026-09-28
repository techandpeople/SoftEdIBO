"""Action & condition catalogue for the declarative behaviour engine.

This module is the **single source of truth** for what a behaviour spec may
contain. It serves two consumers:

- :class:`~src.activities.scripted_activity.ScriptedActivity` - the runtime
  interpreter validates specs against these schemas and dispatches each verb.
- The Blockly block editor (``src/gui/blockly/editor.html``) - it receives
  :func:`editor_catalog` as JSON and generates one block per verb (and per
  variant) from it, including the compile (blocks -> spec) and decompile
  (spec -> blocks) directions. Adding a verb here makes a block appear; the
  page needs no edit unless a brand-new field *type* is introduced.

A behaviour **spec** is plain data (JSON-serialisable). Shape::

    {
      "initial": "phase1",
      "target": {"kind": "thymio", "skin": "natural"},   # optional
      "states": {
        "phase1": {
          "final":       false,             # optional: marks a terminal phase
          "do":          [ <step>, ... ],   # body, runs once on enter
          "on_touch":    [ <step>, ... ],   # optional, spawned per touch press
          "transitions": [ {"to": "phase2", "when": <condition>}, ... ]
        },
        ...
      }
    }

Each **step** is a single-key dict ``{verb: params}`` where ``params`` is a
dict (or a scalar shorthand, e.g. ``{"wait": 500}``). Control-flow verbs
(``sequence`` / ``repeat`` / ``for_each_chamber`` / ``if_robot``) nest more
steps under ``do`` (and ``else`` for ``if_robot``). Instantaneous verbs apply
immediately; ``wait`` / ``wait_for_touch`` suspend the running sequence until
satisfied - that is what lets an author write "inflate 1, wait, deflate 1,
inflate 2 ..." as a literal sequence.

A **condition** is a single-key dict too: ``{"elapsed_ms": 120000}``,
``{"touch_count": {"min": 10}}``, or a combinator ``{"any": [..]}`` /
``{"all": [..]}`` / ``{"not": <cond>}``.

Editor metadata
---------------
Every :class:`Verb` carries a ``template``: the words of its block, one row
per line, with ``{field}`` placeholders where the field's widget goes. Every
:class:`VerbField` says how it is edited through its ``type`` (see the
``EDITOR_FIELD_TYPES`` table), optional ``labels`` for enum choices,
``min``/``max`` bounds, and ``show_when`` (the field - and its whole row when
every field in it is conditional - only shows when another field has one of
the listed values, and is left out of the spec while hidden).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.activities import activity_kind, skin_condition
from src.activities.led_canvas import FILL_MODES, HOLD_MODES
from src.activities.touch_rhythm import MODE_AUTO, SYNC_MODES
from src.ml.gesture_taxonomy import GESTURE_CLASSES

# Field types the editor knows how to render / compile. Anything else is a
# programming error caught by ``test_catalog_editor``.
#   int / float / pct  number box (pct is clamped 0-100)
#   ms                 number box + unit dropdown (ms / s / min), stored as ms
#   color              colour swatch (palette + custom picker)
#   colors             ``count`` colour swatches, stored as a list
#   enum               dropdown over ``choices`` (labelled via ``labels``)
#   chamber            dropdown: current / all / 0..7
#   ring               dropdown: all / 0..2
#   power              the 1-5 pump-power dial
#   phase              dropdown of the phases in the workspace
#   text               free text box
#   ints               ``count`` number boxes, stored as a list of ints
#   steps              a statement input (nested steps)
#   cond               a value input taking one condition block
#   conds              2-4 value inputs taking condition blocks (count picker)
EDITOR_FIELD_TYPES = (
    "int", "float", "pct", "ms", "color", "colors", "enum", "chamber",
    "ring", "power", "phase", "text", "ints", "steps", "cond", "conds",
)


@dataclass(frozen=True)
class VerbField:
    """One parameter of an action/condition verb (drives block inputs)."""
    name: str
    type: str                       # one of EDITOR_FIELD_TYPES
    default: Any = None
    choices: tuple[Any, ...] = ()
    description: str = ""
    # --- editor metadata ---
    labels: tuple[tuple[Any, str], ...] = ()   # (choice value, human label)
    min: float | None = None
    max: float | None = None
    # (other field name, accepted values): show this field only while the
    # other field holds one of the values; hidden fields stay out of the spec.
    show_when: tuple[str, tuple[Any, ...]] | None = None
    count: int = 0                  # colors / ints: number of slots
    hidden: bool = False            # spec-only, never shown in the editor
    omit_default: bool = False      # editor drops the key when at the default

    def label_for(self, value: Any) -> str:
        for v, label in self.labels:
            if v == value:
                return label
        return str(value)


@dataclass(frozen=True)
class Variant:
    """An extra toolbox block for the same verb - e.g. 'set LED quarters' is
    ``set_led_halves`` with four colours. ``count`` overrides the slot count
    of the verb's ``colors`` / ``ints`` field."""
    id: str
    template: str
    count: int


@dataclass(frozen=True)
class Verb:
    name: str
    kind: str                       # "action" | "control" | "condition"
    description: str
    fields: tuple[VerbField, ...] = field(default_factory=tuple)
    # Activity kinds (activity_kind.KINDS) the verb is restricted to. Empty =
    # valid everywhere. Only enforced for kind-targeted specs: a
    # skin-targeted behaviour may use e.g. the Thymio wheel verbs anywhere
    # (wrap them in an `if_robot` block; on other robots they no-op).
    kinds: tuple[str, ...] = ()
    # --- editor metadata ---
    template: str = ""              # block rows ("\n"-separated), {field} slots
    category: str = ""              # toolbox category (CATEGORIES)
    variants: tuple[Variant, ...] = ()
    in_toolbox: bool = True         # False = decompiles, but not offered

    def field_named(self, name: str) -> VerbField | None:
        return next((f for f in self.fields if f.name == name), None)


# Toolbox categories in display order, with their Blockly hue.
CATEGORIES: tuple[tuple[str, int], ...] = (
    ("Phases", 290), ("Lights", 230), ("Chambers", 30), ("Thymio", 160),
    ("Control", 120), ("Conditions", 20),
)

# ---------------------------------------------------------------------------
# Chamber addressing - every chamber action accepts one of these.
# ---------------------------------------------------------------------------
#   int      -> that local chamber index
#   "all"    -> every chamber of the unit
#   "current"/None -> the chamber bound by the enclosing for_each_chamber
CHAMBER_CHOICES: tuple[Any, ...] = ("current", "all", 0, 1, 2, 3, 4, 5, 6, 7)
CHAMBER_FIELD = VerbField(
    name="chamber", type="chamber", default="current",
    choices=CHAMBER_CHOICES,
    description="Chamber index, 'all', or 'current' (the for_each_chamber "
                "binding). Defaults to the current chamber.",
)

# Direct pump-PWM control, shared by the verbs that drive a chamber up. A lower
# duty makes a gentler / lower-energy stroke; 0 (or None) means "full speed".
# Unlike period_ms it needs no calibrated fill curve. Spec-only: the editor
# shows the friendly 1-5 power dial instead.
DUTY_FIELD = VerbField(
    name="duty", type="int", default=0, hidden=True,
    description="Pump PWM 1-255 - lower = gentler, slower stroke. 0 = full "
                "speed (no duty sent). Needs no fill calibration.",
)

# Friendly 1-5 "power" dial the block editor shows instead of the raw ``duty``
# PWM. Mapped onto the calibrated duty range at runtime (see
# :func:`~src.hardware.fill_scaling.duty_for_power`): 1 = the per-skin-type
# minimum usable stroke, 5 = full power. Level 5 is the default - full speed,
# leaving any ``period_ms`` / calibrated slow-fill in charge - so a lower level
# is an explicit "run gentler". Overrides ``duty`` when both are present. The
# editor omits the key at 5 so specs stay clean and back-compatible.
POWER_FIELD = VerbField(
    name="power", type="power", default=5, choices=(5, 4, 3, 2, 1),
    labels=((5, "5 (full)"), (1, "1 (gentle)")), omit_default=True,
    description="Pump strength 1-5: 1 = gentlest usable stroke (the calibrated "
                "minimum for this skin type), 5 = full power. 5 leaves the "
                "'over ms' slow-fill in charge; below 5 runs the pump gentler.",
)

# LED ring selector, shared by the verbs that drive lights. The multiplexed
# board defines three independent rings (0..2; Tree populates all three, Turtle
# only ring 0) that can animate separately; the direct board has a single ring.
# "all" (the default) addresses every ring at once, matching the prior
# whole-ring behaviour; single-ring boards ignore an explicit ring beyond 0.
RING_FIELD = VerbField(
    name="ring", type="ring", default="all", choices=("all", 0, 1, 2),
    labels=(("all", "all rings"), (0, "ring 0"), (1, "ring 1"), (2, "ring 2")),
    omit_default=True,
    description="Which LED ring to drive: 'all' (every ring) or 0..2. Only the "
                "multiplexed board has rings 1..2 (Tree populates 3 rings, "
                "Turtle 1); the direct board has one ring and ignores higher "
                "indices.",
)

# Cross-fade time, shared by the LED verbs. Every LED change cross-fades; this sets
# how long. 0 snaps instantly; the firmware default (~250 ms) is the friendly value.
FADE_FIELD = VerbField(
    name="fade_ms", type="ms", default=250, min=0,
    description="Smooth-transition time for this change. Every colour/pattern "
                "change cross-fades; 0 snaps instantly.",
)

# LED patterns shared by the LED verbs. "comet" sweeps a bright head with a fading
# tail around the ring (one comet per colour, so a two-colour split gives two comets).
LED_PATTERNS = ("solid", "pulse", "blink", "comet", "off")
PATTERN_FIELD = VerbField("pattern", "enum", "solid", choices=LED_PATTERNS)
PERIOD_FIELD = VerbField(
    "period_ms", "ms", 0, min=0,
    description="Animation period (pulse/blink cycle or comet revolution); "
                "ignored for solid/off.")

# Angular rotation of the split/comet around the ring (degrees). Lets a "halves"
# split sit top/bottom instead of left/right, or start a comet elsewhere.
ANGLE_FIELD = VerbField(
    name="angle", type="int", default=0, min=0, max=360,
    description="Rotate the split/comet around the ring (0-360 deg). 0 = default "
                "orientation; e.g. 90 turns left/right halves into top/bottom.",
)

BEAT_MODES = ("sync", "sequential", "random", "aligned")

# What counts as one step of a `zone_fill`: any press, or only a press that
# keeps the zone's cadence (its interval to the previous press in the SAME
# zone is within tolerance of the target - the first press of a run never
# counts, it only starts the clock).
ZONE_FILL_KINDS = ("touch", "rhythmic")

# Live feedback while a sensor is held, shared by the fill verbs.
HOLD_FIELD = VerbField(
    "hold", "enum", "none", choices=HOLD_MODES,
    labels=(("none", "nothing"), ("glow", "glow the lit pixels"),
            ("dim", "dim the unlit pixels")),
    description="Live feedback while a sensor is held, scaled by how hard it "
                "is pressed: glow = the held zone's lit pixels brighten towards "
                "white; dim = its still-unlit pixels fade towards black; none "
                "= nothing extra.")
HOLD_FULL_FIELD = VerbField(
    "hold_full_ut", "float", 300, min=1,
    description="Field strength (uT) read as a full-strength press; the touch "
                "threshold counts as zero. Lower it to make the feedback react "
                "sooner.")

# Shared by the zone/sync fill verbs: how lit pixels are chosen.
FILL_FIELD = VerbField(
    "fill", "enum", "random", choices=FILL_MODES,
    labels=(("random", "scattered"), ("contiguous", "in order"),
            ("centre", "from the sensor")),
    description="random = scattered pixels; contiguous = in strip order; "
                "centre = outward from the touched sensor.")

ON_COLOR_FIELD = VerbField("on_color", "color", "#2ecc71",
                           description="Colour of a lit pixel.")
BG_COLOR_FIELD = VerbField("bg_color", "color", "#222222",
                           description="Colour of an unlit pixel.")

# "Jump to phase" slot shared by the fill verbs (empty = don't advance).
TO_FIELD = VerbField(
    "to", "phase", "", omit_default=True,
    description="Phase to jump to once full (empty = don't advance; let a "
                "transition move on).")

SYNC_MODE_LABELS = (("auto", "whoever touches first (others may join)"),
                    ("fixed", "exactly the zones listed"))

ROBOT_FIELD = VerbField(
    "robot", "enum", activity_kind.THYMIO, choices=activity_kind.KINDS,
    labels=tuple((k, activity_kind.label(k)) for k in activity_kind.KINDS),
    description="Robot kind.")


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

ACTIONS: tuple[Verb, ...] = (
    Verb("set_led", "action",
         "Light one LED ring (or all rings) one colour. 'comet' sweeps a single "
         "rotating light. Pick a 'ring' to animate the multiplexed board's rings "
         "independently.", (
        VerbField("color", "color", "#8e44ad"),
        PATTERN_FIELD,
        PERIOD_FIELD,
        FADE_FIELD,
        ANGLE_FIELD,
        RING_FIELD,
    ), template="set LED colour {color} pattern {pattern} period {period_ms}\n"
                "smooth {fade_ms} angle {angle} ring {ring}",
       category="Lights"),
    Verb("set_led_halves", "action",
         "Split a ring into equal arcs, one colour each (e.g. half purple, "
         "half yellow). 'comet' instead sweeps one rotating comet per colour. "
         "Pick a 'ring' to target one ring.", (
        VerbField("colors", "colors", ["#8e44ad", "#f1c40f"], count=2),
        PATTERN_FIELD,
        PERIOD_FIELD,
        FADE_FIELD,
        ANGLE_FIELD,
        RING_FIELD,
    ), template="set LED halves {colors} pattern {pattern} period {period_ms}\n"
                "smooth {fade_ms} angle {angle} ring {ring}",
       category="Lights",
       variants=(Variant(
           "quarters",
           "set LED quarters {colors}\npattern {pattern} period {period_ms}\n"
           "smooth {fade_ms} angle {angle} ring {ring}", 4),)),
    Verb("fade", "action",
         "Smoothly cross-fade a ring back and forth between two colours. Wrap "
         "in 'repeat forever' for a continuous fade. Pick a 'ring' to fade one "
         "ring independently.", (
        VerbField("color1", "color", "#8e44ad"),
        VerbField("color2", "color", "#f1c40f"),
        VerbField("period_ms", "ms", 2000, min=200,
                  description="One full colour1 -> colour2 -> colour1 cycle."),
        RING_FIELD,
    ), template="fade between {color1} and {color2} over {period_ms} on ring {ring}",
       category="Lights"),
    Verb("touch_progress", "action",
         "Fill an LED ring as the child touches - a touch-counter you can see. "
         "Light one more arc every 'per' touches, from 'bg_color' to 'on_color'. "
         "Put it in a phase's 'on touch' so it repaints on each press; once all "
         "'segments' arcs are lit it jumps to phase 'to' (leave 'to' empty to "
         "just fill and let a transition handle the move). Counts touches since "
         "the phase was entered, so it resets each phase.", (
        VerbField("segments", "int", 4, min=1, max=8,
                  description="Arcs to split the ring into (4 = quarters)."),
        VerbField("per", "int", 1, min=1,
                  description="Touches needed to light each further arc."),
        VerbField("on_color", "color", "#2ecc71",
                  description="Colour of a lit (filled) arc."),
        VerbField("bg_color", "color", "#222222",
                  description="Colour of an unlit (background) arc."),
        RING_FIELD,
        TO_FIELD,
    ), template="touch fill - {segments} arcs, {per} touch(es) each\n"
                "from {bg_color} to {on_color} on ring {ring}\n"
                "when full go to phase {to}",
       category="Lights"),
    Verb("zone_fill", "action",
         "Light part of the LED zone above the sensor that was just touched. "
         "Put it in a phase's 'on touch': each qualifying press lights "
         "'step_pct' more of THAT zone (the strip is split into one zone per "
         "touch sensor - quadrants on the Thymio skin). 'kind' = any touch, or "
         "only a rhythmic one (a press that keeps the zone's cadence). Once "
         "every zone is full it jumps to phase 'to' (empty = don't advance). "
         "Needs a skin whose LED strip and sensor positions are configured.", (
        VerbField("kind", "enum", "touch", choices=ZONE_FILL_KINDS,
                  labels=(("touch", "touch"), ("rhythmic", "rhythmic touch")),
                  description="touch = every press counts; rhythmic = only a "
                              "press within tolerance of the target interval "
                              "since the previous press in the same zone."),
        VerbField("step_pct", "pct", 25, min=1,
                  description="Share of the zone lit per qualifying press "
                              "(25 = a quarter of the zone each time)."),
        FILL_FIELD,
        ON_COLOR_FIELD,
        BG_COLOR_FIELD,
        VerbField("target_interval_ms", "ms", 550, min=1,
                  show_when=("kind", ("rhythmic",)),
                  description="Rhythmic kind: expected time between presses."),
        VerbField("tolerance_ms", "ms", 150, min=0,
                  show_when=("kind", ("rhythmic",)),
                  description="Rhythmic kind: allowed drift around the target."),
        VerbField("min_gap_ms", "ms", 250, min=0,
                  show_when=("kind", ("rhythmic",)),
                  description="Rhythmic kind: ignore faster repeats as chatter."),
        HOLD_FIELD,
        HOLD_FULL_FIELD,
        FADE_FIELD,
        RING_FIELD,
        TO_FIELD,
    ), template="zone fill - each {kind} lights {step_pct} % of the touched zone, {fill}\n"
                "rhythm: every {target_interval_ms} +/- {tolerance_ms}, "
                "ignore repeats under {min_gap_ms}\n"
                "from {bg_color} to {on_color} on ring {ring}, smooth {fade_ms}\n"
                "while held: {hold} by press strength, full at {hold_full_ut} uT\n"
                "when every zone is full go to phase {to}",
       category="Lights"),
    Verb("sync_fill", "action",
         "Show the group's synchronized-round progress on the WHOLE strip: "
         "the lit share of all pixels equals completed rounds / rounds "
         "required of this phase's 'group sync' condition. Put it in the "
         "phase's 'do' (it keeps updating every tick). When the streak breaks "
         "the pixels go out one every 'decay_ms' instead of all at once.", (
        FILL_FIELD,
        ON_COLOR_FIELD,
        BG_COLOR_FIELD,
        VerbField("decay_ms", "ms", 150, min=0,
                  description="After a broken streak, turn one pixel off "
                              "every this long (0 = all at once)."),
        HOLD_FIELD,
        HOLD_FULL_FIELD,
        FADE_FIELD,
        RING_FIELD,
    ), template="sync fill - light the whole strip as the group's rounds complete, {fill}\n"
                "from {bg_color} to {on_color} on ring {ring}, smooth {fade_ms}, "
                "decay {decay_ms}\n"
                "while held: {hold} by press strength, full at {hold_full_ut} uT",
       category="Lights"),
    Verb("score_fill", "action",
         "Score the group's synchronized rounds on the WHOLE strip. Put it in "
         "the phase's 'do'. Every round where all the children press together "
         "(within the phase window) and on the cadence lights 'gain_pct' more "
         "of the strip; every failed round (a child missing, or one child "
         "pressing alone) turns 'penalty_pct' off again. A round together but "
         "off cadence changes nothing. So a few slips hardly show, while lots "
         "of random pressing drains the strip back to 'bg_color'. Once the "
         "strip is full it jumps to phase 'to'. 'mode' auto = whoever presses "
         "forms the group once 'participants' have joined; fixed = sensors "
         "0..N-1.", (
        VerbField("mode", "enum", MODE_AUTO, choices=SYNC_MODES,
                  labels=(("auto", "auto (whoever presses)"),
                          ("fixed", "fixed (sensors 0..N-1)")),
                  description="auto = whoever presses (first N start, others "
                              "join); fixed = sensors 0..N-1."),
        VerbField("participants", "int", 3, min=1, max=4,
                  description="Number of children (auto: the minimum needed "
                              "before rounds score)."),
        VerbField("target_interval_ms", "ms", 550, min=1,
                  description="Expected time between group rounds."),
        VerbField("cadence_tolerance_ms", "ms", 100, min=0,
                  description="Allowed timing error of one round; a round "
                              "outside it is neutral, not a miss."),
        VerbField("phase_tolerance_ms", "ms", 150, min=0,
                  description="Maximum spread between children within one "
                              "round; a press later than this starts a new "
                              "round."),
        VerbField("min_gap_ms", "ms", 250, min=0,
                  description="Per-sensor debounce; faster onsets are ignored."),
        VerbField("gain_pct", "pct", 15, min=1,
                  description="Share of the strip lit per good round."),
        VerbField("penalty_pct", "pct", 5, min=0,
                  description="Share of the strip turned off per failed round."),
        FILL_FIELD,
        VerbField("on_color", "color", "#f1c40f",
                  description="Colour of a lit pixel."),
        VerbField("bg_color", "color", "#8e44ad",
                  description="Colour of an unlit pixel."),
        HOLD_FIELD,
        HOLD_FULL_FIELD,
        FADE_FIELD,
        RING_FIELD,
        TO_FIELD,
    ), template="score fill - {participants} children, {mode} mode, {fill}\n"
                "round every {target_interval_ms} +/- {cadence_tolerance_ms}; "
                "together within {phase_tolerance_ms}; "
                "ignore repeats under {min_gap_ms}\n"
                "each good round lights {gain_pct} % of the strip, "
                "each miss turns {penalty_pct} % off\n"
                "from {bg_color} to {on_color} on ring {ring}, smooth {fade_ms}\n"
                "while held: {hold} by press strength, full at {hold_full_ut} uT\n"
                "when the strip is full go to phase {to}",
       category="Lights"),
    Verb("inflate", "action", "Drive a chamber up to a pressure %.", (
        CHAMBER_FIELD,
        VerbField("pct", "pct", 60, description="Target pressure (0-100 %)."),
        VerbField("period_ms", "ms", 0, min=0,
                  description="Fill gently over about this long; the pump "
                              "slows to roughly match. 0 = full speed. Needs a "
                              "calibrated fill time, else falls back to full speed."),
        POWER_FIELD,
        DUTY_FIELD,
    ), template="inflate chamber {chamber} to {pct} % over {period_ms} power {power}",
       category="Chambers"),
    Verb("deflate", "action", "Empty a chamber back to 0 %.", (
        CHAMBER_FIELD,
    ), template="deflate chamber {chamber}", category="Chambers"),
    Verb("set_pressure", "action",
         "Set a chamber's absolute target %. 0 % empties (wrinkles) it.", (
        CHAMBER_FIELD,
        VerbField("pct", "pct", 0),
        POWER_FIELD,
        DUTY_FIELD,
    ), template="set chamber {chamber} to {pct} % power {power}",
       category="Chambers"),
    Verb("beat", "action",
         "One heartbeat cycle across the unit's chambers. Wrap in "
         "'repeat forever' for a continuous beat. 'strength' is the peak fill %; "
         "only 'some strong, the rest soft' uses the soft-chamber row. 'power' "
         "is the pump strength for the up-strokes; the release stays full speed.", (
        VerbField("mode", "enum", "sync", choices=BEAT_MODES,
                  labels=(("sync", "all chambers together"),
                          ("sequential", "one chamber at a time"),
                          ("random", "chambers in random order"),
                          ("aligned", "some strong, the rest soft")),
                  description="sync=all together; sequential=one at a time; "
                              "random=shuffled one at a time; aligned=N chambers "
                              "at 'pct', the rest at 'pct2'."),
        VerbField("pct", "pct", 60, description="Peak pressure of the beat."),
        VerbField("pct2", "pct", 20, show_when=("mode", ("aligned",)),
                  description="Pressure of the misaligned chambers (aligned mode)."),
        VerbField("aligned", "int", 2, min=0, show_when=("mode", ("aligned",)),
                  description="How many chambers share 'pct' in aligned mode."),
        VerbField("period_ms", "ms", 2000, min=100, description="One full cycle."),
        POWER_FIELD,
        DUTY_FIELD,
    ), template="beat - {mode}\nstrength {pct} %\n"
                "soft chambers at {pct2} % , how many strong {aligned}\n"
                "one cycle every {period_ms} , power {power}",
       category="Chambers"),
    Verb("stop", "action", "Hold every chamber where it is.", (),
         template="hold all chambers", category="Chambers"),
    Verb("log", "action", "Write a line to the activity log (debugging).", (
        VerbField("message", "text", ""),
    ), template="log {message}", category="Control"),
    # --- Thymio wheeled base (no-ops on robots without wheels) ---
    Verb("thymio_drive", "action",
         "Set the Thymio's wheel speeds (-500..500; negative = backwards, "
         "opposite signs turn on the spot) - optionally for a fixed time, then "
         "stop. 0/0 stops the wheels.", (
        VerbField("left", "int", 100, min=-500, max=500,
                  description="Left wheel target, -500..500 (negative = "
                              "backwards)."),
        VerbField("right", "int", 100, min=-500, max=500,
                  description="Right wheel target, -500..500. Opposite signs "
                              "turn on the spot."),
        VerbField("ms", "ms", 0, min=0,
                  description="Drive this long then stop. 0 = keep driving "
                              "until the next thymio_drive."),
    ), template="drive Thymio - left wheel {left} right wheel {right} for {ms}",
       category="Thymio", kinds=(activity_kind.THYMIO,)),
    Verb("thymio_leds", "action",
         "Colour the Thymio's own LEDs.", (
        VerbField("color", "color", "#8e44ad"),
    ), template="Thymio LED colour {color}", category="Thymio",
       kinds=(activity_kind.THYMIO,)),
    Verb("thymio_sound", "action",
         "Play a sound on the Thymio: a built-in system sound (0-7, -1 stops), a "
         "tone (frequency + duration), or a track recorded on the Thymio's microSD "
         "(needs a card; wins when >= 0). The wheel targets are untouched, so a "
         "driving robot beeps without stopping.", (
        VerbField("sys", "int", 2, min=-1, max=7,
                  description="Built-in system sound 0-7 (-1 stops). Used when "
                              "'freq' and 'track' are 0/negative."),
        VerbField("freq", "int", 0, min=0,
                  description="Tone frequency in Hz; 0 = play the system sound "
                              "instead of a tone."),
        VerbField("dur", "ms", 200, min=0,
                  description="Tone duration (only used for a 'freq' tone)."),
        VerbField("track", "int", -1, min=-1,
                  description="Play recorded track N from the microSD (needs a card); "
                              "-1 = don't (use system/tone). Takes priority when >=0."),
    ), template="Thymio sound - system {sys} or tone {freq} Hz for {dur} "
                "or SD track {track}",
       category="Thymio", kinds=(activity_kind.THYMIO,)),
)

CONTROL: tuple[Verb, ...] = (
    Verb("wait", "control", "Pause the sequence for a fixed time.", (
        VerbField("ms", "ms", 500, min=0),
    ), template="wait {ms}", category="Control"),
    Verb("wait_for_touch", "control",
         "Pause the sequence until the child touches (optionally a specific "
         "chamber). Lets a sequence advance on interaction instead of time.", (
        VerbField("chamber", "chamber", "current", choices=CHAMBER_CHOICES,
                  labels=(("current", "any"),),
                  description="Chamber whose touch resumes the sequence; "
                              "'any' = whichever is touched first."),
    ), template="wait for touch on chamber {chamber}", category="Control"),
    Verb("sequence", "control", "Run the nested steps in order.", (
        VerbField("do", "steps", []),
    ), template="sequence\ndo {do}", category="Control", in_toolbox=False),
    Verb("repeat", "control",
         "Repeat the nested steps forever, or a set number of times (N).", (
        VerbField("forever", "enum", True, choices=(True, False),
                  labels=((True, "forever"), (False, "N times"))),
        VerbField("times", "int", 4, min=1, show_when=("forever", (False,))),
        VerbField("do", "steps", []),
    ), template="repeat {forever}\nN = {times}\ndo {do}", category="Control"),
    Verb("for_each_chamber", "control",
         "Run the nested steps once per chamber, binding 'current' to each.", (
        VerbField("do", "steps", []),
    ), template="for each chamber\ndo {do}", category="Control"),
    Verb("if_robot", "control",
         "Run 'do' when the unit's robot is the chosen kind, else 'else'. "
         "Lets one behaviour cover every robot - e.g. only the Thymio drives "
         "while the Turtle and Tree skip that part. Chain another 'if robot "
         "is' inside 'else' for 3 ways.", (
        ROBOT_FIELD,
        VerbField("do", "steps", []),
        VerbField("else", "steps", []),
    ), template="if robot is {robot}\ndo {do}\nelse {else}", category="Control"),
)

CONDITIONS: tuple[Verb, ...] = (
    Verb("elapsed_ms", "condition",
         "True once this long passed since the phase was entered.", (
        VerbField("ms", "ms", 120000, min=0),
    ), template="after {ms}", category="Conditions"),
    Verb("touch_count", "condition",
         "True once the unit was touched at least 'min' times in this phase.", (
        VerbField("min", "int", 10, min=1),
    ), template="touched at least {min} times", category="Conditions"),
    Verb("gesture_count", "condition",
         "True once the child made a gesture at least 'min' times in this phase. "
         "'kind' is either a raw touch (any press - no model needed) or an "
         "ML-classified gesture (tap / press / compressions). Classified kinds "
         "need a trained touch model for this skin type; without one they never "
         "fire, so 'touch' is the safe default.", (
        VerbField("kind", "enum", "touch", choices=("touch", *GESTURE_CLASSES),
                  labels=(("touch", "a touch"), ("tap", "a tap"),
                          ("press", "a press"), ("compressions", "compressions")),
                  description="'touch' = any press; the rest are ML gestures "
                              "needing a trained model for this skin type."),
        VerbField("min", "int", 3, min=1),
    ), template="after {kind} {min} times", category="Conditions"),
    Verb("touch_rhythm", "condition",
         "True after consecutive touches arrive at roughly the same interval. "
         "The first touch starts the measurement; each later touch contributes "
         "one interval, and an out-of-range interval resets the streak. "
         "Multiple sensors in one press count only once.", (
        VerbField("target_interval_ms", "ms", 550, min=1,
                  description="Expected time between touch presses."),
        VerbField("tolerance_ms", "ms", 150, min=0,
                  description="Allowed drift around the target interval."),
        VerbField("min_gap_ms", "ms", 250, min=0,
                  description="Ignore faster changes as duplicate sensor edges."),
        VerbField("intervals", "int", 5, min=1,
                  description="Consecutive matching intervals required."),
    ), template="same touch rhythm: every {target_interval_ms} +/- {tolerance_ms}, "
                "{intervals} intervals\nignore gaps shorter than {min_gap_ms}",
       category="Conditions"),
    Verb("group_touch_sync", "condition",
         "True after consecutive group press rounds: every selected child "
         "must press within one phase window on every beat, and the group beats "
         "must keep the chosen cadence; a late child or wrong beat resets the "
         "streak. 'mode' auto lets the children pick themselves: the first "
         "'participants' sensors to press form the group and any further sensor "
         "that presses joins for good (so a fourth child may come in, and from "
         "then on all four must keep every round). 'mode' fixed uses the listed "
         "sensor zones only.", (
        VerbField("mode", "enum", MODE_AUTO, choices=SYNC_MODES,
                  labels=SYNC_MODE_LABELS,
                  description="auto = whoever presses (first N start, others "
                              "join); fixed = the listed sensors only."),
        VerbField("participants", "int", 3, min=1, max=4,
                  description="Number of independent child/sensor positions "
                              "(auto: the minimum needed to start counting)."),
        VerbField("sensors", "ints", [0, 1, 2, 3], count=4, min=0,
                  show_when=("mode", ("fixed",)),
                  description="Fixed mode: the physical sensor zones that form "
                              "the group (the first 'participants' count)."),
        VerbField("target_interval_ms", "ms", 550, min=1,
                  description="Expected time between completed group beats."),
        VerbField("cadence_tolerance_ms", "ms", 100, min=0,
                  description="Allowed timing error of one group beat."),
        VerbField("phase_tolerance_ms", "ms", 150, min=0,
                  description="Maximum spread between children within one beat."),
        VerbField("min_gap_ms", "ms", 250, min=0,
                  description="Per-sensor debounce; faster onsets are ignored."),
        VerbField("rounds", "int", 6, min=1,
                  description="Consecutive synchronized group beats required."),
    ), template="{participants} children synchronize, {mode}\n"
                "fixed zones {sensors}\n"
                "every {target_interval_ms} +/- {cadence_tolerance_ms}; "
                "together within {phase_tolerance_ms}; {rounds} rounds\n"
                "ignore repeats faster than {min_gap_ms}",
       category="Conditions"),
    Verb("on_impact", "condition",
         "True once the Thymio was knocked ('impact': a sharp accelerometer "
         "deviation from rest) at least 'min' times in this phase, at intensity "
         "'level' or above (touch < knock < slap). Needs the gateway/C6 wireless "
         "link; robots without impact sensing never fire it.", (
        VerbField("min", "int", 1, min=1),
        VerbField("level", "enum", 1, choices=(1, 2, 3),
                  labels=((1, "a touch"), (2, "a knock"), (3, "a slap")),
                  description="Minimum intensity to count: 1 = touch, 2 = knock, "
                              "3 = slap."),
    ), template="Thymio hit at least {min} times, at least {level}",
       category="Conditions"),
    Verb("on_lifted", "condition",
         "True once the Thymio was lifted off the surface at least 'min' times in "
         "this phase (the ground sensors stop seeing the table). Needs the gateway/"
         "C6 wireless link; robots without ground sensing never fire it.", (
        VerbField("min", "int", 1, min=1),
    ), template="Thymio lifted at least {min} times", category="Conditions"),
    Verb("any", "condition", "True if any sub-condition is true (OR).", (
        VerbField("conds", "conds", []),
    ), template="any of: {conds}", category="Conditions"),
    Verb("all", "condition", "True if every sub-condition is true (AND).", (
        VerbField("conds", "conds", []),
    ), template="all of: {conds}", category="Conditions"),
    Verb("not", "condition", "True if the sub-condition is false.", (
        VerbField("cond", "cond", None),
    ), template="not {cond}", category="Conditions"),
    Verb("organs", "condition",
         "Organ-status condition, evaluated against the unit's plugged organs "
         "(resolved good/bad/absent from the skin's organ circuit). 'scope' "
         "all_good / all_bad short-circuit to 'every organ is good / bad'; "
         "'count' compares the good and bad counts via their operators.", (
        VerbField("scope", "enum", "count",
                  choices=("count", "all_good", "all_bad"),
                  labels=(("count", "by count"), ("all_good", "all good"),
                          ("all_bad", "all bad")),
                  description="count=use the good/bad comparisons; "
                              "all_good/all_bad ignore them."),
        VerbField("good_op", "enum", ">=", choices=(">=", "<=", "=="),
                  labels=(("==", "="),), show_when=("scope", ("count",)),
                  description="How to compare the good-organ count."),
        VerbField("good", "int", 1, min=0, show_when=("scope", ("count",)),
                  description="Good-organ count threshold."),
        VerbField("bad_op", "enum", "<=", choices=(">=", "<=", "=="),
                  labels=(("==", "="),), show_when=("scope", ("count",)),
                  description="How to compare the bad-organ count."),
        VerbField("bad", "int", 0, min=0, show_when=("scope", ("count",)),
                  description="Bad-organ count threshold."),
    ), template="organs {scope}\ngood {good_op} {good} bad {bad_op} {bad}",
       category="Conditions"),
    Verb("robot_is", "condition",
         "True when the unit's robot is the chosen kind - lets a transition "
         "fire only on one robot.", (
        ROBOT_FIELD,
    ), template="robot is {robot}", category="Conditions"),
    Verb("always", "condition", "Always true (unconditional transition).", (),
         template="always", category="Conditions"),
)

# Aliases accepted in specs for readability (any<->or, all<->and).
COND_ALIASES = {"or": "any", "and": "all"}

# Verbs no longer offered as blocks but still accepted in saved specs so older
# behaviours keep loading. ``wrinkle`` was dropped because it is identical to
# setting a chamber to 0 % (the runtime maps it to that); author with
# ``set_pressure`` / ``deflate`` instead.
DEPRECATED_STEP_NAMES = {"wrinkle"}

# Verbs that suspend the running sequence (they take time). A `repeat forever`
# whose body has none of them re-runs its instantaneous steps every tick,
# which floods the radio - `lint_spec` warns about it.
SUSPENDING_VERBS = {"wait", "wait_for_touch", "beat", "fade", "thymio_drive"}

_ALL_VERBS = {v.name: v for v in (*ACTIONS, *CONTROL, *CONDITIONS)}
ACTION_NAMES = {v.name for v in ACTIONS}
CONTROL_NAMES = {v.name for v in CONTROL}
CONDITION_NAMES = {v.name for v in CONDITIONS} | set(COND_ALIASES)
STEP_NAMES = ACTION_NAMES | CONTROL_NAMES | DEPRECATED_STEP_NAMES

# Legacy phase names the runtime treated as terminal before the explicit
# ``final`` flag existed; still honoured so old behaviours keep working.
LEGACY_FINAL_STATES = {"complete", "success", "done"}


def verb(name: str) -> Verb | None:
    return _ALL_VERBS.get(name)


def all_verbs() -> tuple[Verb, ...]:
    return (*ACTIONS, *CONTROL, *CONDITIONS)


def is_final_state(spec: dict[str, Any], state: str) -> bool:
    """Whether ``state`` is a terminal phase: flagged ``final`` in the spec,
    or one of the legacy names."""
    st = (spec.get("states") or {}).get(state)
    if isinstance(st, dict) and st.get("final"):
        return True
    return state.lower() in LEGACY_FINAL_STATES


# ---------------------------------------------------------------------------
# Editor export
# ---------------------------------------------------------------------------

def _field_json(f: VerbField) -> dict[str, Any]:
    out: dict[str, Any] = {
        "name": f.name, "type": f.type, "default": f.default,
        "description": f.description, "hidden": f.hidden,
        "omit_default": f.omit_default, "count": f.count,
    }
    if f.choices:
        out["choices"] = [{"label": f.label_for(c), "value": c}
                          for c in f.choices]
    if f.min is not None:
        out["min"] = f.min
    if f.max is not None:
        out["max"] = f.max
    if f.show_when is not None:
        out["show_when"] = {"field": f.show_when[0],
                            "values": list(f.show_when[1])}
    return out


def editor_catalog() -> dict[str, Any]:
    """The catalogue as the block editor consumes it (JSON-serialisable).

    ``verbs`` is in toolbox order; the page turns each into a block (plus one
    per variant), builds the toolbox from ``categories``, and compiles /
    decompiles specs from the field list alone."""
    return {
        "categories": [{"name": n, "colour": c} for n, c in CATEGORIES],
        "robots": [{"label": activity_kind.label(k), "value": k}
                   for k in activity_kind.KINDS],
        "skins": [{"label": skin_condition.label(s), "value": s}
                  for s in skin_condition.CONDITIONS],
        "verbs": [{
            "name": v.name, "kind": v.kind, "category": v.category,
            "description": v.description, "template": v.template,
            "in_toolbox": v.in_toolbox,
            "fields": [_field_json(f) for f in v.fields],
            "variants": [{"id": x.id, "template": x.template, "count": x.count}
                         for x in v.variants],
        } for v in all_verbs()],
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class SpecError(ValueError):
    """Raised when a behaviour spec is malformed."""


def validate_spec(spec: dict[str, Any]) -> None:
    """Raise :class:`SpecError` if ``spec`` is not a runnable behaviour.

    Cheap structural checks only (verb names, state references) - the
    interpreter tolerates missing optional params via defaults. Use
    :func:`lint_spec` for the advisory checks.
    """
    if not isinstance(spec, dict):
        raise SpecError("spec must be a dict")
    states = spec.get("states")
    if not isinstance(states, dict) or not states:
        raise SpecError("spec.states must be a non-empty dict")
    initial = spec.get("initial")
    if initial not in states:
        raise SpecError(f"spec.initial {initial!r} is not a defined state")

    # Optional activity target: {"kind": <robot kind>} narrows the behaviour
    # to one robot topology, {"skin": <condition>} says which silicone set it
    # is written for; either or both. Absent = "any" behaviour.
    target = spec.get("target")
    if target is not None:
        _validate_target(target)
    kind = target.get("kind") if isinstance(target, dict) else None

    for sid, state in states.items():
        if not isinstance(state, dict):
            raise SpecError(f"state {sid!r} must be a dict")
        if "final" in state and not isinstance(state["final"], bool):
            raise SpecError(f"state {sid!r}: 'final' must be true/false")
        _validate_steps(state.get("do", []), f"{sid}.do", kind)
        _validate_steps(state.get("on_touch", []), f"{sid}.on_touch", kind)
        for i, tr in enumerate(state.get("transitions", []) or []):
            if not isinstance(tr, dict) or "to" not in tr:
                raise SpecError(f"{sid}.transitions[{i}] needs a 'to'")
            if tr["to"] not in states:
                raise SpecError(
                    f"{sid}.transitions[{i}] -> unknown state {tr['to']!r}")
            _validate_cond(tr.get("when", {"always": True}),
                           f"{sid}.transitions[{i}].when")


def _validate_target(target: Any) -> None:
    """Check an optional ``spec.target``: ``{"kind": <robot kind>}`` and/or
    ``{"skin": <condition>}`` (at least one must be present)."""
    if not isinstance(target, dict):
        raise SpecError("spec.target must be a dict")
    skin = target.get("skin")
    kind = target.get("kind")
    if skin is None and kind is None:
        raise SpecError("spec.target needs a 'kind' and/or a 'skin'")
    if skin is not None and not skin_condition.is_condition(skin):
        raise SpecError(
            f"spec.target.skin {skin!r} is not a known skin condition")
    if kind is not None and not activity_kind.is_kind(kind):
        raise SpecError(f"spec.target.kind {kind!r} is not a known activity kind")


def spec_target(spec: dict[str, Any]) -> dict[str, str] | None:
    """The activity's declared target, or ``None`` (target-less 'any'
    behaviour): ``{"kind": <robot kind>}`` and/or ``{"skin": <condition>}``.
    Assumes ``spec`` passed :func:`validate_spec`."""
    t = spec.get("target")
    if not isinstance(t, dict):
        return None
    out: dict[str, str] = {}
    if activity_kind.is_kind(t.get("kind")):
        out["kind"] = t["kind"]
    if skin_condition.is_condition(t.get("skin")):
        out["skin"] = t["skin"]
    return out or None


def spec_skin(spec: dict[str, Any]) -> str | None:
    """The activity's declared skin condition, or ``None``."""
    return (spec_target(spec) or {}).get("skin")


def spec_kind(spec: dict[str, Any]) -> str | None:
    """The activity's declared robot kind, or ``None`` (runs on any robot)."""
    return (spec_target(spec) or {}).get("kind")


def _validate_steps(steps: Any, where: str, kind: str | None = None) -> None:
    if not isinstance(steps, list):
        raise SpecError(f"{where} must be a list of steps")
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or len(step) != 1:
            raise SpecError(f"{where}[{i}] must be a single-key {{verb: ...}} dict")
        name = next(iter(step))
        if name not in STEP_NAMES:
            raise SpecError(f"{where}[{i}] unknown verb {name!r}")
        _check_verb_kind(_ALL_VERBS.get(name), name, kind, f"{where}[{i}]")
        params = step[name]
        if name in CONTROL_NAMES and isinstance(params, dict):
            _validate_steps(params.get("do", []), f"{where}[{i}].do", kind)
            _validate_steps(params.get("else", []),
                            f"{where}[{i}].else", kind)


def _check_verb_kind(v: Verb | None, name: str, kind: str | None,
                     where: str) -> None:
    """Kind gating for kind-targeted specs only; skin-targeted and
    target-less specs may use robot-specific verbs anywhere (they no-op on
    other robots - wrap in `if_robot` to be explicit)."""
    if v is not None and v.kinds and kind is not None and kind not in v.kinds:
        raise SpecError(
            f"{where} verb {name!r} needs a spec.target of kind "
            f"{' / '.join(v.kinds)} (got {kind!r})")


def _validate_cond(cond: Any, where: str) -> None:
    if not isinstance(cond, dict) or len(cond) != 1:
        raise SpecError(f"{where} must be a single-key condition dict")
    name = next(iter(cond))
    canon = COND_ALIASES.get(name, name)
    if canon not in {v.name for v in CONDITIONS}:
        raise SpecError(f"{where} unknown condition {name!r}")
    val = cond[name]
    if canon in ("any", "all"):
        if not isinstance(val, list):
            raise SpecError(f"{where}.{name} expects a list of conditions")
        for j, sub in enumerate(val):
            _validate_cond(sub, f"{where}.{name}[{j}]")
    elif canon == "not":
        _validate_cond(val, f"{where}.not")


# ---------------------------------------------------------------------------
# Lint - advisory checks the editor shows before saving
# ---------------------------------------------------------------------------

def _walk_steps(steps: Any):
    """Yield every (verb, params) in a step list, depth first."""
    for step in steps or []:
        if not isinstance(step, dict) or len(step) != 1:
            continue
        name = next(iter(step))
        params = step[name]
        yield name, params
        if isinstance(params, dict):
            yield from _walk_steps(params.get("do"))
            yield from _walk_steps(params.get("else"))


def _step_jumps(state: dict[str, Any]) -> set[str]:
    """Phases a state's steps may jump to via a fill verb's ``to``."""
    out: set[str] = set()
    for key in ("do", "on_touch"):
        for _name, params in _walk_steps(state.get(key)):
            if isinstance(params, dict) and params.get("to"):
                out.add(str(params["to"]))
    return out


def _suspends(steps: Any) -> bool:
    """Whether a step list contains something that takes time."""
    for name, params in _walk_steps(steps):
        if name in SUSPENDING_VERBS:
            if name == "thymio_drive" and isinstance(params, dict) \
                    and not int(params.get("ms", 0) or 0):
                continue
            return True
    return False


def lint_spec(spec: dict[str, Any]) -> list[str]:
    """Advisory warnings for a spec that already passed :func:`validate_spec`:
    things that run but almost certainly are not what the author meant."""
    warnings: list[str] = []
    states: dict[str, Any] = spec.get("states") or {}
    initial = spec.get("initial")

    # Reachability from the initial phase via transitions and fill jumps.
    reach: set[str] = set()
    todo = [initial] if initial in states else []
    while todo:
        s = todo.pop()
        if s in reach:
            continue
        reach.add(s)
        st = states.get(s) or {}
        for tr in st.get("transitions", []) or []:
            if isinstance(tr, dict) and tr.get("to") in states:
                todo.append(tr["to"])
        todo.extend(j for j in _step_jumps(st) if j in states)
    for name in states:
        if name not in reach:
            warnings.append(
                f"Phase '{name}' can never be reached (no transition or fill "
                "jump leads to it).")

    for name, st in states.items():
        if not isinstance(st, dict):
            continue
        for j in _step_jumps(st):
            if j not in states:
                warnings.append(
                    f"Phase '{name}': a fill block jumps to '{j}', which "
                    "does not exist - the jump will be ignored.")
        transitions = [t for t in (st.get("transitions") or [])
                       if isinstance(t, dict)]
        if st.get("final") and transitions:
            warnings.append(
                f"Phase '{name}' is marked final but has transitions - "
                "they still fire; untick 'final' or remove them.")
        for i, tr in enumerate(transitions):
            when = tr.get("when", {"always": True})
            is_always = isinstance(when, dict) and when.get("always") is True
            if is_always and tr.get("to") == name:
                warnings.append(
                    f"Phase '{name}': an 'always' transition to itself "
                    "restarts the phase every tick.")
            if is_always and i < len(transitions) - 1:
                warnings.append(
                    f"Phase '{name}': the 'always' transition to "
                    f"'{tr.get('to')}' fires immediately, so the transitions "
                    "after it never get a chance.")
                break
        for key in ("do", "on_touch"):
            for vname, params in _walk_steps(st.get(key)):
                if vname != "repeat" or not isinstance(params, dict):
                    continue
                forever = bool(params.get("forever")) or \
                    params.get("times") in ("forever", None)
                if forever and not _suspends(params.get("do")):
                    warnings.append(
                        f"Phase '{name}': a 'repeat forever' has nothing that "
                        "takes time inside (no wait / beat / fade), so it "
                        "re-sends its steps every tick. Add a 'wait'.")
    return warnings
