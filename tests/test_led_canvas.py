"""Tests for the per-unit LED zone canvas the fill blocks paint into."""

from src.activities.led_canvas import (FILL_CENTRE, FILL_CONTIGUOUS,
                                       FILL_RANDOM, LedZoneCanvas)
from src.core.led_geometry import LedStripGeometry, decode_pixel_mask
from src.core.touch_zones import TouchZoneMap, quadrant_placements


def _canvas(seed: int = 1, fill: str = FILL_RANDOM) -> LedZoneCanvas:
    strip = LedStripGeometry(count=68, gap=1)
    canvas = LedZoneCanvas(TouchZoneMap(strip, quadrant_placements(4)), seed=seed)
    canvas.reset(fill)
    return canvas


def _lit(canvas: LedZoneCanvas) -> set[int]:
    return {i for i, c in enumerate(canvas.codes()) if c}


class TestZoneFill:
    def test_starts_dark_and_fills_only_the_touched_zone(self):
        canvas = _canvas()
        assert not _lit(canvas)
        canvas.fill_zone(2, 3)
        lit = _lit(canvas)
        assert len(lit) == 3
        assert lit <= set(canvas.zone_map.zone_pixels(2))
        assert canvas.zone_lit(2) == 3 and canvas.zone_lit(0) == 0

    def test_growth_is_incremental_and_capped(self):
        canvas = _canvas()
        canvas.fill_zone(0, 5)
        first = _lit(canvas)
        canvas.fill_zone(0, 5)
        assert first < _lit(canvas)
        canvas.fill_zone(0, 1000)
        assert canvas.zone_full(0)
        assert canvas.zone_lit(0) == canvas.zone_size(0)
        assert not canvas.all_full()

    def test_fraction_step_rounds_up_and_always_lights_something(self):
        import math
        canvas = _canvas()
        size = canvas.zone_size(1)
        quarter = math.ceil(size * 0.25)
        assert canvas.fill_zone_fraction(1, 0.25) == quarter
        assert canvas.fill_zone_fraction(1, 0.001) == quarter + 1
        assert canvas.fill_zone_fraction(1, 0.0) == quarter + 1

    def test_n_steps_of_one_nth_fill_the_zone(self):
        for pct in (0.5, 0.25, 0.2, 1 / 3):
            canvas = _canvas()
            steps = round(1 / pct)
            for _ in range(steps - 1):
                canvas.fill_zone_fraction(0, pct)
                assert not canvas.zone_full(0)
            canvas.fill_zone_fraction(0, pct)
            assert canvas.zone_full(0)

    def test_all_full_after_every_zone(self):
        canvas = _canvas()
        for zone in canvas.zone_map.zone_ids:
            canvas.fill_zone_fraction(zone, 1.0)
        assert canvas.all_full()
        canvas.unfill_zone(3, 2)
        assert not canvas.all_full()

    def test_random_order_is_seeded_and_reset_redraws(self):
        a, b = _canvas(seed=7), _canvas(seed=7)
        a.fill_zone(0, 6); b.fill_zone(0, 6)
        assert _lit(a) == _lit(b)
        a.reset()
        assert not _lit(a)

    def test_contiguous_and_centre_orders(self):
        canvas = _canvas(fill=FILL_CONTIGUOUS)
        canvas.fill_zone(1, 4)
        assert sorted(_lit(canvas)) == canvas.zone_map.zone_pixels(1)[:4]
        canvas = _canvas(fill=FILL_CENTRE)
        canvas.fill_zone(1, 1)
        assert _lit(canvas) == {canvas.zone_map.zone_centre_pixel(1)}

    def test_set_fill_before_first_pixel_redraws_after_it_does_not(self):
        canvas = _canvas(fill=FILL_RANDOM)
        canvas.set_fill(FILL_CONTIGUOUS)
        assert canvas.fill == FILL_CONTIGUOUS
        canvas.fill_zone(0, 2)
        assert sorted(_lit(canvas)) == canvas.zone_map.zone_pixels(0)[:2]
        canvas.set_fill(FILL_RANDOM)            # lit pixels: order kept
        canvas.fill_zone(0, 1)
        assert sorted(_lit(canvas)) == canvas.zone_map.zone_pixels(0)[:3]
        canvas.set_fill("bogus")
        assert canvas.fill == FILL_RANDOM


