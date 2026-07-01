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
    # No error means success


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
