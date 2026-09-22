"""Reusable touch-rhythm detection for declarative activities."""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

# How a group sync condition picks its participants:
#   fixed - the configured sensor list (or sensors 0..N-1);
#   auto  - whoever presses: the first N distinct sensors form the group and any
#           further sensor that presses joins for good, so a fourth child can
#           come in but from then on all four must keep every round.
MODE_FIXED = "fixed"
MODE_AUTO = "auto"
SYNC_MODES = (MODE_FIXED, MODE_AUTO)


@dataclass
class TouchRhythmTracker:
    """Track consecutive touch intervals close to a target cadence."""

    last_press_ms: float | None = None
    intervals_ms: list[float] = field(default_factory=list)

    def reset(self) -> None:
        self.last_press_ms = None
        self.intervals_ms.clear()

    def record(self, timestamp_ms: float) -> None:
        """Record one press and retain its interval for condition evaluation."""
        if self.last_press_ms is None:
            self.last_press_ms = timestamp_ms
            return

        interval_ms = timestamp_ms - self.last_press_ms
        self.last_press_ms = timestamp_ms
        self.intervals_ms.append(interval_ms)

    def matches(self, target_interval_ms: float, tolerance_ms: float,
                min_gap_ms: float, required_intervals: int) -> bool:
        """Return whether the latest intervals match the requested cadence."""
        matching = 0
        for interval_ms in reversed(self.intervals_ms):
            if interval_ms < min_gap_ms:
                continue
            if abs(interval_ms - target_interval_ms) > tolerance_ms:
                break
            matching += 1
        return matching >= max(1, int(required_intervals))

    def latest_interval_ms(self, min_gap_ms: float = 0.0) -> float | None:
        """Return the latest non-duplicate interval, if one is available."""
        for interval_ms in reversed(self.intervals_ms):
            if interval_ms >= min_gap_ms:
                return interval_ms
        return None

    def latest_frequency_hz(self, min_gap_ms: float = 0.0) -> float | None:
        """Return the latest usable cadence as presses per second."""
        interval_ms = self.latest_interval_ms(min_gap_ms)
        return None if interval_ms is None or interval_ms <= 0 else 1000.0 / interval_ms

    def has_matching_intervals(self, min_gap_ms: float,
                               required_intervals: int) -> bool:
        """Return whether this stream has enough usable intervals."""
        usable = sum(interval >= min_gap_ms for interval in self.intervals_ms)
        return usable >= max(1, int(required_intervals))


@dataclass
class MagnitudeCompressionTracker:
    """Turn a continuous magnitude stream into compression onsets.

    A rising crossing of ``enter`` records one beat. A held signal normally stays
    active until it falls below ``exit``; a later sharp rise can also record a
    beat, which handles sensors whose magnetic baseline does not fully recover
    between compressions.
    """

    enter: float
    exit: float
    active: bool = False
    previous: float | None = None
    spike_delta: float = 0.0

    def reset(self) -> None:
        self.active = False
        self.previous = None

    def update(self, magnitude: float) -> bool:
        magnitude = max(0.0, float(magnitude))
        previous = self.previous
        self.previous = magnitude
        spike_delta = self.spike_delta or max(20.0, self.enter * 0.25)
        if self.active:
            if magnitude <= self.exit:
                self.active = False
                return False
            # Count a distinct fast rise even when the signal remains above the
            # release threshold after the previous compression.
            return (previous is not None
                    and magnitude >= self.enter
                    and magnitude - previous >= spike_delta)
        if magnitude >= self.enter:
            self.active = True
            return True
        return False


