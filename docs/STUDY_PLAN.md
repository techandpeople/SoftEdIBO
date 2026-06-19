# Study Plan — "Hospital for Sick Robots"

Living document for the children's study. Tracks what the study needs, what
already exists, what is being added, and what is still open. Check items off
as they land so work can resume after an interruption.

Last updated: 2026-06-11 (initial version + organ/cover sensing chain).

---

## 1. Study context

A behavioral-observation study with children. Researchers observe:

- **Behavior types** — what each child does (touch, swap organs, open/close
  the cover, watch, help, wait).
- **Rhythm** — pacing of actions (inter-event intervals per child / robot).
- **Imitation / convergence** — whether children do the same as each other
  (cross-participant correlation of event sequences).

### Robot setups (3 conditions)

| Setup | Robots | Children | Social structure |
|---|---|---|---|
| Turtle | 1 TurtleRobot, multiple skins | 3 share one robot | Cooperative — one shared "patient" |
| Thymio | 3 ThymioRobots | 1 robot per child | Parallel individual — imitation observable across robots |
| Tree | 1 TreeRobot, 1 branch/skin per child | 3, each owns a branch | Individual within shared body — sharing supported by `TreeRobot.assign_to / share_with` |

### Activity theme

At least one activity per robot, themed **Hospital for sick robots**:
the child must swap the bad organ(s) for good ones, then close the silicone
cover over the skin. The cover **closes an electrical circuit** — that is how
the ESP32 knows the cover is back on. Only then is the robot "cured".

`OrganSwapActivity` already implements the sick/cured state machine and works
on any robot type, so the same activity covers all three setups; per-setup
presets (difficulty, colors, timings) come from the existing ActivityPreset
system.

---

## 2. Cover + organ sensing — hardware design (decided)

One ADC line measures **both** the organ identity and the cover state:

```
3V3 ── R_KNOWN (1 kΩ) ──●── organ resistor network ── cover contact ── GND
                        │
                   ORGAN_SENSE_PIN (ADC)
```

- Each pluggable organ is a silicone block with a known internal resistor;
  all organ slots are wired **in parallel** (one reading per robot/node).
- The **cover contact is in series with the whole network**: cover off →
  open circuit → ADC reads the 3V3 rail → firmware reports `open: true`.
  Cover on → divider gives `R_total = R_KNOWN * raw / (4095 - raw)`.
- This matches the physical story ("the cover closes the circuit") and costs
  **zero extra GPIOs** — no separate cover switch needed.
- Default pin on the direct node: **IO36 (SENSOR_VP)** — the only free
  ADC1 input (IO34/35/39 are the pressure sensors, IO32/33 the pumps).
- **Contact mechanism (decided 2026-06-11): gravity** — the cover simply
  rests on contact pads, its own weight closes the circuit. Consequences:
  - firmware debounces the open/closed flip (3 consecutive 100 ms samples)
    since a resting contact can bounce while children handle the cover;
  - pads should be large-area (conductive fabric / wide copper pads) and the
    cover side weighted or fitted with a conductive patch at the right spot;
  - pilot must verify the contact survives bumps to the table/robot.

> **TODO (hardware):** confirm IO36 is reachable on the direct-node PCB
> (header / test pad).

Detection semantics (PC side, `OrganSwapActivity`):

| Reading | Meaning | Patient state |
|---|---|---|
| `open: true` (R = ∞) | Cover off — "surgery" in progress | `open` |
| R finite, wrong value | Cover on, wrong/missing organs | `sick` |
| R matches cured target (±tol) | Cover on + correct organs | `cured` |

**One circuit (Turtle / Thymio) vs many (Tree).** The direct node has a single
organ circuit on IO36. The multiplexed node (Tree) reads several circuits on
spare mux channels, declared per node in YAML:

```yaml
nodes:
  - mac: "AA:BB:CC:DD:EE:FF"
    node_type: node_multiplexed
    organ_channels: [13, 14, 15]   # one mux channel per branch
skins:
  - skin_id: branch-1
    organ: {slot: 0}               # → organ_channels[0] = mux ch 13
  - skin_id: branch-2
    organ: {slot: 1}
  - skin_id: branch-3
    organ: {slot: 2}
```

