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
from src.hardware.fill_profile import DeflateProfile, FillProfile

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


class ContinuousFillCalibrator:
    """Builds a :class:`FillProfile` from a **continuous** inflate sweep.

    Unlike :class:`FillProfileCalibrator` (discrete step + settle), this feeds on a
    stream of ``(elapsed_ms, pressure_pct)`` readings taken while the inflate valve
    is held continuously open — the firmware's fast ``status_rate`` telemetry during
    a bench ``test_run``. The sensor is fast enough that no per-step settle is
    needed, so a dense curve is captured in a single valve-open pass (from ambient
    up toward the target), instead of the open→settle→read→repeat cycle.

    Driven by the caller (the dialog), which holds the valve open and pumps the
    fast readings in::

        cal = ContinuousFillCalibrator(target_pct=98)
        # chamber deflated to ambient, valve held open (test_run)
        while not cal.done:
            cal.record(elapsed_ms_since_open, read_pressure_pct())
        driver_closes_valve()                 # test_stop
        store(cal.profile.to_list())

    Samples must arrive in increasing time order; a sample at or before the last
    recorded time is ignored so ``elapsed_ms``/:attr:`done` stay monotone (the
    caller may feed from a coarse timer that can repeat a timestamp).
    """

    def __init__(self, target_pct: float = DEFAULT_TARGET_PCT,
                 max_total_ms: float = DEFAULT_TIMEOUT_MS) -> None:
        self.target_pct = float(target_pct)
        self.max_total_ms = float(max_total_ms)
        self._points: list[tuple[float, float]] = [(0.0, 0.0)]   # ambient anchor
        self._elapsed = 0.0
        self._top_pct = 0.0
        self.done = False
        self.timed_out = False

    @property
    def elapsed_ms(self) -> float:
        return self._elapsed

    @property
    def top_pct(self) -> float:
        """Highest pressure recorded so far — cheap live-progress readout (the
        :attr:`profile` property builds a whole FillProfile per call, far too
        heavy for a per-telemetry-message progress bar)."""
        return self._top_pct

    @property
    def samples(self) -> int:
        """Number of readings recorded so far (excludes the ambient anchor)."""
        return len(self._points) - 1

    def record(self, elapsed_ms: float, pressure_pct: float) -> bool:
        """Feed one ``(elapsed_ms, pressure_pct)`` reading from the open-valve sweep.

        ``elapsed_ms`` is measured from when the valve opened; ``pressure_pct`` is
        0–100 % of the chamber max. Returns ``True`` once the sweep is finished
        (target reached or timed out), else ``False``. Out-of-order/duplicate
        timestamps are ignored."""
        if self.done:
            return True
        t = float(elapsed_ms)
        if t <= self._elapsed:
            return False                 # non-monotone sample — ignore
        pct = max(0.0, min(100.0, float(pressure_pct)))
        self._points.append((t, pct))
        self._elapsed = t
        self._top_pct = max(self._top_pct, pct)
        if pct >= self.target_pct:
            self.done = True
        elif t >= self.max_total_ms:
            self.done = True
            self.timed_out = True
        return self.done

    @property
    def profile(self) -> FillProfile:
        """The curve measured so far (usable even mid-sweep)."""
        return FillProfile(self._points)


class PlateauDetector:
    """Detects when a falling reading has stopped dropping — reached its floor.

    Ambient does not read 0 on these gauges (and the floor moves again when the
    -40 kPa sensors arrive), so "empty" is never a fixed threshold: it is the
    level where the reading **plateaus**. A reading counts as still falling while
    it keeps dropping by at least ``min_drop`` (same units as the fed values);
    once no such drop happens for ``settle_ms`` (and at least ``min_ms`` has
    passed, so the pull can start), the floor is declared.

    Pure and clock-free — feed it ``(elapsed_ms, value)`` samples. Used by the
    calibration dialog's vent phase and by :class:`ContinuousDeflateCalibrator`.
    """

    def __init__(self, min_drop: float, settle_ms: float, min_ms: float = 0.0) -> None:
        self.min_drop = float(min_drop)
        self.settle_ms = float(settle_ms)
        self.min_ms = float(min_ms)
        self._floor = float("inf")
        self._floor_ms = 0.0

    def update(self, elapsed_ms: float, value: float) -> bool:
        """Feed one sample; returns True once the reading has plateaued."""
        t = float(elapsed_ms)
        v = float(value)
        if v < self._floor - self.min_drop:
            self._floor = v               # a new, meaningfully lower reading
            self._floor_ms = t
        return t >= self.min_ms and (t - self._floor_ms) >= self.settle_ms


# Plateau tuning for a deflate sweep: the fall has stopped when the level drops
# less than 1 % over 800 ms (the mux sensor is laggy; a shorter window can call
# the floor early on a slow tail).
_DEFLATE_PLATEAU_DROP_PCT = 1.0
_DEFLATE_PLATEAU_MS = 800.0
_DEFLATE_PLATEAU_MIN_MS = 300.0


