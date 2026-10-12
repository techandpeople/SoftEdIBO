"""TouchLog: the metadata recorded with each touch press/release."""

from datetime import datetime

from src.core.touch_log import TouchLog


def test_first_press_has_no_interval():
    log = TouchLog()
    details = log.press("s", 0, sensor_id=2, intensity_ut=123.44,
                        monotonic_ms=1000.0)
    assert details == {"sensor_id": 2, "intensity_ut": 123.4}


def test_second_press_reports_interval_and_rate():
    log = TouchLog()
    log.press("s", 0, sensor_id=2, monotonic_ms=1000.0)
    details = log.press("s", 0, sensor_id=2, monotonic_ms=1500.0)
    assert details["touch_interval_ms"] == 500
    assert details["frequency_hz"] == 2.0


def test_release_pairs_with_its_press_using_the_given_clock():
    log = TouchLog()
    stamp = datetime(2026, 10, 12, 10, 0, 0, 250000)
    log.press("s", 0, sensor_id=1, intensity_ut=100.0, timestamp=stamp,
              monotonic_ms=1000.0)
    details = log.release("s", 0, sensor_id=1, intensity_ut=30.0, peak_ut=410.26,
                          monotonic_ms=1320.0)
    assert details == {
        "duration_ms": 320,
        "press_timestamp": "2026-10-12T10:00:00.250",
        "sensor_id": 1,
        "press_intensity_ut": 100.0,
        "release_intensity_ut": 30.0,
        "peak_intensity_ut": 410.3,
    }


def test_two_sensors_on_one_chamber_do_not_mix():
    log = TouchLog()
    log.press("s", 0, sensor_id=0, monotonic_ms=0.0)
    log.press("s", 0, sensor_id=1, monotonic_ms=100.0)
    assert log.release("s", 0, sensor_id=0, monotonic_ms=500.0)["duration_ms"] == 500
    assert log.release("s", 0, sensor_id=1, monotonic_ms=500.0)["duration_ms"] == 400


def test_release_without_a_press_is_empty():
    assert TouchLog().release("s", 0, sensor_id=0) == {}


def test_reset_forgets_open_presses_and_rates():
    log = TouchLog()
    log.press("s", 0, sensor_id=0, monotonic_ms=0.0)
    log.reset()
    assert log.release("s", 0, sensor_id=0, monotonic_ms=10.0) == {}
    assert "touch_interval_ms" not in log.press("s", 0, sensor_id=0,
                                                monotonic_ms=20.0)
