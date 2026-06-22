#!/usr/bin/env python3
"""Offline touch-gesture labeller (CLI) — recording + live tags → labels CSV.

A thin command-line front-end over ``src/ml/labeling.py`` (the same logic the
in-app **Tools → Touch Gestures…** dialog uses). Aligns the live
``gesture_label`` events tapped during a session with the touch segments in its
recording; ``--review`` confirms/corrects each before writing the CSV.

Usage:
  python scripts/label_touches.py --recording data/recordings/S001.jsonl \\
      --session S001 [--skin-type turtle_square] [--out S001.labels.csv] \\
      [--review]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ml import labeling  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--recording", required=True, type=Path)
    ap.add_argument("--session", required=True)
    ap.add_argument("--skin-type", default="")
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--review", action="store_true",
                    help="confirm/correct each label interactively")
    args = ap.parse_args()

    if not args.recording.exists():
        print(f"Recording not found: {args.recording}", file=sys.stderr)
        return 1

    events = labeling.gesture_events_from_db(args.session)
    rows = labeling.label_rows_for(args.recording, args.skin_type, events)
    print(f"{len(rows)} touch segment(s), {len(events)} live label(s).")

    if args.review:
        for r in rows:
            ans = input(
                f"[{r.source}] {r.duration_ms:.0f} ms → suggested: "
                f"{r.label or '?'}\n  label (Enter=keep, name to set, s=skip): "
            ).strip()
            if ans.lower() == "s":
                r.label = ""
            elif ans:
                r.label = ans

    out_path = args.out or args.recording.with_suffix(".labels.csv")
    n = labeling.write_csv(rows, out_path)
    print(f"Wrote {n} labelled segments → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
