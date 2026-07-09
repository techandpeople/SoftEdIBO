#!/usr/bin/env python3
"""USB isolation test for the sensor-PUSH Aseba program.

Loads the EXACT bytecode the C6 loads onto a Thymio — over its micro-USB cable
(Aseba's own transport, no wireless RF module involved) — and counts the emitted
acc/ground events for 30 s. This decides where the "emits die after ~1 s" fault is:

  * emits SUSTAIN over USB  -> the Aseba program is correct; the fault is the
    WIRELESS path (the Thymio's RF module dropping association, or the C6 radio),
    NOT the program.
  * emits START then DIE over USB -> the Thymio VM/program itself stops -> fix
    the bytecode.
  * NO emits at all -> the program didn't load/run.

Connect the Thymio by its micro-USB cable (close Thymio Suite so it doesn't hold
the port), then:  python3 scripts/thymio_emit_test.py [/dev/ttyACMx]
"""
import asyncio
import glob
import sys
import threading
import time
import warnings

# The exact program the C6 loads: on timer0, emit acc[0x62..0x64] as event 0x0AAC
# and prox.ground.delta[0x54..0x55] as event 0x0AAD. Event-vector header: [size][id][addr].
EV_ACC, EV_GND = 0x0AAC, 0x0AAD
BYTECODE = [
    0x0003, 0xFFEF, 0x0003,            # event table: timer0 (0xFFEF) -> handler @ index 3
    0xB000 | EV_ACC, 0x0062, 0x0003,   # emit acc    from 0x62, len 3
    0xB000 | EV_GND, 0x0054, 0x0002,   # emit ground from 0x54, len 2
    0x0000,                            # STOP
]


def find_port():
    """The Thymio's own USB port (Mobsya VID 0x0617). NOT the gateway (Espressif
    0x303a) — so this never grabs the C6 gateway by mistake."""
    import serial.tools.list_ports as lp
    for p in lp.comports():
        if p.vid == 0x0617:
            return p.device
    return None


def _list_ports():
    import serial.tools.list_ports as lp
    return [(p.device, hex(p.vid) if p.vid else "-", p.description)
            for p in lp.comports() if "ACM" in p.device or "USB" in p.device]


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else find_port()
    if not port:
        print("No Thymio (Mobsya VID 0617) on USB. Connect the THYMIO by its micro-USB "
              "cable (this is NOT the gateway). Ports seen:")
        for dev, vid, desc in _list_ports():
            print(f"   {dev}  vid={vid}  {desc}")
        print("If you know it, pass it: python3 scripts/thymio_emit_test.py /dev/ttyACMx")
        return
    print(f"Using port {port}")

    # thymiodirect's Thymio() needs an event loop on this thread (Python 3.12).
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            pol = asyncio.get_event_loop_policy()
            try:
                pol.get_event_loop()
            except RuntimeError:
                pol.set_event_loop(pol.new_event_loop())

    from thymiodirect import Thymio

    node_ids = []
    th = Thymio(serial_port=port, on_connect=node_ids.append)
    threading.Thread(target=th.connect, daemon=True).start()
    t0 = time.monotonic()
    while not node_ids and time.monotonic() - t0 < 8:
        time.sleep(0.05)
    if not node_ids:
        print("Thymio didn't answer on USB. Power-cycle it, close Thymio Suite, retry.")
        return
    nid = node_ids[0]
    print(f"Connected: node 0x{nid:04x}")
    time.sleep(0.5)                    # let the node description finish loading

    conn = th.thymio_proxy.connection
    counts = {"acc": 0, "gnd": 0, "other": 0}

    async def on_ev(source, ev_id, args):
        if ev_id == EV_ACC:
            counts["acc"] += 1
        elif ev_id == EV_GND:
            counts["gnd"] += 1
        else:
            counts["other"] += 1

    conn.on_user_event = on_ev

    print("Loading the push program (SET_BYTECODE + RUN + timer.period=100)…")
    conn.set_bytecode(nid, BYTECODE)
    conn.run(nid)
    time.sleep(0.1)
    conn.set_var(nid, "timer.period", 100, 0)   # timer0 every 100 ms -> emit at 10 Hz

    print("Watching emits for 30 s (acc should stay ~10/s if the program is fine):")
    last = dict(counts)
    for s in range(30):
        time.sleep(1.0)
        d_acc, d_gnd = counts["acc"] - last["acc"], counts["gnd"] - last["gnd"]
        last = dict(counts)
        print(f"  t={s + 1:2d}s   acc={d_acc:3d}/s   ground={d_gnd:3d}/s   "
              f"(total acc={counts['acc']}, other-events={counts['other']})")

    print("\n=== VERDICT ===")
    if counts["acc"] >= 200:
        print("Emits SUSTAINED over USB → the Aseba program is CORRECT.")
        print("→ The fault is the WIRELESS path (Thymio RF association / C6 radio), not the program.")
    elif counts["acc"] > 0:
        print("Emits STARTED then DIED even over USB → the Thymio VM stops. The bytecode/program needs fixing.")
    else:
        print("NO emits over USB → the program didn't load/run (or the event ids differ). Check the setup.")
    try:
        th.disconnect()
    except Exception:
        pass


if __name__ == "__main__":
    main()
