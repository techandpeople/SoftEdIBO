"""PressRateMeter - shared per-sensor press rate."""

from src.core.press_rate import PressRateMeter


def test_rate_from_last_interval_and_expiry():
    meter = PressRateMeter(stale_ms=1000)
    assert meter.record(0, 0.0) is None
    assert meter.record(0, 500.0) == 500.0
    assert meter.frequency_hz(0, now_ms=600.0) == 2.0
    assert meter.frequency_hz(0, now_ms=1600.0) is None      # stale
    assert meter.frequencies_hz(now_ms=600.0) == {0: 2.0}


def test_stale_time_is_adjustable_and_reset_clears():
    meter = PressRateMeter(stale_ms=100)
    meter.record(1, 0.0)
    meter.record(1, 250.0)
    assert meter.frequency_hz(1, now_ms=400.0) is None
    meter.set_stale_ms(10_000)
    assert meter.frequency_hz(1, now_ms=400.0) == 4.0
    meter.reset()
    assert meter.frequencies_hz(now_ms=400.0) == {}
    assert meter.last_press_ms() == {}
