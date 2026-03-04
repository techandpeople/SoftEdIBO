"""Persistence of the most recent robot-unit=>participant assignments.

Stored as a JSON file (``data/last_assignments.json``) so it survives
between sessions without polluting the relational database.

Format::

    {
      "robot_ids":      ["turtle-1"],
      "participant_ids": ["P001", "P002"],
      "assignments": [
        {"robot_id": "turtle-1", "participant_id": "P001", "unit_ids": ["skin1"]},
        ...
      ]
    }
"""

import json
import logging
from pathlib import Path

from src.data.models import SessionAssignment

logger = logging.getLogger(__name__)


def load(path: Path) -> dict | None:
    """Load last-assignment data from *path*.  Returns ``None`` if not found or corrupt."""
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logger.warning("Could not load last assignments from %s", path)
        return None


def save(
    path: Path,
    robot_ids: list[str],
    participant_ids: list[str],
    assignments: list[SessionAssignment],
) -> None:
    """Persist current assignments to *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "robot_ids": robot_ids,
        "participant_ids": participant_ids,
        "assignments": [
            {
                "robot_id": a.robot_id,
                "participant_id": a.participant_id,
                "unit_ids": a.unit_ids,
            }
            for a in assignments
        ],
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        logger.warning("Could not save last assignments to %s", path)


def should_prefill(
    last: dict,
    current_robot_ids: list[str],
    current_participant_ids: list[str],
) -> bool:
    """Return True if last assignments are compatible enough to pre-fill.

    Conditions (both must hold):
    - No robot from the previous session was removed
      (current set is a superset of previous set).
    - More than half of the previous participants are still selected.
    """
    prev_robots = set(last.get("robot_ids", []))
    prev_parts = set(last.get("participant_ids", []))
    cur_robots = set(current_robot_ids)
    cur_parts = set(current_participant_ids)

    no_robot_removed = prev_robots <= cur_robots
    common_parts = len(prev_parts & cur_parts)
    enough_participants = common_parts > len(prev_parts) / 2 if prev_parts else False

    return no_robot_removed and enough_participants


def to_session_assignments(last: dict, session_id: str) -> list[SessionAssignment]:
    """Convert last-assignment data to ``SessionAssignment`` objects for a new session."""
    return [
        SessionAssignment(
            session_id=session_id,
            robot_id=entry["robot_id"],
            participant_id=entry["participant_id"],
            unit_ids=entry.get("unit_ids", []),
        )
        for entry in last.get("assignments", [])
    ]
