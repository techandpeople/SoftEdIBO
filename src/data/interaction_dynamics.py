"""Interaction dynamics across sessions: episodes, turn-taking, reactions.

Builds on the per-touch table of :mod:`src.data.interaction_model`:

- **episodes**    a source's touches grouped into bouts of continuous touching
- **turns**       who acts after whom (a Markov transition table per condition)
- **behaviours**  what a child does after what (touching and the observer's
  codes), as transitions per condition
- **reactions**   how fast children respond to each robot action, per condition

"Condition" is the session's design cell: robot kinds + how the children and
the touch surfaces relate. Rows are pooled over every session given, so the
tables compare conditions; each row says how many sessions it rests on.

No GUI, no database access, no numpy.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any

from src.data.interaction_model import (
    SOURCE_PARTICIPANT,
    Column,
    InteractionModel,
    ModelParams,
    Table,
)
from src.data.session_data import SessionData

# A source's touches further apart than this belong to different episodes.
EPISODE_GAP_MS = 2000.0
# Behaviour label of a touching episode in the behaviour transitions.
TOUCHING = "touching"

EPISODE_COLUMNS = (
    Column("session_id", "Session."),
    Column("condition", "Robot kinds and surface arrangement of the session."),
    Column("source", "Who the episode belongs to."),
    Column("episode", "Index of the episode within the source's session.", "count"),
    Column("t_ms", "Start of the episode since the session started.", "ms"),
    Column("duration_ms", "From the first press to the last release.", "ms"),
    Column("touches", "Touches in the episode.", "count"),
    Column("tempo_bpm", "Touch tempo inside the episode.", "1/min"),
    Column("interval_cv", "Rhythm regularity inside the episode "
           "(0 = perfectly even).", "ratio"),
    Column("mean_touch_ms", "Mean duration of the touches.", "ms"),
    Column("mean_peak_ut", "Mean of the touches' strongest magnitude.", "uT"),
    Column("together_share", "Share of the touches made together with another "
           "source.", "ratio"),
    Column("reaction_ms", "Time from the last robot action to the episode's "
           "first touch.", "ms"),
    Column("gesture", "Most frequent touch type in the episode."),
    Column("state", "Activity phase the episode started in."),
    Column("style", "Touching style the episode was grouped into (see the "
           "Styles sheet); empty when styles were not computed.", "count"),
)

TURN_COLUMNS = (
    Column("condition", "Robot kinds and surface arrangement."),
    Column("pattern", "self = the same source acted again; other = another "
           "source took over."),
    Column("count", "Times this happened.", "count"),
    Column("probability", "Share of all episode-to-episode steps.", "ratio"),
    Column("median_gap_ms", "Median pause before the next episode.", "ms"),
    Column("sessions", "Sessions the row rests on.", "count"),
)

BEHAVIOUR_COLUMNS = (
    Column("condition", "Robot kinds and surface arrangement."),
    Column("from_behaviour", "What the child was doing."),
    Column("to_behaviour", "What the same child did next."),
    Column("count", "Times this happened.", "count"),
    Column("probability", "Share of the steps leaving 'from_behaviour'.", "ratio"),
    Column("participants", "Children the row rests on.", "count"),
)

REACTION_COLUMNS = (
    Column("condition", "Robot kinds and surface arrangement."),
    Column("robot_action", "Robot-initiated action (or phase change) the "
           "children responded to."),
    Column("reactions", "First touches after that action.", "count"),
    Column("median_ms", "Median time to the first touch.", "ms"),
    Column("q1_ms", "Lower quartile.", "ms"),
    Column("q3_ms", "Upper quartile.", "ms"),
    Column("sessions", "Sessions the row rests on.", "count"),
)


@dataclass
class _Episode:
    session_id: str
    condition: str
    source: str
    touches: list[dict[str, Any]] = field(default_factory=list)

    @property
    def start_ms(self) -> float:
        return float(self.touches[0]["t_ms"])

    @property
    def end_ms(self) -> float:
        last = self.touches[-1]
        return float(last["t_ms"]) + float(last["duration_ms"] or 0.0)


def condition_of(model: InteractionModel) -> str:
    """The session's design cell, e.g. ``"thymio / individual_separate"``."""
    row = model.session().rows[0]
    parts = [str(row["robot_kinds"]), str(row["surface"])]
    return " / ".join(part for part in parts if part) or "unknown"


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def _quartiles(values: list[float]) -> tuple[float, float, float]:
    ordered = sorted(values)
    if len(ordered) < 2:
        return ordered[0], ordered[0], ordered[0]
    q1, median, q3 = statistics.quantiles(ordered, n=4, method="inclusive")
    return q1, median, q3


