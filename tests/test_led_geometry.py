"""Tests for the LED strip geometry and the touch zone map (pure core)."""

import pytest

from src.core.led_geometry import (DEFAULT_LED_LAYOUTS, LedStripGeometry,
                                   circular_distance, decode_pixel_mask,
                                   encode_pixel_mask, led_geometry_for,
                                   start_from_angle)
from src.core.touch_zones import (QUADRANT_POSITIONS, SensorPlacement,
                                  TouchZoneMap, evenly_spaced_placements,
                                  normalise_sensor_quadrants,
                                  quadrant_placements)


# ---------------------------------------------------------------------------
# LedStripGeometry
# ---------------------------------------------------------------------------

class TestStripGeometry:
    def test_thymio_strip_has_69_slots_for_68_pixels(self):
        strip = LedStripGeometry(count=68, gap=1)
        assert strip.slots == 69
        assert list(strip.pixels) == list(range(68))
        # The seam slot (68) is the only position no pixel occupies: pixel 0
        # and pixel 67 are two steps apart around the loop, not one.
        gap = circular_distance(strip.position_of(67), strip.position_of(0))
        assert gap == pytest.approx(2 / 69)

    def test_ring_without_gap_wraps_exactly(self):
        ring = LedStripGeometry(count=24)
        assert ring.slots == 24
        assert ring.position_of(24) == pytest.approx(ring.position_of(0))
        assert ring.position_of(6) == pytest.approx(0.25)

    def test_start_and_direction(self):
        cw = LedStripGeometry(count=8, start=0.25)
        assert cw.position_of(0) == pytest.approx(0.25)
        assert cw.position_of(2) == pytest.approx(0.5)
        ccw = LedStripGeometry(count=8, start=0.25, clockwise=False)
        assert ccw.position_of(2) == pytest.approx(0.0)

    def test_mounting_angle_round_trips_through_start(self):
        for angle in (0.0, 45.0, 90.0, 270.0, 359.0):
            strip = LedStripGeometry(count=68, gap=1,
                                     start=start_from_angle(angle))
            assert strip.mounting_angle_deg == pytest.approx(angle % 360)

    def test_angle_puts_the_rotated_pixel_at_the_front(self):
        # The firmware rotates a split by round(angle/360*slots) pixels, so
        # that pixel is where arc 0 begins = perimeter position 0.
        strip = LedStripGeometry(count=68, gap=1, start=start_from_angle(90.0))
        front = round(90 / 360 * 69)
        assert strip.nearest_pixel(0.0) == front
        assert circular_distance(strip.position_of(front), 0.0) < 1 / 69

    def test_sector_selection(self):
        strip = LedStripGeometry(count=8)
        # Quarter centred on the front: pixels at 0, 1/8 and 7/8.
        assert strip.pixels_in_sector(0.0, 0.25) == [0, 1, 7]
        assert strip.pixels_in_sector(0.5, 0.0) == [4]

    def test_validation(self):
        with pytest.raises(ValueError):
            LedStripGeometry(count=0)
        with pytest.raises(ValueError):
            LedStripGeometry(count=8, gap=-1)
        with pytest.raises(ValueError):
            LedStripGeometry(count=8, brightness=0)


class TestGeometryFromConfig:
    def test_thymio_default_from_skin_type(self):
        geo = led_geometry_for({"skin_type": "thymio"})
        assert geo is not None
        assert (geo.count, geo.gap, geo.ring) == (68, 1, 0)
        assert geo.brightness == 255 and geo.clockwise

    def test_saved_layout_overrides_default_and_angle_sets_start(self):
        geo = led_geometry_for({"skin_type": "thymio",
                                "led_layout": {"count": 60, "gap": 2,
                                               "brightness": 120,
                                               "clockwise": False},
                                "led_angles": {"0": 180.0}})
        assert geo is not None
        assert (geo.count, geo.gap, geo.brightness, geo.clockwise) == (60, 2, 120, False)
        assert geo.start == pytest.approx(start_from_angle(180.0, clockwise=False))

    def test_ring_selects_its_own_angle(self):
        geo = led_geometry_for({"skin_type": "tree_round",
                                "led_layout": {"ring": 2},
                                "led_angles": {"0": 90.0, "2": 45.0}})
        assert geo is not None
        assert geo.ring == 2
        assert geo.mounting_angle_deg == pytest.approx(45.0)

    def test_unknown_type_without_layout_has_no_strip(self):
        assert led_geometry_for({"skin_type": "mystery"}) is None
        assert led_geometry_for({}) is None
        geo = led_geometry_for({"led_layout": {"count": 10}})
        assert geo is not None and geo.count == 10

    def test_every_default_layout_builds(self):
        for skin_type in DEFAULT_LED_LAYOUTS:
            assert led_geometry_for({"skin_type": skin_type}) is not None


# ---------------------------------------------------------------------------
# Pixel mask wire encoding
# ---------------------------------------------------------------------------

