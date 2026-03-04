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
set -euo pipefail

REPO="techandpeople/SoftEdIBO"
INSTALL_DIR="/opt/SoftEdIBO"
APPIMAGE_DEST="$INSTALL_DIR/SoftEdIBO.AppImage"
BIN_LINK="/usr/local/bin/softedibo"
DESKTOP_FILE="$HOME/.local/share/applications/softedibo.desktop"
ICON_FILE="$HOME/.local/share/icons/hicolor/256x256/apps/softedibo.png"

ARCH="$(uname -m)"   # x86_64 | aarch64 | ...
NIGHTLY=false
LOCAL_FILE=""

# ── Parse arguments ────────────────────────────────────────────────────────
for arg in "${@:-}"; do
    case "$arg" in
        --uninstall)
            echo "Uninstalling SoftEdIBO..."
            sudo rm -rf  "$INSTALL_DIR"
            sudo rm -f   "$BIN_LINK"
            rm -f "$DESKTOP_FILE" "$ICON_FILE"
            echo "Done. User data in ~/.local/share/SoftEdIBO (if any) was NOT removed."
            exit 0
            ;;
        --nightly) NIGHTLY=true ;;
        --*) echo "Unknown option: $arg" >&2; exit 1 ;;
        *)   LOCAL_FILE="$arg" ;;
    esac
done

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
sudo mkdir -p "$INSTALL_DIR"
sudo cp "$SRC" "$APPIMAGE_DEST"
sudo chmod +x "$APPIMAGE_DEST"
echo "  → $APPIMAGE_DEST"

# ── Symlink into PATH ──────────────────────────────────────────────────────
sudo ln -sf "$APPIMAGE_DEST" "$BIN_LINK"
echo "  → symlink: $BIN_LINK"

# ── Extract icon from AppImage ─────────────────────────────────────────────
TMP_ICON="$(mktemp -d)"
trap 'rm -rf "$TMP_ICON"; rm -f "${TMP_APPIMAGE:-}"' EXIT
pushd "$TMP_ICON" >/dev/null
"$APPIMAGE_DEST" --appimage-extract softedibo.png >/dev/null 2>&1 || true
popd >/dev/null

if [[ -f "$TMP_ICON/squashfs-root/softedibo.png" ]]; then
    mkdir -p "$(dirname "$ICON_FILE")"
    cp "$TMP_ICON/squashfs-root/softedibo.png" "$ICON_FILE"
    ICON="softedibo"
    echo "  → icon: $ICON_FILE"
else
    ICON="application-x-executable"
fi

# ── Desktop entry ──────────────────────────────────────────────────────────
mkdir -p "$(dirname "$DESKTOP_FILE")"
cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Name=SoftEdIBO
Comment=Soft-based robot for inclusive education
Exec=$APPIMAGE_DEST
Icon=$ICON
Terminal=false
Type=Application
Categories=Education;Science;
EOF
echo "  → desktop entry: $DESKTOP_FILE"
command -v update-desktop-database &>/dev/null && \
    update-desktop-database "$(dirname "$DESKTOP_FILE")" 2>/dev/null || true

# ── Serial port permissions ────────────────────────────────────────────────
if ! id -nG "$USER" | grep -qw dialout; then
    echo ""
    echo "  → Adding $USER to 'dialout' for serial port access..."
    sudo usermod -aG dialout "$USER"
    echo "     Log out and back in (or run 'newgrp dialout') for this to take effect."
fi

echo ""
echo "Installation complete!"
echo "  Run:  softedibo"
echo "   or open it from the application menu."
echo ""
echo "To uninstall: $0 --uninstall"
