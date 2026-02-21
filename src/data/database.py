"""SQLAlchemy-backed database for SoftEdIBO.

Supports SQLite (default, local file) and PostgreSQL.
Backend is selected via settings.yaml → database.backend.
"""

import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    select,
    text,
)
from sqlalchemy.engine import Engine

from src.data.models import InteractionEvent, ParticipantRecord, SessionAssignment, SessionRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema (dialect-neutral DDL via SQLAlchemy)
# ---------------------------------------------------------------------------

_metadata = MetaData()

_sessions = Table(
    "sessions", _metadata,
    Column("session_id", String, primary_key=True),
    Column("activity_name", String, nullable=False),
    Column("start_time", String, nullable=False),
    Column("end_time", String),
    Column("notes", String, default=""),
)

_participants = Table(
    "participants", _metadata,
    Column("participant_id", String, primary_key=True),
    Column("alias", String, nullable=False),
    Column("age", Integer),
)

_session_participants = Table(
    "session_participants", _metadata,
    Column("session_id", String, primary_key=True),
    Column("participant_id", String, primary_key=True),
)

_events = Table(
    "events", _metadata,
    Column("event_id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", String, nullable=False),
    Column("participant_id", String, nullable=False),
    Column("type", String, nullable=False),
    Column("action", String, nullable=False),
    Column("target", String, default=""),
    Column("timestamp", String, nullable=False),
    Column("metadata", String, default=""),
)

_session_assignments = Table(
    "session_assignments", _metadata,
    Column("session_id", String, primary_key=True),
    Column("robot_id", String, primary_key=True),
    Column("participant_id", String, primary_key=True),
    Column("unit_ids", String, nullable=False, default="[]"),  # JSON list of skin/branch IDs
)

_counters = Table(
    "counters", _metadata,
    Column("name", String, primary_key=True),
    Column("value", Integer, nullable=False, default=0),
)


