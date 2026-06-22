"""Session CSV export — flattens the event timeline for offline analysis.

Lives apart from the GUI so the export schema and the participant-attribution
logic have a single home (the DataPanel just calls it). Each row is one
``interaction_event`` enriched with its session header and — crucially — the
``robot_id`` and ``participant`` the event belongs to, resolved from the
session's assignments.

Why the resolution matters: robot-level events (organ readings, cover
open/close, activity state) are logged with ``participant_id="system"`` and the
*patient id* in ``target`` (``"<robot>"`` or ``"<robot>/<skin>"`` for a Tree
branch). Touch events use ``"<skin>:<chamber>"``. To answer "what did child X
do?" the analyst needs those mapped back to a participant; this exporter does
that join once, here, so the CSV is analysis-ready.
"""

from __future__ import annotations

import csv
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from src.data.database import Database
    from src.data.models import InteractionEvent, SessionRecord

COLUMNS = (
    "session_id", "activity", "start", "end",
    "timestamp", "participant_id", "robot_id", "participant",
    "type", "action", "target", "metadata",
)


class SessionExporter:
    """Writes session events to CSV, attributing each to robot + participant.

    Args:
        db: Open database to read sessions, events, and assignments from.
    """

    def __init__(self, db: "Database"):
        self._db = db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def export_session(self, session_id: str, path: str) -> int:
        """Export one session. Returns the number of event rows written."""
        session = self._find_session(session_id)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = self._writer(f)
            return self._write_session(writer, session_id, session)

    def export_all(self, path: str) -> int:
        """Export every session. Returns the total event rows written."""
        total = 0
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = self._writer(f)
            for session in self._db.get_all_sessions():
                total += self._write_session(writer, session.session_id, session)
        return total

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _writer(f: TextIO) -> "csv._writer":
        writer = csv.writer(f)
        writer.writerow(COLUMNS)
        return writer

    def _find_session(self, session_id: str) -> "SessionRecord | None":
        return next((s for s in self._db.get_all_sessions()
                     if s.session_id == session_id), None)

    def _write_session(self, writer, session_id: str,
                       session: "SessionRecord | None") -> int:
        resolver = _Attribution(self._db.get_session_assignments(session_id))
        activity = session.activity_name if session else ""
        start = self._iso(session.start_time) if session else ""
        end = self._iso(session.end_time) if session and session.end_time else ""
        count = 0
        for event in self._db.get_session_events(session_id):
            robot_id, participant = resolver.resolve(event)
            writer.writerow([
                session_id, activity, start, end,
                self._iso(event.timestamp), event.participant_id,
                robot_id, participant,
                event.type, event.action, event.target, event.metadata,
            ])
            count += 1
        return count

    @staticmethod
    def _iso(dt) -> str:
        return dt.isoformat(timespec="seconds") if dt else ""


class _Attribution:
    """Maps an event to (robot_id, participant_id) using session assignments.

    Builds two lookups from the assignments:
    - ``unit → (robot, participant)`` for skin/branch-scoped events;
    - ``robot → participant`` for whole-robot events (only unambiguous when the
      robot has a single participant, e.g. Thymio).
    """

    def __init__(self, assignments) -> None:
        self._unit: dict[str, tuple[str, str]] = {}
        self._robot_participants: dict[str, set[str]] = {}
        for a in assignments:
            self._robot_participants.setdefault(a.robot_id, set()).add(a.participant_id)
            for unit in a.unit_ids:
                self._unit[unit] = (a.robot_id, a.participant_id)

    def resolve(self, event: "InteractionEvent") -> tuple[str, str]:
        """Return ``(robot_id, participant_id)`` for an event, best-effort.

        Explicit ``participant_id`` (touch events) wins. Otherwise the event's
        ``target`` is decoded: ``"<robot>/<skin>"`` or ``"<skin>:<chamber>"``
        resolve via the unit map; a bare ``"<robot>"`` resolves the robot and,
        when that robot has exactly one participant, attributes it too."""
        target = event.target or ""
        skin = self._skin_of(target)
        if skin and skin in self._unit:
            robot_id, participant = self._unit[skin]
            if event.participant_id not in ("", "system"):
                participant = event.participant_id
            return robot_id, participant

        robot_id = self._robot_of(target)
        participant = (event.participant_id
                       if event.participant_id not in ("", "system") else "")
        if not participant and robot_id:
            members = self._robot_participants.get(robot_id, set())
            if len(members) == 1:
                participant = next(iter(members))
        return robot_id, participant

    @staticmethod
    def _skin_of(target: str) -> str | None:
        """Extract a skin/unit id from a target, or None for a bare robot id."""
        if "/" in target:          # "<robot>/<skin>" patient id (Tree branch)
            return target.split("/", 1)[1]
        if ":" in target:          # "<skin>:<chamber>" touch target
            return target.split(":", 1)[0]
        return None

    def _robot_of(self, target: str) -> str:
        """Best-effort robot id from a target string."""
        if "/" in target:
            return target.split("/", 1)[0]
        if ":" in target:
            skin = target.split(":", 1)[0]
            return self._unit.get(skin, ("", ""))[0]
        # Bare token: a robot id if we know it, else assume it is one.
        if target in self._robot_participants:
            return target
        return target if target and target != "system" else ""
