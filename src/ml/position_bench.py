"""Touch-position feasibility bench - capture + analysis core (Phase 0).

Measures how much spatial information the magnet-sensor array actually
carries: guided presses on a grid overlay (or named free-form patterns) are
segmented into labelled press signatures - the per-sensor 3-axis peak deltas -
and cross-validated classifiers report the finest grid the data supports,
against non-ML baselines. See docs/TOUCH_POSITION_ML_PLAN.md (Phase 0);
CLI front-end: scripts/touch_position_bench.py.

Presses are detected PC-side from the *continuous* ``mag`` stream (total
energy across sensors with hysteresis), NOT from the firmware ``act`` set -
a press far from every sensor must still register, and the firmware threshold
exists to gate events, not to gate data collection.

Capture runs without the ``ml`` extra; the analysis needs numpy +
scikit-learn and says so instead of failing (same policy as
:mod:`src.ml.training`).
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

# Fraction of the peak total energy a sample must reach to be part of the
# peak window the signature is averaged over.
PEAK_WINDOW_FRAC = 0.7

# Transient features are appended to a UNIT-NORM signature, so they must be
# squashed to a comparable size or they own every kNN distance. Divisors are
# the "already long/large" end of each feature: 2 s for rise/fall/duration,
# 1.0 for the stability cosine, 2.0 for path length on the unit sphere and
# 20 kuT/s for the rise slope. The weight then keeps the whole block a minority
# of the distance, so the spatial signature still decides.
_TRANSIENT_SCALES = (2.0, 2.0, 2.0, 1.0, 2.0, 20.0)
_TRANSIENT_WEIGHT = 0.5


class AnalysisUnavailable(RuntimeError):
    """Raised when the analysis step needs the optional ``ml`` extra."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class BenchPress:
    """One labelled press: the full sample window plus its target label.

    ``cell`` is ``(row, col)`` for grid captures and ``None`` for named
    patterns (then ``label`` is the pattern name).
    """

    label: str
    cell: tuple[int, int] | None
    rep: int
    times_ms: list[float] = field(default_factory=list)
    mags: list[list[float]] = field(default_factory=list)
    vecs: list[list[list[float]] | None] = field(default_factory=list)

    @property
    def duration_ms(self) -> float:
        if len(self.times_ms) < 2:
            return 0.0
        return self.times_ms[-1] - self.times_ms[0]

    @property
    def sensor_count(self) -> int:
        return max((len(m) for m in self.mags), default=0)

    @property
    def has_vec(self) -> bool:
        return any(v is not None for v in self.vecs)

    def _energies(self) -> list[float]:
        return [sum(m) for m in self.mags]

    @property
    def peak_energy(self) -> float:
        return max(self._energies(), default=0.0)

    def _peak_window(self) -> list[int]:
        energies = self._energies()
        peak = max(energies, default=0.0)
        if peak <= 0.0:
            return []
        floor = peak * PEAK_WINDOW_FRAC
        return [i for i, e in enumerate(energies) if e >= floor]

    def signature(self, use_vec: bool = True) -> list[float]:
        """Peak-window mean signature: 3N-D from ``vec`` rows, N-D from mags.

        Averaging over the samples near the energy peak (>= PEAK_WINDOW_FRAC
        of it) suppresses per-sample noise without smearing the press shape.
        Falls back to the magnitude signature when vector rows are absent.
        """
        window = self._peak_window()
        n = self.sensor_count
        if not window or n == 0:
            return []

        if use_vec:
            rows = [row for row in (self.vecs[i] for i in window)
                    if row is not None]
            if rows:
                out = [0.0] * (3 * n)
                for row in rows:
                    for s in range(min(n, len(row))):
                        out[3 * s + 0] += float(row[s][0])
                        out[3 * s + 1] += float(row[s][1])
                        out[3 * s + 2] += float(row[s][2])
                return [v / len(rows) for v in out]

        out = [0.0] * n
        for i in window:
            row = self.mags[i]
            for s in range(min(n, len(row))):
                out[s] += float(row[s])
        return [v / len(window) for v in out]

    def _unit_rows(self, floor_frac: float = 0.3,
                   use_vec: bool = True) -> list[list[float]]:
        """Per-sample unit signatures for samples above ``floor_frac`` of the
        peak energy - the direction trajectory of the press. Uses vec rows when
        asked for AND available (skipping vec-less samples so dimensions stay
        consistent), else the scalar magnitudes."""
        energies = self._energies()
        peak = max(energies, default=0.0)
        if peak <= 0.0:
            return []
        use_vec = use_vec and self.has_vec
        rows: list[list[float]] = []
        for i, e in enumerate(energies):
            if e < peak * floor_frac:
                continue
            if use_vec:
                row = self.vecs[i]
                if row is None:
                    continue
                flat = [float(v) for triple in row for v in triple]
            else:
                flat = [float(v) for v in self.mags[i]]
            norm = math.sqrt(sum(v * v for v in flat))
            if norm <= 0.0:
                continue
            rows.append([v / norm for v in flat])
        return rows

    def temporal_features(self, use_vec: bool = True) -> list[float]:
        """Transient shape of the press - the plan's lever 4 (time dimension).

        Six features to append to the normalized peak signature: rise/fall/
        duration, direction stability (cosine between the first above-floor
        sample's unit signature and the last one's - two spots with similar
        peaks can differ in how the field *builds*), direction path length,
        and energy rise slope.

        Returned in physical units (seconds, cosine, kuT/s); the caller scales
        them with :func:`_scaled_transients` before appending them to a
        unit-norm signature. ``use_vec`` must match the signature they are
        appended to, or the magnitude-only feature set would smuggle in 3-axis
        information.
        """
        energies = self._energies()
        peak = max(energies, default=0.0)
        if peak <= 0.0 or len(self.times_ms) < 2:
            return [0.0] * 6
        peak_i = energies.index(peak)
        rise_s = (self.times_ms[peak_i] - self.times_ms[0]) / 1000.0
        fall_s = (self.times_ms[-1] - self.times_ms[peak_i]) / 1000.0
        dur_s = self.duration_ms / 1000.0
        units = self._unit_rows(use_vec=use_vec)
        if len(units) >= 2 and len(units[0]) == len(units[-1]):
            stability = sum(a * b for a, b in zip(units[0], units[-1]))
            path = sum(
                math.sqrt(sum((b - a) ** 2 for a, b in zip(u1, u2)))
                for u1, u2 in zip(units, units[1:])
                if len(u1) == len(u2))
        else:
            stability, path = 1.0, 0.0
        slope = peak / max(rise_s, 0.02) / 1000.0     # kuT/s
        return [rise_s, fall_s, dur_s, stability, path, slope]

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "cell": list(self.cell) if self.cell is not None else None,
            "rep": self.rep,
            "times_ms": self.times_ms,
            "mags": self.mags,
            "vecs": self.vecs,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "BenchPress":
        cell = data.get("cell")
        return cls(
            label=str(data.get("label", "")),
            cell=(int(cell[0]), int(cell[1])) if cell else None,
            rep=int(data.get("rep", 0)),
            times_ms=[float(t) for t in data.get("times_ms", [])],
            mags=[[float(v) for v in row] for row in data.get("mags", [])],
            vecs=[row if isinstance(row, list) else None
                  for row in data.get("vecs", [])],
        )


