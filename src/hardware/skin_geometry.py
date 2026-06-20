"""Hardcoded skin-geometry registry, keyed by ``skin_type``.

Single, reliable source of truth for each skin type's physical shape and the
**sensor coordinates** — the data the firmware does not announce and that the
per-skin GUI editor was configuring unreliably. Edit these constants by hand
when the hardware changes; both the GUI (to draw each skin type correctly) and
any future spatial analysis read from here.

Coordinates are in **millimetres**, origin at the skin's top-left bounding box,
x→right, y→down (screen convention). Sensor order matches the firmware's stream
order (sensor index *i* ↔ ``mag[i]``/``act`` index *i*).

The touch-gesture ML does **not** depend on these coordinates (it is per-skin-
type and index-based); they exist for rendering and as the reliable geometry
source should spatial features ever be added.

> Several entries below are first-pass placeholders (marked ``# TODO: measure``)
> — adjust to the real builds. Sensor count varies by build: a skin uses as many
> of the board's sensors as fit (e.g. ``tree_round`` 1, ``turtle_side`` 2,
> ``turtle_square`` 4).
>
> **Sensor-count limitation (honest):** spatial quadrant position tracking only
> engages at **4 sensors** (see ``Skin._setup_touch_tracking``, skin.py:314).
> Fewer-sensor skins still get touch *reactions* and per-skin-type gesture ML,
> but resolve less:
>   - **1 sensor** (e.g. ``tree_round``): only tap / press / hold, by magnitude
>     and timing — no direction, no drag.
>   - **2 sensors** (e.g. ``turtle_side``): the above plus one axis of direction.
>   - **4 sensors** (e.g. ``turtle_square``): full quadrant position + drag.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkinGeometry:
    """Canonical geometry of one skin type.

    Attributes:
        skin_type: Stable identifier (matches ``Skin.skin_type``).
        shape: ``"round"`` | ``"rect"`` | ``"triangle"`` | ``"thymio"``.
        size_mm: Bounding-box ``(width, height)`` in mm.
        sensors_mm: One ``(x, y)`` per sensor, in firmware stream order.
        functional: False for purely decorative skins (no sensors/chambers).
        notes: Free-text provenance / TODO.
    """
    skin_type: str
    shape: str
    size_mm: tuple[float, float]
    sensors_mm: tuple[tuple[float, float], ...] = ()
    robot_kind: str = ""          # "turtle" | "tree" | "thymio" (for GUI filtering)
    functional: bool = True
    notes: str = ""

    @property
    def sensor_count(self) -> int:
        return len(self.sensors_mm)

    def sensor_grid(self, cols: int = 4, rows: int = 4) -> list[list[int]]:
        """Derive a ``rows×cols`` sensor-index grid from the sensor coordinates,
        for the activity view to place sensors without a drawn grid. Each sensor
        is dropped into the cell nearest its position within the bounding box;
        cells with no sensor are ``-1``. (Positions are constants — see the
        ``sensors_mm`` of this type.)"""
        grid = [[-1] * cols for _ in range(rows)]
        w, h = self.size_mm
        for idx, (x, y) in enumerate(self.sensors_mm):
            c = min(cols - 1, max(0, int(x / w * cols))) if w else 0
            r = min(rows - 1, max(0, int(y / h * rows))) if h else 0
            grid[r][c] = idx
        return grid

    def natural_sensor_grid(self, tol_mm: float = 1.0
                            ) -> tuple[int, int, list[list[int]]]:
        """Sensor grid sized to the actual sensor arrangement: one column per
        distinct x position and one row per distinct y position. Each sensor
        then fills a whole cell (e.g. a quadrant for a 2×2 layout) instead of a
        single cell of an arbitrarily fine grid, so the activity view's touch
        highlight covers the sensor's region. Returns ``(cols, rows, grid)``."""
        if not self.sensors_mm:
            return 1, 1, [[-1]]
        xs = _cluster_axis((x for x, _ in self.sensors_mm), tol_mm)
        ys = _cluster_axis((y for _, y in self.sensors_mm), tol_mm)
        cols, rows = len(xs), len(ys)
        grid = [[-1] * cols for _ in range(rows)]
        for idx, (x, y) in enumerate(self.sensors_mm):
            c = min(range(cols), key=lambda i, x=x: abs(xs[i] - x))
            r = min(range(rows), key=lambda i, y=y: abs(ys[i] - y))
            grid[r][c] = idx
        return cols, rows, grid


