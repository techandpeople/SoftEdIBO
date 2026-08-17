"""Touch-position feasibility bench - guided capture CLI (Phase 0).

Answers "how much finer than 4 quadrants can THIS skin resolve?" with data.
Tape/print a grid overlay on the skin, run the script, and press each cell
when prompted; the report at the end compares ML on the 3-axis signatures
against magnitude-only features and today's dominant-sensor quadrant logic.
See docs/TOUCH_POSITION_ML_PLAN.md (Phase 0).

Usage:
  # 4x4 grid, 10 presses per cell, node auto-detected:
  python scripts/touch_position_bench.py --skin-type turtle --cell-size-mm 25

  # free-form patterns instead of a grid (multi-touch / squeeze candidates):
  python scripts/touch_position_bench.py --patterns two_fingers,both_hands,squeeze

  # re-analyze existing datasets (no hardware needed):
  python scripts/touch_position_bench.py --analyze bench_*.jsonl

During capture: press ENTER after typing a command -
  r = redo current target    s = skip current target
  z = re-zero sensors now    q = quit early (saves what was captured)
"""

import argparse
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from src.ml.position_bench import (  # noqa: E402
    AnalysisUnavailable,
    CaptureSession,
    PaceTracker,
    PressDetector,
    analyze,
    load_dataset,
    save_jsonl,
)

CONFIG_PATH = project_root / "config" / "settings.yaml"
BASELINE_SETTLE_S = 4.0     # 70 reads at ~28 Hz, plus margin


def gateway_defaults() -> tuple[str | None, int]:
    try:
        import yaml
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        gw = cfg.get("gateway", {})
        return gw.get("serial_port"), int(gw.get("baud_rate", 115200))
    except Exception:
        return None, 115200


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--analyze", nargs="+", metavar="FILE",
                   help="skip capture; analyze existing dataset file(s)")
    p.add_argument("--port", help="gateway serial port (default: settings.yaml)")
    p.add_argument("--baud", type=int, help="baud rate (default: settings.yaml)")
    p.add_argument("--mac", help="touch node MAC (default: auto-detect the "
                                 "only streaming magnet node)")
    p.add_argument("--rows", type=int, default=4, help="grid rows (default 4)")
    p.add_argument("--cols", type=int, default=4, help="grid cols (default 4)")
    p.add_argument("--reps", type=int, default=10,
                   help="presses per target (default 10)")
    p.add_argument("--patterns",
                   help="comma-separated pattern names to capture INSTEAD of "
                        "the grid (e.g. two_fingers,both_hands,squeeze)")
    p.add_argument("--skin-type", default="", help="skin type (metadata)")
    p.add_argument("--variant", default="", help="skin variant (metadata)")
    p.add_argument("--cell-size-mm", type=float,
                   help="grid cell pitch in mm (reports errors in mm too)")
    p.add_argument("--out", help="output JSONL (default: bench_<type>_<ts>.jsonl)")
    p.add_argument("--enter-ut", type=float, default=60.0,
                   help="press-start total energy threshold, uT (default 60)")
    p.add_argument("--exit-ut", type=float, default=35.0,
                   help="press-end total energy threshold, uT (default 35)")
    p.add_argument("--rebaseline-every", type=int, default=4,
                   help="re-zero sensors every N targets (default 4; 0 = never)")
    return p.parse_args()


def run_analysis(meta: dict, presses, out_path: Path | None) -> None:
    try:
        report = analyze(meta, presses)
    except AnalysisUnavailable as exc:
        print(f"\n{exc}")
        return
    print("\n" + report)
    if out_path is not None:
        report_path = out_path.with_suffix(".report.txt")
        report_path.write_text(report + "\n", encoding="utf-8")
        print(f"\nReport saved to {report_path}")


def stdin_commands(q: "queue.Queue[str]") -> None:
    for line in sys.stdin:
        q.put(line.strip().lower())


