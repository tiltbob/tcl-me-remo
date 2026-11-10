import os
import socket

from tcl_me_remo import system


def fake_runner(state, jobs="", jobs_code=0):
    calls = []

    async def run(argv, timeout):
        calls.append(argv)
        if "is-system-running" in argv:
            return (0 if state == "running" else 1), state + "\n"
        return jobs_code, jobs

    run.calls = calls
    return run


async def test_shutdown_kind_not_shutting_down():
    for state in ("running", "degraded", "starting", ""):
        run = fake_runner(state)
        assert await system.shutdown_kind(run) is None
        assert len(run.calls) == 1  # no need to look at the job queue


async def test_shutdown_kind_distinguishes_reboot_from_poweroff():
    reboot = "  812 reboot.target                 start waiting\n  813 systemd-reboot.service        start waiting\n"
    assert await system.shutdown_kind(fake_runner("stopping", reboot)) == "reboot"
    poweroff = "901 poweroff.target start waiting\n902 shutdown.target start waiting\n"
    assert await system.shutdown_kind(fake_runner("stopping", poweroff)) == "shutdown"
    assert await system.shutdown_kind(fake_runner("stopping", "5 kexec.target start waiting\n")) == "reboot"
    assert await system.shutdown_kind(fake_runner("stopping", "5 halt.target start waiting\n")) == "shutdown"


async def test_shutdown_kind_defaults_to_poweroff_when_unsure():
    assert await system.shutdown_kind(fake_runner("stopping", "")) == "shutdown"
    assert await system.shutdown_kind(fake_runner("stopping", "garbage", jobs_code=-1)) == "shutdown"


async def test_run_command_handles_missing_binary_and_timeouts():
    assert await system.run_command(["/no/such/binary-xyz"], 1.0) == (-1, "")
    code, out = await system.run_command(["sh", "-c", "echo hello"], 5.0)
    assert (code, out.strip()) == (0, "hello")
    assert await system.run_command(["sleep", "5"], 0.1) == (-1, "")


def test_first_start_since_boot(tmp_path):
    run_dir = tmp_path / "run" / "tcl-me-remo"
    assert system.first_start_since_boot(run_dir) is True
    assert system.first_start_since_boot(run_dir) is False  # a service restart is not a boot
    assert (run_dir / "booted").exists()


def test_clean_env_drops_frozen_host_library_path(monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/_MEIabc")
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG", raising=False)
    assert "LD_LIBRARY_PATH" not in system.clean_env()
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/opt/lib")
    assert system.clean_env()["LD_LIBRARY_PATH"] == "/opt/lib"
    assert os.environ["LD_LIBRARY_PATH"] == "/tmp/_MEIabc"  # the process environment is untouched


def test_sd_notify(tmp_path, monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert system.sd_notify("READY=1") is False
    path = str(tmp_path / "notify.sock")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(path)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        assert system.sd_notify("READY=1") is True
        assert server.recv(64) == b"READY=1"
    monkeypatch.setenv("NOTIFY_SOCKET", str(tmp_path / "gone.sock"))
    assert system.sd_notify("READY=1") is False
