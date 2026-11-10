import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tcl_me_remo import doctor
from tcl_me_remo.ipc import ServiceUnavailable

SHOW = """Controller 12:34:56:78:9A:BC (public)
\tName: steamdeck
\tAlias: steamdeck
\tPowered: yes
\tDiscoverable: no
\tPairable: yes
\tRoles: central
\tRoles: peripheral
Advertising Features:
\tActiveInstances: 0x00 (0)
\tSupportedInstances: 0x05 (5)
"""


def make_root(tmp_path, *, adapter=True):
    (tmp_path / "etc" / "bluetooth").mkdir(parents=True)
    os_release = 'NAME="SteamOS"\nPRETTY_NAME="SteamOS"\nID=steamos\nVERSION_ID=3.7.13\n'
    (tmp_path / "etc" / "os-release").write_text(os_release)
    (tmp_path / "etc" / "bluetooth" / "main.conf").write_text(
        "# comment\n[General]\n#Name = x\nFastConnectable=true\n\n[Policy]\nAutoEnable = true\n"
    )
    if adapter:
        dev = tmp_path / "sys" / "class" / "bluetooth" / "hci0" / "device"
        dev.mkdir(parents=True)
        (dev / "uevent").write_text("DEVTYPE=usb_interface\nDRIVER=btusb\nPRODUCT=8087/32/0\n")
    drm = tmp_path / "sys" / "class" / "drm" / "card0-HDMI-A-1"
    drm.mkdir(parents=True)
    (drm / "status").write_text("connected\n")
    (drm / "edid").write_bytes(b"")
    return tmp_path


def make_runner(overrides=None):
    table = {
        ("systemctl", "--version"): (0, "systemd 257 (257.5-1)\n+PAM"),
        ("/usr/lib/bluetooth/bluetoothd", "--version"): (0, "5.79\n"),
        ("systemctl", "is-active", "bluetooth.service"): (0, "active\n"),
        ("systemctl", "show", "bluetooth.service", "-p", "ExecStart", "--value"): (
            0,
            "{ path=/usr/lib/bluetooth/bluetoothd ; argv[]=/usr/lib/bluetooth/bluetoothd ; }",
        ),
        ("bluetoothctl", "show"): (0, SHOW),
        ("bluetoothctl", "devices", "Paired"): (0, "Device AA:BB:CC:DD:EE:FF Living Room TV\n"),
        ("systemctl", "is-active", "tcl-me-remo.service"): (0, "active\n"),
        ("systemctl", "is-enabled", "tcl-me-remo.service"): (0, "enabled\n"),
        ("systemd-inhibit", "--list", "--no-pager", "--no-legend"): (
            0,
            "tcl-me-remo 0 root 812 python3 sleep Turn the TV off first delay\n"
            "tcl-me-remo 0 root 812 python3 shutdown Turn the TV off first delay\n",
        ),
    }
    table.update(overrides or {})

    async def run(argv, timeout):
        return table.get(tuple(argv), (-1, ""))

    return run


STATUS = {
    "version": "0.1.0",
    "bluetooth": {
        "registered": True,
        "paired": True,
        "ready": True,
        "delivery": "raw",
        "tv_name": "Living Room TV",
        "tv_address": "AA:BB:CC:DD:EE:FF",
        "error": "",
    },
    "display": {"gate_ok": True, "gate_reason": "HDMI-A-1 is connected"},
    "config_problems": [],
    "last_action": {"name": "tv_on", "trigger": "boot", "ok": False, "detail": "the TV did not connect within 30s"},
}


def by_name(report):
    return {check["name"]: check for check in report["checks"]}


def service(raw_notify="auto", status=STATUS):
    """A control socket that answers ``status`` and ``get_config`` like the service."""

    def call(method):
        if method == "get_config":
            return {"config": {"bluetooth": {"raw_notify": raw_notify}}, "schema": {}}
        return status

    return call


async def test_healthy_system(tmp_path):
    report = await doctor.collect(make_runner(), call=lambda method: STATUS, root=make_root(tmp_path))
    json.dumps(report)
    checks = by_name(report)
    assert checks["bluez"] == {"name": "bluez", "status": "ok", "detail": "BlueZ 5.79"}
    assert checks["adapter"]["status"] == "ok" and "btusb" in checks["adapter"]["detail"]
    assert checks["peripheral role"]["status"] == "ok"
    assert checks["advertising"]["detail"] == "5 advertising instance(s) supported"
    assert checks["service"]["status"] == "ok"
    assert checks["TV link"]["detail"] == "Living Room TV is connected (direct delivery)"
    assert checks["suspend lock"]["detail"] == "2 delay lock(s) held"
    assert checks["last action"]["status"] == "warn"  # a failed boot action is surfaced
    assert report["system"]["os"] == "SteamOS" and report["system"]["os_version"] == "3.7.13"
    assert report["bluetooth"]["main_conf"] == {"General.FastConnectable": "true", "Policy.AutoEnable": "true"}
    assert report["bluetooth"]["experimental"] is False
    assert doctor.worst(report) == "warn"

    text = doctor.render(report)
    assert "tcl-me-remo 0.1.0 on SteamOS 3.7.13" in text
    assert "[ ok ] bluez: BlueZ 5.79" in text
    assert "General.FastConnectable = true" in text
    assert "Device AA:BB:CC:DD:EE:FF Living Room TV" in text
    assert "Connected displays: HDMI-A-1" in text


