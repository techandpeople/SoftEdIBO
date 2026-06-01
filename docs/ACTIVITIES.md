# Activities & Behaviors

Living document — describes the **current** Activity system (what's shipped)
and the **planned** behavior framework that adds tunable, persistable, multi-
state behaviors (Organ Swap, Breathing, etc.) on top of the existing
hardware.

The framework is delivered in phases:

| Phase | Goal | Status |
|-------|------|--------|
| 1a    | Documentation + design alignment | **done** |
| 1b    | DB-backed activity presets + tunable params + auto-generated GUI | **done** |
| 1c    | Organ Swap activity skeleton (mocked hardware) | **done** |
| 1d    | Firmware extensions: organ ADC + WS2818 LED + Python wrappers | planned |
| 1e    | Wire Organ Swap to real hardware end-to-end | planned |
| 2     | Declarative activities authored from the GUI (state-machine in DB) | planned |

---

## Current activity system (ships today)

### `BaseActivity`

`src/activities/base_activity.py` — every activity inherits from this:

```python
class BaseActivity:
    name: str
    description: str

    def prepare_robots(self, robots): ...   # optional: wrap robots (e.g. simulate)
    def _setup(self, session, robots): ...  # called by Session at start
    def start(self): ...
    def pause(self): ...
    def resume(self): ...
    def stop(self): ...
    def get_state(self): ...
```

Registered in `src/activities/__init__.py` (`ACTIVITIES = [...]`) and looked
up by `get_activity(name)` from `SessionPanel`.

### Shipped activities

| Activity | File | Behavior |
|----------|------|----------|
| Group Touch | `group_touch.py` | Originally the only "real" activity. Now also benefits from the shared `simulation_mode` flag — run it against `SimulatedRobot` for testing by ticking the box in the SessionSetupDialog. |

`SimulationActivity` (in `simulation_activity.py`) is intentionally **not**
registered any more. Simulation became a per-activity boolean — every
activity gets it for free via `BaseActivity.simulation_mode`. The class is
kept on disk as a one-line shim in case we want to expose a "pure
simulation" entry in the dropdown again.

### Where activities show up

- **SessionPanel** lets the user pick an activity + start/stop a session.
- **RobotMonitorPanel** renders `RobotMonitorWidget` per robot; each robot
  shows a `SkinWidget` per skin (with the new `SkinGridView` overlay when
  the skin has `chamber_grid` configured).
- Touch + inflate / deflate are exposed via `ChamberWidget` inside each
  `SkinWidget`.

---

## Planned: behavior framework

### Design choices (decided in the planning round)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Behavior format | **Hybrid: Python plugin + DB preset** | Python class hard-codes the state machine; the preset holds only tunable params (colors, timings, thresholds). Lets engineers add new activities by writing code, while educators tune existing ones in the GUI. |
| Organ sensing | Extension of `node_direct` and `node_multiplexed` | Reuses spare ADCs on existing boards — no new firmware target. |
| LED hardware (WS2818) | Same nodes (`node_direct` / `node_multiplexed`) | Same boards drive valves + lights + organ sensors. |
| Scope rollout | Incremental phases (1a → 1e) | Each phase ships standalone value. |

### State-machine pattern (per activity)

Each activity defines, in code, a **finite state machine**:

```
   ┌──────┐  all_organs_match  ┌───────┐
   │ sick │ ─────────────────► │ cured │
   └──────┘                    └───────┘
      │  on_periodic                │  on_periodic
      │  ─ pulse red LED            │  ─ breathe (slow inflate/deflate)
      │  ─ react to touch           │  ─ steady green LED
      └─────────────────────────────┘
```

Each state has up to three lifecycle hooks:

- `on_enter(activity)` — runs once when the state becomes active.
- `on_periodic(activity, dt_ms)` — runs at the activity's tick rate.
- `on_exit(activity)` — runs once before transitioning out.

Transitions are guarded by **conditions** evaluated each tick (or in
response to an event):

- `all_organs_match`, `touch_count >= N`, `timer >= T`, etc.

### Tunable parameters

Each Python activity declares its tunable params as a class attribute:

```python
class OrganSwapActivity(BaseActivity):
    PARAMS = [
        Param("sick_color",        "color", default="#ff0000"),
        Param("sick_pulse_ms",     "int",   default=1000, min=100, max=5000),
        Param("cured_color",       "color", default="#00ff00"),
        Param("breathe_period_ms", "int",   default=4000, min=500, max=10000),
        Param("breathe_depth_pct", "int",   default=60,   min=10,  max=100),
        Param("organ_resistance_map", "json",
              default={"liver_good": 1500, "heart_good": 2200, "lung_good": 3300}),
    ]
```

The GUI auto-generates a form from `PARAMS` (sliders, colour pickers, JSON
editor). Saved configurations live in the DB as **presets**, each with a
human-readable name. Multiple presets per activity are supported (so the
same Organ Swap can have a "1st grade easy", "2nd grade harder", etc.).

### DB schema (planned)

```
activity_presets
  preset_id TEXT PRIMARY KEY        -- "AP001", "AP002", ...
  activity_name TEXT NOT NULL       -- references ACTIVITIES registry
  name TEXT NOT NULL                -- "Easy", "Therapy v3", ...
  description TEXT
  params TEXT NOT NULL              -- JSON-encoded {param_name: value}
  created_at TEXT NOT NULL
  updated_at TEXT NOT NULL
```

### Worked example: Organ Swap

**Hardware setup (assumed)**:

- 1 silicone organ skin per robot, with 1 or 3 organ "slots". A pluggable
  organ is a silicone block with an internal resistor of a known value.
- All organ slots of the robot are wired **in parallel into ONE ADC pin**
  (we have limited GPIOs — one reading per robot/node, not per slot).
- The firmware measures the total resistance (via voltage divider) and
  broadcasts a single value per node. The PC decomposes that value via
  `1/Rtot = Σ 1/Ri` against a known organ catalogue to figure out which
  organs are currently plugged in.
- Whether the per-organ identity is reported, or only an aggregate "all
  organs are good" flag, is a configuration choice on the PC side (see
  `organ_readout_mode` below) — both modes work off the same firmware
  payload, so we don't have to commit to one shape today.

