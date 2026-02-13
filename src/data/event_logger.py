"""Event logger for recording interactions during sessions."""

import logging
from datetime import datetime

from src.data.database import Database
from src.data.models import InteractionEvent

logger = logging.getLogger(__name__)


class EventLogger:
    """Logs interaction events to the database in real-time."""

    def __init__(self, database: Database, session_id: str):
        self._db = database
        self._session_id = session_id

    def log(
        self,
        participant_id: str,
        robot_type: str,
        action: str,
        target: str = "",
        metadata: str = "",
    ) -> None:
        """Log a single interaction event."""
        event = InteractionEvent(
            session_id=self._session_id,
            participant_id=participant_id,
            robot_type=robot_type,
            action=action,
            target=target,
            timestamp=datetime.now(),
            metadata=metadata,
        )
        self._db.log_event(event)
        logger.debug(
            "Event: %s %s %s on %s",
            participant_id, action, robot_type, target,
        )
