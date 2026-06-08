"""Tests for TouchEventRouter — sensor press/release edge detection + mapping."""

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
    router.handle_magnet({"act": [1]})      # same set → no new event

    assert events == [(1, "press")]


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

    router.handle_magnet({"act": [3]})      # 3 not in mapping → raw index

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
    router.attach(object())        # no on_magnet → no-op


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
