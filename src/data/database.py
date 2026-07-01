"""SQLAlchemy-backed database for SoftEdIBO.

Supports SQLite (default, local file) and PostgreSQL.
Backend is selected via settings.yaml => database.backend.
"""

import logging
import queue
import re
import threading
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    select,
    text,
)
from sqlalchemy.engine import Engine

from src.data.models import (
    ActivityPreset,
    DeclarativeActivity,
    InteractionEvent,
    ParticipantRecord,
    SessionAssignment,
    SessionRecord,
    SkinTemplate,
    TrashedSession,
)

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

# Trash — sessions moved here (soft-delete) before a permanent purge. The whole
# session (record + events + assignments + participant links) is preserved as a
# JSON ``bundle`` so a restore reinstates it exactly; ``activity_name`` /
# ``start_time`` / ``end_time`` are mirrored as columns for cheap listing.
_trashed_sessions = Table(
    "trashed_sessions", _metadata,
    Column("session_id",    String, primary_key=True),
    Column("activity_name", String, nullable=False),
    Column("start_time",    String, nullable=False),
    Column("end_time",      String),
    Column("trashed_at",    String, nullable=False),
    Column("bundle",        String, nullable=False),  # JSON snapshot for restore
)

_counters = Table(
    "counters", _metadata,
    Column("name", String, primary_key=True),
    Column("value", Integer, nullable=False, default=0),
)

# Activity presets — named bundles of tunable parameters for an Activity.
# ``params`` is JSON-encoded. Multiple presets per activity are supported.
_activity_presets = Table(
    "activity_presets", _metadata,
    Column("preset_id",     String, primary_key=True),
    Column("activity_name", String, nullable=False),
    Column("name",          String, nullable=False),
    Column("description",   String, default=""),
    Column("params",        String, nullable=False, default="{}"),
    Column("created_at",    String, nullable=False),
    Column("updated_at",    String, nullable=False),
)

# Declarative activities — behaviour specs authored as data (block editor /
# by hand) and run by ScriptedActivity. ``spec`` is JSON-encoded.
_declarative_activities = Table(
    "declarative_activities", _metadata,
    Column("activity_id",  String, primary_key=True),
    Column("name",         String, nullable=False),
    Column("description",  String, default=""),
    Column("spec",         String, nullable=False, default="{}"),
    Column("created_at",   String, nullable=False),
    Column("updated_at",   String, nullable=False),
)

# Skin templates — reusable layouts shared across skins. ``grid``,
# ``chamber_grid`` and ``sensor_grid`` are JSON-encoded.
_skin_templates = Table(
    "skin_templates", _metadata,
    Column("template_id",          String, primary_key=True),
    Column("name",                 String, nullable=False),
    Column("description",          String, default=""),
    Column("chamber_count",        Integer, nullable=False, default=1),
    Column("default_max_pressure", Float, nullable=False, default=8.0),
    Column("default_min_pressure", Float, nullable=False, default=0.0),
    Column("grid",                 String, nullable=False, default="{}"),
    Column("chamber_grid",         String, nullable=False, default="[]"),
    Column("sensor_count",         Integer, nullable=False, default=0),
    Column("sensor_grid",          String, nullable=False, default="[]"),
)