# ---------------------------------------------------------------------------
# Press detection (energy hysteresis over the continuous stream)
# ---------------------------------------------------------------------------

class PressDetector:
    """Segments presses out of the continuous magnet stream by total energy.

    A press opens when the summed per-sensor magnitude stays at/above
    ``enter_ut`` for ``confirm_samples`` consecutive samples, and closes after
    ``release_samples`` consecutive samples below ``exit_ut`` (Schmitt
    hysteresis, like the quadrant detector but on the energy sum). Segments
    shorter than ``min_duration_ms`` are dropped as noise.
    """

    def __init__(
        self,
        enter_ut: float = 60.0,
        exit_ut: float = 35.0,
        min_duration_ms: float = 120.0,
        confirm_samples: int = 2,
        release_samples: int = 3,
    ) -> None:
        if exit_ut > enter_ut:
            raise ValueError("exit_ut must not exceed enter_ut")
        self.enter_ut = float(enter_ut)
        self.exit_ut = float(exit_ut)
        self.min_duration_ms = float(min_duration_ms)
        self.confirm_samples = max(1, int(confirm_samples))
        self.release_samples = max(1, int(release_samples))
        self.reset()

    def reset(self) -> None:
        self._active = False
        self._hot_run = 0
        self._cold_run = 0
        self._pending: list[tuple[float, list[float], list[list[float]] | None]] = []
        self._cur: BenchPress | None = None

    @property
    def is_active(self) -> bool:
        return self._active

    @staticmethod
    def _sample_of(msg: dict[str, Any]) -> tuple[list[float], list[list[float]] | None]:
        mags: list[float] = []
        for v in msg.get("mag") or []:
            try:
                mags.append(float(v))
            except (TypeError, ValueError):
                mags.append(0.0)
        vec = msg.get("vec")
        return mags, vec if isinstance(vec, list) else None

    def feed(self, msg: dict[str, Any], t_ms: float) -> BenchPress | None:
        """Process one ``magnet`` message; returns a finished press or None."""
        mags, vec = self._sample_of(msg)
        energy = sum(mags)
        if self._active:
            return self._feed_active(t_ms, mags, vec, energy)
        self._feed_idle(t_ms, mags, vec, energy)
        return None

    def _feed_idle(self, t_ms: float, mags: list[float],
                   vec: list[list[float]] | None, energy: float) -> None:
        """Waiting for a press: open one after ``confirm_samples`` hot in a row,
        replaying the samples held back so the rise is not clipped."""
        if energy < self.enter_ut:
            self._hot_run = 0
            self._pending = []
            return
        self._hot_run += 1
        self._pending.append((t_ms, mags, vec))
        if self._hot_run < self.confirm_samples:
            return
        self._active = True
        self._cold_run = 0
        self._cur = BenchPress(label="", cell=None, rep=0)
        for pt, pm, pv in self._pending:
            self._append(pt, pm, pv)
        self._pending = []

    def _feed_active(self, t_ms: float, mags: list[float],
                     vec: list[list[float]] | None,
                     energy: float) -> BenchPress | None:
        """Inside a press: close it after ``release_samples`` cold in a row,
        and hand it back unless it was too short to be anything but noise."""
        self._append(t_ms, mags, vec)
        if energy >= self.exit_ut:
            self._cold_run = 0
            return None
        self._cold_run += 1
        if self._cold_run < self.release_samples:
            return None
        press, self._cur = self._cur, None
        self._active = False
        self._hot_run = 0
        self._cold_run = 0
        if press is not None and press.duration_ms >= self.min_duration_ms:
            return press
        return None

    def _append(self, t_ms: float, mags: list[float],
                vec: list[list[float]] | None) -> None:
        assert self._cur is not None
        self._cur.times_ms.append(t_ms)
        self._cur.mags.append(mags)
        self._cur.vecs.append(vec)


