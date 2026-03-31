#!/usr/bin/env bash
# SoftEdIBO installer
#
# Quick install (latest stable):
#   curl -fsSL https://raw.githubusercontent.com/techandpeople/SoftEdIBO/master/install.sh | bash
#
# Nightly build:
#   curl -fsSL https://raw.githubusercontent.com/techandpeople/SoftEdIBO/master/install.sh | bash -s -- --nightly
#
# Local AppImage:
#   ./install.sh SoftEdIBO-x86_64.AppImage
#
# Uninstall:
#   ./install.sh --uninstall
#
# System-wide install (legacy behavior, requires sudo):
#   ./install.sh --system
set -euo pipefail

REPO="techandpeople/SoftEdIBO"
SYSTEM_INSTALL=false
INSTALL_DIR="$HOME/.local/opt/SoftEdIBO"
APPIMAGE_DEST="$INSTALL_DIR/SoftEdIBO.AppImage"
BIN_LINK="$HOME/.local/bin/softedibo"
DESKTOP_FILE="$HOME/.local/share/applications/softedibo.desktop"
ICON_FILE="$HOME/.local/share/icons/hicolor/256x256/apps/softedibo.png"

ARCH="$(uname -m)"   # x86_64 | aarch64 | ...
NIGHTLY=false
LOCAL_FILE=""

# ── Parse arguments ────────────────────────────────────────────────────────
for arg in "$@"; do
    case "$arg" in
        --uninstall)
            echo "Uninstalling SoftEdIBO..."
            rm -rf "$HOME/.local/opt/SoftEdIBO"
            rm -f "$HOME/.local/bin/softedibo"
            sudo rm -rf /opt/SoftEdIBO 2>/dev/null || true
            sudo rm -f /usr/local/bin/softedibo 2>/dev/null || true
            rm -f "$DESKTOP_FILE" "$ICON_FILE"
            echo "Done. User data in ~/.local/share/SoftEdIBO (if any) was NOT removed."
            exit 0
            ;;
        --nightly) NIGHTLY=true ;;
        --system) SYSTEM_INSTALL=true ;;
        --help|-h)
            cat << 'EOF'
Usage:
  ./install.sh [OPTIONS] [APPIMAGE]

Options:
  --nightly     Install the latest nightly build instead of the stable release
    --system      Install system-wide to /opt + /usr/local/bin (requires sudo)
  --uninstall   Remove SoftEdIBO from the system
  -h, --help    Show this help message

Arguments:
  APPIMAGE      Path to a local .AppImage file (skips download)

Examples:
  # Install latest stable release (downloads automatically):
  curl -fsSL https://raw.githubusercontent.com/techandpeople/SoftEdIBO/master/install.sh | bash

  # Install latest nightly build:
  curl -fsSL https://raw.githubusercontent.com/techandpeople/SoftEdIBO/master/install.sh | bash -s -- --nightly

  # Install from a local AppImage:
  ./install.sh SoftEdIBO-x86_64.AppImage

    # Install system-wide (legacy behavior):
    ./install.sh --system

  # Uninstall:
  ./install.sh --uninstall

Installs to:
    ~/.local/opt/SoftEdIBO/SoftEdIBO.AppImage      AppImage (default)
    ~/.local/bin/softedibo                         Launcher (default, no sudo)
  ~/.local/share/applications/         Desktop entry
  ~/.local/share/icons/                App icon
EOF
            exit 0
            ;;
        --*) echo "Unknown option: $arg. Try --help." >&2; exit 1 ;;
        *)   LOCAL_FILE="$arg" ;;
    esac
done

if $SYSTEM_INSTALL; then
    INSTALL_DIR="/opt/SoftEdIBO"
    BIN_LINK="/usr/local/bin/softedibo"
    APPIMAGE_DEST="$INSTALL_DIR/SoftEdIBO.AppImage"
fi

# ── Locate or download AppImage ────────────────────────────────────────────
if [[ -n "$LOCAL_FILE" ]]; then
    [[ -f "$LOCAL_FILE" ]] || { echo "File not found: $LOCAL_FILE" >&2; exit 1; }
    SRC="$LOCAL_FILE"
else
    if $NIGHTLY; then
        URL="https://github.com/$REPO/releases/download/nightly/SoftEdIBO-${ARCH}.AppImage"
        echo "Downloading nightly build..."
    else
        URL="https://github.com/$REPO/releases/latest/download/SoftEdIBO-${ARCH}.AppImage"
        echo "Downloading latest stable release..."
    fi

    TMP_APPIMAGE="$(mktemp /tmp/SoftEdIBO-XXXXXX.AppImage)"
    trap 'rm -f "$TMP_APPIMAGE"' EXIT

    if command -v curl &>/dev/null; then
        curl -fsSL --progress-bar -o "$TMP_APPIMAGE" "$URL"
    elif command -v wget &>/dev/null; then
        wget -q --show-progress -O "$TMP_APPIMAGE" "$URL"
    else
        echo "Error: curl or wget is required." >&2
        exit 1
    fi
    SRC="$TMP_APPIMAGE"
