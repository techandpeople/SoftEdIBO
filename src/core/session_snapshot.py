"""What a session ran with, captured when it starts.

A session's events only make sense next to the conditions they were recorded
under: the activity definition in use, the robots and skins, the touch tuning
in force and the firmware on each node. Any of these can be edited between
sessions, so they are copied into the database at session start.

Pure Python over plain dicts: no Qt, no hardware objects, no database.
"""

from __future__ import annotations

import copy
from typing import Any

from src.core.skin_config import YAML_KEY

# Snapshot kinds (the ``kind`` column of the ``session_snapshots`` table).
KIND_ACTIVITY = "activity"
KIND_ROBOTS = "robots"
KIND_TOUCH = "touch"
KIND_FIRMWARE = "firmware"

# Key that holds a robot's id in its settings entry, per robot type.
_ID_KEYS = ("id", "thymio_id")


def _robot_entries(settings_data: dict, robot_ids: list[str]) -> list[dict]:
    """Settings entries (with their skins and nodes) of the session's robots."""
    wanted = set(robot_ids)
    entries: list[dict] = []
    robots = settings_data.get("robots", {}) or {}
    for robot_type, key in YAML_KEY.items():
        for entry in robots.get(key, []) or []:
            robot_id = next(
                (entry[k] for k in _ID_KEYS if entry.get(k)), None)
            if robot_id in wanted:
                entries.append({"robot_type": robot_type,
                                **copy.deepcopy(entry)})
    return entries


def build_session_snapshots(
        *, activity_name: str, activity_spec: dict[str, Any] | None,
        settings_data: dict, robot_ids: list[str],
        firmware: dict[str, str] | None = None) -> dict[str, dict]:
    """Return ``{kind: payload}`` describing what the session runs with.

    ``firmware`` maps each node MAC to the firmware marker it reported (empty
    when the node did not report one)."""
    return {
        KIND_ACTIVITY: {"name": activity_name,
                        "spec": copy.deepcopy(activity_spec)},
        KIND_ROBOTS: {"robots": _robot_entries(settings_data, robot_ids)},
        KIND_TOUCH: copy.deepcopy(settings_data.get("touch", {}) or {}),
        KIND_FIRMWARE: dict(firmware or {}),
    }


def node_firmware(robots: list[Any]) -> dict[str, str]:
    """Firmware marker each node of ``robots`` last reported, by MAC.

    Reads the controllers through their public interface; simulated
    controllers (no status, no MAC) are skipped."""
    firmware: dict[str, str] = {}
    for robot in robots:
        for skin in getattr(robot, "skins", {}).values():
            for ctrl in (getattr(skin, "controller", None),
                         getattr(skin, "touch_controller", None)):
                mac = getattr(ctrl, "mac_address", None)
                status = getattr(ctrl, "get_last_status", None)
                if not mac or status is None:
                    continue
                firmware[str(mac)] = str(status().get("fw", ""))
    return firmware
