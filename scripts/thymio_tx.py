#!/usr/bin/env python3
"""Transmit a raw 802.15.4 frame from the gateway's C6 (the RCP `tx` command).

Phase-1 dongle-free control: replay/forge a Thymio SET_VARIABLES motor command and see if
the robot reacts. Sends `{"target":"thymio","cmd":"tx","ch":N,"data":hex}` to the S3
gateway, which forwards it to its C6, which transmits it on the 802.15.4 radio.

Pass the frame hex WITHOUT its 2-byte FCS (the radio appends it). A captured frame's last
two bytes are RSSI/LQI (promiscuous mode), so strip them before replaying.

    # replay a captured motor frame (drop its last 2 bytes = RSSI/LQI):
    python scripts/thymio_tx.py --ch 11 --data "61 88 15 81 44 25 6a 37 32 83 00 6a 25 32 37 11 01 00 0c a0 6a 25 56 00 c8 00"
    # forge a different speed: edit the value bytes at the end (c8 00 = +200, 38 ff = -200)

Close the main app first (it holds the gateway port).
"""
from __future__ import annotations

import argparse
import json
import sys
import time


def find_gateway(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    from serial.tools import list_ports
    for p in list_ports.comports():
        desc = f"{p.description or ''} {p.manufacturer or ''} {p.product or ''}".lower()
        if p.vid == 0x303A or "espressif" in desc or "xiao" in desc:
            return p.device
    return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gateway", help="S3 gateway serial port (default: auto-detect)")
    p.add_argument("--ch", type=int, required=True, help="802.15.4 channel to transmit on")
    p.add_argument("--data", help="raw frame hex (spaces ok), WITHOUT the FCS")
    p.add_argument("--repeat", type=int, default=1, help="send it this many times")
    p.add_argument("--gap", type=float, default=0.3, help="seconds between repeats")
    args = p.parse_args()

    if not args.data:
        print("give --data <hex> (a captured frame minus its last 2 bytes)")
        return 1

    port = find_gateway(args.gateway)
    if port is None:
        print("no gateway found (plugged in? close the main app? or pass --gateway)")
        return 1

    import serial
    ser = serial.Serial(port, 115200, timeout=0.3)
    time.sleep(0.5)
    frame = args.data.replace(" ", "")
    for i in range(args.repeat):
        ser.write((json.dumps({"target": "thymio", "cmd": "tx",
                               "ch": args.ch, "data": frame}) + "\n").encode())
        print(f"→ tx #{i + 1}/{args.repeat} on ch{args.ch} ({len(frame) // 2} bytes)")
        time.sleep(args.gap)

    # Print the C6's tx replies (err code) + anything it tags from the Thymio side.
    deadline = time.time() + 2.0
    while time.time() < deadline:
        line = ser.readline()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("type") == "tx" or msg.get("source") == "thymio":
            print("  ", line.decode("utf-8", "replace").strip())
    ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
