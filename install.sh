#!/usr/bin/env bash
# Installs bglin for the current user (symlink launcher, .desktop, systemd unit).
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$HOME/.local/bin" "$HOME/.local/share/applications" "$HOME/.config/systemd/user"

ln -sf "$REPO/bin/bglin" "$HOME/.local/bin/bglin"
cp "$REPO/bglin.desktop" "$HOME/.local/share/applications/bglin.desktop"
cp "$REPO/systemd/bglin.service" "$HOME/.config/systemd/user/bglin.service"
systemctl --user daemon-reload

echo "Installed."
echo "  Launcher : ~/.local/bin/bglin (make sure ~/.local/bin is in PATH)"
echo "  GUI      : bglin gui   (or app menu: bglin)"
echo "  Daemon   : systemctl --user enable --now bglin.service"
echo "  Folder   : ~/Pictures/bglin (drop your wallpapers there)"
