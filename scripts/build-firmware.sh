#!/usr/bin/env bash
# Build all firmware binaries the GUI/wizard expects to find:
#   firmware/gateway/firmware-s3.bin           (XIAO ESP32-S3, bootloader @ 0x0)
#   firmware/node_actuator/firmware-direct-release.bin
#   firmware/node_actuator/firmware-direct-debug.bin
#   firmware/node_actuator/firmware-direct-rgbw-release.bin   (RGBW LED ring)
#   firmware/node_actuator/firmware-direct-rgbw-debug.bin
#   firmware/node_actuator/firmware-multiplexed-release.bin
#   firmware/node_actuator/firmware-multiplexed-debug.bin
#   firmware/node_actuator/firmware-multiplexed-rgbw-release.bin   (RGBW LED rings)
#   firmware/node_actuator/firmware-multiplexed-rgbw-debug.bin
#   firmware/node_magnet_sensor/firmware-release.bin
#   firmware/thymio_rcp/firmware.bin           (XIAO ESP32-C6 RCP - APP image, WiFi-OTA)
#   firmware/thymio_rcp/firmware-c6.bin        (XIAO ESP32-C6 RCP - MERGED image, first USB flash)
#
# Every board's main .bin is a MERGED image (bootloader + partitions + app) that the setup
# wizard flashes at offset 0x0. The Thymio RCP (C6) ships BOTH: firmware.bin is the bare
# app-only image the WiFi-OTA path needs (it is written into an app partition, never at
# 0x0), and firmware-c6.bin is the merged image for the C6's FIRST flash over USB (the
# wizard writes it at 0x0 like any other board). An app-only bin flashed at 0x0 bricks the
# node with an `invalid header` boot loop.
#
# Mirrors the steps in .github/workflows/build.yml so the local dev bundle
# matches what nightly/stable releases ship.
#
# Usage: build-firmware.sh [gateway|actuator|magnet|thymio]...
# With no arguments every board is built. CI passes only the boards whose
# per-board bin cache missed, so an unchanged board is never recompiled.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

COMPONENTS="${*:-all}"
for c in $COMPONENTS; do
    case "$c" in
        all|gateway|actuator|magnet|thymio) ;;
        *) echo "ERROR: unknown component '$c' (gateway|actuator|magnet|thymio)" >&2; exit 2 ;;
    esac
done
want() { [[ " $COMPONENTS " == *" all "* || " $COMPONENTS " == *" $1 "* ]]; }

if ! command -v pio >/dev/null && ! python -m platformio --version >/dev/null 2>&1; then
    echo "ERROR: PlatformIO not found. Install with: pip install platformio" >&2
    exit 1
fi

if ! python -m esptool version >/dev/null 2>&1; then
    echo "esptool not available - installing into the current Python env..."
    pip install esptool
fi

PIO=(python -m platformio)

# ensure_pioarduino_core <env> - call from inside a project dir right before `pio run`.
# The pioarduino projects (gateway/thymio_rcp) and the stock-espressif32 node projects
# both install a package literally named `framework-arduinoespressif32`, but need
# incompatible arduino-esp32 cores (pioarduino 3.3.9 vs stock 3.0.17). PlatformIO keeps
# only ONE folder by that name in the shared package pool, so a full build (this script's
# order - and CI's - builds the stock nodes before the pioarduino RCP) leaves the stock
# core in place. The stock resolver quietly reinstalls its own core; the pioarduino one
# instead CRASHES with `FRAMEWORK_DIR None`.
#
# The stock core cannot simply be left for pio to fix: both `pio run` and `pio pkg install`
# treat the wrong-version core as "satisfying" the requirement and skip it (the framework
# is `optional: true` in the platform). So we must EVICT it, then FORCE a reinstall with an
# explicit `pkg install` (a bare `pio run` won't re-fetch the evicted optional framework once
# a stock build has touched the pool - verified). Disk-neutral (no duplicate pools; ~76 MB
# re-fetch), version-agnostic (pioarduino core installs from a release URL -> .piopm spec.uri
# set; the stock core from the registry -> spec.uri null), and a no-op for stock node builds.
ensure_pioarduino_core() {
    grep -q 'pioarduino/platform-espressif32' platformio.ini 2>/dev/null || return 0
    local core_dir="${PLATFORMIO_CORE_DIR:-$HOME/.platformio}"
    local piopm="$core_dir/packages/framework-arduinoespressif32/.piopm"
    if [[ -f "$piopm" ]] && grep -q '"uri": *null' "$piopm"; then
        echo "Evicting stock arduino core so the pioarduino build re-fetches its own (framework-arduinoespressif32)"
        rm -rf "$core_dir/packages/framework-arduinoespressif32"
        "${PIO[@]}" pkg install -e "$1"
    fi
}

