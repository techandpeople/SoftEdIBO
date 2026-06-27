"""The live session re-asserts each chamber's configured min/max on the node
before every actuation.

ESP-NOW is fire-and-forget (no ACK yet), so the one-shot push at Skin
construction can be dropped, and external tools (the Test Actuators dialog, an
"ignore max" run) can leave a stale ``max_kpa`` on the node. The firmware clamps
an inflate to whatever limit it currently holds, so without re-pushing, repeated
+ steps could climb far past the configured max (observed: a 20 kPa chamber
reaching ~50 kPa). These tests pin the re-push, mirroring the Test Actuators
dialog which already does it.
"""

from src.hardware.fill_scaling import FillLoadTracker
from src.hardware.skin import Skin


class _RecordingNode:
    """Controller stand-in that records every command in order."""

    mac_address = "AA:01"

    def __init__(self) -> None:
        self.fill_load = FillLoadTracker(pump_count=2)
        self.sent: list[tuple[str, tuple]] = []

    def on_pressure(self, _cb) -> None:
        pass

    def set_max_pressure(self, slot: int, value: float) -> bool:
        self.sent.append(("set_max_pressure", (slot, value)))
        return True

    def set_min_pressure(self, slot: int, value: float) -> bool:
        self.sent.append(("set_min_pressure", (slot, value)))
        return True

    def inflate(self, slot: int, delta: int, ms: int | None = None) -> bool:
        self.sent.append(("inflate", (slot, delta, ms)))
        return True

    def deflate(self, slot: int, delta: int) -> bool:
        self.sent.append(("deflate", (slot, delta)))
        return True

    def set_pressure(self, slot: int, value: int) -> bool:
        self.sent.append(("set_pressure", (slot, value)))
        return True


def _skin(node: _RecordingNode) -> Skin:
    inp = {"controller": node, "node_slot": 0,
           "max_pressure": 20.0, "min_pressure": -2.0, "fill_mode": "pressure"}
    skin = Skin("organ", [inp])
    node.sent.clear()   # drop the construction-time push
    return skin


def _cmds(node: _RecordingNode) -> list[str]:
    return [name for name, _ in node.sent]


def test_inflate_repushes_limits_first():
    node = _RecordingNode()
    skin = _skin(node)
    skin.inflate(0, 10)
    # Limits go out (max then min) before the inflate so the node clamps to the
    # configured cap, not whatever stale max it currently holds.
    assert _cmds(node) == ["set_max_pressure", "set_min_pressure", "inflate"]
    assert node.sent[0][1] == (0, 20.0)
    assert node.sent[1][1] == (0, -2.0)


def test_deflate_repushes_limits_first():
    node = _RecordingNode()
    skin = _skin(node)
    skin.deflate(0, 10)
    assert _cmds(node) == ["set_max_pressure", "set_min_pressure", "deflate"]


def test_set_pressure_repushes_limits_first():
    node = _RecordingNode()
    skin = _skin(node)
    skin.set_pressure(0, 100)
    assert _cmds(node) == ["set_max_pressure", "set_min_pressure", "set_pressure"]


def test_inflate_is_noop_when_already_at_target():
    """Holding + at the cap must not keep firing inflates. The firmware runs the
    pump for one control cycle per inflate regardless of current pressure, so
    re-issuing at max creeps past the configured limit a pulse at a time."""
    node = _RecordingNode()
    skin = _skin(node)
    chamber = skin._chambers[0]
    chamber.pressure = 100          # already full
    chamber.target_pressure = 100
    assert skin.inflate(0, 10)      # clicking + again
    assert "inflate" not in _cmds(node)


def test_inflate_still_fires_when_below_target():
    """Sanity: with room to fill (new target above the measured level), the
    inflate still goes out."""
    node = _RecordingNode()
    skin = _skin(node)
    ch = skin._chambers[0]
    ch.pressure = 30
    ch.target_pressure = 30        # +10 -> target 40, above the measured 30
    assert skin.inflate(0, 10)
    assert "inflate" in _cmds(node)


def test_repeated_inflate_repushes_every_time():
    """Each step re-asserts the cap — that is what stops a stale max from letting
    repeated + clicks climb past the configured limit."""
    node = _RecordingNode()
    skin = _skin(node)
    skin.inflate(0, 10)
    skin.inflate(0, 10)
    assert _cmds(node) == [
        "set_max_pressure", "set_min_pressure", "inflate",
        "set_max_pressure", "set_min_pressure", "inflate",
    ]