**Activity flow**:

1. State `sick` (default):
   - LED: red, pulses at `sick_pulse_ms`.
   - Robot reacts to touch: each press inflates the corresponding chamber
     briefly.
2. Transition condition: `all_organs_match` — every organ slot reads the
   resistance configured for the "good" organ in `organ_resistance_map`.
3. State `cured`:
   - LED: solid green.
   - Robot starts breathing: slow inflate/deflate cycle at
     `breathe_period_ms` with amplitude `breathe_depth_pct`.

All values above are editable in the GUI via the preset form.

---

## Firmware extensions (Phase 1d)

### Organ sensing

Both `node_direct` and `node_multiplexed` gain a single organ ADC input
(one pin per board variant, configurable at compile time). All organ slots
on that robot are wired in parallel into this pin.

The firmware:

- Samples the pin periodically (≥ 10 Hz; cheap relative to chamber sampling).
- Converts ADC reading → resistance via voltage-divider math:
  `R_total = R_known * V_out / (V_in - V_out)`.
- Broadcasts `{"type":"organ", "resistance_ohm":R}` whenever the value
  changes by more than a hysteresis threshold (no spam on noise; no
  per-slot info because there is none in the hardware).

**Parallel decomposition** runs on the **PC side**. The activity preset
holds a catalogue `{organ_id: resistance_ohm}`; for the measured total,
the PC enumerates plausible combinations and picks the one whose
parallel-equivalent resistance matches within tolerance.

