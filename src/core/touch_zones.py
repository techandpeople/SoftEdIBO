"""Touch zones - joining where the sensors are with where the LEDs are.

Pure, Qt-free. Two independent descriptions meet here:

* the **LED strip** knows the perimeter position of every pixel
  (:class:`~src.core.led_geometry.LedStripGeometry`);
* the **touch sensor profile** knows the perimeter position of every sensor
  (:class:`SensorPlacement`, one per sensor index; a magnet board reports its
  four corner quadrants, a future capacitive board its pad positions).

Neither knows about the other. :class:`TouchZoneMap` assigns each pixel to the
nearest sensor along the perimeter, so a touch on sensor *i* has a well-defined
arc of pixels above it - on a symmetric skin with corner sensors, four equal
quarters - without any hand-maintained sensor->LED table.

Positions are perimeter fractions: 0 = front centre, growing clockwise seen
from above (see :mod:`src.core.led_geometry`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from src.core.led_geometry import LedStripGeometry, circular_distance

# The four corner quadrants of a rectangular skin, named as the thesis quadrant
# detector does (Q1 top-left, Q2 top-right, Q3 bottom-left, Q4 bottom-right,
# looking down on the skin with the front at the top), at their perimeter
# positions. Clockwise from the front centre: top-right corner is 1/8 of the
# way round, bottom-right 3/8, bottom-left 5/8, top-left 7/8.
QUADRANT_POSITIONS: dict[str, float] = {
    "Q1": 0.875,   # top-left
    "Q2": 0.125,   # top-right
    "Q3": 0.625,   # bottom-left
    "Q4": 0.375,   # bottom-right
}
QUADRANT_NAMES: tuple[str, ...] = ("Q1", "Q2", "Q3", "Q4")
QUADRANT_LABELS: dict[str, str] = {
    "Q1": "top-left", "Q2": "top-right",
    "Q3": "bottom-left", "Q4": "bottom-right",
}


@dataclass(frozen=True)
class SensorPlacement:
    """Where one touch sensor sits along the skin perimeter."""
    index: int
    position: float          # perimeter fraction 0..1

    def __post_init__(self) -> None:
        object.__setattr__(self, "position", float(self.position) % 1.0)


def evenly_spaced_placements(count: int, offset: float | None = None
                             ) -> list[SensorPlacement]:
    """``count`` sensors equally spaced round the perimeter.

    With no ``offset`` the first sits half a step past the front, so four
    sensors land on the four corners (1/8, 3/8, 5/8, 7/8)."""
    n = max(1, int(count))
    first = (0.5 / n) if offset is None else float(offset)
    return [SensorPlacement(i, first + i / n) for i in range(n)]


def quadrant_placements(count: int,
                        sensor_quadrants: Mapping[Any, Any] | None = None
                        ) -> list[SensorPlacement]:
    """Placements for a four-sensor magnet board from its quadrant assignment.

    ``sensor_quadrants`` maps sensor index -> quadrant name (``"Q1"``..``"Q4"``,
    keys may be str or int). Sensor ``i`` defaults to ``Q{i+1}``. Boards with
    another sensor count fall back to equal spacing."""
    n = max(1, int(count))
    if n != len(QUADRANT_NAMES):
        return evenly_spaced_placements(n)
    assigned: dict[int, str] = {}
    for key, value in (sensor_quadrants or {}).items():
        try:
            idx = int(key)
        except (TypeError, ValueError):
            continue
        name = str(value).strip().upper()
        if 0 <= idx < n and name in QUADRANT_POSITIONS:
            assigned[idx] = name
    out: list[SensorPlacement] = []
    for i in range(n):
        name = assigned.get(i, QUADRANT_NAMES[i])
        out.append(SensorPlacement(i, QUADRANT_POSITIONS[name]))
    return out


def normalise_sensor_quadrants(value: Any, count: int) -> dict[str, str]:
    """A saved ``sensor_quadrants`` map, cleaned: str keys, valid names,
    only for a four-sensor board and only entries that differ from the
    identity default (so the YAML stays terse). Empty dict = default."""
    if int(count) != len(QUADRANT_NAMES) or not isinstance(value, Mapping):
        return {}
    out: dict[str, str] = {}
    for key, name in value.items():
        try:
            idx = int(key)
        except (TypeError, ValueError):
            continue
        name = str(name).strip().upper()
        if 0 <= idx < count and name in QUADRANT_POSITIONS \
                and name != QUADRANT_NAMES[idx]:
            out[str(idx)] = name
    return out


class TouchZoneMap:
    """Pixels of a strip grouped by the touch sensor nearest each one.

    Built once per skin from the strip geometry and the sensor placements.
    Zone *k* is the pixel set of sensor index *k*; pixels are listed in
    strip order. Ties go to the lower sensor index.
    """

    def __init__(self, strip: LedStripGeometry,
                 placements: Sequence[SensorPlacement]) -> None:
        if not placements:
            raise ValueError("a zone map needs at least one sensor placement")
        self._strip = strip
        self._placements = tuple(sorted(placements, key=lambda p: p.index))
        self._zones: dict[int, list[int]] = {p.index: [] for p in self._placements}
        for pixel in strip.pixels:
            pos = strip.position_of(pixel)
            nearest = min(self._placements,
                          key=lambda p: (circular_distance(pos, p.position),
                                         p.index))
            self._zones[nearest.index].append(pixel)

    @property
    def strip(self) -> LedStripGeometry:
        return self._strip

    @property
    def placements(self) -> tuple[SensorPlacement, ...]:
        return self._placements

    @property
    def zone_ids(self) -> list[int]:
        """Sensor indices with a zone, ascending."""
        return [p.index for p in self._placements]

    def zone_pixels(self, sensor_index: int) -> list[int]:
        """Pixels above ``sensor_index`` (empty for an unknown sensor)."""
        return list(self._zones.get(int(sensor_index), []))

    def zone_of_pixel(self, pixel: int) -> int | None:
        for idx, pixels in self._zones.items():
            if pixel in pixels:
                return idx
        return None

    def all_pixels(self) -> list[int]:
        return list(self._strip.pixels)

    def zone_centre_pixel(self, sensor_index: int) -> int | None:
        """The pixel nearest the sensor itself, or None for an unknown sensor."""
        placement = next((p for p in self._placements
                          if p.index == int(sensor_index)), None)
        if placement is None:
            return None
        return self._strip.nearest_pixel(placement.position)

    def ordered_from_centre(self, sensor_index: int) -> list[int]:
        """The zone's pixels ordered outward from the sensor's pixel,
        alternating sides - a fill that grows from where the touch is."""
        pixels = self.zone_pixels(sensor_index)
        centre = self.zone_centre_pixel(sensor_index)
        if centre is None or not pixels:
            return pixels
        cpos = self._strip.position_of(centre)
        return sorted(pixels, key=lambda p: (
            circular_distance(self._strip.position_of(p), cpos), p))

    def describe(self) -> dict[int, tuple[int, int]]:
        """Zone -> (first pixel, count) summary, handy for logs/tests."""
        return {k: (v[0] if v else -1, len(v)) for k, v in self._zones.items()}

    @staticmethod
    def placements_from(iterable: Iterable[tuple[int, float]]
                        ) -> list[SensorPlacement]:
        return [SensorPlacement(i, pos) for i, pos in iterable]
