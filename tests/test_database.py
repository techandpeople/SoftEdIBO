"""Tests for the database module."""

import json
import os
import tempfile
from datetime import datetime

import pytest

from src.data.database import Database
from src.data.models import InteractionEvent, ParticipantRecord, SessionRecord


@pytest.fixture
def db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    database = Database(db_path)
    database.connect()
    yield database
    database.close()
    os.unlink(db_path)


def test_save_and_get_session(db):
    session = SessionRecord(
        session_id="test-001",
        activity_name="Group Touch",
        start_time=datetime.now(),
    )
    db.save_session(session)
    sessions = db.get_all_sessions()
    assert len(sessions) == 1
    assert sessions[0].session_id == "test-001"


def test_save_participant(db):
    participant = ParticipantRecord(
        participant_id="p-001", alias="Alice", age=8
    )
    db.save_participant(participant)
    saved = db.get_all_participants()
    assert [(p.participant_id, p.alias, p.age) for p in saved] == [("p-001", "Alice", 8)]


def test_log_and_get_events(db):
    session = SessionRecord(
        session_id="test-002",
        activity_name="Group Touch",
        start_time=datetime.now(),
    )
    db.save_session(session)

    participant = ParticipantRecord(participant_id="p-002", alias="Bob")
    db.save_participant(participant)

    event = InteractionEvent(
        session_id="test-002",
        participant_id="p-002",
        type="turtle",
        action="inflate",
        target="chamber_3",
        timestamp=datetime.now(),
    )
    db.log_event(event)
    db.flush_events()  # events are written asynchronously

    events = db.get_session_events("test-002")
    assert len(events) == 1
    assert events[0].action == "inflate"
    assert events[0].type == "turtle"


def test_get_session_start_meta_roundtrips_robots(db):
    """Resume rebuilds a session's robots from its start event, not the global
    last_assignments cache, so the per-session robot list survives intact."""
    db.save_session(SessionRecord(
        session_id="S100", activity_name="Group Touch", start_time=datetime.now()))
    db.log_event(InteractionEvent(
        session_id="S100", participant_id="system", type="session", action="start",
        timestamp=datetime.now(),
        metadata=json.dumps({"robot_ids": ["turtle-1", "tree-1"],
                             "simulation_mode": True}),
    ))
    db.flush_events()

    meta = db.get_session_start_meta("S100")
    assert meta["robot_ids"] == ["turtle-1", "tree-1"]
    assert meta["simulation_mode"] is True


def test_get_session_start_meta_absent_returns_empty(db):
    """A session with no recorded start metadata (e.g. created before this was
    tracked) yields an empty dict, so resume restores no robots rather than
    wrong ones."""
    assert db.get_session_start_meta("missing") == {}


def _seed_session_with_event(db, session_id):
    db.save_session(SessionRecord(
        session_id=session_id, activity_name="Group Touch", start_time=datetime.now()))
    db.log_event(InteractionEvent(
        session_id=session_id, participant_id="p1", type="turtle", action="inflate",
        target="chamber_1", timestamp=datetime.now()))
    db.flush_events()


def test_trash_session_moves_out_of_live_into_trash(db):
    _seed_session_with_event(db, "del-1")
    _seed_session_with_event(db, "keep-1")

    assert db.trash_session("del-1") is True

    assert [s.session_id for s in db.get_all_sessions()] == ["keep-1"]
    assert db.get_session_events("del-1") == []
    assert [t.session_id for t in db.list_trashed_sessions()] == ["del-1"]


def test_trash_session_missing_returns_false(db):
    _seed_session_with_event(db, "keep-1")
    assert db.trash_session("nope") is False
    assert len(db.get_all_sessions()) == 1
    assert db.list_trashed_sessions() == []


def test_restore_session_reinstates_record_and_events(db):
    _seed_session_with_event(db, "del-1")
    db.trash_session("del-1")

    assert db.restore_session("del-1") is True

    assert [s.session_id for s in db.get_all_sessions()] == ["del-1"]
    events = db.get_session_events("del-1")
    assert len(events) == 1
    assert events[0].action == "inflate"
    assert events[0].target == "chamber_1"
    assert db.list_trashed_sessions() == []


