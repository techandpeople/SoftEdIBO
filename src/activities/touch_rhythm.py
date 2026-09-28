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


# Onsets older than this are dropped: far longer than any plausible streak,
# while an unattended activity cannot accumulate data forever.
_ONSET_WINDOW_MS = 120_000.0


@dataclass(frozen=True)
class _SyncRules:
    """Normalised round rules for one :meth:`GroupTouchSyncTracker.status`."""

    target: float
    cadence_tol: float
    phase_tol: float
    debounce: float
    rounds_needed: int

    @property
    def stale_after(self) -> float:
        """A streak must be earned by current movement, not a good sequence
        that happened long before the next tick / phase."""
        return max(self.target * 1.5, self.phase_tol * 2.0, 250.0)


@dataclass
class _RoundLog:
    """Rounds segmented from the accepted onsets, oldest first.

    ``outcomes`` holds ``(beat_ms, None)`` for a complete round and
    ``(None, rejection)`` for a failed one. ``open_start``/``open_presses``
    describe the latest round (still open, or the one just closed)."""

    wanted: set[int]
    phase_tol: float
    completed: list[tuple[float, float]] = field(default_factory=list)
    outcomes: list[tuple[float | None, dict | None]] = field(default_factory=list)
    open_start: float | None = None
    open_presses: dict[int, float] = field(default_factory=dict)

    def open(self, timestamp: float, sensor_idx: int) -> None:
        self.open_start, self.open_presses = timestamp, {sensor_idx: timestamp}

    def close(self, *, late_sensor: int | None = None,
              late_time: float | None = None) -> None:
        """Judge the latest round: complete, or a rejection naming who was
        missing (and, when a later onset reveals it, who was late)."""
        assert self.open_start is not None
        start = self.open_start
        if len(self.open_presses) == len(self.wanted):
            times = list(self.open_presses.values())
            beat = float(median(times))
            self.completed.append((beat, max(times) - min(times)))
            self.outcomes.append((beat, None))
            return
        missing = sorted(self.wanted - set(self.open_presses))
        other_times = list(self.open_presses.values())
        is_late = late_sensor in missing and late_time is not None
        rejection = {
            "id": (f"{start:.3f}:late:{late_sensor}" if is_late
                   else f"{start:.3f}:timeout"),
            "round_attempt": len(self.outcomes) + 1,
            "reason": "missing_or_late_touch",
            "missing_sensors": missing,
            "late_sensor": late_sensor if is_late else None,
            "lateness_ms": (max(0.0, late_time - (start + self.phase_tol))
                            if is_late and late_time is not None else None),
            "other_sensors": sorted(self.open_presses),
            "other_sync_spread_ms": (max(other_times) - min(other_times)
                                     if len(other_times) >= 2 else None),
            "phase_tolerance_ms": self.phase_tol,
        }
        self.outcomes.append((None, rejection))


