"""Tests for TouchEventRouter - sensor press/release edge detection + mapping."""

from src.hardware.touch_event_router import TouchEventRouter


class FakeMagnetController:
    """Minimal stand-in exposing on_magnet/fire_magnet like the real boards."""

    def __init__(self):
        self._cbs = []

    def on_magnet(self, cb):
        self._cbs.append(cb)

    def fire_magnet(self, data):
        for cb in self._cbs:
            cb(data)


def _collect(router):
    events = []
    router.subscribe(lambda chamber_id, action: events.append((chamber_id, action)))
    return events


def test_press_and_release_edge_detection():
    router = TouchEventRouter.from_touch_config({"sensor_count": 3}, chamber_count=3)
    events = _collect(router)

    router.handle_magnet({"act": [0, 2]})   # both press
    router.handle_magnet({"act": [2]})      # 0 released, 2 held
    router.handle_magnet({"act": []})       # 2 released

    assert events == [
        (0, "press"), (2, "press"),
        (0, "release"),
        (2, "release"),
    ]


def test_no_event_when_set_unchanged():
    router = TouchEventRouter.from_touch_config({"sensor_count": 2}, chamber_count=2)
    events = _collect(router)

    router.handle_magnet({"act": [1]})
    router.handle_magnet({"act": [1]})      # same set -> no new event

    assert events == [(1, "press")]


def test_detailed_events_include_sensor_and_edge_magnitude():
    router = TouchEventRouter.from_touch_config({"sensor_count": 2}, chamber_count=2)
    events = []
    router.subscribe_detail(
        lambda chamber, action, sensor, intensity: events.append(
            (chamber, action, sensor, intensity)))

    router.handle_magnet({"act": [1], "mag": [12.0, 345.6]})
    router.handle_magnet({"act": [], "mag": [10.0, 78.9]})

    assert events == [(1, "press", 1, 345.6), (1, "release", 1, 78.9)]


def test_explicit_sensor_to_chamber_mapping():
    router = TouchEventRouter.from_touch_config(
        {"sensor_to_chamber": {"0": 5, "1": 6}}, chamber_count=8)
    events = _collect(router)

    router.handle_magnet({"act": [1]})

    assert events == [(6, "press")]


def test_unmapped_sensor_falls_back_to_sensor_index():
    router = TouchEventRouter.from_touch_config(
        {"sensor_to_chamber": {"0": 5}}, chamber_count=8)
    events = _collect(router)

    router.handle_magnet({"act": [3]})      # 3 not in mapping -> raw index

    assert events == [(3, "press")]


def test_attach_consumes_controller_events():
    ctrl = FakeMagnetController()
    router = TouchEventRouter.from_touch_config({"sensor_count": 1}, chamber_count=1)
    events = _collect(router)

    router.attach(ctrl)
    ctrl.fire_magnet({"act": [0]})
    ctrl.fire_magnet({"act": []})

    assert events == [(0, "press"), (0, "release")]


def test_attach_tolerates_missing_controller():
    router = TouchEventRouter.from_touch_config(None, chamber_count=0)
    router.attach(None)            # must not raise
    router.attach(object())        # no on_magnet -> no-op


def test_non_list_act_is_ignored():
    router = TouchEventRouter.from_touch_config({"sensor_count": 2}, chamber_count=2)
    events = _collect(router)

    router.handle_magnet({"act": None})
    router.handle_magnet({})
    router.handle_magnet({"act": "garbage"})

    assert events == []


def test_bad_subscriber_does_not_break_others():
    router = TouchEventRouter.from_touch_config({"sensor_count": 1}, chamber_count=1)
    seen = []
    router.subscribe(lambda c, a: (_ for _ in ()).throw(RuntimeError("boom")))
    router.subscribe(lambda c, a: seen.append((c, a)))

    router.handle_magnet({"act": [0]})

    assert seen == [(0, "press")]


