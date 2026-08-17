"""Touch-sensor profiles - the OOP seam that isolates *how* a board senses touch.

Today every touch board is magnetic (an MLX90393 array under the silicone,
``node_magnet_sensor`` - or a ``node_direct`` that folds the same sensing into
its actuator firmware). Tomorrow it could be capacitive, resistive, or something
else. The rest of the stack should not care: the controller dispatch, the Skin,
the event router and the gesture ML all speak a generic contract (per-sensor
*magnitudes* and an *active-sensor set*), and everything that is specific to a
sensor technology lives in a :class:`TouchSensorProfile`.

A profile owns, in one place:

* the wire strings the board speaks - its boot-``ready`` status and its live
  message ``type`` - so the controller dispatches by asking the registry instead
  of hard-coding ``"magnet"``;
* how a raw message becomes per-sensor magnitudes (:meth:`read_magnitudes`);
* which detection strategy applies - a spatial position tracker
  (:meth:`build_position_tracker`) and whether chamber inflation can masquerade
  as a touch, hence a pressure compensator (:meth:`build_compensator`).

Adding a new touch technology is therefore a new :class:`TouchSensorProfile`
subclass registered here (plus its firmware, and its ``node_type`` added to the
touch-node list in :mod:`src.core.skin_config`) - not edits scattered across the
controller, the skin and the config layer.

The node-type -> technology data stays in ``skin_config`` (pure core); this
module (hardware) references it and adds the behaviour, keeping the core layer
free of hardware imports.
"""

from __future__ import annotations

import logging
import math
from abc import ABC, abstractmethod
from typing import Any, Mapping

from src.core.skin_config import MAGNET_NODE_TYPES
from src.core.touch_compensation import compensator_from_config

logger = logging.getLogger(__name__)


class TouchSensorProfile(ABC):
    """Everything specific to one physical touch-sensing technology.

    Subclasses are stateless strategies - one instance is shared across every
    skin/controller. All the technology-specific knowledge (wire strings, signal
    extraction, detection) is encapsulated here so collaborators can be written
    against the abstract contract.
    """

    #: Short identifier, also the value a skin's ``touch.sensor`` field selects.
    name: str = ""
    #: ``node_type`` strings whose boards stream this kind of touch data.
    node_types: tuple[str, ...] = ()
    #: Boot announce ``status`` this board sends (``""`` if it doesn't announce).
    ready_status: str = ""
    #: Live message ``type`` this board's readings arrive under.
    message_type: str = ""
    #: Keys copied from the boot announce into the controller's cached geometry.
    geometry_keys: tuple[str, ...] = ()
    #: True when chamber inflation can physically fake a touch on this sensor
    #: (magnets shift in the silicone) - i.e. pressure compensation is meaningful.
    supports_pressure_coupling: bool = False

    @abstractmethod
    def read_magnitudes(self, data: Mapping[str, Any],
                        count: int) -> list[float] | None:
        """Return the first ``count`` per-sensor magnitudes from a reading.

        Returns ``None`` when the message carries nothing usable, so the caller
        can skip it. Units are technology-specific (uT for magnets) but must
        match whatever :meth:`build_position_tracker` expects.
        """

    def build_compensator(self, touch: Mapping[str, Any] | None) -> Any:
        """Return a pressure compensator for this skin's touch config, or ``None``.

        Default: no coupling, so no compensation. Technologies where inflation
        fakes a touch override this.
        """
        return None

    def build_position_tracker(self, touch: Mapping[str, Any] | None) -> Any:
        """Return a ``(detector, tracker)`` pair for spatial touch tracking, or
        ``None`` when this technology/layout has no position detection.

        Default: none. Technologies with a spatial detector override this.
        """
        return None


