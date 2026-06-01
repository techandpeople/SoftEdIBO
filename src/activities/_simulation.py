"""Helper: wrap any list of real BaseRobot instances in SimulatedRobot.

Lives in its own module so it can be reused both by ``BaseActivity`` (when
``simulation_mode=True``) and by the legacy ``SimulationActivity`` without
forcing a circular import between activities and robots.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from src.robots.base_robot import BaseRobot

if TYPE_CHECKING:
    from src.robots.simulated_robot import SimulatedRobot

logger = logging.getLogger(__name__)


def wrap_robots_in_simulation(
    robots: list[BaseRobot],
    sim_params: dict[str, Any] | None = None,
) -> list[BaseRobot]:
    """Return a list of SimulatedRobot mirroring each input robot.

    Skin layouts (``grid`` / ``chamber_grid`` / ``touch``) are forwarded so
    the activity-time view (``SkinGridView``) renders the same chamber zones
    in simulation as on real hardware.

    ``sim_params`` is the activity's live ``param_values`` (or the subset that
    starts with ``sim_``). When given, the relevant keys are forwarded to
    each SimulatedController so the operator-tunable inflate/deflate speeds
    take effect.
    """
    from src.robots.simulated_robot import SimulatedRobot

    sims: list[BaseRobot] = []
    for robot in robots:
        skins = getattr(robot, "skins", {})
        skin_configs = [
            {
                "skin_id":      skin.skin_id,
                "name":         skin.name,
                "chambers":     skin.chamber_defs,
                "grid":         skin.grid,
                "chamber_grid": skin.chamber_grid,
                "touch":        skin.touch,
                "shape":        skin.shape,
            }
            for skin in skins.values()
        ]
        tank_kinds: list[str] = []
        if getattr(robot, "pressure_reservoir", None) is not None:
            tank_kinds.append("pressure")
        if getattr(robot, "vacuum_reservoir", None) is not None:
            tank_kinds.append("vacuum")

        sim: SimulatedRobot = SimulatedRobot(
            robot.robot_id, robot.name, skin_configs,
            tank_kinds=tank_kinds,
            sim_params=sim_params,
        )
        sims.append(sim)
        logger.debug("Simulating %s (tanks=%s, sim_params=%s)",
                     robot.robot_id, tank_kinds, sim_params)
    return sims
