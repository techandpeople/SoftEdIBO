#!/usr/bin/env python3
"""Flash every online node over the air - one command (dev mirror of the app's
Tools -> Update Nodes (OTA)... dialog).

Connects to the gateway, scans, and updates each configured node that answered
with the bundled firmware for its type (firmware/*/*.bin - rebuild them with
scripts/build-firmware.sh). Nodes are flashed one at a time. The app must not be
holding the gateway's serial port.

    python scripts/ota_nodes.py [--port /dev/ttyACM0] [--transport wifi|espnow]
                                [--led auto|rgb|rgbw] [--type node_direct]
                                [--debug] [--dry-run]

--led picks the LED pixel format of the actuator builds: "auto" follows what
each node's running firmware reports (rgb when it reports nothing); "rgb" /
"rgbw" force it, which is how a node flashed with the wrong variant is repaired.

A node does not announce which board it is, so an online node that no robot in
settings.yaml lists is skipped - unless --type names the board type to assume
for those (flashing a board with another board's firmware breaks it, so be sure).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.settings import Settings                       # noqa: E402
from src.core import node_sharing                              # noqa: E402
from src.core.led_variant import resolve_rgbw                  # noqa: E402
from src.gui.setup_wizard import (                             # noqa: E402
    NODE_TYPE_FIRMWARES, firmware_for_node_type)
from src.hardware.gateway import Gateway                       # noqa: E402
from src.hardware.node_ota_updater import NodeOTAUpdater       # noqa: E402
from src.hardware.wifi_ota_updater import WifiOTAUpdater       # noqa: E402

SCAN_WAIT_S = 2.5      # reply window of a scan (see Gateway.is_online)
LED_CHOICES = {"auto": None, "rgb": False, "rgbw": True}


def plan_jobs(gateway: Gateway, node_types: dict[str, str], forced_rgbw: bool | None,
              debug: bool, unconfigured_type: str = "",
              ) -> tuple[list[tuple[str, Path]], list[str]]:
    """Return ``(jobs, notes)``: one ``(mac, firmware)`` per online node that
    can be flashed, plus a line for every node that is skipped.

    ``unconfigured_type`` is the node type to assume for online nodes missing
    from ``node_types`` (empty = skip them)."""
    jobs: list[tuple[str, Path]] = []
    notes: list[str] = []
    online = gateway.online_macs
    node_types = dict(node_types)
    for mac in sorted(online - node_types.keys()):
        if unconfigured_type:
            node_types[mac] = unconfigured_type
        else:
            notes.append(f"{mac}: online but not configured on any robot - skipped "
                         "(pass --type / TYPE=<node type> to flash it)")
    for mac, ntype in node_types.items():
        if mac not in online:
            notes.append(f"{mac} ({ntype}): offline - skipped")
            continue
        rgbw = resolve_rgbw(forced_rgbw, gateway.node_rgbw(mac), False)
        fw = firmware_for_node_type(ntype, debug, rgbw)
        if fw is None or not fw.exists():
            notes.append(f"{mac} ({ntype}): firmware not found ({fw}) - skipped")
            continue
        jobs.append((mac, fw))
    return jobs, notes


def flash(gateway: Gateway, mac: str, firmware: Path, wifi: bool) -> bool:
    """Update one node; returns True on success."""
    kwargs = {
        "on_log": lambda s: print(f"    {s}"),
        "on_progress": lambda pct: print(f"    {pct}%", end="\r", flush=True),
    }
    updater = (WifiOTAUpdater(gateway, mac, firmware, **kwargs) if wifi
               else NodeOTAUpdater(gateway, mac, firmware, **kwargs))
    ok, msg = updater.run()
    print(("    ok " if ok else "    FAIL ") + msg)
    return ok


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", help="gateway serial port (default: from settings.yaml)")
    p.add_argument("--transport", choices=("wifi", "espnow"), default="wifi",
                   help="wifi = fast, needs the gateway access point; espnow = slow, "
                        "works anywhere (default: wifi)")
    p.add_argument("--led", choices=tuple(LED_CHOICES), default="auto",
                   help="LED pixel format of the actuator builds (default: auto)")
    p.add_argument("--type", dest="unconfigured_type", default="",
                   choices=("", *NODE_TYPE_FIRMWARES),
                   help="board type to assume for online nodes that no robot lists "
                        "(default: skip them)")
    p.add_argument("--debug", action="store_true", help="flash the debug builds")
    p.add_argument("--dry-run", action="store_true",
                   help="scan and list what would be flashed, then stop")
    args = p.parse_args()

    settings = Settings()
    gw_cfg = settings.data.get("gateway", {})
    port = args.port or gw_cfg.get("serial_port", "/dev/ttyACM0")
    gateway = Gateway(port, int(gw_cfg.get("baud_rate", 115200)))
    if not gateway.connect():
        print(f"could not open the gateway on {port} (is the app using it?)")
        return 1

    try:
        gateway.scan()
        time.sleep(SCAN_WAIT_S)
        jobs, notes = plan_jobs(gateway, node_sharing.node_types_by_mac(settings.data),
                                LED_CHOICES[args.led], args.debug,
                                args.unconfigured_type)
        for note in notes:
            print(f"  {note}")
        if not jobs:
            print("no online node to flash")
            return 1
        for mac, fw in jobs:
            print(f"  {mac} <= {fw.name}")
        if args.dry_run:
            return 0

        failed = 0
        for mac, fw in jobs:
            print(f"{mac}: flashing {fw.name} ({args.transport})")
            if not flash(gateway, mac, fw, args.transport == "wifi"):
                failed += 1
        print(f"{len(jobs) - failed}/{len(jobs)} node(s) updated")
        return 1 if failed else 0
    finally:
        gateway.disconnect()


if __name__ == "__main__":
    sys.exit(main())
