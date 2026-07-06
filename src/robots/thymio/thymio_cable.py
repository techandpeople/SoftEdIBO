"""Read a Wireless Thymio's RF address straight off its USB cable.

A brand-new Thymio isn't on our 802.15.4 network yet, so wireless
:func:`~src.robots.thymio.thymio_discovery.discover_thymios` can't see it. But
plugging it into the PC with its micro-USB cable exposes it as a serial port
speaking Aseba (via ``thymiodirect``), and its **Aseba node id** is the same two
bytes as its **802.15.4 short address**, byte-swapped — so a cable read gives the
exact ``address`` the Gateway-C6 transport wants, with no dongle and no Thymio
Suite. Confirmed live: node id ``0x256a`` ⇒ address ``6a25``.

Domain-only (no Qt): the GUI runs :func:`read_cabled_thymio_address` off-thread
and drops the result into the address field.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

# ``thymiodirect`` has no clean disconnect: closing the port yanks it from under a
# blocked reader thread and a periodic asyncio refresh, which then dump tracebacks
# to stderr. They are harmless teardown races (the read still succeeds and the port
# frees), so we swallow just those, and only during the short teardown window.
_suppress_teardown = threading.Event()
_prev_excepthook = threading.excepthook


def _quiet_excepthook(args: threading.ExceptHookArgs) -> None:
    if _suppress_teardown.is_set():
        return
    _prev_excepthook(args)


threading.excepthook = _quiet_excepthook

# USB identity of a Wireless Thymio-II robot (Mobsya). The RF *dongle* shares the
# vendor id but a different product id (0x000c), so we match the robot exactly and
# never mistake the dongle for a robot.
_THYMIO_VID = 0x0617
_THYMIO_ROBOT_PID = 0x000A


class ThymioCableError(Exception):
    """No cabled Thymio, or it didn't answer over USB."""


def address_from_node_id(node_id: int) -> str:
    """The 802.15.4 short address (hex, e.g. ``"6a25"``) for an Aseba ``node_id``.

    The address is the node id's two bytes swapped: ``0x256a`` ⇒ ``0x6a25``.
    """
    swapped = ((node_id & 0xFF) << 8) | ((node_id >> 8) & 0xFF)
    return f"{swapped:04x}"


def find_cabled_thymio_port() -> str | None:
    """The serial port of a USB-cabled Thymio-II robot, or ``None``.

    Ignores the RF dongle (same vendor, product id 0x000c).
    """
    from serial.tools import list_ports

    for p in list_ports.comports():
        if p.vid == _THYMIO_VID and p.pid == _THYMIO_ROBOT_PID:
            return p.device
    return None


def _shutdown(thymio) -> None:
    """Release the serial port + reader thread of a ``thymiodirect`` ``Thymio``.

    ``thymiodirect`` has no ``disconnect``; the port and its non-daemon reader
    thread live on the ``Connection``, reached lazily once ``connect()`` starts.
    """
    proxy = getattr(thymio, "thymio_proxy", None)
    conn = getattr(proxy, "connection", None) if proxy is not None else None
    if conn is None:
        return
    # Mute the reader thread / asyncio-refresh teardown races while we close.
    _suppress_teardown.set()
    asyncio_log = logging.getLogger("asyncio")
    prev_level = asyncio_log.level
    asyncio_log.setLevel(logging.CRITICAL)
    for method in ("shutdown", "close"):
        try:
            getattr(conn, method)()
        except Exception:  # noqa: BLE001 — teardown must not raise
            logger.debug("thymiodirect %s() failed", method, exc_info=True)
    # Clear the mute after the teardown settles (late reader/refresh errors too),
    # off a timer so we don't block the caller.
    threading.Timer(1.5, lambda: (_suppress_teardown.clear(),
                                  asyncio_log.setLevel(prev_level))).start()


def read_cabled_thymio_address(port: str | None = None,
                               timeout: float = 8.0) -> str:
    """Read a USB-cabled Thymio's 802.15.4 short address (hex, e.g. ``"6a25"``).

    Args:
        port: Serial port; auto-detected (first cabled Thymio) when ``None``.
        timeout: Seconds to wait for the robot to answer the Aseba handshake.

    Raises:
        ThymioCableError: no cabled Thymio, or it didn't answer in ``timeout``.
    """
    port = port or find_cabled_thymio_port()
    if port is None:
        raise ThymioCableError(
            "No Thymio found on USB — connect it with its micro-USB cable "
            "(and make sure Thymio Suite isn't holding the port).")

    from thymiodirect import Thymio

    # thymiodirect's Thymio() constructor calls asyncio.get_event_loop(), which on
    # Python 3.12 raises in any thread without one — and the GUI runs this on a Qt
    # worker thread (run_async), not the main thread. Ensure this thread has a loop
    # set first so that call succeeds. Intentionally probing for a loop, so mute the
    # get_event_loop() deprecation noise that Python emits when it auto-creates one.
    import asyncio
    import warnings
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            policy = asyncio.get_event_loop_policy()
            try:
                policy.get_event_loop()
            except RuntimeError:
                policy.set_event_loop(policy.new_event_loop())

    node_ids: list[int] = []
    thymio = Thymio(serial_port=port, on_connect=node_ids.append)
    # ``Thymio.connect()`` blocks until a node answers, so a silent/wrong device
    # would hang forever — run it on a daemon thread and bound the wait ourselves.
    threading.Thread(target=thymio.connect, daemon=True).start()
    deadline = time.monotonic() + timeout
    while not node_ids and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        if not node_ids:
            raise ThymioCableError(
                f"The device on {port} didn't answer as a Thymio within "
                f"{timeout:.0f}s. Power-cycle it and try again.")
        node_id = node_ids[0]
        addr = address_from_node_id(node_id)
        logger.info("Cabled Thymio on %s: node id 0x%04x → address %s",
                    port, node_id, addr)
        return addr
    finally:
        _shutdown(thymio)
