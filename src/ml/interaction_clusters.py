"""Interaction styles: unsupervised clusters of touching episodes.

Groups the episodes of :mod:`src.data.interaction_dynamics` by how they were
performed (how many touches, how fast, how regular, how hard, how long, how
much together with others), without any labels. Each cluster is a recurring
style of touching; the summary says what characterises it and how widely it
occurs, so a style seen in one child only is not mistaken for a pattern.

scikit-learn / numpy are imported lazily (the optional ``ml`` extra).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.data.interaction_model import Column, Table

# Episode columns the clustering uses (missing values become the column mean).
CLUSTER_FEATURES = ("touches", "duration_ms", "tempo_bpm", "interval_cv",
                    "mean_touch_ms", "mean_peak_ut", "together_share")
MIN_EPISODES = 8
MAX_CLUSTERS = 6

CLUSTER_COLUMNS = (
    Column("cluster", "Style number (also written on each episode).", "count"),
    Column("episodes", "Episodes in the style.", "count"),
    Column("sources", "Different children (or skins/sensors) showing it.", "count"),
    Column("sessions", "Different sessions it appears in.", "count"),
    *(Column(name, f"Mean {name.replace('_', ' ')} of the style's episodes.")
      for name in CLUSTER_FEATURES),
)


class MLNotInstalled(RuntimeError):
    """Raised when the optional ``ml`` extra (scikit-learn) is missing."""


@dataclass
class ClusterResult:
    """Outcome of clustering: the summary, a label per episode, and a note."""

    summary: Table
    labels: list[int | None] = field(default_factory=list)
    silhouette: float | None = None
    note: str = ""


def _matrix(rows: list[dict]) -> list[list[float]]:
    """Feature rows, with each missing value replaced by its column's mean."""
    columns = []
    for name in CLUSTER_FEATURES:
        values = [row.get(name) for row in rows]
        known = [float(v) for v in values if v is not None]
        mean = sum(known) / len(known) if known else 0.0
        columns.append([float(v) if v is not None else mean for v in values])
    return [list(values) for values in zip(*columns)]


def cluster_episodes(episodes: Table) -> ClusterResult:
    """Cluster ``episodes`` into styles (k chosen by silhouette score).

    Returns an empty summary with an explanatory ``note`` when there are too
    few episodes. Raises :class:`MLNotInstalled` without scikit-learn."""
    empty = Table("Styles", CLUSTER_COLUMNS, [])
    rows = episodes.rows
    if len(rows) < MIN_EPISODES:
        return ClusterResult(
            empty, [None] * len(rows),
            note=f"Styles need at least {MIN_EPISODES} episodes; "
                 f"{len(rows)} available.")
    try:
        from sklearn.cluster import KMeans
        from sklearn.metrics import silhouette_score
        from sklearn.preprocessing import StandardScaler
    except ImportError as exc:
        raise MLNotInstalled(
            "scikit-learn not installed. Run: pip install -e '.[ml]'") from exc

    scaled = StandardScaler().fit_transform(_matrix(rows))
    best_labels, best_score = None, -1.0
    for k in range(2, min(MAX_CLUSTERS, len(rows) - 1) + 1):
        labels = KMeans(n_clusters=k, random_state=0).fit_predict(scaled)
        if len(set(labels)) < 2:
            continue
        score = float(silhouette_score(scaled, labels))
        if score > best_score:
            best_labels, best_score = [int(v) for v in labels], score
    if best_labels is None:
        return ClusterResult(
            empty, [None] * len(rows),
            note="The episodes are too alike to separate into styles.")
    # Number the styles by size, largest first, so the numbering is stable.
    order = sorted(set(best_labels), key=lambda c: -best_labels.count(c))
    renumber = {old: new for new, old in enumerate(order, start=1)}
    labels = [renumber[label] for label in best_labels]
    return ClusterResult(
        Table("Styles", CLUSTER_COLUMNS, _summary_rows(rows, labels)),
        list(labels), best_score,
        note=f"{len(order)} styles from {len(rows)} episodes "
             f"(separation {best_score:.2f}; 1 = clear-cut, 0 = overlapping).")


def _summary_rows(rows: list[dict], labels: list[int]) -> list[dict]:
    summary = []
    for cluster in sorted(set(labels)):
        members = [row for row, label in zip(rows, labels) if label == cluster]
        entry: dict[str, float | int | None] = {
            "cluster": cluster,
            "episodes": len(members),
            "sources": len({(m["session_id"], m["source"]) for m in members}),
            "sessions": len({m["session_id"] for m in members}),
        }
        for name in CLUSTER_FEATURES:
            known = [float(m[name]) for m in members if m.get(name) is not None]
            entry[name] = round(sum(known) / len(known), 2) if known else None
        summary.append(entry)
    return summary
