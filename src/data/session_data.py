"""Everything recorded about one session, loaded once for analysis.

Gathers the session record, its events, assignments, participants, the
snapshot of what it ran with and the researcher's annotations/corrections
into one plain object, so the analysis code (``interaction_model``) never
talks to the database and can be tested with hand-built data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from src.data.models import (
    EventCorrection,
    InteractionEvent,
    ParticipantRecord,
    SessionAnnotation,
    SessionAssignment,
    SessionRecord,
)

if TYPE_CHECKING:
    from src.data.database import Database


@dataclass
class SessionData:
    """One session's raw record plus the researcher's overlay."""

    session: SessionRecord
    events: list[InteractionEvent] = field(default_factory=list)
    assignments: list[SessionAssignment] = field(default_factory=list)
    participants: list[ParticipantRecord] = field(default_factory=list)
    snapshots: dict[str, dict] = field(default_factory=dict)
    annotation: SessionAnnotation | None = None
    corrections: dict[int, EventCorrection] = field(default_factory=dict)

    def corrected_events(self) -> list[InteractionEvent]:
        """Events in time order with the corrections applied: excluded events
        dropped, re-attributed ones carrying the corrected participant. The
        stored events are not modified."""
        result: list[InteractionEvent] = []
        for event in sorted(self.events, key=lambda e: e.timestamp):
            correction = (self.corrections.get(event.event_id)
                          if event.event_id is not None else None)
            if correction is None:
                result.append(event)
            elif not correction.excluded:
                result.append(
                    replace(event, participant_id=correction.participant_id)
                    if correction.participant_id else event)
        return result

    def start_meta(self) -> dict[str, Any]:
        """Metadata of the session's start event (robots, kinds, simulation)."""
        for event in self.events:
            if event.type == "session" and event.action == "start":
                return event_details(event)
        return {}


def event_details(event: InteractionEvent) -> dict[str, Any]:
    """An event's metadata as a dict (empty for free text or no metadata)."""
    if not event.metadata:
        return {}
    try:
        details = json.loads(event.metadata)
    except (ValueError, TypeError):
        return {}
    return details if isinstance(details, dict) else {}


def load_session_data(db: "Database", session_id: str) -> SessionData | None:
    """Read everything about ``session_id`` (None when it does not exist)."""
    session = next((s for s in db.get_all_sessions()
                    if s.session_id == session_id), None)
    if session is None:
        return None
    db.flush_events()
    return SessionData(
        session=session,
        events=db.get_session_events(session_id),
        assignments=db.get_session_assignments(session_id),
        participants=db.get_session_participants(session_id),
        snapshots=db.get_session_snapshots(session_id),
        annotation=db.get_session_annotation(session_id),
        corrections=db.get_event_corrections(session_id),
    )
