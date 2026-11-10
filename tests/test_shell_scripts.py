"""install.sh and scripts/steamos-dev-setup.sh, as far as they can run without a real sudo."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


def _stub(directory, name, body):
    path = directory / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def test_install_sh_reaches_sudo_with_its_full_path(tmp_path):
    # "bash install.sh" makes $0 a bare name, which sudo would look up in its own PATH
    # ("sudo: install.sh: command not found").
    if not os.access("/usr/bin/python3", os.X_OK):
        pytest.skip("install.sh insists on /usr/bin/python3")
    stubs = tmp_path / "bin"
    stubs.mkdir()
    _stub(stubs, "id", "echo 1000\n")  # not root, whoever runs the tests
    _stub(stubs, "sudo", 'printf "%s\\n" "$@"\n')
    env = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"}
    out = subprocess.run(
        ["bash", "install.sh", "uninstall", "--purge"], cwd=REPO, env=env, capture_output=True, text=True, timeout=30
    )
    assert out.returncode == 0, out.stderr
    handed_to_sudo = out.stdout.splitlines()[-5:]
    # through bash, so that a lost execute bit (a source zip) does not matter either
    assert handed_to_sudo == ["--", "bash", str(REPO / "install.sh"), "uninstall", "--purge"]


def test_dev_setup_help_prints_the_header_and_nothing_else():
    script = REPO / "scripts" / "steamos-dev-setup.sh"
    out = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0
    lines = out.stdout.splitlines()
    assert lines[0].startswith("Prepare a SteamOS") and lines[-1].lstrip().startswith("7. tells you how to start")
    assert "set -euo pipefail" not in out.stdout and not any(line.startswith("#") for line in lines)
    # every numbered step of the script is announced in the header (the sudo check was missing)
    announced = [line.strip() for line in script.read_text().splitlines() if line.strip().startswith('say "')]
    assert len(announced) == 7 and announced[4] == 'say "5. sudo"' and announced[5] == 'say "6. test tools"'
    assert "5. checks whether sudo can work" in out.stdout
    assert "6. makes a private Python environment" in out.stdout


def test_dev_setup_help_works_when_piped_into_bash():
    # The documented way to run it is "curl ... | bash": there is no script file to read.
    script = (REPO / "scripts" / "steamos-dev-setup.sh").read_text()
    out = subprocess.run(["bash", "-s", "--", "--help"], input=script, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and out.stderr == ""
    assert out.stdout.startswith("usage: steamos-dev-setup.sh [--adb] [--yes] [--branch NAME]")
