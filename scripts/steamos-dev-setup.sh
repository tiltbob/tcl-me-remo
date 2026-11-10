#!/usr/bin/env bash
# Prepare a SteamOS (or Bazzite) machine for hardware testing with Claude Code.
#
# Run it on the machine itself, as your normal user, from Desktop Mode's Konsole or over SSH:
#
#   curl -fsSL https://raw.githubusercontent.com/tiltbob/tcl-me-remo/main/scripts/steamos-dev-setup.sh | bash
#
# or, from a checkout:   scripts/steamos-dev-setup.sh [--adb] [--yes] [--branch NAME]
#
# What it does, all inside your home directory (the read-only system is not touched):
#   1. installs Claude Code with Anthropic's native installer (~/.local/bin/claude)
#   2. makes sure ~/.local/bin is on your PATH
#   3. clones this repository to ~/tcl-me-remo if you are not already in a checkout
#   4. with --adb: downloads Google's Android platform-tools so the TV can be inspected
#   5. tells you how to start the session
set -euo pipefail

REPO_URL="https://github.com/tiltbob/tcl-me-remo"
BRANCH=""
WANT_ADB=0
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --adb) WANT_ADB=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        --branch) BRANCH="${2:?--branch needs a name}"; shift ;;
        -h|--help) sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }

confirm() {
    [[ $ASSUME_YES -eq 1 ]] && return 0
    # Read from the terminal even when the script itself arrives on stdin (curl | bash).
    local answer
    read -r -p "  $1 [y/N] " answer </dev/tty || return 1
    [[ "$answer" == [yY]* ]]
}

if [[ "$(id -u)" -eq 0 ]]; then
    echo "Run this as your normal user, not as root." >&2
    exit 1
fi

# ---------------------------------------------------------------- 1. Claude Code
say "1. Claude Code"
export PATH="$HOME/.local/bin:$PATH"
if command -v claude >/dev/null 2>&1; then
    note "already installed: $(command -v claude)"
else
    note "Not installed. Anthropic's installer puts it in ~/.local/bin (no root, no system changes):"
    note "    curl -fsSL https://claude.ai/install.sh | bash"
    if confirm "Run it now?"; then
        curl -fsSL https://claude.ai/install.sh | bash
    else
        note "Skipped. Install it yourself, then run this script again."
    fi
fi

# ---------------------------------------------------------------- 2. PATH
say "2. PATH"
if grep -qs '\.local/bin' "$HOME/.bashrc"; then
    note "~/.local/bin is already on your PATH in ~/.bashrc"
else
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
    note "added ~/.local/bin to PATH in ~/.bashrc (open a new terminal, or: source ~/.bashrc)"
fi

# ---------------------------------------------------------------- 3. the repository
say "3. tcl-me-remo checkout"
if git -C "$PWD" rev-parse --show-toplevel >/dev/null 2>&1 && [[ -f "$(git -C "$PWD" rev-parse --show-toplevel)/py_modules/tcl_me_remo/__init__.py" ]]; then
    CHECKOUT="$(git -C "$PWD" rev-parse --show-toplevel)"
    note "using the checkout you are in: $CHECKOUT"
elif [[ -d "$HOME/tcl-me-remo/.git" ]]; then
    CHECKOUT="$HOME/tcl-me-remo"
    note "found $CHECKOUT; updating it"
    git -C "$CHECKOUT" pull --ff-only || note "could not update (local changes?); carrying on with what is there"
else
    CHECKOUT="$HOME/tcl-me-remo"
    git clone "$REPO_URL" "$CHECKOUT"
fi
if [[ -n "$BRANCH" ]]; then
    git -C "$CHECKOUT" fetch origin "$BRANCH"
    git -C "$CHECKOUT" checkout "$BRANCH"
    git -C "$CHECKOUT" pull --ff-only origin "$BRANCH" || true
fi

# ---------------------------------------------------------------- 4. adb (optional)
if [[ $WANT_ADB -eq 1 ]]; then
    say "4. Android platform-tools (adb)"
    TOOLS="$HOME/.local/share/tcl-me-remo-dev"
    if [[ -x "$TOOLS/platform-tools/adb" ]]; then
        note "already there: $TOOLS/platform-tools/adb"
    else
        mkdir -p "$TOOLS"
        curl -fsSL -o "$TOOLS/platform-tools.zip" https://dl.google.com/android/repository/platform-tools-latest-linux.zip
        python3 -m zipfile -e "$TOOLS/platform-tools.zip" "$TOOLS"
        chmod +x "$TOOLS/platform-tools/adb"
        rm -f "$TOOLS/platform-tools.zip"
    fi
    mkdir -p "$HOME/.local/bin"
    ln -sf "$TOOLS/platform-tools/adb" "$HOME/.local/bin/adb"
    note "adb is available as ~/.local/bin/adb"
    note "On the TV: Settings > System > About > press OK on 'Android TV OS build' 7 times,"
    note "then Settings > System > Developer options > USB debugging / Network debugging: On."
fi

# ---------------------------------------------------------------- 5. sudo
say "5. sudo"
if sudo -n true 2>/dev/null; then
    note "sudo works without asking (fine for a test session)"
elif [[ "$(passwd -S 2>/dev/null | awk '{print $2}')" == "NP" ]]; then
    note "Your user has no password yet, so sudo cannot work. Set one with:   passwd"
    note "Installing the service needs sudo once."
else
    note "sudo will ask for your password when the service is installed."
fi

# ---------------------------------------------------------------- next steps
say "Ready. Next:"
cat <<NEXT
  cd "$CHECKOUT"
  ./install.sh                      # installs and starts the service (asks for sudo)

  Then start Claude Code in that folder. Either sit at this machine:

      claude "Read CLAUDE.md and docs/hardware-testing.md, then take me through the hardware tests."

  or drive it from your phone or another computer (the session shows up under Code in the
  Claude app; this terminal has to stay open):

      claude --remote-control "tcl-me-remo hardware"

  The first start opens a browser to sign in. If no browser opens, press c to copy the link
  and open it on any device.

  Stay near the TV: pairing needs you to pick this machine in the TV's settings once, and
  several tests ask what the TV is showing.
NEXT
