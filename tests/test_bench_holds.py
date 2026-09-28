"""Tests for BenchHolds - the per-chamber Hold mode of the bench tool."""

from unittest.mock import MagicMock

from src.hardware.bench_holds import BenchHolds


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _holds(levels):
    ctrl = MagicMock()
    ctrl.start_hold.return_value = True
    clock = _Clock()
    bh = BenchHolds(ctrl, level_kpa=levels.get,
                    level_pct=lambda slot, kpa: int(kpa / 30.0 * 100),
                    seed_duty=lambda slot, kpa: 0, clock=clock)
    return bh, ctrl, clock


def test_mode_on_at_a_level_holds_now_on_the_pressure_side():
    bh, ctrl, _ = _holds({0: 30.0})
    bh.set_mode(0, True)
    ctrl.start_hold.assert_called_once_with(0, 0, kpa=30.0, vacuum=False, bench=True)


def test_mode_on_below_ambient_holds_on_the_vacuum_side():
    bh, ctrl, _ = _holds({1: -2.3})
    bh.set_mode(1, True)
    assert ctrl.start_hold.call_args.kwargs["vacuum"] is True


def test_nothing_to_hold_at_ambient_or_empty():
    bh, ctrl, _ = _holds({0: 0.1, 1: 1.0})
    bh.set_mode(0, True)
    bh.set_mode(1, True)          # 1 kPa of 30 = 3 %: empty
    ctrl.start_hold.assert_not_called()


def test_actuation_pauses_then_holds_after_settle_only_in_mode():
    levels = {0: 5.0, 1: 5.0}
    bh, ctrl, clock = _holds(levels)
    bh.set_mode(0, True)
    ctrl.reset_mock()
    bh.actuation_started([0, 1])
    assert ctrl.stop_hold.call_count == 2
    levels[0] = levels[1] = 30.0
    bh.actuation_state(0, True)
    bh.actuation_state(0, False)
    bh.actuation_state(1, True)
    bh.actuation_state(1, False)
    ctrl.start_hold.assert_called_once()
    assert ctrl.start_hold.call_args.args[0] == 0


def test_quick_fill_unseen_moving_holds_after_settle_time():
    bh, ctrl, clock = _holds({0: 30.0})
    bh.set_mode(0, True)
    ctrl.reset_mock()
    bh.actuation_started([0])
    bh.actuation_state(0, False)          # too early, never seen moving
    ctrl.start_hold.assert_not_called()
    clock.t = 2.0
    bh.actuation_state(0, False)
    ctrl.start_hold.assert_called_once()


def test_interrupt_ends_hold_but_keeps_mode():
    bh, ctrl, _ = _holds({0: 30.0})
    bh.set_mode(0, True)
    bh.interrupt()
    ctrl.stop_hold.assert_called_with(None)
    assert bh.is_on(0)


def test_mode_off_stops_hold_and_release_clears_everything():
    bh, ctrl, _ = _holds({0: 30.0})
    bh.set_mode(0, True)
    bh.set_mode(0, False)
    ctrl.stop_hold.assert_called_with(0)
    assert not bh.is_on(0)
    bh.set_mode(0, True)
    bh.release()
    assert not bh.is_on(0)
    ctrl.stop_hold.assert_called_with()
