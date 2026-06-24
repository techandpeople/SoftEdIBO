#!/usr/bin/env bash
# Vendor Blockly (library + media) for the Behaviour Editor so it works offline.
#
# The editor (src/gui/blockly/editor.html) loads ./blockly.min.js if present,
# otherwise the unpkg CDN; likewise it uses ./media/ when present (the trashcan/
# zoom/dropdown icons + click sounds), else the CDN media folder. Run this once
# (with internet) to drop local copies; after that the editor needs no network.
#
# Usage: bash scripts/fetch_blockly.sh [version]

set -e

VERSION="${1:-latest}"
DIR="src/gui/blockly"
BASE="https://unpkg.com/blockly@${VERSION}"

fetch() {  # fetch <url> <dest>  (best-effort for media; required for the lib)
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$1" -o "$2"
    elif command -v wget >/dev/null 2>&1; then
        wget -qO "$2" "$1"
    else
        echo "error: need curl or wget" >&2; exit 1
    fi
}

echo "Fetching Blockly (${VERSION}) library -> ${DIR}/blockly.min.js"
fetch "${BASE}/blockly.min.js" "${DIR}/blockly.min.js"

echo "Fetching Blockly media -> ${DIR}/media/"
mkdir -p "${DIR}/media"
# Blockly 13 media set (icons are sprites.svg now, plus the sound effects and
# drag cursors). Best-effort: names vary across versions, skips any 404.
MEDIA="sprites.svg delete-icon.svg foldout-icon.svg dropdown-arrow.svg
       quote0.png quote1.png
       handclosed.cur handopen.cur handdelete.cur
       click.mp3 delete.mp3 disconnect.mp3 drop.mp3"
for f in $MEDIA; do
    fetch "${BASE}/media/${f}" "${DIR}/media/${f}" 2>/dev/null \
        && echo "  ok  ${f}" || echo "  skip ${f} (not in this version)"
done

echo "Done. The Behaviour Editor will now use the vendored copy offline."
