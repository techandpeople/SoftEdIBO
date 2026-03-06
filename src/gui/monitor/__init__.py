"""Robot monitor subpackage.

Public API:
    RobotMonitorPanel  — embed in any panel; call set_robots(robots) to populate
"""

from src.gui.monitor.robot_monitor_panel import RobotMonitorPanel

__all__ = ["RobotMonitorPanel"]
