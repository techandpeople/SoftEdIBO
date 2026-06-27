"""Chamber fill-time calibration — GUI-free core + settings helpers.

The multiplexed pressure sensors are too slow/laggy to close the loop on the
pump in real time, so chambers are inflated for a **pre-measured time** instead.
Because a chamber does not fill linearly (pressure rises fast, then creeps toward
the max), one number isn't enough: we measure the whole **time→pressure curve**.

:class:`FillProfileCalibrator` builds that curve with a discrete-step sweep,
starting from the ambient (empty) state: open the inflate valve for a fixed
``step_ms``, let the laggy sensor settle, read the pressure (% of the chamber
max), record a curve point, and repeat — accumulating time — until the chamber
reaches the target or a total-time ceiling (asymptotic creep that never gets
there). Re-running with a smaller ``step_ms`` yields a finer curve.

The result is stored per chamber as ``fill_profile`` (a list of ``[ms, pct]``);
:class:`~src.hardware.fill_profile.FillProfile` interpolates it at runtime so the
firmware inflates by time instead of the slow sensor. A hard total-time ceiling
and the firmware's own ``HARD_MAX`` pressure cutoff stay as safety nets.

The calibrator is deliberately Qt-free and feeds on plain pressure readings, so
it's unit-tested; the Qt dialog (``src/gui/fill_calibration_dialog.py``) drives
the hardware (deflate → step inflate → settle → read) and feeds it.
"""

from __future__ import annotations

from typing import Any

from src.core import skin_config
from src.hardware.fill_profile import FillProfile

# Defaults for a discrete-step sweep. The first (coarse) pass uses ~400 ms steps;
# re-running with a smaller step refines the curve. The sweep stops a hair under
# 100 % (sensor noise / the last asymptotic creep never reaches a clean 100), and
# is bounded in total time so a stuck/unplugged sensor can't sweep forever.
DEFAULT_STEP_MS: float = 400.0
DEFAULT_TARGET_PCT: float = 98.0
DEFAULT_TIMEOUT_MS: float = 8000.0

# Step granularity offered in the dialog (coarse → fine), for the refine passes.
STEP_CHOICES_MS: tuple[float, ...] = (600.0, 400.0, 250.0, 150.0)


class FillProfileCalibrator:
    """Builds a :class:`FillProfile` from a discrete-step inflate sweep.

    Driven by the caller (the dialog), which does the hardware work::

        cal = FillProfileCalibrator(step_ms=400)
        # chamber already deflated to ambient
        while not cal.done:
            driver_opens_inflate_valve_for(cal.step_ms)
            wait_for_sensor_to_settle()
            cal.record(read_pressure_pct())     # returns True when finished
        store(cal.profile.to_list())

    Each :meth:`record` advances the cumulative fill time by one ``step_ms`` and
    appends a ``(cumulative_ms, pct)`` point. It finishes when the chamber
    reaches ``target_pct`` or the cumulative time reaches ``max_total_ms``.
    """

    def __init__(self, step_ms: float = DEFAULT_STEP_MS,
                 target_pct: float = DEFAULT_TARGET_PCT,
                 max_total_ms: float = DEFAULT_TIMEOUT_MS) -> None:
        self.step_ms = max(1.0, float(step_ms))
        self.target_pct = float(target_pct)
        self.max_total_ms = float(max_total_ms)
        self._points: list[tuple[float, float]] = [(0.0, 0.0)]   # ambient anchor
        self._elapsed = 0.0
        self.done = False
        self.timed_out = False

    @property
    def elapsed_ms(self) -> float:
        return self._elapsed

    @property
    def steps(self) -> int:
        """Number of inflate steps recorded so far (excludes the anchor)."""
        return len(self._points) - 1

    def record(self, pressure_pct: float) -> bool:
        """Feed the settled pressure reading (0–100 %) taken after one step.

        Advances cumulative time by ``step_ms``, appends the curve point, and
        returns ``True`` once the sweep is finished (target reached or timed
        out), else ``False``."""
        if self.done:
            return True
        self._elapsed += self.step_ms
        pct = max(0.0, min(100.0, float(pressure_pct)))
        self._points.append((self._elapsed, pct))
        if pct >= self.target_pct:
            self.done = True
        elif self._elapsed >= self.max_total_ms:
            self.done = True
            self.timed_out = True
        return self.done

    @property
    def profile(self) -> FillProfile:
        """The curve measured so far (usable even mid-sweep)."""
        return FillProfile(self._points)


