# Touch Sensing — Design & Implementation

## Physical Design

Each skin that has touch sensing is built as a layered stack:

```
[ user ]
───────────────────────────────
  silicone skin with chambers     ← pneumatic layer, inflates toward user
───────────────────────────────
  silicone layer
───────────────────────────────
  magnets in  grid;
  encapsulated in silicone   
───────────────────────────────
  silicone layer
───────────────────────────────
  MLX90393 sensors
───────────────────────────────
[ rigid plastic / PCB (node_imu) ]
```

**How touch detection works:**  
When the user presses a chamber, the silicone compresses and the magnet above
that chamber moves closer to its sensor → the sensor reading increases above the
resting baseline → touch detected on that chamber.

**Why chamber pressure does not affect touch detection:**  
The chambers inflate *upward* (toward the user), while the magnets sit in a
separate fixed layer between the chambers and the PCB.  Inflating or deflating
a chamber does not change the distance between magnet and sensor.  The resting
baseline is therefore stable regardless of inflation state.

Each sensor maps 1-to-1 to one chamber.  There is no cross-talk ambiguity when
chambers are well-separated and each magnet is directly above its own sensor.

---

## Software Architecture

### Key files

| File | Role |
|------|------|
| `src/hardware/quadrant_detector.py` | Signal processing — threshold + hysteresis per sensor, position estimation from active sensors |
| `src/hardware/skin.py` | Receives raw IMU messages, feeds them to the detector, exposes `get_touch_position()` |
| `src/gui/monitor/skin_grid_view.py` | GUI — yellow pulse on the `sensor_grid` cells of each active sensor |

### Data flow

```
node_imu (ESP32)
  → ESP-NOW → ESPNowGateway
    → ESP32Controller._handle_message()
      → _dispatch_imu(data)
        ├── SkinGridView._on_imu_msg()     ← GUI: flash yellow on sensor cells
        └── Skin._on_imu_touch_data()      ← logic: feed QuadrantDetector
              → TouchPositionTracker.update()
                → skin.get_touch_position()  ← polled by activities
```

### IMU message format

The `node_imu` firmware sends via ESP-NOW:

```json
{"type": "imu", "adj": [0.0, 0.82, 0.0, 0.0], "act": [1], "source": "AA:BB:CC:DD:EE:FF"}
```

| Field | Description |
|-------|-------------|
| `adj` | Per-sensor adjusted values (0.0–1.0), baseline-subtracted by the firmware |
| `mag` | Raw magnitudes in mT (fallback if `adj` absent) |
| `act` | Indices of sensors currently above the firmware threshold |

`Skin._extract_sensor_magnitudes()` tries `adj` → `mag` → `act` in order, so the
system works whether or not the firmware sends pre-normalised values.

---

## Sensor-to-chamber mapping

**This mapping is an activity-level concern, not a skin property.**

The same physical skin — sensor 0 above chamber 0, sensor 1 above chamber 1,
etc. — may need different routings in different activities:

| Activity | Sensor 0 fires → | Sensor 1 fires → |
|----------|-----------------|-----------------|
| Organ Swap | chamber 0 | chamber 1 |
| Group Touch | chamber 2 | chamber 0 |

The skin's `touch:` block therefore only describes **hardware layout**
(`sensor_grid` for the GUI, thresholds, node MAC).  Each activity declares its
own `sensor_to_chamber` parameter.

### How to add it to an activity

Declare a `Param` of type `"sensor_map"` and use `_mapping_for(skin)` to
resolve it at runtime (see `organ_swap.py` for the reference implementation):

```python
from src.activities.base_activity import BaseActivity, Param

class MyActivity(BaseActivity):
    PARAMS = (
        Param(
            name="sensor_to_chamber",
            type="sensor_map", default="auto",
            label="Sensor → Chamber routing",
            description="'auto' = 1:1 (sensor N → chamber N). "
                        "Add custom entries to change the routing for this activity.",
        ),
        # ... other params
    )

    def _mapping_for(self, skin) -> dict:
        """Activity preset takes precedence; 'auto' generates 1:1 fallback."""
        from_preset = self.param_values.get("sensor_to_chamber")
        if from_preset and from_preset != "auto":
            return from_preset if isinstance(from_preset, dict) else {}
        count = min(len(skin.chambers),
                    (skin.touch or {}).get("sensor_count", len(skin.chambers)))
        return {str(i): i for i in range(count)}

    def _react_to_touch(self, skin, sensor_idx) -> None:
        mapping = self._mapping_for(skin)
        ch = mapping.get(str(sensor_idx), mapping.get(sensor_idx))
        if ch is not None:
            skin.inflate(int(ch), delta=20)
```

The `"sensor_map"` type renders as an editable table in the activity preset
dialog, so the therapist can customise the routing without touching config files.

### `sensor_grid` (skin YAML) vs `sensor_to_chamber` (activity)

| Property | Where | Purpose |
|----------|-------|---------|
| `sensor_grid` | skin `touch:` block | Visual: which cells flash yellow in `SkinGridView` when sensor N fires |
| `sensor_to_chamber` | activity param | Logic: which chamber inflates/deflates in response to sensor N in *this* activity |

---

## Configuration reference

Full example (add inside a skin entry in `config/settings.yaml`):