class TestTotalFill:
    def test_total_grows_and_shrinks_in_one_order(self):
        canvas = _canvas(fill=FILL_CONTIGUOUS)
        assert canvas.total_size == 68
        assert canvas.set_total_fraction(0.5) == 34
        assert _lit(canvas) == set(range(34))
        canvas.set_total_lit(10)
        assert _lit(canvas) == set(range(10))
        assert canvas.set_total_lit(-5) == 0 and canvas.set_total_lit(999) == 68

    def test_total_and_zone_fills_combine(self):
        canvas = _canvas(fill=FILL_CONTIGUOUS)
        canvas.set_total_lit(2)
        canvas.fill_zone(3, 2)
        assert len(_lit(canvas)) == 4


class TestFrame:
    def test_frame_is_two_colours_plus_mask(self):
        canvas = _canvas(fill=FILL_CONTIGUOUS)
        canvas.fill_zone(0, 3)
        colors, mask = canvas.frame("#00ff00", "#101010")
        assert colors == ["#101010", "#00ff00"]
        assert len(mask) == 34
        codes = decode_pixel_mask(mask, 68)
        assert sum(codes) == 3
        assert [i for i, c in enumerate(codes) if c] == canvas.zone_map.zone_pixels(0)[:3]


class TestHoldFeedback:
    def test_inactive_hold_renders_two_colours(self):
        from src.activities.led_canvas import HoldFeedback
        canvas = _canvas(fill=FILL_CONTIGUOUS)
        canvas.fill_zone(1, 3)
        plain = canvas.frame("#f1c40f", "#8e44ad")
        assert canvas.frame("#f1c40f", "#8e44ad", HoldFeedback()) == plain
        assert canvas.frame("#f1c40f", "#8e44ad",
                            HoldFeedback(frozenset({1}), "glow", 0.0)) == plain
        assert len(plain[0]) == 2

    def test_glow_tints_only_the_held_zones_lit_pixels(self):
        from src.activities.led_canvas import HOLD_CODE, HoldFeedback, ON_CODE
        canvas = _canvas(fill=FILL_CONTIGUOUS)
        canvas.fill_zone(1, 3)
        canvas.fill_zone(2, 3)
        hold = HoldFeedback(frozenset({1}), "glow", 0.5)
        codes = canvas.codes(hold)
        zone1 = canvas.zone_map.zone_pixels(1)
        assert [p for p, c in enumerate(codes) if c == HOLD_CODE] == zone1[:3]
        assert all(codes[p] == ON_CODE for p in canvas.zone_map.zone_pixels(2)[:3])
        colors, _mask = canvas.frame("#000000", "#8e44ad", hold)
        assert colors == ["#8e44ad", "#000000", "#808080"]     # half way to white

    def test_dim_tints_only_the_held_zones_unlit_pixels(self):
        from src.activities.led_canvas import BG_CODE, HOLD_CODE, HoldFeedback
        canvas = _canvas(fill=FILL_CONTIGUOUS)
        canvas.fill_zone(1, 3)
        hold = HoldFeedback(frozenset({1}), "dim", 1.0)
        codes = canvas.codes(hold)
        zone1 = canvas.zone_map.zone_pixels(1)
        assert [p for p, c in enumerate(codes) if c == HOLD_CODE] == zone1[3:]
        assert all(codes[p] == BG_CODE for p in canvas.zone_map.zone_pixels(0))
        colors, _mask = canvas.frame("#f1c40f", "#8e44ad", hold)
        assert colors[2] == "#000000"

    def test_mix_hex_blends_and_clamps(self):
        from src.activities.led_canvas import mix_hex
        assert mix_hex("#000000", "#ffffff", 0.0) == "#000000"
        assert mix_hex("#000000", "#ffffff", 1.0) == "#ffffff"
        assert mix_hex("#000000", "#ffffff", 2.0) == "#ffffff"
        assert mix_hex("#fff", "#000", 0.5) == "#808080"
        assert mix_hex("junk", "#ffffff", 0.5) == "#808080"
