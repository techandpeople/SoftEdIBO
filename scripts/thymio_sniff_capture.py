#!/usr/bin/env python3
"""Phase-1 capture: sniff the Thymio's 802.15.4 traffic while driving it via the dongle.

The reverse-engineering step for dongle-free control (docs/THYMIO_WIRELESS_CONTROL.md).
It runs the two sides at once and writes ONE correlated, timestamped timeline:

  * the **S3 gateway** (USB) carries the boxed C6, now running the sniff-capable RCP.
    We send it `{"target":"thymio","cmd":"sniff_start"}` and log every relayed
    `{"type":"frame",...}` line (the C6's promiscuous 802.15.4 frames).
  * the **RF dongle** (USB) drives the real Thymio through `ThymioLink`, running a
    scripted sequence of well-separated moves/LEDs — each logged with a timestamp.

Diffing the frames that appear right after each distinct command reveals which bytes
carry the motor/LED Aseba SetVariables → the payload format to reimplement on the C6.

    # 1) find the channel: sweep 11..26 while jittering the robot; the Thymio's
    #    channel is the one where frames appear *because we drive* (a Thread/Matter
    #    neighbour shows steady weak traffic and would fool a plain "busiest channel"
    #    — the sweep drives on each channel so it can't).
    python scripts/thymio_sniff_capture.py --scan
    # 2) lock that channel for a clean capture
    python scripts/thymio_sniff_capture.py --ch 15 --out cap_ch15.jsonl
    # sniff only (drive the Thymio yourself), or slow the sequence down:
    python scripts/thymio_sniff_capture.py --no-drive
    python scripts/thymio_sniff_capture.py --ch 15 --secs 3

Close the main app first (it holds the gateway port). Needs both USB devices plugged:
the S3 gateway (Espressif VID 303a) and the RF dongle (Mobsya VID 0617). The C6 must be
running the sniff-capable RCP (OTA it first). Always stops the motors + sniffer on exit.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.robots.thymio.thymio_link import ThymioLink       # noqa: E402

# thymiodirect has no clean disconnect: its reader thread + asyncio loop throw as we
# close the dongle port. That teardown noise is expected and harmless (the capture is
# already saved), so hush it once we start shutting down.
_shutting_down = threading.Event()
_orig_excepthook = threading.excepthook


def _quiet_excepthook(args):
    if not _shutting_down.is_set():
        _orig_excepthook(args)


threading.excepthook = _quiet_excepthook


def find_gateway(explicit: str | None) -> str | None:
    """Serial port of the S3 gateway (Espressif VID 0x303a), or None."""
    if explicit:
        return explicit
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    for p in list_ports.comports():
        desc = f"{p.description or ''} {p.product or ''} {p.manufacturer or ''}".lower()
        if p.vid == 0x303A or "espressif" in desc or "xiao" in desc:
            return p.device
    return None


class Capture:
    """Owns the gateway serial link: sends sniff commands, logs relayed frames."""

    def __init__(self, port: str, log, debug: bool = False):
        import serial
        self._ser = serial.Serial(port, 115200, timeout=0.2)
        self._log = log
        self._debug = debug
        self._wlock = threading.Lock()
        self._stop = threading.Event()
        self.ch_frames: Counter = Counter()      # frames seen per channel
        self.ch_rssi: dict[int, int] = {}        # best (max) RSSI per channel
        self._reader = threading.Thread(target=self._read_loop, name="gw-reader", daemon=True)
        self._reader.start()

    def send(self, obj: dict) -> None:
        line = (json.dumps(obj) + "\n").encode()
        with self._wlock:
            self._ser.write(line)

    def _read_loop(self) -> None:
        buf = b""
        while not self._stop.is_set():
            try:
                buf += self._ser.read(256)
            except Exception:
                break
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                self._handle_raw(raw.strip())

    def _handle_raw(self, raw: bytes) -> None:
        if not raw:
            return
        if self._debug:
            print("RX:", raw[:140].decode("utf-8", "replace"))
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        if msg.get("type") == "frame":
            ch = msg.get("ch", -1)
            self.ch_frames[ch] += 1
            r = msg.get("rssi")
            if r is not None:
                self.ch_rssi[ch] = max(self.ch_rssi.get(ch, -999), r)
        # Keep the sniffer traffic + anything the C6 tagged from the Thymio side;
        # ignore gateway status noise.
        if msg.get("type") in ("frame", "sniff") or msg.get("source") == "thymio":
            self._log("rx", **msg)

    def close(self) -> None:
        self._stop.set()
        try:
            self._ser.close()
        except Exception:
            pass


def _connect_dongle(args) -> ThymioLink | None:
    link = ThymioLink(serial_port=args.dongle, node_id=args.node)
    print("connecting to the Thymio via the dongle…")
    if not link.connect():
        print("dongle connect failed — is a Thymio powered ON + paired?")
        return None
    return link


def _arm_sniffer(cap: "Capture", ch: int, log) -> None:
    """Start the sniffer and lock a channel via sniff_ch (the retune path receives
    reliably, unlike a channel fixed in the initial enable())."""
    cap.send({"target": "thymio", "cmd": "sniff_start"})
    log("cmd", action="sniff_start", ch=ch)
    if ch:
        time.sleep(0.3)
        cap.send({"target": "thymio", "cmd": "sniff_ch", "n": ch})


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--gateway", help="S3 gateway serial port (default: auto-detect)")
    p.add_argument("--dongle", help="RF dongle serial port (default: auto-detect)")
    p.add_argument("--node", type=int, default=None, help="Thymio node id to drive")
    p.add_argument("--ch", type=int, default=0,
                   help="lock the sniffer to this 802.15.4 channel (11..26); 0 = hop")
    p.add_argument("--secs", type=float, default=2.0, help="hold time per drive action")
    p.add_argument("--scan", action="store_true",
                   help="sweep channels 11..26 while driving to find the Thymio's channel")
    p.add_argument("--no-drive", action="store_true",
                   help="sniff + log only; you drive the Thymio yourself")
    p.add_argument("--out", default=None, help="capture file (default: capture_<ts>.jsonl)")
    p.add_argument("--debug", action="store_true",
                   help="print every raw line received from the gateway/C6 (diagnosis)")
    args = p.parse_args()

    gw_port = find_gateway(args.gateway)
    if gw_port is None:
        print("no S3 gateway found (plugged in? close the main app? or pass --gateway)")
        return 1

    out_path = args.out or f"capture_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    out = open(out_path, "w")
    t0 = time.monotonic()
    log_lock = threading.Lock()

    def log(kind: str, **fields) -> None:
        rec = {"t": round(time.monotonic() - t0, 4), "kind": kind, **fields}
        with log_lock:
            out.write(json.dumps(rec) + "\n")
            out.flush()
        if kind == "cmd":
            print(f"[{rec['t']:7.2f}] CMD {fields.get('action')}")

    print(f"gateway {gw_port}  →  capturing to {out_path}"
          f"  (ch={'hop' if not args.ch else args.ch})")
    cap = Capture(gw_port, log, debug=args.debug)
    time.sleep(0.5)

    link = None
    try:
        if args.no_drive:
            _arm_sniffer(cap, args.ch, log)
            print("sniffing… drive the Thymio yourself; Ctrl-C to stop.")
            while True:
                time.sleep(1)
        # Connect the dongle FIRST (its connect handshake is slow), THEN arm the sniffer
        # and drive immediately: the C6 RX goes idle a few seconds after arming, so the
        # driving must land inside that fresh window — not 5 s later once RX has stalled.
        link = _connect_dongle(args)
        if link is None:
            return 1
        _arm_sniffer(cap, args.ch, log)
        time.sleep(0.5)
        if args.scan:
            run_scan(cap, link, log, args.secs)
        else:
            run_sequence(link, log, args.secs)
        return 0
    except KeyboardInterrupt:
        print("\nstopping.")
        return 0
    finally:
        _shutting_down.set()                       # hush thymiodirect's messy teardown
        logging.getLogger("asyncio").setLevel(logging.CRITICAL)
        if link is not None:
            link.set_motors(0, 0)
            link.close()
        cap.send({"target": "thymio", "cmd": "sniff_stop"})
        log("cmd", action="sniff_stop")
        time.sleep(0.3)
        cap.close()
        out.close()
        print(f"saved {out_path}")


_MOTOR_STATES = [
    ("forward",    (150, 150)),
    ("backward",   (-150, -150)),
    ("left_only",  (150, 0)),      # asymmetric → pins motor.left vs motor.right
    ("right_only", (0, 150)),
    ("spin_left",  (-150, 150)),
    ("spin_right", (150, -150)),
    ("stop",       (0, 0)),
]
_LED_STATES = [
    ("led_red",   (32, 0, 0)),
    ("led_green", (0, 32, 0)),
    ("led_blue",  (0, 0, 32)),
    ("led_off",   (0, 0, 0)),
]


def run_sequence(link: ThymioLink, log, secs: float) -> None:
    """Cycle through distinct, labelled states — each transition transmits a frame.

    thymiodirect only sends on a value CHANGE, so holding one value re-sends nothing;
    cycling distinct states keeps it transmitting. At weak signal we rely on many
    repetitions, and the asymmetric (150,0)/(0,150) states pin left vs right.
    """
    # ~4 changes/second: slow enough that thymiodirect actually transmits each distinct
    # value (spamming ~12/s let it coalesce and send almost nothing), fast enough to hit
    # the short fresh-RX window. Motors first — they're what we're mapping.
    reps = max(8, int(secs * 4))
    log("cmd", action="baseline")             # idle — ambient / keep-alives only
    time.sleep(1.0)
    for _ in range(reps):
        for name, (left, right) in _MOTOR_STATES:
            log("cmd", action=name)
            link.set_motors(left, right)
            time.sleep(0.25)
    link.set_motors(0, 0)
    for _ in range(reps):
        for name, rgb in _LED_STATES:
            log("cmd", action=name)
            link.set_leds(*rgb)
            time.sleep(0.25)
    link.set_leds(0, 0, 0)


def run_scan(cap: "Capture", link: ThymioLink, log, secs: float) -> None:
    """Sweep 11..26, jittering the robot on each, and count drive-correlated frames.

    The Thymio's channel is the one that lights up *because we drive* — a steady
    Thread/Matter neighbour can't fake that (we're actively generating the traffic).
    """
    print("scanning channels 11..26 while driving (the Thymio's channel lights up)…")
    results: dict[int, int] = {}
    for ch in range(11, 27):
        cap.send({"target": "thymio", "cmd": "sniff_ch", "n": ch})
        log("cmd", action="scan_ch", ch=ch)
        time.sleep(0.3)                       # settle + flush old-channel frames
        start = cap.ch_frames[ch]
        deadline = time.monotonic() + secs
        toggle = False
        while time.monotonic() < deadline:    # jitter in place → SetVariable frames
            link.set_motors(200 if toggle else -200, -200 if toggle else 200)
            toggle = not toggle
            time.sleep(0.25)
        link.set_motors(0, 0)
        results[ch] = cap.ch_frames[ch] - start
        rssi = cap.ch_rssi.get(ch)
        print(f"  ch{ch:2d}: {results[ch]:4d} frames while driving"
              + (f"  (rssi≤{rssi})" if rssi is not None else ""))
    hits = {c: n for c, n in results.items() if n > 0}
    if not hits:
        print("\nno frames on ANY channel while driving — is the Thymio moving? "
              "(dongle paired, robot powered ON, close to the gateway box?)")
        return
    best = max(hits, key=lambda c: hits[c])
    print(f"\n→ Thymio channel = {best}  ({hits[best]} frames, rssi≤{cap.ch_rssi.get(best)})")
    print(f"  next: python scripts/thymio_sniff_capture.py --ch {best} --out cap.jsonl")


if __name__ == "__main__":
    sys.exit(main())
