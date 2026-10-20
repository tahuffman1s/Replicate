#!/usr/bin/env bash
# Build Replicate-<version>-x86_64.AppImage with appimage-builder.
#
# Needs: python3, pip, and appimage-builder:
#     pip install "appimage-builder==1.1.0" "packaging<22"
# (1.1.0 parses Debian versions with `packaging`, which became strict in 22;
# the pin keeps it working), or its own AppImage from
# https://github.com/AppImageCrafters/appimage-builder/releases.
# Run on Ubuntu 24.04 (the recipe pulls its GTK stack from noble); the
# GitHub workflow in .github/workflows/appimage.yml does exactly this.
set -euo pipefail

cd "$(dirname "$0")"
export REPLICATE_VERSION="${REPLICATE_VERSION:-$(python3 -c 'import tomllib,pathlib;print(tomllib.loads(pathlib.Path("../../pyproject.toml").read_text())["project"]["version"])')}"

if command -v appimage-builder >/dev/null 2>&1; then
  builder=appimage-builder
elif [ -x ./appimage-builder.AppImage ]; then
  builder=./appimage-builder.AppImage
else
  echo "Fetching appimage-builder"
  curl -fsSL -o appimage-builder.AppImage \
    https://github.com/AppImageCrafters/appimage-builder/releases/download/v1.1.0/appimage-builder-1.1.0-x86_64.AppImage
  chmod +x appimage-builder.AppImage
  builder=./appimage-builder.AppImage
fi

"$builder" --recipe AppImageBuilder.yml --skip-test "$@"
ls -la ./*.AppImage
