"""Tests for SessionExporter — CSV flattening + participant attribution."""

import csv
from datetime import datetime

import pytest

from src.data.database import Database
from src.data.export import SessionExporter
from src.data.models import (
    InteractionEvent,
    ParticipantRecord,
    SessionAssignment,
    SessionRecord,
)


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    database.connect()
    yield database
    database.close()


def _rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _seed_session(db, session_id="S001"):
    db.save_session(SessionRecord(
        session_id=session_id, activity_name="Organ Swap",
        start_time=datetime(2026, 6, 11, 10, 0, 0)))
    for pid, alias in (("P1", "Ana"), ("P2", "Bea")):
        db.save_participant(ParticipantRecord(participant_id=pid, alias=alias))
        db.link_participant_to_session(session_id, pid)


def test_export_attributes_branch_events_to_participants(db, tmp_path):
    _seed_session(db)
    # Tree: each branch assigned to a different child.
    db.save_assignment(SessionAssignment("S001", "tree", "P1", ["branch-a"]))
    db.save_assignment(SessionAssignment("S001", "tree", "P2", ["branch-b"]))
    # Robot-level organ events use the patient id "<robot>/<skin>" as target.
    db.log_event(InteractionEvent(
        session_id="S001", participant_id="system", type="activity",
        action="state", target="tree/branch-a",
        timestamp=datetime(2026, 6, 11, 10, 1, 0)))
    db.log_event(InteractionEvent(
        session_id="S001", participant_id="system", type="cover",
        action="close", target="tree/branch-b",
        timestamp=datetime(2026, 6, 11, 10, 2, 0)))
    db.flush_events()

    out = str(tmp_path / "s.csv")
    n = SessionExporter(db).export_session("S001", out)
    assert n == 2
    rows = _rows(out)
    by_target = {r["target"]: r for r in rows}
    assert by_target["tree/branch-a"]["robot_id"] == "tree"
    assert by_target["tree/branch-a"]["participant"] == "P1"
    assert by_target["tree/branch-b"]["participant"] == "P2"


def test_export_attributes_whole_robot_event_to_single_participant(db, tmp_path):
    _seed_session(db)
    # Thymio: one robot, one child → bare robot-id target resolves to them.
    db.save_assignment(SessionAssignment("S001", "thymio-1", "P1", ["belly"]))
    db.log_event(InteractionEvent(
        session_id="S001", participant_id="system", type="organ",
        action="reading", target="thymio-1", metadata="952.4",
        timestamp=datetime(2026, 6, 11, 10, 1, 0)))
    db.flush_events()

    out = str(tmp_path / "s.csv")
    SessionExporter(db).export_session("S001", out)
    row = _rows(out)[0]
    assert row["robot_id"] == "thymio-1"
    assert row["participant"] == "P1"
    assert row["metadata"] == "952.4"


def test_export_touch_event_keeps_explicit_participant(db, tmp_path):
    _seed_session(db)
    db.save_assignment(SessionAssignment("S001", "turtle", "P2", ["belly"]))
    # Touch events carry the participant explicitly + "<skin>:<chamber>" target.
    db.log_event(InteractionEvent(
        session_id="S001", participant_id="P2", type="touch",
        action="press", target="belly:0",
        timestamp=datetime(2026, 6, 11, 10, 1, 0)))
    db.flush_events()

    out = str(tmp_path / "s.csv")
    SessionExporter(db).export_session("S001", out)
    row = _rows(out)[0]
    assert row["robot_id"] == "turtle"
    assert row["participant"] == "P2"


def test_export_all_includes_header_and_rows(db, tmp_path):
    _seed_session(db)
    db.log_event(InteractionEvent(
        session_id="S001", participant_id="system", type="marker",
        action="mark", target="", metadata="clap",
        timestamp=datetime(2026, 6, 11, 10, 0, 30)))
    db.flush_events()

    out = str(tmp_path / "all.csv")
    n = SessionExporter(db).export_all(out)
    assert n == 1
    rows = _rows(out)
    assert rows[0]["type"] == "marker"
    assert rows[0]["metadata"] == "clap"