@dataclass
class GroupTouchSyncTracker:
    """Recognise consecutive, lockstep multi-person compression rounds.

    The older :class:`TouchRhythmTracker` is deliberately per sensor: it is a
    useful way to ask whether several people happen to have similar *latest*
    rates.  CPR-style play needs a stronger guarantee: every accepted round
    must contain one onset from every selected child, close together in time,
    and the resulting group beats must keep the requested cadence.

    This class keeps the raw onsets and rebuilds rounds when queried.  That
    makes it safe for more than one declarative condition to inspect the same
    skin and avoids consuming a press before the activity tick sees it.
    """

    events: list[tuple[float, int]] = field(default_factory=list)

    def reset(self) -> None:
        self.events.clear()

    def record(self, sensor_idx: int, timestamp_ms: float) -> None:
        timestamp_ms = float(timestamp_ms)
        self.events.append((timestamp_ms, int(sensor_idx)))
        cutoff = timestamp_ms - 120_000.0
        if self.events and self.events[0][0] < cutoff:
            self.events = [(time_ms, idx) for time_ms, idx in self.events
                           if time_ms >= cutoff]

    def matches(self, *, participants: int, sensor_ids: list[int] | None,
                target_interval_ms: float, cadence_tolerance_ms: float,
                phase_tolerance_ms: float, min_gap_ms: float,
                required_rounds: int, now_ms: float,
                mode: str = MODE_FIXED) -> bool:
        """Whether the latest rounds form a fresh synchronized streak.

        A round begins with the first accepted press and has a fixed phase
        window.  It is valid only when every selected sensor appears exactly
        once in that window.  The round time is the median press time, which
        avoids one slightly early/late child moving the shared cadence.
        """
        return bool(self.status(
            participants=participants, sensor_ids=sensor_ids,
            target_interval_ms=target_interval_ms,
            cadence_tolerance_ms=cadence_tolerance_ms,
            phase_tolerance_ms=phase_tolerance_ms, min_gap_ms=min_gap_ms,
            required_rounds=required_rounds, now_ms=now_ms, mode=mode,
        )["complete"])

    def status(self, *, participants: int, sensor_ids: list[int] | None,
               target_interval_ms: float, cadence_tolerance_ms: float,
               phase_tolerance_ms: float, min_gap_ms: float,
               required_rounds: int, now_ms: float,
               mode: str = MODE_FIXED) -> dict:
        """Return the current CPR-round progress for a live UI checklist.

        ``mode`` :data:`MODE_FIXED` uses ``sensor_ids`` (or sensors 0..N-1);
        :data:`MODE_AUTO` lets the children pick themselves: every sensor
        that has pressed since the last reset is in the group, and at least
        ``participants`` of them are needed before rounds count.  A sensor
        that joins late makes the earlier rounds incomplete, so the streak
        restarts with the larger group - by design, once four are in, all
        four must keep every round.
        """
        auto = mode == MODE_AUTO
        min_participants = max(1, int(participants))
        selected: list[int] | None = (
            None if auto else self._selected_sensors(participants, sensor_ids))
        rounds_needed = max(1, int(required_rounds))
        if selected is not None and not selected:
            return {"complete": False, "rounds": 0,
                    "rounds_required": rounds_needed, "sensors": [],
                    "missing_sensors": [], "mode": mode,
                    "reason": "Invalid sensor zones"}
        target = max(1.0, float(target_interval_ms))
        cadence_tol = max(0.0, float(cadence_tolerance_ms))
        phase_tol = max(0.0, float(phase_tolerance_ms))
        debounce = max(0.0, float(min_gap_ms))

        # A condition must be earned by current movement, not a good sequence
        # that happened long before the next activity tick/session phase.
        stale_after = max(target * 1.5, phase_tol * 2.0, 250.0)

        # Reject duplicate/chattering onsets before creating groups.  Keep the
        # latest 120 s of input: considerably more than any plausible streak,
        # while preventing an unattended activity from accumulating data.
        cutoff = float(now_ms) - 120_000.0
        last_by_sensor: dict[int, float] = {}
        accepted: list[tuple[float, int]] = []
        joined: list[int] = []            # auto mode: sensors in first-press order
        for timestamp, sensor_idx in sorted(self.events):
            if timestamp < cutoff:
                continue
            if selected is not None and sensor_idx not in selected:
                continue
            previous = last_by_sensor.get(sensor_idx)
            if previous is not None and timestamp - previous < debounce:
                continue
            last_by_sensor[sensor_idx] = timestamp
            accepted.append((timestamp, sensor_idx))
            if sensor_idx not in joined:
                joined.append(sensor_idx)
        if selected is None:
            selected = sorted(joined)
            if len(selected) < min_participants:
                short = min_participants - len(selected)
                return {"complete": False, "rounds": 0,
                        "rounds_required": rounds_needed, "sensors": selected,
                        "missing_sensors": [], "mode": mode,
                        "target_interval_ms": target,
                        "cadence_tolerance_ms": cadence_tol,
                        "phase_tolerance_ms": phase_tol,
                        "last_interval_ms": None, "last_interval_ok": None,
                        "reason": (f"Waiting for {short} more "
                                   f"{'child' if short == 1 else 'children'}")}
        wanted = set(selected)

        completed: list[float] = []
        start: float | None = None
        presses: dict[int, float] = {}

        def finish_round() -> None:
            if len(presses) == len(wanted):
                completed.append(float(median(presses.values())))

        for timestamp, sensor_idx in accepted:
            if start is None:
                start, presses = timestamp, {sensor_idx: timestamp}
                continue
            if timestamp - start <= phase_tol:
                # Debouncing above means a second entry here is either a
                # deliberately very fast press or an overlap; the first onset
                # is the child's contribution to this round.
                presses.setdefault(sensor_idx, timestamp)
                continue
            finish_round()
            start, presses = timestamp, {sensor_idx: timestamp}
        if start is not None:
            finish_round()

        streak = 0
        last_interval_ms: float | None = None
        last_interval_ok: bool | None = None
        for index, current in enumerate(completed):
            if index == 0:
                streak = 1
                continue
            previous = completed[index - 1]
            last_interval_ms = current - previous
            last_interval_ok = abs(last_interval_ms - target) <= cadence_tol
            if last_interval_ok:
                streak += 1
            else:
                streak = 1
        fresh = bool(completed) and float(now_ms) - completed[-1] <= stale_after
        missing = (sorted(wanted - set(presses))
                   if start is not None and float(now_ms) - start <= phase_tol
                   else [])
        if missing:
            reason = "Waiting for " + ", ".join(f"T{idx}" for idx in missing)
        elif not completed:
            reason = "Press all zones together to start"
        elif not fresh:
            reason = "Start the next synchronized round"
        elif last_interval_ok is False:
            reason = "Keep the next round on the target rhythm"
        else:
            reason = "Start the next synchronized round"
        return {
            "complete": fresh and streak >= rounds_needed,
            "rounds": streak if fresh else 0,
            "rounds_required": rounds_needed,
            "sensors": selected,
            "missing_sensors": missing,
            "target_interval_ms": target,
            "cadence_tolerance_ms": cadence_tol,
            "phase_tolerance_ms": phase_tol,
            "last_interval_ms": last_interval_ms,
            "last_interval_ok": last_interval_ok,
            "mode": mode,
            "reason": reason,
        }

    @staticmethod
    def _selected_sensors(participants: int,
                          sensor_ids: list[int] | None) -> list[int]:
        """Use configured physical positions, else the first N sensors."""
        if isinstance(sensor_ids, (list, tuple)) and sensor_ids:
            try:
                selected = [int(value) for value in sensor_ids]
            except (TypeError, ValueError):
                return []
            # Repeated IDs cannot represent independent participants.
            return selected if len(set(selected)) == len(selected) else []
        return list(range(max(1, int(participants))))


