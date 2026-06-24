"""Activity registry — single source of truth for all available activities.

Two kinds of activity are offered to the operator:

- **Code-defined** activities (``ACTIVITIES``): Group Touch, Organ Swap, and
  the three seed behaviour conditions. These ship with the app.
- **Declarative** activities authored in the block editor and stored in the
  database (``declarative_activities`` table). Loaded on demand via
  :func:`available_activities` / :func:`get_activity` when a ``Database`` is
  supplied, so the editor's behaviours appear in the session dropdown without
  any of the editor (Blockly / QtWebEngine) being imported at session time.

Note: ``SimulationActivity`` is intentionally NOT registered. Simulation is
now a per-activity flag (``simulation_mode``) exposed as a checkbox in the
SessionSetupDialog — any activity can run in simulation, so a separate
"Simulation" dropdown entry would be redundant.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.activities.base_activity import BaseActivity
from src.activities.group_touch import GroupTouchActivity
from src.activities.organ_swap import OrganSwapActivity
from src.activities.scripted_activity import ScriptedActivity
from src.activities.seed_behaviors import SEED_CONDITIONS

if TYPE_CHECKING:
    from src.data.database import Database

logger = logging.getLogger(__name__)

ACTIVITIES: list[BaseActivity] = [
    GroupTouchActivity(),
    OrganSwapActivity(),
    # Declarative behaviour engine — the study's 3 conditions as scripted
    # specs. These are editable later via the block editor (Fase 2); for now
    # they ship as code-defined seeds so the study can run.
    *(ScriptedActivity(name, desc, spec) for name, desc, spec in SEED_CONDITIONS),
]


def load_declarative_activities(db: "Database") -> list[ScriptedActivity]:
    """Build runnable activities from the behaviours saved in the block editor.

    Malformed specs are skipped (logged) so one bad row can't break the whole
    session dropdown.
    """
    activities: list[ScriptedActivity] = []
    for record in db.get_declarative_activities():
        try:
            activities.append(
                ScriptedActivity(record.name, record.description, record.spec))
        except Exception:   # noqa: BLE001 — a bad saved spec must not crash startup
            logger.exception("Skipping invalid declarative activity %s (%s)",
                             record.activity_id, record.name)
    return activities


def available_activities(db: "Database | None" = None) -> list[BaseActivity]:
    """All code-defined activities, plus the DB-authored ones when ``db`` is
    given. Used to populate the session activity dropdown."""
    activities = list(ACTIVITIES)
    if db is not None:
        activities.extend(load_declarative_activities(db))
    return activities


def get_activity(name: str, db: "Database | None" = None) -> BaseActivity | None:
    """Return the activity instance with the given name, or None.

    Tolerates the simulation display suffix (``"… (Simulation)"``) so a session
    persisted in simulation mode still resolves back to its activity. Searches
    the DB-authored behaviours too when a ``Database`` is supplied."""
    base = name.removesuffix(BaseActivity.SIM_SUFFIX)
    return next((a for a in available_activities(db) if a.name == base), None)
