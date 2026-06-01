"""SimulatedIMU — mock node_imu (touch / sensor board) for simulation.

On real hardware a ``node_imu`` board streams touch activations (and, later,
organ bio-impedance readings) over ESP-NOW. In simulation there is no one
physically touching the skin, so the **T buttons** in the monitor UI feed this
object instead: a T-button press/release calls :meth:`fire_imu`, which notifies
every ``on_imu`` subscriber exactly as the real board would.

This keeps a clean split of responsibilities:

- ``SimulatedIMU``        — simulated **touch / sensor input** (this file).
- ``SimulatedController`` — simulated **chamber actuation** (pressures).

Activities subscribe to ``on_imu`` / ``on_organ`` without knowing whether the
source is a ``SimulatedIMU`` or a real ``ESP32Controller`` — so plugging in real
hardware leaves the activity behaviour identical.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


class SimulatedIMU:
    """Mock IMU/sensor node — emits ``on_imu`` (touch) and ``on_organ`` events.

    Mirrors the slice of the ``ESP32Controller`` API that activities use for
    sensing, so it can be dropped in as a skin's ``touch_controller`` in
    simulation. It performs no actuation.
    """

    def __init__(self, mac_address: str) -> None:
        self.mac_address = mac_address
        self._imu_callbacks:   list[Callable[[dict[str, Any]], None]] = []
        self._organ_callbacks: list[Callable[[float], None]] = []
        self._touch_callbacks: list[Callable[[int, int], None]] = []

    @property
    def is_connected(self) -> bool:
        return True

    @property
    def imu_geometry(self) -> dict[str, Any] | None:
        """Real boards announce their sensor/magnet geometry on boot; the
        simulated board has none to report."""
        return None

    # ------------------------------------------------------------------
    # Touch / IMU (the `act` set of currently-active sensors)
    # ------------------------------------------------------------------

    def on_imu(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register a callback for IMU messages (``{"act": [...], ...}``)."""
        self._imu_callbacks.append(callback)

    def fire_imu(self, data: dict[str, Any]) -> None:
        """Broadcast a synthetic IMU event to every ``on_imu`` subscriber.
        Tags the message with this board's MAC as ``source`` (unless one is
        already set) so activities can attribute it to the right skin."""
        if "source" not in data:
            data = {**data, "source": self.mac_address}
        for cb in self._imu_callbacks:
            cb(data)

    # ------------------------------------------------------------------
    # Per-sensor raw touch (capacitive-style); kept for interface parity
    # ------------------------------------------------------------------

    def on_touch(self, callback: Callable[[int, int], None]) -> None:
        self._touch_callbacks.append(callback)

    def fire_touch(self, sensor_id: int, raw_value: int) -> None:
        for cb in self._touch_callbacks:
            cb(sensor_id, raw_value)

    # ------------------------------------------------------------------
    # Organ bio-impedance (cure signal); debug-firable in simulation
    # ------------------------------------------------------------------

    def on_organ(self, callback: Callable[[float], None]) -> None:
        self._organ_callbacks.append(callback)

    def fire_organ(self, resistance_ohm: float) -> None:
        for cb in self._organ_callbacks:
            cb(float(resistance_ohm))
