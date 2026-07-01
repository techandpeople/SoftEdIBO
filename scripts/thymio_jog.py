#!/usr/bin/env python3
"""Standalone Thymio smoke-test / jog tool over a wireless dongle (or USB).

Uses tdmclient to talk to a running Thymio Device Manager (Thymio Suite). Plug in
the RF dongle, power a paired Wireless Thymio, then:

    python scripts/thymio_jog.py            # scripted smoke test (moves + LEDs)
    python scripts/thymio_jog.py --repl     # interactive line jog
    python scripts/thymio_jog.py --leds 0 32 0     # just set the top LED (green)
    python scripts/thymio_jog.py --drive 150 150 --secs 1.5   # drive then stop

This is deliberately independent of the SoftEdIBO app: it verifies the borrowed
dongle + Thymio end-to-end and doubles as the command source when capturing the
dongle's 802.15.4 traffic with the `c6_radio` sniffer (see
docs/THYMIO_WIRELESS_CONTROL.md). It always stops the motors on exit.

tdmclient auto-discovers a local TDM; pass --host/--port to reach a remote one.
Thymio native variables: motor.{left,right}.target (~ -500..500),
leds.top = [r, g, b] (0..32).
"""
from __future__ import annotations

import argparse
import sys

from tdmclient import ClientAsync


async def set_motors(node, left: int, right: int) -> None:
    await node.set_variables({
        "motor.left.target":  [int(left)],
        "motor.right.target": [int(right)],
    })


async def set_leds(node, r: int, g: int, b: int) -> None:
    await node.set_variables({"leds.top": [int(r), int(g), int(b)]})


async def smoke(client, node) -> None:
    """A short, gentle sequence that proves movement and LEDs work."""
    print("LED green");        await set_leds(node, 0, 32, 0)
    print("forward");          await set_motors(node, 150, 150); await client.sleep(1.2)
    print("stop");             await set_motors(node, 0, 0);     await client.sleep(0.4)
    print("spin");             await set_motors(node, 150, -150); await client.sleep(1.0)
    print("stop, LED off");    await set_motors(node, 0, 0);     await set_leds(node, 0, 0, 0)


async def repl(client, node) -> None:
    """Line-based manual jog. Commands are read one per line (blocking)."""
    print(
        "commands: f=fwd  b=back  l=left  r=right  s=stop  "
        "m <L> <R>  c <r> <g> <b>  q=quit"
    )
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
            await set_motors(node, speed, speed)
        elif cmd == "b":
            await set_motors(node, -speed, -speed)
        elif cmd == "l":
            await set_motors(node, -speed, speed)
        elif cmd == "r":
            await set_motors(node, speed, -speed)
        elif cmd == "s":
            await set_motors(node, 0, 0)
        elif cmd == "m" and len(args) == 2:
            await set_motors(node, int(args[0]), int(args[1]))
        elif cmd == "c" and len(args) == 3:
            await set_leds(node, int(args[0]), int(args[1]), int(args[2]))
        else:
            print("?")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", help="TDM host (default: auto-discover the local TDM)")
    p.add_argument("--port", type=int, help="TDM port (default: 8596)")
    p.add_argument("--repl", action="store_true", help="interactive line jog")
    p.add_argument("--leds", nargs=3, type=int, metavar=("R", "G", "B"),
                   help="set the top LED (0..32) and exit")
    p.add_argument("--drive", nargs=2, type=int, metavar=("L", "R"),
                   help="set motor targets, hold --secs, then stop")
    p.add_argument("--secs", type=float, default=1.5, help="duration for --drive")
    args = p.parse_args()

    kwargs = {}
    if args.host:
        kwargs["tdm_addr"] = args.host
    if args.port:
        kwargs["tdm_port"] = args.port

    client = ClientAsync(**kwargs)

    async def prog():
        print("waiting for a Thymio (is the dongle plugged in and the robot on?)…")
        with await client.lock() as node:
            print(f"locked node {node.id_str}")
            try:
                if args.leds is not None:
                    await set_leds(node, *args.leds)
                elif args.drive is not None:
                    await set_motors(node, *args.drive)
                    await client.sleep(args.secs)
                elif args.repl:
                    await repl(client, node)
                else:
                    await smoke(client, node)
            finally:
                await set_motors(node, 0, 0)   # always stop the wheels

    try:
        client.run_async_program(prog)
    except KeyboardInterrupt:
        print("\ninterrupted — motors stopped on exit")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
