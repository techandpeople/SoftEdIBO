"""Activity registry — single source of truth for all available activities.

Note: ``SimulationActivity`` is intentionally NOT registered. Simulation is
now a per-activity flag (``simulation_mode``) exposed as a checkbox in the
SessionSetupDialog — any activity can run in simulation, so a separate
"Simulation" dropdown entry would be redundant.
"""

from __future__ import annotations

from src.activities.base_activity import BaseActivity
from src.activities.group_touch import GroupTouchActivity
from src.activities.organ_swap import OrganSwapActivity

ACTIVITIES: list[BaseActivity] = [
    GroupTouchActivity(),
    OrganSwapActivity(),
]


def get_activity(name: str) -> BaseActivity | None:
    """Return the activity instance with the given name, or None.

    Tolerates the simulation display suffix (``"… (Simulation)"``) so a session
    persisted in simulation mode still resolves back to its activity."""
    base = name.removesuffix(BaseActivity.SIM_SUFFIX)
    return next((a for a in ACTIVITIES if a.name == base), None)
