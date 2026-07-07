"""Touch↔chamber coupling calibration — GUI-free core + settings helpers.

Builds the pressure-informed compensation model (see
:mod:`src.core.touch_compensation`) from a *sweep*: inflate one chamber at a
time through a staircase of levels, hold each, and read the magnet sensors at
steady state. The per-(chamber, level, sensor) magnitude shift vs rest is the
coupling curve — measured in raw uT, the same units the PC detection path uses.

:class:`SweepProgram` is the pure step sequence of that sweep (what to send,
how long to dwell); the Qt dialog (``src/gui/touch_calibration_dialog.py``)
just executes it against the gateway and feeds the samples back in. The curve
maths live in :mod:`src.core.touch_coupling` (tested); this module also adds
the settings-tree walks that persist the result onto a skin's ``touch`` block
and toggle compensation.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations, product
from typing import Any

from src.core.skin_config import (
    DEFAULT_MAX_KPA,
    DEFAULT_MIN_KPA,
    MAGNET_NODE_TYPES,
    YAML_KEY,
)
from src.core.touch_compensation import coupling_to_config
from src.core.touch_coupling import (
    ACTIVE_MIN,
    BIN_PCT,
    CouplingModel,
    build_coupling,
)

# Sweep timing defaults. Dwell well past the coupling analyzer's steady-state
# guard (settle_ms 800) so each state contributes plenty of settled samples.
REST_MS = 2500
DWELL_MS = 3500


@dataclass(frozen=True)
class SweepStep:
    """One hardware step of a coupling sweep.

    ``action`` is ``"rest"`` (vent every chamber to establish/refresh the
    baseline) or ``"state"`` (drive the chambers to ``levels`` and hold).
    ``levels`` maps each *target* chamber to its percentage; chambers absent
    from it must be vented. ``wait_ms`` is the dwell before the next step;
    ``progress`` is the 0-100 sweep completion when the step starts."""
    action: str
    levels: dict[int, float]
    wait_ms: int
    label: str
    progress: int


class SweepProgram:
    """The pure step sequence of a coupling sweep over chamber *combinations*.

    Coupling is non-additive — two chambers inflated together can move a sensor
    differently than the sum of each alone — so the sweep visits every chamber
    *subset* (up to ``max_order``) over a per-member level grid. Because a state
    with a member at 0 is just a lower-order subset, this is exactly the full
    Cartesian grid over each chamber's axis ``[0, g1, .., 100]``; the runtime
    interpolates it multilinearly. Each subset is preceded by a ``rest`` (vent
    all) so the baseline stays fresh and boundaries are clean; within a subset
    the states raster the grid, each an explicit per-chamber target. Building it
    here keeps the dialog a dumb executor and makes the sequence unit-testable.
    """

    def __init__(self, slots: list[int], *,
                 step_pct: float = 10.0, max_order: int | None = None,
                 rest_ms: int = REST_MS, dwell_ms: int = DWELL_MS,
                 min_level: float = ACTIVE_MIN) -> None:
        self.slots = list(dict.fromkeys(int(s) for s in slots))
        self.step_pct = float(step_pct)
        self.rest_ms = int(rest_ms)
        self.dwell_ms = int(dwell_ms)
        self.grid = self.grid_levels(step_pct, min_level)
        n = len(self.slots)
        self.max_order = n if max_order is None else max(1, min(int(max_order), n))
        self.subsets = [combo for k in range(1, self.max_order + 1)
                        for combo in combinations(self.slots, k)]
        self.n_states = sum(len(self.grid) ** len(sub) for sub in self.subsets)
        self.n_rests = len(self.subsets)

        steps: list[SweepStep] = []
        total = max(1, self.n_states + self.n_rests)

        def pct() -> int:
            return int(100 * len(steps) / total)

        for sub in self.subsets:
            steps.append(SweepStep("rest", {}, rest_ms,
                                   "Resting (venting all chambers)…", pct()))
            for point in product(self.grid, repeat=len(sub)):
                levels = dict(zip(sub, point))
                label = ("Chambers " if len(sub) > 1 else "Chamber ") + "+".join(
                    f"{c}@{levels[c]:.0f}%" for c in sub) + "…"
                steps.append(SweepStep("state", levels, dwell_ms, label, pct()))
        self.steps = steps

    @staticmethod
    def grid_levels(step_pct: float,
                    min_level: float = ACTIVE_MIN) -> tuple[float, ...]:
        """Per-member grid levels: ``step_pct`` apart up to 100 %, floored at
        ``min_level`` (levels below the inflated threshold cannot register as a
        member, so they are skipped — the origin at 0 is implicit)."""
        step = max(1.0, float(step_pct))
        out, v = [], step
        while v < 100.0:
            if v >= min_level:
                out.append(round(v, 1))
            v += step
        out.append(100.0)
        return tuple(sorted(set(out)))

    @property
    def est_seconds(self) -> float:
        """Rough wall-clock estimate (rests + one dwell per state)."""
        return (self.n_rests * self.rest_ms + self.n_states * self.dwell_ms) / 1000.0


def _iter_robots(settings_data: dict) -> Any:
    """Yield every robot dict across the robots-by-kind buckets."""
    for key in YAML_KEY.values():
        for robot in settings_data.get("robots", {}).get(key, []) or []:
            yield robot


def iter_touch_skins(settings_data: dict) -> list[dict]:
    """List skins whose touch node can stream magnet data (so they can be
    coupling-calibrated), one entry per skin.

    Each entry: ``{robot_id, skin_id, touch_mac, chamber_mac, sensor_count,
    slots, limits, coupling, enabled}``. ``coupling`` is the stored matrix dict
    (or ``None``); ``enabled`` reflects ``touch.compensation.enabled``;
    ``limits`` maps each slot to its configured ``(min_kpa, max_kpa)`` — the
    range the sweep uses to recompute inflation % from the status ``kpa`` (the
    firmware's own ``pressure`` field is computed against the limits the node
    currently holds, which lag the PC config)."""
    out: list[dict] = []
    for robot in _iter_robots(settings_data):
        node_types = {n.get("mac"): n.get("node_type")
                      for n in (robot.get("nodes") or [])}
        for skin in robot.get("skins") or []:
            touch = skin.get("touch") or {}
            tmac = touch.get("node_mac")
            if not tmac or node_types.get(tmac) not in MAGNET_NODE_TYPES:
                continue
            chambers = skin.get("chambers") or []
            comp = touch.get("compensation") or {}
            out.append({
                "robot_id": robot.get("id", ""),
                "skin_id": skin.get("skin_id", ""),
                "touch_mac": tmac,
                "chamber_mac": chambers[0].get("mac") if chambers else None,
                "sensor_count": int(touch.get("sensor_count", 4)),
                "slots": [int(c.get("slot", 0)) for c in chambers],
                "limits": {
                    int(c.get("slot", 0)): (
                        float(c.get("min_pressure", DEFAULT_MIN_KPA)),
                        float(c.get("max_pressure", DEFAULT_MAX_KPA)))
                    for c in chambers},
                "coupling": touch.get("coupling"),
                "enabled": bool(comp.get("enabled", False)),
            })
    return out


def _find_skin(settings_data: dict, robot_id: str, skin_id: str) -> dict | None:
    for robot in _iter_robots(settings_data):
        if robot.get("id", "") != robot_id:
            continue
        for skin in robot.get("skins") or []:
            if skin.get("skin_id", "") == skin_id:
                return skin
    return None


def set_touch_coupling(settings_data: dict, robot_id: str, skin_id: str,
                       coupling: dict | None) -> bool:
    """Write (or clear) the ``touch.coupling`` matrix on a skin. Returns True if
    the skin was found. Clearing also drops the matrix key entirely."""
    skin = _find_skin(settings_data, robot_id, skin_id)
    if skin is None:
        return False
    touch = skin.setdefault("touch", {})
    if coupling:
        touch["coupling"] = coupling
    else:
        touch.pop("coupling", None)
    return True


def set_compensation(settings_data: dict, robot_id: str, skin_id: str, *,
                     enabled: bool | None = None,
                     threshold_ut: float | None = None,
                     margin_frac: float | None = None,
                     guard_ms: float | None = None,
                     suppress_pct: float | None = -1.0,
                     clear_threshold: bool = False) -> bool:
    """Update a skin's ``touch.compensation`` tuning block. Returns True if the
    skin was found.

    Only non-default args are written. ``suppress_pct`` uses the sentinel ``-1``
    to mean 'leave unchanged'; pass ``None`` to clear it (disable suppression).
    ``margin_frac`` raises the touch threshold by that fraction of the applied
    correction; ``guard_ms`` hardens sensors for that long after a coupled
    chamber's level changes (0 disables the guard). ``clear_threshold`` drops any
    stored ``threshold_ut`` so the compensator falls back to the single source of
    truth — the node's ``act_threshold_ut`` set from the sensor tester."""
    skin = _find_skin(settings_data, robot_id, skin_id)
    if skin is None:
        return False
    comp = skin.setdefault("touch", {}).setdefault("compensation", {})
    if enabled is not None:
        comp["enabled"] = bool(enabled)
    if clear_threshold:
        comp.pop("threshold_ut", None)
    elif threshold_ut is not None:
        comp["threshold_ut"] = float(threshold_ut)
    if margin_frac is not None:
        comp["margin_frac"] = round(float(margin_frac), 3)
    if guard_ms is not None:
        comp["guard_ms"] = float(guard_ms)
    if suppress_pct != -1.0:
        if suppress_pct is None:
            comp.pop("suppress_pct", None)
        else:
            comp["suppress_pct"] = float(suppress_pct)
    return True


