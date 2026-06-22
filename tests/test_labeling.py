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
    (source, start), (stype, variant, label, group_id) = next(iter(m.items()))
    assert source == "AA" and label == "tap" and stype == "thymio"
    assert variant == "" and group_id == 0


def test_group_id_csv_round_trip(tmp_path):
    rec = tmp_path / "S1.jsonl"
    _write_recording(rec)
    rows = labeling.label_rows_for(rec, "thymio")
    rows[0].label = rows[1].label = "double_tap"
    rows[0].group_id = rows[1].group_id = 7
    csv_path = tmp_path / "S1.labels.csv"
    labeling.write_csv(rows, csv_path)
    back = labeling.read_csv(csv_path)
    assert [r.group_id for r in back] == [7, 7]


def test_grouped_rows_merge_into_one_training_sample(tmp_path):
    from src.ml.training import collect_samples
    rec = tmp_path / "S1.jsonl"
    _write_recording(rec)
    rows = labeling.label_rows_for(rec, "thymio")
    # Group the two segments as one gesture.
    rows[0].label = rows[1].label = "double_tap"
    rows[0].group_id = rows[1].group_id = 1
    samples = list(collect_samples([(str(rec), labeling.label_map(rows))]))
    # Two labelled segments collapse into a single merged sample.
    assert len(samples) == 1
    skin_type, _fv, label, _baseline, _group = samples[0]
    assert label == "double_tap" and skin_type == "thymio"


def test_ungrouped_rows_stay_separate_samples(tmp_path):
    from src.ml.training import collect_samples
    rec = tmp_path / "S1.jsonl"
    _write_recording(rec)
    rows = labeling.label_rows_for(rec, "thymio")
    rows[0].label, rows[1].label = "tap", "stroke"
    samples = list(collect_samples([(str(rec), labeling.label_map(rows))]))
    assert len(samples) == 2


def test_session_id_of():
    from pathlib import Path
    assert labeling.session_id_of(Path("data/recordings/S042.jsonl")) == "S042"


def _write_recording_with_header(path, source="AA", skin_types=None):
    base = datetime(2026, 6, 15, 10, 0, 0)
    header = {"schema": 1, "session_id": "S1", "started": base.isoformat()}
    if skin_types is not None:
        header["skin_types"] = skin_types
    lines = [json.dumps(header)]

    def at(ms, msg):
        t = (base + timedelta(milliseconds=ms)).isoformat(timespec="milliseconds")
        return json.dumps({"t": t, "msg": msg})

    def mag(src, m, a):
        return {"type": "magnet", "source": src, "mag": m, "act": a}

    seq = [(0, mag(source, [0, 0, 0, 0], [])),
           (50, mag(source, [5, 0, 0, 0], [0])),
           (150, mag(source, [0, 0, 0, 0], []))]
    for ms, m in seq:
        lines.append(at(ms, m))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_skin_types_of_reads_header_map(tmp_path):
    rec = tmp_path / "S1.jsonl"
    _write_recording_with_header(rec, skin_types={"AA": "thymio", "BB": "tree_round"})
    assert labeling.skin_types_of(rec) == {"AA": "thymio", "BB": "tree_round"}


def test_skin_types_of_empty_for_untagged(tmp_path):
    rec = tmp_path / "S1.jsonl"
    _write_recording_with_header(rec, skin_types=None)
    assert labeling.skin_types_of(rec) == {}


def test_label_rows_tagged_per_source(tmp_path):
    rec = tmp_path / "S1.jsonl"
    _write_recording_with_header(rec, source="BB", skin_types={"BB": "tree_round"})
    rows = labeling.label_rows_for(rec, source_types=labeling.skin_types_of(rec))
    assert rows and all(r.skin_type == "tree_round" for r in rows)


def test_skin_variant_header_tags_rows_and_survives_csv(tmp_path):
    rec = tmp_path / "S1.jsonl"
    base = datetime(2026, 6, 15, 10, 0, 0)
    header = {"schema": 1, "session_id": "S1", "started": base.isoformat(),
              "skin_types": {"BB": "turtle_square"},
              "skin_variants": {"BB": "wrinkles"}}
    lines = [json.dumps(header)]

    def at(ms, msg):
        t = (base + timedelta(milliseconds=ms)).isoformat(timespec="milliseconds")
        return json.dumps({"t": t, "msg": msg})

    def mag(a):
        return {"type": "magnet", "source": "BB", "mag": [5, 0, 0, 0], "act": a}

    for ms, a in [(0, []), (50, [0]), (150, [])]:
        lines.append(at(ms, mag(a)))
    rec.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert labeling.skin_variants_of(rec) == {"BB": "wrinkles"}
    rows = labeling.label_rows_for(
        rec, source_types=labeling.skin_types_of(rec),
        source_variants=labeling.skin_variants_of(rec))
    assert rows and all(r.skin_variant == "wrinkles" for r in rows)
    rows[0].label = "tap"
    out = tmp_path / "labels.csv"
    labeling.write_csv(rows, out)
    back = labeling.read_csv(out)
    assert back[0].skin_variant == "wrinkles" and back[0].skin_type == "turtle_square"
