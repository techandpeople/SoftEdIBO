"""Pressure-sensor mapping for node_multiplexed - pure, Qt-free, testable.

The multiplexed board reads its pressure sensors through a 16-input analog
mux, and which input ends up plumbed to which chamber is a property of the
build, not of the PCB. :class:`SensorMapSession` measures it: one chamber at a
time it inflates while the node streams the raw ADC counts of every mux input
(``sensor_scan``) until one of them has risen by the target pressure, takes
that input as the chamber's sensor, and vents the chamber again before moving
on. A chamber with no sensor never gets there, so every inflate also carries a
time cap.

The session is driven from outside - :meth:`SensorMapSession.tick` on a timer
plus the node's ``raw`` / ``status`` frames - and talks to the node only
through the injected ``send(command, **params)``, so it runs without a gateway
in the tests. The resulting map is stored on the node entry
(``sensor_channels``) by :func:`set_sensor_channels` and reaches the board in
every ``configure`` (see ``multiplexed_configure``).
"""

from __future__ import annotations

import enum
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from src.core.node_sharing import node_types_by_mac, robot_ids_by_mac

MUX_CHANNELS = 16
NO_SENSOR = -1

# The inflate stops when an input has risen this far above its rest level...
DEFAULT_STOP_KPA = 15.0
MIN_STOP_KPA = 2.0
MAX_STOP_KPA = 60.0
# ...or, for a chamber no input follows, when this time is up.
DEFAULT_PULSE_MS = 3000
MIN_PULSE_MS = 200
MAX_PULSE_MS = 8000

# ADC counts: 12-bit over the gauge's 90 % output span, on the 0..100 kPa sensor.
COUNTS_PER_KPA = 0.9 * 4095 / 100.0
MIN_RISE = 60        # smallest rise that counts as "this input follows the chamber"
VENT_DONE_RISE = 30  # the matched input is back at its baseline

BASELINE_S = 0.6            # rest reading before each pulse
MIN_BASELINE_FRAMES = 3
PULSE_MARGIN_S = 0.2        # past the node's own time cap, for the frame in flight
SETTLE_S = 0.4              # after the valve closed, the input still catches up
VENT_MIN_S = 0.6
VENT_BLIND_S = 3.5          # no input to watch: vent on time alone
VENT_MAX_S = 4.0            # under the node's 5 s manual dead-man: no keepalive
SCAN_KEEPALIVE_S = 1.0      # the node drops the raw stream 3 s after the last one
STREAM_TIMEOUT_S = 2.5

NO_STREAM_ERROR = (
    "The node is not sending raw sensor readings. Check that it is online and "
    "running firmware that supports sensor mapping (sensormap-1 or newer).")


class Phase(enum.Enum):
    IDLE = "idle"
    WAIT_STREAM = "wait_stream"
    BASELINE = "baseline"
    PULSE = "pulse"
    SETTLE = "settle"
    VENT = "vent"
    DONE = "done"
    FAILED = "failed"


_RUNNING = (Phase.WAIT_STREAM, Phase.BASELINE, Phase.PULSE, Phase.SETTLE, Phase.VENT)


@dataclass(frozen=True)
class SensorMatch:
    """What one chamber's pulse showed."""
    slot: int
    channel: int      # mux input that followed the chamber, or NO_SENSOR
    rise: int         # ADC counts the best input rose (even when below MIN_RISE)
    actuated: bool    # the node reported the chamber open during the pulse

    @property
    def found(self) -> bool:
        return self.channel != NO_SENSOR

    @property
    def rise_kpa(self) -> float:
        return self.rise / COUNTS_PER_KPA


def channel_label(channel: int) -> str:
    """Human name of a mux input: the PCB's sensor reference where it has one."""
    if channel == NO_SENSOR:
        return "none"
    if channel < 12:
        return f"I{channel} (PSENSOR{channel + 1})"
    return f"I{channel} (external)"


