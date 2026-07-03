"""Discover the 802.15.4 short addresses of the Thymios on the air.

:func:`discover_thymios` is a one-call "which Thymios are out there" so robots can be swapped
between studies without hand-editing addresses. It returns the distinct Thymio short addresses
on the Thymio network (PAN 0x4481), as hex strings (e.g. ``["6a25", ...]``) — exactly the
``address`` a :class:`~src.robots.thymio.thymio_gateway_link.ThymioGatewayLink` wants.

Discovery is **active**, exactly how the RF dongle enumerates: the gateway's C6 broadcasts an
Aseba ``LIST_NODES`` and every powered Thymio replies with a ``NODE_PRESENT`` frame carrying
its address (the C6 ``thymio_discover`` command; it emits a ``thymio_found`` line per reply).
This matters because a **Wireless Thymio does NOT announce itself at boot** — a passive sniff
sees an idle robot only if something else makes it transmit. The broadcast makes even an idle,
powered robot answer, so no dongle and no driving are needed.

Only robots already on this network (PAN 0x4481, same channel as our C6/dongle) reply; a fresh
robot must be paired onto the network once (via the dongle or a USB cable) before it shows up.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_PAN_THYMIO = 0x4481        # the Thymio dongle network
_HOST_ADDR  = 0x3237        # the dongle/host short address (never a robot)
_BROADCAST  = 0xFFFF


def parse_thymio_addr(data_hex: str) -> int | None:
    """The Thymio's 16-bit short address from one sniffed frame's hex, or None.

    Frame: ``FCF(2) seq(1) PAN(2) dst(2) src(2) …`` — all little-endian. Returns the address
    only for a data frame on PAN 0x4481 whose dst or src is a real robot (not the host, not
    broadcast). Too-short frames (e.g. bare ACKs) and other networks return None.
    """
    try:
        b = bytes.fromhex(data_hex)
    except ValueError:
        return None
    if len(b) < 9:                                   # no addressing fields (e.g. an ACK)
        return None
    if (b[3] | (b[4] << 8)) != _PAN_THYMIO:          # PAN id (little-endian)
        return None
    dst = b[5] | (b[6] << 8)
    src = b[7] | (b[8] << 8)
    for addr in (dst, src):
        if addr not in (_HOST_ADDR, _BROADCAST):
            return addr
    return None


def _frame_addr(direction: str, text: str) -> int | None:
    """The Thymio address in one raw gateway line, or None (non-frame lines)."""
    if direction != "rx":
        return None
    try:
        msg = json.loads(text)
    except (ValueError, TypeError):
        return None
    if msg.get("type") != "frame":
        return None
    return parse_thymio_addr(msg.get("data", ""))


def _found_addr(direction: str, text: str) -> int | None:
    """The Thymio address in a C6 ``thymio_found`` line, or None (other lines).

    The C6's active discovery reports each replying robot as
    ``{"type":"thymio_found","addr":"6a25"}``; ``addr`` is the robot's short address.
    """
    if direction != "rx":
        return None
    try:
        msg = json.loads(text)
    except (ValueError, TypeError):
        return None
    if msg.get("type") != "thymio_found":
        return None
    try:
        addr = int(str(msg.get("addr", "")), 16)
    except ValueError:
        return None
    return addr if addr not in (_HOST_ADDR, _BROADCAST) else None


def _notify_found(on_found: Any, addr: int) -> None:
    if on_found is None:
        return
    try:
        on_found(f"{addr:04x}")
    except Exception:   # noqa: BLE001 — a bad listener must not kill the scan
        logger.exception("Thymio discovery on_found callback failed")


def _wait(secs: float, stop: Any) -> None:
    """Sleep up to ``secs``, waking early when ``stop`` (an Event) is set."""
    deadline = time.monotonic() + secs
    while time.monotonic() < deadline:
        if stop is not None and stop.is_set():
            return
        time.sleep(0.1)


def discover_thymios(gateway: Any, channel: int = 25, secs: float = 6.0,
                     on_found: Any = None, stop: Any = None) -> list[str]:
    """Actively discover the Thymios on `channel` via the gateway's C6.

    Broadcasts ``LIST_NODES`` through the C6 (``thymio_discover``) so every powered
    robot on the network replies with its address, and returns them as hex strings
    like ``["6a25", ...]`` in FIRST-SEEN order — so powering robots on one at a time
    while scanning tells you which address is which. No dongle, and the robots need
    not be driven: even an idle, powered Thymio answers the broadcast. Leaves the C6
    back in plain-RCP mode.

    Also accepts the legacy passive ``frame`` lines, so an old C6 firmware still
    yields any robots that happen to be transmitting.

    Args:
        on_found: Optional ``callback(addr_hex)`` fired the moment a NEW address
            is seen. Runs on the gateway's serial reader thread — GUI callers
            must bridge through a signal.
        stop: Optional ``threading.Event``-like; set it to end the scan early.
    """
    if gateway is None or not getattr(gateway, "is_connected", False):
        logger.error("Thymio discovery: gateway not connected")
        return []

    found: dict[int, None] = {}

    def _on_raw(direction: str, text: str) -> None:
        addr = _found_addr(direction, text)
        if addr is None:
            addr = _frame_addr(direction, text)     # legacy/passive fallback
        if addr is None or addr in found:
            return
        found[addr] = None
        _notify_found(on_found, addr)

    gateway.on_raw(_on_raw)
    try:
        gateway.send("thymio", "thymio_discover", on=True, ch=channel)
        _wait(secs, stop)
        gateway.send("thymio", "thymio_discover", on=False)
        time.sleep(0.2)                              # let the stop reply drain
    finally:
        gateway.remove_raw_callback(_on_raw)

    addrs = [f"{a:04x}" for a in found]              # first-seen order
    logger.info("Thymio discovery on ch%d: %s", channel, addrs or "(none seen)")
    return addrs
