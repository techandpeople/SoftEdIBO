"""Tests for the database module."""

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
        robot_type="turtle",
        action="inflate",
        target="chamber_3",
        timestamp=datetime.now(),
    )
    db.log_event(event)

    events = db.get_session_events("test-002")
    assert len(events) == 1
    assert events[0].action == "inflate"
    assert events[0].robot_type == "turtle"
