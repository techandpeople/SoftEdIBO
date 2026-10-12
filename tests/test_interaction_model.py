"""InteractionModel: tables derived from one session's events."""

import json
from datetime import datetime, timedelta

import pytest

from src.data import interaction_model as im
from src.data.models import (
    EventCorrection,
    InteractionEvent,
    ParticipantRecord,
    SessionAnnotation,
    SessionAssignment,
    SessionRecord,
)
from src.data.session_data import SessionData

START = datetime(2026, 10, 12, 10, 0, 0)


class _Builder:
    """Builds a session's event list with times in ms since the start."""

    def __init__(self):
        self.events: list[InteractionEvent] = []

    def add(self, t_ms, type_, action, target="", participant="system", **meta):
        self.events.append(InteractionEvent(
            event_id=len(self.events) + 1, session_id="S1",
            participant_id=participant, type=type_, action=action,
            target=target, timestamp=START + timedelta(milliseconds=t_ms),
            metadata=json.dumps(meta) if meta else ""))
        return self

    def touch(self, t_ms, skin, participant, duration=100, sensor=0, peak=300.0):
        self.add(t_ms, "touch", "press", f"{skin}:0", participant,
                 sensor_id=sensor, intensity_ut=150.0)
        self.add(t_ms + duration, "touch", "release", f"{skin}:0", participant,
                 sensor_id=sensor, duration_ms=duration,
                 peak_intensity_ut=peak, release_intensity_ut=40.0)
        return self

    def state(self, t_ms, unit, to, source="condition", final=False,
              in_state_ms=None, frm="", condition=None):
        trigger = {"source": source}
        if condition:
            trigger["condition"] = condition
        meta = {"from": frm, "to": to, "final": final, "trigger": trigger}
        if in_state_ms is not None:
            meta["in_state_ms"] = in_state_ms
        return self.add(t_ms, "activity", "state", unit, **meta)


def _data(builder, assignments, duration_ms=60000, **extra):
    return SessionData(
        session=SessionRecord("S1", "Any activity", START,
                              START + timedelta(milliseconds=duration_ms)),
        events=builder.events,
        assignments=assignments,
        participants=[ParticipantRecord(a.participant_id, a.participant_id)
                      for a in assignments],
        **extra)


def _two_robots():
    return [SessionAssignment("S1", "thymio-1", "P1", ["skin-1"]),
            SessionAssignment("S1", "thymio-2", "P2", ["skin-2"])]


def _row(table, **match):
    [row] = [r for r in table.rows
             if all(r[k] == v for k, v in match.items())]
    return row


def test_touch_row_pairs_press_and_release():
    b = _Builder().touch(1000, "skin-1", "P1", duration=250, peak=420.0)
    [row] = im.InteractionModel(_data(b, _two_robots())).touches().rows
    assert (row["source"], row["robot_id"], row["skin_id"]) == (
        "P1", "thymio-1", "skin-1")
    assert (row["t_ms"], row["duration_ms"]) == (1000, 250)
    assert (row["press_ut"], row["peak_ut"], row["release_ut"]) == (
        150.0, 420.0, 40.0)
    assert row["time"] == "2026-10-12T10:00:01.000"


def test_profile_reports_tempo_and_regularity():
    b = _Builder()
    for t in range(0, 5000, 500):            # steady 120 per minute
        b.touch(t, "skin-1", "P1")
    for t in (0, 300, 1100, 1300, 2600):     # uneven
        b.touch(t, "skin-2", "P2")
    profiles = im.InteractionModel(_data(b, _two_robots())).profiles()
    steady, uneven = _row(profiles, source="P1"), _row(profiles, source="P2")
    assert (steady["touches"], steady["median_interval_ms"]) == (10, 500)
    assert steady["tempo_bpm"] == 120.0
    assert steady["interval_cv"] == 0.0
    assert uneven["interval_cv"] > 0.4
    assert steady["touches_per_min"] == 10.0


def test_pauses_are_not_part_of_the_rhythm():
    b = _Builder()
    for t in (0, 500, 1000, 20000, 20500):   # a long pause in the middle
        b.touch(t, "skin-1", "P1")
    profile = im.InteractionModel(_data(b, _two_robots())).profiles().rows[0]
    assert (profile["rhythm_intervals"], profile["median_interval_ms"]) == (3, 500)


