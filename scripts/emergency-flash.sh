#!/usr/bin/env bash
# Emergency cable-flash of a node whose own USB is dead, using a SECOND ESP32
# (a classic WROOM DevKit with a CP2102/CH340) held in reset as a transparent
# USB-to-serial bridge. After this the node has OTA-capable firmware again and
# can be updated wirelessly (app: Tools -> Update Nodes (OTA)).
#
# The GUI exposes the same thing under Tools -> Emergency Flash (dead USB)…;
# this script is the headless dev fallback. It flashes the LOCAL firmware/*.bin
# (build them with scripts/build-firmware.sh). See docs/EMERGENCY_FLASH.md for
# the wiring (TX->TX, RX->RX straight-through) and the manual download-mode step.
#
# Usage:
#   scripts/emergency-flash.sh -t direct                 # node_direct, release
#   scripts/emergency-flash.sh -t multiplexed -d         # multiplexed, debug
#   scripts/emergency-flash.sh -t magnet -p /dev/ttyUSB0 -b 115200
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

TYPE="direct"
DEBUG=0
PORT=""
BAUD="115200"   # hand-wired bridges are noisy; raise only if reliable

usage() { sed -n '2,18p' "$0"; exit "${1:-0}"; }

while getopts "t:dp:b:h" opt; do
    case "$opt" in
        t) TYPE="$OPTARG" ;;
        d) DEBUG=1 ;;
        p) PORT="$OPTARG" ;;
        b) BAUD="$OPTARG" ;;
        h) usage 0 ;;
        *) usage 1 ;;
    esac
done

# Resolve the firmware bin for the chosen node type / variant.
case "$TYPE" in
    direct)      BASE="$REPO_ROOT/firmware/node_actuator/firmware-direct" ;;
    multiplexed) BASE="$REPO_ROOT/firmware/node_actuator/firmware-multiplexed" ;;
    magnet)      BASE="$REPO_ROOT/firmware/node_magnet_sensor/firmware" ;;
    *) echo "error: unknown -t '$TYPE' (use: direct | multiplexed | magnet)" >&2; exit 1 ;;
esac

if [ "$TYPE" = "magnet" ]; then
    BIN="${BASE}-release.bin"          # single build, no debug variant
elif [ "$DEBUG" = 1 ]; then
    BIN="${BASE}-debug.bin"
else
    BIN="${BASE}-release.bin"
fi

if [ ! -f "$BIN" ]; then
    echo "error: firmware not found: $BIN" >&2
    echo "       build it first: scripts/build-firmware.sh" >&2
    exit 1
fi

# Resolve esptool (avoid a pyenv shim that fails outside its env).
ESPTOOL=""
for c in \
    "$HOME/.pyenv/versions/SoftEdIBO/bin/esptool" \
    "$HOME/.pyenv/versions/3.12.11/envs/SoftEdIBO/bin/esptool" ; do
    [ -x "$c" ] && ESPTOOL="$c" && break
done
if [ -z "$ESPTOOL" ] && command -v esptool >/dev/null 2>&1; then
    ESPTOOL="$(command -v esptool)"
fi
ESPTOOL_CMD=("${ESPTOOL:-}")
[ -z "$ESPTOOL" ] && ESPTOOL_CMD=(python -m esptool)

# Auto-pick the bridge port if none given.
if [ -z "$PORT" ]; then
    PORT="$(ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null | head -n1 || true)"
fi
if [ -z "$PORT" ]; then
    echo "error: no serial port found; pass one with -p /dev/ttyUSB0" >&2
    exit 1
fi

echo ">>> Node:     $TYPE ($([ "$DEBUG" = 1 ] && echo debug || echo release))"
echo ">>> Firmware: $BIN"
echo ">>> Bridge:   $PORT @ $BAUD"
echo ">>> Put the TARGET in download mode now: hold BOOT, tap EN/RST, release BOOT."
echo

# --before/--after no_reset: the bridge can't drive the target's EN/IO0, so we
# enter download mode by hand above. Merged image -> written at 0x0.
"${ESPTOOL_CMD[@]}" --chip esp32 --port "$PORT" --baud "$BAUD" \
    --before no_reset --after no_reset \
    write_flash 0x0 "$BIN"

echo
echo ">>> Done. Remove the IO0/BOOT jumper, tap EN/RST. The node now runs"
echo ">>> OTA-capable firmware — update it wirelessly from the app from here on."