Optionally the activity can ignore the decomposition entirely and just
check the total resistance against a "cured" target — useful when we only
need to know *all good* vs *not all good*, not which organ is missing.
The choice is exposed as a preset parameter (`organ_readout_mode`:
`per_organ` or `aggregate`).

### WS2818 LED control

Both firmwares accept new ESP-NOW commands:

| `cmd` | Fields | Notes |
|-------|--------|-------|
| `set_led` | `color` (`"#RRGGBB"`), `pattern` (`solid`/`pulse`/`breathe`/`rainbow`/`off`), `period_ms`, `count` (LEDs to light, default all) | Drives the WS2818 strip attached to the node. |

The pattern animations run inside the firmware loop — the PC just sets
intent. Saves ESP-NOW bandwidth and keeps animations smooth even if WiFi is
flaky.

### New Python wrappers (`ESP32Controller`)

- `set_led(color, pattern, period_ms=1000, count=None) -> bool`
- `on_organ(callback) -> None`  # callback signature: `(resistance_ohm: float)`

The `_handle_message` dispatcher routes `type=="organ"` messages to the
registered callbacks. There is one resistance reading per node (one ADC
input wired in parallel to all organ slots of that robot) — slot-level
decomposition lives on the PC side via the catalogue.

---

## GUI extensions (Phase 1b — shipped)

### Simulation mode is a per-activity checkbox

The activity dropdown in `SessionSetupDialog` no longer lists "Simulation"
as a separate activity. Instead, a `QCheckBox` labelled
**"Run in simulation mode (no real hardware)"** sits right below it. When
ticked, the chosen activity runs against `SimulatedRobot` instances backed
by `SimulatedController` — same monitor widgets, same flows, no ESP-NOW
traffic. Reads via `SessionSetupDialog.simulation_mode`; written to
`activity.simulation_mode` by `SessionPanel` before `prepare_robots` runs.

### Built-in `SIM_PARAMS`

Every activity inherits a baseline set of simulation knobs declared on
`BaseActivity.SIM_PARAMS`:

| Param | Type | Default | What it controls |
|-------|------|---------|------------------|
| `sim_inflate_speed_pct_per_s` | int | 33  | Simulated chamber fill rate |
| `sim_deflate_speed_pct_per_s` | int | 33  | Simulated chamber drain rate |
| `sim_touch_release_delay_ms`  | int | 300 | Delay after touch release before deflate ramp |

`BaseActivity.all_params()` returns `SIM_PARAMS + PARAMS` so the preset
editor renders them at the top of every activity's form. Subclasses just
add their own activity-specific `PARAMS`; simulation knobs come for free.

The values flow:
`activity.param_values → wrap_robots_in_simulation(sim_params) → SimulatedRobot → SimulatedController`,
where the controller converts the `%/s` values to per-tick step sizes on
its 100-ms internal timer.

### Preset editor dialog (`Tools => Activity Presets…`)

Implemented in
[`src/gui/activity_preset_dialog.py`](../src/gui/activity_preset_dialog.py).
Layout:

```
┌─────────────────────────────────────────────────────────┐
│ Activity:    [Group Touch ▾]                            │
│ Preset:      [Easy [AP001] ▾]  [+ New] [Delete]         │
│ Name:        [Easy______________________]               │
│ Description: [____________________________________]     │
│ ──────── Parameters ────────                            │
│ Sim inflate speed (%/s): [33__]                         │
│ Sim deflate speed (%/s): [33__]                         │
│ Sim touch release (ms):  [300_]                         │
│ (activity-specific params follow…)                      │
│                                                         │
│                                    [Save] [Close]       │
└─────────────────────────────────────────────────────────┘
```

Auto-generated form based on `activity.all_params()`:

