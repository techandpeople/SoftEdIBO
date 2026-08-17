"""Touch-gesture training core - shared by the CLI script and the GUI.

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


def _magnet_by_source(recording: Path) -> dict[str, list]:
    """``{source: [(msg, t_ms), ...]}`` for every magnet message in a recording."""
    by_source: dict[str, list] = {}
    with open(recording, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            msg = obj.get("msg")
            if isinstance(msg, dict) and msg.get("type") == "magnet":
                by_source.setdefault(msg.get("source", "?"), []).append(
                    (msg, _epoch_ms(obj["t"])))
    return by_source


def segments_of(recording: Path):
    """Re-segment a recording JSONL into (source, TouchSegment) pairs.

    When a source carries pressure-compensated magnet messages (recorded
    alongside the raw stream while compensation was enabled), only those are
    segmented - live inference consumes the compensated stream, so training
    must see the same data (no train/serve skew)."""
    out = []
    for source, samples in _magnet_by_source(recording).items():
        compensated = [(m, t) for m, t in samples if m.get("compensated")]
        for seg in TouchSegmenter().segment_stream(compensated or samples):
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
    recording + source) are merged into a single sample - that's how a multi-tap
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
    # Cross-validated confusion matrix (rows = true, cols = predicted) and its
    # label order, when an honest CV ran; ``None`` when CV was skipped.
    confusion: list[list[int]] | None = None
    labels: list[str] = field(default_factory=list)


@dataclass
class TrainingReport:
    results: list[TypeResult] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    def text(self) -> str:
        return "\n".join(self.log)


def _new_report(on_log: Callable[[str], None] | None):
    """A fresh :class:`TrainingReport` plus a ``log`` that mirrors to ``on_log``."""
    report = TrainingReport()

    def log(msg: str) -> None:
        report.log.append(msg)
        if on_log:
            on_log(msg)

    return report, log


def _import_ml():
    """Lazily import the optional ML stack, or raise :class:`MLNotInstalled`."""
    try:
        import joblib
        import numpy as np
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.metrics import (accuracy_score, classification_report,
                                     confusion_matrix)
        from sklearn.model_selection import (GroupKFold, StratifiedKFold,
                                             cross_val_predict)
    except ImportError as exc:
        raise MLNotInstalled(
            "scikit-learn/joblib/numpy not installed. Run: pip install -e '.[ml]'"
        ) from exc
    return {"joblib": joblib, "np": np, "rf": RandomForestClassifier,
            "accuracy_score": accuracy_score,
            "classification_report": classification_report,
            "confusion_matrix": confusion_matrix, "GroupKFold": GroupKFold,
            "StratifiedKFold": StratifiedKFold,
            "cross_val_predict": cross_val_predict}


def _per_type_bins(samples) -> dict[str, dict]:
    """Bucket ``(skin_type, features, label, baseline, group)`` tuples by type."""
    per_type: dict[str, dict] = {}
    for skin_type, fv, label, base, group in samples:
        d = per_type.setdefault(skin_type,
                                {"X": [], "y": [], "base": [], "g": []})
        d["X"].append(fv); d["y"].append(label)
        d["base"].append(base); d["g"].append(group)
    return per_type


def _cv_splitter(ml, cv: str, y, groups):
    """Pick a cross-validation splitter for the fitting mode.

    Returns ``(splitter, groups_arg, description)`` for an honest CV, or
    ``(None, None, reason)`` when there isn't enough structure to cross-validate
    (single recording for ``"group"``; a class with <2 samples for ``"stratified"``).
    """
    if cv == "group":
        n_groups = len(set(groups))
        if n_groups < 2:
            return None, None, "only one recording - fitting on all data"
        return (ml["GroupKFold"](n_splits=min(n_groups, 5)), groups,
                "grouped by recording")
    # "stratified": guided capture is a single session, so group CV would see one
    # group and never hold anything out. Stratified k-fold gives an honest
    # held-out estimate over the independent repetitions instead.
    counts: dict[str, int] = {}
    for label in y:
        counts[label] = counts.get(label, 0) + 1
    min_class = min(counts.values())
    if min_class < 2:
        return None, None, ("a gesture has <2 samples - fitting on all data; "
                            "capture more repetitions")
    n_splits = min(min_class, 5)
    return (ml["StratifiedKFold"](n_splits=n_splits, shuffle=True,
                                  random_state=0),
            None, f"stratified {n_splits}-fold")


def _fit_per_type(per_type: dict[str, dict], models_dir: Path, log,
                  cv: str) -> list[TypeResult]:
    """Fit + cross-validate one model per ``skin_type`` and save each to disk.

    ``cv`` is ``"group"`` (recording-grouped, for :func:`train_models`) or
    ``"stratified"`` (for :func:`train_from_segments`). The RandomForest and the
    >=10-sample / >=2-class gate are identical across both modes; only the CV
    splitter differs.
    """
    ml = _import_ml()
    np = ml["np"]
    models_dir.mkdir(parents=True, exist_ok=True)
    results: list[TypeResult] = []
    for skin_type, d in per_type.items():
        X, y, base, groups = np.array(d["X"]), np.array(d["y"]), d["base"], d["g"]
        n, n_classes = len(y), len(set(y))
        res = TypeResult(skin_type=skin_type, n_samples=n, n_classes=n_classes,
                         baseline_acc=float(ml["accuracy_score"](y, base)))
        log(f"\n=== {skin_type or '(none)'}: {n} samples, {n_classes} classes ===")
        log(f"rule baseline accuracy: {res.baseline_acc:.3f}")
        if n < 10 or n_classes < 2:
            log("Too few samples/classes to train - collect more. Skipping.")
            results.append(res)
            continue

        # balanced: real sessions are dominated by taps, with a handful of
        # squeezes/strokes - unweighted trees would just learn the majority.
        clf = ml["rf"](n_estimators=200, random_state=0, class_weight="balanced",
                       max_features="sqrt", min_samples_leaf=1)
        splitter, groups_arg, why = _cv_splitter(ml, cv, y, groups)
        if splitter is not None:
            pred = ml["cross_val_predict"](clf, X, y, groups=groups_arg,
                                           cv=splitter)
            labels_sorted = sorted(set(y))
            res.model_acc = float(ml["accuracy_score"](y, pred))
            res.report = ml["classification_report"](y, pred, zero_division=0)
            res.labels = labels_sorted
            res.confusion = ml["confusion_matrix"](
                y, pred, labels=labels_sorted).tolist()
            log(f"model CV accuracy ({why}): {res.model_acc:.3f}")
            log(res.report)
        else:
            log(f"No honest CV ({why}).")

        clf.fit(X, y)
        out = models_dir / f"touch_{skin_type}.joblib"
        ml["joblib"].dump(clf, out)
        res.trained = True
        res.model_path = str(out)
        log(f"saved -> {out}")
        results.append(res)
    return results


def train_models(pairs, models_dir: Path,
                 on_log: Callable[[str], None] | None = None) -> TrainingReport:
    """Train one model per ``skin_type`` from ``(recording, labels)`` pairs.

    Raises :class:`MLNotInstalled` if scikit-learn isn't available.
    """
    report, log = _new_report(on_log)
    per_type = _per_type_bins(collect_samples(pairs))
    if not per_type:
        log("No labelled segments matched the recordings - check the CSVs.")
        return report
    report.results = _fit_per_type(per_type, models_dir, log, cv="group")
    return report


def train_from_segments(labeled, models_dir: Path,
                        on_log: Callable[[str], None] | None = None
                        ) -> TrainingReport:
    """Train per-``skin_type`` models directly from labelled touch segments.

    ``labeled`` is an iterable of ``(skin_type, skin_variant, label, segment)`` -
    e.g. from :meth:`src.ml.gesture_capture.GestureCaptureSession.labeled_samples`.
    Feeds the same :func:`_sample` / :func:`~src.ml.touch_features.full_feature_vector`
    pipeline the recording path uses (so features are identical), then cross-
    validates with stratified k-fold (a guided capture is one session, so
    recording-grouped CV can't hold anything out). Each :class:`TypeResult` carries
    an accuracy and a confusion matrix. Raises :class:`MLNotInstalled` without
    scikit-learn.
    """
    report, log = _new_report(on_log)
    samples = (_sample(skin_type, skin_variant, label, seg, 0)
               for skin_type, skin_variant, label, seg in labeled)
    per_type = _per_type_bins(samples)
    if not per_type:
        log("No captured segments to train on.")
        return report
    report.results = _fit_per_type(per_type, models_dir, log, cv="stratified")
    return report