class BenchRig:
    """Serial-side plumbing: gateway, node MAC, message pump, rebaseline."""

    def __init__(self, port: str, baud: int) -> None:
        from src.hardware.gateway import Gateway
        self.gateway = Gateway(port, baud)
        self.messages: "queue.Queue[tuple[dict, float]]" = queue.Queue()
        self.mac: str | None = None
        if not self.gateway.connect():
            raise SystemExit(f"ERROR: could not connect to gateway on {port}")
        self.gateway.on_message(self._on_message)

    @property
    def node_mac(self) -> str:
        """The resolved touch node; call resolve_mac() before anything else."""
        if self.mac is None:
            raise RuntimeError("no magnet node resolved yet - call resolve_mac()")
        return self.mac

    def _on_message(self, data: dict) -> None:
        if data.get("type") == "magnet":
            self.messages.put((data, time.monotonic() * 1000.0))

    def resolve_mac(self, wanted: str | None, listen_s: float = 3.0) -> str:
        if wanted:
            self.mac = wanted.upper()
            return self.mac
        print(f"Listening {listen_s:.0f}s for magnet nodes...")
        seen: set[str] = set()
        deadline = time.monotonic() + listen_s
        while time.monotonic() < deadline:
            try:
                data, _ = self.messages.get(timeout=0.2)
            except queue.Empty:
                continue
            src = str(data.get("source", "")).upper()
            if src:
                seen.add(src)
        if len(seen) == 1:
            self.mac = next(iter(seen))
            print(f"Using magnet node {self.mac}")
            return self.mac
        if not seen:
            raise SystemExit(
                "ERROR: no magnet stream seen. Is the touch node powered and "
                "paired with this gateway? (--mac to force one)")
        raise SystemExit(
            f"ERROR: several magnet nodes streaming ({', '.join(sorted(seen))}) "
            "- pick one with --mac")

    def drain(self) -> list[tuple[dict, float]]:
        out = []
        while True:
            try:
                data, t_ms = self.messages.get_nowait()
            except queue.Empty:
                return out
            if str(data.get("source", "")).upper() == self.mac:
                out.append((data, t_ms))

    def configure_streaming(self) -> bool:
        """Turn on 3-axis vec rows + re-zero; returns whether vec arrived."""
        print("Enabling 3-axis streaming and re-zeroing sensors "
              f"(~{BASELINE_SETTLE_S:.0f}s, do NOT touch the skin)...")
        self.gateway.send(self.node_mac, "configure", stream_vec=True)
        self.rebaseline()
        deadline = time.monotonic() + 3.0
        saw_vec = saw_any = False
        while time.monotonic() < deadline:
            for data, _ in self.drain():
                saw_any = True
                saw_vec = saw_vec or isinstance(data.get("vec"), list)
            time.sleep(0.05)
        if not saw_any:
            raise SystemExit("ERROR: magnet stream went silent after rebaseline.")
        if not saw_vec:
            print("WARNING: node streams no 'vec' rows - firmware predates the "
                  "runtime stream_vec toggle. Capturing magnitudes only "
                  "(weaker features); reflash the node to get 3-axis data.")
        return saw_vec

    def rebaseline(self) -> None:
        self.gateway.send(self.node_mac, "rebaseline")
        time.sleep(BASELINE_SETTLE_S)
        self.drain()   # discard anything captured during settling


