"""Confirmed delivery of the set-once safety limits (set_max/set_min).

A dropped ``set_max_pressure`` used to leave the node clamping to a stale ceiling
(the 20->50 kPa over-inflation). :class:`CommandConfirmer` tags the command with
a per-node seq and retransmits until the node ACKs it. These tests pin the
retry/NACK/timeout behaviour, plus that the controller confirms *limits* but
never routes non-idempotent actuation through the confirmer.
"""

import threading

from src.hardware.command_confirmer import CommandConfirmer
from src.hardware.esp32_controller import ESP32Controller

MAC = "AA:BB:CC:DD:EE:01"


class FakeGateway:
    """Gateway stand-in that emulates the node acking a confirmable command.

    ``drop`` sends are swallowed (no ack) to force retransmits; after that each
    send is acked synchronously on the caller's thread (so the confirmer's wait
    returns immediately — the tests need no real sleeping). ``ok`` toggles
    ack/NACK; ``ack=False`` never acks at all (timeout path)."""

    def __init__(self, *, drop: int = 0, ok: bool = True, ack: bool = True):
        self._cbs: list = []
        self.sends: list[tuple[str, str, dict]] = []
        self._drop = drop
        self._ok = ok
        self._ack = ack
        self._count = 0

    def on_message(self, cb) -> None:
        self._cbs.append(cb)

    def send(self, mac: str, command: str, *, seq=None, **fields) -> bool:
        self.sends.append((mac, command, {"seq": seq, **fields}))
        self._count += 1
        if self._ack and self._count > self._drop:
            for cb in list(self._cbs):
                cb({"source": mac, "type": "ack", "cmd": command,
                    "seq": seq, "ok": self._ok})
        return True


def test_acks_immediately_no_retry():
    gw = FakeGateway()
    c = CommandConfirmer(gw, MAC)
    assert c.confirm("set_max_pressure", chamber=0, value=20.0) is True
    assert len(gw.sends) == 1
    # The frame carried a seq the node can echo back.
    assert gw.sends[0][2]["seq"] is not None
    assert gw.sends[0][2]["value"] == 20.0


def test_retransmits_dropped_frames_then_succeeds():
    gw = FakeGateway(drop=2)   # first two sends vanish, third is acked
    c = CommandConfirmer(gw, MAC)
    assert c.confirm("set_min_pressure", chamber=1, value=-2.0,
                     timeout=0.05, retries=3) is True
    assert len(gw.sends) == 3
    # Every attempt reused the SAME seq (idempotent retransmit, not a new command).
    seqs = {s[2]["seq"] for s in gw.sends}
    assert len(seqs) == 1


def test_nack_fails_fast_without_retry():
    gw = FakeGateway(ok=False)   # node rejects (e.g. bad chamber)
    c = CommandConfirmer(gw, MAC)
    assert c.confirm("set_max_pressure", chamber=9, value=20.0) is False
    assert len(gw.sends) == 1    # a NACK is final — no retransmit


def test_timeout_after_exhausting_retries():
    gw = FakeGateway(ack=False)   # node never answers
    c = CommandConfirmer(gw, MAC)
    assert c.confirm("set_max_pressure", chamber=0, value=20.0,
                     timeout=0.01, retries=3) is False
    assert len(gw.sends) == 4     # first send + 3 retransmits


def test_ignores_acks_for_other_nodes_and_seqs():
    gw = FakeGateway(ack=False)
    c = CommandConfirmer(gw, MAC)

    # Deliver an ack for a different node and a stale seq while a confirm waits.
    def _noise():
        c._handle({"source": "FF:FF:FF:FF:FF:FF", "type": "ack",
                   "seq": 1, "ok": True})
        c._handle({"source": MAC, "type": "ack", "seq": 4242, "ok": True})

    threading.Thread(target=_noise).start()
    assert c.confirm("set_max_pressure", chamber=0, value=20.0,
                     timeout=0.02, retries=1) is False


def test_seq_wraps_without_hitting_the_firmware_sentinel():
    gw = FakeGateway(ack=False)
    c = CommandConfirmer(gw, MAC)
    c._seq = 0xFFFE            # next allocation would wrap
    assert c._next_seq() == 0   # 0xFFFF (the NO_SEQ sentinel) is skipped
    assert c._next_seq() == 1


# --- controller integration ------------------------------------------------


def test_controller_confirms_limits_off_thread():
    gw = FakeGateway()
    ctrl = ESP32Controller(MAC, gw)
    done = threading.Event()
    results: list[bool] = []
    ctrl.confirm_limits(2, 20.0, -2.0,
                        on_result=lambda ok: (results.append(ok), done.set()))
    assert done.wait(2.0)
    assert results == [True]
    cmds = [(cmd, kw["chamber"], kw["value"]) for _, cmd, kw in gw.sends]
    assert cmds == [("set_max_pressure", 2, 20.0), ("set_min_pressure", 2, -2.0)]
    # Both confirmable frames carried a seq.
    assert all(kw["seq"] is not None for _, _, kw in gw.sends)


def test_controller_actuation_stays_fire_and_forget():
    """inflate/deflate are non-idempotent — they must never carry a confirm seq
    (a retransmit would stack a second pulse)."""
    gw = FakeGateway()
    ctrl = ESP32Controller(MAC, gw)
    ctrl.inflate(chamber=0, delta=10)
    ctrl.deflate(chamber=0, delta=10)
    assert gw.sends == [
        (MAC, "inflate", {"seq": None, "chamber": 0, "delta": 10}),
        (MAC, "deflate", {"seq": None, "chamber": 0, "delta": 10}),
    ]
