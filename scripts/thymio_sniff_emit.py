#!/usr/bin/env python3
"""Independent-sniffer test - needs a SECOND gateway combo (a spare S3+C6).

Watches 802.15.4 channel 25 with the second gateway's C6 in pure-RX sniff mode, counting
the Thymio's pushed sensor-emit frames (Aseba event 0x0AAC, "AC0A" on the wire). This
finally separates the two remaining suspects for "the emits die ~1 s after the link
loads the program":

  * emit count stays > 0 the whole time  -> the Thymio KEEPS emitting on air; it's the
    LINK gateway's C6 receiver (RX-after-TX in link mode) that dies -> fix the C6 radio.
  * emit count drops to 0 ~1 s after the link starts -> the Thymio's RF MODULE itself
    stops transmitting (de-association) -> fix the wireless keep-alive / association.

The sniffer only RECEIVES (never transmits), so it can't disturb the real link.

SETUP
  Gateway #1  -> the app's Test Thymio link (loads the push program; the Thymio emits).
  Gateway #2  -> THIS script (a different USB port).
RUN
  1. Plug in the second gateway, note its port (it's an Espressif 303a device, like #1;
     if unsure, unplug it, list ports, plug it back, see which /dev/ttyACM* appears).
  2. Close nothing else - just run:  python3 scripts/thymio_sniff_emit.py /dev/ttyACMx
  3. In the app, open "Test Thymio" on gateway #1 and watch the per-second counts here.
"""
import json
import sys
import time

import serial

EMIT_MARK = "AC0A"   # Aseba user-event id 0x0AAC, little-endian on the wire


def main():
    if len(sys.argv) < 2:
        print("Pass the SECOND gateway's serial port, e.g.:")
        print("   python3 scripts/thymio_sniff_emit.py /dev/ttyACM2")
        import serial.tools.list_ports as lp
        print("Ports seen:")
        for p in lp.comports():
            if p.vid:
                print(f"   {p.device}  vid={hex(p.vid)}  {p.description}")
        return
    port = sys.argv[1]

    s = serial.Serial(port, 115200, timeout=0.2)
    time.sleep(0.4)
    s.reset_input_buffer()
    s.write(b'{"target":"thymio","cmd":"sniff_start","ch":25}\n')
    print(f"Sniffing 802.15.4 ch25 on {port} (pure RX).")
    print("Now open 'Test Thymio' in the app on gateway #1 and drive/idle it.\n")
    print("  time        emit(0x0AAC)/s   all-frames/s   total-emit")

    buf = bytearray()
    counts = {"emit": 0, "any": 0}
    last = dict(counts)
    last_sec = int(time.time())
    t0 = time.time()
    try:
        while time.time() - t0 < 90:
            chunk = s.read(s.in_waiting or 1)
            if chunk:
                buf.extend(chunk)
                while b"\n" in buf:
                    raw, _, rest = buf.partition(b"\n")
                    buf = bytearray(rest)
                    try:
                        d = json.loads(raw.decode("utf-8", "replace").strip())
                    except Exception:
                        continue
                    if d.get("type") == "frame":
                        counts["any"] += 1
                        if EMIT_MARK in (d.get("data") or "").upper():
                            counts["emit"] += 1
            sec = int(time.time())
            if sec != last_sec:
                de = counts["emit"] - last["emit"]
                da = counts["any"] - last["any"]
                print(f"  {time.strftime('%H:%M:%S')}     {de:4d}            {da:4d}          {counts['emit']}")
                last_sec = sec
                last = dict(counts)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            s.write(b'{"target":"thymio","cmd":"sniff_stop"}\n')
        except Exception:
            pass
        s.close()

    print("\n=== VERDICT ===")
    print("emit/s stays > 0 for the whole run  -> the Thymio KEEPS emitting on air ->")
    print("    the LINK gateway's C6 RX (RX-after-TX in link mode) is what dies. Fix the C6 radio.")
    print("emit/s drops to 0 ~1 s after the link starts -> the Thymio's RF module STOPS ->")
    print("    it de-associates; fix the wireless keep-alive / association like the real dongle.")


if __name__ == "__main__":
    main()
