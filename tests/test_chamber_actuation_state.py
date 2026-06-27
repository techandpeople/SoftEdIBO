"""Firmware-reported chamber actuation state (``status`` ``st`` field).

The firmware reports whether a chamber is actually being driven (INFLATING /
DEFLATING) or not (IDLE). Before this, the PC inferred the state purely from
measured-pressure-vs-target, so a chamber whose pressure never reached its
target — e.g. with the pumps off — showed a perpetual INFLATING/DEFLATING.
These tests pin the firmware-authoritative behaviour and the legacy fallback.
"""

from src.hardware.air_chamber import AirChamber, ChamberState
from src.hardware.fill_scaling import FillLoadTracker
from src.hardware.skin import Skin


def _chamber() -> AirChamber:
    return AirChamber(chamber_id=0, esp32_mac="AA:01", max_pressure=8.0)


# ---------------------------------------------------------------------------
# AirChamber.update_pressure — firmware state is authoritative
# ---------------------------------------------------------------------------

def test_firmware_idle_holding_pressure_is_inflated():
    """The reported bug: held above a stale target with pumps off → INFLATED,
    not a perpetual DEFLATING."""
    ch = _chamber()
    ch.target_pressure = 80
    ch.update_pressure(98, ChamberState.IDLE)
    assert ch.state is ChamberState.INFLATED


def test_firmware_idle_empty_is_idle():
    """Never filled, pumps off (pressure below a stale target) → IDLE, not a
    perpetual INFLATING."""
    ch = _chamber()
    ch.target_pressure = 80
    ch.update_pressure(0, ChamberState.IDLE)
    assert ch.state is ChamberState.IDLE


def test_firmware_inflating_overrides_pressure_target():
    ch = _chamber()
    ch.target_pressure = 50            # already past target by measurement…
    ch.update_pressure(90, ChamberState.INFLATING)   # …but firmware still drives it
    assert ch.state is ChamberState.INFLATING


def test_firmware_deflating_reported_directly():
    ch = _chamber()
    ch.update_pressure(40, ChamberState.DEFLATING)
    assert ch.state is ChamberState.DEFLATING


# ---------------------------------------------------------------------------
# Legacy fallback — no firmware state (old firmware / simulator)
# ---------------------------------------------------------------------------

def test_legacy_inference_without_firmware_state():
    ch = _chamber()
    ch.target_pressure = 80
    ch.update_pressure(0)                  # pressure < target
    assert ch.state is ChamberState.INFLATING
    ch.update_pressure(80)                 # reached target
    assert ch.state is ChamberState.INFLATED


# ---------------------------------------------------------------------------
# Skin._on_pressure — maps the firmware ``st`` int through to the chamber
# ---------------------------------------------------------------------------

class _FakeNode:
    mac_address = "AA:01"

    def __init__(self) -> None:
        self.fill_load = FillLoadTracker(pump_count=2)

    def on_pressure(self, _cb) -> None:
        pass


def _skin() -> Skin:
    node = _FakeNode()
    inp = {"controller": node, "node_slot": 0, "max_pressure": 8.0}
    return Skin("belly", [inp])


def test_skin_maps_firmware_state_int():
    skin = _skin()
    chamber = skin._chambers[0]
    chamber.target_pressure = 80

    skin._on_pressure(0, 98, 0)            # st=0 idle, held above target
    assert chamber.state is ChamberState.INFLATED

    skin._on_pressure(0, 90, 1)            # st=1 inflating
    assert chamber.state is ChamberState.INFLATING

    skin._on_pressure(0, 40, 2)            # st=2 deflating
    assert chamber.state is ChamberState.DEFLATING


def test_skin_without_state_falls_back_to_inference():
    skin = _skin()
    chamber = skin._chambers[0]
    chamber.target_pressure = 80
    skin._on_pressure(0, 0)                # no st → pressure-vs-target inference
    assert chamber.state is ChamberState.INFLATING
