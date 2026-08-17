"""Request a node's high-cadence status telemetry for the duration of a task.

The actuator firmware normally broadcasts chamber ``status`` every 500 ms, which
is too coarse for a fill-calibration sweep, a touch-coupling sweep, or a smooth
live pressure gauge. A ``status_rate`` command raises that cadence for a bounded,
keepalive-refreshed window (firmware ``commands::setStatusRate``): the node
reverts to the default once the window (``ttl``) lapses, so a lost "off" can never
leave fast telemetry running - the same dead-man idea as the bench-test keepalive.

:class:`FastTelemetry` owns that request. Drive it from a timer/loop::

    ft = FastTelemetry(gateway, mac)
    ft.start()
    ...                    # each timer tick, well within ttl:
    ft.keepalive()         # refresh the firmware dead-man (idempotent)
    ...
    ft.stop()

or as a context manager for a short, self-contained burst::

    with FastTelemetry(gateway, mac):
        run_short_sweep()

Board-agnostic and Qt-free (the gateway is injected), so it is reused by the fill
calibration dialog, the touch-coupling collector, and the live monitor panels.
Sending ``status_rate`` to a node whose firmware predates it is a harmless no-op.
"""

from __future__ import annotations

from typing import Any

# Default fast cadence (ms) and keepalive window (ms). ``ttl`` is generous versus a
# ~1 Hz keepalive so a single dropped frame does not bounce the node back to the
# 500 ms default; the firmware floors the interval (MIN_STATUS_MS) so this can
# never flood the radio.
DEFAULT_RATE_MS = 40
DEFAULT_TTL_MS = 3000


class FastTelemetry:
    """Holds a node in high-cadence status mode while a task needs dense pressure."""

    def __init__(self, gateway: Any, mac: str, *,
                 rate_ms: int = DEFAULT_RATE_MS,
                 ttl_ms: int = DEFAULT_TTL_MS) -> None:
        self._gateway = gateway
        self._mac = mac
        self._rate_ms = max(1, int(rate_ms))
        self._ttl_ms = max(1, int(ttl_ms))
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> bool:
        """Turn on fast telemetry for this node. Idempotent."""
        self._active = True
        return self._request(self._rate_ms, self._ttl_ms)

    def keepalive(self) -> bool:
        """Re-send the request to refresh the firmware ttl dead-man.

        Call this periodically (well within ``ttl_ms``) from the consumer's timer
        so a long sweep does not lapse back to the 500 ms default mid-run. A no-op
        when not :attr:`active`."""
        if not self._active:
            return False
        return self._request(self._rate_ms, self._ttl_ms)

    def stop(self) -> bool:
        """Revert the node to its default cadence. Idempotent (no-op if inactive)."""
        if not self._active:
            return False
        self._active = False
        return self._request(0, 0)

    def _request(self, ms: int, ttl: int) -> bool:
        if self._gateway is None:
            return False
        return bool(self._gateway.send(self._mac, "status_rate",
                                       ms=int(ms), ttl=int(ttl)))

    def __enter__(self) -> "FastTelemetry":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.stop()
        return False
