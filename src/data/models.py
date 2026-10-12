"""Data models for SoftEdIBO persistence."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.core.skin_config import DEFAULT_MAX_KPA, DEFAULT_MIN_KPA


@dataclass
class SessionRecord:
    """Stored record of a study session."""
    session_id: str
    activity_name: str
    start_time: datetime
    end_time: datetime | None = None
    notes: str = ""


@dataclass
class TrashedSession:
    """Summary of a session sitting in the trash, awaiting restore or purge."""
    session_id: str
    activity_name: str
    start_time: datetime
    end_time: datetime | None
    trashed_at: datetime


@dataclass
class ParticipantRecord:
    """Stored record of a participant."""
    participant_id: str
    alias: str
    age: int | None = None


@dataclass
class SessionAssignment:
    """Assignment of a robot's units (skins/branches) to a participant."""
    session_id: str
    robot_id: str
    participant_id: str
    unit_ids: list[str]  # skin_ids / branch ids assigned to this participant


@dataclass
class InteractionEvent:
    """A single interaction event recorded during a session."""
    event_id: int | None = None
    session_id: str = ""
    participant_id: str = ""
    type: str = ""  # "turtle", "tree", "thymio"
    action: str = ""  # "inflate", "deflate", "touch", "share", etc.
    target: str = ""  # chamber ID, branch ID, etc.
    timestamp: datetime = field(default_factory=datetime.now)
    metadata: str = ""  # JSON string for extra data


# Review status of a session, set by the researcher in the Data tab.
SESSION_STATUS_VALID = "valid"
SESSION_STATUS_TEST = "test"
SESSION_STATUS_EXCLUDED = "excluded"
SESSION_STATUSES = (SESSION_STATUS_VALID, SESSION_STATUS_TEST,
                    SESSION_STATUS_EXCLUDED)


@dataclass
class SessionAnnotation:
    """What the researcher adds to a session after the fact.

    ``group_label`` names the group of children (the sessions of one visit
    share it) and ``visit_order`` is the position of this session within the
    visit. Kept apart from the session record so raw data is never rewritten.
    """
    session_id: str
    status: str = SESSION_STATUS_VALID
    group_label: str = ""
    visit_order: int | None = None
    notes: str = ""
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class EventCorrection:
    """A researcher's correction laid over one logged event.

    The event row itself is left untouched: analysis applies the correction
    on top (another participant, or the event left out), so the original
    record can always be recovered by deleting the correction.
    """
    event_id: int
    session_id: str
    participant_id: str | None = None    # None = keep the logged participant
    excluded: bool = False
    note: str = ""
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class ActivityPreset:
    """A named parameter preset for an Activity.

    The activity itself (Python class) defines the parameter schema via its
    ``PARAMS`` class attribute. The preset only carries the user-tunable
    values for that schema - multiple presets per activity are supported so
    the same Organ Swap can have an 'Easy', 'Therapy v3', etc.
    """
    preset_id: str = ""                          # "AP001", "AP002", ...
    activity_name: str = ""                      # matches ACTIVITIES registry
    name: str = ""                               # human-readable
    description: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class DeclarativeActivity:
    """A behaviour spec authored as data (not Python).

    The ``spec`` is the declarative state machine interpreted by
    :class:`~src.activities.scripted_activity.ScriptedActivity` (see
    :mod:`src.activities.catalog` for its shape). Authored by hand today and
    by the Blockly block editor later - the editor compiles its blocks down to
    this same spec, so nothing of the editor is needed at session time.
    """
    activity_id: str = ""                         # "DA001", "DA002", ...
    name: str = ""                                # human-readable
    description: str = ""
    spec: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


@dataclass
class SkinTemplate:
    """Reusable skin layout template - shared across skins of any robot.

    A template captures everything that's "layout-shaped" about a skin: number
    of chambers, default pressure caps, the painted grid (chamber zones), and
    the optional touch-sensor grid. It does NOT capture a node MAC - that's
    bound at skin-instance time when the template is applied.
    """
    template_id: str = ""                                # "T001", "T002", ...
    name: str = ""                                       # human-readable
    description: str = ""
    chamber_count: int = 1
    default_max_pressure: float = DEFAULT_MAX_KPA        # kPa
    default_min_pressure: float = DEFAULT_MIN_KPA        # kPa
    grid: dict[str, int] = field(default_factory=lambda: {"cols": 8, "rows": 4})
    chamber_grid: list[list[int]] = field(default_factory=list)
    sensor_count: int = 0                                # 0 -> no touch sensor
    sensor_grid: list[list[int]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "template_id":          self.template_id,
            "name":                 self.name,
            "description":          self.description,
            "chamber_count":        self.chamber_count,
            "default_max_pressure": self.default_max_pressure,
            "default_min_pressure": self.default_min_pressure,
            "grid":                 self.grid,
            "chamber_grid":         self.chamber_grid,
            "sensor_count":         self.sensor_count,
            "sensor_grid":          self.sensor_grid,
        }
