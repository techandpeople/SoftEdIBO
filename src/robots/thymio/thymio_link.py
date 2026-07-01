"""ThymioLink — drives a Thymio's motors/LEDs over tdmclient on a background thread.

The Thymio's wheeled base is reached through a **Thymio Device Manager (TDM)** —
Thymio Suite running locally with an RF dongle, or a remote TDM over the network.
tdmclient talks to the TDM; this class owns that connection so ``ThymioRobot`` stays
a plain domain object (collaborator injection — the transport lives here).

tdmclient is not native asyncio: it exposes generator-style coroutines that a driver
steps with ``co.send(None)``. Its own ``run_async_program`` busy-drives that loop,
which would peg a core for a long-lived session, so we step the coroutine ourselves
with a small ``time.sleep`` between steps. Commands arrive from any thread via a
queue and are coalesced and flushed inside the session coroutine (so a burst of motor
updates only sends the latest value).

Nothing here connects until :meth:`connect` is called, so constructing a link (and
therefore a ``ThymioRobot``) never needs a TDM — the sim / no-hardware path is
untouched.
"""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from tdmclient import ClientAsync

logger = logging.getLogger(__name__)


class ThymioLink:
    """Owns a tdmclient session to one Thymio and accepts thread-safe commands."""

    _STEP_S = 0.003          # pacing between coroutine steps (keeps CPU low)
    _IDLE_YIELD_S = 0.02     # how often the session loop yields when idle

    def __init__(self, host: str | None = None, port: int | None = None):
        self._kwargs: dict[str, Any] = {}
        if host:
            self._kwargs["tdm_addr"] = host
        if port:
            self._kwargs["tdm_port"] = int(port)

        self._queue: "queue.Queue[dict[str, list[int]]]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._client: ClientAsync | None = None
        self._connected = threading.Event()
        self._stop = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self, timeout: float = 5.0) -> bool:
        """Start the session thread and wait until a Thymio is locked."""
        if self._thread and self._thread.is_alive():
            return self._connected.is_set()
        self._stop = False
        self._connected.clear()
        self._thread = threading.Thread(target=self._run, name="thymio-link", daemon=True)
        self._thread.start()
        return self._connected.wait(timeout)

    def close(self) -> None:
        self._stop = True
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        self._connected.clear()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    # ------------------------------------------------------------------
    # Commands (safe to call from any thread; queued for the session loop)
    # ------------------------------------------------------------------

    def set_motors(self, left: int, right: int) -> bool:
        return self._push({"motor.left.target": [int(left)],
                           "motor.right.target": [int(right)]})

    def set_leds(self, r: int, g: int, b: int) -> bool:
        return self._push({"leds.top": [int(r), int(g), int(b)]})

    def stop(self) -> bool:
        return self.set_motors(0, 0)

    def _push(self, variables: dict[str, list[int]]) -> bool:
        self._queue.put(variables)
        return True

    def _drain(self) -> dict[str, list[int]] | None:
        """Coalesce every queued command into one variable dict (latest wins)."""
        merged: dict[str, list[int]] = {}
        try:
            while True:
                merged.update(self._queue.get_nowait())
        except queue.Empty:
            pass
        return merged or None

    # ------------------------------------------------------------------
    # Background session
    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._client = ClientAsync(**self._kwargs)
        except Exception:
            logger.exception("Thymio link: could not create tdmclient")
            return

        co = self._session()
        try:
            while True:
                try:
                    co.send(None)          # step the tdmclient coroutine
                except StopIteration:
                    break
                threading.Event().wait(self._STEP_S)  # pace; keeps CPU low
        except Exception:
            logger.exception("Thymio link: session error")
        finally:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
            self._connected.clear()

    async def _session(self) -> None:
        node = await self._client.wait_for_node(timeout=4.0)
        if node is None:
            logger.error("Thymio link: no Thymio found (dongle plugged in? robot paired?)")
            return
        await node.lock()
        logger.info("Thymio link: locked node %s", node.id_str)
        self._connected.set()
        try:
            while not self._stop:
                cmd = self._drain()
                if cmd is not None:
                    await node.set_variables(cmd)
                await self._client.sleep(self._IDLE_YIELD_S)
        finally:
            try:
                await node.set_variables({"motor.left.target": [0], "motor.right.target": [0]})
                await node.unlock()
            except Exception:
                pass