class MultiChamberFillCalibrator:
    """Sweeps several chambers **in lockstep** to measure their curves under the
    real shared-pump load (see :mod:`src.hardware.fill_scaling`).

    Wraps one :class:`FillProfileCalibrator` per slot, all on the same node, all
    using the same ``step_ms`` so their cumulative times stay aligned. Each step
    the driver opens the inflate valves of the still-:meth:`pending_slots`,
    settles, and feeds every pending slot its reading via :meth:`record`. A slot
    finishes when its own calibrator reaches target/timeout; once finished its
    valve is no longer opened, so the remaining chambers continue under the now
    **lighter** load — exactly as at runtime, where a chamber stops when it
    reaches its target while the others keep filling. The run is :attr:`done`
    when every slot has finished.

    A single-slot set degenerates to a plain solo sweep, so the dialog drives
    solo and combination calibrations through one code path."""

    def __init__(self, slots: Any, step_ms: float = DEFAULT_STEP_MS,
                 target_pct: float = DEFAULT_TARGET_PCT,
                 max_total_ms: float = DEFAULT_TIMEOUT_MS) -> None:
        self.step_ms = max(1.0, float(step_ms))
        self._cals: dict[int, FillProfileCalibrator] = {
            int(s): FillProfileCalibrator(self.step_ms, target_pct, max_total_ms)
            for s in slots}

    @property
    def slots(self) -> list[int]:
        return sorted(self._cals)

    def calibrator(self, slot: int) -> FillProfileCalibrator:
        """The per-slot sweep (for its profile / timed_out / steps)."""
        return self._cals[int(slot)]

    def pending_slots(self) -> list[int]:
        """Slots not yet finished — the valves to open on the next step."""
        return sorted(s for s, c in self._cals.items() if not c.done)

    @property
    def done(self) -> bool:
        return all(c.done for c in self._cals.values())

    def record(self, readings: dict[int, float]) -> bool:
        """Advance one step: feed each *pending* slot its settled reading.

        ``readings`` maps slot → pressure %% (0–100); a pending slot missing from
        the dict reuses its last recorded value. Returns ``True`` once every slot
        has finished."""
        for slot, cal in self._cals.items():
            if cal.done:
                continue
            cal.record(readings.get(slot, cal.profile.top_pct))
        return self.done

    def profiles(self) -> dict[int, list[list[float]]]:
        """Per-slot measured curve in the stored ``[[ms, pct], ...]`` form."""
        return {slot: cal.profile.to_list() for slot, cal in self._cals.items()}


# ---------------------------------------------------------------------------
# Settings helpers (pure dict walks over ``Settings.data``)
# ---------------------------------------------------------------------------

# Node types that actuate chambers (and so have fill curves to calibrate).
ACTUATOR_NODE_TYPES = ("node_direct", "node_multiplexed")


def combo_key(slots: Any) -> str:
    """Stable key for a co-active slot set: sorted, de-duped, comma-joined.

    Used to store/look up a chamber's fill curve measured under a specific set of
    concurrently-inflating slots on its node (e.g. ``"0,1,2"``)."""
    return ",".join(str(s) for s in sorted({int(x) for x in slots}))


def parse_combo_key(key: str) -> frozenset[int]:
    """Inverse of :func:`combo_key` — the slot set a stored combo was measured at."""
    return frozenset(int(p) for p in str(key).split(",") if p != "")


def _iter_robots(settings_data: dict) -> Any:
    """Yield every robot dict across the robots-by-kind buckets."""
    for bucket in (settings_data.get("robots") or {}).values():
        for robot in bucket or []:
            yield robot