def _cluster_axis(values, tol_mm: float) -> list[float]:
    """Collapse 1-D coordinates into sorted cluster centres: values within
    ``tol_mm`` of an existing cluster join it, so float drift / near-equal
    positions don't split a row or column into two."""
    clusters: list[float] = []
    for v in sorted(values):
        if clusters and abs(v - clusters[-1]) <= tol_mm:
            continue
        clusters.append(v)
    return clusters


def _grid_2x2(w: float, h: float) -> tuple[tuple[float, float], ...]:
    """Four sensors at the quadrant centres of a ``w×h`` box.
    Order S0=TL, S1=TR, S2=BL, S3=BR (firmware/QuadrantDetector convention)."""
    return ((w * 0.25, h * 0.25), (w * 0.75, h * 0.25),
            (w * 0.25, h * 0.75), (w * 0.75, h * 0.75))


# --- Registry -------------------------------------------------------------
# Add a new robot's skin by adding an entry here (and setting `skin_type` on
# its skins in settings.yaml). Nothing else needs code changes for the data.

SKIN_GEOMETRIES: dict[str, SkinGeometry] = {
    # Turtle — central square pad.
    "turtle_square": SkinGeometry(
        skin_type="turtle_square", shape="rect", size_mm=(125.0, 125.0),
        sensors_mm=_grid_2x2(125.0, 125.0), robot_kind="turtle",
        notes="Turtle central square, 4 sensors at quadrant centres.",
    ),
    # Turtle — lateral rectangles (left/right flanks). Only 2 sensors fit on the
    # narrow 75 mm width, stacked along the 125 mm length.
    "turtle_side": SkinGeometry(
        skin_type="turtle_side", shape="rect", size_mm=(75.0, 125.0),
        sensors_mm=((37.5, 41.67), (37.5, 83.33)), robot_kind="turtle",
        notes="Turtle side rectangle, 2 sensors at 1/3 and 2/3 of the length. "
              "TODO: confirm exact positions on the real build.",
    ),
    # Turtle — functional corner triangles (head/tail are aesthetic and are
    # NOT skins, so they are absent from this registry).
    "turtle_triangle": SkinGeometry(
        skin_type="turtle_triangle", shape="triangle", size_mm=(75.0, 75.0),
        sensors_mm=((37.5, 25.0), (20.0, 60.0), (55.0, 60.0), (37.5, 45.0)),
        robot_kind="turtle",
        notes="TODO: measure — functional corner triangle sensor positions.",
    ),
    # Tree — round branch skins, Ø99 mm. A single sensor at the centre (one
    # magnet per branch); no spatial position tracking, only touch reactions.
    "tree_round": SkinGeometry(
        skin_type="tree_round", shape="round", size_mm=(99.0, 99.0),
        sensors_mm=((49.5, 49.5),),
        robot_kind="tree",
        notes="Tree branch, Ø99 round, single central sensor.",
    ),
    # Thymio — 'D' (rotated +90°): semicircular bulge on top, flat bottom.
    # Square bounding box so it isn't stretched tall/narrow.
    "thymio": SkinGeometry(
        skin_type="thymio", shape="thymio", size_mm=(120.0, 120.0),
        # 2×2 within the box; top pair sits inside the upper semicircle.
        sensors_mm=((36.0, 66.0), (84.0, 66.0), (36.0, 100.0), (84.0, 100.0)),
        robot_kind="thymio",
        notes="TODO: measure — Thymio bulge-up 'D' sensor positions.",
    ),
}


def geometry_for(skin_type: str | None) -> SkinGeometry | None:
    """Return the registry geometry for a skin type, or None if unknown/empty."""
    if not skin_type:
        return None
    return SKIN_GEOMETRIES.get(skin_type)


def known_skin_types() -> list[str]:
    """All registered skin types (for GUI pickers)."""
    return sorted(SKIN_GEOMETRIES)


# --- Silicone variants ----------------------------------------------------
# Orthogonal to skin_type (the shape): the same shape is cast in several
# silicone formats with different chamber sizes. Referenced across the app
# (config, GUI, recordings) and fed to the touch ML as a feature.
SKIN_VARIANTS: tuple[str, ...] = ("natural", "wrinkles", "organ")


def known_skin_variants() -> list[str]:
    """All silicone variants (for GUI pickers / ML encoding)."""
    return list(SKIN_VARIANTS)


def skin_types_for(robot_kind: str | None) -> list[str]:
    """Skin types belonging to a robot kind ("turtle"/"tree"/"thymio").

    Used by the GUI so configuring a robot only offers its own skin types.
    Empty/unknown ``robot_kind`` returns all types."""
    if not robot_kind:
        return known_skin_types()
    return sorted(t for t, g in SKIN_GEOMETRIES.items()
                  if g.robot_kind == robot_kind)
