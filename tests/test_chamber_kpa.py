"""Live percentage is recomputed from measured kPa against the *configured*
range — not trusted from the firmware ``pressure`` field.

The firmware computes its ``pressure`` % against the limits the node currently
holds, which lag the PC config (a dropped ``set_max_pressure``, or the 8 kPa
boot default). The Test Actuators dialog already recomputes from the reported
kPa against the configured min/max; these tests pin the live session path doing
the same, and the fallback to the firmware % when no kPa is reported.
"""

import math

from src.hardware.air_chamber import AirChamber, ChamberState
from src.hardware.units import kpa_to_pct, pct_to_kpa


# ---------------------------------------------------------------------------
# units.kpa_to_pct — mirror of firmware units::kpaToPct
# ---------------------------------------------------------------------------

def test_kpa_to_pct_basic_range():
    assert kpa_to_pct(0.0, 0.0, 20.0) == 0
    assert kpa_to_pct(20.0, 0.0, 20.0) == 100
    assert kpa_to_pct(10.0, 0.0, 20.0) == 50


def test_kpa_to_pct_rounds_half_up():
    # 6.49 / 20 = 32.45 % -> 32 ; 6.5 / 20 = 32.5 % -> 33 (round-half-up)
    assert kpa_to_pct(6.49, 0.0, 20.0) == 32
    assert kpa_to_pct(6.50, 0.0, 20.0) == 33


def test_kpa_to_pct_clamps_and_handles_bad_span():
    assert kpa_to_pct(99.0, 0.0, 20.0) == 100   # above max clamps
    assert kpa_to_pct(-5.0, 0.0, 20.0) == 0      # below min clamps
    assert kpa_to_pct(5.0, 8.0, 8.0) == 0        # zero span -> 0


def test_kpa_to_pct_vacuum_range():
    # Vacuum-fed chamber: 0 % is the deepest vacuum (min), 100 % is atmosphere.
    assert kpa_to_pct(-5.0, -5.0, 0.0) == 0
    assert kpa_to_pct(0.0, -5.0, 0.0) == 100
    assert kpa_to_pct(-2.5, -5.0, 0.0) == 50


def test_pct_to_kpa_inverse():
    assert pct_to_kpa(0, 0.0, 20.0) == 0.0
    assert pct_to_kpa(100, 0.0, 20.0) == 20.0
    assert pct_to_kpa(50, 0.0, 20.0) == 10.0


# ---------------------------------------------------------------------------
# AirChamber.update_pressure — kPa is authoritative when present
# ---------------------------------------------------------------------------

def _chamber(max_p: float = 20.0, min_p: float = 0.0) -> AirChamber:
    return AirChamber(chamber_id=0, esp32_mac="AA:01",
                      max_pressure=max_p, min_pressure=min_p)


def test_kpa_recomputed_against_config_not_firmware_pct():
    """Firmware sent pressure=81 (its stale 8 kPa boot range) but the real kPa
    against the configured 20 kPa max is 32 % — the configured value wins."""
    ch = _chamber(max_p=20.0)
    ch.update_pressure(81, ChamberState.IDLE, kpa=6.49)
    assert ch.pressure == 32
    assert ch.kpa == 6.49


def test_no_kpa_falls_back_to_firmware_pct():
    """Simulator / pre-kPa firmware send no kPa (NaN) → the firmware % is used."""
    ch = _chamber()
    ch.update_pressure(46)
    assert ch.pressure == 46
    assert math.isnan(ch.kpa)


def test_kpa_drives_state_against_target():
    ch = _chamber(max_p=20.0)
    ch.target_pressure = 50
    ch.update_pressure(0, kpa=2.0)   # 10 % < 50 % target, no firmware state
    assert ch.pressure == 10
    assert ch.state is ChamberState.INFLATING