class Database:
    """Database connection for storing session data and interaction events.

    Use :meth:`from_settings` to construct from a settings.yaml ``database`` block.
    """

    def __init__(self, url: str):
        self._url = url
        self._engine: Engine | None = None

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_settings(cls, db_cfg: dict, root: Path) -> "Database":
        """Build a Database from a settings.yaml ``database`` block."""
        backend = db_cfg.get("backend", "sqlite")
        if backend == "sqlite":
            rel = db_cfg.get("path", "data/softedibo.db")
            abs_path = root / rel
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite:///{abs_path}"
        elif backend == "postgresql":
            host = db_cfg.get("host", "localhost")
            port = db_cfg.get("port", 5432)
            user = db_cfg.get("user", "")
            password = db_cfg.get("password", "")
            name = db_cfg.get("name", "softedibo")
            url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"
        else:
            raise ValueError(f"Unsupported database backend: {backend!r}")
        return cls(url)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open the engine and create tables if they don't exist."""
        self._engine = create_engine(self._url)
        _metadata.create_all(self._engine)
        self._init_counters()
        logger.info("Database connected: %s", self._url)

    def close(self) -> None:
        """Dispose the engine."""
        if self._engine:
            self._engine.dispose()
            self._engine = None

    def _init_counters(self) -> None:
        """Seed counters from existing data on first run."""
        with self._engine.begin() as conn:
            for counter_name, table_name, col in [
                ("participant", "participants", "participant_id"),
                ("session", "sessions", "session_id"),
            ]:
                row = conn.execute(
                    select(_counters).where(_counters.c.name == counter_name)
                ).fetchone()
                if row is None:
                    result = conn.execute(
                        text(
                            f"SELECT COALESCE(MAX(CAST(SUBSTR({col}, 2) AS INTEGER)), 0)"
                            f" FROM {table_name}"
                        )
                    ).fetchone()
                    n = result[0] if result else 0
                    conn.execute(_counters.insert().values(name=counter_name, value=n))

    def _bump_counter(self, conn, name: str, num: int) -> None:
        """Advance counter to at least num."""
        conn.execute(
            text(
                "UPDATE counters SET value = CASE WHEN value < :n THEN :n ELSE value END"
                " WHERE name = :name"
            ),
            {"n": num, "name": name},
        )

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    def save_session(self, session: SessionRecord) -> None:
        """Insert or update a session record."""
        values = dict(
            session_id=session.session_id,
            activity_name=session.activity_name,
            start_time=session.start_time.isoformat(),
            end_time=session.end_time.isoformat() if session.end_time else None,
            notes=session.notes,
        )
        with self._engine.begin() as conn:
            result = conn.execute(
                _sessions.update()
                .where(_sessions.c.session_id == session.session_id)
                .values(**values)
            )
            if result.rowcount == 0:
                conn.execute(_sessions.insert().values(**values))
            self._bump_counter(conn, "session", int(session.session_id[1:]))

    def get_all_sessions(self) -> list[SessionRecord]:
        """Return all session records ordered by start time."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(_sessions).order_by(_sessions.c.start_time)
            ).fetchall()
        return [
            SessionRecord(
                session_id=row.session_id,
                activity_name=row.activity_name,
                start_time=datetime.fromisoformat(row.start_time),
                end_time=datetime.fromisoformat(row.end_time) if row.end_time else None,
                notes=row.notes,
            )
            for row in rows
        ]

    def next_session_id(self) -> str:
        """Return the next auto-generated session ID (S001, S002, …)."""
        with self._engine.connect() as conn:
            n = conn.execute(
                select(_counters.c.value).where(_counters.c.name == "session")
            ).scalar()
        return f"S{(n or 0) + 1:03d}"

    def get_active_sessions(self) -> list[SessionRecord]:
        """Return sessions that have no end_time (interrupted/crash), ordered by start time."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(_sessions)
                .where(_sessions.c.end_time.is_(None))
                .order_by(_sessions.c.start_time)
            ).fetchall()
        return [
            SessionRecord(
                session_id=row.session_id,
                activity_name=row.activity_name,
                start_time=datetime.fromisoformat(row.start_time),
                end_time=None,
                notes=row.notes,
            )
            for row in rows
        ]

    def get_session_participants(self, session_id: str) -> list[ParticipantRecord]:
        """Return participants linked to a session."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(_participants)
                .join(
                    _session_participants,
                    _participants.c.participant_id == _session_participants.c.participant_id,
                )
                .where(_session_participants.c.session_id == session_id)
                .order_by(_participants.c.participant_id)
            ).fetchall()
        return [
            ParticipantRecord(
                participant_id=row.participant_id,
                alias=row.alias,
                age=row.age,
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Participants
    # ------------------------------------------------------------------

    def save_participant(self, participant: ParticipantRecord) -> None:
        """Insert or update a participant record."""
        values = dict(
            participant_id=participant.participant_id,
            alias=participant.alias,
            age=participant.age,
        )
        with self._engine.begin() as conn:
            result = conn.execute(
                _participants.update()
                .where(_participants.c.participant_id == participant.participant_id)
                .values(**values)
            )
            if result.rowcount == 0:
                conn.execute(_participants.insert().values(**values))
            self._bump_counter(conn, "participant", int(participant.participant_id[1:]))

    def get_all_participants(self) -> list[ParticipantRecord]:
        """Return all participant records ordered by ID."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(_participants).order_by(_participants.c.participant_id)
            ).fetchall()
        return [
            ParticipantRecord(participant_id=row.participant_id, alias=row.alias, age=row.age)
            for row in rows
        ]

    def next_participant_id(self) -> str:
        """Return the next auto-generated participant ID (P001, P002, …)."""
        with self._engine.connect() as conn:
            n = conn.execute(
                select(_counters.c.value).where(_counters.c.name == "participant")
            ).scalar()
        return f"P{(n or 0) + 1:03d}"

    def delete_participant(self, participant_id: str) -> None:
        """Delete a participant record."""
        with self._engine.begin() as conn:
            conn.execute(
                _participants.delete()
                .where(_participants.c.participant_id == participant_id)
            )

    # ------------------------------------------------------------------
    # Session ↔ Participant links
    # ------------------------------------------------------------------

    def link_participant_to_session(self, session_id: str, participant_id: str) -> None:
        """Link a participant to a session (no-op if already linked)."""
        with self._engine.begin() as conn:
            existing = conn.execute(
                select(_session_participants).where(
                    (_session_participants.c.session_id == session_id)
                    & (_session_participants.c.participant_id == participant_id)
                )
            ).fetchone()
            if existing is None:
                conn.execute(
                    _session_participants.insert().values(
                        session_id=session_id, participant_id=participant_id
                    )
                )

    # ------------------------------------------------------------------
    # Session assignments (robot unit → participant mapping)
    # ------------------------------------------------------------------

    def save_assignment(self, assignment: SessionAssignment) -> None:
        """Insert or replace an assignment of robot units to a participant."""
        import json
        values = dict(
            session_id=assignment.session_id,
            robot_id=assignment.robot_id,
            participant_id=assignment.participant_id,
            unit_ids=json.dumps(assignment.unit_ids),
        )
        with self._engine.begin() as conn:
            result = conn.execute(
                _session_assignments.update()
                .where(
                    (_session_assignments.c.session_id == assignment.session_id)
                    & (_session_assignments.c.robot_id == assignment.robot_id)
                    & (_session_assignments.c.participant_id == assignment.participant_id)
                )
                .values(**values)
            )
            if result.rowcount == 0:
                conn.execute(_session_assignments.insert().values(**values))

    def get_session_assignments(self, session_id: str) -> list[SessionAssignment]:
        """Return all robot-unit→participant assignments for a session."""
        import json
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(_session_assignments)
                .where(_session_assignments.c.session_id == session_id)
            ).fetchall()
        return [
            SessionAssignment(
                session_id=row.session_id,
                robot_id=row.robot_id,
                participant_id=row.participant_id,
                unit_ids=json.loads(row.unit_ids),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def log_event(self, event: InteractionEvent) -> None:
        """Append an interaction event."""
        with self._engine.begin() as conn:
            conn.execute(
                _events.insert().values(
                    session_id=event.session_id,
                    participant_id=event.participant_id,
                    type=event.type,
                    action=event.action,
                    target=event.target,
                    timestamp=event.timestamp.isoformat(),
                    metadata=event.metadata,
                )
            )

    def get_session_events(self, session_id: str) -> list[InteractionEvent]:
        """Return all events for a session ordered by timestamp."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(_events)
                .where(_events.c.session_id == session_id)
                .order_by(_events.c.timestamp)
            ).fetchall()
        return [
            InteractionEvent(
                event_id=row.event_id,
                session_id=row.session_id,
                participant_id=row.participant_id,
                type=row.type,
                action=row.action,
                target=row.target,
                timestamp=datetime.fromisoformat(row.timestamp),
                metadata=row.metadata,
            )
            for row in rows
        ]
