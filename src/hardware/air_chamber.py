"""Air chamber model representing a single inflatable chamber."""

import threading
from enum import Enum

from src.hardware.units import kpa_to_pct


class ChamberState(Enum):
    """Possible states of an air chamber."""
    IDLE = "idle"
    INFLATING = "inflating"
    DEFLATING = "deflating"
    INFLATED = "inflated"


class AirChamber:
    """Represents a single air chamber with inflate/deflate capabilities."""

    def __init__(
        self,
        chamber_id: int,
        esp32_mac: str,
        max_pressure: float = 8.0,
        min_pressure: float = 0.0,
    ):
        self.chamber_id = chamber_id
        self.esp32_mac = esp32_mac
        # Configured per-chamber pressure range in kPa. ``min_pressure`` is
        # negative for vacuum-fed chambers (0 % == deepest vacuum).
        self.max_pressure = max(0.0, float(max_pressure))
        self.min_pressure = float(min_pressure)
        self._state = ChamberState.IDLE
        self._pressure: int = 0         # 0-100 (% of configured range), current measured value
        self._target_pressure: int = 0  # 0-100 (% of configured range), commanded target
        # Latest measured absolute pressure in kPa (NaN until the firmware sends
        # one; the simulator and pre-kPa firmware leave it NaN).
        self._kpa: float = float("nan")
        # Protects compound read-compare-write between the hardware thread
        # (update_pressure) and the main thread (target_pressure setter).
        self._lock = threading.Lock()

    @property
    def state(self) -> ChamberState:
        """Get current chamber state."""
        return self._state

    @state.setter
    def state(self, value: ChamberState) -> None:
        self._state = value

    @property
    def pressure(self) -> int:
        """Get current pressure level (0-100 % of chamber range)."""
        return self._pressure

    @property
    def kpa(self) -> float:
        """Latest measured absolute pressure in kPa, or NaN if unknown.

        NaN means no firmware kPa has arrived (the simulator, or firmware too
        old to report it) - consumers fall back to the percentage.
        """
        return self._kpa

    @pressure.setter
    def pressure(self, value: int) -> None:
        self._pressure = max(0, min(100, value))

    @property
    def target_pressure(self) -> int:
        """Get commanded target pressure (0-100 % of chamber max)."""
        return self._target_pressure

    @target_pressure.setter
    def target_pressure(self, value: int) -> None:
        with self._lock:
            self._target_pressure = max(0, min(100, value))

    def update_pressure(self, pressure: int,
                        actuating: "ChamberState | None" = None,
                        kpa: float = float("nan")) -> None:
        """Update measured pressure and derive state atomically.

        Called from the hardware (serial) thread. Acquires the lock so that
        the read of target_pressure and the write of state are atomic with
        respect to the main thread's target_pressure setter.

        ``actuating`` is the chamber's *real* actuation state as reported by the
        firmware status: ``INFLATING``/``DEFLATING`` while a pump is driving it,
        ``IDLE`` once it stops. When given it is authoritative - a firmware-idle
        chamber settles to ``INFLATED`` (holding a level) or ``IDLE`` (empty), so
        a chamber whose pressure never reached its target (e.g. the pumps are
        off) no longer shows a perpetual INFLATING/DEFLATING. When ``None`` (old
        firmware that doesn't report it, or the simulator) the state is inferred
        from measured pressure vs the commanded target, as before.

        ``kpa`` is the measured absolute pressure. When present (real firmware)
        it is authoritative: the percentage is recomputed from it against this
        chamber's *configured* ``[min, max]`` range, exactly like the Test
        Actuators dialog. The firmware's own ``pressure`` field is computed
        against the limits the node currently holds, which lag the PC config (a
        dropped ``set_max_pressure``, or the 8 kPa boot default), so trusting it
        makes the live readout disagree with the configured range. When NaN
        (simulator / pre-kPa firmware) the given ``pressure`` is used as-is.
        """
        with self._lock:
            if kpa == kpa:  # not NaN -> recompute % from the configured range
                self._kpa = kpa
                self._pressure = kpa_to_pct(kpa, self.min_pressure, self.max_pressure)
            else:
                self._pressure = max(0, min(100, pressure))
            if actuating is not None:
                self._state = (self._settled_state()
                               if actuating is ChamberState.IDLE else actuating)
                return
            target = self._target_pressure
            if self._pressure == target:
                self._state = ChamberState.INFLATED if target > 0 else ChamberState.IDLE
            elif self._pressure < target:
                self._state = ChamberState.INFLATING
            else:
                self._state = ChamberState.DEFLATING

    def _settled_state(self) -> ChamberState:
        """State of a chamber the firmware reports as not actuating: holding a
        level (``INFLATED``) or empty (``IDLE``)."""
        return ChamberState.INFLATED if self._pressure > 0 else ChamberState.IDLE

    def __repr__(self) -> str:
        return (
            f"AirChamber(id={self.chamber_id}, "
            f"state={self._state.value}, pressure={self._pressure}%, max={self.max_pressure}kPa)"
        )
