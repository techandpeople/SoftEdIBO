"""Touch-gesture labelling — segment recordings, align live tags, CSV I/O.

Shared by the in-app Touch Gestures dialog and the CLI labeller so the labelling
logic lives in one place. A *label row* ties one touch segment (identified by
its source MAC + start time within a recording) to a skin type and a gesture
class. Dependency-free (stdlib only).
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.ml.training import segments_of   # re-use the recording segmenter

CSV_FIELDS = ("skin_type", "skin_variant", "source", "start_ms", "end_ms",
              "label", "group_id")


@dataclass
class LabelRow:
    """One labelled (or to-be-labelled) touch segment.

    ``group_id`` ties several rows into one logical gesture (e.g. a triple tap):
    rows with the same non-zero ``group_id`` are merged into a single training
    sample sharing one ``label``. ``0`` means ungrouped (a standalone touch).
    ``skin_variant`` is the silicone format (a touch ML feature)."""
    skin_type: str
    source: str
    start_ms: float
    end_ms: float
    label: str = ""
    group_id: int = 0
    skin_variant: str = ""

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms


def _header_map(recording: Path, key: str) -> dict[str, str]:
    """Read a ``{source: value}`` map from the recording's JSON header line.

    Written by :class:`~src.data.stream_recorder.StreamRecorder`. Returns ``{}``
    for recordings made before the field was tagged (or unreadable headers)."""
    try:
        with open(recording, encoding="utf-8") as f:
            header = json.loads(f.readline() or "{}")
    except (OSError, ValueError):
        return {}
    m = header.get(key)
    return {str(k): str(v) for k, v in m.items()} if isinstance(m, dict) else {}


def skin_types_of(recording: Path) -> dict[str, str]:
    """Per-source skin-type map ``{source: skin_type}`` from the header line."""
    return _header_map(recording, "skin_types")


def skin_variants_of(recording: Path) -> dict[str, str]:
    """Per-source silicone-variant map ``{source: skin_variant}`` from the header."""
    return _header_map(recording, "skin_variants")


def label_rows_for(recording: Path, skin_type: str = "",
                   gesture_events: list[tuple[float, str]] | None = None,
                   window_ms: float = 1500.0,
                   source_types: dict[str, str] | None = None,
                   source_variants: dict[str, str] | None = None
                   ) -> list[LabelRow]:
    """Build one :class:`LabelRow` per touch segment in ``recording``.

    ``gesture_events`` is ``[(epoch_ms, label), …]`` from the live observer
    tags; each segment is pre-filled with the nearest one within ``window_ms``
    (blank if none). Each row's skin type/variant comes from ``source_types`` /
    ``source_variants`` keyed by the touch ``source`` MAC (so one recording can
    mix skins); ``skin_type`` is the fallback when a source isn't in the map."""
    source_types = source_types or {}
    source_variants = source_variants or {}
    rows: list[LabelRow] = [
        LabelRow(skin_type=source_types.get(source, skin_type), source=source,
                 start_ms=seg.start_ms, end_ms=seg.end_ms,
                 skin_variant=source_variants.get(source, ""))
        for source, seg in segments_of(recording)
    ]
    rows.sort(key=lambda r: r.start_ms)
    # Assign each live tag to the ONE segment it best belongs to (the segment
    # containing it, else the nearest within window) — so a tag never labels
    # more than one touch.
    for t_ms, label in (gesture_events or []):
        best_i, best_d = None, window_ms
        for i, r in enumerate(rows):
            d = (0.0 if r.start_ms <= t_ms <= r.end_ms
                 else min(abs(t_ms - r.start_ms), abs(t_ms - r.end_ms)))
            if d <= best_d:
                best_i, best_d = i, d
        if best_i is not None:
            rows[best_i].label = label
    return rows


def gesture_events_from_db(session_id: str) -> list[tuple[float, str]]:
    """Live ``gesture_label`` events (epoch_ms, label) for a session, from the DB."""
    from src.config.settings import Settings
    from src.data.database import Database
    db = Database.from_settings(Settings().db_cfg, Settings.ROOT)
    db.connect()
    try:
        return [(ev.timestamp.timestamp() * 1000.0, ev.action)
                for ev in db.get_session_events(session_id)
                if ev.type == "gesture_label"]
    finally:
        db.close()


def session_id_of(recording: Path) -> str:
    """Recordings are named ``<session_id>.jsonl`` (see StreamRecorder)."""
    return recording.stem


def write_csv(rows: list[LabelRow], path: Path) -> int:
    """Write label rows to CSV (only rows with a non-empty label). Returns count."""
    labelled = [r for r in rows if r.label]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(CSV_FIELDS))
        w.writeheader()
        for r in labelled:
            w.writerow({"skin_type": r.skin_type, "skin_variant": r.skin_variant,
                        "source": r.source,
                        "start_ms": f"{r.start_ms:.1f}", "end_ms": f"{r.end_ms:.1f}",
                        "label": r.label, "group_id": r.group_id})
    return len(labelled)


def read_csv(path: Path) -> list[LabelRow]:
    """Read label rows from a CSV (as written by :func:`write_csv` or by hand)."""
    out: list[LabelRow] = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            out.append(LabelRow(
                skin_type=row.get("skin_type", ""),
                skin_variant=row.get("skin_variant", ""),
                source=row.get("source", "?"),
                start_ms=float(row["start_ms"]),
                end_ms=float(row.get("end_ms", row["start_ms"])),
                label=row.get("label", ""),
                group_id=int(row.get("group_id") or 0)))
    return out


def label_map(rows: list[LabelRow]) -> dict:
    """``{(source, round(start_ms)): (skin_type, skin_variant, label, group_id)}``
    for the trainer."""
    return {(r.source, round(r.start_ms)):
            (r.skin_type, r.skin_variant, r.label, r.group_id)
            for r in rows if r.label}


def _epoch_ms(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp() * 1000.0
