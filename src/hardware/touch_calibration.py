"""Touch↔chamber coupling calibration — GUI-free core + settings helpers.

Builds the pressure-informed compensation matrix (see
:mod:`src.core.touch_compensation`) from a *sweep*: inflate one chamber at a time
to full, hold, and read the magnet sensors at steady state. The per-(chamber,
sensor) magnitude shift vs rest is the coupling — measured in raw uT, the same
units the PC detection path uses.

The matrix maths live in :mod:`src.core.touch_coupling` (tested); this module
adds the settings-tree walks that persist the result onto a skin's ``touch``
block and toggle compensation. The Qt dialog
(``src/gui/touch_calibration_dialog.py``) drives the hardware sweep and feeds
samples in.
"""

from __future__ import annotations

from typing import Any

from src.core.skin_config import MAGNET_NODE_TYPES, YAML_KEY
from src.core.touch_compensation import coupling_to_config
from src.core.touch_coupling import CouplingMatrix, build_coupling


def _iter_robots(settings_data: dict) -> Any:
    """Yield every robot dict across the robots-by-kind buckets."""
    for key in YAML_KEY.values():
        for robot in settings_data.get("robots", {}).get(key, []) or []:
            yield robot


def iter_touch_skins(settings_data: dict) -> list[dict]:
    """List skins whose touch node can stream magnet data (so they can be
    coupling-calibrated), one entry per skin.

    Each entry: ``{robot_id, skin_id, touch_mac, chamber_mac, sensor_count,
    slots, coupling, enabled}``. ``coupling`` is the stored matrix dict (or
    ``None``); ``enabled`` reflects ``touch.compensation.enabled``."""
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
                     suppress_pct: float | None = -1.0) -> bool:
    """Update a skin's ``touch.compensation`` tuning block. Returns True if the
    skin was found.

    Only non-default args are written. ``suppress_pct`` uses the sentinel ``-1``
    to mean 'leave unchanged'; pass ``None`` to clear it (disable suppression)."""
    skin = _find_skin(settings_data, robot_id, skin_id)
    if skin is None:
        return False
    comp = skin.setdefault("touch", {}).setdefault("compensation", {})
    if enabled is not None:
        comp["enabled"] = bool(enabled)
    if threshold_ut is not None:
        comp["threshold_ut"] = float(threshold_ut)
    if suppress_pct != -1.0:
        if suppress_pct is None:
            comp.pop("suppress_pct", None)
        else:
            comp["suppress_pct"] = float(suppress_pct)
    return True


def coupling_config_from_samples(samples: Any, sensor_count: int, *,
                                 ref_pct: float = 100.0,
                                 **build_kw: Any) -> tuple[dict, CouplingMatrix]:
    """Build a stored ``touch.coupling`` dict from sweep samples.

    ``samples`` are ``(t_ms, {slot: pct}, mag_vector)`` tuples (the magnet vector
    in raw uT). Returns ``(coupling_config, matrix)`` — the matrix is handy for a
    UI preview of the per-(chamber, sensor) deltas before saving."""
    matrix = build_coupling(samples, sensor_count, **build_kw)
    cfg = coupling_to_config(matrix.deltas, sensor_count, ref_pct=ref_pct)
    return cfg, matrix
