"""A per-unit model of the LED strip the zone-fill blocks paint into.

The behaviour engine keeps one :class:`LedZoneCanvas` per unit (skin). Blocks
mutate it - "light three more pixels of the zone that was just touched",
"light this fraction of the whole strip" - and the engine renders it as ONE
``set_led_pixels`` frame (a colour list plus a per-pixel mask), so a 68-pixel
strip never costs more than one ESP-NOW frame per change.

The canvas is plain state: it knows the zone map (which pixels belong to which
touch sensor) and which pixels are lit, and in which order each zone lights
up. Order is decided once per phase (``reset``) so growth is incremental -
each new step adds pixels without reshuffling the ones already lit - and a
seeded ``random.Random`` keeps tests deterministic.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Iterable

from src.core.led_geometry import encode_pixel_mask
from src.core.touch_zones import TouchZoneMap

# How a zone (or the whole strip) fills up as steps are added.
FILL_RANDOM = "random"         # pixels in a shuffled order
FILL_CONTIGUOUS = "contiguous" # in strip order from the zone's first pixel
FILL_CENTRE = "centre"         # outward from the sensor's own pixel
FILL_MODES = (FILL_RANDOM, FILL_CONTIGUOUS, FILL_CENTRE)

# Colour index of an unlit pixel in the rendered mask.
BG_CODE = 0
ON_CODE = 1
# Colour index of a pixel tinted by the live hold feedback (see HoldFeedback).
HOLD_CODE = 2

# Live feedback while a sensor is being held, scaled by how hard it is pressed:
# "glow" brightens the held zone's LIT pixels towards white, "dim" fades the
# held zone's still-UNLIT pixels towards black. "none" shows nothing extra.
HOLD_NONE = "none"
HOLD_GLOW = "glow"
HOLD_DIM = "dim"
HOLD_MODES = (HOLD_NONE, HOLD_GLOW, HOLD_DIM)
_HOLD_TARGET = {HOLD_GLOW: "#ffffff", HOLD_DIM: "#000000"}


def mix_hex(color_a: str, color_b: str, t: float) -> str:
    """Linear blend of two ``#RRGGBB`` colours (``t`` 0 = a, 1 = b). A colour
    that does not parse is treated as black."""
    def channels(value: str) -> tuple[int, int, int]:
        text = str(value).strip().lstrip("#")
        try:
            if len(text) == 3:
                text = "".join(ch * 2 for ch in text)
            return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
        except (TypeError, ValueError, IndexError):
            return 0, 0, 0
    t = max(0.0, min(1.0, float(t)))
    a, b = channels(color_a), channels(color_b)
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


@dataclass(frozen=True)
class HoldFeedback:
    """What the strip shows while sensors are held down: which zones, in
    which mode, and how strongly (``level`` 0..1 from the press strength).
    A value object the engine rebuilds per sensor frame and the canvas
    renders as a third mask colour on top of the lit/unlit state."""
    zones: frozenset[int] = frozenset()
    mode: str = HOLD_NONE
    level: float = 0.0

    @property
    def active(self) -> bool:
        return bool(self.zones) and self.mode in _HOLD_TARGET and self.level > 0

    def tint(self, on_color: str, bg_color: str) -> str:
        """The colour of a tinted pixel: the lit colour pushed towards white
        (glow) or the unlit colour pushed towards black (dim) by ``level``."""
        base = on_color if self.mode == HOLD_GLOW else bg_color
        return mix_hex(base, _HOLD_TARGET.get(self.mode, base), self.level)

    def tints_lit(self) -> bool:
        return self.mode == HOLD_GLOW


class LedZoneCanvas:
    """Lit-pixel state of one strip, organised by touch zone."""

    def __init__(self, zone_map: TouchZoneMap, seed: int | None = None) -> None:
        self._map = zone_map
        self._rng = random.Random(seed)
        self._fill = FILL_RANDOM
        self._order: dict[int, list[int]] = {}
        self._lit: dict[int, list[int]] = {}
        self._total_order: list[int] = []
        self._total_lit: int = 0
        self.reset()

    # -- lifecycle ------------------------------------------------------------

    def reset(self, fill: str | None = None) -> None:
        """Clear every pixel and (re)draw the fill orders. Called on phase
        entry so each phase starts dark and with a fresh random order."""
        if fill is not None:
            self.set_fill(fill)
        self._order = {z: self._ordered(z) for z in self._map.zone_ids}
        self._lit = {z: [] for z in self._map.zone_ids}
        self._total_order = self._ordered_total()
        self._total_lit = 0

    def set_fill(self, fill: str) -> None:
        """Choose the fill mode; unknown values keep the current one. While
        nothing is lit yet the orders are redrawn at once, so a block's fill
        choice applies from the phase's first press; once pixels are lit the
        new mode only takes effect at the next ``reset``."""
        if fill not in FILL_MODES or fill == self._fill:
            return
        self._fill = fill
        if self._total_lit == 0 and not any(self._lit.values()):
            self.reset()

    @property
    def fill(self) -> str:
        return self._fill

    @property
    def zone_map(self) -> TouchZoneMap:
        return self._map

    # -- per-zone fill --------------------------------------------------------

    def zone_size(self, zone: int) -> int:
        return len(self._order.get(int(zone), []))

    def zone_lit(self, zone: int) -> int:
        return len(self._lit.get(int(zone), []))

    def zone_full(self, zone: int) -> bool:
        size = self.zone_size(zone)
        return size > 0 and self.zone_lit(zone) >= size

    def all_full(self) -> bool:
        return bool(self._order) and all(self.zone_full(z) for z in self._order)

    def fill_zone(self, zone: int, pixels: int) -> int:
        """Light the next ``pixels`` of ``zone``. Returns how many lit now."""
        zone = int(zone)
        order = self._order.get(zone)
        if order is None:
            return 0
        lit = self._lit[zone]
        take = max(0, min(int(pixels), len(order) - len(lit)))
        lit.extend(order[len(lit):len(lit) + take])
        return len(lit)

    def fill_zone_fraction(self, zone: int, fraction: float) -> int:
        """Light a fraction of the zone in one step: at least one pixel, and
        rounded UP so that N steps of 1/N always fill the zone (a 17-pixel
        quarter takes two 50 % steps, not three)."""
        size = self.zone_size(zone)
        if size <= 0 or fraction <= 0:
            return self.zone_lit(zone)
        pixels = math.ceil(size * float(fraction) - 1e-9)
        return self.fill_zone(zone, max(1, pixels))

    def unfill_zone(self, zone: int, pixels: int) -> int:
        """Turn off the most recently lit ``pixels`` of ``zone``."""
        zone = int(zone)
        lit = self._lit.get(zone)
        if lit is None:
            return 0
        drop = max(0, min(int(pixels), len(lit)))
        if drop:
            del lit[len(lit) - drop:]
        return len(lit)

    # -- whole-strip fill (group progress) ------------------------------------

    @property
    def total_size(self) -> int:
        return len(self._total_order)

    @property
    def total_lit(self) -> int:
        return self._total_lit

    def set_total_lit(self, pixels: int) -> int:
        """Light exactly the first ``pixels`` of the whole-strip order
        (growth keeps what is lit; shrink drops the most recent)."""
        self._total_lit = max(0, min(int(pixels), self.total_size))
        return self._total_lit

    def set_total_fraction(self, fraction: float) -> int:
        return self.set_total_lit(round(self.total_size * max(0.0, min(1.0, float(fraction)))))

    # -- rendering ------------------------------------------------------------

    def codes(self, hold: HoldFeedback | None = None) -> list[int]:
        """Per-pixel colour index: ON where lit by either mode, else BG. With
        an active ``hold``, the held zones' lit (glow) or unlit (dim) pixels
        become HOLD instead."""
        on = set(self._total_order[:self._total_lit])
        for lit in self._lit.values():
            on.update(lit)
        held: set[int] = set()
        if hold is not None and hold.active:
            held = set(self._held_pixels(hold.zones))
        tint_lit = hold.tints_lit() if hold is not None else False
        out: list[int] = []
        for p in self._map.all_pixels():
            is_on = p in on
            if p in held and is_on == tint_lit:
                out.append(HOLD_CODE)
            else:
                out.append(ON_CODE if is_on else BG_CODE)
        return out

    def frame(self, on_color: str, bg_color: str,
              hold: HoldFeedback | None = None) -> tuple[list[str], str]:
        """The ``set_led_pixels`` payload: ``(colors, mask)``. The third
        colour is only present while a hold is active."""
        colors = [str(bg_color), str(on_color)]
        if hold is not None and hold.active:
            colors.append(hold.tint(str(on_color), str(bg_color)))
        return colors, encode_pixel_mask(self.codes(hold))

    def _held_pixels(self, zones: Iterable[int]) -> list[int]:
        pixels: list[int] = []
        for zone in zones:
            pixels.extend(self._map.zone_pixels(int(zone)))
        return pixels

    def snapshot(self) -> dict[str, Any]:
        """Debug/inspection view of the state."""
        return {"fill": self._fill,
                "zones": {z: (len(l), len(self._order[z]))
                          for z, l in self._lit.items()},
                "total": (self._total_lit, self.total_size)}

    # -- orders ---------------------------------------------------------------

    def _ordered(self, zone: int) -> list[int]:
        if self._fill == FILL_CENTRE:
            return self._map.ordered_from_centre(zone)
        pixels = self._map.zone_pixels(zone)
        if self._fill == FILL_RANDOM:
            self._rng.shuffle(pixels)
        return pixels

    def _ordered_total(self) -> list[int]:
        pixels = self._map.all_pixels()
        if self._fill == FILL_RANDOM:
            self._rng.shuffle(pixels)
        return pixels