| Param type | Widget |
|------------|--------|
| `int`      | `QSpinBox` (respects `min` / `max`) |
| `float`    | `QDoubleSpinBox` (respects `min` / `max`) |
| `bool`     | `QCheckBox` |
| `color`    | Coloured button → `QColorDialog` on click |
| `enum`     | `QComboBox` populated from `Param.choices` |
| `json`     | `QPlainTextEdit` with JSON validation on save |
| `str`      | `QLineEdit` |

Flow:

- Pick an activity → form is rebuilt for that activity's params; preset
  dropdown reloads.
- Pick a preset → form fills with that preset's values.
- **+ New** → resets to defaults so the operator can author a fresh preset
  without losing the saved ones.
- **Save** — upserts the current form into the DB. Empty `Name` is
  rejected; invalid JSON in a `json` param is flagged.
- **Delete** — confirms then drops the row from the DB.

### Persistence schema (DB)

```
activity_presets
  preset_id      TEXT PK   -- "AP001", "AP002", …
  activity_name  TEXT      -- "Group Touch", "Organ Swap", …
  name           TEXT      -- "Easy", "Therapy v3"
  description    TEXT
  params         TEXT      -- JSON-encoded {param_name: value}
  created_at     TEXT      -- ISO-8601
  updated_at     TEXT      -- ISO-8601
```

CRUD lives in
[`src/data/database.py`](../src/data/database.py): `save_activity_preset`,
`get_activity_presets(activity_name=None)`, `get_activity_preset(id)`,
`delete_activity_preset(id)`, `next_activity_preset_id()`. IDs auto-
increment via the shared `counters` table (same mechanism as session and
participant IDs).

---

## Open questions

- **Per-robot vs per-session presets**: if I want to run Organ Swap on
  three robots simultaneously, each at a different difficulty preset, do
  they need to be independent? (Today they share the activity instance.)
  Decision needed before Phase 1c.
- **Behavior across robots**: does the state machine track per-robot state,
  or is there a single shared state for the whole session? Likely
  per-robot, but worth confirming before coding.
- **Param validation**: just `min`/`max` and JSON-shape, or do we want
  cross-field validation hooks?

---

## Skin geometry: shape + per-layer dims

Each skin declares two things that govern how the grid editor and the
activity-time `SkinGridView` render it:

- **`shape`** — `"rect"` (default) or `"round"`. Round masks off cells
  whose centroid falls outside the inscribed circle so the user sees the
  physical boundary of the skin without having to paint a custom outline.
- **Per-layer grid dimensions** — the chamber grid and the sensor grid
  can have *different* resolutions. `grid: {cols, rows}` at the top of
  the skin entry is the chamber grid; `touch.grid: {cols, rows}` is the
  sensor grid (optional — defaults to `grid` for legacy skins). Both
  layers occupy the same widget pixel area; the renderer just slices it
  at different densities.

Example: organ skin with 3 chambers on a 3×3 layout but 8×4 sensors for
fine touch resolution:

```yaml
skins:
  - skin_id: belly
    shape: round
    grid: {cols: 3, rows: 3}      # chamber grid: coarse
    chamber_grid:
      - [-1,  0, -1]
      - [ 1,  1,  2]
      - [-1,  2, -1]
    touch:
      node_mac: BB:CC:DD:EE:FF:00
      sensor_count: 4
      grid: {cols: 8, rows: 4}    # sensor grid: finer
      sensor_grid:
        - [0, 0, 0, 0, 1, 1, 1, 1]
        - [0, 0, 0, 0, 1, 1, 1, 1]
        - [2, 2, 2, 2, 3, 3, 3, 3]
        - [2, 2, 2, 2, 3, 3, 3, 3]
      sensor_to_chamber: {"0": 0, "1": 0, "2": 1, "3": 2}
```

In the editor, picking a **Mode** (Chambers / Touch zones) swaps the
spinboxes to that layer's dims. Cells outside the round mask are drawn
muted (light grey) and aren't paintable. In the activity view, the
chamber regions render at chamber resolution while sensor cells (with
their T-buttons + active pulse overlays) render at sensor resolution
over the same area.

