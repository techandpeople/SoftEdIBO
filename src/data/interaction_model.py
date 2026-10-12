"""Interaction model: how the children touched, reacted and aligned.

Turns one session's logged events into tables a researcher can read and
export:

- **touches**    one row per touch (press paired with its release)
- **profiles**   one row per source: how much, how fast, how regularly
- **pairs**      one row per pair of sources: who leads, how aligned
- **milestones** one row per phase change: what triggered it, what it took
- **session**    one row: the design factors and the outcome

A *source* is whoever a touch is attributed to. By default that is the
participant; where one skin is shared and touches cannot be told apart by
participant, group by skin or by sensor instead (:data:`SOURCE_KEYS`).

Everything is derived from the stored events, so it works on sessions
recorded before this module existed (columns whose data was not logged then
are simply empty). No GUI, no database access, no numpy.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from typing import Any

from src.activities.catalog import is_final_state
from src.data.export import EventAttribution
from src.data.models import InteractionEvent
from src.data.session_data import SessionData, event_details

SOURCE_PARTICIPANT = "participant"
SOURCE_SKIN = "skin"
SOURCE_SENSOR = "sensor"
SOURCE_KEYS = (SOURCE_PARTICIPANT, SOURCE_SKIN, SOURCE_SENSOR)

UNKNOWN_SOURCE = "unknown"

# How participants and touch surfaces relate in a session.
SURFACE_SHARED = "shared"                        # several children, one skin
SURFACE_INDIVIDUAL_SHARED = "individual_shared"  # own skin each, one robot
SURFACE_INDIVIDUAL_SEPARATE = "individual_separate"   # own robot each


@dataclass(frozen=True)
class ModelParams:
    """Windows used by the derived measures (all in milliseconds)."""

    # Two touches of different sources this close count as "together".
    together_ms: float = 300.0
    # A touch's nearest touch by another source is searched this far away.
    nearest_other_ms: float = 2000.0
    # Gaps longer than this are a pause, not part of a rhythm.
    rhythm_max_gap_ms: float = 3000.0
    # A touch later than this after a robot action is not a reaction to it.
    reaction_max_ms: float = 10000.0
    # Cross-correlation of two sources' touch onsets: bin width and lag range.
    xcorr_bin_ms: float = 100.0
    xcorr_max_lag_ms: float = 2000.0
    # A classified gesture is attached to touches up to this long before the
    # segment it was classified from (the classifier waits for the bout to end).
    gesture_slack_ms: float = 1500.0


@dataclass(frozen=True)
class Column:
    """One column of a table: its key, meaning and unit."""

    name: str
    description: str
    unit: str = ""


@dataclass
class Table:
    """A named table: ordered columns and rows keyed by column name."""

    name: str
    columns: tuple[Column, ...]
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def headers(self) -> list[str]:
        return [column.name for column in self.columns]

    def values(self) -> list[list[Any]]:
        """Rows as lists, in column order (missing cells are ``None``)."""
        return [[row.get(name) for name in self.headers] for row in self.rows]


TOUCH_COLUMNS = (
    Column("session_id", "Session the touch belongs to."),
    Column("source", "Who the touch is attributed to (see the grouping used)."),
    Column("participant_id", "Participant assigned to the touched skin."),
    Column("robot_id", "Robot the touched skin is on."),
    Column("skin_id", "Touched skin."),
    Column("sensor_id", "Touch sensor that detected the press."),
    Column("time", "Wall-clock time of the press.", "ISO 8601"),
    Column("t_ms", "Time of the press since the session started.", "ms"),
    Column("duration_ms", "How long the touch lasted (press to release).", "ms"),
    Column("press_ut", "Sensor magnitude at the press sample.", "uT"),
    Column("peak_ut", "Strongest magnitude during the touch.", "uT"),
    Column("release_ut", "Sensor magnitude at the release sample.", "uT"),
    Column("gesture", "Touch type the classifier assigned, if any."),
    Column("touch_form", "Shape in time: short, press, rest, repeated_regular, "
           "repeated_irregular."),
    Column("touch_movement", "static, slide or rub."),
    Column("touch_force", "light, medium or strong (against the skin's "
           "activation threshold)."),
    Column("touch_extent", "point, multi (several zones) or whole skin."),
    Column("interval_ms", "Time since the same source's previous touch.", "ms"),
    Column("state", "Activity phase the skin was in."),
    Column("since_state_ms", "Time since that phase started.", "ms"),
    Column("last_robot_action", "Last robot-initiated action on this skin."),
    Column("since_robot_action_ms",
           "Time since that action (the child's reaction latency).", "ms"),
    Column("nearest_other_source", "Other source whose touch was closest in time."),
    Column("nearest_other_lag_ms",
           "This touch minus that touch: negative = this source was first.", "ms"),
)

PROFILE_COLUMNS = (
    Column("session_id", "Session."),
    Column("source", "Who the row describes."),
    Column("touches", "Number of touches.", "count"),
    Column("touches_per_min", "Touches per minute of session.", "1/min"),
    Column("median_interval_ms", "Median time between consecutive touches "
           "(pauses excluded).", "ms"),
    Column("tempo_bpm", "Touch tempo: 60000 / median interval.", "1/min"),
    Column("interval_cv", "Rhythm regularity: standard deviation / mean of "
           "the intervals. 0 = perfectly even.", "ratio"),
    Column("rhythm_intervals", "Intervals the rhythm measures are based on.",
           "count"),
    Column("median_duration_ms", "Median touch duration.", "ms"),
    Column("mean_peak_ut", "Mean of the touches' strongest magnitude.", "uT"),
    Column("median_reaction_ms", "Median time from a robot-initiated action "
           "to this source's first touch after it.", "ms"),
    Column("reactions", "Robot actions this source reacted to.", "count"),
    Column("together_share", "Share of this source's touches made together "
           "with another source.", "ratio"),
    Column("gestures", "Touches per classified touch type.", "JSON"),
    Column("observed", "Observer-coded behaviours for this participant.", "JSON"),
)

PAIR_COLUMNS = (
    Column("session_id", "Session."),
    Column("source_a", "First source of the pair."),
    Column("source_b", "Second source of the pair."),
    Column("together", "Touches of A that had a touch of B within the "
           "'together' window.", "count"),
    Column("together_share", "That count / the touches of the less active "
           "source.", "ratio"),
    Column("median_abs_lag_ms", "Median time between the paired touches.", "ms"),
    Column("a_leads_share", "Share of the paired touches where A was first.",
           "ratio"),
    Column("xcorr_peak", "Peak of the cross-correlation of the two touch "
           "onset series (1 = identical pattern).", "ratio"),
    Column("xcorr_lag_ms", "Lag at that peak: positive = B follows A.", "ms"),
)

MILESTONE_COLUMNS = (
    Column("session_id", "Session."),
    Column("unit", "Robot/skin the phase change happened on."),
    Column("from_state", "Phase left."),
    Column("to_state", "Phase entered."),
    Column("final", "Whether the entered phase ends the activity."),
    Column("time", "Wall-clock time of the change.", "ISO 8601"),
    Column("t_ms", "Time of the change since the session started.", "ms"),
    Column("in_state_ms", "Time spent in the phase left.", "ms"),
    Column("trigger", "What caused the change: condition, step, manual, start."),
    Column("condition", "The condition that fired, as authored.", "JSON"),
    Column("touches", "Touches on this skin during the phase left.", "count"),
    Column("touches_by_source", "Those touches per source.", "JSON"),
    Column("inputs", "Changes of each discrete input during the phase left.",
           "JSON"),
)

SESSION_COLUMNS = (
    Column("session_id", "Session."),
    Column("group", "Group of children (sessions of one visit share it)."),
    Column("visit_order", "Position of this session within the visit."),
    Column("status", "Researcher's review: valid, test or excluded."),
    Column("activity", "Activity that ran."),
    Column("robot_kinds", "Kinds of robot used."),
    Column("surface", "How children and touch surfaces relate: shared, "
           "individual_shared (own skin, same robot), individual_separate."),
    Column("participants", "Number of participants.", "count"),
    Column("start", "Session start.", "ISO 8601"),
    Column("duration_s", "Session length.", "s"),
    Column("touches", "Number of touches.", "count"),
    Column("milestones", "Phase changes that were not operator-forced.", "count"),
    Column("units", "Robot/skin units that ran the activity.", "count"),
    Column("units_finished", "Units that reached a final phase.", "count"),
    Column("time_to_finish_ms", "When the last unit reached its final phase "
           "(empty unless all did).", "ms"),
    Column("notes", "Researcher's notes."),
)


@dataclass
class _Touch:
    """A press paired with its release, plus what was derived about it."""

    event: InteractionEvent
    skin_id: str
    robot_id: str
    participant_id: str
    sensor_id: int | None
    t_ms: float
    press_ut: float | None
    duration_ms: float | None = None
    peak_ut: float | None = None
    release_ut: float | None = None
    source: str = UNKNOWN_SOURCE
    gesture: str = ""
    axes: dict[str, Any] = field(default_factory=dict)
    interval_ms: float | None = None
    state: str = ""
    since_state_ms: float | None = None
    last_action: str = ""
    since_action_ms: float | None = None
    nearest_source: str = ""
    nearest_lag_ms: float | None = None


def _iso(moment: datetime | None) -> str:
    return moment.isoformat(timespec="milliseconds") if moment else ""


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _json(value: dict) -> str:
    return json.dumps(value, sort_keys=True) if value else ""


def _skin_of(target: str) -> str:
    """Skin id of an event target (``skin:chamber`` or ``robot/skin``)."""
    if "/" in target:
        return target.split("/", 1)[1]
    return target.split(":", 1)[0]


class InteractionModel:
    """Derives the interaction tables of one session.

    Args:
        data: The session's recorded data (with corrections).
        source_key: How touches are grouped into sources.
        params: Windows of the derived measures.
    """

    def __init__(self, data: SessionData,
                 source_key: str = SOURCE_PARTICIPANT,
                 params: ModelParams | None = None) -> None:
        if source_key not in SOURCE_KEYS:
            raise ValueError(f"unknown source key {source_key!r}")
        self._data = data
        self._source_key = source_key
        self._params = params or ModelParams()
        self._session_id = data.session.session_id
        self._start = data.session.start_time
        self._events = data.corrected_events()
        self._attribution = EventAttribution(data.assignments)
        self._state_changes = self._collect_state_changes()
        self._touches = self._build_touches()

    # ------------------------------------------------------------------
    # Public tables
    # ------------------------------------------------------------------

    def touches(self) -> Table:
        rows = [{
            "session_id": self._session_id,
            "source": t.source,
            "participant_id": t.participant_id,
            "robot_id": t.robot_id,
            "skin_id": t.skin_id,
            "sensor_id": t.sensor_id,
            "time": _iso(t.event.timestamp),
            "t_ms": _round(t.t_ms, 0),
            "duration_ms": _round(t.duration_ms, 0),
            "press_ut": t.press_ut,
            "peak_ut": t.peak_ut,
            "release_ut": t.release_ut,
            "gesture": t.gesture,
            "touch_form": t.axes.get("form", ""),
            "touch_movement": t.axes.get("movement", ""),
            "touch_force": t.axes.get("force", ""),
            "touch_extent": t.axes.get("extent", ""),
            "interval_ms": _round(t.interval_ms, 0),
            "state": t.state,
            "since_state_ms": _round(t.since_state_ms, 0),
            "last_robot_action": t.last_action,
            "since_robot_action_ms": _round(t.since_action_ms, 0),
            "nearest_other_source": t.nearest_source,
            "nearest_other_lag_ms": _round(t.nearest_lag_ms, 0),
        } for t in self._touches]
        return Table("Touches", TOUCH_COLUMNS, rows)

    def profiles(self) -> Table:
        minutes = self._duration_ms() / 60000.0
        rows = []
        for source, touches in self._by_source().items():
            intervals = self._rhythm_intervals(touches)
            median_interval = statistics.median(intervals) if intervals else None
            reactions = self._reactions(source)
            durations = [t.duration_ms for t in touches if t.duration_ms is not None]
            peaks = [t.peak_ut for t in touches if t.peak_ut is not None]
            together = sum(
                1 for t in touches if t.nearest_lag_ms is not None
                and abs(t.nearest_lag_ms) <= self._params.together_ms)
            rows.append({
                "session_id": self._session_id,
                "source": source,
                "touches": len(touches),
                "touches_per_min": _round(len(touches) / minutes, 2)
                if minutes > 0 else None,
                "median_interval_ms": _round(median_interval, 0),
                "tempo_bpm": _round(60000.0 / median_interval, 1)
                if median_interval else None,
                "interval_cv": _round(self._cv(intervals), 3),
                "rhythm_intervals": len(intervals),
                "median_duration_ms": _round(
                    statistics.median(durations) if durations else None, 0),
                "mean_peak_ut": _round(
                    statistics.fmean(peaks) if peaks else None, 1),
                "median_reaction_ms": _round(
                    statistics.median(reactions) if reactions else None, 0),
                "reactions": len(reactions),
                "together_share": _round(together / len(touches), 3),
                "gestures": _json(self._count(t.gesture for t in touches)),
                "observed": _json(self._observed(source)),
            })
        return Table("Profiles", PROFILE_COLUMNS, rows)

    def pairs(self) -> Table:
        by_source = self._by_source()
        rows = []
        for a, b in combinations(sorted(by_source), 2):
            rows.append(self._pair_row(a, by_source[a], b, by_source[b]))
        return Table("Pairs", PAIR_COLUMNS, rows)

    def milestones(self) -> Table:
        rows = []
        for change in self._state_changes:
            details = change["details"]
            trigger = details.get("trigger") or {}
            span_start = change["t_ms"] - (change["in_state_ms"] or 0.0)
            in_span = [t for t in self._touches
                       if t.skin_id == change["skin_id"]
                       and span_start <= t.t_ms < change["t_ms"]]
            rows.append({
                "session_id": self._session_id,
                "unit": change["unit"],
                "from_state": details.get("from", ""),
                "to_state": change["to"],
                "final": change["final"],
                "time": _iso(change["time"]),
                "t_ms": _round(change["t_ms"], 0),
                "in_state_ms": _round(change["in_state_ms"], 0),
                "trigger": trigger.get("source", ""),
                "condition": _json(trigger.get("condition") or {}),
                "touches": len(in_span),
                "touches_by_source": _json(
                    self._count(t.source for t in in_span)),
                "inputs": _json(self._input_changes(
                    change["skin_id"], span_start, change["t_ms"])),
            })
        return Table("Milestones", MILESTONE_COLUMNS, rows)

    def session(self) -> Table:
        annotation = self._data.annotation
        meta = self._data.start_meta()
        units = sorted({c["unit"] for c in self._state_changes})
        finished = self._finish_times()
        all_finished = bool(units) and len(finished) == len(units)
        row = {
            "session_id": self._session_id,
            "group": annotation.group_label if annotation else "",
            "visit_order": annotation.visit_order if annotation else None,
            "status": annotation.status if annotation else "",
            "activity": self._data.session.activity_name,
            "robot_kinds": ", ".join(sorted(
                {str(k) for k in (meta.get("robot_kinds") or {}).values() if k})),
            "surface": self.surface(),
            "participants": len(self._data.participants),
            "start": _iso(self._start),
            "duration_s": _round(self._duration_ms() / 1000.0, 1),
            "touches": len(self._touches),
            "milestones": sum(
                1 for c in self._state_changes
                if (c["details"].get("trigger") or {}).get("source")
                in ("condition", "step")),
            "units": len(units),
            "units_finished": len(finished),
            "time_to_finish_ms": _round(max(finished.values()), 0)
            if all_finished else None,
            "notes": annotation.notes if annotation else "",
        }
        return Table("Sessions", SESSION_COLUMNS, [row])

    def surface(self) -> str:
        """How the children and the touch surfaces relate (see SURFACE_*)."""
        unit_owners: dict[str, set[str]] = {}
        robot_owners: dict[str, set[str]] = {}
        for assignment in self._data.assignments:
            robot_owners.setdefault(assignment.robot_id, set()).add(
                assignment.participant_id)
            for unit in assignment.unit_ids:
                unit_owners.setdefault(unit, set()).add(assignment.participant_id)
        if not robot_owners:
            return ""
        if any(len(owners) > 1 for owners in unit_owners.values()):
            return SURFACE_SHARED
        if any(len(owners) > 1 for owners in robot_owners.values()):
            return SURFACE_INDIVIDUAL_SHARED
        return SURFACE_INDIVIDUAL_SEPARATE

    def tables(self) -> list[Table]:
        """Every table, in reading order."""
        return [self.session(), self.milestones(), self.touches(),
                self.profiles(), self.pairs()]

    # ------------------------------------------------------------------
    # Timeline helpers
    # ------------------------------------------------------------------

    def _ms(self, moment: datetime) -> float:
        return (moment - self._start).total_seconds() * 1000.0

    def _duration_ms(self) -> float:
        end = self._data.session.end_time
        if end is None and self._events:
            end = self._events[-1].timestamp
        return max(0.0, self._ms(end)) if end is not None else 0.0

    def _collect_state_changes(self) -> list[dict[str, Any]]:
        """Phase changes in time order, with the time spent in the phase left
        (from the log when recorded, else from the previous change)."""
        spec = (self._data.snapshots.get("activity") or {}).get("spec") or {}
        changes: list[dict[str, Any]] = []
        last_by_unit: dict[str, float] = {}
        for event in self._events:
            if event.type != "activity" or event.action != "state":
                continue
            details = event_details(event)
            t_ms = self._ms(event.timestamp)
            in_state = details.get("in_state_ms")
            if in_state is None and event.target in last_by_unit:
                in_state = t_ms - last_by_unit[event.target]
            to_state = str(details.get("to", ""))
            final = details.get("final")
            if final is None and spec:
                final = is_final_state(spec, to_state)
            changes.append({
                "unit": event.target, "skin_id": _skin_of(event.target),
                "to": to_state, "final": final, "time": event.timestamp,
                "t_ms": t_ms, "in_state_ms": in_state, "details": details})
            last_by_unit[event.target] = t_ms
        return changes

    def _finish_times(self) -> dict[str, float]:
        """When each unit first reached a final phase."""
        finished: dict[str, float] = {}
        for change in self._state_changes:
            if change["final"] and change["unit"] not in finished:
                finished[change["unit"]] = change["t_ms"]
        return finished

    def _stimuli(self) -> dict[str, list[tuple[float, str]]]:
        """Robot-initiated moments per skin: ``(t_ms, label)`` in time order.

        A phase change and every action run by the phase program count; an
        action answering a touch or another event does not (it is a
        response, not a stimulus)."""
        stimuli: dict[str, list[tuple[float, str]]] = {}
        for change in self._state_changes:
            stimuli.setdefault(change["skin_id"], []).append(
                (change["t_ms"], f"phase:{change['to']}"))
        for event in self._events:
            if event.type != "robot":
                continue
            if event_details(event).get("cause", "phase") != "phase":
                continue
            stimuli.setdefault(_skin_of(event.target), []).append(
                (self._ms(event.timestamp), event.action))
        for moments in stimuli.values():
            moments.sort()
        return stimuli

    # ------------------------------------------------------------------
    # Touches
    # ------------------------------------------------------------------

    def _build_touches(self) -> list[_Touch]:
        touches = self._pair_presses()
        self._assign_sources(touches)
        self._assign_intervals(touches)
        self._assign_states(touches)
        self._assign_actions(touches)
        self._assign_gestures(touches)
        self._assign_nearest_other(touches)
        return touches

    def _pair_presses(self) -> list[_Touch]:
        touches: list[_Touch] = []
        open_presses: dict[tuple[str, Any], _Touch] = {}
        for event in self._events:
            if event.type != "touch":
                continue
            details = event_details(event)
            sensor = details.get("sensor_id")
            key = (event.target, sensor)
            if event.action == "press":
                robot_id, participant = self._attribution.resolve(event)
                touch = _Touch(
                    event=event, skin_id=_skin_of(event.target),
                    robot_id=robot_id, participant_id=participant,
                    sensor_id=sensor, t_ms=self._ms(event.timestamp),
                    press_ut=details.get("intensity_ut"))
                touches.append(touch)
                open_presses[key] = touch
            elif event.action == "release":
                touch = open_presses.pop(key, None)
                if touch is None:
                    continue
                duration = details.get("duration_ms")
                touch.duration_ms = (
                    float(duration) if duration is not None
                    else self._ms(event.timestamp) - touch.t_ms)
                touch.peak_ut = details.get("peak_intensity_ut")
                touch.release_ut = details.get("release_intensity_ut")
        return touches

    def _assign_sources(self, touches: list[_Touch]) -> None:
        for touch in touches:
            if self._source_key == SOURCE_SKIN:
                touch.source = touch.skin_id
            elif self._source_key == SOURCE_SENSOR:
                touch.source = (touch.skin_id if touch.sensor_id is None
                                else f"{touch.skin_id}#{touch.sensor_id}")
            else:
                touch.source = touch.participant_id or UNKNOWN_SOURCE

    @staticmethod
    def _assign_intervals(touches: list[_Touch]) -> None:
        last: dict[str, float] = {}
        for touch in touches:
            if touch.source in last:
                touch.interval_ms = touch.t_ms - last[touch.source]
            last[touch.source] = touch.t_ms

    def _assign_states(self, touches: list[_Touch]) -> None:
        for touch in touches:
            current = None
            for change in self._state_changes:
                if change["t_ms"] > touch.t_ms:
                    break
                if change["skin_id"] == touch.skin_id:
                    current = change
            if current is not None:
                touch.state = current["to"]
                touch.since_state_ms = touch.t_ms - current["t_ms"]

    def _assign_actions(self, touches: list[_Touch]) -> None:
        stimuli = self._stimuli()
        for touch in touches:
            previous = [m for m in stimuli.get(touch.skin_id, [])
                        if m[0] <= touch.t_ms]
            if previous:
                at_ms, label = previous[-1]
                touch.last_action = label
                touch.since_action_ms = touch.t_ms - at_ms

    def _assign_gestures(self, touches: list[_Touch]) -> None:
        for event in self._events:
            if event.type != "gesture":
                continue
            end_ms = self._ms(event.timestamp)
            details = event_details(event)
            span = float(details.get("duration_ms") or 0.0)
            start_ms = end_ms - span - self._params.gesture_slack_ms
            skin_id = _skin_of(event.target)
            for touch in touches:
                if (touch.skin_id == skin_id and not touch.gesture
                        and start_ms <= touch.t_ms <= end_ms):
                    touch.gesture = event.action
                    touch.axes = details

    def _assign_nearest_other(self, touches: list[_Touch]) -> None:
        window = self._params.nearest_other_ms
        for touch in touches:
            best: _Touch | None = None
            for other in touches:
                if other.source == touch.source:
                    continue
                gap = abs(touch.t_ms - other.t_ms)
                if gap <= window and (
                        best is None or gap < abs(touch.t_ms - best.t_ms)):
                    best = other
            if best is not None:
                touch.nearest_source = best.source
                touch.nearest_lag_ms = touch.t_ms - best.t_ms

    # ------------------------------------------------------------------
    # Per-source and per-pair measures
    # ------------------------------------------------------------------

    def _by_source(self) -> dict[str, list[_Touch]]:
        grouped: dict[str, list[_Touch]] = {}
        for touch in self._touches:
            grouped.setdefault(touch.source, []).append(touch)
        return grouped

    def _rhythm_intervals(self, touches: list[_Touch]) -> list[float]:
        return [t.interval_ms for t in touches
                if t.interval_ms is not None
                and 0 < t.interval_ms <= self._params.rhythm_max_gap_ms]

    @staticmethod
    def _cv(values: list[float]) -> float | None:
        if len(values) < 2:
            return None
        mean = statistics.fmean(values)
        return statistics.pstdev(values) / mean if mean > 0 else None

    @staticmethod
    def _count(labels) -> dict[str, int]:
        counts: dict[str, int] = {}
        for label in labels:
            if label:
                counts[label] = counts.get(label, 0) + 1
        return counts

    def _reactions(self, source: str) -> list[float]:
        """Latency from each robot-initiated moment to ``source``'s first
        touch on that skin before the next such moment."""
        latencies: list[float] = []
        for skin_id, moments in self._stimuli().items():
            mine = [t.t_ms for t in self._touches
                    if t.source == source and t.skin_id == skin_id]
            for index, (at_ms, _label) in enumerate(moments):
                until = (moments[index + 1][0] if index + 1 < len(moments)
                         else float("inf"))
                until = min(until, at_ms + self._params.reaction_max_ms)
                first = next((t for t in mine if at_ms <= t < until), None)
                if first is not None:
                    latencies.append(first - at_ms)
        return latencies

    def _observed(self, source: str) -> dict[str, int]:
        if self._source_key != SOURCE_PARTICIPANT:
            return {}
        return self._count(
            event.action for event in self._events
            if event.type == "observer" and event.participant_id == source)

    def _input_changes(self, skin_id: str, start_ms: float,
                       end_ms: float) -> dict[str, int]:
        return self._count(
            event.action for event in self._events
            if event.type == "input" and _skin_of(event.target) == skin_id
            and start_ms <= self._ms(event.timestamp) < end_ms)

    def _pair_row(self, a: str, touches_a: list[_Touch],
                  b: str, touches_b: list[_Touch]) -> dict[str, Any]:
        times_a = [t.t_ms for t in touches_a]
        times_b = [t.t_ms for t in touches_b]
        lags = []                    # b - a, for each touch of A with a B nearby
        for at in times_a:
            nearest = min(times_b, key=lambda bt: abs(bt - at), default=None)
            if nearest is not None and abs(nearest - at) <= self._params.together_ms:
                lags.append(nearest - at)
        decided = [lag for lag in lags if lag != 0]
        peak, lag_ms = self._xcorr(times_a, times_b)
        fewer = min(len(times_a), len(times_b))
        return {
            "session_id": self._session_id,
            "source_a": a,
            "source_b": b,
            "together": len(lags),
            "together_share": _round(len(lags) / fewer, 3) if fewer else None,
            "median_abs_lag_ms": _round(
                statistics.median(abs(lag) for lag in lags) if lags else None, 0),
            "a_leads_share": _round(
                sum(1 for lag in decided if lag > 0) / len(decided), 3)
            if decided else None,
            "xcorr_peak": _round(peak, 3),
            "xcorr_lag_ms": _round(lag_ms, 0),
        }

    def _xcorr(self, times_a: list[float],
               times_b: list[float]) -> tuple[float | None, float | None]:
        """Peak normalised cross-correlation of two onset series and its lag
        (positive = B follows A). ``(None, None)`` when either is empty."""
        if not times_a or not times_b:
            return None, None
        width = self._params.xcorr_bin_ms
        bins_a = self._count_bins(times_a, width)
        bins_b = self._count_bins(times_b, width)
        norm = (sum(v * v for v in bins_a.values())
                * sum(v * v for v in bins_b.values())) ** 0.5
        if norm <= 0:
            return None, None
        max_lag = int(self._params.xcorr_max_lag_ms // width)
        best_value, best_lag = -1.0, 0
        for lag in range(-max_lag, max_lag + 1):
            value = sum(count * bins_b.get(index + lag, 0)
                        for index, count in bins_a.items()) / norm
            if value > best_value or (value == best_value
                                      and abs(lag) < abs(best_lag)):
                best_value, best_lag = value, lag
        return best_value, best_lag * width

    @staticmethod
    def _count_bins(times_ms: list[float], width: float) -> dict[int, int]:
        bins: dict[int, int] = {}
        for moment in times_ms:
            index = int(moment // width)
            bins[index] = bins.get(index, 0) + 1
        return bins


def combine(tables: list[list[Table]]) -> list[Table]:
    """Stack the same-named tables of several sessions into one each."""
    combined: dict[str, Table] = {}
    for session_tables in tables:
        for table in session_tables:
            if table.name not in combined:
                combined[table.name] = Table(table.name, table.columns, [])
            combined[table.name].rows.extend(table.rows)
    return list(combined.values())
