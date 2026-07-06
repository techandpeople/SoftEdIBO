# Touch-sensor technologies (adding a new sensor kind)

Every touch board today is **magnetic** — an MLX90393 array under the silicone
(`node_magnet_sensor`, or a `node_direct` that folds the same sensing into its
actuator firmware). This doc is about the seam that keeps that technology choice
in *one* place, so a different physical sensor (e.g. **capacitive**) can be added
as a subclass instead of scattered edits.

For the physics/detection of the current magnetic path, see
[TOUCH_POSITION_TRACKING.md](TOUCH_POSITION_TRACKING.md),
[TOUCH_COUPLING.md](TOUCH_COUPLING.md), and the wire protocol in
[../firmware/PROTOCOL.md](../firmware/PROTOCOL.md).

## The layers, and which are sensor-agnostic

A reading travels: **firmware → gateway → `ESP32Controller` → skin → detection**
(events, position, gesture ML). Most of that path never needs to know *how* the
board senses:

| Layer | Sensor-agnostic? |
|-------|------------------|
| `TouchEventRouter` (press/release per chamber from the `act` index set) | ✅ generic |
| `on_magnet` / `subscribe_skin_magnet` fan-out, `SimulatedMagnetSensor` | ✅ generic dict |
| `on_touch_event(chamber, action)` consumed by activities + GUI | ✅ generic |
| Skin ↔ touch-node config (`touch.node_mac`, `sensor_count`, `sensor_grid`) | ✅ generic |
| Wire strings (`type:"magnet"`, `node_magnet_sensor_ready`), signal units | ❌ per-sensor |
| Spatial detector (`QuadrantDetector`, µT thresholds, 4-sensor geometry) | ❌ per-sensor |
| Pressure→touch compensation (magnet shifts in silicone) | ❌ per-sensor |

The **generic contract** every technology speaks is: a message carrying
*per-sensor magnitudes* and an *active-sensor set* (`act`). Anything above the
contract is reused unchanged; everything below it lives in a profile.

## The seam: `TouchSensorProfile`

[`src/hardware/touch_profiles.py`](../src/hardware/touch_profiles.py) holds a
**Strategy + Registry**:

- **`TouchSensorProfile`** (ABC) — one stateless object per *technology*. It owns
  the wire strings (`ready_status`, `message_type`), the `node_types` that stream
  it, `supports_pressure_coupling`, and the behaviour hooks:
  - `read_magnitudes(data, count)` — message → per-sensor magnitudes;
  - `build_compensator(touch)` — a pressure compensator, or `None`;
  - `build_position_tracker(touch)` — a `(detector, tracker)` pair, or `None`.
- **`MagnetSensorProfile`** — the only technology shipped; wraps the µT
  extraction, the `QuadrantDetector`, and `compensator_from_config`.
- **`TouchSensorRegistry`** (`touch_profiles`) — the process-wide lookup.

Collaborators ask the registry instead of branching on `"magnet"`:

- `ESP32Controller._handle_message` dispatches via
  `touch_profiles.for_ready_status(...)` / `is_message_type(...)` — no literals.
- `Skin` picks its profile from the `touch.sensor` config field
  (`touch_profiles.for_config`, default = magnet) and delegates extraction,
  compensation, and spatial detection to it.

## Adding a new technology (e.g. capacitive)

A skeleton `CapacitiveSensorProfile` already exists in `touch_profiles.py`,
deliberately **not registered** so it changes nothing until its firmware exists.
To bring it online:

1. **Firmware.** Two shapes, both handled by one profile:
   - **Own node** (`node_capacitive_sensor`) — its own board + firmware, its own
     `ready_status` / `message_type`.
   - **Folded into the direct board** (like the magnet module is) — add
     `"node_direct"` to the profile's `node_types` and use a `message_type`
     *distinct* from `"magnet"`. Dispatch is by message type, so one board can
     stream both sensors side by side without clashing.
2. **Fill in the profile.** Complete the `TODO(firmware)` markers in
   `CapacitiveSensorProfile` (field name/units in `read_magnitudes`, geometry
   keys, wire strings). Capacitive doesn't move a magnet in the silicone, so
   leave `supports_pressure_coupling = False` and inherit the no-op
   `build_compensator` — the whole [coupling layer](TOUCH_COUPLING.md) is skipped.
   Write a `build_position_tracker` only if spatial position is needed; touch
   *events* (press/release) already work off the generic `act` set.
3. **Register it.** Uncomment
   `touch_profiles.register(CapacitiveSensorProfile())` at the bottom of the
   module — the controller, skin, and gesture ML pick it up at once.
4. **Offer it in the config UI.** Add the new `node_type` to `MAGNET_NODE_TYPES`
   in [`src/core/skin_config.py`](../src/core/skin_config.py) (the touch-node
   list `magnet_macs` / the skin dialog use). Core can't import the hardware
   registry — layering — so this stays a small, explicit second edit.
5. **Select it per skin.** Set `touch.sensor: "capacitive"` on the skin's config;
   without it a skin defaults to magnet, so existing configs are untouched.

Tests for the seam (and the placeholder) live in
[`tests/test_touch_profiles.py`](../tests/test_touch_profiles.py).
