"""ThymioLink — drives ONE Thymio's motors/LEDs: one node on a shared RF dongle.

A single RF dongle relays to several Thymios at once (each is one *node id* on the same
wireless network), so the serial connection lives in :class:`ThymioDongle` and a link is
just a thin handle bound to one node id on it. That keeps :class:`ThymioRobot` a plain
domain object — the transport is the injected dongle, the identity is the node id.

Two ways to build one:

* ``ThymioLink(serial_port=...)`` — owns a fresh single-robot dongle (the jog tool / the
  one-Thymio case). ``node_id=None`` binds to whatever node shows up first.
* ``ThymioLink(dongle=shared, node_id=2)`` — shares one dongle across robots, each bound
  to its own node id. The shared dongle is *not* closed when the link closes.

Nothing connects (or imports thymiodirect) until :meth:`connect`, so constructing a link
— and therefore a ``ThymioRobot`` — never needs the dongle or the package.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from src.robots.thymio.thymio_dongle import ThymioDongle

logger = logging.getLogger(__name__)


class ThymioLink:
    """One Thymio (one node id) reached through a shared or owned :class:`ThymioDongle`."""

    def __init__(
        self,
        serial_port: str | None = None,
        dongle: ThymioDongle | None = None,
        node_id: Any = None,
    ):
        # Share an injected dongle, or own a fresh one for the single-robot case.
        self._dongle = dongle if dongle is not None else ThymioDongle(serial_port=serial_port)
        self._owns_dongle = dongle is None
        # `configured` is what the caller asked for (kept across reconnects); `_node_id`
        # is what we're currently bound to (an auto-pick when configured is None).
        self._configured_node_id = node_id
        self._node_id = node_id
        self._active = False       # this link connected? (a shared dongle outlives it)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self, timeout: float = 6.0) -> bool:
        # Bring the dongle up (idempotent when shared with already-connected robots),
        # then wait for *this* robot's node to appear on it.
        if not self._dongle.connect(timeout=timeout):
            return False

        deadline = time.monotonic() + timeout
        while True:
            nodes = self._dongle.nodes
            if self._node_id is None and nodes:
                self._node_id = nodes[0]        # auto: first node discovered
            if self._node_id is not None and self._node_id in nodes:
                self._active = True
                logger.info("Thymio link: bound to node %s", self._node_id)
                return True
            if time.monotonic() >= deadline:
                logger.error("Thymio link: node %r not found (nodes seen: %s) — powered "
                             "ON, paired, and node id correct?", self._configured_node_id,
                             nodes)
                return False
            time.sleep(0.1)

    def close(self) -> None:
        self._active = False
        # Only tear down the dongle if we own it — a shared dongle outlives its links.
        if self._owns_dongle:
            self._dongle.close()
        self._node_id = self._configured_node_id   # drop any auto-picked binding

    @property
    def connected(self) -> bool:
        return (self._active and self._node_id is not None
                and self._node_id in self._dongle.nodes)

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    def set_motors(self, left: int, right: int) -> bool:
        return self._set({"motor.left.target": int(left),
                          "motor.right.target": int(right)})

    def set_leds(self, r: int, g: int, b: int) -> bool:
        return self._set({"leds.top": [int(r), int(g), int(b)]})

    def stop(self) -> bool:
        return self.set_motors(0, 0)

    def _set(self, variables: dict[str, Any]) -> bool:
        if not self._active or self._node_id is None:
            return False
        return self._dongle.write(self._node_id, variables)
