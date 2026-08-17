"""ThymioGatewayLink - drive a Thymio through the gateway's C6, with NO RF dongle.

Same duck-typed interface as :class:`~src.robots.thymio.thymio_link.ThymioLink`
(``connect`` / ``close`` / ``set_motors`` / ``set_leds`` / ``connected``), so
:class:`~src.robots.thymio.thymio_robot.ThymioRobot` neither knows nor cares which
transport it was handed. Where ``ThymioLink`` talks to the RF dongle via thymiodirect,
this one drives the Thymio over **802.15.4 through the gateway's C6**:

* ``connect`` turns on the C6 firmware's continuous ``thymio_link`` - the C6 then polls
  the Thymio at ~10 Hz on its own to hold its receive window open (the dongle's job),
* ``set_motors`` / ``set_leds`` just push the held targets (``thymio_drive`` /
  ``thymio_leds``) - instant, the C6 keeps re-asserting them on every poll,
* ``close`` turns the link off (poller stops, motors to 0).

Requires the S3 gateway (`Gateway`) with its C6 running the leak-fixed ``rcp_c6``
(the C6's chip antenna is plenty - solid at 20 m through a door; no external
antenna needed). One C6 drives up to 4 Thymios: each robot is a **slot** (``index``)
on the C6, addressed by its 802.15.4 short ``address`` (e.g. ``"6a25"``). Give each
gateway-driven Thymio a distinct ``index`` + ``address`` (discover the addresses via the
sniffer - see :func:`discover_thymios`). ``address=None`` on ``index=0`` uses the C6's
built-in default (the first Thymio we decoded), so the single-Thymio flow needs no address.
"""
from __future__ import annotations

import logging
import math
from typing import Callable

from src.hardware.gateway import Gateway

logger = logging.getLogger(__name__)

# Accelerometer reading with the robot flat + still ~= (0, 0, 20) (z = gravity, ~20).
# An impact ("pancada") is a large deviation of the raw acc from this rest vector;
# a soft touch barely moves it. See memory thymio-sensors-and-sound.
ACC_REST = (0.0, 0.0, 20.0)

# Default deviation (same units as acc) above which a reading counts as an impact.
# Calibrate per robot by knocking it (the Test Thymio dialog tunes this live).
DEFAULT_IMPACT_THRESHOLD = 20.0

# Re-arm fraction: after an impact fires, the deviation must fall back below
# threshold x this before another can fire, so one knock is one event (no chatter).
_IMPACT_REARM_FRAC = 0.6

# Impact intensity levels, classified from the deviation as multiples of the (touch)
# threshold: 1 = touch (>=1x), 2 = knock (>= KNOCKx), 3 = slap (>= SLAPx). Only the base
# (touch) threshold is calibrated; the bands scale from it. NOTE: at the ~10 Hz poll
# a fast slap's peak can be under-sampled, so the level is coarse - a native on-board
# tap event would classify it more accurately (see docs / memory).
IMPACT_TOUCH, IMPACT_KNOCK, IMPACT_SLAP = 1, 2, 3
_KNOCK_MULT = 2.0
_SLAP_MULT = 3.5

# prox.ground.delta reads high over a table (reflected IR) and drops toward 0 when the
# robot is lifted off it. Below this (both ground sensors) counts as "lifted". Coarse;
# a dark/non-reflective table also reads low, so it's really an on/off-surface edge.
DEFAULT_LIFT_THRESHOLD = 150.0


