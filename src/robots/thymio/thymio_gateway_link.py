"""ThymioGatewayLink — drive a Thymio through the gateway's C6, with NO RF dongle.

Same duck-typed interface as :class:`~src.robots.thymio.thymio_link.ThymioLink`
(``connect`` / ``close`` / ``set_motors`` / ``set_leds`` / ``connected``), so
:class:`~src.robots.thymio.thymio_robot.ThymioRobot` neither knows nor cares which
transport it was handed. Where ``ThymioLink`` talks to the RF dongle via thymiodirect,
this one drives the Thymio over **802.15.4 through the gateway's C6**:

* ``connect`` turns on the C6 firmware's continuous ``thymio_link`` — the C6 then polls
  the Thymio at ~10 Hz on its own to hold its receive window open (the dongle's job),
* ``set_motors`` / ``set_leds`` just push the held targets (``thymio_drive`` /
  ``thymio_leds``) — instant, the C6 keeps re-asserting them on every poll,
* ``close`` turns the link off (poller stops, motors to 0).

Requires the S3 gateway (`Gateway`) with its C6 running the leak-fixed ``rcp_c6``
(the C6's chip antenna is plenty — solid at 20 m through a door; no external
antenna needed). One C6 drives up to 4 Thymios: each robot is a **slot** (``index``)
on the C6, addressed by its 802.15.4 short ``address`` (e.g. ``"6a25"``). Give each
gateway-driven Thymio a distinct ``index`` + ``address`` (discover the addresses via the
sniffer — see :func:`discover_thymios`). ``address=None`` on ``index=0`` uses the C6's
built-in default (the first Thymio we decoded), so the single-Thymio flow needs no address.
"""
from __future__ import annotations

import logging

from src.hardware.gateway import Gateway

logger = logging.getLogger(__name__)


class ThymioGatewayLink:
    """One Thymio driven dongle-free through the gateway's C6 (802.15.4)."""

    def __init__(self, gateway: Gateway | None, channel: int = 25,
                 index: int = 0, address: str | None = None):
        self._gateway = gateway
        self._channel = int(channel)
        self._index = int(index)
        self._address = address        # hex short address ("6a25"), or None → C6 default
        self._active = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self, timeout: float = 6.0) -> bool:
        # `timeout` is accepted for interface parity with ThymioLink; there is no
        # discovery handshake here — the C6 starts polling as soon as we ask.
        if self._gateway is None or not self._gateway.is_connected:
            logger.error("Thymio gateway link: gateway not connected — can't reach the C6")
            return False
        ok = self._gateway.send("thymio", "thymio_link", on=True, ch=self._channel)
        # Register this robot's address in its slot. Skip only for the address-less
        # slot 0, which rides the C6's built-in default (single-Thymio back-compat).
        if ok and (self._address is not None or self._index != 0):
            ok = self._gateway.send("thymio", "thymio_set", idx=self._index,
                                    addr=self._address or "6a25") and ok
        self._active = ok
        if ok:
            logger.info("Thymio gateway link: C6 link on (ch %d, slot %d, addr %s)",
                        self._channel, self._index, self._address or "default")
        else:
            logger.error("Thymio gateway link: failed to start the C6 link")
        return ok

    def close(self) -> None:
        # Zero this robot's motors; the shared poller stays up for the other robots.
        if self._active and self._gateway is not None and self._gateway.is_connected:
            self._gateway.send("thymio", "thymio_drive", idx=self._index, left=0, right=0)
        self._active = False

    @property
    def connected(self) -> bool:
        return (self._active and self._gateway is not None
                and self._gateway.is_connected)

    # ------------------------------------------------------------------
    # Commands (the C6 holds these and re-asserts them every poll)
    # ------------------------------------------------------------------

    def set_motors(self, left: int, right: int) -> bool:
        if not self.connected:
            return False
        return self._gateway.send("thymio", "thymio_drive", idx=self._index,
                                  left=int(left), right=int(right))

    def set_leds(self, r: int, g: int, b: int) -> bool:
        if not self.connected:
            return False
        return self._gateway.send("thymio", "thymio_leds", idx=self._index,
                                  r=int(r), g=int(g), b=int(b))

    def stop(self) -> bool:
        return self.set_motors(0, 0)