Each skin with an `organ` block is its **own patient** (own cover, LED, state).
Skins without one share a single whole-robot patient.

---

## 3. Work breakdown

### Phase A — organ + cover sensing chain (in progress, this session)

- [x] Plan written (this file).
- [x] Firmware (direct node): `organ.h` — periodic ADC sample, voltage-divider
      conversion, open-circuit detection, hysteresis + heartbeat broadcast
      `{"type":"organ","resistance_ohm":R,"open":bool}`.
- [x] `ESP32Controller.on_organ(cb)` — dispatch `type=="organ"`;
      `open:true` is delivered as `float("inf")`.
- [x] `SimulatedController.sim_set_organ(ohm | None)` + `on_organ` so the
      activity is fully testable without hardware (`None` = cover off).
- [x] `OrganSwapActivity`: third state **`open`** (cover off) with its own
      LED color; transitions sick ⇄ open ⇄ cured driven by organ readings.
- [x] Behavioral event logging from the activity (see §4).
- [x] `PROTOCOL.md` — document the `organ` broadcast.
- [x] Firmware builds (`pio run -e direct`), Python tests pass.

### Phase B — study tooling

- [x] **Multiplexed node organ sensing** — `multiplexed/organ.h`: per-slot
      sensing on configured mux channels, broadcast
      `{"type":"organ","slot":N,"resistance_ohm":R,"open":bool}`. Channels come
      from `configure`'s `organ_channels` (highest channels, scrubbed from the
      chamber autodetect). Wired into `_robot_builder` from a node's
      `organ_channels` YAML field. Tree gets one patient per branch.
- [x] **OrganSwap per-patient state machines** — a skin with its own
      `organ: {slot}` block is its own patient (Tree branch, own LED + cover);
      skins without one fold into a single whole-robot patient (Turtle shared
      circuit, Thymio). Events/targets use the patient id (`<robot>` or
      `<robot>/<skin>`).
- [x] **Child-safety actuation watchdog** — both firmwares force-stop any
      chamber stuck INFLATING/DEFLATING past 10 s (sensor-failure burst
      protection). See §8.
- [x] **Observer quick-tag panel** (`src/gui/observer_panel.py`) — one button
      per behavior code per child; each click logs a timestamped `observer`
      event. Opened/closed with the session by `SessionPanel`. Replaces video
      coding (researcher's preference). Carries the **marker** button too.
- [x] **Session export** (`src/data/export.py` `SessionExporter`) — flattens
      `interaction_events` to CSV, attributing each row to `robot_id` +
      `participant` via the session's assignments (decodes patient/skin
      targets). Wired into the Data panel's existing export buttons.
- [ ] **GUI: organ/cover state in the monitor** — show per-patient activity
      state (sick / open / cured) + last resistance in `RobotMonitorWidget`;
      tint `SkinGridView` per state (red / blue / green).
- [ ] **GUI: simulation drive for organ swap** — debug buttons (or organ
      catalogue picker) that call `sim_set_organ(ohm, slot)` on the simulated
      controllers, so a full session can be rehearsed before the study.
- [x] **Raw sensor stream recording** — `src/data/stream_recorder.py`
      (`StreamRecorder`) taps the gateway and writes every message of a session
      to `data/recordings/<session_id>.jsonl` (header + 1 line/msg, boot
      announces included so it's self-describing). Toggle "Record sensor
      streams" in the session setup dialog (on by default; off in simulation).
- [x] **Skin geometry registry + `skin_type`** — `src/hardware/skin_geometry.py`
      hardcodes each skin type's shape + sensor coordinates (editable
      constants); `Skin.skin_type` selects it. GUI: the skin dialog offers only
      the robot's own types and draws the real shape/aspect (square vs
      rectangle vs round/triangle/Thymio 'D'), in both editor and monitor.
- [x] **Touch-gesture ML pipeline** — per-`skin_type`, coordinate-free; see
      [TOUCH_ML.md](TOUCH_ML.md). Recording → live label (observer panel) →
      `scripts/label_touches.py` → `scripts/train_touch_model.py`. scikit-learn
      is the optional `ml` extra; the classifier is inert without a model.