def sweep_diagnostics(samples: Any, slots: list[int],
                      measured: Any = None) -> str:
    """Explain a missing (or empty) sweep result from its raw samples.

    Distinguishes the failure modes: no magnet samples at all (the touch node
    never streamed); samples whose chamber levels never reached "inflated"
    (pressure never hit :data:`ACTIVE_MIN` % — stale kPa limits, pumps not
    running, wrong chamber node); or a chamber that *did* inflate but got no
    usable data because a neighbour was co-inflated whenever it peaked.

    ``measured`` is the set of chambers that made it into the matrix; when given,
    only the *missing* slots are diagnosed and, for a slot that reached
    ``ACTIVE_MIN`` but was still dropped, the worst co-inflated neighbour at its
    peak moment is named. Returns a short multi-line hint for the operator."""
    samples = list(samples)
    if not samples:
        return ("Diagnostics: 0 magnet samples arrived — the touch node never "
                "streamed during the sweep. Check that it is powered, paired "
                "to the gateway, and that the skin's touch node_mac is right.")
    peaks: dict[int, float] = {int(s): 0.0 for s in slots}
    for sample in samples:
        for slot, pct in (sample[1] or {}).items():
            if int(slot) in peaks:
                peaks[int(slot)] = max(peaks[int(slot)], float(pct))
    lines = [f"Diagnostics: {len(samples)} magnet samples; "
             "peak inflation seen per chamber:"]
    lines += [f"  slot {slot}: {peak:.0f}%" for slot, peak in sorted(peaks.items())]
    measured_set = None if measured is None else set(measured)
    for slot in sorted(peaks):
        if measured_set is None or slot not in measured_set:
            lines += _missing_slot_hint(samples, slot, peaks[slot])
    if all(peak < ACTIVE_MIN for peak in peaks.values()):
        lines.append(
            f"All below {ACTIVE_MIN:.0f}% — no sample ever classified as "
            "'inflated'. Check the chambers' kPa limits in the skin config, "
            "and that the pumps actually ran during the sweep.")
    return "\n".join(lines)


