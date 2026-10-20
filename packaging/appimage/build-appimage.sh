#!/usr/bin/env bash
# Build Replicate-<version>-x86_64.AppImage.
#
# Two stages:
#  1. appimage-builder assembles the AppDir (GTK 4, libadwaita, Python,
#     python-tds, GSSAPI, qemu-img, virsh, xorriso, bundled glibc) from
#     Ubuntu 24.04 packages, following packaging/appimage/AppImageBuilder.yml.
#  2. appimagetool packs that AppDir with the static "type2" runtime, which
#     mounts with FUSE 3 or FUSE 2 and extracts itself when there is no FUSE at
#     all. appimage-builder's own runtime needs libfuse2, which Rocky, Fedora
#     and newer Ubuntu no longer install by default.
#
# Needs: python3, pip, appimage-builder ("appimage-builder==1.1.0" "packaging<22":
# 1.1.0 parses Debian versions with `packaging`, which became strict in 22),
# squashfs-tools, zsync, desktop-file-utils, and libgdk-pixbuf2.0-bin for the
# loaders-cache step in the recipe. Run on Ubuntu 24.04; the GitHub workflow in
# .github/workflows/appimage.yml does exactly this.
set -euo pipefail

cd "$(dirname "$0")"
export REPLICATE_VERSION="${REPLICATE_VERSION:-$(python3 -c 'import tomllib,pathlib;print(tomllib.loads(pathlib.Path("../../pyproject.toml").read_text())["project"]["version"])')}"
ARCH="${ARCH:-x86_64}"
# Upstream publishes only rolling "continuous" builds, so the runtime (the
# part that ends up inside every AppImage) is pinned by checksum instead;
# when upstream moves, the build stops here until the new hash is reviewed.
TYPE2_RUNTIME_TAG="${TYPE2_RUNTIME_TAG:-continuous}"
TYPE2_RUNTIME_SHA256="${TYPE2_RUNTIME_SHA256:-617aa77314d57c8b8e4fae9c3f0b35af7b30d5529cb69e98f64f92510ed98a46}"
APPIMAGETOOL_TAG="${APPIMAGETOOL_TAG:-continuous}"
UPDATE_INFO="gh-releases-zsync|tahuffman1s|Replicate|latest|Replicate-*${ARCH}.AppImage.zsync"

fetch() {  # fetch URL DEST
  if [ ! -s "$2" ]; then
    echo "Fetching $1"
    curl -fsSL -o "$2.part" "$1" && mv "$2.part" "$2"
  fi
}

# -- 1. the AppDir -----------------------------------------------------------
if command -v appimage-builder >/dev/null 2>&1; then
  builder=appimage-builder
else
  fetch https://github.com/AppImageCrafters/appimage-builder/releases/download/v1.1.0/appimage-builder-1.1.0-x86_64.AppImage appimage-builder.AppImage
  chmod +x appimage-builder.AppImage
  builder="./appimage-builder.AppImage --appimage-extract-and-run"
fi
rm -rf AppDir
$builder --recipe AppImageBuilder.yml --skip-test "$@"
test -d AppDir/usr/lib/python3/dist-packages/replicate

# -- 2. pack it with the static runtime ---------------------------------------
fetch "https://github.com/AppImage/type2-runtime/releases/download/${TYPE2_RUNTIME_TAG}/runtime-${ARCH}" "runtime-${ARCH}"
fetch "https://github.com/AppImage/appimagetool/releases/download/${APPIMAGETOOL_TAG}/appimagetool-${ARCH}.AppImage" appimagetool.AppImage
chmod +x appimagetool.AppImage "runtime-${ARCH}"
if [ "$TYPE2_RUNTIME_SHA256" != skip ]; then
  echo "${TYPE2_RUNTIME_SHA256}  runtime-${ARCH}" | sha256sum -c - || {
    echo "The AppImage runtime changed upstream; review it, then update TYPE2_RUNTIME_SHA256 in $0" >&2
    exit 1
  }
fi
out="Replicate-${REPLICATE_VERSION}-${ARCH}.AppImage"
rm -f "$out" "$out.zsync"
ARCH="$ARCH" ./appimagetool.AppImage --appimage-extract-and-run \
  --runtime-file "runtime-${ARCH}" -u "$UPDATE_INFO" AppDir "$out"
# appimage-builder also left its own (libfuse2) image behind; keep only ours.
find . -maxdepth 1 -name 'Replicate-*.AppImage*' ! -name "$out" ! -name "$out.zsync" -delete
ls -la "$out" "$out.zsync"
./"$out" --appimage-extract-and-run --version