class TestPixelMask:
    def test_round_trip(self):
        codes = [(i * 7) % 4 for i in range(68)]
        mask = encode_pixel_mask(codes)
        assert len(mask) == 34
        assert decode_pixel_mask(mask, 68) == codes

    def test_odd_length_and_short_mask(self):
        assert decode_pixel_mask(encode_pixel_mask([1, 2, 3]), 3) == [1, 2, 3]
        # Pixels past the mask read as background (0).
        assert decode_pixel_mask("f", 4) == [3, 3, 0, 0]
        assert decode_pixel_mask("zz", 2) == [0, 0]

    def test_two_pixels_per_character(self):
        assert encode_pixel_mask([1, 0]) == "1"
        assert encode_pixel_mask([0, 1]) == "4"
        assert encode_pixel_mask([3, 3]) == "f"


# ---------------------------------------------------------------------------
# Sensor placements + zone map
# ---------------------------------------------------------------------------

class TestPlacements:
    def test_four_evenly_spaced_sensors_sit_on_the_corners(self):
        positions = [p.position for p in evenly_spaced_placements(4)]
        assert positions == pytest.approx([0.125, 0.375, 0.625, 0.875])

    def test_quadrant_default_is_identity(self):
        placements = quadrant_placements(4)
        assert [p.position for p in placements] == pytest.approx(
            [QUADRANT_POSITIONS["Q1"], QUADRANT_POSITIONS["Q2"],
             QUADRANT_POSITIONS["Q3"], QUADRANT_POSITIONS["Q4"]])

    def test_quadrant_assignment_moves_a_sensor(self):
        placements = quadrant_placements(4, {"0": "Q4", 3: "q1"})
        by_index = {p.index: p.position for p in placements}
        assert by_index[0] == pytest.approx(QUADRANT_POSITIONS["Q4"])
        assert by_index[3] == pytest.approx(QUADRANT_POSITIONS["Q1"])
        assert by_index[1] == pytest.approx(QUADRANT_POSITIONS["Q2"])

    def test_non_four_boards_fall_back_to_even_spacing(self):
        assert [p.position for p in quadrant_placements(2, {"0": "Q3"})] == \
            pytest.approx([0.25, 0.75])

    def test_normalise_sensor_quadrants_keeps_only_real_changes(self):
        assert normalise_sensor_quadrants({"0": "Q1", "1": "Q3", "x": "Q2",
                                           "2": "bogus"}, 4) == {"1": "Q3"}
        assert normalise_sensor_quadrants({"0": "Q3"}, 2) == {}
        assert normalise_sensor_quadrants("nope", 4) == {}


class TestZoneMap:
    def test_thymio_corner_zones_are_equal_quarters(self):
        strip = LedStripGeometry(count=68, gap=1)
        zone_map = TouchZoneMap(strip, quadrant_placements(4))
        sizes = sorted(len(zone_map.zone_pixels(z)) for z in zone_map.zone_ids)
        assert sum(sizes) == 68
        assert max(sizes) - min(sizes) <= 1
        assert zone_map.zone_ids == [0, 1, 2, 3]
        # Every pixel belongs to exactly one zone.
        seen = [p for z in zone_map.zone_ids for p in zone_map.zone_pixels(z)]
        assert sorted(seen) == list(range(68))

    def test_zone_follows_the_sensor_position(self):
        strip = LedStripGeometry(count=8)
        zone_map = TouchZoneMap(strip, [SensorPlacement(0, 0.0),
                                        SensorPlacement(1, 0.5)])
        # Pixels 2 (0.25) and 6 (0.75) tie and go to the lower index.
        assert zone_map.zone_pixels(0) == [0, 1, 2, 6, 7]
        assert zone_map.zone_pixels(1) == [3, 4, 5]
        assert zone_map.zone_of_pixel(4) == 1
        assert zone_map.zone_pixels(9) == []

    def test_ties_go_to_the_lower_index(self):
        strip = LedStripGeometry(count=4)
        zone_map = TouchZoneMap(strip, [SensorPlacement(0, 0.0),
                                        SensorPlacement(1, 0.5)])
        # Pixels 1 (0.25) and 3 (0.75) are equidistant from both sensors.
        assert zone_map.zone_pixels(0) == [0, 1, 3]
        assert zone_map.zone_pixels(1) == [2]

    def test_ordered_from_centre_starts_at_the_sensor_pixel(self):
        strip = LedStripGeometry(count=68, gap=1)
        zone_map = TouchZoneMap(strip, quadrant_placements(4))
        for zone in zone_map.zone_ids:
            order = zone_map.ordered_from_centre(zone)
            assert order[0] == zone_map.zone_centre_pixel(zone)
            assert sorted(order) == sorted(zone_map.zone_pixels(zone))

    def test_mounting_angle_rotates_the_zones_with_the_strip(self):
        placements = quadrant_placements(4)
        base = TouchZoneMap(LedStripGeometry(count=68, gap=1), placements)
        turned = TouchZoneMap(LedStripGeometry(
            count=68, gap=1, start=start_from_angle(180.0)), placements)
        # Half a turn later the top-left zone's pixels are the old
        # bottom-right zone's, up to the seam.
        assert len(set(turned.zone_pixels(0)) & set(base.zone_pixels(3))) >= 15

    def test_needs_a_placement(self):
        with pytest.raises(ValueError):
            TouchZoneMap(LedStripGeometry(count=4), [])