class Database:
    """Database connection for storing session data and interaction events.

    Use :meth:`from_settings` to construct from a settings.yaml ``database`` block.
    """

    def __init__(self, url: str):
        self._url = self._normalize_url(url)
        self._engine: Engine | None = None
        self._event_queue: queue.Queue[InteractionEvent | None] = queue.Queue()
        self._event_thread: threading.Thread | None = None

    @property
    def _db_engine(self) -> Engine:
        if self._engine is None:
            raise RuntimeError("Database not connected — call connect() first")
        return self._engine

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Accept SQLAlchemy URLs or plain filesystem paths for SQLite.

        Tests and local utilities often pass ``/tmp/test.db`` directly.
        Convert those paths to ``sqlite:///...`` so SQLAlchemy can parse them.
        """
        if "://" in url:
            return url
        return f"sqlite:///{Path(url)}"

    @staticmethod
    def _extract_counter_num(identifier: str) -> int | None:
        """Extract trailing numeric part from IDs like S001, P002, test-003."""
        m = re.search(r"(\d+)$", identifier)
        return int(m.group(1)) if m else None

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
        self._event_thread = threading.Thread(
            target=self._event_worker, daemon=True, name="db-event-writer"
        )
        self._event_thread.start()
        logger.info("Database connected: %s", self._url)

    def close(self) -> None:
        """Flush pending events, then dispose the engine."""
        self._event_queue.put(None)  # sentinel — tells the worker to stop
        if self._event_thread is not None:
            self._event_thread.join(timeout=5)
            self._event_thread = None
        if self._engine:
            self._engine.dispose()
            self._engine = None

    def _init_counters(self) -> None:
        """Seed counters from existing data on first run."""
        with self._db_engine.begin() as conn:
            for counter_name, table_name, col in [
                ("participant",     "participants",      "participant_id"),
                ("session",         "sessions",          "session_id"),
                ("skin_template",   "skin_templates",    "template_id"),
                ("activity_preset", "activity_presets",  "preset_id"),
                ("declarative_activity", "declarative_activities", "activity_id"),
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
        values = {
            "session_id":    session.session_id,
            "activity_name": session.activity_name,
            "start_time":    session.start_time.isoformat(),
            "end_time":      session.end_time.isoformat() if session.end_time else None,
            "notes":         session.notes,
        }
        with self._db_engine.begin() as conn:
            result = conn.execute(
                _sessions.update()
                .where(_sessions.c.session_id == session.session_id)
                .values(**values)
            )
            if result.rowcount == 0:
                conn.execute(_sessions.insert().values(**values))
            n = self._extract_counter_num(session.session_id)
            if n is not None:
                self._bump_counter(conn, "session", n)

    @staticmethod
    def _delete_live_session(conn, session_id: str) -> None:
        """Remove a session and its dependent rows from the live tables."""
        conn.execute(_events.delete().where(_events.c.session_id == session_id))
        conn.execute(
            _session_participants.delete()
            .where(_session_participants.c.session_id == session_id)
        )
        conn.execute(
            _session_assignments.delete()
            .where(_session_assignments.c.session_id == session_id)
        )
        conn.execute(_sessions.delete().where(_sessions.c.session_id == session_id))

    def trash_session(self, session_id: str) -> bool:
        """Move a session to the trash (soft-delete). Returns False if unknown.

        Snapshots the session (record + events + assignments + participant
        links) into the trash table, then removes it from the live tables, all
        in one transaction. Restore with :meth:`restore_session`; delete for
        good with :meth:`purge_session`. The session's sensor recording (a file
        on disk) is handled by the caller.
        """
        import json
        # Make sure no queued events are still in flight for this session.
        self.flush_events()
        with self._db_engine.begin() as conn:
            srow = conn.execute(
                select(_sessions).where(_sessions.c.session_id == session_id)
            ).first()
            if srow is None:
                return False

            events = conn.execute(
                select(_events).where(_events.c.session_id == session_id)
                .order_by(_events.c.event_id)
            ).fetchall()
            assignments = conn.execute(
                select(_session_assignments)
                .where(_session_assignments.c.session_id == session_id)
            ).fetchall()
            links = conn.execute(
                select(_session_participants.c.participant_id)
                .where(_session_participants.c.session_id == session_id)
            ).fetchall()

            bundle = {
                "session": {
                    "session_id": srow.session_id,
                    "activity_name": srow.activity_name,
                    "start_time": srow.start_time,
                    "end_time": srow.end_time,
                    "notes": srow.notes,
                },
                "events": [
                    {
                        "participant_id": e.participant_id,
                        "type": e.type,
                        "action": e.action,
                        "target": e.target,
                        "timestamp": e.timestamp,
                        "metadata": e.metadata,
                    }
                    for e in events
                ],
                # unit_ids kept as the raw JSON string exactly as stored.
                "assignments": [
                    {
                        "robot_id": a.robot_id,
                        "participant_id": a.participant_id,
                        "unit_ids": a.unit_ids,
                    }
                    for a in assignments
                ],
                "participant_ids": [row.participant_id for row in links],
            }

            conn.execute(
                _trashed_sessions.delete()
                .where(_trashed_sessions.c.session_id == session_id)
            )
            conn.execute(_trashed_sessions.insert().values(
                session_id=srow.session_id,
                activity_name=srow.activity_name,
                start_time=srow.start_time,
                end_time=srow.end_time,
                trashed_at=datetime.now().isoformat(),
                bundle=json.dumps(bundle),
            ))
            self._delete_live_session(conn, session_id)
        return True

    def list_trashed_sessions(self) -> list[TrashedSession]:
        """Return trashed sessions, most-recently-trashed first."""
        with self._db_engine.connect() as conn:
            rows = conn.execute(
                select(_trashed_sessions).order_by(_trashed_sessions.c.trashed_at.desc())
            ).fetchall()
        return [
            TrashedSession(
                session_id=row.session_id,
                activity_name=row.activity_name,
                start_time=datetime.fromisoformat(row.start_time),
                end_time=datetime.fromisoformat(row.end_time) if row.end_time else None,
                trashed_at=datetime.fromisoformat(row.trashed_at),
            )
            for row in rows
        ]

    def restore_session(self, session_id: str) -> bool:
        """Restore a trashed session to the live tables. False if not in trash."""
        import json
        with self._db_engine.begin() as conn:
            row = conn.execute(
                select(_trashed_sessions.c.bundle)
                .where(_trashed_sessions.c.session_id == session_id)
            ).first()
            if row is None:
                return False
            bundle = json.loads(row.bundle)

            s = bundle["session"]
            conn.execute(_sessions.insert().values(
                session_id=s["session_id"],
                activity_name=s["activity_name"],
                start_time=s["start_time"],
                end_time=s["end_time"],
                notes=s.get("notes", ""),
            ))
            for pid in bundle.get("participant_ids", []):
                conn.execute(_session_participants.insert().values(
                    session_id=session_id, participant_id=pid))
            for a in bundle.get("assignments", []):
                conn.execute(_session_assignments.insert().values(
                    session_id=session_id,
                    robot_id=a["robot_id"],
                    participant_id=a["participant_id"],
                    unit_ids=a["unit_ids"],
                ))
            for e in bundle.get("events", []):
                conn.execute(_events.insert().values(
                    session_id=session_id,
                    participant_id=e["participant_id"],
                    type=e["type"],
                    action=e["action"],
                    target=e["target"],
                    timestamp=e["timestamp"],
                    metadata=e["metadata"],
                ))
            conn.execute(
                _trashed_sessions.delete()
                .where(_trashed_sessions.c.session_id == session_id)
            )
        return True

    def purge_session(self, session_id: str) -> None:
        """Permanently delete a single trashed session (no restore possible)."""
        with self._db_engine.begin() as conn:
            conn.execute(
                _trashed_sessions.delete()
                .where(_trashed_sessions.c.session_id == session_id)
            )

    def empty_trash(self) -> list[str]:
        """Permanently delete every trashed session. Returns the purged IDs."""
        with self._db_engine.begin() as conn:
            ids = [
                row.session_id for row in conn.execute(
                    select(_trashed_sessions.c.session_id)
                ).fetchall()
            ]
            conn.execute(_trashed_sessions.delete())
        return ids

    def get_all_sessions(self) -> list[SessionRecord]:
        """Return all session records ordered by start time."""
        with self._db_engine.connect() as conn:
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
        with self._db_engine.connect() as conn:
            n = conn.execute(
                select(_counters.c.value).where(_counters.c.name == "session")
            ).scalar()
        return f"S{(n or 0) + 1:03d}"

    def get_active_sessions(self) -> list[SessionRecord]:
        """Return sessions that have no end_time (interrupted/crash), ordered by start time."""
        with self._db_engine.connect() as conn:
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
        with self._db_engine.connect() as conn:
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
        values = {
            "participant_id": participant.participant_id,
            "alias":          participant.alias,
            "age":            participant.age,
        }
        with self._db_engine.begin() as conn:
            result = conn.execute(
                _participants.update()
                .where(_participants.c.participant_id == participant.participant_id)
                .values(**values)
            )
            if result.rowcount == 0:
                conn.execute(_participants.insert().values(**values))
            n = self._extract_counter_num(participant.participant_id)
            if n is not None:
                self._bump_counter(conn, "participant", n)

    def get_all_participants(self) -> list[ParticipantRecord]:
        """Return all participant records ordered by ID."""
        with self._db_engine.connect() as conn:
            rows = conn.execute(
                select(_participants).order_by(_participants.c.participant_id)
            ).fetchall()
        return [
            ParticipantRecord(participant_id=row.participant_id, alias=row.alias, age=row.age)
            for row in rows
        ]

    def next_participant_id(self) -> str:
        """Return the next auto-generated participant ID (P001, P002, …)."""
        with self._db_engine.connect() as conn:
            n = conn.execute(
                select(_counters.c.value).where(_counters.c.name == "participant")
            ).scalar()
        return f"P{(n or 0) + 1:03d}"

    def delete_participant(self, participant_id: str) -> None:
        """Delete a participant record."""
        with self._db_engine.begin() as conn:
            conn.execute(
                _participants.delete()
                .where(_participants.c.participant_id == participant_id)
            )

    # ------------------------------------------------------------------
    # Session ↔ Participant links
    # ------------------------------------------------------------------

    def link_participant_to_session(self, session_id: str, participant_id: str) -> None:
        """Link a participant to a session (no-op if already linked)."""
        with self._db_engine.begin() as conn:
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
    # Session assignments (robot unit => participant mapping)
    # ------------------------------------------------------------------

    def save_assignment(self, assignment: SessionAssignment) -> None:
        """Insert or merge an assignment of robot units to a participant.

        If a row already exists for the same (session, robot, participant),
        the new unit_ids are merged into the existing list (no duplicates).
        """
        import json
        key_filter = (
            (_session_assignments.c.session_id == assignment.session_id)
            & (_session_assignments.c.robot_id == assignment.robot_id)
            & (_session_assignments.c.participant_id == assignment.participant_id)
        )
        with self._db_engine.begin() as conn:
            existing = conn.execute(
                select(_session_assignments.c.unit_ids).where(key_filter)
            ).scalar()
            if existing is not None:
                merged = list(dict.fromkeys(json.loads(existing) + assignment.unit_ids))
                conn.execute(
                    _session_assignments.update().where(key_filter)
                    .values(unit_ids=json.dumps(merged))
                )
            else:
                conn.execute(_session_assignments.insert().values(
                    session_id=assignment.session_id,
                    robot_id=assignment.robot_id,
                    participant_id=assignment.participant_id,
                    unit_ids=json.dumps(assignment.unit_ids),
                ))

    def get_session_start_meta(self, session_id: str) -> dict:
        """Return the parsed metadata of a session's ``start`` event.

        The start event records the exact robots and simulation flag the
        session ran with, as a JSON object (``{"robot_ids": [...],
        "simulation_mode": bool}``). This is the durable, per-session source
        of truth used to rebuild a session on resume — unlike the global
        ``last_assignments.json`` cache, which only ever holds the most recent
        session and so restores wrong/stale robots for any other session.

        Returns an empty dict when there is no start event or its metadata is
        missing/unparseable (e.g. sessions created before this was recorded).
        """
        import json
        with self._db_engine.connect() as conn:
            row = conn.execute(
                select(_events.c.metadata)
                .where(
                    (_events.c.session_id == session_id)
                    & (_events.c.type == "session")
                    & (_events.c.action == "start")
                )
                .order_by(_events.c.event_id)
            ).first()
        if row is None or not row.metadata:
            return {}
        try:
            data = json.loads(row.metadata)
        except (ValueError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}

    def get_session_assignments(self, session_id: str) -> list[SessionAssignment]:
        """Return all robot-unit=>participant assignments for a session."""
        import json
        with self._db_engine.connect() as conn:
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
        """Enqueue an interaction event for async writing (non-blocking)."""
        self._event_queue.put(event)

    def flush_events(self) -> None:
        """Block until all queued interaction events have been written.

        Events are logged asynchronously (see :meth:`log_event`); call this when
        you need to read them back immediately afterwards.
        """
        self._event_queue.join()

    def _event_worker(self) -> None:
        """Background thread: drains the event queue and writes to the DB."""
        while True:
            event = self._event_queue.get()
            try:
                if event is None:  # sentinel
                    break
                with self._db_engine.begin() as conn:
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
            except Exception:
                logger.exception("Failed to write event to database: %s", event)
            finally:
                self._event_queue.task_done()

    def get_session_events(self, session_id: str) -> list[InteractionEvent]:
        """Return all events for a session ordered by timestamp."""
        with self._db_engine.connect() as conn:
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

    # ------------------------------------------------------------------
    # Skin templates
    # ------------------------------------------------------------------

    def save_skin_template(self, template: SkinTemplate) -> None:
        """Insert or update a skin template. ``template.template_id`` must be set."""
        import json
        values = {
            "template_id":          template.template_id,
            "name":                 template.name,
            "description":          template.description,
            "chamber_count":        int(template.chamber_count),
            "default_max_pressure": float(template.default_max_pressure),
            "default_min_pressure": float(template.default_min_pressure),
            "grid":                 json.dumps(template.grid),
            "chamber_grid":         json.dumps(template.chamber_grid),
            "sensor_count":         int(template.sensor_count),
            "sensor_grid":          json.dumps(template.sensor_grid),
        }
        with self._db_engine.begin() as conn:
            result = conn.execute(
                _skin_templates.update()
                .where(_skin_templates.c.template_id == template.template_id)
                .values(**values)
            )
            if result.rowcount == 0:
                conn.execute(_skin_templates.insert().values(**values))
            n = self._extract_counter_num(template.template_id)
            if n is not None:
                self._bump_counter(conn, "skin_template", n)

    def get_all_skin_templates(self) -> list[SkinTemplate]:
        """Return all skin templates ordered by template ID."""
        with self._db_engine.connect() as conn:
            rows = conn.execute(
                select(_skin_templates).order_by(_skin_templates.c.template_id)
            ).fetchall()
        return [self._row_to_template(row) for row in rows]

    def get_skin_template(self, template_id: str) -> SkinTemplate | None:
        """Fetch one template by ID, or ``None`` if not found."""
        with self._db_engine.connect() as conn:
            row = conn.execute(
                select(_skin_templates)
                .where(_skin_templates.c.template_id == template_id)
            ).fetchone()
        return self._row_to_template(row) if row is not None else None

    def delete_skin_template(self, template_id: str) -> None:
        with self._db_engine.begin() as conn:
            conn.execute(
                _skin_templates.delete()
                .where(_skin_templates.c.template_id == template_id)
            )

    def next_skin_template_id(self) -> str:
        """Return the next auto-generated template ID (T001, T002, …)."""
        with self._db_engine.connect() as conn:
            n = conn.execute(
                select(_counters.c.value)
                .where(_counters.c.name == "skin_template")
            ).scalar()
        return f"T{(n or 0) + 1:03d}"

    # ------------------------------------------------------------------
    # Activity presets
    # ------------------------------------------------------------------

    def save_activity_preset(self, preset: ActivityPreset) -> None:
        """Insert or update an activity preset. ``preset.preset_id`` must be set."""
        import json
        now_iso = datetime.now().isoformat()
        values = {
            "preset_id":     preset.preset_id,
            "activity_name": preset.activity_name,
            "name":          preset.name,
            "description":   preset.description,
            "params":        json.dumps(preset.params),
            "created_at":    (preset.created_at or datetime.now()).isoformat(),
            "updated_at":    now_iso,
        }
        with self._db_engine.begin() as conn:
            result = conn.execute(
                _activity_presets.update()
                .where(_activity_presets.c.preset_id == preset.preset_id)
                .values(**values)
            )
            if result.rowcount == 0:
                conn.execute(_activity_presets.insert().values(**values))
            n = self._extract_counter_num(preset.preset_id)
            if n is not None:
                self._bump_counter(conn, "activity_preset", n)

    def get_activity_presets(self, activity_name: str | None = None
                             ) -> list[ActivityPreset]:
        """Return all presets, optionally filtered by activity name."""
        with self._db_engine.connect() as conn:
            stmt = select(_activity_presets).order_by(_activity_presets.c.preset_id)
            if activity_name:
                stmt = stmt.where(_activity_presets.c.activity_name == activity_name)
            rows = conn.execute(stmt).fetchall()
        return [self._row_to_preset(row) for row in rows]

    def get_activity_preset(self, preset_id: str) -> ActivityPreset | None:
        with self._db_engine.connect() as conn:
            row = conn.execute(
                select(_activity_presets)
                .where(_activity_presets.c.preset_id == preset_id)
            ).fetchone()
        return self._row_to_preset(row) if row is not None else None

    def delete_activity_preset(self, preset_id: str) -> None:
        with self._db_engine.begin() as conn:
            conn.execute(
                _activity_presets.delete()
                .where(_activity_presets.c.preset_id == preset_id)
            )

    def next_activity_preset_id(self) -> str:
        """Return the next auto-generated preset ID (AP001, AP002, …)."""
        with self._db_engine.connect() as conn:
            n = conn.execute(
                select(_counters.c.value)
                .where(_counters.c.name == "activity_preset")
            ).scalar()
        return f"AP{(n or 0) + 1:03d}"

    @staticmethod
    def _row_to_preset(row) -> ActivityPreset:
        import json
        return ActivityPreset(
            preset_id=row.preset_id,
            activity_name=row.activity_name,
            name=row.name,
            description=row.description or "",
            params=json.loads(row.params or "{}"),
            created_at=datetime.fromisoformat(row.created_at),
            updated_at=datetime.fromisoformat(row.updated_at),
        )

    # ------------------------------------------------------------------
    # Declarative activities (behaviour specs authored as data)
    # ------------------------------------------------------------------

    def save_declarative_activity(self, activity: DeclarativeActivity) -> None:
        """Insert or update a declarative activity. ``activity_id`` must be set."""
        import json
        now_iso = datetime.now().isoformat()
        values = {
            "activity_id": activity.activity_id,
            "name":        activity.name,
            "description": activity.description,
            "spec":        json.dumps(activity.spec),
            "created_at":  (activity.created_at or datetime.now()).isoformat(),
            "updated_at":  now_iso,
        }
        with self._db_engine.begin() as conn:
            result = conn.execute(
                _declarative_activities.update()
                .where(_declarative_activities.c.activity_id == activity.activity_id)
                .values(**values)
            )
            if result.rowcount == 0:
                conn.execute(_declarative_activities.insert().values(**values))
            n = self._extract_counter_num(activity.activity_id)
            if n is not None:
                self._bump_counter(conn, "declarative_activity", n)

    def get_declarative_activities(self) -> list[DeclarativeActivity]:
        with self._db_engine.connect() as conn:
            rows = conn.execute(
                select(_declarative_activities)
                .order_by(_declarative_activities.c.activity_id)
            ).fetchall()
        return [self._row_to_declarative(row) for row in rows]

    def get_declarative_activity(self, activity_id: str
                                 ) -> DeclarativeActivity | None:
        with self._db_engine.connect() as conn:
            row = conn.execute(
                select(_declarative_activities)
                .where(_declarative_activities.c.activity_id == activity_id)
            ).fetchone()
        return self._row_to_declarative(row) if row is not None else None

    def delete_declarative_activity(self, activity_id: str) -> None:
        with self._db_engine.begin() as conn:
            conn.execute(
                _declarative_activities.delete()
                .where(_declarative_activities.c.activity_id == activity_id)
            )

    def next_declarative_activity_id(self) -> str:
        """Return the next auto-generated id (DA001, DA002, …)."""
        with self._db_engine.connect() as conn:
            n = conn.execute(
                select(_counters.c.value)
                .where(_counters.c.name == "declarative_activity")
            ).scalar()
        return f"DA{(n or 0) + 1:03d}"

    @staticmethod
    def _row_to_declarative(row) -> DeclarativeActivity:
        import json
        return DeclarativeActivity(
            activity_id=row.activity_id,
            name=row.name,
            description=row.description or "",
            spec=json.loads(row.spec or "{}"),
            created_at=datetime.fromisoformat(row.created_at),
            updated_at=datetime.fromisoformat(row.updated_at),
        )

    @staticmethod
    def _row_to_template(row) -> SkinTemplate:
        import json
        return SkinTemplate(
            template_id=row.template_id,
            name=row.name,
            description=row.description or "",
            chamber_count=int(row.chamber_count),
            default_max_pressure=float(row.default_max_pressure),
            default_min_pressure=float(row.default_min_pressure),
            grid=json.loads(row.grid or "{}"),
            chamber_grid=json.loads(row.chamber_grid or "[]"),
            sensor_count=int(row.sensor_count),
            sensor_grid=json.loads(row.sensor_grid or "[]"),
        )
