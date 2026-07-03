#!/usr/bin/env python3
"""Update the Thymio radio co-processor (XIAO ESP32-C6) over WiFi — one command.

Fully automatic, no manual JSON, no USB to the C6, no buttons: connects to the
gateway, stages the C6 image on the S3 (buffered in PSRAM), tells the C6 to join the
S3's SoftAP and pull it from /fw, and waits for the C6 to reboot on the new firmware.

Requires the S3 flashed with the gateway build (`seeed_xiao_esp32s3` — SoftAP + Thymio
route are always on) and the C6 already running the WiFi-OTA `rcp_c6` build.
See docs/THYMIO_WIRELESS_CONTROL.md.

    python scripts/ota_c6_wifi.py [--port /dev/ttyACM0] [--ssid SoftEdIBO]
                                  [--pass softedibo] [--image <factory.bin>]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.settings import Settings                       # noqa: E402
from src.hardware.gateway import Gateway          # noqa: E402
from src.hardware.wifi_ota_updater import C6WifiOTAUpdater     # noqa: E402

# The bare APP image (goes at 0x10000), NOT firmware.factory.bin — the merged image
# starts with the bootloader, whose magic byte is also 0xE9, so extract_app_image
# would serve the whole merged blob and the node's esp_image_verify rejects it
# (ESP_ERR_OTA_VALIDATE_FAILED). WiFi-OTA flashes an app partition, so serve the app.
DEFAULT_IMAGE = "firmware/thymio_rcp/firmware.bin"    # bundled app image (build-firmware.sh)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", help="gateway serial port (default: from settings.yaml)")
    p.add_argument("--ssid", default="",
                   help="override the S3 SoftAP SSID (normally omitted — the gateway "
                        "injects its own stored credentials while forwarding)")
    p.add_argument("--pass", dest="password", default="",
                   help="override the S3 SoftAP password (goes with --ssid)")
    p.add_argument("--image", default=DEFAULT_IMAGE, help="C6 firmware .factory.bin")
    args = p.parse_args()

    settings = Settings()
    gw_cfg = settings.data.get("gateway", {})
    port = args.port or gw_cfg.get("serial_port", "/dev/ttyACM0")
    baud = int(gw_cfg.get("baud_rate", 115200))

    image = Path(args.image)
    if not image.is_file():
        print(f"image not found: {image}\n"
              f"  build it first: scripts/build-firmware.sh "
              f"(or pio run -d firmware/thymio_rcp -e rcp_c6, then pass its .pio bin)")
        return 1

    gateway = Gateway(port, baud)
    if not gateway.connect():
        print(f"could not open the gateway on {port} "
              f"(is the app using it? is the S3 the SoftAP+Thymio build?)")
        return 1

    try:
        updater = C6WifiOTAUpdater(
            gateway, image, args.ssid, args.password,
            on_log=lambda s: print(f"  {s}"),
            on_progress=lambda pct: print(f"  staging {pct}%", end="\r", flush=True),
        )
        print(f"Updating the C6 over WiFi via '{args.ssid}' — this takes ~30-60 s…")
        ok, msg = updater.run()
        print()
        print(("✓ " if ok else "✗ ") + msg)
        return 0 if ok else 1
    finally:
        gateway.disconnect()


if __name__ == "__main__":
    sys.exit(main())
