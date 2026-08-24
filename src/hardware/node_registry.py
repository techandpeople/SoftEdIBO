"""NodeRegistry - one controller per physical node, shared across robots.

A single board can be configured on more than one robot: the Turtle and the
Tree are built on the SAME ``node_multiplexed`` PCB, swapped from one body to
the other (only the chambers and the body change). Both robots stay configured
in ``settings.yaml`` at the same time, so at runtime two robot objects claim the
same MAC.

Giving each robot its own :class:`~src.hardware.esp32_controller.ESP32Controller`
for that MAC would mean two owners for one board: two gateway message handlers
parsing every frame, two fill-load trackers splitting the pump budget wrongly,
and two limit confirmers. This registry hands out ONE controller per MAC
instead, so the board keeps a single owner no matter how many robots list it.

Which of the sharing robots the board is currently wearing is not tracked here:
that is decided when a robot claims it (see
:meth:`~src.robots.esp_robot.EspRobot.claim_board`).
"""

from __future__ import annotations

from src.hardware.esp32_controller import ESP32Controller
from src.hardware.gateway import Gateway


class NodeRegistry:
    """Cache of ``{mac: ESP32Controller}`` shared by every robot on a gateway.

    Args:
        gateway: The gateway all controllers talk through.
    """

    def __init__(self, gateway: Gateway):
        self._gateway = gateway
        self._controllers: dict[str, ESP32Controller] = {}

    @property
    def gateway(self) -> Gateway:
        """The gateway this registry's controllers talk through."""
        return self._gateway

    def controller(self, mac: str) -> ESP32Controller:
        """The controller for ``mac``, creating it on first use."""
        ctrl = self._controllers.get(mac)
        if ctrl is None:
            ctrl = ESP32Controller(mac, self._gateway)
            self._controllers[mac] = ctrl
        return ctrl

    def controllers_for(self, macs: list[str]) -> dict[str, ESP32Controller]:
        """``{mac: controller}`` for every MAC in ``macs`` (order preserved)."""
        return {mac: self.controller(mac) for mac in macs}

    @property
    def macs(self) -> list[str]:
        """Every MAC handed out so far."""
        return list(self._controllers)
