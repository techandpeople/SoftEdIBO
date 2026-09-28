"""HoldKeeper - the single owner of one node's leak-compensating holds.

The firmware drops a ``hold_duty`` hold that is not refreshed for ~6 s (its
dead-man), so the PC re-asserts every active hold every ~2 s. That keepalive
used to live in several places (the controller for the app's skins, the Test
Actuators dialog for its own buttons), and the copies fought: one held a
chamber on the pressure side while the other held it on the vacuum side, and
the node flipped between them forever. This class is now the only place that
keeps holds alive for a node, and it knows when a hold must die:

* **Node rebooted.** A node that has just booted holds nothing. The keeper
  forgets every hold on the boot announce instead of re-creating them with
  the next keepalive (the "deflate turns on by itself when the Thymio is
  switched on" bug).
* **Node silent.** No frame from the node for longer than the firmware's own
  dead-man: the node is off or out of range, so the keeper drops its holds
  and stops sending, rather than keeping them alive into the void.
* **Bench claim.** While a bench tool (Test Actuators) owns the node, only
  holds started by that tool are accepted; the app's automatic holds are
  dropped on the claim and refused until the bench releases the node.

Keepalive frames carry ``ka: 1``: firmware that knows the flag only refreshes
a hold it already runs, so a keepalive can never start a hold on a node that
rebooted in between. The first frame of a hold carries no flag.

Listeners registered with :meth:`on_dropped` hear about every hold the keeper
drops on its own (reboot, silence, bench claim, :meth:`drop_all`), so a skin
can forget its automatic holds too.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# PC keepalive cadence. The firmware drops a hold not refreshed for ~6 s, so
# ~2 s survives a couple of dropped ESP-NOW frames.
KEEPALIVE_S = 2.0
# Node silence after which its holds are dropped: the firmware's own
# dead-man (hold_duty.h KEEPALIVE_MS), so the PC never outlives the node.
SILENCE_S = 6.0


class HoldKeeper:
    """Keeps one node's regulated holds alive, and only while they make sense.

    Args:
        send: Sends one node command, ``send(command, **payload) -> bool``.
        keepalive_s: Re-assert cadence.
        silence_s: Drop every hold once the node has been silent this long.
        clock: Monotonic clock (injectable for tests).
        name: Label for the keepalive thread and log lines.
    """

    def __init__(self, send: Callable[..., bool], *,
                 keepalive_s: float = KEEPALIVE_S,
                 silence_s: float = SILENCE_S,
                 clock: Callable[[], float] = time.monotonic,
                 name: str = "node") -> None:
        self._send = send
        self._keepalive_s = keepalive_s
        self._silence_s = silence_s
        self._clock = clock
        self._name = name
        self._holds: dict[int, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_heard = clock()
        self._bench = False
        self._listeners: list[Callable[[list[int]], None]] = []

    # ------------------------------------------------------------------
    # Holds
    # ------------------------------------------------------------------

    def start(self, payload: dict[str, Any], *, bench: bool = False) -> bool:
        """Start (or retune) the hold described by a ``hold_duty`` payload.

        ``bench`` marks a hold started by the bench tool that owns the node;
        while the node is claimed every other hold is refused (returns False).
        """
        chamber = int(payload["chamber"])
        with self._lock:
            if self._bench and not bench:
                logger.debug("%s: hold on chamber %d refused, bench owns the node",
                             self._name, chamber)
                return False
            self._holds[chamber] = dict(payload)
            self._ensure_thread()
            # Sent under the lock so a keepalive can never land after a stop.
            return self._send("hold_duty", **payload)

    def stop(self, chamber: int | None = None) -> None:
        """End one hold (every hold when ``chamber`` is None) and tell the node."""
        with self._lock:
            if chamber is None:
                had = bool(self._holds)
                self._holds.clear()
            else:
                had = self._holds.pop(int(chamber), None) is not None
            if had:
                self._send("hold_duty",
                           chamber=-1 if chamber is None else int(chamber), off=1)

    def active(self) -> list[int]:
        """Chambers currently held."""
        with self._lock:
            return sorted(self._holds)

    def drop_all(self) -> None:
        """Forget every hold WITHOUT telling the node (it is being stopped or
        has already lost them) and notify the listeners."""
        with self._lock:
            dropped = sorted(self._holds)
            self._holds.clear()
        self._notify(dropped)

    # ------------------------------------------------------------------
    # Node liveness
    # ------------------------------------------------------------------

    def node_heard(self) -> None:
        """Any frame from the node: it is alive."""
        self._last_heard = self._clock()

    def node_rebooted(self) -> None:
        """The node announced a boot: it holds nothing any more."""
        self.node_heard()
        if self.active():
            logger.info("%s rebooted: dropping its holds", self._name)
        self.drop_all()

    # ------------------------------------------------------------------
    # Bench ownership
    # ------------------------------------------------------------------

    @property
    def bench_active(self) -> bool:
        """True while a bench tool owns this node's holds."""
        return self._bench

    def claim_bench(self) -> None:
        """A bench tool takes the node: drop (and release on the node) every
        hold the app had, and refuse new app holds until :meth:`release_bench`."""
        with self._lock:
            self._bench = True
            dropped = sorted(self._holds)
            self._holds.clear()
            if dropped:
                self._send("hold_duty", chamber=-1, off=1)
        self._notify(dropped)

    def release_bench(self) -> None:
        """The bench tool is done: end its holds and accept app holds again."""
        self.stop()
        with self._lock:
            self._bench = False

    # ------------------------------------------------------------------
    # Listeners / lifetime
    # ------------------------------------------------------------------

    def on_dropped(self, callback: Callable[[list[int]], None]) -> None:
        """Call ``callback(chambers)`` whenever the keeper drops holds itself."""
        self._listeners.append(callback)

    def close(self) -> None:
        """Stop keeping anything alive (owner going away). Silent: the node's
        own dead-man releases whatever it still runs."""
        with self._lock:
            self._holds.clear()
            self._bench = False
        self._listeners.clear()

    def _notify(self, dropped: list[int]) -> None:
        if not dropped:
            return
        for cb in list(self._listeners):
            try:
                cb(dropped)
            except Exception:
                logger.exception("%s: hold-drop listener failed", self._name)

    # ------------------------------------------------------------------
    # Keepalive thread
    # ------------------------------------------------------------------

    def _ensure_thread(self) -> None:
        """Start the keepalive thread if not running (lock held)."""
        t = self._thread
        if t is not None and t.is_alive():
            return
        self._thread = threading.Thread(target=self._loop,
                                        name=f"hold-keepalive-{self._name}",
                                        daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        """Re-assert every hold until none remain or the node goes silent."""
        while True:
            time.sleep(self._keepalive_s)
            if self.tick():
                continue
            return

    def tick(self) -> bool:
        """One keepalive round. Returns False once there is nothing left to
        keep (the thread then exits). Public for deterministic tests."""
        silent = self._clock() - self._last_heard > self._silence_s
        with self._lock:
            if self._holds and not silent:
                for p in self._holds.values():
                    self._send("hold_duty", **p, ka=1)
                return True
            dropped = sorted(self._holds)
            self._holds.clear()
            self._thread = None
        if dropped:
            logger.info("%s silent for %.0f s: dropping its holds",
                        self._name, self._silence_s)
        self._notify(dropped)
        return False
