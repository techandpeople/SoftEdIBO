"""Tests for HoldKeeper - the single owner of a node's regulated holds."""

from src.hardware.hold_keeper import HoldKeeper


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class _Recorder:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    def __call__(self, command, **kwargs):
        self.sent.append((command, kwargs))
        return True


def _keeper():
    clock, send = _Clock(), _Recorder()
    # Huge keepalive period: the thread never ticks on its own, tests call tick().
    keeper = HoldKeeper(send, keepalive_s=3600, silence_s=6.0, clock=clock)
    return keeper, send, clock


def test_first_frame_starts_hold_and_keepalives_carry_ka():
    keeper, send, _ = _keeper()
    assert keeper.start({"chamber": 1, "duty": 0, "kpa": 5.0})
    assert send.sent == [("hold_duty", {"chamber": 1, "duty": 0, "kpa": 5.0})]
    send.sent.clear()
    assert keeper.tick()
    assert send.sent == [("hold_duty", {"chamber": 1, "duty": 0, "kpa": 5.0, "ka": 1})]


def test_node_reboot_drops_holds_and_notifies():
    keeper, send, _ = _keeper()
    dropped: list[list[int]] = []
    keeper.on_dropped(dropped.append)
    keeper.start({"chamber": 1, "duty": 0, "kpa": -2.3, "dir": 1})
    keeper.start({"chamber": 2, "duty": 0, "kpa": -2.4, "dir": 1})
    send.sent.clear()
    keeper.node_rebooted()
    assert keeper.active() == []
    assert dropped == [[1, 2]]
    assert not keeper.tick()          # nothing left to keep alive
    assert send.sent == []            # and nothing re-created on the idle node


def test_silent_node_holds_are_dropped_not_kept_alive():
    keeper, send, clock = _keeper()
    dropped: list[list[int]] = []
    keeper.on_dropped(dropped.append)
    keeper.start({"chamber": 0, "duty": 0, "kpa": 5.0})
    send.sent.clear()
    clock.t = 5.0
    assert keeper.tick()              # still within the dead-man: refreshed
    clock.t = 7.0
    assert not keeper.tick()          # silent > 6 s: dropped
    assert keeper.active() == []
    assert dropped == [[0]]
    assert [c for c, kw in send.sent if "ka" in kw] == ["hold_duty"]


def test_heard_node_keeps_holds_alive():
    keeper, _, clock = _keeper()
    keeper.start({"chamber": 0, "duty": 0, "kpa": 5.0})
    for t in (4.0, 8.0, 12.0):
        clock.t = t
        keeper.node_heard()
        assert keeper.tick()
    assert keeper.active() == [0]


def test_bench_claim_drops_app_holds_and_refuses_new_ones():
    keeper, send, _ = _keeper()
    dropped: list[list[int]] = []
    keeper.on_dropped(dropped.append)
    keeper.start({"chamber": 0, "duty": 0, "kpa": -2.0, "dir": 1})
    send.sent.clear()
    keeper.claim_bench()
    assert keeper.bench_active
    assert dropped == [[0]]
    assert send.sent == [("hold_duty", {"chamber": -1, "off": 1})]
    assert not keeper.start({"chamber": 0, "duty": 0, "kpa": -2.0, "dir": 1})
    assert keeper.start({"chamber": 0, "duty": 0, "kpa": 30.0}, bench=True)
    assert keeper.active() == [0]
    send.sent.clear()
    keeper.release_bench()
    assert not keeper.bench_active
    assert keeper.active() == []
    assert send.sent == [("hold_duty", {"chamber": -1, "off": 1})]
    assert keeper.start({"chamber": 0, "duty": 0, "kpa": 5.0})


def test_stop_one_hold_sends_off_only_when_held():
    keeper, send, _ = _keeper()
    keeper.stop(3)
    assert send.sent == []
    keeper.start({"chamber": 3, "duty": 0, "kpa": 5.0})
    send.sent.clear()
    keeper.stop(3)
    assert send.sent == [("hold_duty", {"chamber": 3, "off": 1})]
