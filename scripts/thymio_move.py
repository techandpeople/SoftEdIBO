#!/usr/bin/env python3
"""Drive the Thymio's motors + LEDs with NO dongle — the gateway's C6 forges a Thymio
Aseba SET_VARIABLES frame and transmits it over 802.15.4.

Proven on hardware 2026-07-02: the C6 (`tx` command) transmits the frame, the Thymio obeys.
This replaces hand-crafted hex — give motor/LED values, it builds the frame(s) with a fresh
incrementing sequence number (so the Thymio doesn't reject repeats) and sends them.

    python scripts/thymio_move.py --ch 25 --gateway /dev/ttyACM0 --left 150 --right 150   # forward
    python scripts/thymio_move.py --ch 25 --gateway /dev/ttyACM0 --left 200 --right -200  # spin
    python scripts/thymio_move.py --ch 25 --gateway /dev/ttyACM0 --stop
    python scripts/thymio_move.py --ch 25 --gateway /dev/ttyACM0 --led 32 0 0             # red top LED

--gateway is the port of the C6 running our RCP (`tx` command). The Thymio's channel, PAN
and addresses below were read off the air; if you re-pair to a different network, re-capture
and update them (only the channel usually changes — set it with --ch).
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time

# Read off the air (docs/THYMIO_WIRELESS_CONTROL.md): the Thymio's 802.15.4 network.
_PAN = bytes([0x81, 0x44])          # PAN id 0x4481, little-endian on the wire
_DST = bytes([0x25, 0x6A])          # 802.15.4 dest = the Thymio (0x6a25)
_SRC = bytes([0x37, 0x32])          # 802.15.4 src  = the host/dongle (0x3237)
_WRAP = bytes([0x83, 0x00, 0x6A, 0x25, 0x32, 0x37, 0x11])   # RF-module header
_NODE = bytes([0x6A, 0x25])         # Aseba destination node id (0x256a)
_SET_VARIABLES = bytes([0x0C, 0xA0])   # Aseba message type 0xA00C
_SOURCE = bytes([0x01, 0x00])       # Aseba source (the host)

# Variable word addresses in the Thymio VM (from skel-usb-user.h).
_MOTOR_LEFT = 0x56
_MOTOR_RIGHT = 0x57
_LEDS_TOP = 0x65


def _le16(v: int) -> bytes:
    return (v & 0xFFFF).to_bytes(2, "little")


def _frame(seq: int, start_addr: int, values: list[int]) -> str:
    """Build one SET_VARIABLES 802.15.4 frame (PSDU hex, WITHOUT the FCS)."""
    body = _SOURCE + _SET_VARIABLES + _NODE + _le16(start_addr)
    for v in values:
        body += _le16(v)
    frame = bytes([0x61, 0x88, seq & 0xFF]) + _PAN + _DST + _SRC + _WRAP + body
    return frame.hex()


def find_gateway(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    from serial.tools import list_ports
    for p in list_ports.comports():
        desc = f"{p.description or ''} {p.manufacturer or ''}".lower()
        if p.vid == 0x303A or "espressif" in desc or "xiao" in desc:
            return p.device
    return None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gateway", help="C6 serial port (default: auto-detect Espressif)")
    p.add_argument("--ch", type=int, default=25, help="802.15.4 channel the Thymio is on")
    p.add_argument("--left", type=int, help="motor.left.target (~ -500..500)")
    p.add_argument("--right", type=int, help="motor.right.target")
    p.add_argument("--stop", action="store_true", help="both motors to 0")
    p.add_argument("--led", nargs=3, type=int, metavar=("R", "G", "B"),
                   help="leds.top r g b (0..32)")
    p.add_argument("--repeat", type=int, default=6, help="send each frame this many times")
    args = p.parse_args()

    # Which SET_VARIABLES frames to send: motors are separate single-value writes (that's
    # what the dongle does), LEDs are one 3-value write.
    jobs: list[tuple[int, list[int]]] = []
    if args.stop:
        jobs += [(_MOTOR_LEFT, [0]), (_MOTOR_RIGHT, [0])]
    else:
        if args.left is not None:
            jobs.append((_MOTOR_LEFT, [args.left]))
        if args.right is not None:
            jobs.append((_MOTOR_RIGHT, [args.right]))
    if args.led is not None:
        jobs.append((_LEDS_TOP, args.led))
    if not jobs:
        print("nothing to do — give --left/--right, --stop, or --led")
        return 1

    port = find_gateway(args.gateway)
    if port is None:
        print("no C6/gateway found (plugged in? or pass --gateway)")
        return 1

    import serial
    ser = serial.Serial(port, 115200, timeout=0.3)
    time.sleep(0.5)
    seq = random.randint(0, 255)      # start from a random seq to dodge dup-rejection
    for _ in range(args.repeat):
        for addr, values in jobs:
            data = _frame(seq, addr, values)
            seq = (seq + 1) & 0xFF
            ser.write((json.dumps({"target": "thymio", "cmd": "tx",
                                   "ch": args.ch, "data": data}) + "\n").encode())
            time.sleep(0.08)
    print(f"sent {args.repeat}× {[(hex(a), v) for a, v in jobs]} on ch{args.ch}")

    deadline = time.time() + 1.5      # show the C6's tx replies
    while time.time() < deadline:
        line = ser.readline()
        if line and (b'"tx"' in line or b'thymio' in line):
            print("  ", line.decode("utf-8", "replace").strip())
    ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
