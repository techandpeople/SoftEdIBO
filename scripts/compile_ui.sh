#!/usr/bin/env bash
# Regenerate Python files from Qt Designer .ui files.
# Run from the project root: bash scripts/compile_ui.sh

set -e

# Prefer a local .venv, else fall back to whatever pyside6-uic is on PATH
# (e.g. a pyenv virtualenv). Override with UIC=... if needed.
if [ -n "${UIC:-}" ]; then
    :
elif [ -x ".venv/bin/pyside6-uic" ]; then
    UIC=".venv/bin/pyside6-uic"
elif command -v pyside6-uic >/dev/null 2>&1; then
    UIC="pyside6-uic"
else
    echo "error: pyside6-uic not found (set UIC=/path/to/pyside6-uic)" >&2
    exit 1
fi

UI_DIR="src/gui/ui"
OUT_DIR="src/gui"

for ui_file in "$UI_DIR"/*.ui; do
    base=$(basename "$ui_file" .ui)
    out="$OUT_DIR/ui_${base}.py"
    echo "  $ui_file => $out"
    "$UIC" "$ui_file" -o "$out"
done

echo "Done."
