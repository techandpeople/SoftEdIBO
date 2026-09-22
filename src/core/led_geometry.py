"""LED strip geometry - where each pixel sits along a skin's perimeter.

Pure, Qt-free. The node_direct board no longer drives a NeoPixel ring but a
cut length of WS2812B COB strip wrapped once around the skin: 68 pixels with
one empty slot at the seam where the two ends meet, so the light reads as a
closed loop of 69 equal *slots* of which the last is dark. Ring boards (the
multiplexed PCB's 24-LED rings) are the gap-less special case.

Positions are expressed as a **perimeter fraction** (0..1) rather than degrees:
a strip wrapped around a square has pixels at constant *arc length*, so equal
fractions of the perimeter hold equal numbers of pixels, which equal angles
would not. 0 is the front centre of the skin and the fraction grows clockwise
seen from above - the same convention the touch-sensor placements use (see
:mod:`src.core.touch_zones`), which is what lets the two be joined.

The strip's mounting is described by the skin's saved ``led_angles`` (the
angle the Test Actuators handle sets, in degrees): the firmware rotates a
split so that arc 0 starts at pixel ``round(angle / 360 * slots)``, i.e. that
pixel is at perimeter position 0, so pixel 0 sits at ``-angle / 360``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# Perimeter-fraction position of the front centre (the reference point).
FRONT = 0.0

# Highest pixel brightness the firmware accepts (Adafruit_NeoPixel scale).
MAX_BRIGHTNESS = 255

# Per-skin-type default strip layout. The Thymio skin cover carries the cut
# COB strip (68 pixels + 1 empty seam slot); the Turtle and Tree skins sit on
# the multiplexed board's 24-LED rings (no seam). A skin's saved ``led_layout``
# overrides any of these fields.
DEFAULT_LED_LAYOUTS: dict[str, dict[str, Any]] = {
    "thymio":          {"count": 68, "gap": 1},
    "turtle_square":   {"count": 24, "gap": 0},
    "turtle_side":     {"count": 24, "gap": 0},
    "turtle_triangle": {"count": 24, "gap": 0},
    "tree_round":      {"count": 24, "gap": 0},
}


@dataclass(frozen=True)
class LedStripGeometry:
    """An addressable strip closed into a loop around a skin.

    Args:
        count: Physical pixels on the wire (what the firmware drives).
        gap: Empty slots at the seam after the last pixel (0 for a true ring).
        start: Perimeter fraction (0..1) where pixel 0 sits.
        clockwise: True when pixel indices grow clockwise seen from above.
        brightness: Firmware brightness cap, 1..255.
        ring: Which ring of a multi-ring node this strip is (0 on single-ring
            boards) - the ``ring`` field LED commands carry.
    """

    count: int
    gap: int = 0
    start: float = FRONT
    clockwise: bool = True
    brightness: int = MAX_BRIGHTNESS
    ring: int = 0

    def __post_init__(self) -> None:
        if self.count < 1:
            raise ValueError("a strip needs at least one pixel")
        if self.gap < 0:
            raise ValueError("gap cannot be negative")
        if not 1 <= self.brightness <= MAX_BRIGHTNESS:
            raise ValueError("brightness must be 1..255")

    @property
    def slots(self) -> int:
        """Geometric positions around the loop: pixels plus the seam gap."""
        return self.count + self.gap

    @property
    def pixels(self) -> range:
        return range(self.count)

    def position_of(self, pixel: int) -> float:
        """Perimeter fraction (0..1) of ``pixel`` (any int; wraps)."""
        step = (pixel % self.slots) / self.slots
        frac = self.start + (step if self.clockwise else -step)
        return frac % 1.0

    def pixels_in_sector(self, centre: float, width: float) -> list[int]:
        """Pixels whose position lies within ``width/2`` of ``centre`` (both
        perimeter fractions). Returned in pixel order."""
        half = max(0.0, float(width)) / 2.0
        return [p for p in self.pixels
                if circular_distance(self.position_of(p), centre) <= half]

    def nearest_pixel(self, position: float) -> int:
        """The pixel closest to a perimeter position."""
        return min(self.pixels,
                   key=lambda p: circular_distance(self.position_of(p), position))

    @property
    def mounting_angle_deg(self) -> float:
        """The ``led_angles`` value (degrees) this geometry's ``start`` encodes."""
        return (-self.start * 360.0) % 360.0

    def to_dict(self) -> dict[str, Any]:
        """The saved ``led_layout`` shape (``start`` lives in ``led_angles``)."""
        return {"count": self.count, "gap": self.gap,
                "clockwise": self.clockwise, "brightness": self.brightness,
                "ring": self.ring}