def test_pair_shows_who_leads_and_by_how_much():
    b = _Builder()
    for t in range(1000, 6000, 1000):
        b.touch(t, "skin-1", "P1")           # P1 leads
        b.touch(t + 200, "skin-2", "P2")     # P2 follows 200 ms later
    model = im.InteractionModel(_data(b, _two_robots()))
    [pair] = model.pairs().rows
    assert (pair["source_a"], pair["source_b"]) == ("P1", "P2")
    assert (pair["together"], pair["together_share"]) == (5, 1.0)
    assert pair["median_abs_lag_ms"] == 200
    assert pair["a_leads_share"] == 1.0
    assert pair["xcorr_lag_ms"] == 200
    assert pair["xcorr_peak"] == pytest.approx(1.0)
    follower = _row(model.touches(), source="P2", t_ms=1200)
    assert (follower["nearest_other_source"], follower["nearest_other_lag_ms"]) == (
        "P1", 200)


def test_unrelated_touching_is_not_counted_as_together():
    b = _Builder()
    for t in (1000, 2000, 3000):
        b.touch(t, "skin-1", "P1")
    for t in (1500, 2500, 9000):
        b.touch(t, "skin-2", "P2")
    [pair] = im.InteractionModel(_data(b, _two_robots())).pairs().rows
    assert pair["together"] == 0 and pair["a_leads_share"] is None


def test_reaction_is_timed_from_robot_initiated_actions_only():
    b = _Builder()
    b.state(0, "thymio-1/skin-1", "phase1", source="start")
    b.add(5000, "robot", "inflate", "thymio-1/skin-1", cause="phase", params={})
    b.touch(5800, "skin-1", "P1")
    b.add(5900, "robot", "set_led", "thymio-1/skin-1", cause="touch", params={})
    b.touch(6400, "skin-1", "P1")
    model = im.InteractionModel(_data(b, _two_robots()))
    first, second = model.touches().rows
    assert (first["last_robot_action"], first["since_robot_action_ms"]) == (
        "inflate", 800)
    # The LED answering the touch is not a new stimulus.
    assert (second["last_robot_action"], second["since_robot_action_ms"]) == (
        "inflate", 1400)
    assert (first["state"], first["since_state_ms"]) == ("phase1", 5800)
    profile = model.profiles().rows[0]
    assert (profile["reactions"], profile["median_reaction_ms"]) == (1, 800)


def test_milestones_say_what_fired_and_what_it_took():
    cond = {"touch_count": {"min": 2}}
    b = _Builder()
    b.state(0, "thymio-1/skin-1", "phase1", source="start")
    b.touch(1000, "skin-1", "P1").touch(2000, "skin-1", "P1")
    b.add(2500, "input", "button", "thymio-1/skin-1", value=True)
    b.state(3000, "thymio-1/skin-1", "done", final=True, in_state_ms=3000,
            frm="phase1", condition=cond)
    model = im.InteractionModel(_data(b, _two_robots()))
    milestone = _row(model.milestones(), to_state="done")
    assert (milestone["trigger"], milestone["in_state_ms"]) == ("condition", 3000)
    assert json.loads(milestone["condition"]) == cond
    assert milestone["touches"] == 2
    assert json.loads(milestone["touches_by_source"]) == {"P1": 2}
    assert json.loads(milestone["inputs"]) == {"button": 1}
    [session] = model.session().rows
    assert (session["units"], session["units_finished"]) == (1, 1)
    assert (session["time_to_finish_ms"], session["milestones"]) == (3000, 1)


def test_finish_needs_every_unit_and_reads_final_from_the_snapshot():
    spec = {"initial": "a", "states": {"a": {}, "end": {"final": True}}}
    b = _Builder()
    b.add(0, "activity", "state", "r/skin-1", **{"from": "", "to": "a"})
    b.add(0, "activity", "state", "r/skin-2", **{"from": "", "to": "a"})
    b.add(4000, "activity", "state", "r/skin-1", **{"from": "a", "to": "end"})
    data = _data(b, _two_robots(), snapshots={"activity": {"spec": spec}})
    [session] = im.InteractionModel(data).session().rows
    assert (session["units"], session["units_finished"]) == (2, 1)
    assert session["time_to_finish_ms"] is None
    # Old logs carry no "in_state_ms": it is derived from the previous change.
    assert _row(im.InteractionModel(data).milestones(),
                to_state="end")["in_state_ms"] == 4000