# merge_node <dir> <env> <out> [chip] [bootloader_offset] [flash_freq]
# Builds <dir>/<env> and merges into <dir>/<out>. Defaults target the ESP32
# nodes (chip esp32, bootloader @ 0x1000, 40m).
merge_node() {
    local dir="$1" env="$2" out="$3"
    local chip="${4:-esp32}" boot_off="${5:-0x1000}" freq="${6:-40m}"
    local out_path="$dir/$out"
    echo
    echo "=== $dir [$env] -> $out ==="

    # Skip when the merged bundle already exists and is newer than every source it
    # can depend on: this env's src/, the shared firmware/common/ headers, the
    # board's platformio.ini and any sdkconfig. A re-run then rebuilds only the
    # envs that actually changed (e.g. edit a node -> the gateway/magnet bundles are
    # left untouched). Pass REBUILD=1 to force a full rebuild.
    local deps=("$dir/src" firmware/common "$dir/platformio.ini")
    local f
    for f in "$dir"/sdkconfig*; do [[ -e "$f" ]] && deps+=("$f"); done
    if [[ "${REBUILD:-0}" != 1 && -f "$out_path" \
          && -z "$(find "${deps[@]}" -newer "$out_path" 2>/dev/null)" ]]; then
        echo "skip (bundle up to date)"
        return
    fi

    (
        cd "$dir"
        ensure_pioarduino_core "$env"
        "${PIO[@]}" run -e "$env"
        python -m esptool --chip "$chip" merge-bin \
            --flash-mode dio --flash-freq "$freq" --flash-size 4MB \
            -o "$out" \
            "$boot_off" ".pio/build/${env}/bootloader.bin" \
            0x8000      ".pio/build/${env}/partitions.bin" \
            0x10000     ".pio/build/${env}/firmware.bin"
    )
}

# build_app <dir> <env> <out>
# Builds <dir>/<env> and copies its bare APP image (NOT merged) to <dir>/<out>. Used
# for the Thymio RCP (C6): it is WiFi-OTA'd into an app partition, so it needs the app
# image at 0x10000, never the merged factory image (that starts with the bootloader).
# Skips (like merge_node) when the output is newer than the sources; REBUILD=1 forces.
build_app() {
    local dir="$1" env="$2" out="$3"
    local out_path="$dir/$out"
    echo
    echo "=== $dir [$env] -> $out (app image) ==="

    local deps=("$dir/src" "$dir/platformio.ini")
    local f
    for f in "$dir"/sdkconfig*; do [[ -e "$f" ]] && deps+=("$f"); done
    if [[ "${REBUILD:-0}" != 1 && -f "$out_path" \
          && -z "$(find "${deps[@]}" -newer "$out_path" 2>/dev/null)" ]]; then
        echo "skip (up to date)"
        return
    fi

    ( cd "$dir"; ensure_pioarduino_core "$env"; "${PIO[@]}" run -e "$env"; cp ".pio/build/${env}/firmware.bin" "$out" )
}

# Gateway - XIAO ESP32-S3 (plain ESP-NOW + a SoftAP build, -DGATEWAY_AP).
if want gateway; then
    merge_node firmware/gateway seeed_xiao_esp32s3 firmware-s3.bin esp32s3 0x0 80m
fi

# Actuator node - direct + multiplexed, each release/debug. Both boards also
# ship RGBW-ring variants (-DLED_RGBW); same source, different NeoPixel pixel
# type (SK6812 RGBW vs WS2812 RGB). See firmware/node_actuator/src/*/leds.h.
if want actuator; then
    merge_node firmware/node_actuator direct                 firmware-direct-release.bin
    merge_node firmware/node_actuator direct_debug           firmware-direct-debug.bin
    merge_node firmware/node_actuator direct_rgbw            firmware-direct-rgbw-release.bin
    merge_node firmware/node_actuator direct_rgbw_debug      firmware-direct-rgbw-debug.bin
    merge_node firmware/node_actuator multiplexed            firmware-multiplexed-release.bin
    merge_node firmware/node_actuator multiplexed_debug      firmware-multiplexed-debug.bin
    merge_node firmware/node_actuator multiplexed_rgbw       firmware-multiplexed-rgbw-release.bin
    merge_node firmware/node_actuator multiplexed_rgbw_debug firmware-multiplexed-rgbw-debug.bin
fi

# Magnet/touch sensor node.
if want magnet; then
    merge_node firmware/node_magnet_sensor release firmware-release.bin
fi

# Thymio RCP (XIAO ESP32-C6) - TWO images from the one `rcp_c6` build:
#   firmware.bin     app-only, for WiFi-OTA (Tools -> Update Nodes -> C6 row / ota_c6_wifi.py)
#   firmware-c6.bin  merged bundle, for the C6's FIRST flash over USB via the setup wizard
# The merge params match the C6 bootloader exactly (esp32c6, boot @ 0x0, 80m/dio/4MB), so
# the wizard can write it at 0x0 like any node bin. A merged image is required here - an
# app-only bin at 0x0 bricks the C6 (`invalid header`).
if want thymio; then
    build_app  firmware/thymio_rcp rcp_c6 firmware.bin
    merge_node firmware/thymio_rcp rcp_c6 firmware-c6.bin esp32c6 0x0 80m
fi

echo
echo "Firmware binaries built:"
if want gateway;  then ls -1 firmware/gateway/firmware-s3.bin; fi
if want actuator; then ls -1 firmware/node_actuator/firmware-*.bin; fi
if want magnet;   then ls -1 firmware/node_magnet_sensor/firmware-release.bin; fi
if want thymio;   then ls -1 firmware/thymio_rcp/firmware.bin firmware/thymio_rcp/firmware-c6.bin; fi