---

## Sensor → Chamber mapping

A skin's `touch:` block in `settings.yaml` can carry a
`sensor_to_chamber` dict that wires individual sensors to chambers:

```yaml
skins:
  - skin_id: belly
    chambers: [...]
    touch:
      node_mac: AA:BB:CC:DD:EE:FF
      sensor_count: 4
      sensor_grid: [[...]]
      sensor_to_chamber: {"0": 0, "1": 0, "2": 1, "3": 1}   # sensor → chamber
```

- Edited in the **SkinConfigDialog** via a "Sensor → Chamber mapping"
  table that auto-rebuilds when the user changes sensor count or chamber
  count.
- Activities subscribe to `controller.on_imu(...)` and read the mapping
  to decide which chamber to drive when a sensor fires.
- In simulation, clicking a sensor's **T-button** on `SkinGridView` also
  pulses the mapped chamber in blue — visual confirmation without an
  activity wired in.

JSON keys are stored as strings; consumers should normalise to `int`
before lookup.

---

## Phase 2 — Declarative activities from the GUI

After Phase 1 lands (OrganSwap on real hardware), the next layer lets
educators and researchers **author new activities without writing
Python**. The activity definition becomes data (a JSON state machine
stored in the DB), interpreted by a generic `ScriptedActivity`.

### Why declarative, not "paste Python"

Python is too powerful (and too dangerous) to allow inside the app —
any imported code can crash the GUI, leak memory, or open files. A
declarative spec, by contrast, is **always safe to load**: the
interpreter only knows how to run actions and check conditions from a
fixed catalogue. New verbs are added by the developer (in code) but
become available to every existing spec automatically.

### Schema (DB)

```
declarative_activities
  id           TEXT PK     -- "DA001", "DA002", …
  name         TEXT        -- "My Behaviour"
  description  TEXT
  spec         TEXT        -- JSON, see below
  created_at   TEXT
  updated_at   TEXT
```

### Spec format

```json
{
  "initial_state": "sick",
  "states": {
    "sick": {
      "on_enter": [
        {"action": "set_led", "params": {"color": "#ff0000", "pattern": "pulse", "period_ms": 1000}}
      ],
      "on_touch": [
        {"action": "inflate", "params": {"chamber": "$sensor_chamber", "delta": 30}}
      ],
      "transitions": [
        {"to": "cured", "when": {"type": "organ_match",
                                  "params": {"target_ohm": 952.4, "tolerance": 80}}}
      ]
    },
    "cured": {
      "on_enter": [
        {"action": "set_led", "params": {"color": "#00ff00", "pattern": "solid"}}
      ],
      "on_periodic": [
        {"action": "breathe", "params": {"period_ms": 4000, "depth": 60}}
      ],
      "transitions": []
    }
  }
}
```

- `$sensor_chamber` etc. are **placeholders** the interpreter fills with
  context from the firing event (e.g. the chamber mapped to the active
  sensor via `sensor_to_chamber`).
- `transitions[].when` is a tree of condition nodes. Each node has a
  `type` (looked up in the condition catalogue) and a `params` dict.

### Action catalogue (initial, extensible)

| `action` | Params | Effect |
|----------|--------|--------|
| `set_led` | `color`, `pattern` (`solid`/`pulse`/`breathe`/`rainbow`/`off`), `period_ms` | Drive the WS2818 strip |
| `set_pressure` | `chamber`, `value` (0–100 %) | Set chamber target |
| `inflate` / `deflate` | `chamber`, `delta` (0–100 %) | Step the chamber |
| `breathe` | `period_ms`, `depth` (0–100 %) | Start breathing animation |
| `stop_breathing` | — | Stop the breathing animation |
| `log` | `message` | Append to the activity log (debugging) |

### Condition catalogue (initial, extensible)