- [ ] **Per-setup presets** — author and save three ActivityPresets
      ("Hospital – Turtle", "Hospital – Thymio", "Hospital – Tree") with the
      organ catalogues matching the physically built organs (measure real
      resistor values, set `cured_total_resistance_ohm` accordingly).
- [ ] **Thymio movement reactions** (optional polish) — wheeled "happy dance"
      on cure via `ThymioRobot.set_motors` once the tdm-client TODO lands.
- [ ] **Tree sharing events** — log `assign_to` / `share_with` calls as
      events so branch-sharing behavior is in the same timeline.
- [ ] **Pilot run** — full dry run in simulation, then with one real node:
      organ values discriminate reliably (check ADC noise vs ±tolerance),
      cover contact is robust to child handling.

### Phase C — analysis support (after pilot)

- [ ] Notebook/script with first-pass metrics:
      - per-child event rate + inter-event interval distributions (rhythm);
      - state-transition timelines per robot (time-to-cure, number of
        cover openings, organ trial-and-error count);
      - cross-child lagged correlation / sequence similarity (imitation).
- [ ] Decide on ML for behavior detection (see §5) **after** the pilot data
      exists.

---

## 4. Behavioral event logging (what gets recorded)

All events go to the existing `interaction_events` table
(`session_id, participant_id, type, action, target, timestamp, metadata`).

Already logged today:

- `session` / `start|pause|resume|stop` (SessionPanel).
- `touch` / `press` (+ release) per `skin_id:chamber`, attributed to the
  participant assigned to the skin (TouchAssignmentPanel flow).

Added by `OrganSwapActivity`, keyed by **patient id** in ``target`` (a whole
robot ``<robot>`` or a single branch ``<robot>/<skin>``):

| type | action | target | metadata |
|---|---|---|---|
| `activity` | `state` | patient_id | `{"from": "...", "to": "..."}` |
| `cover` | `open` / `close` | patient_id | — |
| `organ` | `reading` | patient_id | resistance in Ω (`-1`/`inf` when cover off) |

Added by the **observer quick-tag panel** (live coding, no video):

| type | action | target | metadata |
|---|---|---|---|
| `observer` | behavior code (`watches`, `points`, `helps`, …) | participant_id | — |
| `marker` | `mark` | (empty) | optional free-text sync note |

`SessionExporter` (Data panel → Export) resolves `robot_id` + `participant`
for every row from the session's assignments, decoding patient/skin targets,
so the CSV is analysis-ready without a manual join.

These streams + touch are enough to compute rhythm (timestamps),
trial-and-error (organ readings between cover open/close), and imitation
(compare streams across patients/participants in the same session).

> Single-PC timestamps: all events are stamped on receipt by the one PC, so
> streams are mutually comparable without clock sync. ESP-NOW latency
> (< 50 ms) is negligible at child-behavior timescales.

---

## 5. Should ML be used to detect behaviors? (assessment)

Constraint (researcher): **no video recording**. ML on sensor data is
acceptable. Recommendation: **classical event analysis for the core
pipeline; sensor-only ML as a later, additive layer** (see decision §6.4).

- The behaviors of interest (touch, organ swap, cover open/close, cure) are
  **directly instrumented** — the sensors emit them as discrete, labeled,
  timestamped events. There is nothing for a classifier to recover; rules on
  the event log are exact, explainable, and auditable (important for a study
  with children, and for reviewers).
- Rhythm and imitation are better served by **classical sequence statistics**
  (inter-event intervals, lagged cross-correlation, edit/alignment distance
  between event sequences) than by a learned model — with N ≈ 3 children per
  session there is far too little data to train anything robust, and ML output
  would be hard to defend methodologically.
- Where ML **does** make sense later:
  - **Video-based annotation assist** (pose estimation / action recognition on
    the recordings) to pre-label off-sensor behaviors (watching, pointing,
    talking) for the human coder to confirm — a big time-saver, but it's a
    post-hoc tool over video, not part of this app. **Note:** the researcher
    prefers minimal video (decision §6.4), so the default is live observer
    coding via the quick-tag panel instead.
  - **Touch-gesture classification** on the magnet-sensor streams (stroke vs
    poke vs press) if the study later needs touch *quality*, not just
    occurrence. Collect the raw `magnet` streams now (cheap) so this stays
    possible.
