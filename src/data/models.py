"""Data models for SoftEdIBO persistence."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SessionRecord:
    """Stored record of a study session."""
    session_id: str
    activity_name: str
    start_time: datetime
    end_time: datetime | None = None
    notes: str = ""


@dataclass
class ParticipantRecord:
    """Stored record of a participant."""
    participant_id: str
    alias: str
    age: int | None = None


@dataclass
class InteractionEvent:
    """A single interaction event recorded during a session."""
    event_id: int | None = None
    session_id: str = ""
    participant_id: str = ""
    robot_type: str = ""  # "turtle", "thymio", "tree"
    action: str = ""  # "inflate", "deflate", "touch", "share", etc.
    target: str = ""  # chamber ID, branch ID, etc.
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: str = ""  # JSON string for extra data
