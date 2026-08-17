"""Activity registry - single source of truth for all available activities.

All activities are **declarative** behaviours authored in the block editor and
stored in the database (``declarative_activities`` table). They are loaded on
demand via :func:`available_activities` / :func:`get_activity` when a
``Database`` is supplied, so the editor's behaviours appear in the session
dropdown without any of the editor (Blockly / QtWebEngine) being imported at
session time.

There are no longer any code-defined activities (``ACTIVITIES`` is empty): the
old hardcoded game activities (Group Touch / Organ Swap), the standalone
``SimulationActivity``, and the seed behaviour conditions have all been
removed - example behaviours ship as importable JSON instead. Every behaviour
runs on the ``ScriptedActivity`` engine; simulation is a per-activity flag
(``simulation_mode``) exposed as a checkbox in the SessionSetupDialog.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.activities.base_activity import BaseActivity
from src.activities.scripted_activity import ScriptedActivity

if TYPE_CHECKING:
    from src.data.database import Database

logger = logging.getLogger(__name__)

# No code-defined activities ship with the app; all behaviours come from the
# block editor (DB) or imported JSON examples.
ACTIVITIES: list[BaseActivity] = []


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
        except Exception:   # noqa: BLE001 - a bad saved spec must not crash startup
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

    Tolerates the simulation display suffix (``"... (Simulation)"``) so a session
    persisted in simulation mode still resolves back to its activity. Searches
    the DB-authored behaviours too when a ``Database`` is supplied."""
    base = name.removesuffix(BaseActivity.SIM_SUFFIX)
    return next((a for a in available_activities(db) if a.name == base), None)
