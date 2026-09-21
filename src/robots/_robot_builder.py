"""Internal helpers for constructing Skin objects from config dicts.

Used by TurtleRobot, TreeRobot, ThymioRobot, and SimulatedRobot so the
config-parsing logic lives in one place.
"""

from __future__ import annotations

import logging
from typing import Any

from src.core.skin_config import DEFAULT_MAX_KPA, DEFAULT_MIN_KPA
from src.hardware.skin import Skin

logger = logging.getLogger(__name__)


def set_pump_counts(
    node_configs: list[dict[str, Any]],
    controllers: dict[str, Any],
) -> None:
    """Tell each controller how many pressure pumps its node shares.

    Drives the shared-pump fill-time scaling (see
    :mod:`src.hardware.fill_scaling`): concurrent inflations on a node split its
    pumps' airflow. ``node_direct`` has two onboard pumps; every other node type
    is treated as a single shared pump.
    """
    for node_cfg in node_configs:
        ctrl = controllers.get(node_cfg.get("mac", ""))
        if ctrl is None or not hasattr(ctrl, "fill_load"):
            continue
        count = 2 if node_cfg.get("node_type") == "node_direct" else 1
        ctrl.fill_load.pump_count = max(1, count)


def multiplexed_configure(node_cfg: dict[str, Any]) -> dict[str, Any] | None:
    """The runtime `configure` payload for a node_multiplexed entry, or None.

    The multiplexed firmware is runtime-sized by gateway config, and two robots
    sharing one board may size it differently, so the payload is derived from a
    node config here and pushed by whoever claims the board (a robot through its
    controller, a bench dialog straight through the gateway).
    """
    if node_cfg.get("node_type") != "node_multiplexed":
        return None
    return {
        "num_chambers": max(1, min(int(node_cfg.get("max_slots", 12)), 16)),
        # Mux channels carrying organ+cover circuits (index = slot in the
        # firmware's organ broadcasts). Convention: highest channels first
        # (I13..I15) so they stay clear of the chamber autodetect. Always a
        # list, even empty: the Turtle and Tree share this board, and an omitted
        # key leaves the other robot's organ channels active on the node.
        "organ_channels": [int(c) for c in node_cfg.get("organ_channels", [])],
    }


def configure_multiplexed_nodes(
    node_configs: list[dict[str, Any]],
    controllers: dict[str, Any],
) -> None:
    """Send runtime `configure` to every node_multiplexed controller."""
    for node_cfg in node_configs:
        payload = multiplexed_configure(node_cfg)
        ctrl = controllers.get(node_cfg.get("mac", ""))
        if payload is None or ctrl is None:
            continue
        ctrl.configure(num_chambers=payload["num_chambers"],
                       organ_channels=payload["organ_channels"])


def push_led_angles(
    skin_configs: list[dict[str, Any]],
    controllers: dict[str, Any],
) -> None:
    """Push each skin's per-ring LED mounting angle to its node.

    The angle is added to every LED command, so an activity's look is rotated to
    match a physically-turned ring without the behaviour knowing about it. It is
    board state, not skin state: when two robots share a node (Turtle and Tree on
    the same PCB) the angles of whichever robot claims the board win, so this is
    re-pushed on every claim rather than once at build time.
    """
    for skin_cfg in skin_configs:
        led_angles = skin_cfg.get("led_angles")
        if not led_angles:
            continue
        mac = next((ch.get("mac") for ch in skin_cfg.get("chambers", [])
                    if ch.get("mac")), None)
        ctrl = controllers.get(mac or "")
        if ctrl is not None and hasattr(ctrl, "set_led_angles"):
            ctrl.set_led_angles(led_angles)


def build_skins(
    skin_configs: list[dict[str, Any]],
    controllers: dict[str, Any],
    touch_controllers: dict[str, Any] | None = None,
    sensorless_macs: set[str] | None = None,
) -> dict[str, Skin]:
    """Construct Skin objects from the config format.

    Each skin's chambers must all reference the same MAC (single-node invariant
    - see Skin docstring). Skins that mix MACs or reference unknown nodes are
    skipped with an error log.

    Args:
        skin_configs:  List of skin dicts::

            [{"skin_id": "belly",
              "chambers": [{"mac": "AA:BB:...", "slot": 0, "max_pressure": 8.0}, ...]},
             ...]

        controllers:   Pre-built ``{mac: controller}`` dict for all nodes of this robot.
        touch_controllers:  Optional ``{skin_id: touch_controller}`` overriding the
            per-skin touch device. Used in simulation to give each skin its own
            ``SimulatedMagnetSensor`` so its T-buttons drive only that skin, even when
            several skins share a touch ``node_mac``. When absent, the touch
            controller is resolved from ``controllers`` by ``touch.node_mac``.
        sensorless_macs:  MACs of nodes whose PCB has NO pressure sensors
            populated (node config ``pressure_sensors: false``). Skins on such
            a node run every actuation open-loop on the manual per-chamber
            times (see Skin ``pressure_sensors``).
    """
    skins: dict[str, Skin] = {}
    for skin_cfg in skin_configs:
        skin = _build_one_skin(skin_cfg, controllers, touch_controllers,
                               sensorless_macs or set())
        if skin is not None:
            skins[skin.skin_id] = skin
    return skins


