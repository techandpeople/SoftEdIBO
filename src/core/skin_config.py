"""Skin-configuration domain logic — pure, Qt-free, testable.

Extracted from ``src/gui/skin_config_dialog.py`` so the rules that read/write a
skin entry in the settings tree (node lookup, validation, entry building,
persistence) live independently of the dialog widgets. The dialog keeps only
presentation + wiring and calls into here.

Everything operates on the plain ``settings.data`` dict (the same structure
persisted to ``settings.yaml``), so it can be unit-tested without a GUI.
"""

from __future__ import annotations

from dataclasses import dataclass

# YAML list key under ``robots`` for each robot kind.
YAML_KEY: dict[str, str] = {"turtle": "turtles", "tree": "trees", "thymio": "thymios"}

DEFAULT_MAX_KPA = 8.0
# Upper/lower bound for the per-chamber Max/Min the config dialog accepts. These
# are intentionally generous (effectively uncapped for these low-pressure air
# chambers) — the pressure gauge is unreliable, so over-pressure is held off by
# TIME-based backstops (firmware MAX_FILL_MS, the manual dead-man, and the
# actuation watchdog), not by an artificial pressure ceiling. Kept in sync with
# the firmware HARD_*_KPA constants.
MAX_ALLOWED_KPA = 100.0
# Lower cap: 0 by default; negative for chambers fed from a vacuum reservoir.
DEFAULT_MIN_KPA = 0.0
MIN_ALLOWED_KPA = -100.0
CONFIRM_DELTA = 2.0
MAX_CHAMBERS = 3

# How a chamber decides when to stop inflating:
#   "time"     — open the inflate valve for a calibrated time window (fill_profile);
#                the firmware never closes the loop on the laggy pressure sensor.
#   "pressure" — classic closed loop: inflate until the gauge sensor hits target.
# Deflate is always pressure-based above atmosphere (and time-bounded below, since
# the gauge sensor is blind to vacuum), so this only governs inflation.
FILL_MODE_TIME = "time"
FILL_MODE_PRESSURE = "pressure"
DEFAULT_FILL_MODE = FILL_MODE_TIME
FILL_MODES = (FILL_MODE_TIME, FILL_MODE_PRESSURE)


def normalize_fill_mode(value: object) -> str:
    """Coerce a stored/UI fill-mode value to a known mode (default ``time``)."""
    return value if value in FILL_MODES else DEFAULT_FILL_MODE
# Node types that actually own chambers (can inflate/deflate). Sensor-only
# boards (node_magnet_sensor) are excluded from chamber selection.
ACTUATOR_NODE_TYPES = ("node_direct", "node_multiplexed")
# Node types that can stream magnet/touch data, hence be picked as a skin's
# touch node. Either the dedicated sensor board, or a ``node_direct`` that folds
# the MLX90393 sensing into the actuator firmware (same board, same MAC, so the
# touch node is the actuator's own MAC). ``node_multiplexed`` has no magnet bus.
MAGNET_NODE_TYPES = ("node_magnet_sensor", "node_direct")


# ---------------------------------------------------------------------------
# Settings-tree navigation
# ---------------------------------------------------------------------------

def robot_nodes(data: dict, robot_type: str, robot_index: int) -> list[dict]:
    """Return the node list of the parent robot, or ``[]`` if out of range."""
    robots_list = data.get("robots", {}).get(YAML_KEY[robot_type], [])
    if 0 <= robot_index < len(robots_list):
        return robots_list[robot_index].get("nodes", [])
    return []


def actuator_macs(data: dict, robot_type: str, robot_index: int) -> list[str]:
    """MACs of actuator boards (chambers live only on these)."""
    return [n["mac"] for n in robot_nodes(data, robot_type, robot_index)
            if n.get("mac") and n.get("node_type") in ACTUATOR_NODE_TYPES]


def node_max_slots(data: dict, robot_type: str, robot_index: int) -> dict[str, int]:
    """Map actuator MAC -> its ``max_slots`` (default 3)."""
    return {n["mac"]: int(n.get("max_slots", 3))
            for n in robot_nodes(data, robot_type, robot_index)
            if n.get("mac") and n.get("node_type") in ACTUATOR_NODE_TYPES}


def magnet_macs(data: dict, robot_type: str, robot_index: int) -> list[str]:
    """MACs of nodes that can serve as a skin's touch node — the dedicated
    ``node_magnet_sensor`` boards plus any ``node_direct`` (which folds the
    magnet sensing into its own firmware, so its actuator MAC is also its
    touch MAC)."""
    return [n["mac"] for n in robot_nodes(data, robot_type, robot_index)
            if n.get("node_type") in MAGNET_NODE_TYPES and n.get("mac")]


def load_skin_cfg(data: dict, robot_type: str, robot_index: int,
                  skin_index: int) -> dict:
    """Return the saved skin entry, or ``{}`` for a new skin / bad index."""
    if skin_index < 0:
        return {}
    robots_list = data.get("robots", {}).get(YAML_KEY[robot_type], [])
    if 0 <= robot_index < len(robots_list):
        skins = robots_list[robot_index].get("skins", [])
        if 0 <= skin_index < len(skins):
            return skins[skin_index]
    return {}


def sibling_skins(data: dict, robot_type: str, robot_index: int,
                  skin_index: int) -> list[dict]:
    """All other skins on the same robot (excludes the one being edited)."""
    robots_list = data.get("robots", {}).get(YAML_KEY[robot_type], [])
    if not (0 <= robot_index < len(robots_list)):
        return []
    all_skins = robots_list[robot_index].get("skins", [])
    return [sc for i, sc in enumerate(all_skins) if i != skin_index]


