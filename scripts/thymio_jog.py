#!/usr/bin/env python3
"""Standalone Thymio smoke-test / jog tool over the wireless RF dongle (thymiodirect).

No Thymio Suite / TDM needed - thymiodirect talks to the dongle's serial port directly.
One dongle relays to several Thymios at once (each a wireless *node id*). Plug in the RF
dongle (paired with a powered Thymio) and:

    python scripts/thymio_jog.py                 # scripted smoke test (moves + LEDs)
    python scripts/thymio_jog.py --list          # list node ids the dongle sees, exit
    python scripts/thymio_jog.py --node 2 --repl # jog a specific Thymio by node id
    python scripts/thymio_jog.py --leds 0 32 0   # set the top LED (green) and exit
    python scripts/thymio_jog.py --drive 150 150 --secs 1.5

Use --list to learn which node id is which robot (blink/drive one to tell them apart),
then set those ids in Robot Config -> Thymio. Reuses ThymioLink / ThymioDongle (the same
transport the app uses) and always stops the motors on exit. Needs: pip install
thymiodirect. Thymio native vars: motor.{left,right}.target (~ -500..500), leds.top =
[r, g, b] (0..32).
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.robots.thymio.thymio_dongle import ThymioDongle   # noqa: E402
from src.robots.thymio.thymio_link import ThymioLink       # noqa: E402


def smoke(link: ThymioLink) -> None:
    """A short, gentle sequence that proves movement and LEDs work."""
    print("LED green");     link.set_leds(0, 32, 0)
    print("forward");       link.set_motors(150, 150); time.sleep(1.2)
    print("stop");          link.set_motors(0, 0);     time.sleep(0.4)
    print("spin");          link.set_motors(150, -150); time.sleep(1.0)
    print("stop, LED off"); link.set_motors(0, 0);     link.set_leds(0, 0, 0)


def repl(link: ThymioLink) -> None:
    """Line-based manual jog (commands read one per line)."""
    print("commands: f=fwd b=back l=left r=right s=stop  m <L> <R>  c <r> <g> <b>  q=quit")
    speed = 150
    while True:
        try:
            parts = input("thymio> ").strip().split()
        except EOFError:
            break
        if not parts:
            continue
        cmd, args = parts[0], parts[1:]
        if cmd == "q":
            break
        elif cmd == "f":
            link.set_motors(speed, speed)
        elif cmd == "b":
            link.set_motors(-speed, -speed)
        elif cmd == "l":
            link.set_motors(-speed, speed)
        elif cmd == "r":
            link.set_motors(speed, -speed)
        elif cmd == "s":
            link.set_motors(0, 0)
        elif cmd == "m" and len(args) == 2:
            link.set_motors(int(args[0]), int(args[1]))
        elif cmd == "c" and len(args) == 3:
            link.set_leds(int(args[0]), int(args[1]), int(args[2]))
        else:
            print("?")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", help="dongle serial port (default: auto-detect)")
    p.add_argument("--list", action="store_true",
                   help="list the node ids the dongle currently sees, then exit")
    p.add_argument("--node", type=int, default=None,
                   help="node id of the Thymio to drive (default: first discovered)")
    p.add_argument("--repl", action="store_true", help="interactive line jog")
    p.add_argument("--leds", nargs=3, type=int, metavar=("R", "G", "B"),
                   help="set the top LED (0..32) and exit")
    p.add_argument("--drive", nargs=2, type=int, metavar=("L", "R"),
                   help="set motor targets, hold --secs, then stop")
    p.add_argument("--secs", type=float, default=1.5, help="duration for --drive")
    args = p.parse_args()

    if args.list:
        dongle = ThymioDongle(serial_port=args.port)
        print("connecting to the dongle...")
        if not dongle.connect():
            print("could not connect - dongle plugged in and a Thymio powered ON + paired?")
            return 1
        print("node ids seen:", dongle.nodes or "(none)")
        dongle.close()
        return 0

    link = ThymioLink(serial_port=args.port, node_id=args.node)
    print("connecting to the Thymio via the dongle...")
    if not link.connect():
        print("could not connect - is the dongle plugged in and paired with a powered "
              "Thymio? (and: pip install thymiodirect)")
        return 1
    try:
        if args.leds is not None:
            link.set_leds(*args.leds)
        elif args.drive is not None:
            link.set_motors(*args.drive)
            time.sleep(args.secs)
        elif args.repl:
            repl(link)
        else:
            smoke(link)
        return 0
    finally:
        link.set_motors(0, 0)   # always stop the wheels
        link.close()


if __name__ == "__main__":
    sys.exit(main())