def _missing_slot_hint(samples: Any, slot: int, peak: float) -> list[str]:
    """Explain why an inflated-but-dropped ``slot`` produced no usable data."""
    if peak < ACTIVE_MIN:
        return []
    other, other_pct = _worst_neighbor_at_peak(samples, slot)
    if other is None:
        return []
    return [f"slot {slot} reached {peak:.0f}% but was dropped: chamber {other} "
            f"was co-inflated (~{other_pct:.0f}%) when it peaked. Let that "
            f"neighbour vent fully before slot {slot}, or widen its kPa range "
            f"so its residual reads low."]


def _worst_neighbor_at_peak(samples: Any, slot: int) -> tuple[int | None, float]:
    """The other chamber that was most inflated at ``slot``'s peak moment.

    Finds the sample where ``slot`` read highest and returns the neighbour with
    the largest level there — the likely reason ``slot``'s samples were all
    classified ambiguous. Returns ``(None, 0.0)`` if no neighbour was up."""
    best_level, best_other, best_other_pct = -1.0, None, 0.0
    for sample in samples:
        pressures = sample[1] or {}
        level = float(pressures.get(slot, 0.0))
        if level <= best_level:
            continue
        best_level = level
        best_other, best_other_pct = None, 0.0
        for other, pct in pressures.items():
            if int(other) != slot and float(pct) > best_other_pct:
                best_other, best_other_pct = int(other), float(pct)
    return best_other, best_other_pct


def coupling_config_from_samples(samples: Any, sensor_count: int,
                                 **build_kw: Any) -> tuple[dict, CouplingModel]:
    """Build a stored ``touch.coupling`` dict from sweep samples.

    ``samples`` are ``(t_ms, {slot: pct}, mag_vector[, vec_rows])`` tuples (the
    magnet vector in raw uT). The config carries every measured state — single
    chambers *and* combinations — as an N-dimensional grid the runtime
    interpolates. Returns ``(coupling_config, model)`` — the model is handy for a
    UI preview of the measured states before saving."""
    model = build_coupling(samples, sensor_count, **build_kw)
    bin_pct = float(build_kw.get("bin_pct", BIN_PCT))
    cfg = coupling_to_config(model, bin_pct=bin_pct)
    return cfg, model