def next_skin_id(name_source: str, existing_skin_ids: list[str]) -> str:
    """Return ``{prefix}-{N}`` where prefix derives from ``name_source`` and N
    is one past the highest existing ``{prefix}-{n}`` among ``existing_skin_ids``."""
    prefix = (name_source or "").strip().lower().replace(" ", "_") or "skin"
    used_nums: list[int] = []
    for sid in existing_skin_ids:
        sid = str(sid).lower()
        if sid.startswith(prefix + "-"):
            try:
                used_nums.append(int(sid.rsplit("-", 1)[1]))
            except ValueError:
                pass
    n = (max(used_nums) + 1) if used_nums else 1
    return f"{prefix}-{n}"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@dataclass
class PressureChange:
    """A chamber whose max_pressure shifted by more than ``CONFIRM_DELTA``."""
    mac: str
    slot: int
    old_kpa: float
    new_kpa: float


def find_missing_mac(chambers: list[dict]) -> bool:
    """True if any chamber row has no node MAC selected."""
    return any(not ch.get("mac") for ch in chambers)


def find_duplicate(chambers: list[dict]) -> dict | None:
    """Return the first chamber that repeats a (mac, slot) already seen, else None."""
    seen: set[tuple[str, int]] = set()
    for ch in chambers:
        key = (ch["mac"], ch["slot"])
        if key in seen:
            return ch
        seen.add(key)
    return None


def sibling_conflicts(chambers: list[dict], siblings: list[dict]) -> list[dict]:
    """Chambers whose (mac, slot) is already used by another skin."""
    used = {(ch.get("mac"), ch.get("slot"))
            for sk in siblings for ch in sk.get("chambers", [])}
    return [ch for ch in chambers if (ch["mac"], ch["slot"]) in used]


def large_pressure_changes(chambers: list[dict], prev_chambers: list[dict],
                           threshold: float = CONFIRM_DELTA,
                           default_kpa: float = DEFAULT_MAX_KPA
                           ) -> list[PressureChange]:
    """Chambers whose max_pressure moved by >= ``threshold`` vs the saved skin."""
    prev = {(c.get("mac"), c.get("slot")): c for c in prev_chambers}
    out: list[PressureChange] = []
    for ch in chambers:
        key = (ch["mac"], ch["slot"])
        old_max = float(prev.get(key, {}).get("max_pressure", default_kpa))
        if abs(ch["max_pressure"] - old_max) >= threshold:
            out.append(PressureChange(ch["mac"], ch["slot"], old_max,
                                      ch["max_pressure"]))
    return out


# ---------------------------------------------------------------------------
# Entry building + persistence
# ---------------------------------------------------------------------------

def build_skin_entry(skin_id: str, chambers: list[dict],
                     skin_type: str = "", skin_variant: str = "") -> dict:
    """Build the base skin entry (chambers + metadata).

    ``max_pressure``/``min_pressure`` are dropped from a chamber when they equal
    the defaults, so the YAML stays terse. Layout/touch fields are added by the
    caller afterwards.
    """
    out_chambers: list[dict] = []
    for ch in chambers:
        c = dict(ch)
        if abs(c.get("max_pressure", DEFAULT_MAX_KPA) - DEFAULT_MAX_KPA) < 1e-9:
            c.pop("max_pressure", None)
        if abs(c.get("min_pressure", DEFAULT_MIN_KPA) - DEFAULT_MIN_KPA) < 1e-9:
            c.pop("min_pressure", None)
        # Keep the YAML terse: only persist a non-default fill mode.
        if normalize_fill_mode(c.get("fill_mode")) == DEFAULT_FILL_MODE:
            c.pop("fill_mode", None)
        out_chambers.append(c)
    entry: dict = {"skin_id": skin_id, "chambers": out_chambers}
    if skin_type:
        entry["skin_type"] = skin_type
    if skin_variant:
        entry["skin_variant"] = skin_variant
    return entry


def apply_organs(entry: dict, organs: list[dict]) -> None:
    """Write (or drop) the ``organs`` list on a skin entry.

    Each organ is ``{"id", "shape", "rect": [x, y, w, h], "good_ohm",
    "bad_ohm"}`` with ``rect`` normalised to the skin bounding box. An empty
    list removes the key so the YAML stays terse for organ-less skins."""
    if organs:
        entry["organs"] = [dict(o) for o in organs]
    else:
        entry.pop("organs", None)


def save_skin_entry(data: dict, robot_type: str, robot_index: int,
                    skin_index: int, entry: dict) -> None:
    """Append (skin_index < 0) or replace the skin entry in the settings tree."""
    robots_list = (data.setdefault("robots", {})
                   .setdefault(YAML_KEY[robot_type], []))
    if 0 <= robot_index < len(robots_list):
        skins = robots_list[robot_index].setdefault("skins", [])
        if skin_index < 0:
            skins.append(entry)
        else:
            skins[skin_index] = entry


def delete_skin(data: dict, robot_type: str, robot_index: int,
                skin_index: int) -> None:
    """Remove the skin entry from the settings tree (no-op on bad index)."""
    robots_list = data.get("robots", {}).get(YAML_KEY[robot_type], [])
    if 0 <= robot_index < len(robots_list):
        skins = robots_list[robot_index].get("skins", [])
        if 0 <= skin_index < len(skins):
            skins.pop(skin_index)
