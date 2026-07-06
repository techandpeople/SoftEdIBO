#!/usr/bin/env python3
"""Drive a Thymio dongle-free via the C6's CONTINUOUS on-board link.

The leak-fixed `rcp_c6` keeps the Thymio's RF link hot BY ITSELF — once `thymio_link` is
on, the C6 polls the Thymio at ~10 Hz in firmware and holds the motor/LED targets. This
tool opens the C6 once, turns the link on, and just sends target updates: **instant**, no
per-command port-reopen (which resets the C6 and cost seconds), link stays hot the whole
session. This is the reliable dongle-free drive — needs a **U.FL-antenna C6** to reach the
robot, and the leak-fixed firmware (reflash `rcp_c6`).

    python scripts/thymio_link.py --ch 25 --gateway /dev/ttyACM0 --left 200 --right -200
    python scripts/thymio_link.py --ch 25 --gateway /dev/ttyACM0 --stop
    python scripts/thymio_link.py --ch 25 --gateway /dev/ttyACM0 --led 32 0 0
    python scripts/thymio_link.py --ch 25 --gateway /dev/ttyACM0 --repl      # live jog
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
        desc = f"{p.description or ''} {p.manufacturer or ''}".lower()
        if p.vid == 0x303A or "espressif" in desc or "xiao" in desc:
            return p.device
    return None


def repl(drive, leds, sound) -> None:
    """Line-based manual jog (same keys as thymio_jog)."""
    print("commands: f=fwd b=back l=left r=right s=stop  m <L> <R>  c <r> <g> <b>  "
          "snd <0-7>  tone <hz> [ms]  q=quit")
    speed = 150
    while True:
        try:
            parts = input("thymio> ").strip().split()
        except EOFError:
            break
        if not parts:
            continue
        cmd, a = parts[0], parts[1:]
        if cmd == "q":
            break
        elif cmd == "f":
            drive(speed, speed)
        elif cmd == "b":
            drive(-speed, -speed)
        elif cmd == "l":
            drive(-speed, speed)
        elif cmd == "r":
            drive(speed, -speed)
        elif cmd == "s":
            drive(0, 0)
        elif cmd == "m" and len(a) == 2:
            drive(int(a[0]), int(a[1]))
        elif cmd == "c" and len(a) == 3:
            leds(int(a[0]), int(a[1]), int(a[2]))
        elif cmd == "snd" and len(a) == 1:
            sound({"sys": int(a[0])})
        elif cmd == "tone" and a:
            sound({"freq": int(a[0]), "dur": int(a[1]) if len(a) > 1 else 30})
        else:
            print("?")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gateway", help="C6 serial port (default: auto-detect Espressif)")
    p.add_argument("--ch", type=int, default=25, help="802.15.4 channel the Thymio is on")
    p.add_argument("--left", type=int, help="motor.left.target")
    p.add_argument("--right", type=int, help="motor.right.target")
    p.add_argument("--stop", action="store_true", help="both motors to 0")
    p.add_argument("--led", nargs=3, type=int, metavar=("R", "G", "B"), help="leds.top r g b")
    p.add_argument("--sound", type=int, metavar="ID", help="play system sound 0-7 (-1 stops)")
    p.add_argument("--tone", nargs=2, type=int, metavar=("HZ", "DUR60"),
                   help="play a tone: frequency Hz + duration in 1/60 s")
    p.add_argument("--repl", action="store_true", help="interactive line jog")
    p.add_argument("--index", type=int, default=0,
                   help="which Thymio slot on the C6 (0..3) — for driving several")
    p.add_argument("--addr", help="this Thymio's 802.15.4 short address, hex (e.g. 6a25); "
                                  "omit for slot 0 to use the C6 default")
    p.add_argument("--secs", type=float, default=0.0,
                   help="hold the drive N s then stop (0 = until Ctrl-C)")
    args = p.parse_args()

    port = find_gateway(args.gateway)
    if port is None:
        print("no C6/gateway found (plugged in? or pass --gateway)")
        return 1

    import serial
    ser = serial.Serial(port, 115200, timeout=0.2)
    time.sleep(0.3)

    def send(obj: dict) -> None:
        ser.write((json.dumps({"target": "thymio", **obj}) + "\n").encode())

    def drive(left: int, right: int) -> None:
        send({"cmd": "thymio_drive", "idx": args.index, "left": left, "right": right})

    def leds(r: int, g: int, b: int) -> None:
        send({"cmd": "thymio_leds", "idx": args.index, "r": r, "g": g, "b": b})

    def sound(spec: dict) -> None:
        send({"cmd": "thymio_sound", "idx": args.index, **spec})

    # The serial open resets the C6; wait for it to answer before starting the link.
    ser.write(b'{"target":"thymio","cmd":"ping"}\n')
    t0 = time.time()
    while time.time() - t0 < 3.0:
        if b"pong" in (ser.readline() or b""):
            break
    ser.reset_input_buffer()
    send({"cmd": "thymio_link", "on": True, "ch": args.ch})   # C6 starts polling on its own
    if args.addr or args.index != 0:                          # register this robot's address
        send({"cmd": "thymio_set", "idx": args.index, "addr": args.addr or "6a25"})
    time.sleep(0.1)

    has_motion = args.left is not None or args.right is not None
    has_sound = args.sound is not None or args.tone is not None
    do_repl = args.repl or not (args.stop or args.led or has_motion or has_sound)
    try:
        if args.led is not None:
            leds(*args.led)
        if args.tone is not None:
            sound({"freq": args.tone[0], "dur": args.tone[1]})
        elif args.sound is not None:
            sound({"sys": args.sound})
        if args.stop:
            drive(0, 0)
        elif has_motion:
            drive(args.left or 0, args.right or 0)
        if do_repl:
            repl(drive, leds, sound)
        elif has_motion and not args.stop:
            if args.secs:
                time.sleep(args.secs)
            else:
                print("driving (C6 holds the link) — Ctrl-C to stop")
                while True:
                    time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        drive(0, 0)
        send({"cmd": "thymio_link", "on": False})   # stop the poller, motors to 0
        time.sleep(0.1)
        ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
