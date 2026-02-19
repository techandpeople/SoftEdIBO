"""SQLite database management for SoftEdIBO."""

import logging
import sqlite3
from pathlib import Path
from datetime import datetime

from src.data.models import InteractionEvent, ParticipantRecord, SessionRecord

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    activity_name TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT,
    notes TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS participants (
    participant_id TEXT PRIMARY KEY,
    alias TEXT NOT NULL,
    age INTEGER
);

CREATE TABLE IF NOT EXISTS session_participants (
    session_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    PRIMARY KEY (session_id, participant_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id)
);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    type TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT DEFAULT '',
    timestamp TEXT NOT NULL,
    metadata TEXT DEFAULT '',
    FOREIGN KEY (session_id) REFERENCES sessions(session_id),
    FOREIGN KEY (participant_id) REFERENCES participants(participant_id)
);
"""


class Database:
    """SQLite database for storing session data and interaction events."""

    def __init__(self, db_path: str = "data/softedibo.db"):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> None:
        """Open the database connection and create tables."""
        self._conn = sqlite3.connect(str(self._db_path))
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        logger.info("Database connected: %s", self._db_path)

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None

    def save_session(self, session: SessionRecord) -> None:
        """Insert or update a session record."""
        self._conn.execute(
            "INSERT OR REPLACE INTO sessions (session_id, activity_name, start_time, end_time, notes) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                session.session_id,
                session.activity_name,
                session.start_time.isoformat(),
                session.end_time.isoformat() if session.end_time else None,
                session.notes,
            ),
        )
        self._conn.commit()

    def save_participant(self, participant: ParticipantRecord) -> None:
        """Insert or update a participant record."""
        self._conn.execute(
            "INSERT OR REPLACE INTO participants (participant_id, alias, age) VALUES (?, ?, ?)",
            (participant.participant_id, participant.alias, participant.age),
        )
        self._conn.commit()

    def link_participant_to_session(self, session_id: str, participant_id: str) -> None:
        """Link a participant to a session."""
        self._conn.execute(
            "INSERT OR IGNORE INTO session_participants (session_id, participant_id) VALUES (?, ?)",
            (session_id, participant_id),
        )
        self._conn.commit()

    def log_event(self, event: InteractionEvent) -> None:
        """Log an interaction event."""
        self._conn.execute(
            "INSERT INTO events (session_id, participant_id, type, action, target, timestamp, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.session_id,
                event.participant_id,
                event.type,
                event.action,
                event.target,
                event.timestamp.isoformat(),
                event.metadata,
            ),
        )
        self._conn.commit()

    def get_session_events(self, session_id: str) -> list[InteractionEvent]:
        """Get all events for a session."""
        cursor = self._conn.execute(
            "SELECT event_id, session_id, participant_id, type, action, target, timestamp, metadata "
            "FROM events WHERE session_id = ? ORDER BY timestamp",
            (session_id,),
        )
        return [
            InteractionEvent(
                event_id=row[0],
                session_id=row[1],
                participant_id=row[2],
                type=row[3],
                action=row[4],
                target=row[5],
                timestamp=datetime.fromisoformat(row[6]),
                metadata=row[7],
            )
            for row in cursor.fetchall()
        ]

    def get_all_sessions(self) -> list[SessionRecord]:
        """Get all session records."""
        cursor = self._conn.execute(
            "SELECT session_id, activity_name, start_time, end_time, notes FROM sessions ORDER BY start_time ASC"
        )
        return [
            SessionRecord(
                session_id=row[0],
                activity_name=row[1],
                start_time=datetime.fromisoformat(row[2]),
                end_time=datetime.fromisoformat(row[3]) if row[3] else None,
                notes=row[4],
            )
            for row in cursor.fetchall()
        ]