class ContinuousDeflateCalibrator:
    """Builds a :class:`DeflateProfile` from a continuous deflate sweep.

    Mirror of :class:`ContinuousFillCalibrator` for the vacuum side: the driver
    fills the chamber, then holds the deflate valve open (bench ``test_run``
    dir=1) while feeding timestamped ``(elapsed_ms, pct)`` readings from the fast
    telemetry. The sweep finishes when the falling reading **plateaus** at the
    gauge's floor (measured, never assumed — see :class:`PlateauDetector`) or on
    the time ceiling. The resulting curve times deflates the gauge cannot
    supervise (targets at/below the sensor floor).
    """

    def __init__(self, max_total_ms: float = DEFAULT_TIMEOUT_MS,
                 plateau_drop_pct: float = _DEFLATE_PLATEAU_DROP_PCT,
                 plateau_ms: float = _DEFLATE_PLATEAU_MS) -> None:
        self.max_total_ms = float(max_total_ms)
        self._plateau = PlateauDetector(plateau_drop_pct, plateau_ms,
                                        _DEFLATE_PLATEAU_MIN_MS)
        self._points: list[tuple[float, float]] = []
        self._elapsed = -1.0
        self.done = False
        self.timed_out = False

    @property
    def elapsed_ms(self) -> float:
        return max(0.0, self._elapsed)

    @property
    def samples(self) -> int:
        return len(self._points)

    def record(self, elapsed_ms: float, pressure_pct: float) -> bool:
        """Feed one reading from the open-valve deflate sweep.

        Returns True once finished (floor plateau reached, or timed out).
        Out-of-order/duplicate timestamps are ignored."""
        if self.done:
            return True
        t = float(elapsed_ms)
        if t <= self._elapsed:
            return False
        pct = max(0.0, min(100.0, float(pressure_pct)))
        self._points.append((t, pct))
        self._elapsed = t
        if self._plateau.update(t, pct):
            self.done = True
        elif t >= self.max_total_ms:
            self.done = True
            self.timed_out = True
        return self.done

    @property
    def profile(self) -> DeflateProfile:
        """The falling curve measured so far (usable even mid-sweep)."""
        return DeflateProfile(self._points)


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
                    "skin_type": skin.get("skin_type", ""),
                    "skin_variant": skin.get("skin_variant", ""),
                    "mac": mac,
                    "slot": int(ch.get("slot", 0)),
                    "node_type": nt,
                    "fill_mode": skin_config.normalize_fill_mode(ch.get("fill_mode")),
                    "fill_profile": profile,
                    "fill_profiles": ch.get("fill_profiles") or {},
                    "deflate_profile": ch.get("deflate_profile"),
                    "duty_curve": ch.get("duty_curve"),
                    "fill_time_ms": fill_ms,
                    "calibrated": bool(profile) or bool(fill_ms),
                })
    return out


