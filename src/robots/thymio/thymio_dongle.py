"""ThymioDongle — one thymiodirect connection to the RF dongle, shared by all Thymios.

The wireless Aseba network carries several Thymios over a single dongle/radio: each
robot is one *node id* on the same channel + network id, and thymiodirect discovers
them all through one serial connection. This class owns that single connection and
relays reads/writes to a chosen node, so several per-robot :class:`ThymioLink` objects
can share one dongle instead of each trying (and failing) to open the same port.

Nothing here imports thymiodirect until :meth:`connect` runs, so building a dongle — and
therefore a ThymioLink / ThymioRobot — never needs the package or the hardware; the sim
/ no-hardware path is untouched.

thymiodirect has no public ``disconnect``: ``Thymio.connect()`` spawns a *non-daemon*
thread running an asyncio loop plus a serial-reader thread, neither of which stops on
its own. :meth:`close` reaches into the proxy to stop both, otherwise the app hangs on
exit once a dongle has ever been opened.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

logger = logging.getLogger(__name__)


class ThymioDongle:
    """Owns one thymiodirect connection to the RF dongle; relays to Thymios by node id."""

    def __init__(self, serial_port: str | None = None):
        # None → auto-detect the dongle by its Mobsya USB id on connect.
        self._serial_port = serial_port or None
        self._th: Any = None            # thymiodirect.Thymio
        self._lock = threading.Lock()   # guards connect + shared writes across robots

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    @staticmethod
    def find_dongle() -> str | None:
        """Serial port of the Thymio wireless dongle (Mobsya VID 0x0617), or None.

        Picks it out by USB id/name so we never grab the ESP gateway sitting on an
        adjacent ``/dev/ttyACM*`` (thymiodirect would then choke decoding its JSON as
        Aseba).
        """
        try:
            from serial.tools import list_ports
        except ImportError:
            return None
        for p in list_ports.comports():
            desc = f"{p.description or ''} {p.product or ''} {p.manufacturer or ''}".lower()
            if p.vid == 0x0617 or "thymio" in desc or "mobsya" in desc:
                return p.device
        return None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self, timeout: float = 6.0) -> bool:
        """Open the dongle and wait for at least one node. Idempotent + thread-safe.

        The first caller opens the serial port and blocks (off-thread) until the dongle
        reports a node; later callers (other robots' links sharing us) return at once.
        More nodes keep appearing asynchronously after this returns, so each link waits
        for its own node id in :meth:`ThymioLink.connect`.
        """
        with self._lock:
            if self._th is not None:
                return True

            try:
                from thymiodirect import Thymio
            except ImportError:
                logger.error("thymiodirect not installed — run: pip install thymiodirect")
                return False

            port = self._serial_port or self.find_dongle()
            if port is None:
                logger.error("Thymio dongle: no RF dongle found "
                             "(plugged in? or pass serial_port=)")
                return False

            try:
                th = Thymio(serial_port=port, refreshing_rate=0.1)
            except Exception:
                logger.exception("Thymio dongle: could not open %s", port)
                return False

            # Thymio.connect() blocks until a node appears, so run it off-thread and give
            # up after `timeout` if the dongle reports nothing (none powered / paired).
            ready = threading.Event()

            def _do_connect() -> None:
                try:
                    th.connect()
                    ready.set()
                except Exception:
                    logger.exception("Thymio dongle: connect failed")

            threading.Thread(target=_do_connect, name="thymio-dongle-connect",
                             daemon=True).start()
            if not ready.wait(timeout):
                logger.error("Thymio dongle: no Thymio discovered on %s within %.0fs — is "
                             "any Thymio powered ON and paired with this dongle?",
                             port, timeout)
                self._stop_thymio(th)
                return False

            self._th = th
            self._serial_port = port
            logger.info("Thymio dongle: connected on %s, nodes=%s", port, self.nodes)
            return True

    def close(self) -> None:
        with self._lock:
            th, self._th = self._th, None
        if th is not None:
            self._stop_thymio(th)

    @staticmethod
    def _stop_thymio(th: Any) -> None:
        """Best-effort clean shutdown of a thymiodirect Thymio (it has no ``disconnect``).

        Stops the serial-reader thread and the asyncio loop so the non-daemon thread
        that ``Thymio.connect()`` spawned actually exits — otherwise the app hangs on
        exit. All private-attribute access is defensive: thymiodirect may change.
        """
        proxy = getattr(th, "thymio_proxy", None)
        if proxy is None:
            return
        conn = getattr(proxy, "connection", None)
        if conn is not None:
            for stop in ("shutdown", "close"):     # terminate reader, then close io
                try:
                    getattr(conn, stop)()
                except Exception:
                    pass
        loop = getattr(proxy, "loop", None)
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)   # break run_forever()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._th is not None

    @property
    def nodes(self) -> list:
        """Node ids of every Thymio the dongle currently sees (may grow after connect)."""
        th = self._th
        if th is None:
            return []
        try:
            return sorted(th.nodes())          # a set → stable order for display / pick
        except TypeError:
            return list(th.nodes())            # unsortable ids: keep whatever order
        except Exception:
            return []

    # ------------------------------------------------------------------
    # I/O to a specific node
    # ------------------------------------------------------------------

    def write(self, node_id: Any, variables: dict[str, Any]) -> bool:
        """Set native variables on one node. Thread-safe across robots sharing us."""
        th = self._th
        if th is None or node_id is None:
            return False
        try:
            with self._lock:
                node = th[node_id]
                for name, value in variables.items():
                    node[name] = value
            return True
        except Exception:
            logger.exception("Thymio dongle: failed to set %s on node %s",
                             variables, node_id)
            return False

    def play_sound(self, node_id: Any, *, system: int | None = None,
                   freq: int | None = None, dur60: int = 30) -> bool:
        """Play a beep on one node by loading + running a tiny Aseba program.

        A ``system`` sound (0-7, -1 stops) or a ``freq`` Hz tone (``dur60`` in 1/60 s).
        Loading bytecode leaves the node's variables (motor targets) untouched.
        """
        th = self._th
        if th is None or node_id is None:
            return False
        # event.args (@2, 32-word scratch) holds the native-call args by address.
        if freq is not None:
            asm = (f"\tdc end_toc\n\tdc _ev.init, init\nend_toc:\ninit:\n"
                   f"\tpush {int(freq)}\n\tstore 2\n\tpush {int(dur60)}\n\tstore 3\n"
                   f"\tpush 2\n\tpush 3\n\tcallnat _nf.sound.freq\n\tstop\n")
        else:
            asm = (f"\tdc end_toc\n\tdc _ev.init, init\nend_toc:\ninit:\n"
                   f"\tpush {int(system if system is not None else 0)}\n\tstore 2\n"
                   f"\tpush 2\n\tcallnat _nf.sound.system\n\tstop\n")
        try:
            with self._lock:
                th.run_asm(node_id, asm)
            return True
        except Exception:
            logger.exception("Thymio dongle: failed to play sound on node %s", node_id)
            return False
