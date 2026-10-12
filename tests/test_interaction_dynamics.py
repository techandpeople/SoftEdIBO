"""Cross-session dynamics: episodes, turn-taking, behaviours, reactions, styles."""

import json
from datetime import datetime, timedelta

import pytest

from src.data.interaction_dynamics import TOUCHING, InteractionDynamics
from src.data.models import (
    InteractionEvent,
    ParticipantRecord,
    SessionAssignment,
    SessionRecord,
)
from src.data.session_data import SessionData

START = datetime(2026, 10, 12, 10, 0, 0)


def _session(session_id, touches, extra=(), kinds="thymio"):
    """``touches`` = (t_ms, participant); each child has their own robot."""
    events = []

    def add(t_ms, type_, action, target="", participant="system", **meta):
        events.append(InteractionEvent(
            event_id=len(events) + 1, session_id=session_id,
            participant_id=participant, type=type_, action=action, target=target,
            timestamp=START + timedelta(milliseconds=t_ms),
            metadata=json.dumps(meta) if meta else ""))

    add(0, "session", "start", robot_kinds={"r1": kinds, "r2": kinds})
    for t_ms, participant in touches:
        skin = "skin-1" if participant == "P1" else "skin-2"
        add(t_ms, "touch", "press", f"{skin}:0", participant, sensor_id=0)
        add(t_ms + 100, "touch", "release", f"{skin}:0", participant,
            sensor_id=0, duration_ms=100, peak_intensity_ut=300.0)
    for args, meta in extra:
        add(*args, **meta)
    return SessionData(
        session=SessionRecord(session_id, "A", START,
                              START + timedelta(seconds=60)),
        events=events,
        assignments=[SessionAssignment(session_id, "r1", "P1", ["skin-1"]),
                     SessionAssignment(session_id, "r2", "P2", ["skin-2"])],
        participants=[ParticipantRecord("P1", "a"), ParticipantRecord("P2", "b")])


def _rows(table, **match):
    return [r for r in table.rows if all(r[k] == v for k, v in match.items())]


def test_touches_are_grouped_into_episodes_by_pauses():
    touches = [(t, "P1") for t in (0, 500, 1000)] + [(10000, "P1"), (10400, "P1")]
    episodes = InteractionDynamics([_session("S1", touches)]).episodes()
    first, second = episodes.rows
    assert (first["touches"], first["duration_ms"], first["tempo_bpm"]) == (
        3, 1100, 120.0)
    assert first["interval_cv"] == 0.0
    assert (second["episode"], second["touches"], second["t_ms"]) == (2, 2, 10000)
    assert first["condition"] == "thymio / individual_separate"


def test_turns_tell_alternating_from_solo_play():
    alternating = [(0, "P1"), (5000, "P2"), (10000, "P1"), (15000, "P2")]
    solo = [(0, "P1"), (5000, "P1"), (10000, "P1"), (15000, "P2")]
    turns = InteractionDynamics([_session("S1", alternating)]).turns()
    [other] = turns.rows
    assert (other["pattern"], other["count"], other["probability"]) == (
        "other", 3, 1.0)
    assert other["median_gap_ms"] == 4900
    turns = InteractionDynamics([_session("S2", solo)]).turns()
    assert [(r["pattern"], r["count"]) for r in turns.rows] == [
        ("other", 1), ("self", 2)]


def test_turns_pool_sessions_of_the_same_condition():
    a = _session("S1", [(0, "P1"), (5000, "P2")])
    b = _session("S2", [(0, "P1"), (5000, "P2")])
    [row] = InteractionDynamics([a, b]).turns().rows
    assert (row["count"], row["sessions"]) == (2, 2)


def test_behaviour_transitions_mix_touching_with_observer_codes():
    extra = [((3000, "observer", "watches", "P1", "P1"), {}),
             ((8000, "observer", "helps", "P1", "P1"), {})]
    data = _session("S1", [(0, "P1"), (5000, "P1"), (12000, "P1")], extra)
    table = InteractionDynamics([data]).behaviours()
    steps = {(r["from_behaviour"], r["to_behaviour"]): r for r in table.rows}
    assert set(steps) == {(TOUCHING, "watches"), ("watches", TOUCHING),
                          (TOUCHING, "helps"), ("helps", TOUCHING)}
    assert steps[(TOUCHING, "watches")]["probability"] == 0.5
    assert steps[("watches", TOUCHING)]["participants"] == 1


def test_reactions_count_only_the_first_touch_after_each_action():
    extra = [((1000, "robot", "inflate", "r1/skin-1"),
              {"cause": "phase", "params": {}}),
             ((20000, "robot", "inflate", "r1/skin-1"),
              {"cause": "phase", "params": {}})]
    touches = [(1600, "P1"), (2000, "P1"), (2400, "P1"), (21000, "P1")]
    [row] = InteractionDynamics([_session("S1", touches, extra)]).reactions().rows
    assert (row["robot_action"], row["reactions"]) == ("inflate", 2)
    assert (row["q1_ms"], row["median_ms"], row["q3_ms"]) == (700, 800, 900)


def test_styles_separate_fast_bursts_from_slow_presses():
    pytest.importorskip("sklearn")
    from src.ml.interaction_clusters import cluster_episodes
    touches = []
    for burst in range(6):                       # fast bursts by P1
        touches += [(burst * 10000 + i * 250, "P1") for i in range(6)]
    for slow in range(6):                        # slow pairs by P2
        touches += [(5000 + slow * 10000 + i * 1500, "P2") for i in range(2)]
    episodes = InteractionDynamics([_session("S1", touches)]).episodes()
    result = cluster_episodes(episodes)
    assert len(result.summary.rows) == 2
    by_source = {}
    for row, label in zip(episodes.rows, result.labels):
        by_source.setdefault(row["source"], set()).add(label)
    assert by_source["P1"] != by_source["P2"]
    assert all(len(labels) == 1 for labels in by_source.values())
    assert result.silhouette is not None and result.silhouette > 0.5
    assert {r["sources"] for r in result.summary.rows} == {1}


def test_styles_need_enough_episodes():
    pytest.importorskip("sklearn")
    from src.ml.interaction_clusters import cluster_episodes
    episodes = InteractionDynamics(
        [_session("S1", [(0, "P1"), (9000, "P1")])]).episodes()
    result = cluster_episodes(episodes)
    assert result.summary.rows == [] and "at least" in result.note
    assert result.labels == [None, None]
