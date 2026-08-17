"""Touch-motion helpers - where a touch sits and which way it moved.

Pure, Qt-free geometry over the magnet stream: a magnitude-weighted centroid per
sample (for the live motion trail in the sensor grid) and a start->end direction
for a completed :class:`~src.ml.touch_segmenter.TouchSegment` (for the capture
summary - "stroke S1->S0 <- left"). Positions come from the skin-geometry
registry's ``sensors_mm``, so everything stays in the skin's own millimetres.

With 4 sensors the direction is coarse (quadrant to quadrant) but honest; a
denser (e.g. capacitive) layout sharpens it automatically since only the
position list grows.
"""

from __future__ import annotations

from math import atan2, hypot, pi, sqrt

from src.ml import gesture_taxonomy as _tax
from src.ml.gesture_taxonomy import (
    SLIDE_MAX_Z_FRAC,
    SLIDE_MIN_STRAIGHTNESS,
    SLIDE_MIN_TRAVEL_MM,
)

# 8-way compass in SCREEN coordinates (y down), anticlockwise from +x.
# Each entry is (arrow, english name); bucket size is 45 deg.
_CARDINALS = (
    ("->", "right"), ("v>", "down-right"), ("v", "down"), ("<v", "down-left"),
    ("<-", "left"), ("<^", "up-left"), ("^", "up"), ("^>", "up-right"),
)

# Tunables live in gesture_taxonomy (the single home of gesture thresholds);
# kept here as module aliases for readability at the call sites below.
MIN_TRAVEL_MM = SLIDE_MIN_TRAVEL_MM
MAX_SLIDE_Z_FRAC = SLIDE_MAX_Z_FRAC
MIN_STRAIGHTNESS = SLIDE_MIN_STRAIGHTNESS


def path_straightness(points) -> float:
    """Net displacement / total path length of a ``(x, y)`` sequence (0..1).

    ``1`` is a perfectly straight line; near ``0`` is wandering or circling
    (long path, small net travel). Returns ``0`` for fewer than two points or a
    zero-length path. Scale-invariant, so pixel or fraction coordinates both
    work. This is what separates a finger dragging A->B from the touch centroid
    orbiting between sensors as an inflating chamber shifts the magnet."""
    if len(points) < 2:
        return 0.0
    path = 0.0
    for a, b in zip(points, points[1:]):
        path += hypot(b[0] - a[0], b[1] - a[1])
    if path <= 0.0:
        return 0.0
    net = hypot(points[-1][0] - points[0][0], points[-1][1] - points[0][1])
    return net / path


def _centroid_path(seg, positions) -> list[tuple[float, float]]:
    """Per-sample magnitude-weighted centroid of a segment (skips quiet samples)."""
    out: list[tuple[float, float]] = []
    for row in getattr(seg, "mags", []) or []:
        c = weighted_centroid(row, positions)
        if c is not None:
            out.append(c)
    return out


def frame_z_frac(mag, vec) -> float | None:
    """|z|-fraction of the dominant sensor's 3-axis delta in ONE reading.

    ``None`` when the reading carries no usable vector data. The live trail
    uses this to judge, frame by frame, whether the touch is a vertical push
    (z-dominated) or a lateral drag - same physics as
    :func:`src.ml.touch_features.dominant_z_frac` uses per segment."""
    if not isinstance(vec, (list, tuple)) or not mag:
        return None
    dominant = max(range(len(mag)), key=lambda i: mag[i])
    v = vec[dominant] if dominant < len(vec) else None
    if not (isinstance(v, (list, tuple)) and len(v) >= 3):
        return None
    norm = sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)
    if norm < 1e-6:
        return None
    return abs(v[2]) / norm


