"""High-level controller for a single ESP32 node via the SoftEdIBO gateway."""

import logging
from typing import Any, Callable

from src.hardware.gateway import Gateway
from src.hardware.fill_scaling import FillLoadTracker

logger = logging.getLogger(__name__)


class ESP32Controller:
    """Controls a single remote ESP32 node through the gateway."""

    def __init__(self, mac_address: str, gateway: Gateway):
        self.mac_address = mac_address
        self._gateway = gateway
        # Shared per-node fill-load tracker: every Skin on this node consults it
        # to scale calibrated fill times for concurrent inflation (pumps are
        # shared per node). ``pump_count`` is set by the robot builder.
        self.fill_load = FillLoadTracker()
        self._last_status: dict[str, Any] = {}
        self._touch_callbacks:    list[Callable[[int, int], None]] = []
        self._pressure_callbacks: list[Callable[[int, int], None]] = []
        self._magnet_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._organ_callbacks: list[Callable[[float, int], None]] = []
        # Latest magnet sensor geometry, captured from a `node_magnet_sensor_ready` boot announce.
        # Shape: {"sensors": N, "magnets": M, "variant": str|None, "geometry": {...}}.
        self._magnet_geometry: dict[str, Any] | None = None
        # Gauge floor (kPa) self-reported by the node in ready/pong ("kpa_min"):
        # the lowest pressure its sensor can see. 0 for today's 0..100 kPa parts
        # and for old firmware that doesn't report it; -40 once the vacuum-capable
        # sensors arrive. Below this a deflate needs a time budget, not the gauge.
        self._sensor_floor_kpa: float = 0.0
        # Per-ring LED mounting angle (degrees), from the skin config. Added to
        # every LED command's angle so a physically-rotated ring shows the right
        # orientation without every activity having to compensate. Empty = no offset.
        self._led_angles: dict[int, float] = {}

        self._gateway.on_message(self._handle_message)

    @property
    def is_connected(self) -> bool:
        """True if the underlying gateway is connected."""
        return self._gateway.is_connected

    def send_command(self, command: str, **kwargs: Any) -> bool:
        """Send a command to this ESP32 node."""
        return self._gateway.send(self.mac_address, command, **kwargs)

    def inflate(self, chamber: int, delta: int = 10,
                ms: int | None = None, duty: int | None = None) -> bool:
        """Inflate a chamber by delta % of its max pressure (0-100).

        When ``ms`` is given, the node inflates for that many milliseconds
        (time-based fill from a calibrated ``fill_time_ms``) instead of closing
        the loop on the pressure sensor; the firmware still caps at 5 s and at
        the chamber's HARD_MAX pressure.

        ``duty`` (1-255) optionally lowers the inflate pump's PWM duty so the
        chamber fills more slowly; omit it (or pass None) for full speed.
        """
        payload: dict[str, Any] = {"chamber": chamber, "delta": delta}
        if ms is not None:
            payload["ms"] = int(ms)
        if duty is not None:
            payload["duty"] = max(1, min(255, int(duty)))
        return self.send_command("inflate", **payload)

    def deflate(self, chamber: int, delta: int = 10,
                ms: int | None = None, duty: int | None = None) -> bool:
        """Deflate a chamber by delta % of its max pressure (0-100).

        When ``ms`` is given it becomes the chamber's open-time budget on the
        node — the closing authority for a target the gauge can't see (below the
        sensor floor), timed from the calibrated deflate curve. The firmware
        still caps it and holds the HARD limits.

        ``duty`` (1-255) optionally lowers the deflate (vacuum) pump's PWM duty
        so the chamber empties more slowly; omit it for full speed.
        """
        payload: dict[str, Any] = {"chamber": chamber, "delta": delta}
        if ms is not None:
            payload["ms"] = int(ms)
        if duty is not None:
            payload["duty"] = max(1, min(255, int(duty)))
        return self.send_command("deflate", **payload)

    @property
    def sensor_floor_kpa(self) -> float:
        """Lowest pressure this node's gauge can see (see ``_sensor_floor_kpa``)."""
        return self._sensor_floor_kpa

    def hold(self, chamber: int) -> bool:
        """Hold pressure — stop pump, close inflate and deflate valves for this chamber."""
        return self.send_command("hold", chamber=chamber)

    def emergency_stop(self) -> bool:
        """Latch every actuator on this node OFF — all pumps off, all valves closed.

        The node stays stopped (ignoring inflate/deflate/etc.) until ``resume()``
        re-arms it, so the firmware holds the safe state even if the app crashes
        or the gateway link drops afterwards.
        """
        return self.send_command("stop")

    def resume(self) -> bool:
        """Re-arm the node after an :meth:`emergency_stop` so it accepts commands again."""
        return self.send_command("resume")

    def set_pressure(self, chamber: int, value: int,
                     duty: int | None = None) -> bool:
        """Set target pressure for a chamber as 0-100 % of that chamber max.

        ``duty`` (1-255) optionally lowers the pump's PWM duty so the chamber
        approaches the target gently; the pressure cutoff still stops at target.
        """
        payload: dict[str, Any] = {"chamber": chamber, "value": value}
        if duty is not None:
            payload["duty"] = max(1, min(255, int(duty)))
        return self.send_command("set_pressure", **payload)

    def set_max_pressure(self, chamber: int, value: float) -> bool:
        """Set per-chamber max pressure on the ESP32 node (kPa).

        The node refuses to inflate past this limit, even if the app crashes.
        """
        return self.send_command("set_max_pressure", chamber=chamber, value=float(value))

    def set_min_pressure(self, chamber: int, value: float) -> bool:
        """Set per-chamber min pressure on the ESP32 node (kPa).

        Sets the lowest pressure the firmware will deflate to. Defaults to 0 kPa
        for chambers without vacuum supply; for chambers fed by a vacuum tank
        this is typically negative (e.g. -5 kPa).
        """
        return self.send_command("set_min_pressure", chamber=chamber, value=float(value))

    def configure(
        self,
        num_chambers: int,
        *,
        organ_channels: list[int] | None = None,
    ) -> bool:
        """Configure a multiplexed node at runtime.

        Args:
            num_chambers: Active chamber count for this node.
            organ_channels: Mux channels carrying organ+cover circuits; the
                index in this list becomes the ``slot`` in organ broadcasts.
        """
        payload: dict[str, Any] = {"num_chambers": int(num_chambers)}
        if organ_channels:
            payload["organ_channels"] = [int(c) for c in organ_channels]
        return self.send_command("configure", **payload)

    def debug(self) -> bool:
        """Request a debug snapshot from the node (debug firmware only)."""
        return self.send_command("debug")

    def calibrate_sensor(self, sensor_id: int) -> bool:
        """Request sensor calibration on the ESP32."""
        return self.send_command("calibrate_sensor", sensor=sensor_id)

    def set_led_angles(self, angles: dict[int, float] | None) -> None:
        """Set the per-ring LED mounting angles (degrees) from the skin config.

        Keys are ring indices (0 for a single-ring node); the angle is added to
        every LED command's ``angle`` so a physically-rotated ring reads right
        without each activity compensating. ``None`` / empty clears the offset."""
        self._led_angles = {int(k): float(v) for k, v in (angles or {}).items()}

    def _effective_angle(self, ring: int | None, angle: float | None) -> float | None:
        """Combine the ring's saved mounting angle with the command's angle.
        Returns None when both are zero/absent (so no ``angle`` is sent)."""
        base = self._led_angles.get(0 if ring is None else int(ring), 0.0)
        total = base + (float(angle) if angle is not None else 0.0)
        return total if total else None

    def set_led(self, color: str, pattern: str = "solid",
                period_ms: int = 0, count: int | None = None,
                index: int | None = None, ring: int | None = None,
                fade_ms: int | None = None, angle: float | None = None) -> bool:
        """Drive the node's LED ring(s).

        color:   "#RRGGBB". pattern: "off" | "solid" | "blink" | "pulse" |
                 "comet" (a single bright head with a fading tail sweeping the ring).
        period_ms/count: animation timing — pulse/blink cycle or comet revolution.
        index:   when given, set just that pixel (solid); otherwise the whole
                 ring. Per-pixel is used by the LED test panel.
        ring:    multi-ring nodes (node_multiplexed: 4 rings) only — selects ring
                 0..3; omitted addresses all rings. Single-ring nodes ignore it.
        fade_ms: cross-fade time for this change. Every change cross-fades; the
                 node's default (~250 ms) applies when omitted, 0 snaps instantly.
        angle:   0-360° rotation of the split/comet around the ring (0 = default
                 orientation). Lets a comet start elsewhere; more useful on halves.
        """
        kwargs: dict[str, Any] = {"color": color, "pattern": pattern,
                                  "period_ms": int(period_ms)}
        if count is not None:
            kwargs["count"] = int(count)
        if index is not None:
            kwargs["index"] = int(index)
        if ring is not None:
            kwargs["ring"] = int(ring)
        if fade_ms is not None:
            kwargs["fade_ms"] = int(fade_ms)
        eff_angle = self._effective_angle(ring, angle)
        if eff_angle is not None:
            kwargs["angle"] = eff_angle
        return self.send_command("set_led", **kwargs)

    def set_led_halves(self, colors: list[str],
                       pattern: str = "solid", period_ms: int = 0,
                       ring: int | None = None,
                       fade_ms: int | None = None,
                       angle: float | None = None) -> bool:
        """Paint a ring split into ``len(colors)`` equal contiguous arcs.

        Used for the "half purple / half yellow" behaviour look. Sent as a
        single ``set_led_halves`` frame carrying the colour list; the firmware
        splits the ring across its own LED count and renders one frame from
        loop(). ``pattern`` / ``period_ms`` animate the whole split ring together;
        ``pattern="comet"`` paints one rotating comet per colour (so two colours
        give two comets 180° apart). ``ring`` selects one of the multiplexed
        board's four rings (0..3); omitted addresses all rings, and single-ring
        boards ignore it. ``fade_ms`` is the cross-fade time for this change
        (node default ~250 ms when omitted, 0 snaps). ``angle`` (0-360°) rotates
        the split around the ring, so e.g. halves can sit top/bottom instead of
        left/right.

        This used to loop one ``set_led(index=…)`` per LED — a 24-frame burst
        that reset the node, because the firmware calls ``strip.show()`` (which
        disables interrupts) once per pixel from the ESP-NOW receive task. The
        single-frame command does one ``show()`` off that task instead.
        """
        cols = [str(c) for c in colors]
        if not cols:
            return False
        kwargs: dict[str, Any] = {"colors": cols, "pattern": pattern,
                                  "period_ms": int(period_ms)}
        if ring is not None:
            kwargs["ring"] = int(ring)
        if fade_ms is not None:
            kwargs["fade_ms"] = int(fade_ms)
        eff_angle = self._effective_angle(ring, angle)
        if eff_angle is not None:
            kwargs["angle"] = eff_angle
        return self.send_command("set_led_halves", **kwargs)

    def on_touch(self, callback: Callable[[int, int], None]) -> None:
        """Register a callback for touch sensor events.

        Args:
            callback: Called with (sensor_id, raw_value) on each reading.
        """
        self._touch_callbacks.append(callback)

    def on_pressure(self, callback: Callable[..., None]) -> None:
        """Register a callback for pressure status messages.

        Args:
            callback: Called with ``(chamber_id, pressure, state, kpa)`` on each
                status reading. ``state`` is the firmware-reported actuation
                state (0 idle, 1 inflating, 2 deflating) or ``None`` when the
                firmware doesn't report it. ``kpa`` is the measured absolute
                pressure, or NaN when the firmware doesn't report it. Both are
                trailing optional arguments so simulator callbacks that emit only
                ``(chamber_id, pressure)`` still work.
        """
        self._pressure_callbacks.append(callback)

    @property
    def magnet_geometry(self) -> dict[str, Any] | None:
        """Last magnet sensor geometry captured from `node_magnet_sensor_ready` (None if never seen).

        Contains the fields the firmware announced at boot: ``sensors``,
        ``magnets``, ``variant``, and ``geometry`` (with ``sensors`` /
        ``magnets`` coordinate arrays).
        """
        return self._magnet_geometry

    def on_magnet(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback for magnet sensor (`type:"magnet"`) messages from this node.

        Args:
            callback: Called with the full message dict (mag, act) as sent by the
                firmware. The ``source`` MAC added by the gateway is preserved.
        """
        self._magnet_callbacks.append(callback)

    def on_organ(self, callback: Callable[[float, int], None]) -> None:
        """Register a callback for organ-resistance (`type:"organ"`) messages.

        Args:
            callback: Called with ``(resistance_ohm, slot)``. The resistance
                is the total of the organ network in ohms; an open circuit
                (silicone cover off) is delivered as ``float("inf")``. ``slot``
                is 0 on direct nodes; multiplexed nodes report one slot per
                configured organ circuit (``configure`` ``organ_channels``).
        """
        self._organ_callbacks.append(callback)

    def get_last_status(self) -> dict[str, Any]:
        """Get the last known status of this ESP32 node."""
        return self._last_status.copy()

    @staticmethod
    def _call_callbacks(callbacks: list, *args: Any) -> None:
        """Call each callback, pruning any whose Qt signal source has been deleted."""
        dead: list[int] = []
        for i, callback in enumerate(callbacks):
            try:
                callback(*args)
            except RuntimeError:
                dead.append(i)
        for i in reversed(dead):
            callbacks.pop(i)

    def _dispatch_touch(self, data: dict[str, Any]) -> None:
        sensor_id = data.get("sensor", 0)
        raw_value = data.get("value", 0)
        self._call_callbacks(self._touch_callbacks, sensor_id, raw_value)

    def _dispatch_chamber_pressure(self, data: dict[str, Any]) -> None:
        chamber_id = int(data["chamber"])
        pressure = int(data["pressure"])
        # ``st`` is the firmware-reported actuation state (0 idle, 1 inflating,
        # 2 deflating); absent on older firmware → None (the consumer then
        # infers state from pressure vs target).
        st = data.get("st")
        state = int(st) if isinstance(st, (int, float)) else None
        # ``kpa`` is the measured absolute pressure; only firmware new enough to
        # send it includes it. NaN flags "unknown" so the consumer recomputes the
        # percentage from the *configured* range when it has a kPa, and falls
        # back to the firmware ``pressure`` field when it doesn't.
        raw_kpa = data.get("kpa")
        kpa = float(raw_kpa) if isinstance(raw_kpa, (int, float)) else float("nan")
        self._call_callbacks(self._pressure_callbacks, chamber_id, pressure, state, kpa)

    def _dispatch_magnet(self, data: dict[str, Any]) -> None:
        self._call_callbacks(self._magnet_callbacks, data)

    def _dispatch_organ(self, data: dict[str, Any]) -> None:
        if data.get("open"):
            resistance = float("inf")
        else:
            resistance = float(data.get("resistance_ohm", -1.0))
        slot = int(data.get("slot", 0))
        self._call_callbacks(self._organ_callbacks, resistance, slot)

    def _handle_message(self, data: dict[str, Any]) -> None:
        """Process incoming messages, filtering for this node's MAC."""
        if data.get("source") == self.mac_address:
            self._last_status.update(data)
            logger.debug("Status from %s: %s", self.mac_address, data)

            # Gauge floor self-report, carried by ready and pong messages.
            kpa_min = data.get("kpa_min")
            if isinstance(kpa_min, (int, float)):
                self._sensor_floor_kpa = float(kpa_min)

            if data.get("type") == "debug":
                logger.info("Debug from %s: %s", self.mac_address, data)

            elif data.get("status") == "node_magnet_sensor_ready":
                # Cache the magnet sensor board's self-described geometry so subscribers
                # (skin grid panels, calibration UI, …) can read it later.
                self._magnet_geometry = {
                    k: data[k] for k in ("sensors", "magnets", "variant", "geometry")
                    if k in data
                }
                logger.info("magnet sensor ready from %s: %s", self.mac_address, self._magnet_geometry)

            elif data.get("type") == "touch":
                self._dispatch_touch(data)

            elif data.get("type") == "status" and "chamber" in data and "pressure" in data:
                self._dispatch_chamber_pressure(data)

            elif data.get("type") == "magnet":
                self._dispatch_magnet(data)

            elif data.get("type") == "organ":
                self._dispatch_organ(data)

    def __repr__(self) -> str:
        return f"ESP32Controller(mac={self.mac_address!r})"
