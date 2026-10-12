"""The axis taxonomy: form, movement, force, extent -> derived gesture."""

import pytest

from src.ml import gesture_taxonomy as tax
from src.ml.touch_axes import activation_threshold, read_touch
from src.ml.touch_classifier import LiveTouchClassifier
from src.ml.touch_segmenter import TouchSegmenter, merge_segments

IDLE = {"type": "magnet", "mag": [0.0] * 4, "act": []}


def _hot(sensors, level=400.0, vec=None):
    mag = [0.0] * 4
    for sensor in sensors:
        mag[sensor] = level
    msg = {"type": "magnet", "mag": mag, "act": list(sensors)}
    if vec is not None:
        msg["vec"] = [list(vec) if i in sensors else [0.0, 0.0, 0.0]
                      for i in range(4)]
    return msg


def _touch(start_ms, length_ms, sensors=(0,), level=400.0, step_ms=50.0, vec=None):
    """Samples of one press of ``length_ms`` followed by an idle frame."""
    samples, t = [], start_ms
    while t <= start_ms + length_ms:
        samples.append((_hot(sensors, level, vec), t))
        t += step_ms
    samples.append((IDLE, t))
    return samples


def _segment(samples):
    segments = TouchSegmenter().segment_stream([(IDLE, -10.0)] + samples)
    merged = merge_segments(segments)
    assert merged is not None
    return merged


def _bout(starts, length_ms=100.0, level=400.0):
    samples = []
    for start in starts:
        samples += _touch(start, length_ms, level=level)
    return _segment(samples)


def _path(order, level=400.0, vec=None):
    samples = [(_hot((sensor,), level, vec), 100.0 * i)
               for i, sensor in enumerate(order)]
    return _segment(samples + [(IDLE, 100.0 * len(order))])


def _read(segment, **kwargs):
    return read_touch(segment, threshold_ut=300.0, **kwargs)


def test_short_touch_is_a_tap():
    reading = _read(_segment(_touch(0.0, 100.0)))
    assert (reading.form, reading.movement, reading.extent) == (
        tax.FORM_SHORT, tax.MOVE_STATIC, tax.EXTENT_POINT)
    assert reading.gesture == tax.TAP


def test_very_short_strong_touch_is_a_hit():
    reading = _read(_segment(_touch(0.0, 100.0, level=1500.0)))
    assert reading.force == tax.FORCE_STRONG and reading.gesture == tax.HIT


def test_sustained_touch_is_a_press_then_a_resting_hand():
    assert _read(_segment(_touch(0.0, 900.0))).gesture == tax.PRESS
    rest = _read(_segment(_touch(0.0, 4000.0, level=350.0)))
    assert (rest.form, rest.force, rest.gesture) == (
        tax.FORM_REST, tax.FORCE_LIGHT, tax.REST)


def test_regular_bout_is_compressions_and_uneven_one_is_pats():
    steady = _read(_bout([0, 500, 1000, 1500, 2000], level=1500.0))
    assert steady.form == tax.FORM_REGULAR and steady.gesture == tax.COMPRESSIONS
    assert steady.descriptors["pulse_interval_ms"] == 500
    assert steady.descriptors["pulse_interval_cv"] == 0.0
    uneven = _read(_bout([0, 250, 1100, 1300, 2300]))
    assert uneven.form == tax.FORM_IRREGULAR and uneven.gesture == tax.PATS
    assert uneven.descriptors["pulse_interval_cv"] > tax.REGULAR_MAX_CV


def test_two_quick_touches_are_pats_not_compressions():
    assert _read(_bout([0, 300])).gesture == tax.PATS


def test_travelling_touch_is_a_stroke_and_back_and_forth_is_a_rub():
    stroke = _read(_path([0, 1, 2, 3]))
    assert (stroke.movement, stroke.gesture) == (tax.MOVE_SLIDE, tax.STROKE)
    rub = _read(_path([0, 1, 0, 1]))
    assert (rub.movement, rub.gesture) == (tax.MOVE_RUB, tax.RUB)


