"""Skin module — a logical grouping of 1-3 air chambers from a single ESP32 node.

A Skin is a physical tactile piece. With the current hardware (`node_direct`
3 chambers, `node_multiplexed` up to 12 chambers) every chamber of a skin lives
on the same ESP32 — multi-node skins are no longer a use case. The class is
correspondingly simple: one controller, one slot list.

Config expected by the constructor (``chamber_inputs``):
    [
        {"controller":   <ESP32Controller|SimulatedController>,
         "node_slot":    <int>,            # physical slot on the node
         "max_pressure": <float>,          # kPa upper cap for this chamber
         "min_pressure": <float>},         # kPa lower cap (default 0; negative for vacuum-fed chambers)
        ...
    ]

All entries must share the same ``controller`` (single-MAC invariant).
"""

import logging
import math
import time
from typing import Any, Optional

from src.hardware.air_chamber import AirChamber, ChamberState

logger = logging.getLogger(__name__)


class Skin:
    """A physical skin unit with 1-3 air chambers on a single ESP32 node."""

    def __init__(
        self,
        skin_id: str,
        chamber_inputs: list[dict[str, Any]],
        name: str | None = None,
        *,
        grid: dict[str, int] | None = None,
        chamber_grid: list[list[int]] | None = None,
        touch: dict[str, Any] | None = None,
        touch_controller: Any = None,
        shape: str = "rect",
    ):
        if not chamber_inputs:
            raise ValueError(f"Skin {skin_id!r} has no chambers")

        self.skin_id = skin_id
        self.name = name or skin_id

        # Layout descriptors — see SkinGridEditor for the grid format.
        # ``shape``: "rect" or "round" (round skins mask off-circle cells).
        # ``grid``: {"cols": int, "rows": int} — chamber grid dimensions.
        # ``chamber_grid``: rows × cols of chamber-index-or-(-1).
        # ``touch``: {"node_mac": str, "sensor_count": int,
        #   "grid": {cols, rows}?,            # optional, defaults to ``grid``
        #   "sensor_grid": rows × cols of sensor-index-or-(-1),
        #   "sensor_to_chamber": {str_idx: chamber_idx}?}.
        # ``touch_controller``: optional ESP32Controller (or sim) for the magnet sensor
        # node referenced by ``touch.node_mac`` — bound in build_skins so the
        # UI can subscribe to `on_magnet` directly via ``skin.touch_controller``.
        self.shape = shape if shape in ("rect", "round") else "rect"
        self.grid = grid
        self.chamber_grid = chamber_grid
        self.touch = touch
        self.touch_controller = touch_controller

        self._ctrl = chamber_inputs[0]["controller"]
        self.mac: str = self._ctrl.mac_address

        # local_idx → node_slot
        self._slots: list[int] = []
        # node_slot → local_idx
        self._reverse: dict[int, int] = {}
        # local_idx → AirChamber
        self._chambers: dict[int, AirChamber] = {}

        for local_idx, inp in enumerate(chamber_inputs):
            if inp["controller"] is not self._ctrl:
                raise ValueError(
                    f"Skin {skin_id!r}: all chambers must share one controller "
                    f"(got {inp['controller'].mac_address!r} vs {self.mac!r}). "
                    "With node_direct (3 chambers) and node_multiplexed (12 chambers) "
                    "a single skin always fits inside one node."
                )

            node_slot    = int(inp["node_slot"])
            max_pressure = float(inp.get("max_pressure", 8.0))
            min_pressure = float(inp.get("min_pressure", 0.0))

            self._slots.append(node_slot)
            self._reverse[node_slot] = local_idx
            self._chambers[local_idx] = AirChamber(
                chamber_id=local_idx,
                esp32_mac=self.mac,
                max_pressure=max_pressure,
            )
            # Stash min_pressure on the AirChamber for serialisation (chamber_defs).
            self._chambers[local_idx].min_pressure = min_pressure  # type: ignore[attr-defined]

        # One callback registration for the whole skin.
        self._ctrl.on_pressure(self._on_pressure)
        on_target = getattr(self._ctrl, "on_target", None)
        if on_target is not None:
            on_target(self._on_target)

        # Push per-chamber max + min pressure to the firmware so they survive PC crashes.
        set_max = getattr(self._ctrl, "set_max_pressure", None)
        set_min = getattr(self._ctrl, "set_min_pressure", None)
        for local_idx, slot in enumerate(self._slots):
            ch = self._chambers[local_idx]
            if set_max is not None:
                set_max(slot, ch.max_pressure)
            if set_min is not None:
                set_min(slot, getattr(ch, "min_pressure", 0.0))

        # Touch position tracking (if touch sensing is configured)
        self._touch_position_tracker = None
        self._touch_detector = None
        if touch and touch_controller:
            self._setup_touch_tracking(touch, touch_controller)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def chambers(self) -> dict[int, AirChamber]:
        """Local-index → AirChamber mapping."""
        return self._chambers

    @property
    def chamber_count(self) -> int:
        return len(self._chambers)

    @property
    def node_macs(self) -> list[str]:
        """Single-element list with this skin's node MAC (kept for backward compat)."""
        return [self.mac]

    @property
    def is_connected(self) -> bool:
        return self._ctrl.is_connected

    @property
    def chamber_defs(self) -> list[dict[str, Any]]:
        """Chamber descriptors in config format (for serialisation / simulation)."""
        defs = []
        for idx, slot in enumerate(self._slots):
            ch = self._chambers[idx]
            d: dict[str, Any] = {
                "mac": self.mac, "slot": slot,
                "max_pressure": ch.max_pressure,
            }
            min_p = getattr(ch, "min_pressure", 0.0)
            if abs(min_p) > 1e-3:   # only persist explicit non-zero defaults
                d["min_pressure"] = min_p
            defs.append(d)
        return defs

    # ------------------------------------------------------------------
    # Commands  (local_idx = 0-based position within this skin)
    # ------------------------------------------------------------------

    def inflate(self, local_idx: int | None = None, delta: int = 10) -> bool:
        """Inflate by delta % (relative). Pass None for all chambers."""
        if local_idx is None:
            return all(self._apply(i, "inflate", delta) for i in self._chambers)
        return self._apply(local_idx, "inflate", delta)

    def deflate(self, local_idx: int | None = None, delta: int = 10) -> bool:
        """Deflate by delta % (relative). Pass None for all chambers."""
        if local_idx is None:
            return all(self._apply(i, "deflate", delta) for i in self._chambers)
        return self._apply(local_idx, "deflate", delta)

    def set_pressure(self, local_idx: int | None = None, value: int = 100) -> bool:
        """Set absolute target pressure (0-100 %). Pass None for all chambers."""
        if local_idx is None:
            return all(self._apply(i, "set_pressure", value) for i in self._chambers)
        return self._apply(local_idx, "set_pressure", value)

    def hold(self, local_idx: int) -> bool:
        chamber = self._chambers.get(local_idx)
        if chamber is None:
            logger.error("Skin %s has no chamber at local index %d", self.skin_id, local_idx)
            return False
        chamber.target_pressure = chamber.pressure
        chamber.state = ChamberState.IDLE
        return self._ctrl.hold(self._slots[local_idx])

    def pause(self) -> None:
        for chamber in self._chambers.values():
            chamber.state = ChamberState.IDLE

    def get_status(self) -> dict[str, Any]:
        return {
            "skin_id": self.skin_id,
            "mac": self.mac,
            "chambers": {
                idx: {"state": c.state.value, "pressure": c.pressure}
                for idx, c in self._chambers.items()
            },
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _on_pressure(self, node_slot: int, pressure: int) -> None:
        local_idx = self._reverse.get(node_slot)
        if local_idx is not None:
            self._chambers[local_idx].update_pressure(pressure)

    def _on_target(self, node_slot: int, target: int) -> None:
        local_idx = self._reverse.get(node_slot)
        if local_idx is not None:
            self._chambers[local_idx].target_pressure = target

    def _apply(self, local_idx: int, kind: str, value: int) -> bool:
        chamber = self._chambers.get(local_idx)
        if chamber is None:
            logger.error("Skin %s: no chamber at local index %d", self.skin_id, local_idx)
            return False
        slot = self._slots[local_idx]

        if kind == "inflate":
            new_target = min(100, chamber.target_pressure + value)
            chamber.target_pressure = new_target
            if chamber.pressure < new_target:
                chamber.state = ChamberState.INFLATING
            elif new_target > 0:
                chamber.state = ChamberState.INFLATED
            return self._ctrl.inflate(slot, value)

        if kind == "deflate":
            new_target = max(0, chamber.target_pressure - value)
            chamber.target_pressure = new_target
            if chamber.pressure > new_target:
                chamber.state = ChamberState.DEFLATING
            else:
                chamber.state = ChamberState.IDLE
            return self._ctrl.deflate(slot, value)

        # set_pressure
        v = max(0, min(100, value))
        chamber.target_pressure = v
        if chamber.pressure < v:
            chamber.state = ChamberState.INFLATING
        elif chamber.pressure > v:
            chamber.state = ChamberState.DEFLATING
        elif v > 0:
            chamber.state = ChamberState.INFLATED
        else:
            chamber.state = ChamberState.IDLE
        return self._ctrl.set_pressure(slot, v)

    # ------------------------------------------------------------------
    # Touch position tracking
    # ------------------------------------------------------------------

    def _setup_touch_tracking(self, touch: dict[str, Any], touch_controller: Any) -> None:
        """Initialize touch position tracking with quadrant detection.

        The quadrant detector resolves *where* on the skin a touch lands from a
        4-sensor magnet board, so it only engages when the touch node actually
        exposes 4 sensors. Other layouts (e.g. the simulated T-button skins)
        skip it — they still get touch *reactions* via the activity's on_magnet
        handler; only spatial position tracking is unavailable."""
        if int(touch.get("sensor_count", 0)) != 4:
            return
        try:
            from src.hardware.quadrant_detector import QuadrantDetector, TouchPositionTracker

            # Get magnet strength from touch config or default to strong
            magnet_strength = touch.get("magnet_strength", "strong")

            # Thresholds and hysteresis are now in raw μT units (absolute).
            # Default 100 μT assumes the firmware re-zeroed at rest; tune via
            # the Touch Tuning panel until rest < threshold < touch peak.
            thresholds = touch.get("quadrant_thresholds", None)
            hysteresis = float(touch.get("hysteresis", 20.0))
            ema_alpha  = float(touch.get("ema_alpha", 0.25))

            # Create quadrant detector
            self._touch_detector = QuadrantDetector(
                thresholds=thresholds,
                hysteresis=hysteresis,
                ema_alpha=ema_alpha,
                magnet_strength=magnet_strength,
            )

            # Create position tracker
            smoothing = touch.get("position_smoothing", 0.3)
            min_duration = touch.get("min_touch_duration_ms", 100)
            self._touch_position_tracker = TouchPositionTracker(
                detector=self._touch_detector,
                smoothing_alpha=smoothing,
                min_touch_duration_ms=min_duration,
            )

            # Register magnet sensor callback for touch data
            if hasattr(touch_controller, "on_magnet"):
                touch_controller.on_magnet(self._on_magnet_touch_data)

            logger.info(f"Touch position tracking enabled for skin {self.skin_id}")

        except ImportError:
            logger.warning("Quadrant detector not available - touch position tracking disabled")
        except Exception:
            logger.exception(f"Failed to setup touch tracking for skin {self.skin_id}")

    def _on_magnet_touch_data(self, data: dict[str, Any]) -> None:
        """Process magnet sensor touch data for position tracking.

        The node_magnet_sensor sends: {"type":"magnet", "raw":[...], "mag":[...], "adj":[...], "act":[...]}
        - mag: raw magnitudes in μT (preferred — passed directly to QuadrantDetector)
        - act: list of active sensor indices (binary fallback)
        The firmware 'adj' field is intentionally skipped: it depends on the
        firmware fullscaleMt constant and saturates at 1.0 when that constant is
        too small.  Raw μT values with absolute PC-side thresholds are more robust.
        """
        if self._touch_position_tracker is None:
            return

        try:
            sensor_count = self.touch.get("sensor_count", 4) if self.touch else 4
            values = self._extract_sensor_magnitudes(data, sensor_count)
            if values is None:
                return

            current_time_ms = int(time.time() * 1000)
            state = self._touch_position_tracker.update(values, current_time_ms)

            if state["events"]["position_changed"]:
                logger.debug("Touch position on skin %s: %s", self.skin_id, state["position"])
            if state["events"]["touch_started"]:
                logger.info("Touch started on skin %s: zone=%s", self.skin_id, state["zone"])
            if state["events"]["touch_ended"] and state["is_valid_touch"]:
                logger.info("Touch ended on skin %s: zone=%s duration=%dms",
                            self.skin_id, state["zone"], state["touch_duration_ms"])

        except Exception:
            logger.exception("Error processing touch data for skin %s", self.skin_id)

    @staticmethod
    def _extract_sensor_magnitudes(data: dict[str, Any], count: int) -> list[float] | None:
        """Extract raw per-sensor magnitudes (μT) from a node_magnet_sensor message.

        Tries mag (raw μT) → act (binary) in order.  The firmware 'adj' field
        is deliberately skipped — it normalises against fullscaleMt which can
        saturate, making all values 1.0 and breaking detection.
        """
        return (Skin._try_mag(data, count)
                or Skin._try_act(data, count))

    @staticmethod
    def _try_mag(data: dict[str, Any], count: int) -> list[float] | None:
        """Extract raw magnitudes in μT from the 'mag' field."""
        raw = data.get("mag")
        if not isinstance(raw, (list, tuple)) or len(raw) < count:
            return None
        try:
            vals = [float(v) for v in raw[:count]]
        except (TypeError, ValueError):
            return None
        if not all(math.isfinite(v) for v in vals):
            return None
        return [max(0.0, v) for v in vals]

    @staticmethod
    def _try_act(data: dict[str, Any], count: int) -> list[float] | None:
        """Extract from 'act' (list of active sensor indices) as binary 0/1.

        Used as last-resort fallback — the QuadrantDetector thresholds (μT) will
        not fire on these 0/1 values unless the threshold is ≤1.0 μT, which is
        unlikely in practice.  The fallback is kept so the tracker doesn't crash
        when only 'act' is present.
        """
        raw = data.get("act")
        if not isinstance(raw, (list, tuple)):
            return None
        try:
            active = {int(i) for i in raw}
            return [1.0 if i in active else 0.0 for i in range(count)]
        except (TypeError, ValueError):
            return None

    @property
    def has_touch_tracking(self) -> bool:
        """Check if this skin has touch position tracking enabled."""
        return self._touch_position_tracker is not None

    def get_touch_position(self) -> dict[str, Any]:
        """Get current touch position tracking state."""
        if self._touch_position_tracker is None:
            return {
                "enabled": False,
                "position": "NONE",
                "zone": "none",
                "confidence": 0.0,
            }

        tracker_state = self._touch_position_tracker.to_dict()
        return {
            "enabled": True,
            "position": tracker_state["current_position"],
            "zone": tracker_state["current_zone"],
            "confidence": tracker_state["confidence"],
            "is_touching": tracker_state["is_touching"],
            "is_valid_touch": tracker_state["is_valid_touch"],
            "touch_duration_ms": tracker_state["touch_duration_ms"],
        }

    def reset_touch_tracking(self) -> None:
        """Reset touch position tracking state."""
        if self._touch_position_tracker:
            self._touch_position_tracker.reset()
            logger.debug(f"Touch tracking reset for skin {self.skin_id}")

    # ------------------------------------------------------------------
    # Touch tuning (used by the live tuning panel in the GUI)
    # ------------------------------------------------------------------

    @property
    def touch_thresholds(self) -> list[float] | None:
        """Current per-quadrant detection thresholds, or None if no detector."""
        if self._touch_detector is None:
            return None
        return list(self._touch_detector.thresholds)

    @property
    def touch_hysteresis(self) -> float | None:
        """Current detection hysteresis, or None if no detector."""
        if self._touch_detector is None:
            return None
        return float(self._touch_detector.hysteresis)

    def set_touch_thresholds(self, thresholds: list[float]) -> None:
        """Live-update the per-quadrant detection thresholds."""
        if self._touch_detector is not None:
            self._touch_detector.set_thresholds([float(t) for t in thresholds])
            logger.debug("Touch thresholds set for skin %s: %s",
                         self.skin_id, thresholds)

    def set_touch_hysteresis(self, hysteresis: float) -> None:
        """Live-update the detection hysteresis."""
        if self._touch_detector is not None:
            self._touch_detector.set_hysteresis(float(hysteresis))

    def rebaseline_touch(self) -> bool:
        """Ask the touch node to re-zero its sensors (ESP-NOW `rebaseline`).

        Returns False when the controller can't take raw commands (e.g. the
        simulated touch source). Also resets local tracking so the next reading
        starts clean.
        """
        self.reset_touch_tracking()
        ctrl = self.touch_controller
        if ctrl is not None and hasattr(ctrl, "send_command"):
            return bool(ctrl.send_command("rebaseline"))
        return False

    def __repr__(self) -> str:
        return (
            f"Skin(id={self.skin_id!r}, chambers={self.chamber_count}, "
            f"mac={self.mac!r})"
        )