class MagnetSensorProfile(TouchSensorProfile):
    """MLX90393 magnet array - the only touch technology shipped today."""

    name = "magnet"
    node_types = MAGNET_NODE_TYPES
    ready_status = "node_magnet_sensor_ready"
    message_type = "magnet"
    geometry_keys = ("sensors", "magnets", "variant", "geometry")
    supports_pressure_coupling = True

    def read_magnitudes(self, data: Mapping[str, Any],
                        count: int) -> list[float] | None:
        """Prefer raw ``mag`` (uT); fall back to the binary ``act`` set."""
        return self._from_mag(data, count) or self._from_act(data, count)

    @staticmethod
    def _from_mag(data: Mapping[str, Any], count: int) -> list[float] | None:
        """Extract raw magnitudes in uT from the ``mag`` field."""
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
    def _from_act(data: Mapping[str, Any], count: int) -> list[float] | None:
        """Extract from ``act`` (active sensor indices) as binary 0/1.

        Last-resort fallback - the uT thresholds won't fire on 0/1 unless the
        threshold is <=1 uT, but it keeps the tracker from crashing when only
        ``act`` is present.
        """
        raw = data.get("act")
        if not isinstance(raw, (list, tuple)):
            return None
        try:
            active = {int(i) for i in raw}
        except (TypeError, ValueError):
            return None
        return [1.0 if i in active else 0.0 for i in range(count)]

    def build_compensator(self, touch: Mapping[str, Any] | None) -> Any:
        """A magnet under an inflating chamber shifts and reads as touch, so the
        actuation offset is subtracted when a coupling matrix is calibrated and
        enabled (see :func:`compensator_from_config`)."""
        return compensator_from_config(touch)

    def build_position_tracker(self, touch: Mapping[str, Any] | None) -> Any:
        """Build a 4-sensor quadrant detector + position tracker.

        The quadrant detector resolves *where* a touch lands from a 4-sensor
        magnet board, so it only engages for that layout; other sensor counts
        get touch *events* (via the router) but no spatial position.
        """
        if not touch or int(touch.get("sensor_count", 0)) != 4:
            return None
        try:
            from src.hardware.quadrant_detector import (
                QuadrantDetector, TouchPositionTracker)
        except ImportError:
            logger.warning("Quadrant detector unavailable - position tracking off")
            return None

        detector = QuadrantDetector(
            thresholds=touch.get("quadrant_thresholds", None),
            hysteresis=float(touch.get("hysteresis", 20.0)),
            ema_alpha=float(touch.get("ema_alpha", 0.25)),
            magnet_strength=touch.get("magnet_strength", "strong"),
        )
        tracker = TouchPositionTracker(
            detector=detector,
            smoothing_alpha=touch.get("position_smoothing", 0.3),
            min_touch_duration_ms=touch.get("min_touch_duration_ms", 100),
        )
        return detector, tracker


class CapacitiveSensorProfile(TouchSensorProfile):
    """PLACEHOLDER - a future capacitive touch board. NOT wired up yet.

    This is a skeleton so the shape is on record; it is deliberately **not
    registered** (see the bottom of this module), so it changes nothing at
    runtime until its firmware exists. Activating it is a one-line uncomment.

    Two possible hardware shapes, both handled the same way here - pick when the
    firmware is built:

    * **Own node** (``node_capacitive_sensor``): keep ``node_types`` as below and
      give it its own ``ready_status`` / ``message_type``.
    * **Folded into the direct board** (like the magnet sensing is): add
      ``"node_direct"`` to ``node_types`` and use a ``message_type`` *distinct*
      from ``"magnet"`` (dispatch is by message type, so one board can stream
      both - magnet and capacitive - side by side without clashing).

    Capacitive doesn't move a magnet in the silicone, so there is no
    chamber->touch contamination: ``supports_pressure_coupling`` stays False and
    ``build_compensator`` inherits the no-op default. Spatial position tracking
    (``build_position_tracker``) also inherits None until a capacitive-specific
    detector is written; touch *events* (press/release) already work off the
    generic ``act`` set with no extra code.
    """

    name = "capacitive"
    # TODO(firmware): confirm when the board exists. If folded into the direct
    # board, add "node_direct" here and keep a distinct message_type below.
    node_types = ("node_capacitive_sensor",)
    ready_status = "node_capacitive_sensor_ready"
    message_type = "capacitive"
    # TODO(firmware): whatever geometry the board announces at boot, if any.
    geometry_keys = ("sensors", "geometry")
    supports_pressure_coupling = False

    def read_magnitudes(self, data: Mapping[str, Any],
                        count: int) -> list[float] | None:
        """Template extractor - reads a provisional ``cap`` list of per-sensor
        readings. TODO(firmware): confirm the field name, units, and whether a
        baseline is already subtracted on-device (mirror ``mag`` if so)."""
        raw = data.get("cap")
        if not isinstance(raw, (list, tuple)) or len(raw) < count:
            return None
        try:
            vals = [float(v) for v in raw[:count]]
        except (TypeError, ValueError):
            return None
        return [max(0.0, v) for v in vals]

    # build_compensator  -> inherits None (no pressure coupling on capacitive)
    # build_position_tracker -> inherits None (write a capacitive detector here
    #                           only if/when spatial position is needed)


