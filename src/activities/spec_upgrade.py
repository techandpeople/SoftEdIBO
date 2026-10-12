"""Rewrites the older all-in-one blocks as the small composable ones.

`score_fill`, `sync_fill`, `zone_fill` and `touch_progress` each bundled a
detector, a score, a display, hold feedback and a phase jump. They still run
unchanged (old behaviours keep working), but the editor opens a behaviour in
its upgraded form so authors see - and can rearrange - the parts:

- the detector  -> ``group_rhythm``
- the score     -> ``when`` + ``change_value`` (a named value)
- the display   -> ``show_value`` / ``zone_light``
- hold feedback -> ``hold_feedback``
- the jump      -> an ordinary transition on ``value_is`` / ``zones_full``

Only blocks at the top level of a phase's ``do`` / ``on_touch`` are rewritten;
one nested inside another block is left as it is.

Pure data in, pure data out.
"""

from __future__ import annotations

import copy
from typing import Any

OLD_VERBS = ("score_fill", "sync_fill", "zone_fill", "touch_progress")

_RHYTHM_KEYS = ("mode", "participants", "sensors", "target_interval_ms",
                "cadence_tolerance_ms", "phase_tolerance_ms", "min_gap_ms")
_LIGHT_KEYS = ("fill", "on_color", "bg_color", "fade_ms", "ring")
_ZONE_KEYS = ("kind", "step_pct", "target_interval_ms", "tolerance_ms",
              "min_gap_ms", *_LIGHT_KEYS)
SCORE_NAME = "score"
TOUCHES_NAME = "touches"


def _verb(step: Any) -> str:
    return next(iter(step)) if isinstance(step, dict) and len(step) == 1 else ""


def _params(step: dict) -> dict:
    params = step[_verb(step)]
    return params if isinstance(params, dict) else {}


def _pick(params: dict, keys: tuple[str, ...]) -> dict:
    return {key: params[key] for key in keys if key in params}


def needs_upgrade(spec: dict[str, Any]) -> bool:
    """True if any phase uses one of the older all-in-one blocks at the top
    level of its ``do`` or ``on_touch``."""
    for state in (spec.get("states") or {}).values():
        if not isinstance(state, dict):
            continue
        for key in ("do", "on_touch"):
            if any(_verb(step) in OLD_VERBS for step in state.get(key) or []):
                return True
    return False


def upgrade_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``spec`` with the older blocks rewritten."""
    upgraded = copy.deepcopy(spec)
    for state in (upgraded.get("states") or {}).values():
        if isinstance(state, dict):
            _upgrade_state(state)
    return upgraded


def _upgrade_state(state: dict) -> None:
    jumps: list[dict] = []      # transitions the old blocks' "to" turns into
    body: list = []
    for step in state.get("do") or []:
        verb = _verb(step)
        if verb == "score_fill":
            body.extend(_score_steps(_params(step), jumps))
        elif verb == "sync_fill":
            body.extend(_sync_steps(_params(step)))
        else:
            body.append(step)

    on_touch = state.get("on_touch") or []
    if any(_verb(step) in ("zone_fill", "touch_progress") for step in on_touch):
        handler: list = []
        extra: list = []        # armed once, beside the handler
        for step in on_touch:
            verb = _verb(step)
            if verb == "zone_fill":
                handler.append({"zone_light": _pick(_params(step), _ZONE_KEYS)})
                extra.extend(_hold_steps(_params(step)))
                _jump(_params(step), {"zones_full": {}}, jumps)
            elif verb == "touch_progress":
                handler.extend(_touch_progress_steps(_params(step), jumps))
            else:
                handler.append(step)
        # First in the body: arming is instant, and a body that waits or
        # loops for ever would otherwise never reach it.
        body = [{"when": {"event": "touch", "do": handler}}, *extra, *body]
        state.pop("on_touch", None)

    if body or "do" in state:
        state["do"] = body
    if jumps:
        state["transitions"] = [*jumps, *(state.get("transitions") or [])]


def _jump(params: dict, when: dict, jumps: list[dict]) -> None:
    target = str(params.get("to", "") or "")
    if target:
        jumps.append({"to": target, "when": when})


def _hold_steps(params: dict) -> list[dict]:
    hold = str(params.get("hold", "none") or "none")
    if hold == "none":
        return []
    return [{"hold_feedback": {
        "hold": hold, **_pick(params, ("hold_full_ut", "on_color", "bg_color",
                                       "fade_ms", "ring"))}}]


def _score_steps(params: dict, jumps: list[dict]) -> list[dict]:
    gain = params.get("gain_pct", 15)
    penalty = params.get("penalty_pct", 5)
    limits = {"lowest": 0, "highest": 100}
    _jump(params, {"value_is": {"name": SCORE_NAME, "op": "at_least",
                                "value": 100}}, jumps)
    return [
        {"group_rhythm": _pick(params, _RHYTHM_KEYS)},
        {"set_value": {"name": SCORE_NAME, "to": 0}},
        {"when": {"event": "round_good", "do": [
            {"change_value": {"name": SCORE_NAME, "by": gain, **limits}}]}},
        {"when": {"event": "round_missed", "do": [
            {"change_value": {"name": SCORE_NAME, "by": -penalty, **limits}}]}},
        {"show_value": {"source": "value", "name": SCORE_NAME, "full_at": 100,
                        "arcs": 0, **_pick(params, _LIGHT_KEYS)}},
        *_hold_steps(params),
    ]


def _sync_steps(params: dict) -> list[dict]:
    return [
        {"show_value": {"source": "group_sync", "arcs": 0,
                        "decay_ms": params.get("decay_ms", 150),
                        **_pick(params, _LIGHT_KEYS)}},
        *_hold_steps(params),
    ]


def _touch_progress_steps(params: dict, jumps: list[dict]) -> list[dict]:
    segments = max(1, int(params.get("segments", 4) or 4))
    per = max(1, int(params.get("per", 1) or 1))
    full = segments * per
    _jump(params, {"value_is": {"name": TOUCHES_NAME, "op": "at_least",
                                "value": full}}, jumps)
    return [
        {"change_value": {"name": TOUCHES_NAME, "by": 1, "lowest": 0,
                          "highest": full}},
        {"show_value": {"source": "value", "name": TOUCHES_NAME,
                        "full_at": full, "arcs": segments,
                        **_pick(params, ("on_color", "bg_color", "fade_ms",
                                         "ring"))}},
    ]
