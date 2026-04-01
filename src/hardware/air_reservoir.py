"""AirReservoir — monitored pressure or vacuum reservoir shared across chambers.

A reservoir is an optional per-robot component. It is backed by a dedicated
ESP32 node (node_type: reservoir) that reports its pressure via the standard
status message format and drives one or more pumps.

The software treats it as read-only for activities: activities only see the
pressure level. Refilling is handled autonomously by the reservoir firmware.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)


class AirReservoir:
    """Monitored air reservoir (pressure or vacuum) backed by an ESP32 node.

    Args:
        kind:        ``"pressure"`` or ``"vacuum"``.
        controller:  ESP32 controller for the reservoir node.
        node_slot:   Which slot on the node reports reservoir pressure (default 0).
        pump_count:  Number of pumps on this reservoir (informational; firmware
                     manages them autonomously).
    """

    def __init__(
        self,
        kind: Literal["pressure", "vacuum"],
        controller: Any,
        node_slot: int = 0,
        pump_count: int = 1,
    ) -> None:
        self.kind = kind
        self.mac = controller.mac_address
        self.pump_count = pump_count
        self._controller = controller
        self._node_slot = node_slot
        self._pressure: int = 0
        self._is_active: bool = False

        controller.on_pressure(self._on_pressure_update)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def pressure(self) -> int:
        """Current reservoir pressure (0-100 % of its configured max)."""
        return self._pressure

    @property
    def is_active(self) -> bool:
        """True while the reservoir firmware is actively pumping."""
        return self._is_active

    @property
    def is_connected(self) -> bool:
        """True if the reservoir node is reachable via the gateway."""
        return self._controller.is_connected

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_pressure_update(self, node_slot: int, pressure: int) -> None:
        if node_slot == self._node_slot:
            self._pressure = pressure
            logger.debug(
                "Reservoir %s (%s) pressure: %d%%", self.kind, self.mac, pressure
            )

    def get_status(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "mac": self.mac,
            "pressure": self._pressure,
            "pump_count": self.pump_count,
            "connected": self.is_connected,
        }

    def __repr__(self) -> str:
        return (
            f"AirReservoir(kind={self.kind!r}, mac={self.mac!r}, "
            f"pressure={self._pressure}%, pumps={self.pump_count})"
        )
