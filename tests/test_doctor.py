import json

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


def test_parsers(tmp_path):
    assert doctor.parse_version("bluetoothctl: 5.83") == (5, 83)
    assert doctor.parse_version("nonsense") == ()
    assert doctor.parse_key_values(SHOW)["Roles"] == "central"  # first occurrence wins
    assert doctor.read_os_release(tmp_path) == {}
    assert doctor.read_main_conf(tmp_path / "nope.conf") == {}
    assert doctor.read_adapters(tmp_path) == []
