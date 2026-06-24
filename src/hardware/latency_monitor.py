"""Round-trip latency monitor for ESP-NOW nodes via the gateway.

Sends a ``ping`` to a node and times the ``pong`` the gateway forwards back
(tagged with the node's ``source`` MAC). The latest round-trip time per MAC is
kept and reported through callbacks registered with :meth:`on_update`.

Qt-free by design — the ``hardware`` layer stays GUI-agnostic. Callbacks fire on
the gateway's serial read thread, so GUI consumers must marshal to the GUI thread
(e.g. via a Qt signal), exactly like the ESP32 sensor callbacks.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from src.hardware.espnow_gateway import ESPNowGateway

logger = logging.getLogger(__name__)


class LatencyMonitor:
    """Measures round-trip latency to each ESP-NOW node through the gateway."""

    def __init__(self, gateway: ESPNowGateway, timeout_ms: int = 2500):
        self._gateway = gateway
        self._timeout_s = timeout_ms / 1000.0
        self._pending: dict[str, float] = {}        # mac -> monotonic send time
        self._latency: dict[str, float | None] = {}  # mac -> last RTT (ms); None = stale
        self._update_callbacks: list[Callable[[str, float | None], None]] = []
        self._gateway.on_message(self._handle_message)

    def on_update(self, callback: Callable[[str, float | None], None]) -> None:
        """Register a callback ``(mac, rtt_ms)`` invoked on each new measurement.

        ``rtt_ms`` is ``None`` when a node that had been answering stops doing so
        (its outstanding ping timed out).
        """
        self._update_callbacks.append(callback)

    def latency_ms(self, mac: str) -> float | None:
        """Last measured round-trip latency for ``mac`` in ms (None if unknown/stale)."""
        return self._latency.get(mac)

    def ping(self, mac: str) -> None:
        """Send a timed ping to one node; its ``pong`` reply updates the latency.

        If a ping to this MAC is already outstanding, its send time is kept so the
        timeout still fires — pings are spaced far wider than a healthy RTT, so a
        single outstanding ping per node is the normal case.
        """
        if not mac or not self._gateway.is_connected:
            return
        self._pending.setdefault(mac, time.monotonic())
        self._gateway.send(mac, "ping")

    def sweep(self) -> None:
        """Expire outstanding pings older than the timeout, reporting them stale.

        Call this periodically alongside :meth:`ping` so a node that goes offline
        reverts from a stale number to "no reply" instead of freezing at its last
        good value.
        """
        now = time.monotonic()
        for mac, sent in list(self._pending.items()):
            if now - sent > self._timeout_s:
                del self._pending[mac]
                if self._latency.get(mac) is not None:
                    self._latency[mac] = None
                    self._notify(mac, None)

    def reset(self) -> None:
        """Forget all measurements (e.g. when the gateway link drops)."""
        self._pending.clear()
        self._latency.clear()

    def _handle_message(self, data: dict[str, Any]) -> None:
        if data.get("type") != "pong":
            return
        mac = data.get("source")
        sent = self._pending.pop(mac, None)
        if sent is None:
            return
        rtt = (time.monotonic() - sent) * 1000.0
        self._latency[mac] = rtt
        self._notify(mac, rtt)

    def _notify(self, mac: str, rtt: float | None) -> None:
        """Fan out a measurement, pruning callbacks whose Qt source was deleted."""
        dead: list[int] = []
        for i, callback in enumerate(self._update_callbacks):
            try:
                callback(mac, rtt)
            except RuntimeError:
                dead.append(i)
        for i in reversed(dead):
            self._update_callbacks.pop(i)
