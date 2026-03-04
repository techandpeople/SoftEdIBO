#!/usr/bin/env bash
# Regenerate Python files from Qt Designer .ui files.
# Run from the project root: bash scripts/compile_ui.sh

set -e

VENV=".venv/bin/pyside6-uic"
UI_DIR="src/gui/ui"
OUT_DIR="src/gui"

for ui_file in "$UI_DIR"/*.ui; do
    base=$(basename "$ui_file" .ui)
    out="$OUT_DIR/ui_${base}.py"
    echo "  $ui_file => $out"
    "$VENV" "$ui_file" -o "$out"
done

echo "Done."