def test_unsubscribe_and_prune_deleted_qt_source():
    router = TouchEventRouter.from_touch_config({"sensor_count": 2}, chamber_count=2)
    events = _collect(router)

    def dead(_chamber_id, _action):
        raise RuntimeError("Signal source has been deleted")

    router.subscribe(dead)
    router.handle_magnet({"act": [0]})        # dead listener pruned, live one fires
    assert events == [(0, "press")]
    assert dead not in router._cbs

    cb = router._cbs[0]
    router.unsubscribe(cb)
    router.handle_magnet({"act": []})
    assert events == [(0, "press")]


def test_edges_carry_detection_time_and_press_peak():
    router = TouchEventRouter.from_touch_config({"sensor_count": 2}, chamber_count=2)
    edges = []
    router.subscribe_edge(edges.append)

    router.handle_magnet({"act": [1], "mag": [0.0, 120.0]})
    router.handle_magnet({"act": [1], "mag": [0.0, 480.0]})   # strongest sample
    router.handle_magnet({"act": [1], "mag": [0.0, 200.0]})
    router.handle_magnet({"act": [], "mag": [0.0, 40.0]})

    press, release = edges
    assert (press.action, press.sensor_idx, press.intensity_ut) == ("press", 1, 120.0)
    assert press.peak_ut is None
    assert (release.action, release.intensity_ut, release.peak_ut) == (
        "release", 40.0, 480.0)
    assert release.monotonic_ms >= press.monotonic_ms
    assert release.timestamp >= press.timestamp


def test_peak_does_not_leak_into_the_next_press():
    router = TouchEventRouter.from_touch_config({"sensor_count": 1}, chamber_count=1)
    edges = []
    router.subscribe_edge(edges.append)

    router.handle_magnet({"act": [0], "mag": [900.0]})
    router.handle_magnet({"act": [], "mag": [0.0]})
    router.handle_magnet({"act": [0], "mag": [150.0]})
    router.handle_magnet({"act": [], "mag": [0.0]})

    assert [e.peak_ut for e in edges if e.action == "release"] == [900.0, 150.0]


def _fake_clock(seconds):
    """A monotonic() stand-in returning each value in turn, then the last."""
    values = list(seconds)

    def clock():
        return values.pop(0) if len(values) > 1 else values[0]
    return clock


def test_node_stamped_frames_time_edges_by_the_node_clock(monkeypatch):
    import src.hardware.touch_event_router as mod

    monkeypatch.setattr(
        mod.time, "monotonic", _fake_clock([10.000, 10.180, 10.215, 10.430]))
    router = TouchEventRouter.from_touch_config({"sensor_count": 1}, chamber_count=1)
    edges = []
    router.subscribe_edge(edges.append)

    router.handle_magnet({"act": [], "mag": [0.0], "n": 0, "t": 5000})
    router.handle_magnet({"act": [0], "mag": [300.0], "n": 1, "t": 5100})
    router.handle_magnet({"act": [0], "mag": [320.0], "n": 2, "t": 5200})
    router.handle_magnet({"act": [], "mag": [0.0], "n": 4, "t": 5400})

    press, release = edges
    # 300 ms on the node clock, although the frames arrived 250 ms apart.
    assert release.monotonic_ms - press.monotonic_ms == 300.0
    assert router.frames_lost == 1


def test_unstamped_frames_fall_back_to_arrival_time(monkeypatch):
    import src.hardware.touch_event_router as mod

    monkeypatch.setattr(mod.time, "monotonic", _fake_clock([1.0, 1.25]))
    router = TouchEventRouter.from_touch_config({"sensor_count": 1}, chamber_count=1)
    edges = []
    router.subscribe_edge(edges.append)
    router.handle_magnet({"act": [0]})
    router.handle_magnet({"act": []})
    assert edges[1].monotonic_ms - edges[0].monotonic_ms == 250.0
    assert router.frames_lost == 0