class ThymioGatewayLink:
    """One Thymio driven dongle-free through the gateway's C6 (802.15.4)."""

    def __init__(self, gateway: Gateway | None, channel: int = 25,
                 index: int = 0, address: str | None = None,
                 impact_threshold: float = DEFAULT_IMPACT_THRESHOLD):
        self._gateway = gateway
        self._channel = int(channel)
        self._index = int(index)
        self._address = address        # hex short address ("6a25"), or None -> C6 default
        self._active = False
        self._registered = False       # our gateway on_message hook is installed once
        # Latest sensor reading from the C6's thymio_sensors stream (None until first).
        self._last_acc: tuple[int, int, int] | None = None
        self._last_mic: int | None = None
        self._last_ground: tuple[int, int] | None = None
        # Impact detection (edge-triggered against the deviation threshold).
        self._impact_threshold = float(impact_threshold)
        self._impact_high = False      # currently above threshold (armed-low -> fired)
        self._impact_count = 0         # monotonic knock count
        self._last_impact_level = 0    # intensity of the last knock (1/2/3), 0 = none yet
        # Lifted detection (edge-triggered against the ground threshold).
        self._lift_threshold = DEFAULT_LIFT_THRESHOLD
        self._lifted = False           # currently off the surface
        self._lifted_count = 0         # monotonic lift count
        # Observers, fired on the gateway read thread (consumers marshal to their
        # own thread as needed - e.g. the Test Thymio dialog via a Qt signal).
        self._sensor_listeners: list[Callable[..., None]] = []
        self._impact_listeners: list[Callable[[int], None]] = []
        self._lifted_listeners: list[Callable[[bool], None]] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self, timeout: float = 6.0) -> bool:
        # `timeout` is accepted for interface parity with ThymioLink; there is no
        # discovery handshake here - the C6 starts polling as soon as we ask.
        if self._gateway is None or not self._gateway.is_connected:
            logger.error("Thymio gateway link: gateway not connected - can't reach the C6")
            return False
        ok = self._gateway.send("thymio", "thymio_link", on=True, ch=self._channel)
        # Register this robot's address in its slot. Skip only for the address-less
        # slot 0, which rides the C6's built-in default (single-Thymio back-compat).
        if ok and (self._address is not None or self._index != 0):
            ok = self._gateway.send("thymio", "thymio_set", idx=self._index,
                                    addr=self._address or "6a25") and ok
        # Listen for this robot's sensor stream (acc/mic) once. The gateway keeps a
        # weak ref, so this drops when the link is GC'd; a flag stops a re-connect
        # from double-registering.
        if ok and not self._registered:
            self._gateway.on_message(self._on_gateway_message)
            self._registered = True
        self._active = ok
        if ok:
            logger.info("Thymio gateway link: C6 link on (ch %d, slot %d, addr %s)",
                        self._channel, self._index, self._address or "default")
        else:
            logger.error("Thymio gateway link: failed to start the C6 link")
        return ok

    def close(self) -> None:
        # Zero this robot's motors; the shared poller stays up for the other robots.
        if self._active and self._gateway is not None and self._gateway.is_connected:
            self._gateway.send("thymio", "thymio_drive", idx=self._index, left=0, right=0)
        self._active = False

    @property
    def connected(self) -> bool:
        return (self._active and self._gateway is not None
                and self._gateway.is_connected)

    # ------------------------------------------------------------------
    # Commands (the C6 holds these and re-asserts them every poll)
    # ------------------------------------------------------------------

    def set_motors(self, left: int, right: int) -> bool:
        gw = self._gateway
        if gw is None or not self.connected:
            return False
        return gw.send("thymio", "thymio_drive", idx=self._index,
                       left=int(left), right=int(right))

    def set_leds(self, r: int, g: int, b: int) -> bool:
        gw = self._gateway
        if gw is None or not self.connected:
            return False
        return gw.send("thymio", "thymio_leds", idx=self._index,
                       r=int(r), g=int(g), b=int(b))

    # ------------------------------------------------------------------
    # Sensors & impact (the C6 streams thymio_sensors from its poll reply)
    # ------------------------------------------------------------------

    def on_sensors(self, callback) -> None:
        """Register ``callback(acc, mic, ground)`` fired on every sensor frame.

        ``acc`` is the raw ``(x, y, z)`` accelerometer, ``mic`` the mic intensity, and
        ``ground`` the two ``prox.ground.delta`` values. Fires on the gateway's serial
        read thread - GUI callers must marshal to the GUI thread (e.g. via a signal)."""
        self._sensor_listeners.append(callback)

    def remove_sensors_listener(self, callback) -> None:
        if callback in self._sensor_listeners:
            self._sensor_listeners.remove(callback)

    def on_impact(self, callback: Callable[[int], None]) -> None:
        """Register ``callback(level)`` fired once per knock, where ``level`` is the
        intensity (1 touch / 2 knock / 3 slap). Read-thread; marshal as needed."""
        self._impact_listeners.append(callback)

    def remove_impact_listener(self, callback) -> None:
        if callback in self._impact_listeners:
            self._impact_listeners.remove(callback)

    def on_lifted(self, callback: Callable[[bool], None]) -> None:
        """Register ``callback(is_lifted)`` fired when the robot is lifted off / set
        back on the surface (edge-triggered). Read-thread; marshal as needed."""
        self._lifted_listeners.append(callback)

    def remove_lifted_listener(self, callback) -> None:
        if callback in self._lifted_listeners:
            self._lifted_listeners.remove(callback)

    @property
    def impact_threshold(self) -> float:
        return self._impact_threshold

    @impact_threshold.setter
    def impact_threshold(self, value: float) -> None:
        self._impact_threshold = max(0.0, float(value))

    @property
    def lift_threshold(self) -> float:
        return self._lift_threshold

    @lift_threshold.setter
    def lift_threshold(self, value: float) -> None:
        self._lift_threshold = max(0.0, float(value))

    @property
    def impact_count(self) -> int:
        return self._impact_count

    @property
    def last_impact_level(self) -> int:
        return self._last_impact_level

    @property
    def lifted_count(self) -> int:
        return self._lifted_count

    @property
    def is_lifted(self) -> bool:
        return self._lifted

    @property
    def last_sensors(self):
        """The most recent ``(acc, mic, ground)`` reading, all ``None`` until the first."""
        return self._last_acc, self._last_mic, self._last_ground

    @staticmethod
    def acc_deviation(acc: tuple[int, int, int]) -> float:
        """Euclidean distance of a raw acc reading from the rest vector (0,0,20)."""
        return math.sqrt(sum((a - r) ** 2 for a, r in zip(acc, ACC_REST)))

    def impact_level(self, dev: float) -> int:
        """Classify a deviation into an intensity level (1 touch / 2 knock / 3 slap),
        or 0 when below the touch threshold. Bands scale off the base threshold."""
        thr = self._impact_threshold
        if dev >= thr * _SLAP_MULT:
            return IMPACT_SLAP
        if dev >= thr * _KNOCK_MULT:
            return IMPACT_KNOCK
        if dev >= thr:
            return IMPACT_TOUCH
        return 0

    def _on_gateway_message(self, data: dict) -> None:
        """Gateway read-thread callback: parse this robot's thymio_sensors frame."""
        if (data.get("source") != "thymio"
                or data.get("type") != "thymio_sensors"
                or int(data.get("idx", 0)) != self._index):
            return
        acc = data.get("acc")
        if not isinstance(acc, list) or len(acc) != 3:
            return
        acc = (int(acc[0]), int(acc[1]), int(acc[2]))
        mic = int(data.get("mic", 0))
        gnd = data.get("ground")
        ground = ((int(gnd[0]), int(gnd[1]))
                  if isinstance(gnd, list) and len(gnd) == 2 else self._last_ground)
        self._last_acc, self._last_mic, self._last_ground = acc, mic, ground
        self._detect_impact(acc)
        if ground is not None:
            self._detect_lifted(ground)
        for cb in list(self._sensor_listeners):
            self._safe(cb, acc, mic, ground)

    def _detect_impact(self, acc: tuple[int, int, int]) -> None:
        """Edge-trigger an impact when the deviation crosses the threshold up, with
        hysteresis so one knock fires once and settling re-arms it. The fired
        ``level`` classifies the intensity (touch / knock / slap)."""
        dev = self.acc_deviation(acc)
        thr = self._impact_threshold
        if not self._impact_high and dev >= thr:
            self._impact_high = True
            self._impact_count += 1
            level = self.impact_level(dev)
            self._last_impact_level = level
            for cb in list(self._impact_listeners):
                self._safe(cb, level)
        elif self._impact_high and dev < thr * _IMPACT_REARM_FRAC:
            self._impact_high = False

    def _detect_lifted(self, ground: tuple[int, int]) -> None:
        """Edge-trigger lifted/landed when both ground sensors cross the threshold."""
        low = max(ground) < self._lift_threshold      # both sensors below -> off-surface
        if low and not self._lifted:
            self._lifted = True
            self._lifted_count += 1
            for cb in list(self._lifted_listeners):
                self._safe(cb, True)
        elif not low and self._lifted:
            self._lifted = False
            for cb in list(self._lifted_listeners):
                self._safe(cb, False)

    @staticmethod
    def _safe(cb, *args) -> None:
        try:
            cb(*args)
        except Exception:   # noqa: BLE001 - a bad listener must not kill the stream
            logger.exception("Thymio sensor listener failed")

    def play_sound(self, system: int | None = None, freq: int | None = None,
                   duration_ms: int = 500, track: int | None = None) -> bool:
        """Play a sound: a built-in ``system`` sound (0-7, -1 stops), a ``freq`` Hz tone,
        or a ``track`` recorded on the Thymio's microSD (sound.play, needs a card).

        The C6 loads a tiny Aseba program that calls the sound native fn and runs it -
        the motor targets are untouched, so a driving robot keeps driving.
        """
        gw = self._gateway
        if gw is None or not self.connected:
            return False
        if track is not None:
            return gw.send("thymio", "thymio_sound", idx=self._index,
                           track=int(track))
        if freq is not None:
            dur60 = max(1, round(duration_ms * 60 / 1000))   # Thymio dur unit = 1/60 s
            return gw.send("thymio", "thymio_sound", idx=self._index,
                           freq=int(freq), dur=dur60)
        return gw.send("thymio", "thymio_sound", idx=self._index,
                       sys=int(system if system is not None else 0))

    def stop(self) -> bool:
        return self.set_motors(0, 0)
