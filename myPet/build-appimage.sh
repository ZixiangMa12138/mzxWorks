#!/usr/bin/env bash
set -euo pipefail

# Build one self-mounting PetCat AppImage from the current checkout. The Python
# runtime and project payload are embedded; GTK/GStreamer remain platform
# libraries because bundling glibc-facing desktop stacks reduces portability.
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
build_root="${PETCAT_APPIMAGE_BUILD_DIR:-$project_dir/build/appimage}"
appdir="$build_root/PetCat.AppDir"
tool_dir="$build_root/tools"
dist_dir="${PETCAT_APPIMAGE_DIST_DIR:-$project_dir/dist/appimage}"
version="${VERSION:-1.0.0}"

case "$(uname -m)" in
  x86_64) appimage_arch=x86_64 ;;
  aarch64|arm64) appimage_arch=aarch64 ;;
  *) echo "Unsupported AppImage architecture: $(uname -m)" >&2; exit 2 ;;
esac

for command_name in python3 install cp find curl; do
  command -v "$command_name" >/dev/null || {
    echo "Missing build command: $command_name" >&2
    exit 2
  }
done

python_executable="$(command -v python3)"
python_version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
python_stdlib="$(python3 -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')"
gi_dir="$(python3 -c 'import gi; print(gi.__path__[0])')"
cairo_dir="$(python3 -c 'import cairo, pathlib; print(pathlib.Path(cairo.__file__).parent)')"
pil_dir="$(python3 -c 'import PIL; print(PIL.__path__[0])')"

test -f "$project_dir/src/standalone_pet.py" || { echo "Missing pet runtime" >&2; exit 2; }
test -f "$project_dir/assets/icons/swing-pet.png" || { echo "Missing pet icon" >&2; exit 2; }

# This directory is generated output only. Recreating it never touches sources,
# runtime settings, or an existing AppImage under dist/.
if test -d "$appdir"; then
  find "$appdir" -depth -mindepth 1 -delete
fi
install -d \
  "$appdir/usr/bin" \
  "$appdir/usr/lib/python$python_version" \
  "$appdir/usr/lib/python3/dist-packages" \
  "$appdir/usr/share/applications" \
  "$appdir/usr/share/icons/hicolor/512x512/apps" \
  "$appdir/usr/share/metainfo" \
  "$appdir/usr/share/petcat/src" \
  "$appdir/usr/share/petcat/assets/runtime" \
  "$appdir/usr/share/petcat/assets/audio" \
  "$tool_dir" "$dist_dir"

# Embed a matching CPython interpreter and standard library. Runtime-only third
# party packages are copied explicitly so NumPy/SciPy build dependencies do not
# inflate the desktop application.
install -m 0755 "$python_executable" "$appdir/usr/bin/python3"
cp -a "$python_stdlib/." "$appdir/usr/lib/python$python_version/"
cp -a "$gi_dir" "$appdir/usr/lib/python3/dist-packages/gi"
cp -a "$cairo_dir" "$appdir/usr/lib/python3/dist-packages/cairo"
cp -a "$pil_dir" "$appdir/usr/lib/python3/dist-packages/PIL"

install -m 0644 "$project_dir/src/standalone_pet.py" "$appdir/usr/share/petcat/src/"
install -m 0644 "$project_dir/src/codex_bridge.py" "$appdir/usr/share/petcat/src/"
install -m 0644 "$project_dir/assets/runtime/swing-interactive.png" "$appdir/usr/share/petcat/assets/runtime/"
install -m 0644 "$project_dir/assets/runtime/standing-transparent.png" "$appdir/usr/share/petcat/assets/runtime/"
install -m 0644 "$project_dir/assets/runtime/jump-down.png" "$appdir/usr/share/petcat/assets/runtime/"
install -m 0644 "$project_dir/assets/runtime/jump-up.png" "$appdir/usr/share/petcat/assets/runtime/"

for audio_name in \
  landing-zhedia.mp3 drag-wocao.mp3 danger-rotor.mp3 \
  explosion.wav chains-metal.wav unlock.wav; do
  install -m 0644 "$project_dir/assets/audio/$audio_name" \
    "$appdir/usr/share/petcat/assets/audio/$audio_name"
done

install -m 0755 "$project_dir/packaging/appimage/AppRun" "$appdir/AppRun"
sed "s/@VERSION@/$version/g" "$project_dir/packaging/appimage/petcat.desktop" \
  > "$appdir/petcat.desktop"
install -m 0644 "$appdir/petcat.desktop" "$appdir/usr/share/applications/petcat.desktop"
sed "s/@VERSION@/$version/g" "$project_dir/packaging/appimage/petcat.metainfo.xml" \
  > "$appdir/usr/share/metainfo/io.github.petcat.metainfo.xml"
install -m 0644 "$project_dir/assets/icons/swing-pet.png" "$appdir/petcat.png"
install -m 0644 "$project_dir/assets/icons/swing-pet.png" \
  "$appdir/usr/share/icons/hicolor/512x512/apps/petcat.png"
ln -sfn petcat.png "$appdir/.DirIcon"

# Validate the payload before compression. Explicitly importing cairo catches
# a subtle PyGObject failure where GTK starts but cannot convert cairo.Context
# in draw callbacks.
"$appdir/AppRun" --check-cairo
"$appdir/AppRun" --diagnose

# Prefer an explicitly supplied packager, then PATH, then either form of our
# local cache. linuxdeploy exposes the extracted form used here, which enables
# fully offline builds without FUSE.
extracted_appimagetool="$tool_dir/appimagetool-root/usr/bin/appimagetool"
if test -n "${APPIMAGETOOL:-}"; then
  appimagetool="$APPIMAGETOOL"
elif command -v appimagetool >/dev/null 2>&1; then
  appimagetool="$(command -v appimagetool)"
elif test -x "$extracted_appimagetool"; then
  appimagetool="$extracted_appimagetool"
else
  appimagetool="$tool_dir/appimagetool-$appimage_arch.AppImage"
fi
if ! test -x "$appimagetool"; then
  tool_url="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$appimage_arch.AppImage"
  echo "Downloading official appimagetool for $appimage_arch..."
  curl -L --fail --retry 3 --connect-timeout 15 --max-time 300 \
    --output "$appimagetool.download" "$tool_url"
  chmod 0755 "$appimagetool.download"
  mv "$appimagetool.download" "$appimagetool"
fi

output="$dist_dir/PetCat-$version-$appimage_arch.AppImage"
runtime_file="${APPIMAGE_RUNTIME_FILE:-}"
if test -z "$runtime_file" && test -s "$tool_dir/runtime-$appimage_arch"; then
  runtime_file="$tool_dir/runtime-$appimage_arch"
fi
runtime_options=()
if test -n "$runtime_file"; then
  test -s "$runtime_file" || {
    echo "AppImage runtime does not exist: $runtime_file" >&2
    exit 2
  }
  runtime_options=(--runtime-file "$runtime_file")
fi

ARCH="$appimage_arch" VERSION="$version" APPIMAGE_EXTRACT_AND_RUN=1 \
  "$appimagetool" --no-appstream "${runtime_options[@]}" "$appdir" "$output"
chmod 0755 "$output"
(
  cd "$dist_dir"
  sha256sum "$(basename "$output")" > SHA256SUMS
)
printf 'Built %s\nChecksum: %s\n' "$output" "$dist_dir/SHA256SUMS"