def iter_actuator_nodes(settings_data: dict) -> list[dict]:
    """Group calibratable chambers by node (``mac``), one entry per node.

    Each entry: ``{mac, node_type, slots, chambers}``. Combinations only matter
    within a node (pumps are shared per node), so the calibration
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


def set_deflate_profile(settings_data: dict, mac: str, slot: int,
                        profile: list[list[float]] | None) -> int:
    """Write ``deflate_profile`` (the falling time→pressure curve) onto every
    chamber entry matching ``mac``+``slot``. Times deflates the gauge cannot
    supervise (targets at/below the sensor floor). ``None`` clears it. Returns
    the number of chamber entries updated."""
    n = 0
    for robot in _iter_robots(settings_data):
        for skin in robot.get("skins") or []:
            for ch in skin.get("chambers") or []:
                if ch.get("mac") == mac and int(ch.get("slot", 0)) == int(slot):
                    if profile:
                        ch["deflate_profile"] = profile
                    else:
                        ch.pop("deflate_profile", None)
                    n += 1
    return n


def set_duty_curve(settings_data: dict, mac: str, slot: int,
                   curve: list[list[float]] | None) -> int:
    """Write the pump-duty→fill-speed sweep (``duty_curve``) onto matching chamber(s).

    ``curve`` is ``[[duty, full_time_ms], ...]`` measured at several PWM duties (each
    swept from empty); it feeds :class:`~src.hardware.fill_scaling.DutyModel` at
    runtime to pick the slow-fill duty. Stored next to ``fill_profile``; ``None``
    clears it. Returns the number of chamber entries updated."""
    n = 0
    for robot in _iter_robots(settings_data):
        for skin in robot.get("skins") or []:
            for ch in skin.get("chambers") or []:
                if ch.get("mac") == mac and int(ch.get("slot", 0)) == int(slot):
                    if curve:
                        ch["duty_curve"] = curve
                    else:
                        ch.pop("duty_curve", None)
                    n += 1
    return n


# ---------------------------------------------------------------------------
# Per-skin-type fill-curve templates
# ---------------------------------------------------------------------------
# A chamber's fill curve depends mostly on the silicone it inflates, which is
# fixed by the skin's (skin_type, skin_variant) — so a curve measured once for a
# type seeds every physical instance of it, keyed additionally by the slot
# position within the skin (a skin may hold differently sized chambers). Stored
# under the global ``fill_profiles_by_type`` map; a chamber's own ``fill_profile``
# (a manual override) wins over the type template at runtime.

TYPE_PROFILES_KEY = "fill_profiles_by_type"
# Same slug+slot scheme for the falling (deflate) curves.
DEFLATE_TYPE_PROFILES_KEY = "deflate_profiles_by_type"


def type_slug(skin_type: Any, skin_variant: Any) -> str:
    """Composite template key, e.g. ``"tree_round_organ"``.

    Empty when the skin type is unset (a skin with no type can't share a template)."""
    st = str(skin_type or "").strip()
    if not st:
        return ""
    sv = str(skin_variant or "").strip()
    return f"{st}_{sv}" if sv else st


def _get_type_curve(settings_data: dict, root_key: str, skin_type: Any,
                    skin_variant: Any, slot: Any) -> list | None:
    slug = type_slug(skin_type, skin_variant)
    if not slug:
        return None
    by_type = settings_data.get(root_key) or {}
    return (by_type.get(slug) or {}).get(str(int(slot)))


def _set_type_curve(settings_data: dict, root_key: str, skin_type: Any,
                    skin_variant: Any, slot: Any, curve: list | None) -> bool:
    slug = type_slug(skin_type, skin_variant)
    if not slug:
        return False
    slot_key = str(int(slot))
    if curve:
        settings_data.setdefault(root_key, {}).setdefault(slug, {})[slot_key] = curve
        return True
    by_type = settings_data.get(root_key) or {}
    slots = by_type.get(slug) or {}
    slots.pop(slot_key, None)
    if not slots:
        by_type.pop(slug, None)
    if not by_type:
        settings_data.pop(root_key, None)
    return True


def get_type_profile(settings_data: dict, skin_type: Any, skin_variant: Any,
                     slot: Any) -> list | None:
    """The stored type-template fill curve for (type, variant, slot), or ``None``."""
    return _get_type_curve(settings_data, TYPE_PROFILES_KEY,
                           skin_type, skin_variant, slot)


def set_type_profile(settings_data: dict, skin_type: Any, skin_variant: Any,
                     slot: Any, profile: list | None) -> bool:
    """Write (``None`` clears) a type-template fill curve. Returns True if the type
    had a slug to key on. Prunes empty branches so the YAML stays terse."""
    return _set_type_curve(settings_data, TYPE_PROFILES_KEY,
                           skin_type, skin_variant, slot, profile)


def get_type_deflate_profile(settings_data: dict, skin_type: Any,
                             skin_variant: Any, slot: Any) -> list | None:
    """The stored type-template deflate curve for (type, variant, slot), or ``None``."""
    return _get_type_curve(settings_data, DEFLATE_TYPE_PROFILES_KEY,
                           skin_type, skin_variant, slot)


def set_type_deflate_profile(settings_data: dict, skin_type: Any,
                             skin_variant: Any, slot: Any,
                             profile: list | None) -> bool:
    """Write (``None`` clears) a type-template deflate curve (same slug+slot
    scheme as the fill templates)."""
    return _set_type_curve(settings_data, DEFLATE_TYPE_PROFILES_KEY,
                           skin_type, skin_variant, slot, profile)


def resolve_fill_profiles(settings_data: dict,
                          skin_configs: list[dict]) -> list[dict]:
    """Return skin configs with each chamber's effective ``fill_profile`` and
    ``deflate_profile`` filled in.

    A chamber's own curve (a manual override) wins; otherwise the chamber
    inherits its skin's (skin_type, skin_variant, slot) type template. Produces
    shallow copies so the persisted settings dict is never mutated — the template
    stays the single source; a save must not bake the resolved curve onto every
    chamber. Chambers with neither keep no curve (pressure/uncalibrated)."""
    out: list[dict] = []
    for skin in skin_configs:
        st, sv = skin.get("skin_type"), skin.get("skin_variant")
        chambers = [_resolve_chamber(settings_data, st, sv, ch)
                    for ch in skin.get("chambers") or []]
        out.append({**skin, "chambers": chambers})
    return out


def _resolve_chamber(settings_data: dict, skin_type: Any, skin_variant: Any,
                     ch: dict) -> dict:
    """One chamber with its inherited curves patched in (copy-on-write)."""
    slot = ch.get("slot", 0)
    patch: dict = {}
    for key, getter in (("fill_profile", get_type_profile),
                        ("deflate_profile", get_type_deflate_profile)):
        if not ch.get(key):
            tmpl = getter(settings_data, skin_type, skin_variant, slot)
            if tmpl:
                patch[key] = tmpl
    return {**ch, **patch} if patch else ch


def chambers_missing_calibration(settings_data: dict) -> list[dict]:
    """Configured actuator chambers that still need a fill curve — used by the
    pre-activity guard to offer calibration.

    Chambers set to ``pressure`` fill mode inflate closed-loop on the gauge
    sensor and need no calibration, so they are never flagged."""
    return [c for c in iter_actuator_chambers(settings_data)
            if not c["calibrated"]
            and c["fill_mode"] != skin_config.FILL_MODE_PRESSURE]