@dataclass
class GroupTouchSyncTracker:
    """Recognise consecutive, lockstep multi-participant rounds.

    Every accepted round must contain one onset from every selected sensor,
    close together in time (the phase window), and the resulting group beats
    must keep the requested cadence.

    This class keeps the raw onsets and rebuilds rounds when queried, so more
    than one condition can inspect the same skin and no press is consumed
    before the activity tick sees it.
    """

    events: list[tuple[float, int]] = field(default_factory=list)

    def reset(self) -> None:
        self.events.clear()

    def record(self, sensor_idx: int, timestamp_ms: float) -> None:
        timestamp_ms = float(timestamp_ms)
        self.events.append((timestamp_ms, int(sensor_idx)))
        cutoff = timestamp_ms - _ONSET_WINDOW_MS
        if self.events and self.events[0][0] < cutoff:
            self.events = [(time_ms, idx) for time_ms, idx in self.events
                           if time_ms >= cutoff]

    def matches(self, *, participants: int, sensor_ids: list[int] | None,
                target_interval_ms: float, cadence_tolerance_ms: float,
                phase_tolerance_ms: float, min_gap_ms: float,
                required_rounds: int, now_ms: float,
                mode: str = MODE_FIXED) -> bool:
        """Whether the latest rounds form a fresh synchronized streak."""
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
        """Current round progress (streak, last beat, last rejection, reason).

        A round begins with the first accepted press and lasts one phase
        window; it is complete only when every selected sensor appears in it.
        Its beat time is the median press time, so one slightly early/late
        participant does not move the shared cadence.

        ``mode`` :data:`MODE_FIXED` uses ``sensor_ids`` (or sensors 0..N-1);
        :data:`MODE_AUTO` makes every sensor that has pressed since the last
        reset part of the group, with at least ``participants`` needed before
        rounds count. A late joiner makes earlier rounds incomplete, so the
        streak restarts with the larger group.
        """
        rules = _SyncRules(
            target=max(1.0, float(target_interval_ms)),
            cadence_tol=max(0.0, float(cadence_tolerance_ms)),
            phase_tol=max(0.0, float(phase_tolerance_ms)),
            debounce=max(0.0, float(min_gap_ms)),
            rounds_needed=max(1, int(required_rounds)),
        )
        now_ms = float(now_ms)
        result: dict = {
            "complete": False, "rounds": 0,
            "rounds_required": rules.rounds_needed, "sensors": [],
            "missing_sensors": [], "mode": mode,
            "target_interval_ms": rules.target,
            "cadence_tolerance_ms": rules.cadence_tol,
            "phase_tolerance_ms": rules.phase_tol,
            "last_interval_ms": None, "last_interval_ok": None,
            "last_round_ms": None, "last_phase_spread_ms": None,
            "last_rejection": None, "frequency_hz_by_sensor": {},
        }
        selected = (None if mode == MODE_AUTO
                    else self._selected_sensors(participants, sensor_ids))
        if selected is not None and not selected:
            return {**result, "reason": "Invalid sensor zones"}
        accepted = self._accepted_onsets(selected, rules.debounce, now_ms)
        if selected is None:
            selected = sorted({idx for _, idx in accepted})
            short = max(1, int(participants)) - len(selected)
            if short > 0:
                return {**result, "sensors": selected,
                        "reason": (f"Waiting for {short} more participant"
                                   f"{'' if short == 1 else 's'}")}

        rounds = self._segment_rounds(accepted, set(selected),
                                      rules.phase_tol, now_ms)
        streak, last_interval_ms, last_interval_ok = self._streak(
            rounds.outcomes, rules)
        last_round_ms, last_spread = (rounds.completed[-1] if rounds.completed
                                      else (None, None))
        fresh = (last_round_ms is not None
                 and now_ms - last_round_ms <= rules.stale_after)
        missing = (sorted(set(selected) - set(rounds.open_presses))
                   if rounds.open_start is not None
                   and now_ms - rounds.open_start <= rules.phase_tol else [])
        rejections = [rej for _, rej in rounds.outcomes if rej is not None]
        result.update({
            "complete": fresh and streak >= rules.rounds_needed,
            "rounds": streak if fresh else 0,
            "sensors": selected,
            "missing_sensors": missing,
            "last_interval_ms": last_interval_ms,
            "last_interval_ok": last_interval_ok,
            "last_round_ms": last_round_ms,
            "last_phase_spread_ms": last_spread,
            "last_rejection": rejections[-1] if rejections else None,
            "frequency_hz_by_sensor": self._frequencies(accepted),
            "reason": self._reason(missing, bool(rounds.completed), fresh,
                                   last_interval_ok),
        })
        return result

    def _accepted_onsets(self, selected: list[int] | None, debounce: float,
                         now_ms: float) -> list[tuple[float, int]]:
        """Recent onsets of the selected sensors, minus per-sensor chatter."""
        cutoff = now_ms - _ONSET_WINDOW_MS
        last_by_sensor: dict[int, float] = {}
        accepted: list[tuple[float, int]] = []
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
        return accepted

    @staticmethod
    def _frequencies(accepted: list[tuple[float, int]]) -> dict[int, float]:
        """Latest per-sensor cadence (Hz) from the debounced onsets."""
        frequency_hz: dict[int, float] = {}
        previous_by_sensor: dict[int, float] = {}
        for timestamp, sensor_idx in accepted:
            previous = previous_by_sensor.get(sensor_idx)
            if previous is not None and timestamp > previous:
                frequency_hz[sensor_idx] = round(1000.0 / (timestamp - previous), 3)
            previous_by_sensor[sensor_idx] = timestamp
        return frequency_hz

    @staticmethod
    def _segment_rounds(accepted: list[tuple[float, int]], wanted: set[int],
                        phase_tol: float, now_ms: float) -> _RoundLog:
        """Group onsets into phase-window rounds and judge each one."""
        log = _RoundLog(wanted=wanted, phase_tol=phase_tol)
        for timestamp, sensor_idx in accepted:
            if log.open_start is None:
                log.open(timestamp, sensor_idx)
            elif timestamp - log.open_start <= phase_tol:
                # The first onset is the participant's contribution.
                log.open_presses.setdefault(sensor_idx, timestamp)
            else:
                # An onset past the window starts the next round and may
                # name who was late for the one being closed.
                log.close(late_sensor=sensor_idx, late_time=timestamp)
                log.open(timestamp, sensor_idx)
        if log.open_start is not None and (
                len(log.open_presses) == len(wanted)
                or now_ms - log.open_start > phase_tol):
            # Also judged from the tick, so a participant who never presses
            # is reported promptly.
            log.close()
        return log

    @staticmethod
    def _streak(outcomes: list[tuple[float | None, dict | None]],
                rules: _SyncRules) -> tuple[int, float | None, bool | None]:
        """Consecutive in-cadence rounds ending at the latest outcome, plus
        the last beat interval and whether it was on cadence. A rejection
        restarts the streak and is stamped with the round it broke."""
        streak = 0
        last_interval_ms: float | None = None
        last_interval_ok: bool | None = None
        previous: float | None = None
        for beat, rejection in outcomes:
            if beat is None:
                if rejection is not None:
                    rejection["round"] = streak + 1
                streak, previous = 0, None
                last_interval_ms = last_interval_ok = None
                continue
            if previous is not None:
                last_interval_ms = beat - previous
                last_interval_ok = (abs(last_interval_ms - rules.target)
                                    <= rules.cadence_tol)
                streak = streak + 1 if last_interval_ok else 1
            else:
                streak = 1
            previous = beat
        return streak, last_interval_ms, last_interval_ok

    @staticmethod
    def _reason(missing: list[int], any_completed: bool, fresh: bool,
                last_interval_ok: bool | None) -> str:
        if missing:
            return "Waiting for " + ", ".join(f"T{idx}" for idx in missing)
        if not any_completed:
            return "Press all zones together to start"
        if fresh and last_interval_ok is False:
            return "Keep the next round on the target rhythm"
        return "Start the next synchronized round"

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