def capture(args: argparse.Namespace) -> tuple[dict, list, Path]:
    port, baud = args.port, args.baud
    if port is None or baud is None:
        cfg_port, cfg_baud = gateway_defaults()
        port = port or cfg_port
        baud = baud or cfg_baud
    if not port:
        raise SystemExit("ERROR: no serial port (give --port or set it in "
                         "config/settings.yaml)")

    rig = BenchRig(port, baud)
    rig.resolve_mac(args.mac)
    saw_vec = rig.configure_streaming()

    if args.patterns:
        targets = [(name.strip(), None) for name in args.patterns.split(",")
                   if name.strip()]
        intro = ("Pattern capture: perform each pattern when prompted, "
                 "releasing fully between repetitions.")
    else:
        targets = [(f"{r},{c}", (r, c))
                   for r in range(args.rows) for c in range(args.cols)]
        intro = (f"Grid capture: {args.rows}x{args.cols} cells, row-major from "
                 "the top-left (as seen facing the skin). Press firmly in the "
                 "middle of the prompted cell, then release fully.")

    detector = PressDetector(enter_ut=args.enter_ut, exit_ut=args.exit_ut)
    session = CaptureSession(detector)
    pace = PaceTracker()
    commands: "queue.Queue[str]" = queue.Queue()
    threading.Thread(target=stdin_commands, args=(commands,), daemon=True).start()

    total_reps = len(targets) * args.reps
    print(f"\n{intro}")
    print(f"{len(targets)} targets x {args.reps} reps = {total_reps} presses. "
          "Commands: r=redo  s=skip  z=re-zero  q=quit&save\n")

    done_reps = 0
    quit_early = False
    for t_idx, (label, cell) in enumerate(targets):
        if quit_early:
            break
        if args.rebaseline_every and t_idx and t_idx % args.rebaseline_every == 0:
            print("  ... re-zeroing sensors (hands off) ...")
            rig.rebaseline()
        session.set_target(label, cell)
        target_name = f"cell ({label})" if cell else f"pattern '{label}'"
        eta = pace.eta_s(total_reps - done_reps)
        print(f"[{t_idx + 1}/{len(targets)}] {target_name} - {args.reps} presses"
              f"   (ETA {PaceTracker.fmt(eta)})")

        while session.target_count(label) < args.reps:
            for data, t_ms in rig.drain():
                press = session.feed_message(data, t_ms)
                if press is None:
                    continue
                done_reps += 1
                pace.mark(time.monotonic())
                eta = pace.eta_s(total_reps - done_reps)
                print(f"    rep {session.target_count(label)}/{args.reps} ok - "
                      f"peak {press.peak_energy:.0f} uT, "
                      f"{press.duration_ms:.0f} ms   (ETA {PaceTracker.fmt(eta)})")
            try:
                cmd = commands.get_nowait()
            except queue.Empty:
                time.sleep(0.03)
                continue
            if cmd == "r":
                dropped = session.drop_target(label)
                done_reps -= dropped
                print(f"    redo: dropped {dropped} presses of {target_name}")
            elif cmd == "s":
                done_reps += args.reps - session.target_count(label)
                print(f"    skipped {target_name}")
                break
            elif cmd == "z":
                print("  ... re-zeroing sensors (hands off) ...")
                rig.rebaseline()
            elif cmd == "q":
                quit_early = True
                break

    rig.gateway.disconnect()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = args.out or f"bench_{args.skin_type or 'skin'}_{stamp}.jsonl"
    out_path = Path(name)
    meta = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "skin_type": args.skin_type,
        "skin_variant": args.variant,
        "node_mac": rig.mac,
        "rows": 0 if args.patterns else args.rows,
        "cols": 0 if args.patterns else args.cols,
        "reps": args.reps,
        "cell_size_mm": args.cell_size_mm,
        "enter_ut": args.enter_ut,
        "exit_ut": args.exit_ut,
        "vec": saw_vec,
    }
    save_jsonl(out_path, meta, session.presses)
    print(f"\nSaved {len(session.presses)} presses to {out_path}")
    return meta, session.presses, out_path


def main() -> None:
    args = parse_args()
    if args.analyze:
        meta, presses = load_dataset(args.analyze)
        if not presses:
            raise SystemExit("No presses found in the given file(s).")
        run_analysis(meta, presses, Path(args.analyze[0]))
        return
    meta, presses, out_path = capture(args)
    if presses:
        run_analysis(meta, presses, out_path)


if __name__ == "__main__":
    main()
