"""Tests for Skin's per-combination fill curves.

When a curve was measured for exactly the set of chambers about to fill together,
Skin inflates by that combination curve directly (the shared-pump slowdown is
baked in). Otherwise it scales the solo curve by the concurrent fill load. A
whole-skin broadcast inflate passes the co-active set explicitly so every chamber
matches the full combination, rather than the incrementally-growing fill-load
snapshot a per-chamber call sees.
"""

from src.hardware.fill_scaling import FillLoadTracker
from src.hardware.skin import Skin

# Solo: 1000 ms to full -> 500 ms to 50 %. Combo {0,1}: twice as slow (shared
# pump) -> 2000 ms to full -> 1000 ms to 50 %. The two are easy to tell apart.
_SOLO = [[0, 0], [1000, 100]]
_COMBO_01 = [[0, 0], [2000, 100]]


class _RecordingCtrl:
    def __init__(self, clock) -> None:
        self.mac_address = "AA:01"
        self.fill_load = FillLoadTracker(pump_count=2, clock=clock)
        self.inflate_calls: list[dict] = []

    def on_pressure(self, _cb) -> None:
        pass

    def inflate(self, chamber: int, delta: int = 10, ms=None) -> bool:
        self.inflate_calls.append({"chamber": chamber, "delta": delta, "ms": ms})
        return True


def _two_chamber_skin():
    # Frozen clock so fill-load windows never expire mid-test.
    ctrl = _RecordingCtrl(clock=lambda: 0.0)
    inputs = [
        {"controller": ctrl, "node_slot": 0, "max_pressure": 8.0,
         "fill_profile": _SOLO, "fill_profiles": {"0,1": _COMBO_01}},
        {"controller": ctrl, "node_slot": 1, "max_pressure": 8.0,
         "fill_profile": _SOLO, "fill_profiles": {"0,1": _COMBO_01}},
    ]
    return Skin("belly", inputs), ctrl


def test_broadcast_inflate_uses_full_combination_curve():
    skin, ctrl = _two_chamber_skin()
    assert skin.inflate(None, 50)              # inflate the whole skin together
    # Both chambers fill together -> both use the {0,1} combo curve (1000 ms),
    # not the solo curve scaled (which would be 500 ms).
    assert [c["ms"] for c in ctrl.inflate_calls] == [1000, 1000]


def test_lone_chamber_falls_back_to_scaled_solo():
    skin, ctrl = _two_chamber_skin()
    assert skin.inflate(0, 50)                 # slot 0 alone, no {0} combo exists
    # Falls back to solo (500 ms); one chamber on two pumps -> no slowdown.
    assert ctrl.inflate_calls[-1]["ms"] == 500


def test_overlapping_per_chamber_inflates_match_combo_via_snapshot():
    skin, ctrl = _two_chamber_skin()
    skin.inflate(0, 50)                         # starts slot 0 (solo path, 500 ms)
    skin.inflate(1, 50)                         # slot 0 still active -> set {0,1}
    assert ctrl.inflate_calls[0]["ms"] == 500   # slot 0 alone at the time
    assert ctrl.inflate_calls[1]["ms"] == 1000  # slot 1 matched the {0,1} combo


def test_chamber_defs_round_trips_combo_curves():
    skin, _ = _two_chamber_skin()
    defs = {d["slot"]: d for d in skin.chamber_defs}
    assert defs[0]["fill_profiles"] == {"0,1": _COMBO_01}
    assert defs[1]["fill_profiles"] == {"0,1": _COMBO_01}