class TouchSensorRegistry:
    """Lookup of the known :class:`TouchSensorProfile` strategies.

    Collaborators ask the registry rather than branching on sensor strings:
    the controller dispatches by ``message_type``/``ready_status``, the skin
    picks a profile from its ``touch.sensor`` field. Registering a new profile
    is the single edit that teaches the whole stack about a new technology.
    """

    def __init__(self) -> None:
        self._profiles: list[TouchSensorProfile] = []
        self._default: TouchSensorProfile | None = None

    def register(self, profile: TouchSensorProfile, *, default: bool = False) -> None:
        """Add a profile. The first registered (or ``default=True``) becomes the
        fallback used when a skin doesn't name its sensor technology."""
        self._profiles.append(profile)
        if default or self._default is None:
            self._default = profile

    @property
    def default(self) -> TouchSensorProfile:
        """The fallback profile for touch configs with no ``sensor`` field."""
        if self._default is None:
            raise RuntimeError("no touch sensor profiles registered")
        return self._default

    def all(self) -> tuple[TouchSensorProfile, ...]:
        return tuple(self._profiles)

    def for_name(self, name: str | None) -> TouchSensorProfile | None:
        """Profile whose :attr:`~TouchSensorProfile.name` matches, else ``None``."""
        return next((p for p in self._profiles if p.name == name), None)

    def for_config(self, touch: Mapping[str, Any] | None) -> TouchSensorProfile:
        """Profile a skin's ``touch`` block selects via its ``sensor`` field,
        falling back to :attr:`default` (magnet) when unset/unknown."""
        if touch:
            named = self.for_name(touch.get("sensor"))
            if named is not None:
                return named
        return self.default

    def for_node_type(self, node_type: str | None) -> TouchSensorProfile | None:
        """Profile a board of ``node_type`` streams, else ``None``."""
        return next((p for p in self._profiles
                     if node_type in p.node_types), None)

    def for_message_type(self, message_type: str | None) -> TouchSensorProfile | None:
        """Profile whose live-message ``type`` matches, else ``None``."""
        if not message_type:
            return None
        return next((p for p in self._profiles
                     if p.message_type == message_type), None)

    def for_ready_status(self, status: str | None) -> TouchSensorProfile | None:
        """Profile whose boot-``ready`` status matches, else ``None``."""
        if not status:
            return None
        return next((p for p in self._profiles
                     if p.ready_status and p.ready_status == status), None)

    def is_message_type(self, message_type: str | None) -> bool:
        """True if ``message_type`` is a touch reading from any known sensor."""
        return self.for_message_type(message_type) is not None

    def node_types(self) -> tuple[str, ...]:
        """Union of every ``node_type`` that can serve as a touch node."""
        seen: list[str] = []
        for p in self._profiles:
            for nt in p.node_types:
                if nt not in seen:
                    seen.append(nt)
        return tuple(seen)


# The process-wide registry. Import this and ask it; register new technologies
# here so the whole stack picks them up at once.
touch_profiles = TouchSensorRegistry()
touch_profiles.register(MagnetSensorProfile(), default=True)
# Uncomment once the capacitive firmware exists (see CapacitiveSensorProfile).
# Also add its node_type to MAGNET_NODE_TYPES in src/core/skin_config.py so the
# skin config dialog offers the board as a touch node.
# touch_profiles.register(CapacitiveSensorProfile())