async def test_everything_missing_is_reported_not_raised(tmp_path):
    async def nothing(argv, timeout):
        return -1, ""

    def down(method):
        raise ServiceUnavailable("cannot reach the tcl-me-remo service")

    (tmp_path / "empty").mkdir()
    report = await doctor.collect(nothing, call=down, root=tmp_path / "empty")
    checks = by_name(report)
    assert checks["bluez"]["status"] == "unknown"
    assert checks["adapter"]["status"] == "fail" and "no Bluetooth controller" in checks["adapter"]["detail"]
    assert checks["service"]["status"] == "fail" and "cannot reach" in checks["service"]["detail"]
    assert checks["suspend lock"]["status"] == "unknown"
    assert doctor.worst(report) == "fail"
    assert "none detected" in doctor.render(report)


async def test_specific_faults(tmp_path):
    root = make_root(tmp_path)
    off = SHOW.replace("Powered: yes", "Powered: no").replace("\tRoles: peripheral\n", "")
    overrides = {
        ("bluetoothctl", "show"): (0, off),
        ("/usr/lib/bluetooth/bluetoothd", "--version"): (0, "5.50"),
        ("systemd-inhibit", "--list", "--no-pager", "--no-legend"): (0, "NetworkManager 0 root 1 nm sleep x delay\n"),
        ("systemctl", "show", "bluetooth.service", "-p", "ExecStart", "--value"): (0, "bluetoothd --experimental"),
    }
    unpaired = dict(STATUS, bluetooth={"registered": False, "paired": False, "error": "BlueZ is not running"})
    report = await doctor.collect(make_runner(overrides), call=lambda method: unpaired, root=root)
    checks = by_name(report)
    assert checks["bluez"]["status"] == "fail" and "older than 5.52" in checks["bluez"]["detail"]
    assert checks["adapter"]["detail"] == "Bluetooth is turned off"
    assert checks["peripheral role"]["status"] == "fail"
    assert checks["HID service"] == {"name": "HID service", "status": "fail", "detail": "BlueZ is not running"}
    assert checks["TV pairing"]["status"] == "warn"
    assert checks["suspend lock"]["status"] == "fail"
    assert report["bluetooth"]["experimental"] is True


@pytest.mark.parametrize(
    "kernel, setting, status, said",
    [
        # Linux 6.17 and later: no side effects, so "auto" uses it.
        ("7.2.7-valve3-1-neptune", "auto", "ok", "used when the TV needs it (no side effects from Linux 6.17 on)"),
        ("6.17.0", "auto", "ok", "used when the TV needs it"),
        ("6.17.0", "on", "ok", "used when the TV needs it"),
        ("6.17.0", "off", "ok", "never used; a TV that does not subscribe again after a reboot"),
        ("6.17.0", "always", "ok", "used for every key (a diagnostic setting)"),
        # Older kernels: "auto" leaves it alone and says how to get it.
        ("6.11.11-valve19", "auto", "ok", "not used on this kernel (only from Linux 6.17 on"),
        ("6.16.12-200.fc42.x86_64", "auto", "ok", "or set raw_notify to 'on'"),
        ("6.11.11-valve19", "off", "ok", "never used"),
        # The user asked for it on a kernel it disturbs: say what to watch for.
        ("6.11.11-valve19", "on", "warn", "used when the TV needs it; before Linux 6.17 it can disturb"),
        # The first release candidates of 6.17 did not have the fix yet.
        ("6.17.0-rc3", "auto", "ok", "an early release candidate, treated as older than 6.17"),
        ("6.17.0-rc3", "auto", "ok", "not used on this kernel"),
        ("6.17.0-rc3", "on", "warn", "the service repairs after each link (unverified)"),
        ("6.17.0-rc4", "auto", "ok", "used when the TV needs it (no side effects from Linux 6.17 on)"),
        ("6.11.11-valve19", "always", "warn", "if controllers or headsets stall, set it to 'auto'"),
        # A release that cannot be read counts as old.
        ("garbage", "auto", "ok", "not used on this kernel"),
        ("garbage", "on", "warn", "the service repairs after each link (unverified)"),
        # The service could not be asked, or says something newer than this doctor.
        ("6.17.0", None, "unknown", "with the default 'auto' direct delivery would be used when the TV needs it"),
        ("6.11.11-valve19", None, "unknown", "with the default 'auto' direct delivery would not be used"),
        ("6.17.0", "sometimes", "unknown", "a value this version does not know"),
    ],
)
def test_direct_delivery_line_names_kernel_setting_and_meaning(kernel, setting, status, said):
    check = doctor.direct_delivery_check(kernel, setting)
    assert check["name"] == "direct delivery" and check["status"] == status
    detail = check["detail"]
    assert said in detail and "\n" not in detail
    assert kernel in detail  # the version is named, readable or not
    assert f"raw_notify = {setting}" in detail if setting else "raw_notify unknown" in detail
    if kernel == "garbage":
        assert "version not recognised, treated as older than 6.17" in detail


