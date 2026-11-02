#!/bin/sh
set -e
dir=$(cd "$(dirname "$0")" && pwd)
if ! (cd "$dir" && python3 -c "import marstek_tray"); then
    echo "The app cannot start: see the requirements in the README." >&2
    exit 1
fi
for target in "$HOME/.config/autostart" "$HOME/.local/share/applications"; do
    mkdir -p "$target"
    sed "s|@DIR@|$dir|" "$dir/marstek-tray.desktop" > "$target/marstek-tray.desktop"
done
# Restart the app if it was already running, to pick up a moved or updated folder.
pkill -f "python3 $dir/marstek_tray.py\$" || true
setsid python3 "$dir/marstek_tray.py" >/dev/null 2>&1 &
echo "Installed and started: launched at login, with a \"Marstek Tray\" entry in the application menu."