class SensorMapSession:
    """Finds the mux input behind each chamber of one node, one pulse at a time.

    Args:
        slots: Chambers to probe, in order.
        send: ``send(command, **params)`` to the node.
        stop_kpa: Rise (above the rest level) on any input that ends a
            chamber's inflate: by then the input is unmistakably its sensor.
        pulse_ms: Longest the inflate valve stays open per chamber (the node
            enforces it; the pulse ends sooner once an input has moved).
        clock: Monotonic seconds, injectable for the tests.
    """

    def __init__(self, slots: Sequence[int], send: Callable[..., Any], *,
                 stop_kpa: float = DEFAULT_STOP_KPA,
                 pulse_ms: int = DEFAULT_PULSE_MS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._slots = list(slots)
        self._send = send
        stop_kpa = max(MIN_STOP_KPA, min(float(stop_kpa), MAX_STOP_KPA))
        self._stop_rise = stop_kpa * COUNTS_PER_KPA
        self._pulse_ms = max(MIN_PULSE_MS, min(int(pulse_ms), MAX_PULSE_MS))
        self._clock = clock
        self._phase = Phase.IDLE
        self._error = ""
        self._results: dict[int, SensorMatch] = {}
        self._index = 0
        self._phase_t = 0.0
        self._last_raw_t = 0.0
        self._last_scan_t = 0.0
        self._frames: list[list[int]] = []
        self._baseline: list[float] = [0.0] * MUX_CHANNELS
        self._peak: list[float] = [0.0] * MUX_CHANNELS
        self._vent_rise = 0.0
        self._vent_min_s = VENT_MIN_S
        self._actuated = False

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def running(self) -> bool:
        return self._phase in _RUNNING

    @property
    def finished(self) -> bool:
        return self._phase is Phase.DONE

    @property
    def error(self) -> str:
        return self._error

    @property
    def results(self) -> dict[int, SensorMatch]:
        return dict(self._results)

    @property
    def current_slot(self) -> int | None:
        if self.running and self._index < len(self._slots):
            return self._slots[self._index]
        return None

    @property
    def progress(self) -> int:
        """0-100 across all chambers."""
        if not self._slots:
            return 100
        if self._phase is Phase.DONE:
            return 100
        return int(100 * len(self._results) / len(self._slots))

    @property
    def status(self) -> str:
        slot = self.current_slot
        if self._phase is Phase.FAILED:
            return self._error
        if self._phase is Phase.DONE:
            found = sum(1 for m in self._results.values() if m.found)
            return f"Done: {found} of {len(self._results)} chambers have a sensor."
        if self._phase is Phase.WAIT_STREAM:
            return "Waiting for the node's raw sensor readings..."
        if self._phase is Phase.BASELINE:
            return f"Chamber {slot}: reading the rest level..."
        if self._phase in (Phase.PULSE, Phase.SETTLE):
            return f"Chamber {slot}: inflating, watching every input..."
        if self._phase is Phase.VENT:
            return f"Chamber {slot}: venting..."
        return "Ready."

    def channels(self) -> list[int]:
        """The measured map: index = chamber, value = mux input (NO_SENSOR = none).

        An input claimed by two chambers goes to the one it rose most for - a
        sensor sits on one chamber, the other reading was line coupling."""
        if not self._results:
            return []
        out = [NO_SENSOR] * (max(self._results) + 1)
        best: dict[int, SensorMatch] = {}
        for match in self._results.values():
            if match.found and (match.channel not in best
                                or match.rise > best[match.channel].rise):
                best[match.channel] = match
        for channel, match in best.items():
            out[match.slot] = channel
        return out

    def shared_channels(self) -> dict[int, list[int]]:
        """Inputs more than one chamber's pulse pointed at: ``{channel: [slots]}``."""
        by_channel: dict[int, list[int]] = {}
        for match in self._results.values():
            if match.found:
                by_channel.setdefault(match.channel, []).append(match.slot)
        return {ch: sorted(slots) for ch, slots in by_channel.items()
                if len(slots) > 1}

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._results = {}
        self._error = ""
        self._index = 0
        now = self._clock()
        self._last_raw_t = now
        self._scan(now)
        if not self._slots:
            self._finish()
            return
        self._enter(Phase.WAIT_STREAM, now)

    def stop(self) -> None:
        """Abort and leave the node safe (see :meth:`_make_safe`)."""
        if not self.running:
            return
        self._make_safe()
        self._phase = Phase.IDLE

    def tick(self) -> None:
        """Advance on time. Call a few times per second while running."""
        if not self.running:
            return
        now = self._clock()
        if now - self._last_raw_t >= STREAM_TIMEOUT_S:
            self._fail(NO_STREAM_ERROR)
            return
        if now - self._last_scan_t >= SCAN_KEEPALIVE_S:
            self._scan(now)
        elapsed = now - self._phase_t
        if self._phase is Phase.BASELINE:
            if elapsed >= BASELINE_S and len(self._frames) >= MIN_BASELINE_FRAMES:
                self._begin_pulse(now)
        elif self._phase is Phase.PULSE:
            if elapsed >= self._pulse_ms / 1000.0 + PULSE_MARGIN_S:
                self._enter(Phase.SETTLE, now)
        elif self._phase is Phase.SETTLE:
            if elapsed >= SETTLE_S:
                self._decide(now)
        elif self._phase is Phase.VENT:
            vented = (elapsed >= self._vent_min_s
                      and self._vent_rise <= VENT_DONE_RISE)
            if vented or elapsed >= VENT_MAX_S:
                self._next_chamber(now)

    def on_raw(self, adc: Sequence[Any]) -> None:
        """Feed one ``raw`` frame: the ADC counts of all mux inputs."""
        if not self.running or len(adc) < MUX_CHANNELS:
            return
        try:
            frame = [int(v) for v in adc[:MUX_CHANNELS]]
        except (TypeError, ValueError):
            return
        now = self._clock()
        self._last_raw_t = now
        if self._phase is Phase.WAIT_STREAM:
            self._begin_baseline(now)
            self._frames.append(frame)
        elif self._phase is Phase.BASELINE:
            self._frames.append(frame)
        elif self._phase in (Phase.PULSE, Phase.SETTLE):
            for ch in range(MUX_CHANNELS):
                self._peak[ch] = max(self._peak[ch], frame[ch] - self._baseline[ch])
            if self._phase is Phase.PULSE and max(self._peak) >= self._stop_rise:
                self._send("hold", chamber=self._slots[self._index])
                self._enter(Phase.SETTLE, now)
        elif self._phase is Phase.VENT:
            channel = self._results[self._slots[self._index]].channel
            if channel != NO_SENSOR:
                self._vent_rise = frame[channel] - self._baseline[channel]

    def on_status(self, states: Sequence[Any], inflate_open: Sequence[Any]) -> None:
        """Feed one batched status frame (per-chamber state + inflate valve)."""
        if self._phase is not Phase.PULSE:
            return
        slot = self._slots[self._index]
        if ((slot < len(states) and states[slot])
                or (slot < len(inflate_open) and inflate_open[slot])):
            self._actuated = True

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _enter(self, phase: Phase, now: float) -> None:
        self._phase = phase
        self._phase_t = now

    def _scan(self, now: float) -> None:
        self._send("sensor_scan", on=1)
        self._last_scan_t = now

    def _begin_baseline(self, now: float) -> None:
        self._frames = []
        self._enter(Phase.BASELINE, now)

    def _begin_pulse(self, now: float) -> None:
        count = len(self._frames)
        self._baseline = [sum(f[ch] for f in self._frames) / count
                          for ch in range(MUX_CHANNELS)]
        self._peak = [0.0] * MUX_CHANNELS
        self._actuated = False
        # Open-loop on the node on purpose: this chamber's gauge is exactly
        # what is unknown. The stop on the target rise (on_raw) ends it; the
        # node's time cap bounds a chamber that no input follows.
        self._send("inflate", chamber=self._slots[self._index], delta=100,
                   ms=self._pulse_ms, timed=1)
        self._enter(Phase.PULSE, now)

    def _decide(self, now: float) -> None:
        slot = self._slots[self._index]
        channel = max(range(MUX_CHANNELS), key=lambda ch: self._peak[ch])
        rise = int(round(self._peak[channel]))
        if rise >= MIN_RISE:
            # The pulse was cut short on the rise, possibly before any status
            # frame showed the valve open - the rise itself proves it opened.
            self._actuated = True
        else:
            channel = NO_SENSOR
        self._results[slot] = SensorMatch(slot, channel, rise, self._actuated)
        self._vent_rise = float(rise) if channel != NO_SENSOR else 0.0
        self._vent_min_s = VENT_MIN_S if channel != NO_SENSOR else VENT_BLIND_S
        self._send("vent", chamber=slot, open=1)
        self._enter(Phase.VENT, now)

    def _next_chamber(self, now: float) -> None:
        self._send("vent", chamber=self._slots[self._index], open=0)
        self._index += 1
        if self._index >= len(self._slots):
            self._finish()
        else:
            self._begin_baseline(now)

    def _finish(self) -> None:
        self._send("sensor_scan", on=0)
        self._phase = Phase.DONE

    def _fail(self, message: str) -> None:
        self._make_safe()
        self._error = message
        self._phase = Phase.FAILED

    def _make_safe(self) -> None:
        """Stop the chamber in hand; if it may hold air, leave it venting.

        The vent is not closed from here: the node's manual dead-man closes it
        a few seconds later, so the air gets out even if nothing follows."""
        if self._index < len(self._slots):
            slot = self._slots[self._index]
            self._send("hold", chamber=slot)
            if self._phase in (Phase.PULSE, Phase.SETTLE, Phase.VENT):
                self._send("vent", chamber=slot, open=1)
        self._send("sensor_scan", on=0)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _node_entries(settings_data: dict[str, Any], mac: str) -> list[dict[str, Any]]:
    """Every node entry for ``mac`` (a board shared by two robots has two)."""
    out: list[dict[str, Any]] = []
    for group in (settings_data.get("robots") or {}).values():
        if not isinstance(group, list):
            continue
        for robot_cfg in group:
            out.extend(n for n in robot_cfg.get("nodes", []) if n.get("mac") == mac)
    return out


def iter_multiplexed_nodes(settings_data: dict[str, Any]) -> list[dict[str, Any]]:
    """One ``{mac, max_slots, robots, sensor_channels, organ_channels}`` per
    multiplexed board (the organ channels so a claim can restate them)."""
    robots = robot_ids_by_mac(settings_data)
    out: list[dict[str, Any]] = []
    for mac, node_type in node_types_by_mac(settings_data).items():
        if node_type != "node_multiplexed":
            continue
        entries = _node_entries(settings_data, mac)
        out.append({
            "mac": mac,
            "max_slots": max((int(n.get("max_slots", 0)) for n in entries), default=0),
            "robots": robots.get(mac, []),
            "sensor_channels": get_sensor_channels(settings_data, mac),
            "organ_channels": next(
                ([int(c) for c in n["organ_channels"]] for n in entries
                 if n.get("organ_channels")), []),
        })
    return out


def get_sensor_channels(settings_data: dict[str, Any], mac: str) -> list[int]:
    """The saved map for ``mac`` (empty = the board's default, never measured)."""
    for node in _node_entries(settings_data, mac):
        channels = node.get("sensor_channels")
        if channels:
            return [int(c) for c in channels]
    return []


def set_sensor_channels(settings_data: dict[str, Any], mac: str,
                        channels: Sequence[int] | None) -> int:
    """Store the map on every node entry for ``mac``; empty/None removes it.

    The map belongs to the board, so a board configured on two robots gets it
    on both entries. Returns how many entries were updated."""
    entries = _node_entries(settings_data, mac)
    for node in entries:
        if channels:
            node["sensor_channels"] = [int(c) for c in channels]
        else:
            node.pop("sensor_channels", None)
    return len(entries)
