"""Confirmed (ACK'd) delivery of set-once, idempotent commands to one node.

ESP-NOW PC->node commands are fire-and-forget: a dropped ``set_max_pressure`` /
``set_min_pressure`` leaves the node clamping an inflate to a *stale* safety
limit — the cause of an observed 20->50 kPa over-inflation (see
``docs/ACK_RELIABILITY.md``). This helper tags such a command with a per-node
sequence number, waits for the node's ``{"type":"ack","seq":...,"ok":...}``,
and retransmits the *same* seq on timeout, so the limit reliably lands.

Only *idempotent* commands may be confirmed here — a retransmitted ``inflate``
would stack a second pulse, whereas setting a limit twice is harmless. The node
ACKs *after applying* the command, so an ack means "applied", not merely
"received"; a rejected command (bad chamber) comes back ``ok=false`` (a NACK) so
the caller fails fast instead of retrying.

Framework-agnostic (no Qt). The ack handler runs on the gateway read thread and
only touches a lock-guarded map; :meth:`confirm` blocks the *calling* thread up
to ``(retries + 1) * timeout`` seconds, so run it off the GUI/actuation thread
(``ESP32Controller.confirm_limits`` does that for you).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from src.hardware.gateway import Gateway

logger = logging.getLogger(__name__)

# The PC's per-node sequence wraps within [0, 0xFFFE]; 0xFFFF is the firmware's
# Cmd::seq "no confirmation" sentinel, so this modulus keeps us clear of it.
_SEQ_MODULUS = 0xFFFF


class _Pending:
    """One in-flight confirmation: an event the ack handler sets, plus the ack's
    ``ok`` (True = applied, False = node rejected the command)."""

    __slots__ = ("event", "ok")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.ok = False


class CommandConfirmer:
    """Send-and-confirm for one node's safety-critical, idempotent commands."""

    ACK_TIMEOUT = 0.2   # seconds to wait for an ack before retransmitting
    MAX_RETRIES = 3     # retransmits after the first send (~0.8 s worst case)

    def __init__(self, gateway: Gateway, mac: str) -> None:
        self._gateway = gateway
        self._mac = mac
        self._lock = threading.Lock()
        self._seq = 0
        self._pending: dict[int, _Pending] = {}
        # One persistent handler routes this node's acks into the pending map.
        # The gateway holds it weakly, so it lives exactly as long as this
        # confirmer (owned by the ESP32Controller); it fires on the read thread
        # and only touches the lock-guarded map — no GUI work.
        self._gateway.on_message(self._handle)

    def _next_seq(self) -> int:
        """Allocate the next per-node sequence number ([0, 0xFFFE], never the
        firmware's 0xFFFF sentinel)."""
        with self._lock:
            self._seq = (self._seq + 1) % _SEQ_MODULUS
            return self._seq

    def confirm(self, command: str, *, timeout: float | None = None,
                retries: int | None = None, **fields: Any) -> bool:
        """Send ``command`` and block until the node ACKs it, or give up.

        Retransmits the *same* seq on each ``timeout`` up to ``retries`` times
        (the command must be idempotent). ``fields`` are the command payload
        (e.g. ``chamber=0, value=20.0``). Returns:

        - ``True``  — the node acked ``ok`` (applied);
        - ``False`` — the node NACK'd (rejected; no retry), or no ack arrived
          after exhausting the retries (node likely unreachable).
        """
        timeout = self.ACK_TIMEOUT if timeout is None else timeout
        retries = self.MAX_RETRIES if retries is None else retries
        seq = self._next_seq()
        pending = _Pending()
        with self._lock:
            self._pending[seq] = pending
        try:
            for _ in range(retries + 1):
                self._gateway.send(self._mac, command, seq=seq, **fields)
                if pending.event.wait(timeout):
                    if pending.ok:
                        return True
                    logger.warning("Node %s rejected %s (seq %d) — NACK, not retrying",
                                   self._mac, command, seq)
                    return False
            logger.warning("No ack for %s (seq %d) on %s after %d retries — "
                           "node may be unreachable", command, seq, self._mac, retries)
            return False
        finally:
            with self._lock:
                self._pending.pop(seq, None)

    def _handle(self, data: dict[str, Any]) -> None:
        """Route an incoming ``{"type":"ack",...}`` into the pending map.

        Runs on the gateway read thread. Ignores acks from other nodes, non-ack
        messages, and the legacy seq-less acks (stop/resume) that predate
        confirmation. An ack for an unknown seq (a late duplicate, or one already
        resolved) is harmlessly dropped."""
        if data.get("source") != self._mac or data.get("type") != "ack":
            return
        seq = data.get("seq")
        if seq is None:
            return
        with self._lock:
            pending = self._pending.get(int(seq))
            if pending is None:
                return
            pending.ok = bool(data.get("ok", True))
            pending.event.set()
