"""Tests for touch_motion - centroid, direction and the capture summary line.

Pure geometry, no Qt/sklearn. Uses the Thymio 'D' sensor positions (2x2:
S3 top-left, S0 top-right, S1 bottom-left, S2 bottom-right in screen coords).
"""

import pytest

from src.hardware.skin_geometry import geometry_for
from src.ml.touch_motion import (
    cardinal,
    path_straightness,
    segment_direction,
    segment_summary,
    weighted_centroid,
)
from src.ml.touch_segmenter import TouchSegmenter

_GEOM = geometry_for("thymio")
assert _GEOM is not None
_POS = list(_GEOM.sensors_mm)


@pytest.fixture
def slides_on(monkeypatch):
    """Enable the (default-off) slide/direction visuals for logic tests."""
    monkeypatch.setattr("src.ml.gesture_taxonomy.SLIDE_DETECTION_ENABLED", True)


def _msg(mag, act):
    return {"type": "magnet", "mag": mag, "act": list(act)}


def _segment(samples):
    """Build one TouchSegment from (mag, act) rows at 50 ms intervals."""
    stream = [(_msg([0.0] * 4, []), 0.0)]
    stream += [(_msg(mag, act), 50.0 * (i + 1))
               for i, (mag, act) in enumerate(samples)]
    stream.append((_msg([0.0] * 4, []), 50.0 * (len(samples) + 1)))
    return TouchSegmenter().segment_stream(stream)[0]


# ---------------------------------------------------------------------------
# weighted_centroid
# ---------------------------------------------------------------------------

def test_centroid_is_none_when_quiet():
    assert weighted_centroid([0.0, 0.0], [(0, 0), (10, 0)]) is None
    assert weighted_centroid([5.0, 5.0], [(0, 0), (10, 0)], floor=10.0) is None


def test_centroid_weights_by_magnitude():
    # All weight on one sensor -> its exact position.
    assert weighted_centroid([100.0, 0.0], [(0.0, 0.0), (10.0, 0.0)]) == (0.0, 0.0)
    # Equal weights -> midpoint; floor drops the quiet sensor.
    assert weighted_centroid([50.0, 50.0], [(0.0, 0.0), (10.0, 0.0)]) == (5.0, 0.0)
    centroid = weighted_centroid([50.0, 5.0], [(0.0, 0.0), (10.0, 0.0)], floor=10.0)
    assert centroid is not None
    cx, _cy = centroid
    assert cx == 0.0


# ---------------------------------------------------------------------------
# cardinal - screen coords (y down)
# ---------------------------------------------------------------------------

def test_cardinal_screen_coords():
    assert cardinal(1.0, 0.0) == ("->", "right")
    assert cardinal(-1.0, 0.0) == ("<-", "left")
    assert cardinal(0.0, 1.0) == ("v", "down")       # y down = towards viewer's down
    assert cardinal(0.0, -1.0) == ("^", "up")
    assert cardinal(1.0, -1.0) == ("^>", "up-right")


# ---------------------------------------------------------------------------
# segment_direction
# ---------------------------------------------------------------------------

def test_static_tap_has_no_direction():
    seg = _segment([([300.0, 0, 0, 0], [0]), ([300.0, 0, 0, 0], [0])])
    assert segment_direction(seg, _POS) is None


def test_stroke_direction_left_to_right():
    # S1 (bottom-left, x=40) -> S2 (bottom-right, x=105): rightward slide.
    seg = _segment([([0, 300.0, 0, 0], [1]),
                    ([0, 150.0, 150.0, 0], [1, 2]),
                    ([0, 0, 300.0, 0], [2])])
    d = segment_direction(seg, _POS)
    assert d is not None
    assert d["arrow"] == "->" and d["name"] == "right"
    assert d["from_sensors"] == [1] and d["to_sensors"] == [2]
    assert d["dist_mm"] > 60                       # 40 -> 105 mm


def test_min_travel_gate():
    seg = _segment([([0, 300.0, 0, 0], [1]), ([0, 0, 300.0, 0], [2])])
    assert segment_direction(seg, _POS, min_travel_mm=1000.0) is None
    assert segment_direction(seg, []) is None      # no positions -> no direction


def test_merged_taps_on_different_sensors_are_not_a_slide():
    # Tap S1 then tap S2 (released in between -> merged as n_pulses=2): travel
    # exists but the contact broke - must NOT read as a slide.
    from src.ml.touch_segmenter import merge_segments
    a = _segment([([0, 300.0, 0, 0], [1])])
    b = _segment([([0, 0, 300.0, 0], [2])])
    merged = merge_segments([a, b])
    assert merged is not None
    assert merged.n_pulses == 2
    assert segment_direction(merged, _POS) is None


