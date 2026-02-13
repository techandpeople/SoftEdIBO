"""Session management for SoftEdIBO activities."""

import logging
from datetime import datetime
from enum import Enum
from typing import Any

from src.core.participant import Participant

logger = logging.getLogger(__name__)


class SessionState(Enum):
    """Possible states of a session."""
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"


class Session:
    """Represents a study session with participants and a specific activity."""

    def __init__(self, session_id: str, activity_name: str):
        self.session_id = session_id
        self.activity_name = activity_name
        self._state = SessionState.CREATED
        self._participants: list[Participant] = []
        self._start_time: datetime | None = None
        self._end_time: datetime | None = None

    @property
    def state(self) -> SessionState:
        """Get current session state."""
        return self._state

    @property
    def participants(self) -> list[Participant]:
        """Get list of participants."""
        return self._participants

    @property
    def duration_seconds(self) -> float | None:
        """Get session duration in seconds, or None if not started."""
        if self._start_time is None:
            return None
        end = self._end_time or datetime.now()
        return (end - self._start_time).total_seconds()

    def add_participant(self, participant: Participant) -> None:
        """Add a participant to the session."""
        self._participants.append(participant)
        logger.info("Added participant %s to session %s", participant.participant_id, self.session_id)

    def start(self) -> None:
        """Start the session."""
        self._state = SessionState.RUNNING
        self._start_time = datetime.now()
        logger.info("Session %s started", self.session_id)

    def pause(self) -> None:
        """Pause the session."""
        self._state = SessionState.PAUSED
        logger.info("Session %s paused", self.session_id)

    def resume(self) -> None:
        """Resume a paused session."""
        self._state = SessionState.RUNNING
        logger.info("Session %s resumed", self.session_id)

    def finish(self) -> None:
        """Finish the session."""
        self._state = SessionState.FINISHED
        self._end_time = datetime.now()
        logger.info("Session %s finished", self.session_id)

    def to_dict(self) -> dict[str, Any]:
        """Serialize session data."""
        return {
            "session_id": self.session_id,
            "activity_name": self.activity_name,
            "state": self._state.value,
            "participants": [p.to_dict() for p in self._participants],
            "start_time": self._start_time.isoformat() if self._start_time else None,
            "end_time": self._end_time.isoformat() if self._end_time else None,
        }
