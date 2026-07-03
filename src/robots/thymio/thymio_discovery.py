"""Discover the 802.15.4 short addresses of the Thymios on the air.

The gateway's C6 can sniff (promiscuous 802.15.4); :func:`discover_thymios` turns that into
a one-call "which Thymios are out there" so robots can be swapped between studies without
hand-editing addresses. It sniffs a channel for a few seconds and returns the distinct
Thymio short addresses seen on the Thymio network (PAN 0x4481), as hex strings (e.g.
``["6a25", ...]``) — exactly the ``address`` a
:class:`~src.robots.thymio.thymio_gateway_link.ThymioGatewayLink` wants.

The Thymios must be **transmitting** while we sniff — drive them with the RF dongle, or just
power them on (a Wireless Thymio emits frames looking for its coordinator). Each frame on
PAN 0x4481 carries the Thymio address as whichever of its 802.15.4 dst/src is not the host
(0x3237) or broadcast.
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
    """Sniff `channel` via the gateway's C6 and return the Thymio addresses seen.

    Returns hex strings like ``["6a25", ...]`` in FIRST-SEEN order — so powering
    robots on one at a time while scanning tells you which address is which. The
    Thymios must be transmitting: a Wireless Thymio announces itself when powered
    on, so switching it on (or off/on) during the scan is enough — no dongle.
    Leaves the C6 back in plain-RCP mode.

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
        addr = _frame_addr(direction, text)
        if addr is None or addr in found:
            return
        found[addr] = None
        _notify_found(on_found, addr)

    gateway.on_raw(_on_raw)
    try:
        gateway.send("thymio", "sniff_start", ch=channel)
        _wait(secs, stop)
        gateway.send("thymio", "sniff_stop")
        time.sleep(0.2)                              # let the stop reply drain
    finally:
        gateway.remove_raw_callback(_on_raw)

    addrs = [f"{a:04x}" for a in found]              # first-seen order
    logger.info("Thymio discovery on ch%d: %s", channel, addrs or "(none seen)")
    return addrs
