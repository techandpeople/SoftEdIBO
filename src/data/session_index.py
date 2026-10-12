"""The list of sessions shown in the Data tab.

One row per session with what a researcher scans for: when, how long, how
many children, the review status and whether the activity was completed.
No GUI.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from src.data.interaction_model import InteractionModel
from src.data.session_data import SessionData, load_session_data

if TYPE_CHECKING:
    from src.data.database import Database

NO_GROUP = "(no group)"


@dataclass(frozen=True)
class SessionListRow:
    session_id: str
    activity: str
    start: datetime
    duration_s: float
    children: int
    status: str
    group: str            # "" when the session has no group label
    finished: str         # "2/3" units finished, "" when unknown
    search_text: str      # lower-case haystack for the text filter

    def matches(self, text: str, status: str) -> bool:
        """Whether the row passes the text filter and the status filter
        (an empty filter matches everything)."""
        if status and self.status != status:
            return False
        return not text or text.strip().lower() in self.search_text


def session_list_row(data: SessionData) -> SessionListRow:
    """Summarise one session for the list."""
    row = InteractionModel(data).session().rows[0]
    finished = (f"{row['units_finished']}/{row['units']}" if row["units"] else "")
    participants = " ".join(
        f"{p.participant_id} {p.alias}" for p in data.participants)
    return SessionListRow(
        session_id=data.session.session_id,
        activity=data.session.activity_name,
        start=data.session.start_time,
        duration_s=float(row["duration_s"] or 0.0),
        children=int(row["participants"]),
        status=str(row["status"]),
        group=str(row["group"]),
        finished=finished,
        search_text=" ".join((
            data.session.session_id, data.session.activity_name,
            str(row["group"]), str(row["robot_kinds"]), participants)).lower(),
    )


def build_session_rows(db: "Database") -> list[SessionListRow]:
    """One row per stored session, newest first."""
    rows = []
    for session in db.get_all_sessions():
        data = load_session_data(db, session.session_id)
        if data is not None:
            rows.append(session_list_row(data))
    return sorted(rows, key=lambda r: r.start, reverse=True)