def _vec_segment(samples):
    """One TouchSegment from (mag, act, vec) rows at 50 ms intervals."""
    stream = [({"type": "magnet", "mag": [0.0] * 4, "act": [], "vec": None}, 0.0)]
    stream += [({"type": "magnet", "mag": mag, "act": list(act), "vec": vec},
                50.0 * (i + 1))
               for i, (mag, act, vec) in enumerate(samples)]
    stream.append(({"type": "magnet", "mag": [0.0] * 4, "act": [],
                    "vec": None}, 50.0 * (len(samples) + 1)))
    return TouchSegmenter().segment_stream(stream)[0]


def test_vertical_push_with_travel_is_not_a_slide():
    # Act migrates S1->S2 within one pulse, but the 3-axis deltas are straight
    # down (pure z): two vertical presses, not a finger travelling.
    down = [[0, 0, 300]] * 4
    seg = _vec_segment([([0, 300.0, 0, 0], [1], down),
                        ([0, 0, 300.0, 0], [2], down)])
    assert segment_direction(seg, _POS) is None


def test_lateral_shear_with_travel_is_a_slide():
    # Same travel, but the deltas carry strong x/y (finger dragging sideways).
    shear = [[300, 100, 60]] * 4
    seg = _vec_segment([([0, 300.0, 0, 0], [1], shear),
                        ([0, 0, 300.0, 0], [2], shear)])
    d = segment_direction(seg, _POS)
    assert d is not None and d["name"] == "right"


def test_slide_thresholds_live_in_the_taxonomy():
    # Single common home for the tunables (gesture_taxonomy) - touch_motion
    # only aliases them, so retuning one place retunes every consumer.
    from src.ml import gesture_taxonomy as tax
    from src.ml import touch_motion
    assert touch_motion.MIN_TRAVEL_MM == tax.SLIDE_MIN_TRAVEL_MM
    assert touch_motion.MAX_SLIDE_Z_FRAC == tax.SLIDE_MAX_Z_FRAC


def test_path_straightness():
    assert path_straightness([(0, 0), (10, 0)]) == 1.0            # straight
    assert path_straightness([(0, 0), (5, 0), (10, 0)]) == 1.0    # colinear
    # There and back -> net 0, path 20 -> 0.
    assert path_straightness([(0, 0), (10, 0), (0, 0)]) == 0.0
    # L-shape: net = sqrt(200) ~= 14.14, path = 20 -> ~0.707.
    assert abs(path_straightness([(0, 0), (10, 0), (10, 10)]) - 0.7071) < 1e-3
    assert path_straightness([(0, 0)]) == 0.0                     # degenerate


def test_wandering_touch_is_not_a_slide():
    # Centroid oscillates S1<->S2 without releasing (an inflating chamber shifting
    # the magnet): net travel one hop, path three hops -> not straight -> None,
    # even though the endpoints are displaced.
    seg = _segment([([0, 300.0, 0, 0], [1]), ([0, 0, 300.0, 0], [2]),
                    ([0, 300.0, 0, 0], [1]), ([0, 0, 300.0, 0], [2])])
    assert seg.n_pulses == 1                     # never released -> one segment
    assert segment_direction(seg, _POS) is None


def test_frame_z_frac_reads_the_dominant_sensor():
    from src.ml.touch_motion import frame_z_frac
    # Dominant sensor is index 1; its delta is pure z -> 1.0.
    assert frame_z_frac([0.0, 300.0], [[300, 0, 0], [0, 0, 300]]) == 1.0
    # Lateral drag on the dominant sensor -> low z-fraction.
    z = frame_z_frac([300.0, 0.0], [[300, 0, 0], [0, 0, 300]])
    assert z == 0.0
    # No usable vector data -> None.
    assert frame_z_frac([300.0], None) is None
    assert frame_z_frac([], [[0, 0, 300]]) is None
    assert frame_z_frac([300.0], [[0, 0]]) is None


# ---------------------------------------------------------------------------
# segment_summary
# ---------------------------------------------------------------------------

def test_summary_for_a_stroke_carries_direction(slides_on):
    seg = _segment([([0, 300.0, 0, 0], [1]), ([0, 0, 300.0, 0], [2])])
    text = segment_summary(seg, "stroke", _POS)
    assert text.startswith("stroke")
    assert "ms" in text and "peak 300" in text
    assert "S1->S2" in text and "right" in text


def test_summary_omits_direction_when_slides_disabled():
    # Master switch off (the default) -> summary keeps duration/peak/sensors but
    # never shows a travel direction, even for a clear stroke.
    seg = _segment([([0, 300.0, 0, 0], [1]), ([0, 0, 300.0, 0], [2])])
    text = segment_summary(seg, "stroke", _POS)
    assert text.startswith("stroke") and "peak 300" in text
    assert "->" not in text and "right" not in text
    assert "S1" in text                          # falls back to the sensor list


def test_summary_for_a_tap_lists_sensors_instead():
    seg = _segment([([250.0, 0, 0, 0], [0])])
    text = segment_summary(seg, "tap", _POS)
    assert text.startswith("tap")
    assert "S0" in text and "-> right" not in text