# ---------------------------------------------------------------------------
# Guided capture session
# ---------------------------------------------------------------------------

class CaptureSession:
    """Assigns detected presses to the current capture target.

    The CLI sets the target (a grid cell or a pattern name), feeds every
    magnet message through, and gets back the labelled press when one
    completes. ``drop_target`` supports redoing a target.
    """

    def __init__(self, detector: PressDetector) -> None:
        self._detector = detector
        self._label: str | None = None
        self._cell: tuple[int, int] | None = None
        self.presses: list[BenchPress] = []

    def set_target(self, label: str, cell: tuple[int, int] | None = None) -> None:
        self._label = label
        self._cell = cell
        self._detector.reset()

    def target_count(self, label: str) -> int:
        return sum(1 for p in self.presses if p.label == label)

    def drop_target(self, label: str) -> int:
        """Remove every press captured for ``label``; returns how many."""
        before = len(self.presses)
        self.presses = [p for p in self.presses if p.label != label]
        return before - len(self.presses)

    def feed_message(self, msg: dict[str, Any], t_ms: float) -> BenchPress | None:
        if self._label is None:
            return None
        press = self._detector.feed(msg, t_ms)
        if press is None:
            return None
        press.label = self._label
        press.cell = self._cell
        press.rep = self.target_count(self._label)
        self.presses.append(press)
        return press


