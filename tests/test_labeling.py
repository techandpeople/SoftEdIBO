"""Tests for in-app touch-gesture labelling (segment + align + CSV I/O)."""

import json
from datetime import datetime, timedelta

from src.ml import labeling


def _write_recording(path, source="AA"):
    base = datetime(2026, 6, 15, 10, 0, 0)
    lines = [json.dumps({"schema": 1, "session_id": "S1",
                         "started": base.isoformat()})]

    def at(ms, msg):
        t = (base + timedelta(milliseconds=ms)).isoformat(timespec="milliseconds")
        return json.dumps({"t": t, "msg": msg})

    def mag(m, a):
        return {"type": "magnet", "source": source, "mag": m, "act": a}

    seq = [(0, mag([0, 0, 0, 0], [])),
           (50, mag([5, 0, 0, 0], [0])), (100, mag([5, 0, 0, 0], [0])),
           (150, mag([0, 0, 0, 0], [])),                       # tap ends
           (500, mag([5, 0, 0, 0], [0])), (600, mag([0, 5, 0, 0], [1])),
           (700, mag([0, 0, 5, 0], [2])), (750, mag([0, 0, 0, 0], []))]  # stroke
    for ms, m in seq:
        lines.append(at(ms, m))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return base


def test_label_rows_one_per_segment(tmp_path):
    rec = tmp_path / "S1.jsonl"
    _write_recording(rec)
    rows = labeling.label_rows_for(rec, skin_type="thymio")
    assert len(rows) == 2
    assert all(r.skin_type == "thymio" for r in rows)
    assert all(r.label == "" for r in rows)          # no live events given
    assert rows[0].duration_ms == 100.0


def test_auto_fill_from_live_events(tmp_path):
    rec = tmp_path / "S1.jsonl"
    base = _write_recording(rec)
    # A live "tap" tag during the first segment (≈75 ms in).
    t_ms = (base + timedelta(milliseconds=75)).timestamp() * 1000.0
    rows = labeling.label_rows_for(rec, "thymio", [(t_ms, "tap")])
    assert rows[0].label == "tap"
    assert rows[1].label == ""                        # second segment untouched


def test_csv_round_trip(tmp_path):
    rec = tmp_path / "S1.jsonl"
    _write_recording(rec)
    rows = labeling.label_rows_for(rec, "thymio")
    rows[0].label = "tap"
    rows[1].label = "stroke"
    csv_path = tmp_path / "S1.labels.csv"
    assert labeling.write_csv(rows, csv_path) == 2

    back = labeling.read_csv(csv_path)
    assert [r.label for r in back] == ["tap", "stroke"]
    assert back[0].skin_type == "thymio"


def test_label_map_keys_match_segments(tmp_path):
    rec = tmp_path / "S1.jsonl"
    _write_recording(rec)
    rows = labeling.label_rows_for(rec, "thymio")
    rows[0].label = "tap"
    m = labeling.label_map(rows)
    # Only labelled rows are in the map; key is (source, round(start_ms)).
    assert len(m) == 1
    (source, start), (stype, label) = next(iter(m.items()))
    assert source == "AA" and label == "tap" and stype == "thymio"


def test_session_id_of():
    from pathlib import Path
    assert labeling.session_id_of(Path("data/recordings/S042.jsonl")) == "S042"