```yaml
touch:
  node_mac: "BB:CC:DD:EE:FF:00"   # MAC of the node_imu for this skin
  sensor_count: 4                  # must match number of sensors on the board

  # Visual layout — which cells flash yellow in SkinGridView when sensor N fires.
  # Arrange to match the physical position of each sensor on the skin.
  grid: {cols: 2, rows: 2}
  sensor_grid:
    - [0, 1]
    - [2, 3]

  # Detection tuning (all optional)
  quadrant_thresholds: [0.3, 0.3, 0.3, 0.3]  # per-sensor, 0.0–1.0
  hysteresis: 0.05                              # raise to reduce flicker
  position_smoothing: 0.3                       # EMA factor, higher = faster
  min_touch_duration_ms: 100                    # ignore taps shorter than this

# NOTE: sensor → chamber routing is NOT here.
# Each activity declares its own sensor_to_chamber param so the same skin
# can react differently in different activities.
```

### Parameter reference

| Parameter | Default | Notes |
|-----------|---------|-------|
| `node_mac` | required | MAC of the `node_imu` node |
| `sensor_count` | 4 | Total number of MLX90393 sensors |
| `sensor_grid` | — | Rows × cols of sensor indices; drives the yellow GUI highlight |
| `quadrant_thresholds` | `[0.3, …]` | One threshold per sensor; lower = more sensitive |
| `hysteresis` | `0.05` | Band below threshold before deactivation |
| `position_smoothing` | `0.3` | 1.0 = no smoothing, 0.0 = maximum smoothing |
| `min_touch_duration_ms` | `100` | Filter accidental brief contacts |

`sensor_to_chamber` is **not** a skin parameter — declare it as a `Param` in
each activity (see the *Sensor-to-chamber mapping* section above).

---

## API

### `skin.get_touch_position() → dict`

Returns the current touch state.  Call this from an activity on each tick.

```python
state = skin.get_touch_position()

# state keys:
# "enabled"          bool  — False if no touch: block configured
# "is_touching"      bool  — at least one sensor above threshold
# "is_valid_touch"   bool  — touch duration >= min_touch_duration_ms
# "position"         str   — e.g. "Q1", "Q1-Q2", "CENTER", "NONE"
# "zone"             str   — e.g. "top_left", "top_edge", "center", "none"
# "confidence"       float — 0.0–1.0
# "touch_duration_ms" int  — ms since touch started (0 if not touching)
```

### `skin.has_touch_tracking → bool`

True when the skin was configured with a `touch:` block and the
`QuadrantDetector` was initialised successfully.

### `skin.reset_touch_tracking()`

Resets internal hysteresis and timing state (e.g. between activity rounds).

---

## Activity integration

The pattern used by `organ_swap.py` (the reference implementation):

```python
class MyActivity(BaseActivity):
    PARAMS = (
        Param(
            name="sensor_to_chamber",
            type="sensor_map", default="auto",
            label="Sensor → Chamber routing",
            description="'auto' = 1:1. Customise per activity so the same "
                        "skin can drive different chambers in different activities.",
        ),
    )

    def _mapping_for(self, skin) -> dict:
        from_preset = self.param_values.get("sensor_to_chamber")
        if from_preset and from_preset != "auto":
            return from_preset if isinstance(from_preset, dict) else {}
        count = min(len(skin.chambers),
                    (skin.touch or {}).get("sensor_count", len(skin.chambers)))
        return {str(i): i for i in range(count)}

    def _on_imu(self, skin, data: dict) -> None:
        mapping = self._mapping_for(skin)
        for raw in (data.get("act") or []):
            ch = mapping.get(str(raw), mapping.get(raw))
            if ch is not None:
                skin.inflate(int(ch), delta=20)
```

`get_touch_position()` is useful when you want the zone name
(`"top_left"`, `"center"`, …) rather than raw sensor indices:

```python
state = skin.get_touch_position()
if state["is_valid_touch"]:
    logger.info("Touch at %s for %d ms", state["zone"], state["touch_duration_ms"])
```

---

## GUI

`SkinGridView` shows touch feedback using the existing yellow pulse:

- When a sensor fires (appears in `act`), the `sensor_grid` cells for that
  sensor flash with a yellow outline.
- The pulse decays over ~400 ms; while touching continuously it stays lit.
- No separate indicator is drawn — the yellow cells directly show which
  chamber is being pressed.

The layout of the highlighted cells is fully determined by `sensor_grid` in the
YAML, so any skin shape (rectangular, round, asymmetric) is supported.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| No touch detected | `adj` all zeros; baseline not set | Check firmware baseline initialisation; lower `quadrant_thresholds` |
| Constant false positive | Threshold too low or magnet too strong | Raise threshold, or add `hysteresis: 0.1` |
| Flickering on/off | Sensor noise at edge of threshold | Increase `hysteresis` (try 0.1–0.15) |
| Wrong chamber reacts | `sensor_to_chamber` mapping wrong | Check sensor physical positions and update mapping |
| No yellow flash in GUI | `node_mac` mismatch or IMU not connected | Verify MAC in config matches node_imu |

---

## Source reference

| Component | Origin |
|-----------|--------|
| `QuadrantDetector` / `TouchPositionTracker` | Adapted from `Tese/tools/quadrant_detection.py` |
| IMU firmware | Colleague's repo (external, not in this tree) |
| `Skin._extract_sensor_magnitudes` | New — handles `adj`/`mag`/`act` fallback chain |
| `SkinGridView` yellow pulse | Pre-existing, unchanged |