class PaceTracker:
    """Estimates time left in a capture run from the user's actual pace.

    ``mark()`` on every accepted repetition; ``eta_s(remaining)`` multiplies
    the *median* of the recent inter-repetition intervals (robust to pauses)
    by the repetitions still to go. Returns None until two marks exist.
    """

    def __init__(self, window: int = 15) -> None:
        self._window = max(2, int(window))
        self._marks: list[float] = []

    def mark(self, t_s: float) -> None:
        self._marks.append(float(t_s))
        if len(self._marks) > self._window + 1:
            self._marks = self._marks[-(self._window + 1):]

    def seconds_per_rep(self) -> float | None:
        if len(self._marks) < 2:
            return None
        gaps = [b - a for a, b in zip(self._marks, self._marks[1:])]
        return statistics.median(gaps)

    def eta_s(self, remaining_reps: int) -> float | None:
        per = self.seconds_per_rep()
        if per is None:
            return None
        return per * max(0, remaining_reps)

    @staticmethod
    def fmt(seconds: float | None) -> str:
        if seconds is None:
            return "--:--"
        s = max(0, int(round(seconds)))
        return f"{s // 60}:{s % 60:02d}"


# ---------------------------------------------------------------------------
# Dataset I/O (JSONL: one header line, then one line per press)
# ---------------------------------------------------------------------------

def save_jsonl(path: Path | str, meta: dict[str, Any],
               presses: Sequence[BenchPress]) -> None:
    path = Path(path)
    with open(path, "w", encoding="utf-8") as f:
        header = {"kind": "touch_position_bench", **meta}
        f.write(json.dumps(header) + "\n")
        for p in presses:
            f.write(json.dumps(p.to_json()) + "\n")


def load_dataset(paths: Iterable[Path | str]) -> tuple[dict[str, Any], list[BenchPress]]:
    """Load one or more bench files; meta comes from the first header."""
    meta: dict[str, Any] = {}
    presses: list[BenchPress] = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if i == 0 and data.get("kind") == "touch_position_bench":
                    if not meta:
                        meta = data
                    continue
                presses.append(BenchPress.from_json(data))
    return meta, presses


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Models:
    """The lazily-imported numpy/sklearn handles, carried as one collaborator.

    ``ml`` is an optional extra, so nothing here can be imported at module
    level; bundling the handles keeps the report helpers from threading six
    positional parameters through each other.
    """

    np: Any
    forest_clf: Any
    forest_reg: Any
    kfold: Any
    cross_val_predict: Any
    knn_clf: Any

    def rf(self) -> Any:
        """A fresh classification forest, seeded so reports are reproducible."""
        return self.forest_clf(n_estimators=300, random_state=0)

    def knn(self, neighbors: int = 3) -> Any:
        return self.knn_clf(n_neighbors=neighbors)

    def classifiers(self) -> list[tuple[str, Any]]:
        """The two scored side by side in every section."""
        return [("kNN(3)", self.knn()), ("RandomForest", self.rf())]


def _import_ml() -> _Models:
    try:
        import numpy as np
        from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
        from sklearn.model_selection import StratifiedKFold, cross_val_predict
        from sklearn.neighbors import KNeighborsClassifier
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise AnalysisUnavailable(
            "Analysis needs numpy + scikit-learn - install the ml extra: "
            "pip install -e '.[ml]'"
        ) from exc
    return _Models(np=np, forest_clf=RandomForestClassifier,
                   forest_reg=RandomForestRegressor, kfold=StratifiedKFold,
                   cross_val_predict=cross_val_predict,
                   knn_clf=KNeighborsClassifier)


