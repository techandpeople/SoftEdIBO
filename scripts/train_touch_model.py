#!/usr/bin/env python3
"""Train per-skin-type touch-gesture models from recordings + labels.

For each ``(recording.jsonl, labels.csv)`` pair, re-segments the recorded touch
stream, matches each segment to its label, extracts coordinate-free features and
trains **one model per ``skin_type``**. Cross-validation is grouped by recording
(a stand-in for participant when per-segment participant info isn't available)
and the learned model is compared against the rule baseline so you can see
whether ML actually helps on the sparse 4-sensor hardware.

scikit-learn / joblib are required only here (the ``ml`` optional extra):
    pip install -e '.[ml]'

Usage:
  python scripts/train_touch_model.py \\
      --data data/recordings/S001.jsonl S001.labels.csv \\
      --data data/recordings/S002.jsonl S002.labels.csv
With no data it explains what to collect and exits cleanly.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ml.training import MLNotInstalled, train_models  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", nargs=2, action="append", metavar=("REC", "LABELS"),
                    default=[], help="a recording JSONL and its labels CSV")
    ap.add_argument("--models-dir", type=Path, default=None)
    args = ap.parse_args()

    if not args.data:
        print(__doc__)
        print("\nNo --data given: collect sessions first (record streams + "
              "label with scripts/label_touches.py), then re-run.")
        return 0

    from src.config.settings import Settings
    models_dir = args.models_dir or (Settings.ROOT / "models")
    try:
        train_models([(a, b) for a, b in args.data], models_dir, on_log=print)
    except MLNotInstalled as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
