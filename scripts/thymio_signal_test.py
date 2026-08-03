#!/usr/bin/env python3
"""Measure a gateway's RF signal quality to a Thymio — for comparing gateways/antennas.

Runs the C6's active discovery (LIST_NODES broadcast at 10 Hz; every powered Thymio
replies with NODE_PRESENT) with `thymio_rx_debug` on, so each reply frame arrives as a
`thymio_rx` line carrying the C6-measured **RSSI**. Two numbers come out per robot:

  * reply rate — replies/s vs the 10/s broadcast cadence. A reply needs our broadcast to
    REACH the robot and its answer to reach us, so this is round-trip link reliability.
  * RSSI stats — signal strength (dBm) of the robot's frames at the gateway's antenna.

Run once per gateway (plug one at a time), then compare:

    python scripts/thymio_signal_test.py --secs 20 --json /tmp/gw_a.json
    # swap gateways...
    python scripts/thymio_signal_test.py --secs 20 --json /tmp/gw_b.json

The C6 is rebooted first (its 15.4 RX dies cumulatively across link/discover sessions —
see thymio_discovery.reboot_c6), so every run starts from a clean radio. Works both via
the S3 gateway (relays the C6's lines) and on a solo-flashed C6.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time

PAN_THYMIO = 0x4481
HOST_ADDR = 0x3237
BROADCAST = 0xFFFF
DISCOVER_HZ = 10.0          # the C6 broadcasts LIST_NODES every 100 ms


def find_gateway(explicit: str | None) -> tuple[str | None, str | None]:
    """(port, usb_serial) of the gateway — usb_serial uniquely identifies the board."""
    from serial.tools import list_ports
    for p in list_ports.comports():
        desc = f"{p.description or ''} {p.manufacturer or ''}".lower()
        if (explicit and p.device == explicit) or (
                not explicit and (p.vid == 0x303A or "espressif" in desc or "xiao" in desc)):
            return p.device, p.serial_number
    return (explicit, None) if explicit else (None, None)


def frame_src(data_hex: str) -> int | None:
    """The robot short address a raw frame came FROM, or None (ACKs, other PANs, us)."""
    try:
        b = bytes.fromhex(data_hex)
    except ValueError:
        return None
    if len(b) < 9:                                   # no addressing (e.g. a bare ACK)
        return None
    if (b[3] | (b[4] << 8)) != PAN_THYMIO:
        return None
    src = b[7] | (b[8] << 8)
    return src if src not in (HOST_ADDR, BROADCAST) else None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gateway", help="serial port (default: auto-detect Espressif)")
    p.add_argument("--ch", type=int, default=25, help="802.15.4 channel (default 25)")
    p.add_argument("--secs", type=float, default=20.0, help="measurement duration")
    p.add_argument("--label", help="name for this gateway in the report "
                                   "(default: the USB serial number)")
    p.add_argument("--json", help="write the results as JSON to this file")
    args = p.parse_args()

    port, usb_serial = find_gateway(args.gateway)
    if port is None:
        print("no gateway found (plugged in? or pass --gateway)")
        return 1
    label = args.label or usb_serial or port

    import serial
    ser = serial.Serial(port, 115200, timeout=0.2)

    def send(obj: dict) -> None:
        ser.write((json.dumps({"target": "thymio", **obj}) + "\n").encode())

    def wait_for(types: set[str], timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = ser.readline()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if msg.get("type") in types:
                return msg
        return None

    # The port-open resets the board; wait until the thymio target answers a ping.
    time.sleep(0.5)
    print(f"[{label}] port {port} — waiting for the C6...")
    deadline = time.monotonic() + 8.0
    alive = False
    while time.monotonic() < deadline and not alive:
        send({"cmd": "ping"})
        alive = wait_for({"pong", "rcp_ready"}, 1.0) is not None
    if not alive:
        print("C6 never answered a ping — is this a gateway/rcp_c6 board?")
        ser.close()
        return 1

    # Clean radio: the C6's 15.4 RX dies cumulatively across sessions; only a reboot
    # restores it (same reason discover_thymios reboots first).
    send({"cmd": "reboot"})
    if wait_for({"rcp_ready"}, 6.0) is None:
        print("no rcp_ready after reboot — measuring anyway (RX may be deaf)")
    ser.reset_input_buffer()

    send({"cmd": "thymio_rx_debug", "on": True})
    send({"cmd": "thymio_discover", "on": True, "ch": args.ch})
    print(f"[{label}] discovering on ch {args.ch} for {args.secs:.0f}s "
          f"(Thymio must be powered on)...")

    rssi: dict[int, list[int]] = {}                 # addr -> RSSI samples
    replies: dict[int, int] = {}                    # addr -> thymio_found count
    t0 = time.monotonic()
    last_note = t0
    while (now := time.monotonic()) - t0 < args.secs:
        raw = ser.readline()
        if now - last_note >= 5.0:
            last_note = now
            n = sum(len(v) for v in rssi.values())
            med = (statistics.median([s for v in rssi.values() for s in v])
                   if n else float("nan"))
            print(f"  {now - t0:4.0f}s: {n} frames, median RSSI {med:.0f} dBm")
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if msg.get("type") == "thymio_found":
            try:
                addr = int(str(msg.get("addr", "")), 16)
            except ValueError:
                continue
            replies[addr] = replies.get(addr, 0) + 1
        elif msg.get("type") == "thymio_rx":
            addr = frame_src(msg.get("data", ""))
            if addr is not None and "rssi" in msg:
                rssi.setdefault(addr, []).append(int(msg["rssi"]))

    send({"cmd": "thymio_discover", "on": False})
    send({"cmd": "thymio_rx_debug", "on": False})
    time.sleep(0.2)
    ser.close()

    result = {"label": label, "port": port, "usb_serial": usb_serial,
              "channel": args.ch, "secs": args.secs, "robots": {}}
    if not replies and not rssi:
        print(f"\n[{label}] NO Thymio heard at all — powered on? paired on PAN 0x4481? "
              f"right channel (--ch)?")
    for addr in sorted(set(replies) | set(rssi)):
        n = replies.get(addr, 0)
        samples = rssi.get(addr, [])
        rate = n / args.secs
        stats = {
            "replies": n,
            "reply_rate_hz": round(rate, 1),
            "reply_pct": round(100.0 * rate / DISCOVER_HZ, 1),
        }
        if samples:
            stats.update(rssi_median=statistics.median(samples),
                         rssi_mean=round(statistics.fmean(samples), 1),
                         rssi_min=min(samples), rssi_max=max(samples),
                         rssi_n=len(samples))
        result["robots"][f"{addr:04x}"] = stats
        print(f"\n[{label}] Thymio {addr:04x}:")
        print(f"  replies    : {n} in {args.secs:.0f}s = {rate:.1f}/s "
              f"({stats['reply_pct']:.0f}% of the {DISCOVER_HZ:.0f}/s broadcasts)")
        if samples:
            print(f"  RSSI (dBm) : median {stats['rssi_median']:.0f}  "
                  f"mean {stats['rssi_mean']:.1f}  "
                  f"range {stats['rssi_min']}..{stats['rssi_max']}  "
                  f"(n={stats['rssi_n']})")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)
        print(f"\nsaved -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
