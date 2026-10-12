# Session data: what is recorded and how to analyse it

Written for whoever analyses the sessions. It covers what the app stores, the
Data tab, the Excel export and how the derived measures are defined.

## 1. Where the data lives

| What | Where | Notes |
|---|---|---|
| Sessions, participants, events, reviews | `data/softedibo.db` (SQLite) under the app's data folder | The source of truth |
| Raw sensor streams | `data/recordings/<session>.jsonl` | Only when "Record sensor streams" is ticked at session start |
| `softedibo.log` | The app's state folder | A rotating debug trace, NOT a data record |

Raw events are never rewritten. Reviews and corrections are stored next to
them and applied on top, so the original record can always be recovered.

## 2. Events (table `events`)

Every row has `session_id, participant_id, type, action, target, timestamp,
metadata`. `timestamp` is wall-clock with millisecond precision; `metadata` is
JSON. `target` is `<skin>:<chamber>` for touches and `<robot>/<skin>` (the
"unit") for everything the behaviour engine logs.

| type | action | metadata |
|---|---|---|
| `session` | `start`, `pause`, `resume`, `stop`, `next_phase`, `previous_phase`, `emergency_stop` | `start`: robots, robot kinds, simulation flag |
| `touch` | `press` | `sensor_id`, `intensity_ut`, `touch_interval_ms`, `frequency_hz` |
| `touch` | `release` | `duration_ms`, `press_timestamp`, `sensor_id`, `press_intensity_ut`, `release_intensity_ut`, `peak_intensity_ut` |
| `gesture` | the touch type (`tap`, `press`, `rest`, `compressions`, `pats`, `stroke`, `rub`, `hit`) | the four axes (`form`, `movement`, `force`, `extent`) and the descriptors behind them |
| `activity` | `state` | `from`, `to`, `in_state_ms`, `final`, `trigger` (`source`: start / condition / step / manual, plus the `condition` that fired) |
| `robot` | the action the robot ran (`set_led`, `inflate`, ...) | `cause` (`phase` = the robot's own initiative, anything else = answering that event) and `params` |
| `input` | name of a discrete input (`button`, `cover`, `organs`, `impact`, `lifted`) | `value`; logged on change only |
| `value` | name of a named value (e.g. `score`) | `value`; logged on change only |
| `sync` | `round_accepted`, `round_off_cadence`, `round_rejected` (group sync condition); `round_good`, `round_off_cadence`, `round_missed` (group round detector) | round details |
| `observer` | the coded behaviour (`watches`, `helps`, ...) | - |
| `gesture_label` | a touch type tagged by the observer (a training label) | - |
| `marker` | `mark` | free text |

Touch times are those of the sensor sample itself when the node firmware stamps
its frames (`magtime-1` and later), otherwise of the frame's arrival at the PC.

## 3. What a session ran with (table `session_snapshots`)

Captured at session start, one JSON payload per kind: `activity` (name and the
full definition), `robots` (the robots and skins as configured), `touch`
(thresholds and tuning) and `firmware` (the marker each board reported).
Sessions are only comparable when these match.

## 4. Your review (tables `session_annotations`, `event_corrections`)

- Per session: status (`valid` / `test` / `excluded`), group label, order in
  the visit and notes. The group label is the key for joining with the
  questionnaires, together with the participant code.
- Per event: attribute it to another participant, or exclude it, with a note.

## 5. The Data tab

- **Left**: sessions grouped by group of children, with a text filter and a
  status filter. Select several to export or to compare.
- **Group touches by**: who a touch is attributed to. *Participant* (the child
  assigned to the skin), *Skin*, or *Sensor* (for a skin several children
  share, where the sensor position is the only way to tell them apart).
- **Summary**: the session's facts, your review, and the recorded conditions.
- **Timeline**: phases, one lane of touches per child, robot actions, inputs
  and observer codes on a common time axis. Wheel to zoom, drag to move, hover
  to read.
- **Children**: one row per child, and one per pair of children.
- **Events**: every event, with corrections.
- **Patterns**: tables pooled over ALL selected sessions, per condition.

## 6. The Excel export

"Export for Analysis..." writes one workbook. Every column is explained in its
last sheet, **Dictionary**.

| Sheet | One row per |
|---|---|
| Sessions | session: design factors and outcome |
| Milestones | phase change: what triggered it and what it took |
| Participants | participant in a session |
| Touches | touch (press paired with its release) |
| Profiles | child in a session |
| Pairs | pair of children in a session |
| Events | raw event, with any correction marked |
| Episodes | bout of continuous touching |
| Turns | condition x who acts next (same child / another) |
| Behaviours | condition x step from one behaviour to the next |
| Reactions | condition x robot action |
| Styles | touching style found by clustering (needs the optional ML packages) |

## 7. How the measures are defined

- **Touch**: a `press` paired with the next `release` of the same sensor.
- **Interval**: time between consecutive touches of the same source.
- **Tempo**: 60000 / median interval, ignoring pauses longer than 3 s.
- **Regularity** (`interval_cv`): standard deviation / mean of those intervals.
  0 is a perfectly even rhythm.
- **Reaction**: time from the last robot-initiated moment on that skin (a phase
  change or an action run by the phase program) to the touch. Robot actions
  that answer a touch are not stimuli.
- **Together**: two touches of different sources within 300 ms.
- **A leads**: share of the "together" touches where the first source of the
  pair was first.
- **Cross-correlation**: the touch onsets of two sources in 100 ms bins,
  compared at lags up to 2 s. The peak is the similarity (1 = same pattern)
  and its lag says how far the second source trails the first.
- **Surface**: `shared` (several children on one skin), `individual_shared`
  (own skin each on one robot), `individual_separate` (own robot each).
- **Finished**: every unit reached a phase marked `final` in the activity.
- **Episode**: a source's touches with no pause longer than 2 s.

The windows are parameters of `src/data/interaction_model.py` (`ModelParams`).

## 8. Limits to keep in mind

- On a skin shared by several children every touch carries the participant
  assigned to the skin. Group by sensor to separate them.
- The Thymio skin samples at 10 Hz at rest and 20 Hz during a touch, so touch
  times there are good to about 50-100 ms.
- With a handful of groups, the pooled tables and the styles are exploratory.
  Each row states how many sessions or children it rests on.
