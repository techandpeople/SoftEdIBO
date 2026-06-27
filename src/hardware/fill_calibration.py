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


# ---------------------------------------------------------------------------
# Settings helpers (pure dict walks over ``Settings.data``)
# ---------------------------------------------------------------------------

# Node types that actuate chambers (and so have fill curves to calibrate).
ACTUATOR_NODE_TYPES = ("node_direct", "node_multiplexed")


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
                    "fill_profile": profile,
                    "fill_time_ms": fill_ms,
                    "calibrated": bool(profile) or bool(fill_ms),
                })
    return out


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


def chambers_missing_calibration(settings_data: dict) -> list[dict]:
    """Configured actuator chambers with no fill curve (or legacy scalar) yet —
    used by the pre-activity guard to offer calibration."""
    return [c for c in iter_actuator_chambers(settings_data)
            if not c["calibrated"]]
