#!/bin/sh
dir=$(cd "$(dirname "$0")" && pwd)
pkill -f "python3 $dir/marstek_tray.py\$"
rm -f "$HOME/.config/autostart/marstek-tray.desktop" "$HOME/.local/share/applications/marstek-tray.desktop"
echo "Uninstalled: app stopped, no longer launched at login nor listed in the application menu."
echo "The settings are kept: $HOME/.config/marstek-tray.toml"
