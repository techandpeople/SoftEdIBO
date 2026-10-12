"""A session laid out on a time axis, ready to draw.

Builds lanes of time-stamped items from a session's interaction model: the
activity phases of each unit, one lane of touches per source, and lanes for
what the robot did, its discrete inputs and what the observer coded. The
drawing widget only paints what this module hands it.

No GUI and no database access.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.data.interaction_model import InteractionModel
from src.data.session_data import SessionData, event_details

# Item kinds (the drawing widget picks a colour per kind).
KIND_PHASE = "phase"
KIND_TOUCH = "touch"
KIND_ROBOT = "robot"          # robot-initiated action
KIND_RESPONSE = "response"    # robot action answering a touch
KIND_INPUT = "input"
KIND_OBSERVER = "observer"
KIND_MARKER = "marker"
KIND_SESSION = "session"

# A touch with no recorded release is drawn this long.
_OPEN_TOUCH_MS = 60.0


@dataclass(frozen=True)
class TimelineItem:
    """One thing on a lane. A point when ``end_ms == start_ms``."""

    start_ms: float
    end_ms: float
    label: str
    kind: str
    group: str = ""           # e.g. the phase name, to colour phases apart

    @property
    def is_point(self) -> bool:
        return self.end_ms <= self.start_ms


@dataclass
class TimelineLane:
    label: str
    items: list[TimelineItem] = field(default_factory=list)


@dataclass
class Timeline:
    duration_ms: float
    lanes: list[TimelineLane] = field(default_factory=list)


def _point(t_ms: float, label: str, kind: str) -> TimelineItem:
    return TimelineItem(t_ms, t_ms, label, kind)


def _phase_lanes(model: InteractionModel, duration_ms: float) -> list[TimelineLane]:
    by_unit: dict[str, list[dict]] = {}
    for row in model.milestones().rows:
        by_unit.setdefault(row["unit"], []).append(row)
    lanes = []
    for unit, changes in by_unit.items():
        lane = TimelineLane(f"Phases: {unit}")
        for index, change in enumerate(changes):
            end = (changes[index + 1]["t_ms"] if index + 1 < len(changes)
                   else duration_ms)
            lane.items.append(TimelineItem(
                float(change["t_ms"]), float(max(end, change["t_ms"])),
                str(change["to_state"]), KIND_PHASE, str(change["to_state"])))
        lanes.append(lane)
    return lanes


def _touch_label(row: dict) -> str:
    parts = [row["gesture"] or "touch"]
    if row["duration_ms"] is not None:
        parts.append(f"{row['duration_ms']:.0f} ms")
    if row["peak_ut"] is not None:
        parts.append(f"peak {row['peak_ut']:.0f} uT")
    if row["skin_id"]:
        parts.append(f"on {row['skin_id']}")
    return ", ".join(parts)


def _touch_lanes(model: InteractionModel) -> list[TimelineLane]:
    lanes: dict[str, TimelineLane] = {}
    for row in model.touches().rows:
        lane = lanes.setdefault(row["source"], TimelineLane(row["source"]))
        start = float(row["t_ms"])
        length = row["duration_ms"] if row["duration_ms"] else _OPEN_TOUCH_MS
        lane.items.append(TimelineItem(
            start, start + float(length), _touch_label(row), KIND_TOUCH))
    return [lanes[name] for name in sorted(lanes)]


def _event_lanes(data: SessionData) -> list[TimelineLane]:
    start = data.session.start_time
    robot = TimelineLane("Robot")
    inputs = TimelineLane("Inputs")
    observer = TimelineLane("Observer")
    session = TimelineLane("Session")
    for event in data.corrected_events():
        t_ms = (event.timestamp - start).total_seconds() * 1000.0
        details = event_details(event)
        if event.type == "robot":
            answering = details.get("cause", "phase") != "phase"
            robot.items.append(_point(
                t_ms, f"{event.action} ({event.target})",
                KIND_RESPONSE if answering else KIND_ROBOT))
        elif event.type == "input":
            inputs.items.append(_point(
                t_ms, f"{event.action} = {details.get('value')} ({event.target})",
                KIND_INPUT))
        elif event.type == "observer":
            observer.items.append(_point(
                t_ms, f"{event.participant_id} {event.action}", KIND_OBSERVER))
        elif event.type == "marker":
            observer.items.append(_point(
                t_ms, f"marker: {event.metadata}".rstrip(": "), KIND_MARKER))
        elif event.type == "session":
            session.items.append(_point(
                t_ms, f"{event.action} {event.target}".strip(), KIND_SESSION))
    return [lane for lane in (robot, inputs, observer, session) if lane.items]


def build_timeline(data: SessionData, model: InteractionModel) -> Timeline:
    """Lay ``data`` out on a time axis, touches grouped as ``model`` does."""
    duration_ms = float(model.session().rows[0]["duration_s"] or 0.0) * 1000.0
    lanes = (_phase_lanes(model, duration_ms) + _touch_lanes(model)
             + _event_lanes(data))
    latest = max((item.end_ms for lane in lanes for item in lane.items),
                 default=0.0)
    return Timeline(max(duration_ms, latest), lanes)