def iter_actuator_chambers(settings_data: dict) -> list[dict]:
    """List configured chambers that can be calibrated, one entry per chamber.

    Each entry: ``{robot_id, skin_id, mac, slot, node_type, fill_profile,
    fill_time_ms, calibrated}``. ``fill_profile`` is the stored ``[[ms, pct],
    ...]`` list (or ``None``); ``fill_time_ms`` is the legacy scalar (or
    ``None``); ``calibrated`` is True when either is present. Built by joining
    each skin's ``chambers`` to its node's ``node_type``."""
    out: list[dict] = []
    for robot in _iter_robots(settings_data):
        node_types = {n.get("mac"): n.get("node_type")
                      for n in (robot.get("nodes") or [])}
        for skin in robot.get("skins") or []:
            for ch in skin.get("chambers") or []:
                mac = ch.get("mac")
                nt = node_types.get(mac)
                if nt not in ACTUATOR_NODE_TYPES:
                    continue
                profile = ch.get("fill_profile")
                fill_ms = ch.get("fill_time_ms")
                out.append({
                    "robot_id": robot.get("id", ""),
                    "skin_id": skin.get("skin_id", ""),
                    "mac": mac,
                    "slot": int(ch.get("slot", 0)),
                    "node_type": nt,
                    "fill_mode": skin_config.normalize_fill_mode(ch.get("fill_mode")),
                    "fill_profile": profile,
                    "fill_profiles": ch.get("fill_profiles") or {},
                    "fill_time_ms": fill_ms,
                    "calibrated": bool(profile) or bool(fill_ms),
                })
    return out


def iter_actuator_nodes(settings_data: dict) -> list[dict]:
    """Group calibratable chambers by node (``mac``), one entry per node.

    Each entry: ``{mac, node_type, slots, chambers}``. Combinations only matter
    within a node (pumps/reservoir are shared per node), so the calibration
    enumerates concurrent subsets from each node's ``slots``."""
    nodes: dict[str, dict] = {}
    for ch in iter_actuator_chambers(settings_data):
        node = nodes.setdefault(ch["mac"], {
            "mac": ch["mac"], "node_type": ch["node_type"],
            "slots": [], "chambers": []})
        node["slots"].append(ch["slot"])
        node["chambers"].append(ch)
    for node in nodes.values():
        node["slots"] = sorted(set(node["slots"]))
    return list(nodes.values())


def set_fill_profile(settings_data: dict, mac: str, slot: int,
                     profile: list[list[float]] | None) -> int:
    """Write ``fill_profile`` onto every chamber entry matching ``mac``+``slot``.

    Stored next to ``max_pressure``. Writing a profile drops any legacy
    ``fill_time_ms`` so the two can't disagree. ``None`` clears the profile.
    Returns the number of chamber entries updated."""
    n = 0
    for robot in _iter_robots(settings_data):
        for skin in robot.get("skins") or []:
            for ch in skin.get("chambers") or []:
                if ch.get("mac") == mac and int(ch.get("slot", 0)) == int(slot):
                    if profile:
                        ch["fill_profile"] = profile
                        ch.pop("fill_time_ms", None)
                    else:
                        ch.pop("fill_profile", None)
                    n += 1
    return n


def set_fill_profiles(settings_data: dict, mac: str, slot: int,
                      combos: dict[str, list[list[float]]] | None) -> int:
    """Write the per-combination ``fill_profiles`` map onto matching chamber(s).

    ``combos`` maps a :func:`combo_key` (co-active slot set) to that chamber's
    measured curve under that load. Stored next to ``fill_profile`` (the solo
    curve); an empty/``None`` map clears it. Like :func:`set_fill_profile`,
    writing a measured curve drops any legacy ``fill_time_ms``. Returns the number
    of chamber entries updated."""
    n = 0
    for robot in _iter_robots(settings_data):
        for skin in robot.get("skins") or []:
            for ch in skin.get("chambers") or []:
                if ch.get("mac") == mac and int(ch.get("slot", 0)) == int(slot):
                    if combos:
                        ch["fill_profiles"] = {str(k): v for k, v in combos.items()}
                        ch.pop("fill_time_ms", None)
                    else:
                        ch.pop("fill_profiles", None)
                    n += 1
    return n


def chambers_missing_calibration(settings_data: dict) -> list[dict]:
    """Configured actuator chambers that still need a fill curve — used by the
    pre-activity guard to offer calibration.

    Chambers set to ``pressure`` fill mode inflate closed-loop on the gauge
    sensor and need no calibration, so they are never flagged."""
    return [c for c in iter_actuator_chambers(settings_data)
            if not c["calibrated"]
            and c["fill_mode"] != skin_config.FILL_MODE_PRESSURE]
