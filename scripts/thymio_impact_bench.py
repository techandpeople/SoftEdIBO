#!/usr/bin/env python3
"""USB bench for the ON-BOARD impact-detection Aseba program.

The PC-side impact detection is fundamentally sampling-limited: a knock is a
~10-50 ms transient, the C6 stream shows the acc at 10 Hz and the Thymio itself
only refreshes it at ~16 Hz (smoothed) — most knock peaks are simply never seen.
This program moves the detection ON the Thymio:

  * ``onevent tap``  — the accelerometer's HARDWARE knock detector (~120 Hz
    internal), catches sharp hits no software sampling can; counts monotonically.
  * ``onevent acc``  — 16 Hz peak-hold of the summed |delta| per axis, so the
    strongest jolt between two emits is never lost (magnitude for levels).
  * ``onevent timer0`` (10 Hz) — emits the usual sensor events plus a new
    ``[peak, taps]`` event, then resets the peak.

Run this on a USB-cabled Thymio (data cable, close Thymio Suite) to
(a) validate the program on the real VM and (b) dump the assembled bytecode
words to bake into the C6 firmware:

    python3 scripts/thymio_impact_bench.py [--secs 30] [/dev/ttyACMx]

While it watches, KNOCK the robot (gentle / medium / hard) and also turn it
over and leave it — peak should spike on knocks, settle to ~0 in any pose, and
``taps`` should increment on sharp hits only.
"""
import argparse
import asyncio
import sys
import threading
import time
import warnings

EV_ACC, EV_MIC, EV_PROX, EV_IMPACT = 0x0AAC, 0x0AAD, 0x0AAE, 0x0AAF

# Scratch layout in _userdata (first free word after the named variables):
#   +0..+2 prev acc sample   +3 peak (reset each emit)   +4 taps   +5 temp
ASM = """
    dc end_toc
    dc _ev.init, init
    dc _ev.timer0, tick
    dc _ev.acc, accev
    dc _ev.tap, tapev
end_toc:

init:
    load acc
    store _userdata
    load acc+1
    store _userdata+1
    load acc+2
    store _userdata+2
    push.s 100
    store timer.period
    stop

tick:
    emit 0x0aae, prox.horizontal, 7
    emit 0x0aad, mic.intensity, 1
    emit 0x0aaf, _userdata+3, 2
    emit 0x0aac, prox.ground.delta, 17
    push.s 0
    store _userdata+3
    stop

accev:
    load acc
    load _userdata
    sub
    abs
    load acc+1
    load _userdata+1
    sub
    abs
    add
    load acc+2
    load _userdata+2
    sub
    abs
    add
    store _userdata+5
    load _userdata+5
    load _userdata+3
    jump.if.not gt, upd
    load _userdata+5
    store _userdata+3
upd:
    load acc
    store _userdata
    load acc+1
    store _userdata+1
    load acc+2
    store _userdata+2
    stop

tapev:
    load _userdata+4
    push.s 1
    add
    store _userdata+4
    stop
"""


def find_port():
    import serial.tools.list_ports as lp
    for p in lp.comports():
        if p.vid == 0x0617:
            return p.device
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", default=None)
    ap.add_argument("--secs", type=int, default=30)
    args = ap.parse_args()

    port = args.port or find_port()
    if not port:
        print("No Thymio (Mobsya VID 0617) on USB — connect it with a DATA cable.")
        return 1
    print(f"Using port {port}")

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
    from thymiodirect.assembler import Assembler

    node_ids = []
    th = Thymio(serial_port=port, on_connect=node_ids.append)
    threading.Thread(target=th.connect, daemon=True).start()
    t0 = time.monotonic()
    while not node_ids and time.monotonic() - t0 < 8:
        time.sleep(0.05)
    if not node_ids:
        print("Thymio didn't answer on USB. Power-cycle it, close Thymio Suite, retry.")
        return 1
    nid = node_ids[0]
    print(f"Connected: node 0x{nid:04x}")
    time.sleep(1.0)                     # let the node description finish loading

    conn = th.thymio_proxy.connection
    rn = conn.remote_nodes[nid]
    print(f"local events: {rn.local_events}")
    for ev in ("timer0", "acc", "tap"):
        if ev not in rn.local_events:
            print(f"FATAL: local event '{ev}' not in the node description — adjust the asm.")
            return 1
    print(f"_userdata (scratch base) = {rn.var_total_size}")

    bytecode = Assembler(rn, ASM).assemble()
    print(f"\nAssembled {len(bytecode)} words. C array for the C6 firmware:")
    print("static const int16_t prog[] = {")
    for i in range(0, len(bytecode), 8):
        row = ", ".join(f"0x{w & 0xFFFF:04X}" for w in bytecode[i:i + 8])
        print(f"    (int16_t)" + ", (int16_t)".join(row.split(", ")) + ",")
    print("};")

    counts = {EV_ACC: 0, EV_MIC: 0, EV_PROX: 0, EV_IMPACT: 0}
    state = {"peak": 0, "taps": 0, "max_peak": 0}

    async def on_ev(source, ev_id, ev_args):
        if ev_id in counts:
            counts[ev_id] += 1
        if ev_id == EV_IMPACT and len(ev_args) >= 2:
            state["peak"], state["taps"] = int(ev_args[0]), int(ev_args[1])
            state["max_peak"] = max(state["max_peak"], state["peak"])

    conn.on_user_event = on_ev

    print("\nLoading program (SET_BYTECODE + RUN — init sets its own timer)…")
    conn.set_bytecode(nid, bytecode)
    conn.run(nid)

    print(f"Watching for {args.secs} s — KNOCK the robot (gentle/medium/hard), "
          f"then flip it over and leave it:\n")
    last = dict(counts)
    for s in range(args.secs):
        time.sleep(1.0)
        d = {k: counts[k] - last[k] for k in counts}
        last = dict(counts)
        print(f"  t={s + 1:2d}s  sensors={d[EV_ACC]:2d}/s  impact_ev={d[EV_IMPACT]:2d}/s  "
              f"peak={state['peak']:4d}  taps={state['taps']:3d}  (max_peak={state['max_peak']})")

    print("\n=== VERDICT ===")
    ok_stream = counts[EV_ACC] >= args.secs * 5
    print(f"sensor emits: {counts[EV_ACC]} ({'OK — sustained' if ok_stream else 'LOW — check'})")
    print(f"impact emits: {counts[EV_IMPACT]}, max peak seen: {state['max_peak']}, "
          f"hardware taps: {state['taps']}")
    print("Expected: peak spikes when knocked, ~0 when still in ANY pose; taps increments on")
    print("sharp hits. If so, the program is good — bake the C array above into the C6.")
    try:
        th.disconnect()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