@pytest.mark.parametrize("assignments, expected", [
    ([SessionAssignment("S1", "turtle-1", p, ["belly"]) for p in ("P1", "P2")],
     im.SURFACE_SHARED),
    ([SessionAssignment("S1", "tree-1", "P1", ["b1"]),
      SessionAssignment("S1", "tree-1", "P2", ["b2"])],
     im.SURFACE_INDIVIDUAL_SHARED),
    (_two_robots(), im.SURFACE_INDIVIDUAL_SEPARATE),
    ([], ""),
])
def test_surface_is_derived_from_the_assignments(assignments, expected):
    assert im.InteractionModel(_data(_Builder(), assignments)).surface() == expected


def test_shared_skin_can_be_grouped_by_sensor():
    shared = [SessionAssignment("S1", "turtle-1", "P1", ["belly"])]
    b = _Builder()
    b.touch(1000, "belly", "P1", sensor=0).touch(1100, "belly", "P1", sensor=2)
    by_sensor = im.InteractionModel(_data(b, shared), im.SOURCE_SENSOR)
    assert [r["source"] for r in by_sensor.profiles().rows] == [
        "belly#0", "belly#2"]
    assert len(by_sensor.pairs().rows) == 1
    by_child = im.InteractionModel(_data(b, shared))
    assert [r["source"] for r in by_child.profiles().rows] == ["P1"]


def test_corrections_are_applied_without_touching_the_events():
    b = _Builder().touch(1000, "skin-1", "P1").touch(2000, "skin-1", "P1")
    corrections = {
        1: EventCorrection(1, "S1", participant_id="P2"),   # first press
        3: EventCorrection(3, "S1", excluded=True),         # second press
    }
    data = _data(b, _two_robots(), corrections=corrections)
    [row] = im.InteractionModel(data).touches().rows
    assert row["source"] == "P2"
    assert b.events[0].participant_id == "P1"


def test_gesture_and_observer_counts_reach_the_profile():
    b = _Builder().touch(1000, "skin-1", "P1", duration=120)
    b.add(2400, "gesture", "tap", "thymio-1/skin-1", duration_ms=120,
          form="short", movement="static", force="light", extent="point")
    b.add(3000, "observer", "watches", "P1", participant="P1")
    model = im.InteractionModel(_data(b, _two_robots()))
    touch = model.touches().rows[0]
    assert touch["gesture"] == "tap"
    assert (touch["touch_form"], touch["touch_movement"], touch["touch_force"],
            touch["touch_extent"]) == ("short", "static", "light", "point")
    profile = model.profiles().rows[0]
    assert json.loads(profile["gestures"]) == {"tap": 1}
    assert json.loads(profile["observed"]) == {"watches": 1}


def test_session_row_carries_the_design_factors_and_annotation():
    b = _Builder().add(0, "session", "start", robot_kinds={
        "thymio-1": "thymio", "thymio-2": "thymio"})
    b.touch(1000, "skin-1", "P1")
    data = _data(b, _two_robots(), duration_ms=90000,
                 annotation=SessionAnnotation("S1", group_label="G2",
                                              visit_order=3, notes="ok"))
    [row] = im.InteractionModel(data).session().rows
    assert (row["group"], row["visit_order"], row["status"]) == ("G2", 3, "valid")
    assert (row["robot_kinds"], row["surface"]) == (
        "thymio", im.SURFACE_INDIVIDUAL_SEPARATE)
    assert (row["participants"], row["duration_s"], row["touches"]) == (2, 90.0, 1)


def test_every_row_key_is_a_declared_column():
    b = _Builder().touch(1000, "skin-1", "P1").touch(1200, "skin-2", "P2")
    b.state(0, "thymio-1/skin-1", "a", source="start")
    for table in im.InteractionModel(_data(b, _two_robots())).tables():
        assert table.rows, table.name
        for row in table.rows:
            assert list(row) == table.headers, table.name


def test_combine_stacks_sessions_per_table():
    b = _Builder().touch(1000, "skin-1", "P1")
    one = im.InteractionModel(_data(b, _two_robots())).tables()
    stacked = {t.name: t for t in im.combine([one, one])}
    assert len(stacked["Touches"].rows) == 2 and len(stacked["Sessions"].rows) == 2


def test_unknown_source_key_is_rejected():
    with pytest.raises(ValueError):
        im.InteractionModel(_data(_Builder(), []), "robot")
