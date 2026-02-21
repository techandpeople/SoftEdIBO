#!/usr/bin/env bash
# SoftEdIBO installer
# Usage: ./install.sh [--uninstall]
set -euo pipefail

INSTALL_DIR="/opt/SoftEdIBO"
BIN_LINK="/usr/local/bin/softedibo"
DESKTOP_FILE="$HOME/.local/share/applications/softedibo.desktop"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Uninstall ──────────────────────────────────────────────────────────────
if [[ "${1:-}" == "--uninstall" ]]; then
    echo "Uninstalling SoftEdIBO..."
    sudo rm -rf "$INSTALL_DIR"
    sudo rm -f  "$BIN_LINK"
    rm -f "$DESKTOP_FILE"
    echo "Done. User data in ~/SoftEdIBO-data (if any) was NOT removed."
    exit 0
fi

# ── Install ────────────────────────────────────────────────────────────────
echo "Installing SoftEdIBO to $INSTALL_DIR ..."

sudo mkdir -p "$INSTALL_DIR"
sudo cp -r "$SCRIPT_DIR/." "$INSTALL_DIR/"
sudo chmod +x "$INSTALL_DIR/SoftEdIBO" "$INSTALL_DIR/esptool"

# Symlink into PATH
sudo ln -sf "$INSTALL_DIR/SoftEdIBO" "$BIN_LINK"
echo "  → symlink: $BIN_LINK"

# Desktop entry (application menu)
mkdir -p "$(dirname "$DESKTOP_FILE")"
cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Name=SoftEdIBO
Comment=Soft-based robot for inclusive education
Exec=$INSTALL_DIR/SoftEdIBO
Terminal=false
Type=Application
Categories=Education;Science;
EOF
echo "  → desktop entry: $DESKTOP_FILE"

# Serial port permissions (needed for USB flash and gateway)
if ! id -nG "$USER" | grep -qw dialout; then
    echo ""
    echo "  → Adding $USER to the 'dialout' group for serial port access..."
    sudo usermod -aG dialout "$USER"
    echo "     Log out and back in (or run 'newgrp dialout') for this to take effect."
fi

echo ""
echo "Installation complete!"
echo "  Run:  softedibo"
echo "   or open it from the application menu."
echo ""
echo "To uninstall: $INSTALL_DIR/install.sh --uninstall"
