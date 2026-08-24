"""Shared-node domain logic - which robots are built on the same board.

One physical PCB can be configured on more than one robot: the Turtle and the
Tree share a single ``node_multiplexed`` board that is moved from one body to
the other, and both robots stay configured so neither has to be re-entered on
every swap. The consequence is that the same MAC appears under two robots, and
the app has to keep them apart at runtime:

* only one of them can be physically mounted, so a session must not run two
  robots that share a board (see the session setup dialog);
* the node is one device, so the node lists (OTA, discovery pickers) show it
  once, marked with the robots that share it.

Pure dict/attribute reading - Qt-free and with no robot-class imports - so it
unit-tests without a GUI. Works on the ``settings.data`` tree (config side) and
on live robot objects (runtime side).
"""

from __future__ import annotations

from typing import Any, Iterable


def robot_id_of(robot_cfg: dict[str, Any]) -> str:
    """The id of a robot config entry (``id``, or ``thymio_id`` for Thymios)."""
    return str(robot_cfg.get("id") or robot_cfg.get("thymio_id") or "")


def robot_ids_by_mac(settings_data: dict[str, Any]) -> dict[str, list[str]]:
    """``{mac: [robot ids that list it]}`` across every configured robot kind.

    Ids keep their config order, and repeat only when a MAC really is listed by
    more than one robot (a MAC repeated inside one robot collapses to one entry).
    """
    out: dict[str, list[str]] = {}
    for group in (settings_data.get("robots") or {}).values():
        if not isinstance(group, list):
            continue
        for robot_cfg in group:
            rid = robot_id_of(robot_cfg)
            for node in robot_cfg.get("nodes", []):
                mac = node.get("mac")
                if mac and rid not in out.setdefault(mac, []):
                    out[mac].append(rid)
    return out


def shared_macs(settings_data: dict[str, Any]) -> dict[str, list[str]]:
    """``{mac: [robot ids]}`` for the MACs listed by more than one robot."""
    return {mac: rids for mac, rids in robot_ids_by_mac(settings_data).items()
            if len(rids) > 1}


def macs_of(settings_data: dict[str, Any], yaml_key: str,
            robot_index: int) -> set[str]:
    """MACs already configured on one robot (``robots[yaml_key][robot_index]``).

    Used by the "add node" picker: a node is only off-limits for the robot that
    ALREADY has it, since another robot may legitimately share the board.
    """
    group = (settings_data.get("robots") or {}).get(yaml_key) or []
    if not 0 <= robot_index < len(group):
        return set()
    return {n.get("mac", "") for n in group[robot_index].get("nodes", [])
            if n.get("mac")}


def conflicts(robots: Iterable[Any]) -> dict[str, list[str]]:
    """``{mac: [robot ids]}`` for boards shared by two or more of ``robots``.

    Takes any robot object: those without ``node_macs`` (a bare wireless Thymio,
    a simulated robot) own no board and so can never clash. Empty when the
    selection is physically possible - no two robots need the same board at the
    same time.
    """
    by_mac: dict[str, list[str]] = {}
    for robot in robots:
        for mac in getattr(robot, "node_macs", []) or []:
            rid = getattr(robot, "robot_id", "")
            if rid not in by_mac.setdefault(mac, []):
                by_mac[mac].append(rid)
    return {mac: rids for mac, rids in by_mac.items() if len(rids) > 1}


def conflict_message(clashes: dict[str, list[str]]) -> str:
    """Human explanation for :func:`conflicts` (empty string when there is none)."""
    if not clashes:
        return ""
    lines = ["ERROR: These robots share the same board, so only one of them "
             "can be mounted at a time:"]
    for mac, rids in clashes.items():
        lines.append(f"{', '.join(rids)} - node {mac}")
    return "\n".join(lines)
