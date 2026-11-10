#!/usr/bin/env bash
# Install (or update) the tcl-me-remo service from this checkout.
#
#   ./install.sh              install or update, then start the service
#   ./install.sh uninstall    stop and remove the service (settings and the pairing are kept)
#   ./install.sh uninstall --purge    ... and delete the settings and the Bluetooth pairing
#   ./install.sh status
#
# The Decky plugin does the same thing on its own; this script is for using the service
# without Decky, or for trying a checkout.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command="${1:-install}"
[[ $# -gt 0 ]] && shift

python=/usr/bin/python3
if [[ ! -x "$python" ]]; then
    echo "tcl-me-remo: $python not found; Python 3.11 or newer is required" >&2
    exit 1
fi

if [[ "$command" != "status" && "$(id -u)" -ne 0 ]]; then
    if ! command -v sudo >/dev/null; then
        echo "tcl-me-remo: run this as root" >&2
        exit 1
    fi
    if [[ "$command" == "install" && ":$PATH:" != *":$HOME/.local/bin:"* ]]; then
        # Checked here because sudo replaces PATH: the installer itself cannot see the user's.
        echo "Note: the tcl-me-remo command is linked into ~/.local/bin, which is not on your PATH."
        echo "      Type ~/.local/bin/tcl-me-remo, or add the directory once and open a new terminal:"
        echo "        echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.bashrc"
    fi
    echo "Root is needed to install a system service; asking sudo."
    echo "(On SteamOS, set a password first with: passwd)"
    # Not "$0": started as "bash install.sh" that is a bare name, which sudo would look up
    # in its own PATH.  And through bash, so that a lost execute bit does not matter.
    exec sudo -- bash "$here/install.sh" "$command" "$@"
fi

args=("$command" "$@")
if [[ "$command" == "install" && -n "${SUDO_USER:-}" ]]; then
    home="$(getent passwd "$SUDO_USER" | cut -d: -f6)"
    [[ -n "$home" ]] && args+=(--user-home "$home")
fi

# -I: ignore the caller's environment and user site-packages; this runs as root.
exec "$python" -I -c '
import sys
sys.path.insert(0, sys.argv[1])
from tcl_me_remo import installer
sys.exit(installer.main(sys.argv[2:]))
' "$here/py_modules" "${args[@]}"