def weighted_centroid(mag, positions, floor: float = 0.0
                      ) -> tuple[float, float] | None:
    """Magnitude-weighted mean position of a reading, or None when quiet.

    ``mag`` is the per-sensor magnitudes (uT); ``positions`` the matching
    ``(x, y)`` list (mm). Sensors below ``floor`` don't pull the centroid, so a
    resting sensor's noise doesn't drag the point around.
    """
    total = 0.0
    cx = cy = 0.0
    for i, (x, y) in enumerate(positions):
        w = float(mag[i]) if i < len(mag) else 0.0
        if w <= floor:
            continue
        total += w
        cx += x * w
        cy += y * w
    if total <= 0.0:
        return None
    return cx / total, cy / total


def cardinal(dx: float, dy: float) -> tuple[str, str]:
    """8-way (arrow, name) of a screen-coords vector (y down)."""
    angle = atan2(dy, dx)                       # -pi..pi, 0 = +x
    sector = round(angle / (pi / 4)) % 8
    return _CARDINALS[sector]


def segment_direction(seg, positions,
                      min_travel_mm: float = MIN_TRAVEL_MM) -> dict | None:
    """Start->end travel of a REAL slide, or None for anything else.

    Two separate touches on different sensors must not read as a slide, so the
    travel (centroid of the sensors active at touch-down versus at lift-off)
    only counts when the contact was genuinely continuous:

    * multi-pulse segments (``n_pulses > 1``) released the skin between pulses -
      merged taps, never a slide;
    * with 3-axis ``vec`` data, the dominant sensor must show lateral (x/y)
      shear - a straight-down push that happens to migrate sensors is separate
      vertical touches, not a finger travelling (see :data:`MAX_SLIDE_Z_FRAC`).

    Returns ``{"arrow", "name", "dist_mm", "from_sensors", "to_sensors"}``;
    None for a static touch (tap/press) or when positions are unknown.
    """
    if not positions or not getattr(seg, "acts", None):
        return None
    if getattr(seg, "n_pulses", 1) > 1:
        return None                      # contact released between pulses
    from src.ml.touch_features import dominant_z_frac
    z_frac = dominant_z_frac(seg)
    if z_frac is not None and z_frac >= MAX_SLIDE_Z_FRAC:
        return None                      # pure vertical push - no lateral drag
    first = next((a for a in seg.acts if a), None)
    last = next((a for a in reversed(seg.acts) if a), None)
    if not first or not last:
        return None

    # Direction + straightness come from the magnitude-weighted centroid PATH,
    # so a touch that wandered/circled between sensors (an inflating chamber
    # shifting the magnet) is rejected even when its endpoints look displaced.
    path = _centroid_path(seg, positions)
    if len(path) < 2 or path_straightness(path) < MIN_STRAIGHTNESS:
        return None
    dx, dy = path[-1][0] - path[0][0], path[-1][1] - path[0][1]
    dist = hypot(dx, dy)
    if dist < min_travel_mm:
        return None
    arrow, name = cardinal(dx, dy)
    return {"arrow": arrow, "name": name, "dist_mm": dist,
            "from_sensors": sorted(first), "to_sensors": sorted(last)}


def segment_summary(seg, label: str, positions) -> str:
    """One human line about a captured gesture - everything the stream shows.

    E.g. ``stroke | 420 ms | S1->S0 <- left`` or ``double_tap | 2 pulses |
    310 ms | peak 260 uT | S3``.
    """
    parts = [label]
    pulses = getattr(seg, "n_pulses", 1)
    if pulses > 1:
        parts.append(f"{pulses} pulses")
    parts.append(f"{seg.duration_ms:.0f} ms")
    peak = max((v for row in seg.mags for v in row), default=0.0)
    if peak > 0.0:
        parts.append(f"peak {peak:.0f} uT")
    direction = (segment_direction(seg, positions)
                 if _tax.SLIDE_DETECTION_ENABLED else None)
    if direction is not None:
        frm = "+".join(f"S{i}" for i in direction["from_sensors"])
        to = "+".join(f"S{i}" for i in direction["to_sensors"])
        parts.append(f"{frm}->{to} {direction['arrow']} {direction['name']}")
    else:
        touched = sorted(set().union(*seg.acts)) if seg.acts else []
        if touched:
            parts.append("+".join(f"S{i}" for i in touched))
    return " | ".join(parts)
