#!/bin/sh
# Installs the sonyctl binary for the current user and adds it to the app menu.
set -e
here="$(cd "$(dirname "$0")" && pwd)"
bin_dir="$HOME/.local/bin"
app_dir="$HOME/.local/share/applications"

mkdir -p "$bin_dir" "$app_dir"
# Works from the release archive (binary next to this script) or a repo checkout (dist/sonyctl).
if [ -x "$here/sonyctl" ] && [ ! -d "$here/sonyctl" ]; then src="$here/sonyctl"
elif [ -x "$here/dist/sonyctl" ]; then src="$here/dist/sonyctl"
else echo "No sonyctl binary found. Build it first with ./build.sh" >&2; exit 1; fi
install -m 755 "$src" "$bin_dir/sonyctl"

cat > "$app_dir/sonyctl.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Sony XM4 Control
Comment=Battery and noise canceling for Sony WH-1000XM4
Exec=$bin_dir/sonyctl gui --connect
Icon=audio-headphones
Terminal=false
Categories=AudioVideo;Audio;Settings;
EOF

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$app_dir" 2>/dev/null || true

echo "Installed: $bin_dir/sonyctl"
echo "App menu:  Sony XM4 Control"
case ":$PATH:" in
  *":$bin_dir:"*) echo "Run 'sonyctl' for the window or 'sonyctl battery' etc. in a terminal." ;;
  *) echo "Note: $bin_dir is not on your PATH; run it as $bin_dir/sonyctl or add that folder to PATH." ;;
esac
