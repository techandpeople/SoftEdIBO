"""Touch-gesture training core — shared by the CLI script and the GUI.

Keeps the data-wrangling (segment recordings, match labels, extract features)
and the model fitting in one place so ``scripts/train_touch_model.py`` and the
Train Touch Models dialog behave identically. scikit-learn / joblib / numpy are
imported lazily inside :func:`train_models`, so importing this module never
requires the optional ``ml`` extra.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from src.ml import rule_baseline
from src.ml.touch_features import full_feature_vector
from src.ml.touch_segmenter import TouchSegmenter, merge_segments


class MLNotInstalled(RuntimeError):
    """Raised when the optional ``ml`` extra (scikit-learn) is missing."""


def _epoch_ms(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp() * 1000.0


def segments_of(recording: Path):
    """Re-segment a recording JSONL into (source, TouchSegment) pairs."""
    by_source: dict[str, list] = {}
    with open(recording, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "msg" not in obj:
                continue
            msg = obj["msg"]
            if msg.get("type") != "magnet":
                continue
            by_source.setdefault(msg.get("source", "?"), []).append(
                (msg, _epoch_ms(obj["t"])))
    out = []
    for source, samples in by_source.items():
        for seg in TouchSegmenter().segment_stream(samples):
            out.append((source, seg))
    return out


def _labels_of(labels_csv: Path) -> dict:
    out = {}
    with open(labels_csv, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row.get("source", "?"), round(float(row["start_ms"])))
            out[key] = (row.get("skin_type", ""), row.get("skin_variant", ""),
                        row["label"], int(row.get("group_id") or 0))
    return out


def collect_samples(pairs):
    """Yield ``(skin_type, feature_vector, label, baseline_label, group)``.

    ``pairs`` is a list of ``(recording_path, labels)`` where ``labels`` is
    either a CSV path or an in-memory ``{(source, round(start_ms)):
    (skin_type, skin_variant, label, group_id)}`` map (so the GUI can train
    without writing a CSV). Rows sharing a non-zero ``group_id`` (within a
    recording + source) are merged into a single sample — that's how a multi-tap
    is labelled as one gesture (see :func:`merge_segments`)."""
    for gi, (rec, lab) in enumerate(pairs):
        labels = lab if isinstance(lab, dict) else _labels_of(Path(lab))
        # Collect grouped segments first; emit ungrouped ones immediately.
        grouped: dict[tuple, dict] = {}
        for source, seg in segments_of(Path(rec)):
            entry = labels.get((source, round(seg.start_ms)))
            if entry is None:
                continue
            skin_type, skin_variant, label, group_id = entry
            if group_id:
                g = grouped.setdefault(
                    (source, group_id),
                    {"skin_type": skin_type, "skin_variant": skin_variant,
                     "label": label, "segs": []})
                g["segs"].append(seg)
            else:
                yield _sample(skin_type, skin_variant, label, seg, gi)
        for g in grouped.values():
            merged = merge_segments(g["segs"])
            if merged is not None:
                yield _sample(g["skin_type"], g["skin_variant"], g["label"],
                              merged, gi)


def _sample(skin_type, skin_variant, label, seg, gi):
    """One training tuple ``(skin_type, features, label, baseline, group)``.

    Features include the one-hot silicone variant so the per-shape model can
    adapt across silicone formats."""
    return (skin_type, full_feature_vector(seg, skin_variant), label,
            rule_baseline.classify(seg), f"rec{gi}")


@dataclass
class TypeResult:
    skin_type: str
    n_samples: int
    n_classes: int
    baseline_acc: float
    model_acc: float | None = None
    trained: bool = False
    model_path: str = ""
    report: str = ""


@dataclass
class TrainingReport:
    results: list[TypeResult] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(self.log)


def train_models(pairs, models_dir: Path,
                 on_log: Callable[[str], None] | None = None) -> TrainingReport:
    """Train one model per ``skin_type`` from ``(recording, labels)`` pairs.

    Raises :class:`MLNotInstalled` if scikit-learn isn't available.
    """
    report = TrainingReport()

    def log(msg: str) -> None:
        report.log.append(msg)
        if on_log:
            on_log(msg)

    try:
        import joblib
        import numpy as np
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.metrics import accuracy_score, classification_report
        from sklearn.model_selection import GroupKFold, cross_val_predict
    except ImportError as exc:
        raise MLNotInstalled(
            "scikit-learn/joblib/numpy not installed. Run: pip install -e '.[ml]'"
        ) from exc

    models_dir.mkdir(parents=True, exist_ok=True)

    per_type: dict[str, dict] = {}
    for skin_type, fv, label, base, group in collect_samples(pairs):
        d = per_type.setdefault(skin_type, {"X": [], "y": [], "base": [], "g": []})
        d["X"].append(fv); d["y"].append(label); d["base"].append(base); d["g"].append(group)

    if not per_type:
        log("No labelled segments matched the recordings — check the CSVs.")
        return report

    for skin_type, d in per_type.items():
        X, y, base, groups = np.array(d["X"]), np.array(d["y"]), d["base"], d["g"]
        n, n_classes = len(y), len(set(y))
        res = TypeResult(skin_type=skin_type, n_samples=n, n_classes=n_classes,
                         baseline_acc=float(accuracy_score(y, base)))
        log(f"\n=== {skin_type or '(none)'}: {n} samples, {n_classes} classes ===")
        log(f"rule baseline accuracy: {res.baseline_acc:.3f}")
        if n < 10 or n_classes < 2:
            log("Too few samples/classes to train — collect more. Skipping.")
            report.results.append(res)
            continue

        clf = RandomForestClassifier(n_estimators=200, random_state=0)
        n_groups = len(set(groups))
        if n_groups >= 2:
            cv = GroupKFold(n_splits=min(n_groups, 5))
            pred = cross_val_predict(clf, X, y, groups=groups, cv=cv)
            res.model_acc = float(accuracy_score(y, pred))
            res.report = classification_report(y, pred, zero_division=0)
            log(f"model CV accuracy (grouped by recording): {res.model_acc:.3f}")
            log(res.report)
        else:
            log("Only one recording — no honest CV; fitting on all data.")

        clf.fit(X, y)
        out = models_dir / f"touch_{skin_type}.joblib"
        joblib.dump(clf, out)
        res.trained = True
        res.model_path = str(out)
        log(f"saved → {out}")
        report.results.append(res)

    return report
