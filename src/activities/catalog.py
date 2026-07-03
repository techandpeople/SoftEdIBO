"""Action & condition catalogue for the declarative behaviour engine.

This module is the **single source of truth** for what a behaviour spec may
contain. It serves two consumers:

- :class:`~src.activities.scripted_activity.ScriptedActivity` — the runtime
  interpreter validates specs against these schemas and dispatches each verb.
- The future Blockly block editor (Fase 2) — generates one block per entry
  here, so adding a verb makes a block appear automatically.

A behaviour **spec** is plain data (JSON-serialisable), authored by hand today
and by dragging blocks later. Shape::

    {
      "initial": "phase1",
      "states": {
        "phase1": {
          "do":          [ <step>, ... ],   # body, runs once on enter
          "on_touch":    [ <step>, ... ],   # optional, spawned per touch press
          "transitions": [ {"to": "phase2", "when": <condition>}, ... ]
        },
        ...
      }
    }

Each **step** is a single-key dict ``{verb: params}`` where ``params`` is a
dict (or a scalar shorthand, e.g. ``{"wait": 500}``). Control-flow verbs
(``sequence`` / ``repeat`` / ``for_each_chamber``) nest more steps under
``do``. Instantaneous verbs apply immediately; ``wait`` / ``wait_for_touch``
suspend the running sequence until satisfied — that is what lets an author
write "inflate 1, wait, deflate 1, inflate 2 …" as a literal sequence.

A **condition** is a single-key dict too: ``{"elapsed_ms": 120000}``,
``{"touch_count": {"min": 10}}``, or a combinator ``{"any": [..]}`` /
``{"all": [..]}`` / ``{"not": <cond>}``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.activities import activity_kind


@dataclass(frozen=True)
class VerbField:
    """One parameter of an action/condition verb (drives block inputs)."""
    name: str
    type: str                       # int | float | pct | color | colors | enum | chamber | steps | ms | cond | conds
    default: Any = None
    choices: tuple[Any, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class Verb:
    name: str
    kind: str                       # "action" | "control" | "condition"
    description: str
    fields: tuple[VerbField, ...] = field(default_factory=tuple)
    # Activity kinds (activity_kind.KINDS) the verb is restricted to. Empty =
    # valid everywhere. E.g. the Thymio wheel verbs only make sense in a
    # thymio-targeted spec, and the editor only offers them there.
    kinds: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Chamber addressing — every chamber action accepts one of these.
# ---------------------------------------------------------------------------
#   int      → that local chamber index
#   "all"    → every chamber of the unit
#   "current"/None → the chamber bound by the enclosing for_each_chamber
CHAMBER_FIELD = VerbField(
    name="chamber", type="chamber", default="current",
    description="Chamber index, 'all', or 'current' (the for_each_chamber "
                "binding). Defaults to the current chamber.",
)

# Direct pump-PWM control, shared by the verbs that drive a chamber up. A lower
# duty makes a gentler / lower-energy stroke; 0 (or None) means "full speed".
# Unlike period_ms it needs no calibrated fill curve.
DUTY_FIELD = VerbField(
    name="duty", type="int", default=0,
    description="Pump PWM 1-255 — lower = gentler, slower stroke. 0 = full "
                "speed (no duty sent). Needs no fill calibration.",
)

# LED ring selector, shared by the verbs that drive lights. The multiplexed
# board has four independent rings (0..3) that can animate separately; the
# direct board has a single ring. "all" (the default) addresses every ring at
# once, matching the prior whole-ring behaviour; single-ring boards ignore an
# explicit 0..3 beyond ring 0.
RING_FIELD = VerbField(
    name="ring", type="ring", default="all",
    description="Which LED ring to drive: 'all' (every ring) or 0..3. Only the "
                "multiplexed board has rings 1..3 (4 independent rings); the "
                "direct board has one ring and ignores higher indices.",
)

# Cross-fade time, shared by the LED verbs. Every LED change cross-fades; this sets
# how long. 0 snaps instantly; the firmware default (~250 ms) is the friendly value.
FADE_FIELD = VerbField(
    name="fade_ms", type="ms", default=250,
    description="Smooth-transition time (ms) for this change. Every colour/pattern "
                "change cross-fades; 0 snaps instantly.",
)

# LED patterns shared by the LED verbs. "comet" sweeps a bright head with a fading
# tail around the ring (one comet per colour, so a two-colour split gives two comets).
LED_PATTERNS = ("solid", "pulse", "blink", "comet", "off")

# Angular rotation of the split/comet around the ring (degrees). Lets a "halves"
# split sit top/bottom instead of left/right, or start a comet elsewhere.
ANGLE_FIELD = VerbField(
    name="angle", type="int", default=0,
    description="Rotate the split/comet around the ring (0-360°). 0 = default "
                "orientation; e.g. 90 turns left/right halves into top/bottom.",
)

BEAT_MODES = ("sync", "sequential", "random", "aligned")


# ---------------------------------------------------------------------------
# Catalogue
# ---------------------------------------------------------------------------

ACTIONS: tuple[Verb, ...] = (
    Verb("set_led", "action",
         "Light one LED ring (or all rings) one colour. 'comet' sweeps a single "
         "rotating light. Pick a 'ring' to animate the multiplexed board's four "
         "rings independently.", (
        VerbField("color", "color", "#8e44ad"),
        VerbField("pattern", "enum", "solid", choices=LED_PATTERNS),
        VerbField("period_ms", "ms", 0,
                  description="Animation period (pulse/blink cycle or comet "
                              "revolution)."),
        FADE_FIELD,
        ANGLE_FIELD,
        RING_FIELD,
    )),
    Verb("set_led_halves", "action",
         "Split a ring into equal arcs, one colour each (e.g. half purple, "
         "half yellow). 'comet' instead sweeps one rotating comet per colour. "
         "Pick a 'ring' to target one of the four rings.", (
        VerbField("colors", "colors", ["#8e44ad", "#f1c40f"]),
        VerbField("pattern", "enum", "solid", choices=LED_PATTERNS),
        VerbField("period_ms", "ms", 0,
                  description="Animation period (pulse/blink cycle or comet "
                              "revolution)."),
        FADE_FIELD,
        ANGLE_FIELD,
        RING_FIELD,
    )),
    Verb("fade", "action",
         "Smoothly cross-fade a ring back and forth between two colours. Wrap "
         "in 'repeat forever' for a continuous fade. Pick a 'ring' to fade one "
         "of the four rings independently.", (
        VerbField("color1", "color", "#8e44ad"),
        VerbField("color2", "color", "#f1c40f"),
        VerbField("period_ms", "ms", 2000,
                  description="One full colour1 → colour2 → colour1 cycle."),
        RING_FIELD,
    )),
    Verb("inflate", "action", "Drive a chamber up to a pressure %.", (
        CHAMBER_FIELD,
        VerbField("pct", "pct", 60, description="Target pressure (0-100 %)."),
        VerbField("period_ms", "ms", 0,
                  description="Fill gently over about this long (ms); the pump "
                              "slows to roughly match. 0 = full speed. Needs a "
                              "calibrated fill time, else falls back to full speed."),
        DUTY_FIELD,
    )),
    Verb("deflate", "action", "Empty a chamber back to 0 %.", (
        CHAMBER_FIELD,
    )),
    Verb("set_pressure", "action", "Set a chamber's absolute target %.", (
        CHAMBER_FIELD,
        VerbField("pct", "pct", 0),
        DUTY_FIELD,
    )),
    Verb("beat", "action",
         "One heartbeat cycle across the unit's chambers. Wrap in "
         "'repeat forever' for a continuous beat.", (
        VerbField("mode", "enum", "sync", choices=BEAT_MODES,
                  description="sync=all together; sequential=one at a time; "
                              "random=shuffled one at a time; aligned=N chambers "
                              "at 'pct', the rest at 'pct2'."),
        VerbField("pct", "pct", 60, description="Peak pressure of the beat."),
        VerbField("pct2", "pct", 20,
                  description="Pressure of the misaligned chambers (aligned mode)."),
        VerbField("aligned", "int", 2,
                  description="How many chambers share 'pct' in aligned mode."),
        VerbField("period_ms", "ms", 2000, description="One full cycle."),
        DUTY_FIELD,
    )),
    Verb("wait", "control", "Pause the sequence for a fixed time.", (
        VerbField("ms", "ms", 500),
    )),
    Verb("wait_for_touch", "control",
         "Pause the sequence until the child touches (optionally a specific "
         "chamber). Lets a sequence advance on interaction instead of time.", (
        CHAMBER_FIELD,
    )),
    Verb("stop", "action", "Hold every chamber where it is.", ()),
    Verb("log", "action", "Write a line to the activity log (debugging).", (
        VerbField("message", "enum", ""),
    )),
    # --- Thymio wheeled base (thymio-targeted specs only) ---
    Verb("thymio_drive", "action",
         "Set the Thymio's wheel speeds — optionally for a fixed time, then "
         "stop. 0/0 stops the wheels.", (
        VerbField("left", "int", 100,
                  description="Left wheel target, -500..500 (negative = "
                              "backwards)."),
        VerbField("right", "int", 100,
                  description="Right wheel target, -500..500. Opposite signs "
                              "turn on the spot."),
        VerbField("ms", "ms", 0,
                  description="Drive this long then stop. 0 = keep driving "
                              "until the next thymio_drive."),
    ), kinds=(activity_kind.THYMIO,)),
    Verb("thymio_leds", "action",
         "Colour the Thymio's top LED.", (
        VerbField("color", "color", "#8e44ad"),
    ), kinds=(activity_kind.THYMIO,)),
)

CONTROL: tuple[Verb, ...] = (
    Verb("sequence", "control", "Run the nested steps in order.", (
        VerbField("do", "steps", []),
    )),
    Verb("repeat", "control", "Repeat the nested steps N times, or forever.", (
        VerbField("times", "int", 1),
        VerbField("forever", "enum", False, choices=(True, False)),
        VerbField("do", "steps", []),
    )),
    Verb("for_each_chamber", "control",
         "Run the nested steps once per chamber, binding 'current' to each.", (
        VerbField("do", "steps", []),
    )),
)

CONDITIONS: tuple[Verb, ...] = (
    Verb("elapsed_ms", "condition",
         "True once this many ms passed since the state was entered.", (
        VerbField("ms", "ms", 120000),
    )),
    Verb("touch_count", "condition",
         "True once the unit was touched at least 'min' times in this state.", (
        VerbField("min", "int", 10),
    )),
    Verb("any", "condition", "True if any sub-condition is true (OR).", (
        VerbField("conds", "conds", []),
    )),
    Verb("all", "condition", "True if every sub-condition is true (AND).", (
        VerbField("conds", "conds", []),
    )),
    Verb("not", "condition", "True if the sub-condition is false.", (
        VerbField("cond", "cond", None),
    )),
    Verb("organs", "condition",
         "Organ-status condition, evaluated against the unit's plugged organs "
         "(resolved good/bad/absent from the skin's organ circuit). 'scope' "
         "all_good / all_bad short-circuit to 'every organ is good / bad'; "
         "'count' compares the good and bad counts via their operators.", (
        VerbField("scope", "enum", "count",
                  choices=("count", "all_good", "all_bad"),
                  description="count=use the good/bad comparisons; "
                              "all_good/all_bad ignore them."),
        VerbField("good_op", "enum", ">=", choices=(">=", "<=", "=="),
                  description="How to compare the good-organ count."),
        VerbField("good", "int", 1, description="Good-organ count threshold."),
        VerbField("bad_op", "enum", "<=", choices=(">=", "<=", "=="),
                  description="How to compare the bad-organ count."),
        VerbField("bad", "int", 0, description="Bad-organ count threshold."),
    )),
    Verb("always", "condition", "Always true (unconditional transition).", ()),
)

# Aliases accepted in specs for readability (any↔or, all↔and).
COND_ALIASES = {"or": "any", "and": "all"}

# Verbs no longer offered as blocks but still accepted in saved specs so older
# behaviours keep loading. ``wrinkle`` was dropped because it is identical to
# setting a chamber to 0 % (the runtime maps it to that); author with
# ``set_pressure`` / ``deflate`` instead.
DEPRECATED_STEP_NAMES = {"wrinkle"}

_ALL_VERBS = {v.name: v for v in (*ACTIONS, *CONTROL, *CONDITIONS)}
ACTION_NAMES = {v.name for v in ACTIONS}
CONTROL_NAMES = {v.name for v in CONTROL}
CONDITION_NAMES = {v.name for v in CONDITIONS} | set(COND_ALIASES)
STEP_NAMES = ACTION_NAMES | CONTROL_NAMES | DEPRECATED_STEP_NAMES


def verb(name: str) -> Verb | None:
    return _ALL_VERBS.get(name)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class SpecError(ValueError):
    """Raised when a behaviour spec is malformed."""


def validate_spec(spec: dict[str, Any]) -> None:
    """Raise :class:`SpecError` if ``spec`` is not a runnable behaviour.

    Cheap structural checks only (verb names, state references) — the
    interpreter tolerates missing optional params via defaults.
    """
    if not isinstance(spec, dict):
        raise SpecError("spec must be a dict")
    states = spec.get("states")
    if not isinstance(states, dict) or not states:
        raise SpecError("spec.states must be a non-empty dict")
    initial = spec.get("initial")
    if initial not in states:
        raise SpecError(f"spec.initial {initial!r} is not a defined state")

    # Optional activity target (robot kind + skin variant). Absent = legacy "any"
    # behaviour that runs on whatever robot is picked.
    target = spec.get("target")
    if target is not None:
        _validate_target(target)
    kind = target.get("kind") if isinstance(target, dict) else None

    for sid, state in states.items():
        if not isinstance(state, dict):
            raise SpecError(f"state {sid!r} must be a dict")
        _validate_steps(state.get("do", []), f"{sid}.do", kind)
        _validate_steps(state.get("on_touch", []), f"{sid}.on_touch", kind)
        for i, tr in enumerate(state.get("transitions", []) or []):
            if not isinstance(tr, dict) or "to" not in tr:
                raise SpecError(f"{sid}.transitions[{i}] needs a 'to'")
            if tr["to"] not in states:
                raise SpecError(
                    f"{sid}.transitions[{i}] → unknown state {tr['to']!r}")
            _validate_cond(tr.get("when", {"always": True}),
                           f"{sid}.transitions[{i}].when")


def _validate_target(target: Any) -> None:
    """Check an optional ``spec.target`` (robot kind + skin variant)."""
    if not isinstance(target, dict):
        raise SpecError("spec.target must be a dict")
    kind = target.get("kind")
    if not activity_kind.is_kind(kind):
        raise SpecError(f"spec.target.kind {kind!r} is not a known activity kind")
    # ``skin_variant`` is optional and validated against the type at session setup.


def spec_target(spec: dict[str, Any]) -> dict[str, str] | None:
    """The activity's declared target ``{kind, skin_variant}``, or ``None`` (legacy
    'any' behaviour). Assumes ``spec`` already passed :func:`validate_spec`."""
    t = spec.get("target")
    if isinstance(t, dict) and activity_kind.is_kind(t.get("kind")):
        return {"kind": t["kind"], "skin_variant": t.get("skin_variant", "") or ""}
    return None


def _validate_steps(steps: Any, where: str, kind: str | None = None) -> None:
    if not isinstance(steps, list):
        raise SpecError(f"{where} must be a list of steps")
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or len(step) != 1:
            raise SpecError(f"{where}[{i}] must be a single-key {{verb: …}} dict")
        name = next(iter(step))
        if name not in STEP_NAMES:
            raise SpecError(f"{where}[{i}] unknown verb {name!r}")
        v = _ALL_VERBS.get(name)
        if v is not None and v.kinds and kind not in v.kinds:
            raise SpecError(
                f"{where}[{i}] verb {name!r} needs a spec.target of kind "
                f"{' / '.join(v.kinds)} (got {kind!r})")
        params = step[name]
        if name in CONTROL_NAMES and isinstance(params, dict):
            _validate_steps(params.get("do", []), f"{where}[{i}].do", kind)


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