class InteractionDynamics:
    """Pools several sessions into the cross-session tables.

    Args:
        sessions: The sessions to pool.
        source_key: How touches are grouped into sources.
        params: Windows of the per-touch measures.
        episode_gap_ms: Pause that ends an episode.
    """

    def __init__(self, sessions: list[SessionData],
                 source_key: str = SOURCE_PARTICIPANT,
                 params: ModelParams | None = None,
                 episode_gap_ms: float = EPISODE_GAP_MS) -> None:
        self._params = params or ModelParams()
        self._gap_ms = float(episode_gap_ms)
        self._models = [(data, InteractionModel(data, source_key, self._params))
                        for data in sessions]
        self._episodes = [episode for data, model in self._models
                          for episode in self._session_episodes(data, model)]

    # ------------------------------------------------------------------
    # Episodes
    # ------------------------------------------------------------------

    def _session_episodes(self, data: SessionData,
                          model: InteractionModel) -> list[_Episode]:
        condition = condition_of(model)
        open_by_source: dict[str, _Episode] = {}
        episodes: list[_Episode] = []
        for touch in model.touches().rows:
            current = open_by_source.get(touch["source"])
            if current is None or touch["t_ms"] - current.end_ms > self._gap_ms:
                current = _Episode(data.session.session_id, condition,
                                   touch["source"])
                open_by_source[touch["source"]] = current
                episodes.append(current)
            current.touches.append(touch)
        return sorted(episodes, key=lambda e: e.start_ms)

    def episodes(self) -> Table:
        rows = []
        counters: dict[tuple[str, str], int] = {}
        for episode in self._episodes:
            key = (episode.session_id, episode.source)
            counters[key] = counters.get(key, 0) + 1
            rows.append(self._episode_row(episode, counters[key]))
        return Table("Episodes", EPISODE_COLUMNS, rows)

    def _episode_row(self, episode: _Episode, index: int) -> dict[str, Any]:
        touches = episode.touches
        starts = [float(t["t_ms"]) for t in touches]
        intervals = [b - a for a, b in zip(starts, starts[1:])]
        mean_interval = _mean(intervals)
        cv = (statistics.pstdev(intervals) / mean_interval
              if len(intervals) >= 2 and mean_interval else None)
        together = sum(
            1 for t in touches if t["nearest_other_lag_ms"] is not None
            and abs(t["nearest_other_lag_ms"]) <= self._params.together_ms)
        gestures = [t["gesture"] for t in touches if t["gesture"]]
        return {
            "session_id": episode.session_id,
            "condition": episode.condition,
            "source": episode.source,
            "episode": index,
            "t_ms": round(episode.start_ms),
            "duration_ms": round(episode.end_ms - episode.start_ms),
            "touches": len(touches),
            "tempo_bpm": _round(60000.0 / mean_interval) if mean_interval else None,
            "interval_cv": _round(cv, 3),
            "mean_touch_ms": _round(_mean(
                [float(t["duration_ms"]) for t in touches
                 if t["duration_ms"] is not None]), 0),
            "mean_peak_ut": _round(_mean(
                [float(t["peak_ut"]) for t in touches
                 if t["peak_ut"] is not None])),
            "together_share": round(together / len(touches), 3),
            "reaction_ms": touches[0]["since_robot_action_ms"],
            "gesture": max(set(gestures), key=gestures.count) if gestures else "",
            "state": touches[0]["state"],
            "style": None,
        }

    # ------------------------------------------------------------------
    # Turn-taking
    # ------------------------------------------------------------------

    def turns(self) -> Table:
        """Who acts after whom, pooled per condition."""
        gaps: dict[tuple[str, str], list[float]] = {}
        sessions: dict[tuple[str, str], set[str]] = {}
        by_session: dict[str, list[_Episode]] = {}
        for episode in self._episodes:
            by_session.setdefault(episode.session_id, []).append(episode)
        for session_id, episodes in by_session.items():
            for previous, following in zip(episodes, episodes[1:]):
                pattern = ("self" if following.source == previous.source
                           else "other")
                key = (previous.condition, pattern)
                gaps.setdefault(key, []).append(
                    max(0.0, following.start_ms - previous.end_ms))
                sessions.setdefault(key, set()).add(session_id)
        totals: dict[str, int] = {}
        for (condition, _pattern), values in gaps.items():
            totals[condition] = totals.get(condition, 0) + len(values)
        rows = [{
            "condition": condition,
            "pattern": pattern,
            "count": len(values),
            "probability": round(len(values) / totals[condition], 3),
            "median_gap_ms": round(statistics.median(values)),
            "sessions": len(sessions[(condition, pattern)]),
        } for (condition, pattern), values in sorted(gaps.items())]
        return Table("Turns", TURN_COLUMNS, rows)

    # ------------------------------------------------------------------
    # Behaviour transitions
    # ------------------------------------------------------------------

    def behaviours(self) -> Table:
        """What each child does after what: touching episodes interleaved
        with the observer's codes, pooled per condition."""
        counts: dict[tuple[str, str, str], int] = {}
        children: dict[tuple[str, str, str], set[tuple[str, str]]] = {}
        for data, model in self._models:
            condition = condition_of(model)
            for participant, sequence in self._behaviour_sequences(
                    data, model).items():
                for current, following in zip(sequence, sequence[1:]):
                    key = (condition, current, following)
                    counts[key] = counts.get(key, 0) + 1
                    children.setdefault(key, set()).add(
                        (data.session.session_id, participant))
        leaving: dict[tuple[str, str], int] = {}
        for (condition, current, _following), count in counts.items():
            leaving[(condition, current)] = (
                leaving.get((condition, current), 0) + count)
        rows = [{
            "condition": condition,
            "from_behaviour": current,
            "to_behaviour": following,
            "count": count,
            "probability": round(count / leaving[(condition, current)], 3),
            "participants": len(children[(condition, current, following)]),
        } for (condition, current, following), count in sorted(counts.items())]
        return Table("Behaviours", BEHAVIOUR_COLUMNS, rows)

    def _behaviour_sequences(self, data: SessionData,
                             model: InteractionModel) -> dict[str, list[str]]:
        """Each participant's behaviours in time order (repeats collapsed)."""
        start = data.session.start_time
        timed: dict[str, list[tuple[float, str]]] = {}
        for episode in self._session_episodes(data, model):
            participant = episode.touches[0]["participant_id"]
            if participant:
                timed.setdefault(participant, []).append(
                    (episode.start_ms, TOUCHING))
        for event in data.corrected_events():
            if event.type == "observer" and event.participant_id:
                timed.setdefault(event.participant_id, []).append(
                    ((event.timestamp - start).total_seconds() * 1000.0,
                     event.action))
        sequences: dict[str, list[str]] = {}
        for participant, moments in timed.items():
            sequence: list[str] = []
            for _t_ms, behaviour in sorted(moments):
                if not sequence or sequence[-1] != behaviour:
                    sequence.append(behaviour)
            sequences[participant] = sequence
        return sequences

    # ------------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------------

    def reactions(self) -> Table:
        """Time to the first touch after each robot action, per condition."""
        latencies: dict[tuple[str, str], list[float]] = {}
        sessions: dict[tuple[str, str], set[str]] = {}
        for data, model in self._models:
            condition = condition_of(model)
            # The first touch after each stimulus: the stimulus it follows is
            # named on the touch, and its latency is the smallest of the run.
            seen: set[tuple[str, str, float]] = set()
            for touch in model.touches().rows:
                latency = touch["since_robot_action_ms"]
                if latency is None or latency > self._params.reaction_max_ms:
                    continue
                stimulus = (touch["skin_id"], touch["source"],
                            float(touch["t_ms"]) - float(latency))
                if stimulus in seen:
                    continue
                seen.add(stimulus)
                key = (condition, touch["last_robot_action"])
                latencies.setdefault(key, []).append(float(latency))
                sessions.setdefault(key, set()).add(data.session.session_id)
        rows = []
        for (condition, action), values in sorted(latencies.items()):
            q1, median, q3 = _quartiles(values)
            rows.append({
                "condition": condition, "robot_action": action,
                "reactions": len(values), "median_ms": round(median),
                "q1_ms": round(q1), "q3_ms": round(q3),
                "sessions": len(sessions[(condition, action)]),
            })
        return Table("Reactions", REACTION_COLUMNS, rows)

    def tables(self) -> list[Table]:
        return [self.episodes(), self.turns(), self.behaviours(),
                self.reactions()]
