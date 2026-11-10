#!/usr/bin/env bash
# Refresh the vendored copy of dbus-fast under py_modules/tcl_me_remo/_vendor/.
#
# SteamOS has a read-only root and no pip, so the one third-party dependency is committed to
# the repository as plain Python source.  dbus-fast ships optional Cython accelerators; only
# the .py files are vendored, which the library supports as its pure-Python mode.
set -euo pipefail

VERSION="5.2.0"
URL="https://files.pythonhosted.org/packages/4c/5b/ce64b8788c10a8bd313c8638b28be5dccdd5c2daf14839f23aff37e0b39d/dbus_fast-${VERSION}.tar.gz"
SHA256="a4a5dddc04b1ade5eb7650d791e2f6fb7c1334595593473914e78a2526ecddda"

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest="$root/py_modules/tcl_me_remo/_vendor"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

curl -fsSL -o "$work/dbus_fast.tar.gz" "$URL"
actual="$(sha256sum "$work/dbus_fast.tar.gz" | cut -d' ' -f1)"
if [[ "${1:-}" == "--print-sha256" ]]; then
    echo "$actual"
    exit 0
fi
if [[ "$actual" != "$SHA256" ]]; then
    echo "sha256 mismatch for dbus-fast ${VERSION}: expected $SHA256, got $actual" >&2
    exit 1
fi

tar -xzf "$work/dbus_fast.tar.gz" -C "$work"
src="$work/dbus_fast-${VERSION}"

rm -rf "$dest/dbus_fast"
mkdir -p "$dest"
cp -r "$src/src/dbus_fast" "$dest/dbus_fast"
# Pure-Python mode: drop Cython declaration files, the GLib backend (needs PyGObject) and
# typing markers.
find "$dest/dbus_fast" -name '*.pxd' -delete
find "$dest/dbus_fast" -name 'py.typed' -delete
find "$dest/dbus_fast" -name '__pycache__' -prune -exec rm -rf {} +
rm -rf "$dest/dbus_fast/glib"
cp "$src/LICENSE" "$dest/dbus_fast/LICENSE"
printf '%s\n' "$VERSION" > "$dest/dbus_fast/VENDORED_VERSION"
echo "vendored dbus-fast ${VERSION} into ${dest#"$root"/}/dbus_fast"
