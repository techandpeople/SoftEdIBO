"""Reads a touch segment on the taxonomy's four axes.

Form, force and extent are plain rules on what the sensors measured, so every
reading can be explained. Movement has a rule too, used when no trained
movement model is available; a model's verdict replaces it.

``read_touch`` returns the axis values, the gesture they derive to
(:func:`src.ml.gesture_taxonomy.derive_gesture`) and the continuous descriptors
behind them. Pure Python, no numpy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.ml import gesture_taxonomy as tax
from src.ml.touch_features import extract_features
from src.ml.touch_segmenter import TouchSegment

# Activation threshold assumed when a skin configures none (uT).
DEFAULT_THRESHOLD_UT = 300.0


@dataclass(frozen=True)
class TouchReading:
    """One touch described on the four axes."""

    gesture: str
    form: str
    movement: str
    force: str
    extent: str
    descriptors: dict[str, float] = field(default_factory=dict)

    def as_details(self) -> dict[str, Any]:
        """Flat dict for the session log."""
        return {"form": self.form, "movement": self.movement,
                "force": self.force, "extent": self.extent,
                **self.descriptors}


def activation_threshold(touch_cfg: dict | None) -> float:
    """The uT level a skin's sensors count as touched, from its touch config."""
    cfg = touch_cfg or {}
    value = cfg.get("act_threshold_ut")
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    per_sensor = [v for v in cfg.get("quadrant_thresholds") or []
                  if isinstance(v, (int, float)) and v > 0]
    if per_sensor:
        return sum(per_sensor) / len(per_sensor)
    return DEFAULT_THRESHOLD_UT


def form_of(feats: dict[str, float]) -> str:
    pulses = int(feats["n_pulses"])
    if pulses >= tax.REPEATED_MIN_PULSES:
        # Two pulses give one gap: no rhythm to call regular yet.
        steady = (pulses >= tax.COMPRESSIONS_MIN_PULSES
                  and feats["pulse_interval_cv"] <= tax.REGULAR_MAX_CV)
        return tax.FORM_REGULAR if steady else tax.FORM_IRREGULAR
    if feats["duration_ms"] <= tax.TAP_MAX_MS:
        return tax.FORM_SHORT
    if feats["duration_ms"] >= tax.REST_MIN_MS:
        return tax.FORM_REST
    return tax.FORM_PRESS


def force_of(feats: dict[str, float], threshold_ut: float) -> str:
    ratio = feats["peak_mag"] / threshold_ut if threshold_ut > 0 else 0.0
    if ratio >= tax.FORCE_STRONG_RATIO:
        return tax.FORCE_STRONG
    if ratio >= tax.FORCE_MEDIUM_RATIO:
        return tax.FORCE_MEDIUM
    return tax.FORCE_LIGHT


def extent_of(feats: dict[str, float]) -> str:
    sensors = feats["n_sensors"]
    at_once = round(feats["active_frac_max"] * sensors)
    if at_once <= 1:
        return tax.EXTENT_POINT
    return tax.EXTENT_WHOLE if at_once >= sensors else tax.EXTENT_MULTI


def movement_by_rules(feats: dict[str, float]) -> str:
    """Movement from the order sensors activated in (and, when the node
    streams 3-axis data, whether the push was lateral rather than straight
    down). A fallback: coarse with few sensors."""
    travelled = feats["is_sequential"] >= 1.0 and feats["path_len"] >= 2
    pushed_down = (feats["vec_present"] >= 1.0
                   and feats["vec_z_frac"] >= tax.SLIDE_MAX_Z_FRAC)
    if not travelled or pushed_down:
        return tax.MOVE_STATIC
    return tax.MOVE_RUB if feats["path_revisits"] >= 1 else tax.MOVE_SLIDE


def read_touch(seg: TouchSegment, threshold_ut: float = DEFAULT_THRESHOLD_UT,
               movement: str | None = None,
               pressure_rise_kpa: float | None = None) -> TouchReading:
    """Describe ``seg`` on the four axes and name its gesture.

    ``movement`` is a trained model's verdict, when there is one; otherwise
    the rule decides. ``pressure_rise_kpa`` is the chamber pressure rise
    during the touch, when known (needed to call a squeeze)."""
    feats = extract_features(seg)
    if feats["n_samples"] == 0:
        return TouchReading(tax.UNKNOWN, "", "", "", "")
    form = form_of(feats)
    if movement not in tax.MOVEMENTS:
        movement = movement_by_rules(feats)
    force = force_of(feats, threshold_ut)
    extent = extent_of(feats)
    gesture = tax.derive_gesture(
        form, movement, force, extent, n_pulses=int(feats["n_pulses"]),
        duration_ms=feats["duration_ms"], pressure_rise_kpa=pressure_rise_kpa)
    descriptors = {
        "duration_ms": round(feats["duration_ms"]),
        "n_pulses": int(feats["n_pulses"]),
        "peak_ut": round(feats["peak_mag"], 1),
        "rise_ms": round(feats["rise_ms"]),
        "sensors": int(feats["n_distinct_sensors"]),
        "path_len": int(feats["path_len"]),
        "pulse_interval_ms": round(feats["pulse_interval_ms"]),
        "pulse_interval_cv": round(feats["pulse_interval_cv"], 3),
    }
    if feats["vec_present"] >= 1.0:
        descriptors["z_frac"] = round(feats["vec_z_frac"], 3)
        descriptors["dir_consistency"] = round(feats["vec_dir_consistency"], 3)
    if pressure_rise_kpa is not None:
        descriptors["pressure_rise_kpa"] = round(pressure_rise_kpa, 2)
    return TouchReading(gesture, form, movement, force, extent, descriptors)