def circular_distance(a: float, b: float) -> float:
    """Shortest distance between two perimeter fractions (0..0.5)."""
    d = abs((a - b) % 1.0)
    return min(d, 1.0 - d)


def start_from_angle(angle_deg: float, clockwise: bool = True) -> float:
    """Perimeter fraction of pixel 0 given the strip's mounting angle.

    The firmware rotates a split by ``round(angle/360 * slots)`` pixels, so the
    pixel at index ``angle/360 * slots`` sits at position 0 and pixel 0 sits
    at ``-angle/360`` (mirrored for a counter-clockwise strip)."""
    frac = (float(angle_deg) / 360.0) % 1.0
    return (-frac if clockwise else frac) % 1.0


def led_geometry_for(skin_cfg: Mapping[str, Any],
                     defaults: Mapping[str, Mapping[str, Any]] | None = None
                     ) -> LedStripGeometry | None:
    """Build a skin's strip geometry from its saved config.

    ``skin_cfg["led_layout"]`` (count / gap / clockwise / brightness / ring)
    overlays the ``skin_type`` default from :data:`DEFAULT_LED_LAYOUTS`; the
    mounting angle comes from ``skin_cfg["led_angles"][ring]``. Returns
    ``None`` when neither a saved layout nor a type default exists - such a
    skin has no LED strip the zone logic can address."""
    table = DEFAULT_LED_LAYOUTS if defaults is None else defaults
    base = dict(table.get(str(skin_cfg.get("skin_type") or ""), {}))
    saved = skin_cfg.get("led_layout")
    if isinstance(saved, Mapping):
        base.update({k: v for k, v in saved.items() if v is not None})
    if not base.get("count"):
        return None
    ring = int(base.get("ring", 0) or 0)
    clockwise = bool(base.get("clockwise", True))
    angles = skin_cfg.get("led_angles") or {}
    angle = 0.0
    if isinstance(angles, Mapping):
        try:
            angle = float(angles.get(str(ring), angles.get(ring, 0.0)) or 0.0)
        except (TypeError, ValueError):
            angle = 0.0
    try:
        return LedStripGeometry(
            count=int(base["count"]), gap=int(base.get("gap", 0) or 0),
            start=start_from_angle(angle, clockwise), clockwise=clockwise,
            brightness=int(base.get("brightness", MAX_BRIGHTNESS)
                           or MAX_BRIGHTNESS),
            ring=ring)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Wire encoding of a per-pixel colour selection ("set_led_pixels" mask)
# ---------------------------------------------------------------------------
# One frame carries up to four colours and a 2-bit colour index per pixel,
# packed two pixels per hex character: pixel i's index is bits (i % 2) * 2 of
# character i // 2. 68 pixels = 34 characters, well under the ESP-NOW budget.

MAX_MASK_COLORS = 4


def encode_pixel_mask(codes: list[int]) -> str:
    """Pack per-pixel colour indices (0..3) into the hex mask string."""
    out: list[str] = []
    for i in range(0, len(codes), 2):
        lo = int(codes[i]) & 3
        hi = (int(codes[i + 1]) & 3) if i + 1 < len(codes) else 0
        out.append(format(lo | (hi << 2), "x"))
    return "".join(out)


def decode_pixel_mask(mask: str, count: int) -> list[int]:
    """Unpack a hex mask into ``count`` colour indices (missing = 0)."""
    codes: list[int] = []
    for i in range(count):
        char = mask[i // 2] if i // 2 < len(mask) else "0"
        try:
            value = int(char, 16)
        except ValueError:
            value = 0
        codes.append((value >> ((i % 2) * 2)) & 3)
    return codes
