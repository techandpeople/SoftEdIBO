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
from typing import Any, Callable, Optional

from src.core.skin_config import (
    DEFAULT_FILL_MODE, FILL_MODE_PRESSURE, normalize_fill_mode)
from src.core.touch_compensation import compensator_from_config
from src.hardware.air_chamber import AirChamber, ChamberState
from src.hardware.fill_calibration import combo_key, parse_combo_key
from src.hardware.fill_profile import DeflateProfile, FillProfile
from src.hardware.fill_scaling import DutyModel, duty_for_period, scale_fill_ms
from src.hardware.touch_event_router import TouchEventRouter
from src.hardware.touch_source import CompensatedMagnetSource

logger = logging.getLogger(__name__)

# Firmware status ``st`` field → chamber actuation state. The firmware reports
# only whether a chamber is actively driven (INFLATING/DEFLATING) or not (IDLE);
# AirChamber maps a firmware-IDLE chamber to INFLATED/IDLE from its pressure.
_FW_ACTUATION = {
    0: ChamberState.IDLE,
    1: ChamberState.INFLATING,
    2: ChamberState.DEFLATING,
}


class Skin:
    """A physical skin unit with 1-3 air chambers on a single ESP32 node."""

    def __init__(
        self,
        skin_id: str,
        chamber_inputs: list[dict[str, Any]],
        *,
        grid: dict[str, int] | None = None,
        chamber_grid: list[list[int]] | None = None,
        touch: dict[str, Any] | None = None,
        touch_controller: Any = None,
        shape: str = "rect",
        organ: dict[str, Any] | None = None,
        organs: list[dict[str, Any]] | None = None,
        skin_type: str = "",
        skin_variant: str = "",
    ):
        if not chamber_inputs:
            raise ValueError(f"Skin {skin_id!r} has no chambers")

        self.skin_id = skin_id

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
        # ``organ``: {"slot": int, "node_mac": str?} — this skin has its OWN
        # organ+cover circuit (e.g. a Tree branch). ``slot`` indexes the
        # node's organ circuits (``configure`` ``organ_channels``); ``node_mac``
        # defaults to this skin's chamber node. Skins without this block share
        # their robot's single circuit (Turtle / Thymio) or have none.
        self.organ = organ
        # ``organs``: list of pluggable organ SHAPES displayed on this skin,
        # each ``{"id": str, "shape": str, "rect": [x, y, w, h] (normalised
        # 0..1), "good_ohm": float, "bad_ohm": float}``. Distinct from
        # ``organ`` above (the electrical circuit): ``organs`` is what the GUI
        # draws and the OrganResolver decomposes from the circuit's reading.
        self.organs: list[dict[str, Any]] = list(organs or [])
        # ``skin_type``: stable identity of this skin's TYPE (e.g.
        # "turtle_square"). Indexes the hardcoded geometry registry
        # (src/hardware/skin_geometry.py) — used by the GUI to render the skin
        # and by the touch-gesture ML to pick a per-type model. Empty string
        # means the skin opts out of both.
        self.skin_type = skin_type or ""
        # ``skin_variant``: the silicone format of this skin (e.g. "wrinkles",
        # "natural", "organ"), orthogonal to ``skin_type``. Different chamber
        # sizes per variant; fed to the touch ML as a feature. Empty = unset.
        self.skin_variant = skin_variant or ""

        self._ctrl = chamber_inputs[0]["controller"]
        self.mac: str = self._ctrl.mac_address

        # local_idx → node_slot
        self._slots: list[int] = []
        # node_slot → local_idx
        self._reverse: dict[int, int] = {}
        # local_idx → AirChamber
        self._chambers: dict[int, AirChamber] = {}
        # local_idx → calibrated fill time (ms) or None (pressure-based). Kept
        # for status/serialisation; the runtime fill timing reads _fill_profiles.
        self._fill_times: dict[int, int | None] = {}
        # local_idx → calibrated time→pressure curve, or None (pressure-based).
        # Built from the chamber's ``fill_profile`` curve, falling back to a
        # straight line through a legacy scalar ``fill_time_ms``.
        self._fill_profiles: dict[int, FillProfile | None] = {}
        # local_idx → {frozenset(node_slots): FillProfile} — curves measured with
        # exactly that set of chambers inflating together (the shared-pump load is
        # baked into the curve). Runtime prefers an exact match over scaling the
        # solo curve; falls back to scale_fill_ms when no combination matches.
        self._combo_profiles: dict[int, dict[frozenset[int], FillProfile]] = {}
        # local_idx → measured pump-duty→fill-speed model, or None. When present the
        # runtime picks the "over (ms)" slow-fill duty from the measured curve
        # instead of the rough linear duty_for_period fallback.
        self._duty_models: dict[int, DutyModel | None] = {}
        # local_idx → measured falling deflate curve, or None. Times a deflate the
        # gauge cannot supervise: a target below the node's sensor floor gets an
        # open-time budget from this curve instead of the (blind) closed loop.
        self._deflate_profiles: dict[int, DeflateProfile | None] = {}
        # local_idx → fill mode ("time" | "pressure"). In "pressure" mode the
        # chamber inflates closed-loop on the gauge sensor even when a calibrated
        # curve exists, so the curve is ignored for runtime timing.
        self._fill_modes: dict[int, str] = {}

        self._build_chambers(chamber_inputs)

        # One callback registration for the whole skin.
        self._ctrl.on_pressure(self._on_pressure)
        on_target = getattr(self._ctrl, "on_target", None)
        if on_target is not None:
            on_target(self._on_target)

        self._push_pressure_limits()

        # Magnet stream for DETECTION consumers. When pressure-informed
        # compensation is configured+enabled this wraps the raw controller and
        # subtracts the actuation offset (so an inflating chamber can't fake a
        # touch); otherwise it is just the raw controller, so behaviour is
        # unchanged. The raw ``touch_controller`` stays the source for the
        # recorder / monitor / coupling calibration (they need uncompensated uT).
        self.touch_source = self._build_touch_source(touch, touch_controller)

        # Touch position tracking (if touch sensing is configured)
        self._touch_position_tracker = None
        self._touch_detector = None
        if touch and touch_controller:
            self._setup_touch_tracking(touch)

        # Higher-level touch events (press/release per chamber), independent of
        # position tracking so they work for any sensor count. Delegated to a
        # TouchEventRouter (edge detection + sensor→chamber mapping). Fed from the
        # compensated source so a false touch never reaches the activity layer.
        self._touch_router = TouchEventRouter.from_touch_config(
            touch, len(self._chambers), name=self.skin_id)
        self._touch_router.attach(self.touch_source)

    # ------------------------------------------------------------------
    # Construction helpers
    # ------------------------------------------------------------------

    def _build_touch_source(self, touch: dict[str, Any] | None,
                            touch_controller: Any) -> Any:
        """Return the magnet source for detection consumers.

        A :class:`CompensatedMagnetSource` when the skin's ``touch`` config has an
        enabled coupling matrix; otherwise the raw controller unchanged (so the
        detection path is byte-for-byte identical when compensation is off)."""
        if touch_controller is None:
            return None
        compensator = compensator_from_config(touch)
        if compensator is None:
            return touch_controller
        logger.info("Pressure-informed touch compensation enabled for skin %s",
                    self.skin_id)
        if compensator.has_vector and hasattr(touch_controller, "send_command"):
            # The calibration carries offset vectors — ask the node to stream
            # its 3-axis deltas so the compensator can subtract vectorially
            # (RAM-only firmware flag, so re-push on every build; harmless
            # no-op on older firmware).
            touch_controller.send_command("configure", stream_vec=True)
        return CompensatedMagnetSource(
            touch_controller, compensator, self._chamber_levels)

    def _chamber_levels(self) -> dict[int, float]:
        """Current inflation level (% of max) per node slot — the live signal the
        compensator scales the coupling matrix by (matrix chambers are keyed by
        slot, matching the firmware ``status`` broadcasts)."""
        return {self._slots[idx]: float(ch.pressure)
                for idx, ch in self._chambers.items()}

    def _build_chambers(self, chamber_inputs: list[dict[str, Any]]) -> None:
        """Create one AirChamber per input, recording slot/index maps and fill
        times. All chambers must share this skin's single controller."""
        for local_idx, inp in enumerate(chamber_inputs):
            if inp["controller"] is not self._ctrl:
                raise ValueError(
                    f"Skin {self.skin_id!r}: all chambers must share one "
                    f"controller (got {inp['controller'].mac_address!r} vs "
                    f"{self.mac!r}). With node_direct (3 chambers) and "
                    "node_multiplexed (12 chambers) a single skin always fits "
                    "inside one node."
                )
            node_slot = int(inp["node_slot"])
            fill_time = inp.get("fill_time_ms")
            self._fill_times[local_idx] = int(fill_time) if fill_time else None
            # Prefer a measured curve; fall back to a linear curve through the
            # legacy scalar so old calibrations keep working unchanged.
            self._fill_profiles[local_idx] = (
                FillProfile.from_list(inp.get("fill_profile"))
                or FillProfile.linear(fill_time))
            self._combo_profiles[local_idx] = self._parse_combos(inp.get("fill_profiles"))
            self._duty_models[local_idx] = DutyModel.from_list(inp.get("duty_curve"))
            self._deflate_profiles[local_idx] = \
                DeflateProfile.from_list(inp.get("deflate_profile"))
            self._fill_modes[local_idx] = normalize_fill_mode(inp.get("fill_mode"))
            self._slots.append(node_slot)
            self._reverse[node_slot] = local_idx
            ch = AirChamber(
                chamber_id=local_idx,
                esp32_mac=self.mac,
                max_pressure=float(inp.get("max_pressure", 8.0)),
                min_pressure=float(inp.get("min_pressure", 0.0)),
            )
            self._chambers[local_idx] = ch

    @staticmethod
    def _parse_combos(raw: Any) -> dict[frozenset[int], FillProfile]:
        """Parse a chamber's stored ``fill_profiles`` (combo_key → curve) into a
        ``{frozenset(node_slots): FillProfile}`` lookup. Ignores malformed entries
        and single-slot keys (those are the solo ``fill_profile``)."""
        out: dict[frozenset[int], FillProfile] = {}
        if isinstance(raw, dict):
            for key, curve in raw.items():
                slots = parse_combo_key(key)
                prof = FillProfile.from_list(curve)
                if len(slots) >= 2 and prof is not None:
                    out[slots] = prof
        return out

    def _push_pressure_limits(self) -> None:
        """Push every chamber's max + min pressure to the firmware (e.g. once at
        construction). No-op for controllers lacking setters (the simulator)."""
        for local_idx in range(len(self._slots)):
            self._push_limits(local_idx)

    def _push_limits(self, local_idx: int) -> None:
        """Push one chamber's max + min pressure to the firmware (max first, then
        min, so the firmware validates min against the fresh max).

        Re-sent before every actuation because ESP-NOW is fire-and-forget (no ACK
        yet): the one-shot push at construction can be dropped, and external tools
        (e.g. the Test Actuators dialog, or an "ignore max" run) can leave a stale
        ``max_kpa`` on the node. Without this the firmware clamps an inflate to
        whatever limit it currently holds, so repeated steps could climb far past
        the configured max. The Test Actuators dialog already re-pushes the same
        way; this brings the live session in line."""
        set_max = getattr(self._ctrl, "set_max_pressure", None)
        set_min = getattr(self._ctrl, "set_min_pressure", None)
        ch = self._chambers[local_idx]
        slot = self._slots[local_idx]
        if set_max is not None:
            set_max(slot, ch.max_pressure)
        if set_min is not None:
            set_min(slot, ch.min_pressure)

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
    def geometry(self):
        """Hardcoded geometry for this skin's ``skin_type`` (or None).

        Looks the type up in the geometry registry — the reliable source of the
        skin's shape and sensor coordinates. Returns None when ``skin_type`` is
        empty or unregistered, so callers fall back to per-skin descriptors."""
        from src.hardware.skin_geometry import geometry_for
        return geometry_for(self.skin_type)

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
            min_p = ch.min_pressure
            if abs(min_p) > 1e-3:   # only persist explicit non-zero defaults
                d["min_pressure"] = min_p
            mode = self._fill_modes.get(idx, DEFAULT_FILL_MODE)
            if mode != DEFAULT_FILL_MODE:   # only persist a non-default fill mode
                d["fill_mode"] = mode
            profile = self._fill_profiles.get(idx)
            if profile is not None and not profile.is_empty:
                d["fill_profile"] = profile.to_list()
            else:
                fill = self._fill_times.get(idx)
                if fill:
                    d["fill_time_ms"] = fill
            combos = self._combo_profiles.get(idx)
            if combos:
                d["fill_profiles"] = {combo_key(slots): prof.to_list()
                                      for slots, prof in combos.items()}
            defs.append(d)
        return defs

    # ------------------------------------------------------------------
    # Commands  (local_idx = 0-based position within this skin)
    # ------------------------------------------------------------------

    def inflate(self, local_idx: int | None = None, delta: int = 10) -> bool:
        """Inflate by delta % (relative). Pass None for all chambers.

        A broadcast (``None``) inflates the whole skin in one batch, so the
        chambers fill *together*: their node slots are passed as the co-active set
        so each can use a curve measured for exactly that combination (rather than
        the incrementally-growing fill-load snapshot a per-chamber call sees)."""
        if local_idx is None:
            co_active = set(self._slots)
            return all(self._apply(i, "inflate", delta, co_active=co_active)
                       for i in self._chambers)
        return self._apply(local_idx, "inflate", delta)

    def deflate(self, local_idx: int | None = None, delta: int = 10) -> bool:
        """Deflate by delta % (relative). Pass None for all chambers."""
        if local_idx is None:
            return all(self._apply(i, "deflate", delta) for i in self._chambers)
        return self._apply(local_idx, "deflate", delta)

    def set_pressure(self, local_idx: int | None = None, value: int = 100,
                     period_ms: int = 0, duty: int | None = None) -> bool:
        """Set absolute target pressure (0-100 %). Pass None for all chambers.

        ``period_ms`` > 0 asks the chamber to reach the target gently over roughly
        that long: the pump runs at a reduced duty derived from the chamber's
        measured fill time (the pressure cutoff still decides the final level).
        Needs a calibrated fill curve; without one it falls back to full speed.

        ``duty`` (1-255) sets the pump PWM directly — a gentler/slower stroke at
        a lower value — and takes precedence over the ``period_ms``-derived duty.
        Unlike ``period_ms`` it needs no fill curve, so it works uncalibrated."""
        if local_idx is None:
            return all(self._apply(i, "set_pressure", value,
                                   period_ms=period_ms, duty=duty)
                       for i in self._chambers)
        return self._apply(local_idx, "set_pressure", value,
                           period_ms=period_ms, duty=duty)

    def hold(self, local_idx: int) -> bool:
        chamber = self._chambers.get(local_idx)
        if chamber is None:
            logger.error("Skin %s has no chamber at local index %d", self.skin_id, local_idx)
            return False
        chamber.target_pressure = chamber.pressure
        chamber.state = ChamberState.IDLE
        self._ctrl.fill_load.note_stop(self._slots[local_idx])
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

    def _on_pressure(self, node_slot: int, pressure: int,
                     state: int | None = None,
                     kpa: float = float("nan")) -> None:
        local_idx = self._reverse.get(node_slot)
        if local_idx is not None:
            actuating = _FW_ACTUATION.get(state) if state is not None else None
            self._chambers[local_idx].update_pressure(pressure, actuating, kpa)

    def _on_target(self, node_slot: int, target: int) -> None:
        local_idx = self._reverse.get(node_slot)
        if local_idx is not None:
            self._chambers[local_idx].target_pressure = target

    def _apply(self, local_idx: int, kind: str, value: int,
               co_active: set[int] | None = None, period_ms: int = 0,
               duty: int | None = None) -> bool:
        chamber = self._chambers.get(local_idx)
        if chamber is None:
            logger.error("Skin %s: no chamber at local index %d", self.skin_id, local_idx)
            return False
        slot = self._slots[local_idx]

        # Re-assert the configured min/max on the node before acting, so the
        # firmware clamps to them even if the construction-time push was lost or
        # an external tool left a stale limit (see _push_limits).
        self._push_limits(local_idx)

        if kind == "inflate":
            return self._inflate(local_idx, chamber, slot, value, co_active)

        if kind == "deflate":
            new_target = max(0, chamber.target_pressure - value)
            chamber.target_pressure = new_target
            if chamber.pressure > new_target:
                chamber.state = ChamberState.DEFLATING
            else:
                chamber.state = ChamberState.IDLE
            self._ctrl.fill_load.note_stop(slot)
            ms = self._deflate_ms(local_idx, chamber.pressure, new_target)
            if ms is not None:
                return self._ctrl.deflate(slot, value, ms=ms)
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
        # set_pressure overrides any time-based fill window for this slot.
        self._ctrl.fill_load.note_stop(slot)
        # An explicit duty wins; otherwise derive one from the fill curve so a
        # period_ms still stretches the stroke when the chamber is calibrated.
        if duty is None:
            duty = self._duty_for_period(local_idx, chamber.pressure, v, period_ms)
        if duty is not None:
            return self._ctrl.set_pressure(slot, v, duty=duty)
        return self._ctrl.set_pressure(slot, v)

    def _inflate(self, local_idx: int, chamber: AirChamber, slot: int,
                 value: int, co_active: set[int] | None = None) -> bool:
        """Relative inflate of one chamber, choosing time- vs pressure-based fill.

        A calibrated chamber in ``time`` mode inflates by a *time window* derived
        from its measured fill curve (time to climb from the current target to
        the new one — the curve is non-linear, so this beats assuming
        proportionality). When a curve was measured for *exactly* the set of
        chambers about to fill together (``co_active`` for a batch, else the live
        fill-load snapshot), that combination curve is used directly — its
        shared-pump slowdown is already baked in. Otherwise the solo curve is
        scaled by the node's concurrent fill load (more chambers at once = each
        takes longer). A chamber in ``pressure`` mode (or one with no curve) uses
        classic closed-loop pressure inflate, letting the gauge sensor decide when
        to stop.
        """
        prev_target = chamber.target_pressure
        new_target = min(100, prev_target + value)
        chamber.target_pressure = new_target
        if chamber.pressure < new_target:
            chamber.state = ChamberState.INFLATING
        elif new_target > 0:
            chamber.state = ChamberState.INFLATED

        # Already at (or above) the requested level: there is nothing to inflate.
        # The firmware ``inflate`` path runs the pump for one control cycle per
        # command regardless of current pressure (it only stops at the next
        # pressure check), so holding ``+`` at the cap over-inflates past the
        # limit a pulse at a time. The firmware guards ``set_pressure`` this way
        # but not ``inflate``; mirror it here so the relative inflate can't push
        # past where we already are.
        if chamber.pressure >= new_target:
            return True

        profile = self._fill_profiles.get(local_idx)
        pressure_mode = self._fill_modes.get(local_idx) == FILL_MODE_PRESSURE
        if profile is not None and not pressure_mode:
            load = self._ctrl.fill_load
            active = set(co_active) if co_active is not None \
                else load.active_slots() | {slot}
            measured = self._combo_profiles.get(local_idx, {}).get(frozenset(active))
            if measured is not None:
                ms = max(1, int(round(measured.time_for_pct(new_target)
                                      - measured.time_for_pct(prev_target))))
            else:
                base_ms = (profile.time_for_pct(new_target)
                           - profile.time_for_pct(prev_target))
                ms = scale_fill_ms(base_ms, load.active_count() + 1, load.pump_count)
            load.note_inflate(slot, ms)
            return self._ctrl.inflate(slot, value, ms=ms)
        return self._ctrl.inflate(slot, value)

    # Targets this close to (or below) the measured deflate floor get a time
    # budget: readings hover at the floor, so the closed loop can't be trusted
    # to end the pull there.
    _DEFLATE_FLOOR_MARGIN_PCT = 1.0

    def _deflate_ms(self, local_idx: int, cur_pct: float,
                    target_pct: float) -> int | None:
        """Open-time budget (ms) for a deflate the gauge cannot supervise.

        The calibrated deflate curve's **measured floor** (where the falling
        reading plateaued) embodies the sensor's real low end — today ~ambient,
        below zero once the -40 kPa sensors are in — so a target under it (with
        a small margin) is timed off the curve (:meth:`DeflateProfile.extrapolate_ms`,
        hard-capped) and sent as the firmware's per-chamber cap. Returns ``None``
        (pure closed loop) when the target is comfortably above the floor or the
        chamber has no measured deflate curve."""
        profile = self._deflate_profiles.get(local_idx)
        if profile is None or profile.is_empty:
            return None
        if target_pct >= profile.floor_pct + self._DEFLATE_FLOOR_MARGIN_PCT:
            return None
        return max(1, int(round(profile.extrapolate_ms(cur_pct, target_pct))))

    def _duty_for_period(self, local_idx: int, cur_pct: float, target_pct: float,
                         period_ms: int) -> int | None:
        """Pump duty that stretches an inflate to ~``period_ms`` (None = full speed).

        Returns None — meaning "send no duty, run at full speed" — when no period
        was asked, when the move is a deflate (no inflate curve applies), or when
        the chamber has no measured fill curve to time the move with."""
        if not period_ms or period_ms <= 0 or target_pct <= cur_pct:
            return None
        profile = self._fill_profiles.get(local_idx)
        if profile is None or profile.is_empty:
            logger.debug("Skin %s ch %d: period_ms ignored (no fill curve)",
                         self.skin_id, local_idx)
            return None
        natural_ms = profile.time_for_pct(target_pct) - profile.time_for_pct(cur_pct)
        # Prefer the chamber's measured duty→speed curve; fall back to the rough
        # linear model when it was never swept.
        model = self._duty_models.get(local_idx)
        if model is not None and not model.is_empty:
            return model.duty_for_period(natural_ms, period_ms)
        return duty_for_period(natural_ms, period_ms)

    # ------------------------------------------------------------------
    # Touch events (press/release per chamber)
    # ------------------------------------------------------------------

    def on_touch_event(self, callback: Callable[[int, str], None]) -> None:
        """Register ``callback(chamber_id, action)`` for sensor press/release on
        this skin's magnet board. ``chamber_id`` is the skin-local chamber the
        sensor maps to (raw sensor index when unmapped); ``action`` is
        ``"press"`` or ``"release"``. Fires on the gateway thread — marshal to
        the GUI thread before touching Qt."""
        self._touch_router.subscribe(callback)

    def on_magnet(self, callback: Callable[[dict[str, Any]], None]) -> bool:
        """Register ``callback(data)`` for this skin's compensated magnet stream.

        This is the source detection consumers (activities, gesture ML) should
        use instead of subscribing to ``touch_controller`` directly, so they see
        pressure-compensated readings. Returns False when the skin has no magnet
        source. Fires on the gateway thread — marshal to the GUI thread first."""
        src = self.touch_source
        on_magnet = getattr(src, "on_magnet", None) if src is not None else None
        if on_magnet is None:
            return False
        on_magnet(callback)
        return True

    # ------------------------------------------------------------------
    # Touch position tracking
    # ------------------------------------------------------------------

    def _setup_touch_tracking(self, touch: dict[str, Any]) -> None:
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

            # Register for the compensated magnet stream (position tracking).
            if hasattr(self.touch_source, "on_magnet"):
                self.touch_source.on_magnet(self._on_magnet_touch_data)

            logger.info(f"Touch position tracking enabled for skin {self.skin_id}")

        except ImportError:
            logger.warning("Quadrant detector not available - touch position tracking disabled")
        except Exception:
            logger.exception(f"Failed to setup touch tracking for skin {self.skin_id}")

    def _on_magnet_touch_data(self, data: dict[str, Any]) -> None:
        """Process magnet sensor touch data for position tracking.

        The node_magnet_sensor sends: {"type":"magnet", "mag":[...], "act":[...]}
        - mag: raw magnitudes in μT (preferred — passed directly to QuadrantDetector)
        - act: list of active sensor indices (binary fallback)
        Raw μT values with absolute PC-side thresholds keep detection robust and
        independent of any firmware normalisation.
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

        Tries mag (raw μT) → act (binary) in order.
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