def test_restore_session_missing_returns_false(db):
    assert db.restore_session("nope") is False


def test_purge_session_removes_from_trash_permanently(db):
    _seed_session_with_event(db, "del-1")
    db.trash_session("del-1")

    db.purge_session("del-1")

    assert db.list_trashed_sessions() == []
    assert db.restore_session("del-1") is False  # gone for good
    assert db.get_all_sessions() == []


def test_empty_trash_purges_all_and_returns_ids(db):
    _seed_session_with_event(db, "del-1")
    _seed_session_with_event(db, "del-2")
    db.trash_session("del-1")
    db.trash_session("del-2")

    purged = db.empty_trash()

    assert sorted(purged) == ["del-1", "del-2"]
    assert db.list_trashed_sessions() == []


# --- research tables: snapshots, annotations, event corrections -----------

def _session_with_touch(db, session_id="S9"):
    from src.data.models import SessionRecord
    db.save_session(SessionRecord(session_id=session_id, activity_name="A",
                                  start_time=datetime.now()))
    db.log_event(InteractionEvent(session_id=session_id, participant_id="P001",
                                  type="touch", action="press", target="s:0"))
    db.flush_events()
    return db.get_session_events(session_id)[0]


def test_session_snapshot_roundtrip_and_replace(db):
    db.save_session_snapshot("S1", "activity", {"spec": {"initial": "a"}})
    db.save_session_snapshot("S1", "touch", {"threshold": 100})
    db.save_session_snapshot("S1", "touch", {"threshold": 250})
    assert db.get_session_snapshots("S1") == {
        "activity": {"spec": {"initial": "a"}}, "touch": {"threshold": 250}}
    assert db.get_session_snapshots("other") == {}


def test_session_annotation_defaults_then_saved(db):
    from src.data.models import SESSION_STATUS_TEST, SessionAnnotation
    default = db.get_session_annotation("S1")
    assert (default.status, default.group_label, default.visit_order) == (
        "valid", "", None)
    db.save_session_annotation(SessionAnnotation(
        session_id="S1", status=SESSION_STATUS_TEST, group_label="G3",
        visit_order=2, notes="pilot"))
    saved = db.get_session_annotation("S1")
    assert (saved.status, saved.group_label, saved.visit_order, saved.notes) == (
        "test", "G3", 2, "pilot")
    assert set(db.get_session_annotations()) == {"S1"}


def test_event_correction_leaves_the_event_untouched(db):
    from src.data.models import EventCorrection
    event = _session_with_touch(db)
    db.save_event_correction(EventCorrection(
        event_id=event.event_id, session_id="S9", participant_id="P002",
        note="wrong child"))
    assert db.get_session_events("S9")[0].participant_id == "P001"
    correction = db.get_event_corrections("S9")[event.event_id]
    assert (correction.participant_id, correction.excluded) == ("P002", False)
    db.delete_event_correction(event.event_id)
    assert db.get_event_corrections("S9") == {}


def test_trash_and_restore_keep_research_tables(db):
    from src.data.models import EventCorrection, SessionAnnotation
    event = _session_with_touch(db)
    db.save_event_correction(EventCorrection(
        event_id=event.event_id, session_id="S9", excluded=True, note="noise"))
    db.save_session_snapshot("S9", "activity", {"name": "A"})
    db.save_session_annotation(SessionAnnotation(session_id="S9", group_label="G1"))

    assert db.trash_session("S9")
    assert db.get_event_corrections("S9") == {}
    assert db.get_session_snapshots("S9") == {}
    assert db.get_session_annotations() == {}

    assert db.restore_session("S9")
    restored = db.get_session_events("S9")[0]
    correction = db.get_event_corrections("S9")[restored.event_id]
    assert correction.excluded and correction.note == "noise"
    assert db.get_session_snapshots("S9") == {"activity": {"name": "A"}}
    assert db.get_session_annotation("S9").group_label == "G1"
