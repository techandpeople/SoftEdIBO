"""Tests for the touch-sensor profile registry (the sensor-technology seam)."""

from src.hardware.touch_profiles import (
    CapacitiveSensorProfile, MagnetSensorProfile, TouchSensorProfile,
    TouchSensorRegistry, touch_profiles)


# ---------------------------------------------------------------------------
# The shipped magnet profile via the process-wide registry
# ---------------------------------------------------------------------------

def test_magnet_profile_registered_as_default():
    assert isinstance(touch_profiles.default, MagnetSensorProfile)
    assert touch_profiles.for_config(None) is touch_profiles.default
    assert touch_profiles.for_config({}) is touch_profiles.default


def test_dispatch_by_wire_strings():
    magnet = touch_profiles.for_message_type("magnet")
    assert isinstance(magnet, MagnetSensorProfile)
    assert touch_profiles.is_message_type("magnet")
    # Unknown / non-touch messages are not touch readings.
    assert touch_profiles.for_message_type("organ") is None
    assert touch_profiles.for_message_type(None) is None
    assert not touch_profiles.is_message_type("status")

    assert touch_profiles.for_ready_status("node_magnet_sensor_ready") is magnet
    assert touch_profiles.for_ready_status("node_direct_ready") is None
    assert touch_profiles.for_ready_status(None) is None


def test_node_types_and_config_selection():
    # Both boards that can carry the magnet bus are touch nodes.
    assert "node_magnet_sensor" in touch_profiles.node_types()
    assert "node_direct" in touch_profiles.node_types()
    assert isinstance(touch_profiles.for_node_type("node_magnet_sensor"),
                      MagnetSensorProfile)
    assert touch_profiles.for_node_type("node_multiplexed") is None

    # A skin naming its sensor explicitly resolves the same profile; an unknown
    # name falls back to the default rather than raising.
    assert touch_profiles.for_config({"sensor": "magnet"}) is touch_profiles.default
    assert touch_profiles.for_config({"sensor": "nope"}) is touch_profiles.default


# ---------------------------------------------------------------------------
# Signal extraction (moved out of Skin, now owned by the profile)
# ---------------------------------------------------------------------------

def test_read_magnitudes_prefers_mag():
    p = MagnetSensorProfile()
    assert p.read_magnitudes({"mag": [205.0, 8.0, 1.0, 2.0]}, 4) == [205.0, 8.0, 1.0, 2.0]
    # Negatives clamp to 0; short/absent mag falls back to act.
    assert p.read_magnitudes({"mag": [-5.0, 8.0]}, 2) == [0.0, 8.0]
    assert p.read_magnitudes({"act": [0, 2]}, 4) == [1.0, 0.0, 1.0, 0.0]
    assert p.read_magnitudes({}, 4) is None
    # A non-finite reading is rejected (falls through to act, here absent).
    assert p.read_magnitudes({"mag": [float("nan"), 1.0]}, 2) is None


# ---------------------------------------------------------------------------
# Detection strategy is per-profile
# ---------------------------------------------------------------------------

def test_position_tracker_only_for_four_sensors():
    p = MagnetSensorProfile()
    assert p.build_position_tracker({"sensor_count": 2}) is None
    assert p.build_position_tracker(None) is None
    built = p.build_position_tracker({"sensor_count": 4})
    assert built is not None
    detector, tracker = built
    assert tracker.detector is detector


def test_compensator_gated_by_config():
    p = MagnetSensorProfile()
    assert p.supports_pressure_coupling is True
    # No coupling matrix / disabled → no compensator.
    assert p.build_compensator(None) is None
    assert p.build_compensator({"compensation": {"enabled": False}}) is None


# ---------------------------------------------------------------------------
# Registry mechanics with a stub technology (proves adding one is a subclass)
# ---------------------------------------------------------------------------

class _StubCapacitive(TouchSensorProfile):
    name = "capacitive"
    node_types = ("node_capacitive_sensor",)
    ready_status = "node_capacitive_sensor_ready"
    message_type = "capacitive"

    def read_magnitudes(self, data, count):
        raw = data.get("cap")
        if not isinstance(raw, (list, tuple)) or len(raw) < count:
            return None
        return [float(v) for v in raw[:count]]


def test_registering_a_new_technology_is_one_subclass():
    reg = TouchSensorRegistry()
    reg.register(MagnetSensorProfile(), default=True)
    reg.register(_StubCapacitive())

    assert reg.for_config({"sensor": "capacitive"}).name == "capacitive"
    by_message = reg.for_message_type("capacitive")
    assert by_message is not None and by_message.name == "capacitive"
    by_ready = reg.for_ready_status("node_capacitive_sensor_ready")
    assert by_ready is not None and by_ready.name == "capacitive"
    by_node_type = reg.for_node_type("node_capacitive_sensor")
    assert by_node_type is not None and by_node_type.name == "capacitive"
    assert "node_capacitive_sensor" in reg.node_types()
    # Default is still magnet; a capacitive skin has no pressure coupling.
    assert reg.for_config(None).name == "magnet"
    assert reg.for_config({"sensor": "capacitive"}).supports_pressure_coupling is False
    assert reg.for_config({"sensor": "capacitive"}).build_compensator({}) is None


# ---------------------------------------------------------------------------
# The shipped capacitive PLACEHOLDER (skeleton, deliberately NOT registered)
# ---------------------------------------------------------------------------

def test_capacitive_placeholder_not_registered_yet():
    # It must not be live until its firmware exists, so a "capacitive" skin
    # still falls back to the default rather than resolving the skeleton.
    assert touch_profiles.for_config({"sensor": "capacitive"}) is touch_profiles.default
    assert touch_profiles.for_message_type("capacitive") is None


def test_capacitive_placeholder_is_well_formed():
    p = CapacitiveSensorProfile()
    assert p.name == "capacitive"
    assert p.supports_pressure_coupling is False
    # Template extractor reads a provisional 'cap' list, clamps negatives.
    assert p.read_magnitudes({"cap": [3.0, -1.0, 5.0]}, 3) == [3.0, 0.0, 5.0]
    assert p.read_magnitudes({"cap": [1.0]}, 3) is None
    assert p.read_magnitudes({}, 2) is None
    # Inherited no-ops: no coupling, no spatial detector.
    assert p.build_compensator({}) is None
    assert p.build_position_tracker({"sensor_count": 4}) is None
