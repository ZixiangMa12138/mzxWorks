#!/usr/bin/env bash
set -euo pipefail

# Resolve paths from the script itself so the installer works from any shell cwd.
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
template="$project_dir/desktop/swing-pet.desktop.in"
icon="$project_dir/assets/icons/swing-pet.png"

if [[ ! -f "$template" || ! -f "$icon" ]]; then
  echo "Desktop launcher template or icon is missing." >&2
  exit 1
fi

# xdg-user-dir respects localized desktop folders such as ~/桌面. Fall back to
# the conventional English name only when the XDG helper is unavailable.
if command -v xdg-user-dir >/dev/null 2>&1; then
  desktop_dir="$(xdg-user-dir DESKTOP)"
else
  desktop_dir="$HOME/Desktop"
fi
applications_dir="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

mkdir -p "$desktop_dir" "$applications_dir"
temporary="$(mktemp)"
trap 'rm -f "$temporary"' EXIT

# Desktop Entry files do not expand shell variables, so substitute the absolute
# project directory at install time. The current project path contains no '|'.
sed "s|@PROJECT_DIR@|$project_dir|g" "$template" > "$temporary"
install -m 755 "$temporary" "$desktop_dir/秋千猫.desktop"
install -m 755 "$temporary" "$applications_dir/swing-pet.desktop"

# GNOME stores the launcher trust bit as file metadata. Other desktops may not
# support it, so failure here is harmless: executable permissions still apply.
if command -v gio >/dev/null 2>&1; then
  gio set "$desktop_dir/秋千猫.desktop" metadata::trusted true 2>/dev/null || true
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$applications_dir" >/dev/null 2>&1 || true
fi

echo "Desktop shortcut installed: $desktop_dir/秋千猫.desktop"
echo "Application menu entry installed: $applications_dir/swing-pet.desktop"
