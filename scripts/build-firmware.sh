#!/usr/bin/env bash
# Build all firmware binaries the GUI/wizard expects to find:
#   firmware/gateway/firmware.bin              (XIAO ESP32-C6, bootloader @ 0x0)
#   firmware/gateway/firmware-esp32.bin        (ESP32-WROOM,   bootloader @ 0x1000)
#   firmware/node_actuator/firmware-direct-release.bin
#   firmware/node_actuator/firmware-direct-debug.bin
#   firmware/node_actuator/firmware-multiplexed-release.bin
#   firmware/node_actuator/firmware-multiplexed-debug.bin
#   firmware/node_magnet_sensor/firmware-release.bin
#
# Every output is a MERGED image (bootloader + partitions + app) because the
# wizard flashes each .bin at offset 0x0. An app-only firmware.bin flashed at
# 0x0 bricks the node with an `invalid header` boot loop.
#
# Mirrors the steps in .github/workflows/build.yml so the local dev bundle
# matches what nightly/stable releases ship.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if ! command -v pio >/dev/null && ! python -m platformio --version >/dev/null 2>&1; then
    echo "ERROR: PlatformIO not found. Install with: pip install platformio" >&2
    exit 1
fi

if ! python -m esptool version >/dev/null 2>&1; then
    echo "esptool not available — installing into the current Python env…"
    pip install esptool
fi

PIO=(python -m platformio)

# merge_node <dir> <env> <out> [chip] [bootloader_offset] [flash_freq]
# Builds <dir>/<env> and merges into <dir>/<out>. Defaults target the ESP32
# nodes (chip esp32, bootloader @ 0x1000, 40m).
merge_node() {
    local dir="$1" env="$2" out="$3"
    local chip="${4:-esp32}" boot_off="${5:-0x1000}" freq="${6:-40m}"
    echo
    echo "=== $dir [$env] -> $out ==="
    (
        cd "$dir"
        "${PIO[@]}" run -e "$env"
        python -m esptool --chip "$chip" merge-bin \
            --flash-mode dio --flash-freq "$freq" --flash-size 4MB \
            -o "$out" \
            "$boot_off" ".pio/build/${env}/bootloader.bin" \
            0x8000      ".pio/build/${env}/partitions.bin" \
            0x10000     ".pio/build/${env}/firmware.bin"
    )
}

# Gateway — two board variants (see firmware/gateway/README.md):
merge_node firmware/gateway seeed_xiao_esp32c6 firmware.bin       esp32c6 0x0    80m
merge_node firmware/gateway esp32dev           firmware-esp32.bin esp32   0x1000 40m

# Actuator node — direct + multiplexed, each release/debug.
merge_node firmware/node_actuator direct            firmware-direct-release.bin
merge_node firmware/node_actuator direct_debug      firmware-direct-debug.bin
merge_node firmware/node_actuator multiplexed       firmware-multiplexed-release.bin
merge_node firmware/node_actuator multiplexed_debug firmware-multiplexed-debug.bin

# Magnet/touch sensor node.
merge_node firmware/node_magnet_sensor release firmware-release.bin

echo
echo "All firmware binaries built:"
ls -1 firmware/gateway/firmware.bin \
       firmware/gateway/firmware-esp32.bin \
       firmware/node_actuator/firmware-*.bin \
       firmware/node_magnet_sensor/firmware-release.bin