def _scaled_transients(raw: Sequence[float]) -> list[float]:
    """Squash the physical transient features so they can join a unit signature.

    They are appended to a vector of norm 1, so raw seconds and kuT/s would own
    every distance a kNN computes - the spatial pattern would stop deciding.
    """
    return [_TRANSIENT_WEIGHT * min(v / scale, 1.0)
            for v, scale in zip(raw, _TRANSIENT_SCALES)]


def _normalized_signatures(
    presses: Sequence[BenchPress], use_vec: bool, with_time: bool = False,
) -> tuple[list[list[float]], list[int]]:
    """Unit-normalized signatures + indices of the presses that yielded one.

    Normalizing strips press strength (magnitude ~ force), leaving the spatial
    pattern - see the force/position separation argument in the plan doc.
    ``with_time`` appends the six transient features (lever 4): the press
    *trajectory* carries information the peak snapshot collapses away.
    """
    rows: list[list[float]] = []
    kept: list[int] = []
    for i, p in enumerate(presses):
        sig = p.signature(use_vec=use_vec)
        norm = math.sqrt(sum(v * v for v in sig))
        if not sig or norm <= 0.0:
            continue
        row = [v / norm for v in sig]
        if with_time:
            row += _scaled_transients(p.temporal_features(use_vec=use_vec))
        rows.append(row)
        kept.append(i)

    # ``signature(use_vec=True)`` falls back to the N-D magnitudes for a press
    # with no vec rows, so a dataset mixing pre- and post-reflash captures would
    # otherwise build a ragged matrix that numpy refuses. Keep the majority
    # dimension and drop the rest; the caller reports the loss via ``kept``.
    if rows:
        widths = [len(r) for r in rows]
        want = max(set(widths), key=widths.count)
        keep_at = [j for j, r in enumerate(rows) if len(r) == want]
        rows = [rows[j] for j in keep_at]
        kept = [kept[j] for j in keep_at]
    return rows, kept


def _quadrant_of(cell: tuple[int, int], rows: int, cols: int) -> str:
    r = 0 if cell[0] < rows / 2 else 1
    c = 0 if cell[1] < cols / 2 else 1
    return f"{r},{c}"


def _dominant_sensor_quadrant(press: BenchPress) -> str | None:
    """Today's logic as a baseline: strongest of the first 4 sensors -> quadrant.

    Wiring order S0..S3 = Q1(TL) Q2(TR) Q3(BL) Q4(BR) per
    docs/TOUCH_POSITION_TRACKING.md.
    """
    sig = press.signature(use_vec=False)[:4]
    if not sig or max(sig) <= 0.0:
        return None
    s = sig.index(max(sig))
    return {0: "0,0", 1: "0,1", 2: "1,0", 3: "1,1"}.get(s)


