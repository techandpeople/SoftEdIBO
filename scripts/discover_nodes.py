"""Interactive node discovery and mapping tool.

Connects to the ESP-NOW gateway, discovers all ESP32 nodes, and lets
the user identify each one by inflating its chambers one at a time.
The user watches which skin inflates and types a name for it.
The result is saved to config/settings.yaml.
"""

import json
import sys
import time
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

import yaml

from src.hardware.espnow_gateway import ESPNowGateway

CONFIG_PATH = project_root / "config" / "settings.yaml"


def load_config() -> dict:
    """Load the current settings.yaml."""
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def save_config(config: dict) -> None:
    """Save updated config to settings.yaml."""
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


def discover_nodes(gateway: ESPNowGateway, timeout: float = 5.0) -> list[str]:
    """Ask the gateway to scan for ESP-NOW nodes and return their MACs.

    The gateway firmware should respond to a 'scan' command with
    a list of discovered peer MAC addresses.
    """
    discovered: list[str] = []

    def on_msg(data: dict):
        if data.get("type") == "scan_result":
            mac = data.get("mac")
            if mac and mac not in discovered:
                discovered.append(mac)

    gateway.on_message(on_msg)
    gateway.send("FF:FF:FF:FF:FF:FF", "scan")

    print(f"Scanning for nodes ({timeout}s)...")
    time.sleep(timeout)
    return discovered


def inflate_node(gateway: ESPNowGateway, mac: str, chamber: int) -> None:
    """Inflate a specific chamber on a node."""
    gateway.send(mac, "inflate", chamber=chamber, value=255)


def deflate_node(gateway: ESPNowGateway, mac: str, chamber: int) -> None:
    """Deflate a specific chamber on a node."""
    gateway.send(mac, "deflate", chamber=chamber)


def deflate_all_on_node(gateway: ESPNowGateway, mac: str) -> None:
    """Deflate all 3 chambers on a node."""
    for ch in range(3):
        gateway.send(mac, "deflate", chamber=ch)


def identify_node(gateway: ESPNowGateway, mac: str) -> list[dict]:
    """Interactively identify the skins on a single ESP32 node.

    Inflates each chamber slot one by one. The user sees which
    physical skin inflates and names it.

    Returns:
        List of skin dicts with 'skin_id' and 'slots'.
    """
    print(f"\n--- Node: {mac} ---")
    print("This node has 3 chamber slots (0, 1, 2).")
    print("I will inflate each slot one at a time.")
    print("Watch which skin inflates and type a name for it.\n")

    slot_names: dict[int, str] = {}

    for slot in range(3):
        print(f"  Inflating slot {slot}...")
        inflate_node(gateway, mac, slot)
        time.sleep(1.5)

        name = input(f"  Which skin inflated? (name, or ENTER to skip if empty slot): ").strip()
        deflate_node(gateway, mac, slot)
        time.sleep(0.5)

        if name:
            slot_names[slot] = name

    # Group slots by skin name (same name = same skin with multiple chambers)
    skins_by_name: dict[str, list[int]] = {}
    for slot, name in slot_names.items():
        skins_by_name.setdefault(name, []).append(slot)

    skins = [
        {"skin_id": name, "slots": sorted(slots)}
        for name, slots in skins_by_name.items()
    ]

    if not skins:
        print("  No skins identified on this node.")
    else:
        for s in skins:
            print(f"  -> {s['skin_id']}: slots {s['slots']}")

    return skins


def assign_to_robot(skin_id: str) -> str:
    """Ask the user which robot this skin belongs to."""
    robot = input(f"  Robot for '{skin_id}' (turtle/thymio/tree): ").strip().lower()
    if robot not in ("turtle", "thymio", "tree"):
        print(f"  Unknown robot '{robot}', defaulting to 'turtle'")
        return "turtle"
    return robot


def main():
    config = load_config()
    port = config["gateway"]["serial_port"]
    baud = config["gateway"]["baud_rate"]

    print("=== SoftEdIBO Node Discovery ===\n")
    print(f"Connecting to gateway on {port}...")

    gateway = ESPNowGateway(port, baud)
    if not gateway.connect():
        print("ERROR: Could not connect to gateway. Check the serial port.")
        sys.exit(1)

    print("Connected!\n")

    # Step 1: Discover nodes
    nodes = discover_nodes(gateway)
    if not nodes:
        print("No nodes found. Make sure all ESP32s are powered on.")
        gateway.disconnect()
        sys.exit(1)

    print(f"\nFound {len(nodes)} node(s): {nodes}\n")

    # Step 2: Identify each node interactively
    robot_nodes: dict[str, list[dict]] = {
        "turtle": [],
        "thymios": [],
        "tree": [],
    }

    for mac in nodes:
        skins = identify_node(gateway, mac)
        if not skins:
            continue

        # Ask which robot each skin belongs to
        robot = assign_to_robot(skins[0]["skin_id"])

        node_entry = {"mac": mac, "skins": skins}

        if robot == "turtle":
            robot_nodes["turtle"].append(node_entry)
        elif robot == "thymio":
            thymio_id = input(f"  Thymio ID for this node: ").strip()
            robot_nodes["thymios"].append({
                "thymio_id": thymio_id,
                "node_mac": mac,
                "skins": skins,
            })
        elif robot == "tree":
            robot_nodes["tree"].append(node_entry)

        # Deflate everything before moving on
        deflate_all_on_node(gateway, mac)
        time.sleep(0.5)

    # Step 3: Save to config
    if robot_nodes["turtle"]:
        config["robots"]["turtle"] = {"nodes": robot_nodes["turtle"]}
    if robot_nodes["thymios"]:
        config["robots"]["thymios"] = robot_nodes["thymios"]
    if robot_nodes["tree"]:
        config["robots"]["tree"] = {"nodes": robot_nodes["tree"]}

    save_config(config)
    print(f"\nConfig saved to {CONFIG_PATH}")
    print("Done!")

    gateway.disconnect()


if __name__ == "__main__":
    main()
