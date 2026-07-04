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
from typing import Any

from src.core.skin_config import (
    DEFAULT_MAX_KPA,
    DEFAULT_MIN_KPA,
    MAGNET_NODE_TYPES,
    YAML_KEY,
)
from src.core.touch_compensation import coupling_to_config
from src.core.touch_coupling import ACTIVE_MIN, CouplingMatrix, build_coupling

# Sweep timing defaults. Dwell well past the coupling analyzer's steady-state
# guard (settle_ms 800) so each level contributes plenty of settled samples.
REST_MS = 2500
DWELL_MS = 3500
# The lowest staircase level: safely above touch_coupling.ACTIVE_MIN so a
# chamber settling slightly under target still classifies as that chamber.
MIN_SWEEP_LEVEL = 25.0


@dataclass(frozen=True)
class SweepStep:
    """One hardware step of a coupling sweep.

    ``action`` is what the executor sends (``deflate_all`` / ``set_pressure`` /
    ``deflate``); ``wait_ms`` is how long to dwell before the next step;
    ``progress`` is the 0-100 sweep completion when the step starts."""
    action: str
    slot: int | None
    level: float
    wait_ms: int
    label: str
    progress: int


class SweepProgram:
    """The pure step sequence of a coupling sweep.

    Rest first (baseline), then per chamber an *ascending staircase* of
    inflation levels (one dwell each — each level becomes a coupling-curve
    point), then deflate back and settle. Building it here keeps the dialog a
    dumb executor and makes the sequence unit-testable.
    """

    def __init__(self, slots: list[int],
                 levels: tuple[float, ...] = (100.0,), *,
                 rest_ms: int = REST_MS, dwell_ms: int = DWELL_MS) -> None:
        self.slots = list(slots)
        self.levels = tuple(sorted({float(lv) for lv in levels}))
        steps: list[SweepStep] = []
        total = max(1, len(self.slots) * (len(self.levels) + 1) + 1)

        def pct() -> int:
            return int(100 * len(steps) / total)

        steps.append(SweepStep("deflate_all", None, 0.0, rest_ms,
                               "Resting (establishing baseline)…", pct()))
        for slot in self.slots:
            for level in self.levels:
                steps.append(SweepStep(
                    "set_pressure", slot, level, dwell_ms,
                    f"Chamber slot {slot} → {level:.0f} %…", pct()))
            steps.append(SweepStep(
                "deflate", slot, 0.0, rest_ms,
                f"Chamber slot {slot} → rest…", pct()))
        self.steps = steps

    @staticmethod
    def levels_for(count: int) -> tuple[float, ...]:
        """``count`` staircase levels evenly spaced up to 100 %, floored at
        :data:`MIN_SWEEP_LEVEL` (1 → the legacy full-inflation-only sweep)."""
        count = max(1, int(count))
        return tuple(sorted({float(max(MIN_SWEEP_LEVEL,
                                       round(100.0 * i / count)))
                             for i in range(1, count + 1)}))


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
                     suppress_pct: float | None = -1.0) -> bool:
    """Update a skin's ``touch.compensation`` tuning block. Returns True if the
    skin was found.

    Only non-default args are written. ``suppress_pct`` uses the sentinel ``-1``
    to mean 'leave unchanged'; pass ``None`` to clear it (disable suppression).
    ``margin_frac`` raises the touch threshold by that fraction of the applied
    correction; ``guard_ms`` hardens sensors for that long after a coupled
    chamber's level changes (0 disables the guard)."""
    skin = _find_skin(settings_data, robot_id, skin_id)
    if skin is None:
        return False
    comp = skin.setdefault("touch", {}).setdefault("compensation", {})
    if enabled is not None:
        comp["enabled"] = bool(enabled)
    if threshold_ut is not None:
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


def sweep_diagnostics(samples: Any, slots: list[int]) -> str:
    """Explain an empty sweep result from its raw samples.

    Distinguishes the two failure modes: no magnet samples at all (the touch
    node never streamed) vs samples whose chamber levels never classified as
    "inflated" (pressure never reached :data:`ACTIVE_MIN` % — stale kPa limits,
    pumps not running, wrong chamber node). Returns a short multi-line hint for
    the operator."""
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
    if all(peak < ACTIVE_MIN for peak in peaks.values()):
        lines.append(
            f"All below {ACTIVE_MIN:.0f}% — no sample ever classified as "
            "'inflated'. Check the chambers' kPa limits in the skin config, "
            "and that the pumps actually ran during the sweep.")
    return "\n".join(lines)


def coupling_config_from_samples(samples: Any, sensor_count: int, *,
                                 ref_pct: float = 100.0,
                                 **build_kw: Any) -> tuple[dict, CouplingMatrix]:
    """Build a stored ``touch.coupling`` dict from sweep samples.

    ``samples`` are ``(t_ms, {slot: pct}, mag_vector[, vec_rows])`` tuples (the
    magnet vector in raw uT). The config carries both the legacy single-point
    ``deltas`` (scaled to ``ref_pct`` for older readers) and the full
    multi-level ``curves``. Returns ``(coupling_config, matrix)`` — the matrix
    is handy for a UI preview of the per-(chamber, sensor) deltas before
    saving."""
    matrix = build_coupling(samples, sensor_count, **build_kw)
    deltas = {}
    for chamber, points in matrix.curves.items():
        top = points[-1]
        scale = ref_pct / top.level_pct if top.level_pct > 0.0 else 1.0
        deltas[chamber] = [v * scale for v in top.mag]
    cfg = coupling_to_config(deltas, sensor_count, ref_pct=ref_pct,
                             curves=matrix.curves_for_config())
    return cfg, matrix