def _label_counts(y: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for label in y:
        counts[label] = counts.get(label, 0) + 1
    return counts


def _cv_accuracy(models: _Models, clf, X, y) -> float | None:
    """Stratified CV accuracy; None when a class is too small to fold."""
    counts = _label_counts(y)
    min_count = min(counts.values())
    if len(counts) < 2 or min_count < 2:
        return None
    from sklearn.model_selection import cross_val_score
    skf = models.kfold(n_splits=min(5, min_count), shuffle=True, random_state=0)
    scores = cross_val_score(clf, models.np.asarray(X), models.np.asarray(y),
                             cv=skf)
    return float(scores.mean())


def analyze(meta: dict[str, Any], presses: Sequence[BenchPress]) -> str:
    """Build the feasibility report for a captured dataset.

    Raises :class:`AnalysisUnavailable` without the ``ml`` extra installed.
    """
    models = _import_ml()

    lines: list[str] = ["=== Touch position bench report ==="]
    grid_presses = [p for p in presses if p.cell is not None]
    pattern_presses = [p for p in presses if p.cell is None]
    has_vec = any(p.has_vec for p in presses)

    rows = int(meta.get("rows") or 0)
    cols = int(meta.get("cols") or 0)
    cell_mm = meta.get("cell_size_mm")
    lines.append(
        f"presses: {len(presses)} ({len(grid_presses)} grid, "
        f"{len(pattern_presses)} pattern) | grid {rows}x{cols} | "
        f"vec data: {'yes' if has_vec else 'NO (magnitudes only)'}"
    )

    if grid_presses and rows and cols:
        lines += _grid_section(models, grid_presses, rows, cols, cell_mm,
                               has_vec)
    if pattern_presses:
        lines += _pattern_section(models, pattern_presses, has_vec)
    return "\n".join(lines)


@dataclass
class _Scored:
    """The best-scoring feature set, and the matrix that produced it."""

    accuracy: float
    X: list[list[float]]
    y: list[str]
    kept: list[int]


def _grid_section(models: _Models, presses: list[BenchPress], rows: int,
                  cols: int, cell_mm, has_vec: bool) -> list[str]:
    out = ["", f"-- Grid cells ({rows}x{cols} = {rows * cols}) --"]
    best, score_lines = _score_feature_sets(models, presses, has_vec)
    out += score_lines
    if best is None:
        out.append("  (not enough data per cell for cross-validation)")
        return out
    out += _quadrant_lines(models, presses, best, rows, cols)
    out += _regression_lines(models, presses, best, cell_mm)
    out += _recall_lines(models, best, rows, cols)
    return out


def _feature_sets(has_vec: bool) -> list[tuple[str, bool, bool]]:
    """(label, use_vec, with_time). The mag rows are the control for the vec
    ones, so they are always scored, even on a vec-capable dataset."""
    sets = ([("vec 3-axis", True, False), ("vec+transient", True, True)]
            if has_vec else [])
    return sets + [("mag scalar", False, False), ("mag+transient", False, True)]


def _score_feature_sets(models: _Models, presses: list[BenchPress],
                        has_vec: bool) -> tuple[_Scored | None, list[str]]:
    """Cross-validate every feature set x classifier; keep the best matrix."""
    out: list[str] = []
    best: _Scored | None = None
    for name, use_vec, with_time in _feature_sets(has_vec):
        X, kept = _normalized_signatures(presses, use_vec=use_vec,
                                         with_time=with_time)
        y = [presses[i].label for i in kept]
        if not X:
            out.append(f"  {name}: no usable signatures")
            continue
        if len(kept) < len(presses):
            out.append(f"  {name}: {len(presses) - len(kept)} of {len(presses)} "
                       "presses skipped (no usable signature at this width)")
        dims = len(X[0])
        for clf_name, clf in models.classifiers():
            acc = _cv_accuracy(models, clf, X, y)
            shown = (f"{acc * 100:5.1f} %" if acc is not None
                     else "  n/a (too few reps)")
            out.append(f"  {clf_name:<13} {name:<14} ({dims:>2}-D): {shown}")
            if acc is not None and (best is None or acc > best.accuracy):
                best = _Scored(acc, X, y, kept)
    return best, out


def _quadrant_lines(models: _Models, presses: list[BenchPress], best: _Scored,
                    rows: int, cols: int) -> list[str]:
    """Merged 2x2 (today's resolution) vs the dominant-sensor baseline."""
    quad_y = [_quadrant_of(presses[i].cell, rows, cols)  # type: ignore[arg-type]
              for i in best.kept]
    quad_acc = _cv_accuracy(models, models.rf(), best.X, quad_y)
    hits = total = 0
    for i in best.kept:
        guess = _dominant_sensor_quadrant(presses[i])
        if guess is None:
            continue
        total += 1
        hits += guess == _quadrant_of(presses[i].cell, rows, cols)  # type: ignore[arg-type]

    out = ["", "  Merged 2x2 (quadrants):"]
    if quad_acc is not None:
        out.append(f"    best features + RF      : {quad_acc * 100:5.1f} %")
    if total:
        out.append(f"    dominant-sensor baseline: {hits / total * 100:5.1f} %"
                   "   <- roughly today's quadrant logic")
    return out


def _regression_lines(models: _Models, presses: list[BenchPress],
                      best: _Scored, cell_mm) -> list[str]:
    """Continuous (x, y) position error, in cells and mm."""
    counts = _label_counts(best.y)
    if len(counts) < 2 or min(counts.values()) < 2:
        return []
    np = models.np
    Y = np.asarray([[presses[i].cell[0], presses[i].cell[1]]  # type: ignore[index]
                    for i in best.kept], dtype=float)
    reg = models.forest_reg(n_estimators=300, random_state=0)
    pred = models.cross_val_predict(reg, np.asarray(best.X), Y,
                                    cv=min(5, len(best.X)))
    err_cells = float(np.mean(np.linalg.norm(pred - Y, axis=1)))
    mm = f" (= {err_cells * float(cell_mm):.1f} mm)" if cell_mm else ""
    return [f"  Continuous (x,y) regression mean error: "
            f"{err_cells:.2f} cells{mm}"]


def _recall_lines(models: _Models, best: _Scored, rows: int,
                  cols: int) -> list[str]:
    """Per-cell recall heat grid + worst confusions, from CV predictions."""
    counts = _label_counts(best.y)
    np = models.np
    skf = models.kfold(n_splits=min(5, min(counts.values())), shuffle=True,
                       random_state=0)
    y_pred = models.cross_val_predict(models.rf(), np.asarray(best.X),
                                      np.asarray(best.y), cv=skf)
    recall: dict[str, tuple[int, int]] = {}
    confusions: dict[tuple[str, str], int] = {}
    for truth, pred_label in zip(best.y, y_pred):
        ok, n = recall.get(truth, (0, 0))
        recall[truth] = (ok + (pred_label == truth), n + 1)
        if pred_label != truth:
            key = (truth, str(pred_label))
            confusions[key] = confusions.get(key, 0) + 1

    out = ["", "  Per-cell recall (%, RF on best features):"]
    for r in range(rows):
        vals = []
        for c in range(cols):
            ok, n = recall.get(f"{r},{c}", (0, 0))
            vals.append(f"{ok / n * 100:4.0f}" if n else "   -")
        out.append("    " + " ".join(vals))
    if confusions:
        worst = sorted(confusions.items(), key=lambda kv: -kv[1])[:5]
        out.append("  Worst confusions: " + ", ".join(
            f"{a}->{b} x{n}" for (a, b), n in worst))
    return out


def _pattern_section(models: _Models, presses: list[BenchPress],
                     has_vec: bool) -> list[str]:
    out = ["", f"-- Patterns ({len({p.label for p in presses})} classes) --"]
    # Transients matter most here (a squeeze *builds* differently from a
    # press), so patterns are scored with and without them.
    for tag, with_time in (("peak only", False), ("+transient", True)):
        X, kept = _normalized_signatures(presses, use_vec=has_vec,
                                         with_time=with_time)
        y = [presses[i].label for i in kept]
        if not X:
            out.append("  no usable signatures")
            return out
        for clf_name, clf in models.classifiers():
            acc = _cv_accuracy(models, clf, X, y)
            shown = (f"{acc * 100:5.1f} %" if acc is not None
                     else "n/a (too few reps)")
            out.append(f"  {clf_name:<13} {tag:<11}: {shown}")
    X, kept = _normalized_signatures(presses, use_vec=has_vec)
    y = [presses[i].label for i in kept]
    for label in sorted(set(y)):
        sub = [presses[i] for i in kept if presses[i].label == label]
        peak = statistics.median(p.peak_energy for p in sub)
        spread = statistics.median(
            sum(1 for v in p.signature(use_vec=False) if v >= 20.0) for p in sub)
        out.append(f"    {label:<16} reps={len(sub):<3} median peak={peak:6.0f} uT"
                   f"  sensors involved~{spread:.0f}")
    return out