def test_straight_down_push_across_sensors_is_not_a_slide():
    # 3-axis data says the magnets were pushed down, not sheared sideways.
    pushed = _read(_path([0, 1], vec=(0.0, 0.0, 50.0)))
    assert pushed.movement == tax.MOVE_STATIC
    sheared = _read(_path([0, 1], vec=(40.0, 0.0, 20.0)))
    assert sheared.movement == tax.MOVE_SLIDE
    assert "z_frac" in sheared.descriptors


def test_a_model_verdict_replaces_the_movement_rule():
    reading = _read(_segment(_touch(0.0, 900.0)), movement=tax.MOVE_SLIDE)
    assert reading.gesture == tax.STROKE


def test_force_is_scaled_by_the_skin_threshold():
    segment = _segment(_touch(0.0, 900.0, level=700.0))
    assert read_touch(segment, threshold_ut=300.0).force == tax.FORCE_MEDIUM
    assert read_touch(segment, threshold_ut=100.0).force == tax.FORCE_STRONG


def test_extent_counts_sensors_held_at_once():
    assert _read(_segment(_touch(0.0, 900.0, sensors=(0, 1)))).extent == tax.EXTENT_MULTI
    assert _read(_segment(_touch(0.0, 900.0, sensors=(0, 1, 2, 3)))).extent == tax.EXTENT_WHOLE


def test_squeeze_needs_a_pressure_rise():
    held = _segment(_touch(0.0, 900.0, sensors=(0, 1, 2)))
    assert _read(held).gesture == tax.PRESS
    assert _read(held, pressure_rise_kpa=1.2).gesture == tax.SQUEEZE
    assert _read(held, pressure_rise_kpa=0.1).gesture == tax.PRESS


def test_every_gesture_class_is_reachable_and_labelled():
    reachable = {
        tax.derive_gesture(form, movement, force, extent, n_pulses=pulses,
                           duration_ms=duration, pressure_rise_kpa=rise)
        for form in tax.FORMS for movement in tax.MOVEMENTS
        for force in tax.FORCES for extent in tax.EXTENTS
        for pulses in (1, 5) for duration in (100.0, 900.0)
        for rise in (None, 2.0)}
    assert set(tax.GESTURE_CLASSES) <= reachable
    assert set(tax.GESTURE_CLASSES) == set(tax.LABELS) == set(tax.MOVEMENT_OF)
    assert set(tax.GESTURE_CLASSES) < set(tax.DEFINITIONS)


@pytest.mark.parametrize("config, expected", [
    ({"act_threshold_ut": 120}, 120.0),
    ({"quadrant_thresholds": [100, 200]}, 150.0),
    ({}, 300.0),
    (None, 300.0),
])
def test_activation_threshold_comes_from_the_touch_config(config, expected):
    assert activation_threshold(config) == expected


class _Source:
    def __init__(self):
        self.cb = None

    def on_magnet(self, cb):
        self.cb = cb
        return True


class _Skin:
    skin_id = "skin-1"
    skin_type = ""
    skin_variant = ""
    touch = {"act_threshold_ut": 300.0}

    def __init__(self):
        self.touch_source = _Source()
        self.touch_controller = self.touch_source

    def on_magnet(self, cb):
        return self.touch_source.on_magnet(cb)


def test_live_classifier_names_gestures_without_any_trained_model(monkeypatch):
    import time
    clock = {"now": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    seen = []
    skin = _Skin()
    live = LiveTouchClassifier(
        skin, lambda sid, label, seg, reading: seen.append((sid, label, reading)))
    assert live.attach()
    feed = skin.touch_source.cb
    assert feed is not None

    def at(ms, msg):
        clock["now"] = ms / 1000.0
        feed(msg)

    at(0, IDLE)
    at(50, _hot((0,)))
    at(100, _hot((0,)))
    at(150, IDLE)
    at(1000, IDLE)
    assert seen == []                      # still inside the bout window
    at(1400, IDLE)
    [(skin_id, label, reading)] = seen
    assert (skin_id, label) == ("skin-1", tax.TAP)
    assert reading.as_details()["form"] == tax.FORM_SHORT
