"""High-level controller for a single ESP32 node via the SoftEdIBO gateway."""

import logging
import threading
import time
from typing import Any, Callable

from src.hardware.command_confirmer import CommandConfirmer
from src.hardware.hold_duty import clamp_hold_duty
from src.hardware.gateway import Gateway
from src.hardware.fill_scaling import FillLoadTracker
from src.hardware.touch_profiles import touch_profiles

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
        # Confirmed (ACK'd, retransmitted) delivery for this node's set-once
        # safety limits, so a dropped set_max/set_min can't leave the node on a
        # stale ceiling (the 20->50 kPa over-inflation). See confirm_limits().
        self._confirmer = CommandConfirmer(gateway, mac_address)
        # Active leak-compensating holds ({chamber: hold_duty payload}), kept
        # alive by a background thread (see start_hold): the firmware drops a
        # hold not refreshed for ~6 s, so the pose can't outlive the app.
        self._holds: dict[int, dict[str, Any]] = {}
        self._holds_lock = threading.Lock()
        self._hold_thread: threading.Thread | None = None

        self._gateway.on_message(self._handle_message)

    @property
    def is_connected(self) -> bool:
        """True if the underlying gateway is connected."""
        return self._gateway.is_connected

    def send_command(self, command: str, **kwargs: Any) -> bool:
        """Send a command to this ESP32 node."""
        return self._gateway.send(self.mac_address, command, **kwargs)

    def inflate(self, chamber: int, delta: int = 10,
                ms: int | None = None, duty: int | None = None,
                timed: bool = False) -> bool:
        """Inflate a chamber by delta % of its [min, max] range (0-100).

        When ``ms`` is given it becomes the chamber's open-time budget on the
        node (from a calibrated fill curve); the target still closes on the
        gauge. The firmware's round/sequence caps, the actuation watchdog and
        the chamber's HARD_MAX pressure still bound it.

        ``duty`` (1-255) is forwarded as the requested pump PWM duty, but
        neither board's pump control applies it on this path yet (FIXME.md).

        ``timed=True`` marks the node as having NO pressure sensor populated
        (bench board awaiting sensors): the firmware runs the fill fully
        open-loop on ``ms`` and ignores its (floating-pin noise) gauge readings
        entirely. Requires ``ms``.
        """
        payload: dict[str, Any] = {"chamber": chamber, "delta": delta}
        if ms is not None:
            payload["ms"] = int(ms)
        if duty is not None:
            payload["duty"] = max(1, min(255, int(duty)))
        if timed:
            payload["timed"] = 1
        return self.send_command("inflate", **payload)

    def deflate(self, chamber: int, delta: int = 10,
                ms: int | None = None, duty: int | None = None,
                timed: bool = False) -> bool:
        """Deflate a chamber by delta % of its [min, max] range (0-100).

        When ``ms`` is given it becomes the chamber's open-time budget on the
        node - the closing authority for a target the gauge can't see (below the
        sensor floor), timed from the calibrated deflate curve. The firmware
        still caps it and holds the HARD limits.

        ``duty`` (1-255) optionally lowers the deflate (vacuum) pump's PWM duty
        so the chamber empties more slowly; omit it for full speed.

        ``timed=True`` runs the pull fully open-loop on ``ms`` for a node with
        no pressure sensor populated (see :meth:`inflate`). Without it a
        sensorless node's firmware drops the deflate outright - its floating
        gauge reads ~min pressure, so the below-target guard never passes.
        """
        payload: dict[str, Any] = {"chamber": chamber, "delta": delta}
        if ms is not None:
            payload["ms"] = int(ms)
        if duty is not None:
            payload["duty"] = max(1, min(255, int(duty)))
        if timed:
            payload["timed"] = 1
        return self.send_command("deflate", **payload)

    @property
    def sensor_floor_kpa(self) -> float:
        """Lowest pressure this node's gauge can see (see ``_sensor_floor_kpa``)."""
        return self._sensor_floor_kpa

    def hold(self, chamber: int) -> bool:
        """Hold pressure - stop pump, close inflate and deflate valves for this chamber.

        Also ends any leak-compensating regulated hold on the chamber (the
        firmware drops it on ``hold`` too; this stops the PC keepalive)."""
        self.stop_hold(chamber)
        return self.send_command("hold", chamber=chamber)

    # ------------------------------------------------------------------
    # Leak-compensating regulated hold (firmware ``hold_duty``)
    # ------------------------------------------------------------------

    # PC keepalive cadence for active holds. The firmware drops a hold not
    # refreshed for ~6 s (its dead-man), so ~2 s survives a couple of dropped
    # ESP-NOW frames while still dying quickly if the app goes away.
    _HOLD_KEEPALIVE_S = 2.0

    def start_hold(self, chamber: int, duty: int, kpa: float | None = None,
                   timed: bool = False, vacuum: bool = False) -> bool:
        """Start (or retune) a leak-compensating hold on ``chamber``.

        Given ``kpa`` the node regulates the chamber on its gauge from the
        losses it measures: a tight chamber stays closed and unpumped, a
        leaky one keeps its valve open with the side's pump PWM servoed
        continuously (never under :data:`HOLD_DUTY_MIN`, the run floor) to
        balance the loss - the pressure side by default, the vacuum side
        (deflate valve + vacuum pump) with ``vacuum=True`` for a pose below
        ambient. ``duty`` only seeds that servo: the calibrated equilibrium
        PWM when known, else 0 and the node predicts its own from the loss it
        measures before opening. ``timed=True`` (or no ``kpa``) keeps the
        valve open at ``duty`` for sensorless boards. A background keepalive
        re-asserts the hold every ~2 s until :meth:`stop_hold`; without it
        the firmware dead-man releases the hold in ~6 s.
        """
        payload: dict[str, Any] = {"chamber": int(chamber),
                                   "duty": clamp_hold_duty(duty)}
        if kpa is not None and not timed:
            payload["kpa"] = round(float(kpa), 2)
        if timed:
            payload["timed"] = 1
        if vacuum:
            payload["dir"] = 1
        # Sends happen under the lock so a keepalive re-assert can never land
        # after a stop_hold "off" and re-arm a just-released hold.
        with self._holds_lock:
            self._holds[int(chamber)] = payload
            self._ensure_hold_keepalive()
            return self.send_command("hold_duty", **payload)

    def stop_hold(self, chamber: int | None = None) -> None:
        """End a regulated hold (all of this node's holds when ``chamber`` is
        None): stop the keepalive and tell the node to drop it."""
        with self._holds_lock:
            if chamber is None:
                had = bool(self._holds)
                self._holds.clear()
            else:
                had = self._holds.pop(int(chamber), None) is not None
            if had:
                self.send_command("hold_duty",
                                  chamber=-1 if chamber is None else int(chamber),
                                  off=1)

    def active_holds(self) -> list[int]:
        """Chambers currently under a PC-kept regulated hold."""
        with self._holds_lock:
            return sorted(self._holds)

    def _ensure_hold_keepalive(self) -> None:
        """Start the keepalive thread if not running (holds_lock held)."""
        t = self._hold_thread
        if t is not None and t.is_alive():
            return
        self._hold_thread = threading.Thread(
            target=self._hold_keepalive_loop,
            name=f"hold-keepalive-{self.mac_address}", daemon=True)
        self._hold_thread.start()

    def _hold_keepalive_loop(self) -> None:
        """Re-assert every active hold until none remain, then exit."""
        while True:
            time.sleep(self._HOLD_KEEPALIVE_S)
            with self._holds_lock:
                if not self._holds:
                    self._hold_thread = None
                    return
                for p in self._holds.values():
                    self.send_command("hold_duty", **p)

    def emergency_stop(self) -> bool:
        """Latch every actuator on this node OFF - all pumps off, all valves closed.

        The node stays stopped (ignoring inflate/deflate/etc.) until ``resume()``
        re-arms it, so the firmware holds the safe state even if the app crashes
        or the gateway link drops afterwards.
        """
        # Kill the PC keepalive too (the firmware aborts its holds on stop; the
        # keepalive must not re-establish them the moment the node is resumed).
        with self._holds_lock:
            self._holds.clear()
        return self.send_command("stop")

    def resume(self) -> bool:
        """Re-arm the node after an :meth:`emergency_stop` so it accepts commands again."""
        return self.send_command("resume")

    def set_pressure(self, chamber: int, value: int,
                     duty: int | None = None) -> bool:
        """Set target pressure for a chamber as 0-100 % of its [min, max] range.

        ``duty`` (1-255) is forwarded as the requested pump PWM duty, but
        neither board's pump control applies it on this path yet (FIXME.md).
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

    def set_max_pressure_confirmed(self, chamber: int, value: float) -> bool:
        """Like :meth:`set_max_pressure`, but block until the node ACKs the new
        limit, retransmitting on loss (see :class:`CommandConfirmer`).

        Returns ``False`` if the node never confirms (a stale ceiling risks
        over-inflation, so the caller should warn) or rejects it. Blocks the
        calling thread up to ~0.8 s - run it off the GUI/actuation thread
        (:meth:`confirm_limits` does that)."""
        return self._confirmer.confirm("set_max_pressure",
                                       chamber=int(chamber), value=float(value))

    def set_min_pressure_confirmed(self, chamber: int, value: float) -> bool:
        """Like :meth:`set_min_pressure`, but confirmed (ACK'd + retransmitted);
        see :meth:`set_max_pressure_confirmed`."""
        return self._confirmer.confirm("set_min_pressure",
                                       chamber=int(chamber), value=float(value))

    def confirm_limits(self, chamber: int, max_pressure: float,
                       min_pressure: float, *,
                       on_result: Callable[[bool], None] | None = None) -> None:
        """Reliably apply a chamber's max+min safety limits on the node, off-thread.

        Runs the confirmed (ACK'd, retransmitted) pushes on a background daemon
        thread so the caller never blocks, then invokes ``on_result(ok)`` where
        ``ok`` means both limits were confirmed. Max is sent before min so the
        firmware validates the min against the fresh max (mirrors the
        fire-and-forget order in :meth:`set_max_pressure`). On failure it logs a
        warning; the session's pre-actuation re-push stays a fire-and-forget
        backstop, so a briefly-unreachable node still self-heals on the next
        actuation."""
        def _worker() -> None:
            ok_max = self.set_max_pressure_confirmed(chamber, max_pressure)
            ok_min = self.set_min_pressure_confirmed(chamber, min_pressure)
            ok = ok_max and ok_min
            if not ok:
                logger.warning(
                    "Couldn't confirm limits on %s chamber %d (max ok=%s, "
                    "min ok=%s) - node may be unreachable; the pre-actuation "
                    "re-push still self-heals", self.mac_address, chamber,
                    ok_max, ok_min)
            if on_result is not None:
                on_result(ok)

        threading.Thread(target=_worker, daemon=True,
                         name=f"confirm-limits-{self.mac_address}").start()

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
                ``[]`` clears them on the node; ``None`` leaves them unchanged.
        """
        payload: dict[str, Any] = {"num_chambers": int(num_chambers)}
        if organ_channels is not None:
            payload["organ_channels"] = [int(c) for c in organ_channels]
        return self.send_command("configure", **payload)

    def debug(self) -> bool:
        """Request a debug snapshot from the node (debug firmware only)."""
        return self.send_command("debug")

    def set_led_angles(self, angles: dict[int, float] | None) -> None:
        """Set the per-ring LED mounting angles (degrees) from the skin config.

        Keys are ring indices (0 for a single-ring node); the angle is added to
        every LED command's ``angle`` so a physically-rotated ring reads right
        without each activity compensating. ``None`` / empty clears the offset."""
        self._led_angles = {int(k): float(v) for k, v in (angles or {}).items()}

    @property
    def led_angles(self) -> dict[int, float]:
        """The per-ring mounting angles currently applied (see set_led_angles)."""
        return dict(self._led_angles)

    def _effective_angle(self, ring: int | None, angle: float | None) -> float | None:
        """Combine the ring's saved mounting angle with the command's angle.
        Returns None when both are zero/absent (so no ``angle`` is sent)."""
        base = self._led_angles.get(0 if ring is None else int(ring), 0.0)
        total = base + (float(angle) if angle is not None else 0.0)
        return total if total else None

    def set_led(self, color: str, pattern: str = "solid",
                period_ms: int = 0, count: int | None = None,
                index: int | None = None, ring: int | None = None,
                fade_ms: int | None = None, angle: float | None = None,
                color2: str | None = None) -> bool:
        """Drive the node's LED ring(s).

        color:   "#RRGGBB". pattern: "off" | "solid" | "blink" | "pulse" |
                 "comet" (a single bright head with a fading tail sweeping the ring) |
                 "fade" (cross-fade back and forth between color and color2).
        color2:  second colour for the "fade" pattern; ignored by the others. The
                 node runs the interpolation, so a continuous fade is one frame per
                 cycle instead of a per-step colour stream over ESP-NOW.
        period_ms/count: animation timing - pulse/blink/fade cycle or comet revolution.
        index:   when given, set just that pixel (solid); otherwise the whole
                 ring. Per-pixel is used by the LED test panel.
        ring:    multi-ring nodes (node_multiplexed: 3 rings) only - selects ring
                 0..2; omitted addresses all rings. Single-ring nodes ignore it.
        fade_ms: cross-fade time for this change. Every change cross-fades; the
                 node's default (~250 ms) applies when omitted, 0 snaps instantly.
        angle:   0-360 deg rotation of the split/comet around the ring (0 = default
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
        if color2 is not None:
            kwargs["color2"] = color2
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
        give two comets 180 deg apart). ``ring`` selects one of the multiplexed
        board's three rings (0..2); omitted addresses all rings, and single-ring
        boards ignore it. ``fade_ms`` is the cross-fade time for this change
        (node default ~250 ms when omitted, 0 snaps). ``angle`` (0-360 deg) rotates
        the split around the ring, so e.g. halves can sit top/bottom instead of
        left/right.

        This used to loop one ``set_led(index=...)`` per LED - a one-frame-per-LED burst
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

    def set_led_pixels(self, colors: list[str], mask: str,
                       pattern: str = "solid", period_ms: int = 0,
                       ring: int | None = None,
                       fade_ms: int | None = None) -> bool:
        """Paint an arbitrary per-pixel colour selection in ONE frame.

        ``colors`` is up to four ``"#RRGGBB"`` entries and ``mask`` the packed
        2-bit-per-pixel colour index (see
        :func:`src.core.led_geometry.encode_pixel_mask`; index 0 = ``colors[0]``,
        the background). Pixels past the mask, or whose index has no colour,
        go dark. This is how the zone/sync fill blocks light scattered pixels
        of a 68-LED strip without a per-pixel burst. No mounting ``angle`` is
        applied: the PC already resolved pixel indices from the skin's
        geometry. ``pattern`` (solid/blink/pulse) and ``period_ms`` animate the
        whole selection; ``ring`` / ``fade_ms`` as for :meth:`set_led`."""
        cols = [str(c) for c in colors][:4]
        if not cols or not mask:
            return False
        kwargs: dict[str, Any] = {"colors": cols, "mask": str(mask),
                                  "pattern": pattern, "period_ms": int(period_ms)}
        if ring is not None:
            kwargs["ring"] = int(ring)
        if fade_ms is not None:
            kwargs["fade_ms"] = int(fade_ms)
        return self.send_command("set_led_pixels", **kwargs)

    def set_led_config(self, brightness: int, ring: int | None = None) -> bool:
        """Set the node's LED brightness cap (1..255) - board state pushed from
        the skin's ``led_layout`` at build/claim, like the mounting angles. A
        cut strip can draw far more than the old 16-LED ring, so a skin may
        cap it below full. ``ring`` selects one of the multiplexed board's
        rings; omitted applies to all."""
        kwargs: dict[str, Any] = {"brightness": max(1, min(255, int(brightness)))}
        if ring is not None:
            kwargs["ring"] = int(ring)
        return self.send_command("led_config", **kwargs)

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

    def remove_magnet_listener(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Deregister a callback passed to :meth:`on_magnet` (no-op if absent)."""
        self._magnet_callbacks[:] = [cb for cb in self._magnet_callbacks if cb != callback]

    def remove_organ_listener(self, callback: Callable[[float, int], None]) -> None:
        """Deregister a callback passed to :meth:`on_organ` (no-op if absent)."""
        self._organ_callbacks[:] = [cb for cb in self._organ_callbacks if cb != callback]

    def get_last_status(self) -> dict[str, Any]:
        """Get the last known status of this ESP32 node."""
        return self._last_status.copy()

    @staticmethod
    def _call_callbacks(callbacks: list, *args: Any) -> None:
        """Call each callback, pruning any whose Qt signal source has been deleted.

        Iterates a snapshot and prunes by identity: listeners are added/removed
        on the GUI thread while this runs on the gateway read thread."""
        dead: list = []
        for callback in list(callbacks):
            try:
                callback(*args)
            except RuntimeError:
                dead.append(callback)
        if dead:
            callbacks[:] = [cb for cb in callbacks if all(cb is not d for d in dead)]

    def _dispatch_status_batch(self, data: dict[str, Any]) -> None:
        """Expand a batched status frame into per-chamber dispatches.

        New actuator firmware sends every chamber in ONE ESP-NOW frame as parallel
        arrays - ``{"type":"status","kpa":[..],"st":[..],"vi":[..],"vd":[..]}`` - to cut
        the per-chamber frame count (less ESP-NOW airtime, which the Thymio's co-channel
        802.15.4 shares). The per-chamber ``pressure`` % is not sent: it's redundant, so
        we pass 0 and the consumer recomputes it from the authoritative ``kpa`` (see
        :meth:`_dispatch_chamber_pressure`). Older nodes still send one scalar frame per
        chamber, handled there directly - both wire forms stay supported.
        """
        kpa = data.get("kpa") or []
        st = data.get("st") or []
        vi = data.get("vi") or []
        vd = data.get("vd") or []
        for i, k in enumerate(kpa):
            per = {
                "type": "status", "source": data.get("source"), "chamber": i,
                "pressure": 0,   # recomputed from kpa downstream (kpa is authoritative)
                "kpa": k,
                "st": st[i] if i < len(st) else None,
                "vi": vi[i] if i < len(vi) else None,
                "vd": vd[i] if i < len(vd) else None,
            }
            self._last_status.update(per)
            self._dispatch_chamber_pressure(per)

    def _dispatch_chamber_pressure(self, data: dict[str, Any]) -> None:
        chamber_id = int(data["chamber"])
        pressure = int(data["pressure"])
        # ``st`` is the firmware-reported actuation state (0 idle, 1 inflating,
        # 2 deflating); absent on older firmware -> None (the consumer then
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

            elif (ready := touch_profiles.for_ready_status(data.get("status"))) is not None:
                # A touch board announced itself at boot. Cache the geometry it
                # self-describes (per its profile) so subscribers (skin grid
                # panels, calibration UI, ...) can read it later.
                self._magnet_geometry = {
                    k: data[k] for k in ready.geometry_keys if k in data}
                logger.info("%s sensor ready from %s: %s",
                            ready.name, self.mac_address, self._magnet_geometry)

            elif data.get("type") == "status" and isinstance(data.get("kpa"), list):
                # Batched status: new firmware sends every chamber in one frame
                # (parallel arrays). Expand to the per-chamber form below.
                self._dispatch_status_batch(data)

            elif data.get("type") == "status" and "chamber" in data and "pressure" in data:
                # Scalar status: one frame per chamber (older firmware).
                self._dispatch_chamber_pressure(data)

            elif touch_profiles.is_message_type(data.get("type")):
                self._dispatch_magnet(data)

            elif data.get("type") == "organ":
                self._dispatch_organ(data)

    def __repr__(self) -> str:
        return f"ESP32Controller(mac={self.mac_address!r})"
