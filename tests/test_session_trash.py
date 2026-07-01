"""Tests for the session-trash service (database rows + recording files)."""

import os
import tempfile
from datetime import datetime

import pytest

from src.core.session_trash import SessionTrash
from src.data.database import Database
from src.data.models import SessionRecord


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


def _make_recording(recordings_dir, session_id, text="{}"):
    recordings_dir.mkdir(parents=True, exist_ok=True)
    path = recordings_dir / f"{session_id}.jsonl"
    path.write_text(text)
    return path


def test_trash_moves_recording_into_trash_subfolder(db, tmp_path):
    db.save_session(SessionRecord("s01", "A", datetime.now()))
    rec = _make_recording(tmp_path, "s01")
    trash = SessionTrash(db, tmp_path)

    assert trash.trash("s01") is True
    assert not rec.exists()
    assert (tmp_path / ".trash" / "s01.jsonl").exists()


def test_trash_unknown_session_returns_false(db, tmp_path):
    trash = SessionTrash(db, tmp_path)
    assert trash.trash("nope") is False


def test_restore_brings_recording_back(db, tmp_path):
    db.save_session(SessionRecord("s01", "A", datetime.now()))
    rec = _make_recording(tmp_path, "s01", '{"hi": 1}')
    trash = SessionTrash(db, tmp_path)
    trash.trash("s01")

    assert trash.restore("s01") is True
    assert rec.exists()
    assert rec.read_text() == '{"hi": 1}'
    assert not (tmp_path / ".trash" / "s01.jsonl").exists()
    assert [s.session_id for s in db.get_all_sessions()] == ["s01"]


def test_purge_deletes_recording_and_trash_row(db, tmp_path):
    db.save_session(SessionRecord("s01", "A", datetime.now()))
    _make_recording(tmp_path, "s01")
    trash = SessionTrash(db, tmp_path)
    trash.trash("s01")

    trash.purge("s01")

    assert not (tmp_path / ".trash" / "s01.jsonl").exists()
    assert db.list_trashed_sessions() == []


def test_empty_deletes_all_recordings_and_returns_ids(db, tmp_path):
    trash = SessionTrash(db, tmp_path)
    for sid in ("s01", "s02"):
        db.save_session(SessionRecord(sid, "A", datetime.now()))
        _make_recording(tmp_path, sid)
        trash.trash(sid)

    ids = trash.empty()

    assert sorted(ids) == ["s01", "s02"]
    assert list((tmp_path / ".trash").glob("*.jsonl")) == []
    assert db.list_trashed_sessions() == []


def test_trash_without_recording_file_is_fine(db, tmp_path):
    """Sessions with no recording (e.g. never recorded) still trash cleanly."""
    db.save_session(SessionRecord("s01", "A", datetime.now()))
    trash = SessionTrash(db, tmp_path)

    assert trash.trash("s01") is True
    assert [t.session_id for t in trash.list()] == ["s01"]
