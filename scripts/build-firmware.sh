#!/usr/bin/env bash
# Build all firmware binaries the GUI expects to find:
#   firmware/gateway/firmware.bin
#   firmware/node_direct/firmware-release.bin
#   firmware/node_direct/firmware-debug.bin
#   firmware/node_multiplexed/firmware-release.bin
#   firmware/node_multiplexed/firmware-debug.bin
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
ESPTOOL=(python -m esptool --chip esp32 merge_bin
         --flash_mode dio --flash_freq 40m --flash_size 4MB)

build_merged() {
    local dir="$1" env="$2" out="$3"
    echo
    echo "=== $dir [$env] -> $out ==="
    (
        cd "$dir"
        "${PIO[@]}" run -e "$env"
        "${ESPTOOL[@]}" -o "$out" \
            0x1000  ".pio/build/${env}/bootloader.bin" \
            0x8000  ".pio/build/${env}/partitions.bin" \
            0x10000 ".pio/build/${env}/firmware.bin"
    )
}

build_merged firmware/gateway          esp32dev firmware.bin
build_merged firmware/node_direct      release  firmware-release.bin
build_merged firmware/node_direct      debug    firmware-debug.bin
build_merged firmware/node_multiplexed release  firmware-release.bin
build_merged firmware/node_multiplexed debug    firmware-debug.bin

echo
echo "All firmware binaries built:"
ls -1 firmware/gateway/firmware.bin \
       firmware/node_direct/firmware-*.bin \
       firmware/node_multiplexed/firmware-*.bin
