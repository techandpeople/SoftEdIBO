"""Tests for chamber fill-time calibration core + settings helpers."""

from pytest import approx

from src.hardware.fill_calibration import (
    FillTimeCalibrator,
    chambers_missing_fill_time,
    iter_actuator_chambers,
    set_fill_time,
)


class _FakeClock:
    """Manually-advanced monotonic clock (seconds)."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance_ms(self, ms):
        self.t += ms / 1000.0


def test_records_time_when_target_reached():
    clk = _FakeClock()
    cal = FillTimeCalibrator(target_pct=95.0, clock=clk)
    cal.start()
    clk.advance_ms(800)
    assert cal.update(50.0) is None          # not full yet
    clk.advance_ms(400)
    res = cal.update(96.0)                    # crosses target at t=1200 ms
    assert res == approx(1200.0) and not cal.timed_out
    assert not cal.running


def test_caps_and_flags_timeout_when_target_never_reached():
    clk = _FakeClock()
    cal = FillTimeCalibrator(target_pct=95.0, max_ms=5000.0, clock=clk)
    cal.start()
    clk.advance_ms(5001)
    res = cal.update(40.0)                    # still low, but past the ceiling
    assert res == 5000.0 and cal.timed_out


def test_tick_enforces_timeout_without_pressure_updates():
    clk = _FakeClock()
    cal = FillTimeCalibrator(max_ms=5000.0, clock=clk)
    cal.start()
    clk.advance_ms(5001)
    assert cal.tick() == 5000.0 and cal.timed_out


def _settings():
    return {"robots": {"turtles": [{
        "id": "turtle_1",
        "nodes": [
            {"mac": "AA:01", "node_type": "node_direct"},
            {"mac": "BB:02", "node_type": "node_magnet_sensor"},
        ],
        "skins": [{
            "skin_id": "belly",
            "chambers": [
                {"mac": "AA:01", "slot": 0, "max_pressure": 8.0},
                {"mac": "AA:01", "slot": 1, "max_pressure": 8.0,
                 "fill_time_ms": 1800},
            ],
        }],
    }]}}


def test_iter_actuator_chambers_joins_node_type_and_skips_sensors():
    chs = iter_actuator_chambers(_settings())
    assert len(chs) == 2                      # both on the node_direct
    by_slot = {c["slot"]: c for c in chs}
    assert by_slot[0]["fill_time_ms"] is None
    assert by_slot[1]["fill_time_ms"] == 1800
    assert all(c["node_type"] == "node_direct" for c in chs)


def test_set_fill_time_writes_and_clears():
    data = _settings()
    assert set_fill_time(data, "AA:01", 0, 1234.6) == 1
    ch0 = data["robots"]["turtles"][0]["skins"][0]["chambers"][0]
    assert ch0["fill_time_ms"] == 1235        # rounded to int
    assert set_fill_time(data, "AA:01", 0, None) == 1
    assert "fill_time_ms" not in ch0


def test_chambers_missing_fill_time():
    missing = chambers_missing_fill_time(_settings())
    assert [c["slot"] for c in missing] == [0]