| `type` | Params | True when |
|--------|--------|-----------|
| `organ_match` | `target_ohm`, `tolerance` | Total resistance within tolerance of target |
| `touch_count` | `chamber`, `min` | Chamber touched at least `min` times since state entered |
| `elapsed` | `min_ms` | At least `min_ms` since state entered |
| `pressure` | `chamber`, `min`, `max` | Chamber pressure inside `[min, max]` % |
| `and` / `or` / `not` | `all` / `any` / `cond` | Logical combinators of sub-conditions |

### Interpreter (`ScriptedActivity`)

A `BaseActivity` subclass that:

- Loads the spec from the DB on `_setup`.
- Builds per-robot state + event handlers from the spec.
- Dispatches `on_enter`, `on_periodic`, `on_touch`, `on_organ` to the
  matching action handlers.
- Re-evaluates `transitions` after each event / on every periodic tick.

Action & condition dispatch is a registry — adding a new verb is just
registering a function. Spec authors gain it automatically.

### Editor UI (Tools => Custom Activities…)

- List existing declarative activities (rename / duplicate / delete).
- Visual state-machine editor: a tree of states; each state has
  collapsible `on_enter`, `on_periodic`, `on_touch`, `transitions`
  sections. Action / condition pickers use the catalogue's PARAMS
  schema so adding a new verb gains a GUI form for free (same pattern
  as the preset editor in Phase 1b).

### Where this fits with Phase 1

- Python plugin activities (Phase 1c, e.g. `OrganSwapActivity`) remain
  the path for performance-sensitive or hardware-specific behaviours.
- Declarative activities are for the long tail — researcher-authored
  variations, school-specific tweaks, exploratory designs.
- Both share the `ACTIVITIES` registry and the SessionPanel flow; the
  user picks an activity, optionally an `ActivityPreset`, and hits Go.

---

## Status of recent (related) refactors

The behaviour framework builds on top of work already shipped this session:

- **Skin templates** ([src/data/database.py](../src/data/database.py)
  → `skin_templates` table, [src/gui/skin_config_dialog.py](../src/gui/skin_config_dialog.py)
  → dropdown + Apply + Save). Reuse layouts across skins; same pattern
  is now used for activity presets.
- **Activity presets** (this phase) — DB-backed bundles of tunable
  values, edited via `Tools => Activity Presets…`.
- **`simulation_mode` per activity** (this phase) — replaces the standalone
  `SimulationActivity` with a checkbox; baseline `SIM_PARAMS` give every
  activity tunable inflate/deflate speeds for free.
- **Sensor → Chamber mapping** (this phase) — declarative wiring from
  IMU sensor index to chamber index, stored per skin in YAML.
- **Skin geometry config** (this phase) — `shape: rect | round` +
  per-layer grid dimensions (chambers vs sensors can have different
  resolutions on the same skin). Editor and `SkinGridView` both honour
  the shape mask.
- **`SkinGridView`** ([src/gui/monitor/skin_grid_view.py](../src/gui/monitor/skin_grid_view.py))
  — spatial render of the skin during activities, with per-chamber
  pressure fill and a touch-pulse overlay tied to `controller.on_imu`.
  Same widget will display behavior state (e.g. tint the whole skin red
  in `sick`, green in `cured`).
- **`node_imu` plumbing** ([src/hardware/esp32_controller.py](../src/hardware/esp32_controller.py)
  → `on_imu`, [src/gui/node_config_dialog.py](../src/gui/node_config_dialog.py)
  → 4-sensor type). The new `on_organ` follows the exact same pattern.
- **Unified Serial output** (`LOG` always, `DBG_PRINT` only in debug
  builds) and **boot broadcast** of `{"status":"node_*_ready"}` across
  all firmwares. The new `node_organ` extensions inherit the same.

See [firmware/PROTOCOL.md](../firmware/PROTOCOL.md) for the complete
ESP-NOW message reference (kept up-to-date as new types are added).
