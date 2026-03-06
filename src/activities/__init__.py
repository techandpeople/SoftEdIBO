"""Activity registry — single source of truth for all available activities."""

from __future__ import annotations

from src.activities.base_activity import BaseActivity
from src.activities.group_touch import GroupTouchActivity
from src.activities.simulation_activity import SimulationActivity

ACTIVITIES: list[BaseActivity] = [
    GroupTouchActivity(),
    SimulationActivity(),
]


def get_activity(name: str) -> BaseActivity | None:
    """Return the activity instance with the given name, or None."""
    return next((a for a in ACTIVITIES if a.name == name), None)