def _build_one_skin(skin_cfg: dict[str, Any],
                    controllers: dict[str, Any],
                    touch_controllers: dict[str, Any] | None = None,
                    sensorless_macs: set[str] | frozenset[str] = frozenset(),
                    ) -> Skin | None:
    """Build a single Skin from its config dict, or return None if the
    config is invalid (logs the reason)."""
    skin_id = skin_cfg.get("skin_id", "?")
    chambers = skin_cfg.get("chambers", [])
    if not chambers:
        return None

    macs = {ch["mac"] for ch in chambers}
    if len(macs) > 1:
        logger.error(
            "Skin %s spans multiple MACs (%s) - skipping. "
            "A skin must belong to a single node.", skin_id, sorted(macs))
        return None

    mac = next(iter(macs))
    ctrl = controllers.get(mac)
    if ctrl is None:
        logger.error("Skin %s references unknown MAC %s - skipping.",
                     skin_id, mac)
        return None

    chamber_inputs = [
        {"controller":   ctrl,
         "node_slot":    int(ch["slot"]),
         "max_pressure": float(ch.get("max_pressure", DEFAULT_MAX_KPA)),
         "min_pressure": float(ch.get("min_pressure", DEFAULT_MIN_KPA)),
         "fill_time_ms": ch.get("fill_time_ms"),
         "fill_profile": ch.get("fill_profile"),
         "fill_profiles": ch.get("fill_profiles"),
         "deflate_profile": ch.get("deflate_profile"),
         "duty_curve": ch.get("duty_curve"),
         "fill_mode": ch.get("fill_mode"),
         "empty_time_ms": ch.get("empty_time_ms"),
         "hold_duty_curve": ch.get("hold_duty_curve")}
        for ch in chambers
    ]
    touch_ctrl = (touch_controllers or {}).get(skin_id)
    if touch_ctrl is None:
        touch_ctrl = _resolve_touch_ctrl(skin_cfg, controllers)
    return Skin(
        skin_id=skin_id,
        chamber_inputs=chamber_inputs,
        grid=skin_cfg.get("grid"),
        chamber_grid=skin_cfg.get("chamber_grid"),
        touch=_touch_with_saved_threshold(skin_cfg.get("touch"),
                                          skin_cfg.get("skin_type", "")),
        touch_controller=touch_ctrl,
        shape=skin_cfg.get("shape", "rect"),
        organ=skin_cfg.get("organ"),
        organs=skin_cfg.get("organs"),
        skin_type=skin_cfg.get("skin_type", ""),
        skin_variant=skin_cfg.get("skin_variant", ""),
        pressure_sensors=mac not in sensorless_macs,
    )


def _resolve_touch_ctrl(skin_cfg: dict[str, Any],
                        controllers: dict[str, Any]) -> Any:
    """Return the controller for the magnet sensor referenced by ``skin_cfg.touch``."""
    touch_cfg = skin_cfg.get("touch") or {}
    mac = touch_cfg.get("node_mac")
    return controllers.get(mac) if mac else None


def _touch_with_saved_threshold(touch_cfg: dict[str, Any] | None,
                                skin_type: str) -> dict[str, Any] | None:
    """Overlay the skin type's saved sensitivity onto a touch config.

    The uT activation threshold calibrated once per skin type (guided gesture
    capture / Test Actuators -> ``Settings.touch_threshold_ut``) becomes the
    general default: the Skin pushes it to the node at build and the pressure
    compensator rederives ``act`` at the same value. An explicit per-skin
    ``act_threshold_ut`` in the config always wins; without a saved value the
    config passes through untouched."""
    if not touch_cfg:
        return touch_cfg
    from src.config.settings import Settings
    # Do not copy unless there is an overlay to apply.  Apart from avoiding a
    # needless allocation, callers use object identity to distinguish an
    # untouched config from one whose saved defaults were merged in.
    out = touch_cfg

    def apply(key: str, value: Any) -> None:
        nonlocal out
        if out is touch_cfg:
            out = dict(touch_cfg)
        out[key] = value

    settings = Settings()
    key = str(touch_cfg.get("node_mac") or skin_type or "")
    quadrant_thresholds = settings.touch_quadrant_thresholds(key)
    if quadrant_thresholds:
        apply("quadrant_thresholds", quadrant_thresholds)
    spike_threshold = settings.touch_spike_threshold(key)
    if spike_threshold is not None:
        apply("rhythm_spike_ut", spike_threshold)
    frequency_reset_ms = settings.touch_frequency_reset_ms(key)
    if frequency_reset_ms is not None:
        apply("frequency_reset_ms", frequency_reset_ms)
    sync_tolerance_ms = settings.touch_sync_tolerance_ms(key)
    if sync_tolerance_ms is not None:
        apply("rhythm_sync_tolerance_ms", sync_tolerance_ms)
    if not touch_cfg.get("act_threshold_ut") and skin_type:
        saved = settings.touch_threshold_ut(skin_type)
        if saved is not None:
            apply("act_threshold_ut", saved)
    return out