async def test_report_states_the_direct_delivery_situation(tmp_path):
    root = make_root(tmp_path)
    old = await doctor.collect(make_runner(), call=service("auto"), root=root, kernel="6.11.11-valve19-1-neptune-611")
    check = by_name(old)["direct delivery"]
    assert check["status"] == "ok"
    assert check["detail"].startswith("kernel 6.11.11-valve19-1-neptune-611, raw_notify = auto: not used on this")
    assert old["direct_delivery"] == {
        "kernel": "6.11.11-valve19-1-neptune-611",
        "raw_notify": "auto",
        "side_effect_free": False,
    }
    assert old["system"]["kernel"] == "6.11.11-valve19-1-neptune-611"
    assert "[ ok ] direct delivery: kernel 6.11.11-valve19-1-neptune-611, raw_notify = auto" in doctor.render(old)

    risky = await doctor.collect(make_runner(), call=service("on"), root=root, kernel="6.11.11-valve19-1-neptune-611")
    assert by_name(risky)["direct delivery"]["status"] == "warn"
    assert "[warn] direct delivery" in doctor.render(risky)

    new = await doctor.collect(make_runner(), call=service("auto"), root=root, kernel="6.17.2-arch1-1")
    assert by_name(new)["direct delivery"]["status"] == "ok" and new["direct_delivery"]["side_effect_free"] is True

    # Without an injected kernel it is the running one, and a service that cannot say is "unknown".
    here = await doctor.collect(make_runner(), call=lambda method: STATUS, root=root)
    assert here["direct_delivery"]["kernel"] == doctor.platform.release()
    assert by_name(here)["direct delivery"]["status"] == "unknown"

    def down(method):
        raise ServiceUnavailable("cannot reach the tcl-me-remo service")

    stopped = await doctor.collect(make_runner(), call=down, root=root, kernel="6.17.0")
    assert by_name(stopped)["direct delivery"]["status"] == "unknown"
    assert "raw_notify unknown" in by_name(stopped)["direct delivery"]["detail"]


def test_parsers(tmp_path):
    assert doctor.parse_version("bluetoothctl: 5.83") == (5, 83)
    assert doctor.parse_version("nonsense") == ()
    assert doctor.parse_key_values(SHOW)["Roles"] == "central"  # first occurrence wins
    assert doctor.read_os_release(tmp_path) == {}
    assert doctor.read_main_conf(tmp_path / "nope.conf") == {}
    assert doctor.read_adapters(tmp_path) == []


@pytest.mark.parametrize(
    "bluetooth, status, detail",
    [
        # After a boot or a wake on which nothing was sent: up and unused is not a problem.
        ({"connected": True, "ready": False}, "ok", "Living Room TV is connected (no key sent on this link yet)"),
        (
            {"connected": True, "ready": False, "error": "the TV is connected but will not receive keys: ..."},
            "warn",
            "Living Room TV is connected but not ready for keys: the TV is connected but will not receive keys: ...",
        ),
        (
            {"connected": False, "ready": False},
            "warn",
            "Living Room TV is not connected: the link comes up when it is needed",
        ),
        (
            {"connected": False, "ready": False, "error": "the TV did not connect over Bluetooth"},
            "warn",
            "Living Room TV is not connected: the TV did not connect over Bluetooth",
        ),
    ],
)
async def test_tv_link_line_tells_an_unused_link_from_a_missing_one(tmp_path, bluetooth, status, detail):
    state = {**STATUS, "bluetooth": {**STATUS["bluetooth"], "delivery": "", **bluetooth}}
    report = await doctor.collect(make_runner(), call=service(status=state), root=make_root(tmp_path))
    assert by_name(report)["TV link"] == {"name": "TV link", "status": status, "detail": detail}


def test_the_doctor_does_not_need_the_bluetooth_transport_to_start():
    """It is the tool for a half-broken installation: it must import, and state the direct
    delivery situation, where the transport or the vendored D-Bus library cannot be loaded."""
    script = textwrap.dedent(
        """
        import importlib.abc, sys

        class Broken(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if "dbus_fast" in name or name in ("tcl_me_remo.dbus", "tcl_me_remo.bluez.remote"):
                    raise ImportError(f"simulated: {name} is unusable")
                return None

        sys.meta_path.insert(0, Broken())
        from tcl_me_remo import doctor

        line = doctor.direct_delivery_check("6.11.11-valve19-1-neptune-611", "auto")
        assert line["status"] == "ok" and "not used on this kernel" in line["detail"], line
        loaded = sorted(name for name in sys.modules if "dbus" in name or name.startswith("tcl_me_remo.bluez."))
        assert loaded == ["tcl_me_remo.bluez.kernel"], loaded
        print("fine")
        """
    )
    env = {**os.environ, "PYTHONPATH": str(Path(doctor.__file__).resolve().parents[1])}
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, env=env)
    assert result.returncode == 0 and result.stdout.strip() == "fine", result.stderr