@dataclass
class SyncScoreTracker:
    """Score the group's synchronized rounds as they happen.

    Unlike :class:`GroupTouchSyncTracker` (a *streak*: one miss restarts the
    count), this keeps a running score in 0..1: every complete round in
    cadence adds ``gain``, every failed round (a child missing from the phase
    window, or a stray press by one child alone) subtracts ``penalty``. A
    complete round that is off cadence is neutral. So a few slips barely
    show while a lot of random pressing drains the score back to zero.

    Rounds are segmented incrementally: the first accepted press opens a
    round, presses within ``phase_tolerance_ms`` join it, the next press
    beyond the window (or :meth:`tick` once the window has expired) closes
    it. ``mode`` works as for the streak tracker: *fixed* uses
    ``sensor_ids`` (or sensors 0..N-1), *auto* lets whoever presses form the
    group, scoring only once ``participants`` sensors have joined.
    """

    score: float = 0.0
    good_rounds: int = 0
    bad_rounds: int = 0
    _params: dict | None = None
    _joined: list[int] = field(default_factory=list)
    _last_onset: dict[int, float] = field(default_factory=dict)
    _open_start: float | None = None
    _open_presses: dict[int, float] = field(default_factory=dict)
    _last_round_ms: float | None = None

    def configure(self, *, participants: int, sensor_ids: list[int] | None,
                  target_interval_ms: float, cadence_tolerance_ms: float,
                  phase_tolerance_ms: float, min_gap_ms: float,
                  gain: float, penalty: float,
                  mode: str = MODE_FIXED) -> None:
        """Set the round rules. Until configured, presses are ignored."""
        selected: list[int] | None = None
        if mode != MODE_AUTO:
            selected = GroupTouchSyncTracker._selected_sensors(
                participants, sensor_ids)
        self._params = {
            "auto": mode == MODE_AUTO,
            "participants": max(1, int(participants)),
            "selected": selected,
            "target": max(1.0, float(target_interval_ms)),
            "cadence_tol": max(0.0, float(cadence_tolerance_ms)),
            "phase_tol": max(0.0, float(phase_tolerance_ms)),
            "debounce": max(0.0, float(min_gap_ms)),
            "gain": max(0.0, float(gain)),
            "penalty": max(0.0, float(penalty)),
        }

    def reset(self) -> None:
        self.score = 0.0
        self.good_rounds = 0
        self.bad_rounds = 0
        self._joined.clear()
        self._last_onset.clear()
        self._open_start = None
        self._open_presses = {}
        self._last_round_ms = None

    @property
    def complete(self) -> bool:
        return self.score >= 1.0 - 1e-9

    @property
    def sensors(self) -> list[int]:
        """The sensors currently required in every round."""
        if self._params is None:
            return []
        selected = self._params["selected"]
        return list(selected) if selected is not None else sorted(self._joined)

    def record(self, sensor_idx: int, timestamp_ms: float) -> None:
        p = self._params
        if p is None:
            return
        sensor_idx = int(sensor_idx)
        timestamp_ms = float(timestamp_ms)
        if p["selected"] is not None and sensor_idx not in p["selected"]:
            return
        previous = self._last_onset.get(sensor_idx)
        if previous is not None and timestamp_ms - previous < p["debounce"]:
            return
        self._last_onset[sensor_idx] = timestamp_ms
        if sensor_idx not in self._joined:
            self._joined.append(sensor_idx)
        if self._open_start is None:
            self._open_start, self._open_presses = timestamp_ms, {sensor_idx: timestamp_ms}
            return
        if timestamp_ms - self._open_start <= p["phase_tol"]:
            self._open_presses.setdefault(sensor_idx, timestamp_ms)
            return
        self._close_round()
        self._open_start, self._open_presses = timestamp_ms, {sensor_idx: timestamp_ms}

    def tick(self, now_ms: float) -> None:
        """Close the open round once its phase window has expired, so a
        lone stray press is judged without waiting for the next one."""
        p = self._params
        if p is None or self._open_start is None:
            return
        if float(now_ms) - self._open_start > p["phase_tol"]:
            self._close_round()
            self._open_start, self._open_presses = None, {}

    def status(self) -> dict:
        return {"score": self.score, "complete": self.complete,
                "good_rounds": self.good_rounds, "bad_rounds": self.bad_rounds,
                "sensors": self.sensors}

    def _close_round(self) -> None:
        p = self._params
        if p is None or not self._open_presses:
            return
        wanted = set(self.sensors)
        if p["auto"] and len(wanted) < p["participants"]:
            return                           # not enough children yet: no score
        pressed = set(self._open_presses)
        if not wanted <= pressed:
            self.bad_rounds += 1
            self.score = max(0.0, self.score - p["penalty"])
            return
        round_ms = float(median(self._open_presses.values()))
        last = self._last_round_ms
        self._last_round_ms = round_ms
        if last is None or abs((round_ms - last) - p["target"]) <= p["cadence_tol"]:
            self.good_rounds += 1
            self.score = min(1.0, self.score + p["gain"])