fi

echo "Installing from: $SRC"

# ── Copy AppImage ──────────────────────────────────────────────────────────
if $SYSTEM_INSTALL; then
    sudo mkdir -p "$INSTALL_DIR"
    sudo cp "$SRC" "$APPIMAGE_DEST"
    sudo chmod 755 "$APPIMAGE_DEST"
    sudo chown "$USER" "$APPIMAGE_DEST"
else
    mkdir -p "$INSTALL_DIR"
    cp "$SRC" "$APPIMAGE_DEST"
    chmod 755 "$APPIMAGE_DEST"
fi
echo "  => $APPIMAGE_DEST"

# ── Wrapper script (no FUSE needed) ───────────────────────────────────────
mkdir -p "$(dirname "$BIN_LINK")"
if $SYSTEM_INSTALL; then
    sudo tee "$BIN_LINK" > /dev/null << EOF
#!/bin/bash
if [[ "\${1:-}" == "--uninstall" ]]; then
    echo "Uninstalling SoftEdIBO..."
    sudo rm -rf  "$INSTALL_DIR"
    sudo rm -f   "$BIN_LINK"
    rm -f "$DESKTOP_FILE" "$ICON_FILE"
    echo "Done. User data in ~/.local/share/SoftEdIBO (if any) was NOT removed."
    exit 0
fi
export APPIMAGE_EXTRACT_AND_RUN=1
export APPIMAGE="$APPIMAGE_DEST"
exec "$APPIMAGE_DEST" "\$@"
EOF
    sudo chmod +x "$BIN_LINK"
else
    cat > "$BIN_LINK" << EOF
#!/bin/bash
if [[ "\${1:-}" == "--uninstall" ]]; then
    echo "Uninstalling SoftEdIBO..."
    rm -rf "$INSTALL_DIR"
    rm -f "$BIN_LINK"
    rm -f "$DESKTOP_FILE" "$ICON_FILE"
    echo "Done. User data in ~/.local/share/SoftEdIBO (if any) was NOT removed."
    exit 0
fi
export APPIMAGE_EXTRACT_AND_RUN=1
export APPIMAGE="$APPIMAGE_DEST"
exec "$APPIMAGE_DEST" "\$@"
EOF
    chmod +x "$BIN_LINK"
fi
echo "  => launcher: $BIN_LINK"

# ── Download icon ──────────────────────────────────────────────────────────
trap 'rm -f "${TMP_APPIMAGE:-}"' EXIT
ICON_URL="https://raw.githubusercontent.com/$REPO/master/softedibo.png"
mkdir -p "$(dirname "$ICON_FILE")"
if command -v curl &>/dev/null; then
    curl -fsSL -o "$ICON_FILE" "$ICON_URL" 2>/dev/null && ICON="softedibo" || ICON="application-x-executable"
elif command -v wget &>/dev/null; then
    wget -q -O "$ICON_FILE" "$ICON_URL" 2>/dev/null && ICON="softedibo" || ICON="application-x-executable"
else
    ICON="application-x-executable"
fi
[[ "$ICON" == "softedibo" ]] && echo "  => icon: $ICON_FILE"

# ── Desktop entry ──────────────────────────────────────────────────────────
mkdir -p "$(dirname "$DESKTOP_FILE")"
cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Name=SoftEdIBO
Comment=Soft-based robot for inclusive education
Exec=$BIN_LINK
Icon=$ICON
Terminal=false
Type=Application
Categories=Education;Science;
EOF
echo "  => desktop entry: $DESKTOP_FILE"
command -v update-desktop-database &>/dev/null && \
    update-desktop-database "$(dirname "$DESKTOP_FILE")" 2>/dev/null || true

# ── Serial port permissions ────────────────────────────────────────────────
if ! id -nG "$USER" | grep -qw dialout; then
    echo ""
    echo "  => Adding $USER to 'dialout' for serial port access..."
    sudo usermod -aG dialout "$USER"
    echo "     Log out and back in (or run 'newgrp dialout') for this to take effect."
fi

echo ""
echo "Installation complete!"
echo "  Run:  softedibo"
if ! command -v softedibo >/dev/null 2>&1; then
    echo "  Note: add ~/.local/bin to PATH if command is not found."
fi
echo "   or open it from the application menu."
echo ""
echo "To uninstall: softedibo --uninstall"