- Concrete advice: log everything (already the design), add the video sync
  marker, run the pilot, and only reach for ML if a behavior of interest turns
  out not to be derivable from the instrumented events.

---

## 6. Decisions (closed 2026-06-11 with the researcher)

1. **Turtle**: ONE shared organ circuit — the group cures a single patient
   together (cooperation is the point). Covered by the direct-node firmware
   already implemented.
2. **Tree**: one organ + cover circuit **per branch** — each child treats
   their own branch independently (imitation/rhythm observable). Requires
   the Phase B multiplexed-node organ work (per-slot readings over spare
   mux channels).
3. **Cover contact**: gravity — the cover rests on the pads (see §2).
4. **ML / video** (updated): **no video**; ML is allowed **on sensor data
   only**. Behaviors the sensors can't see (watching, pointing, helping,
   talking) are captured by **live observer coding** → Phase B item: an
   observer quick-tag panel in the GUI (one button per behavior code, per
   child; each click logs a timestamped `observer` event into the same
   timeline). Sensor-only ML candidates, in order of value:
   - **touch-gesture classification** on the raw `magnet` streams (stroke vs
     poke vs press vs hold) — adds touch *quality* as a behavior dimension;
   - **unsupervised behavior profiling** over the event log (event rates,
     inter-event intervals, state-transition patterns) to cluster
     interaction styles;
   - imitation/synchrony still starts with classical lagged correlation —
     revisit ML only if that proves insufficient.
   Prerequisite for all of it: **record the raw sensor streams during
   sessions** (see Phase B), otherwise there is nothing to train on later.

## 7. Still open

1. **PCB**: is IO36 routed to a usable connector on the direct node? If not,
   which pin do we sacrifice / patch?
2. **Tree mux channels**: confirm which 74HC4067 channels remain free after
   chambers + tanks on the actual build (need 3 — one per branch). Set them in
   each Tree node's `organ_channels:` YAML; each skin gets `organ: {slot: i}`.
3. **Organ resistor values**: pick values with ≥ 25 % separation between all
   plausible parallel combinations so ADC noise never confuses two states
   (the default catalogue 1.5k/2.2k/3.3k vs 4.7k/5.6k/6.8k is OK on paper —
   verify with real parts and 1 kΩ R_KNOWN).

---

## 8. Child safety (researcher requirement)

The robots are handled directly by children, so **no chamber may over-inflate
and burst**. Layered defences, innermost first:

1. **Per-chamber hard cap in firmware** — `HARD_*_KPA` limits (direct 12 kPa,
   multiplexed 12 kPa chamber / 80 kPa tank) are enforced inside the control
   loop regardless of any PC command. A target above the cap is clamped; a
   reading at the cap stops the actuation.
2. **Per-chamber configured max** — `set_max_pressure` (kPa) is pushed from the
   `Skin` constructor on connect, so each chamber keeps its safe ceiling **even
   if the PC crashes mid-session**. Default 8 kPa, never above the hard cap.
3. **Actuation watchdog (added this session)** — both firmwares force-stop any
   chamber stuck INFLATING/DEFLATING longer than `ACTUATION_TIMEOUT_MS` (10 s).
   This catches the dangerous failure the hard cap can't: a pressure sensor
   that unplugs or sticks below target, which would otherwise leave the inflate
   valve/pump open indefinitely. Normal actuations finish in a few seconds.
4. **Dead-man on manual/dev actuation** — operator valve/pump overrides
   auto-off after 5 s, and are never exposed to children.
5. **Idle-safe boot** — multiplexed node keeps all valves closed and pumps off
   until it receives `configure`; both nodes power up with valves closed.

> **TODO (pilot):** confirm the 10 s watchdog is comfortably longer than the
> slowest legitimate inflate at the chosen pump duty, and physically verify a
> chamber cannot reach burst pressure within that window at the configured
> `max_pressure`. Tune `ACTUATION_TIMEOUT_MS` / `max_pressure` down if needed.
> Also pressure-test the silicone chambers to know the real burst margin.
