"""``tcl-me-remo doctor``: one report with everything needed to judge a set-up or a bug.

It only reads: system facts, the Bluetooth stack's own view of the adapter, and the
service's status.  Every external tool is optional; a missing tool becomes "unknown", never
an error, because the report is most useful exactly when things are half broken.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import re
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import __version__, display, paths, system
from .ipc import RpcError, call_sync

Runner = Callable[[list[str], float], Awaitable[tuple[int, str]]]

OK, WARN, FAIL, UNKNOWN = "ok", "warn", "fail", "unknown"
_MARK = {OK: "[ ok ]", WARN: "[warn]", FAIL: "[FAIL]", UNKNOWN: "[ ?? ]"}

# BlueZ gained pinned attribute handles (needed for a stable HID table) in 5.52.
MIN_BLUEZ = (5, 52)
SERVICE_UNIT = "tcl-me-remo.service"
BLUETOOTHD_PATHS = ("/usr/lib/bluetooth/bluetoothd", "/usr/libexec/bluetooth/bluetoothd", "/usr/sbin/bluetoothd")


def _check(name: str, status: str, detail: str) -> dict[str, str]:
    return {"name": name, "status": status, "detail": detail}


def parse_key_values(text: str) -> dict[str, str]:
    """Parse ``Key: value`` lines (as printed by bluetoothctl); the first occurrence wins."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition(":")
        if sep and key and key not in out and not key.startswith(("[", "Controller", "Device")):
            out[key.strip()] = value.strip()
    return out


def parse_version(text: str) -> tuple[int, ...]:
    match = re.search(r"(\d+)\.(\d+)", text)
    return (int(match.group(1)), int(match.group(2))) if match else ()


def read_os_release(root: Path = Path("/")) -> dict[str, str]:
    for candidate in (root / "etc/os-release", root / "usr/lib/os-release"):
        try:
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        out = {}
        for line in lines:
            key, sep, value = line.partition("=")
            if sep:
                out[key.strip()] = value.strip().strip('"')
        return out
    return {}


def read_main_conf(path: Path = Path("/etc/bluetooth/main.conf")) -> dict[str, str]:
    """Settings that are actually set (uncommented) in BlueZ's main.conf, as ``Section.Key``."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    out: dict[str, str] = {}
    section = ""
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, sep, value = line.partition("=")
        if sep:
            out[f"{section}.{key.strip()}"] = value.strip()
    return out


def read_adapters(sys_root: Path = Path("/sys")) -> list[dict[str, str]]:
    """Bluetooth controllers as the kernel sees them (works without bluetoothd)."""
    found = []
    base = sys_root / "class" / "bluetooth"
    try:
        entries = sorted(p for p in base.iterdir() if re.fullmatch(r"hci\d+", p.name))
    except OSError:
        return []
    for entry in entries:
        info = {"name": entry.name, "driver": "", "id": ""}
        try:
            for line in (entry / "device" / "uevent").read_text(errors="replace").splitlines():
                key, _, value = line.partition("=")
                if key == "DRIVER":
                    info["driver"] = value
                elif key in ("PRODUCT", "PCI_ID", "SDIO_ID"):
                    info["id"] = value
        except OSError:
            pass
        found.append(info)
    return found


async def collect(
    run: Runner = system.run_command,
    *,
    call: Callable[..., Any] | None = None,
    root: Path = Path("/"),
) -> dict[str, Any]:
    """Gather the report as a JSON-serialisable dict."""
    sys_root = root / "sys"
    checks: list[dict[str, str]] = []

    # ---------------------------------------------------------------- system
    os_release = read_os_release(root)
    _, systemd_out = await run(["systemctl", "--version"], 3.0)
    report: dict[str, Any] = {
        "tcl_me_remo": __version__,
        "system": {
            "os": os_release.get("PRETTY_NAME") or os_release.get("NAME", "unknown"),
            "os_id": os_release.get("ID", ""),
            "os_version": os_release.get("VERSION_ID", "") or os_release.get("BUILD_ID", ""),
            "kernel": platform.release(),
            "python": platform.python_version(),
            "python_has_af_bluetooth": hasattr(__import__("socket"), "AF_BLUETOOTH"),
            "systemd": systemd_out.splitlines()[0] if systemd_out else "",
            "is_root": os.geteuid() == 0,
        },
    }
    python = platform.python_version()
    if sys.version_info >= (3, 11):  # noqa: UP036 - the doctor must also run on a Python that is too old
        checks.append(_check("python", OK, f"Python {python}"))
    else:
        checks.append(_check("python", FAIL, f"Python {python} is too old (3.11 or newer needed)"))

    # ---------------------------------------------------------------- bluetooth stack
    bluez_version = ""
    for path in BLUETOOTHD_PATHS:
        code, out = await run([path, "--version"], 3.0)
        if code == 0 and out.strip():
            bluez_version = out.strip()
            break
    if not bluez_version:
        code, out = await run(["bluetoothctl", "--version"], 3.0)
        if code == 0:
            bluez_version = out.strip().replace("bluetoothctl:", "").strip()
    version = parse_version(bluez_version)
    if not version:
        checks.append(_check("bluez", UNKNOWN, "could not find bluetoothd or bluetoothctl"))
    elif version < MIN_BLUEZ:
        checks.append(_check("bluez", FAIL, f"BlueZ {bluez_version} is older than {MIN_BLUEZ[0]}.{MIN_BLUEZ[1]}"))
    else:
        checks.append(_check("bluez", OK, f"BlueZ {bluez_version}"))

    _, bt_active = await run(["systemctl", "is-active", "bluetooth.service"], 3.0)
    _, bt_unit = await run(["systemctl", "show", "bluetooth.service", "-p", "ExecStart", "--value"], 3.0)
    experimental = "--experimental" in bt_unit or " -E" in bt_unit
    main_conf = read_main_conf(root / "etc/bluetooth/main.conf")
    if bt_active.strip() == "active":
        checks.append(_check("bluetooth service", OK, "bluetooth.service is active"))
    else:
        state = bt_active.strip()
        level = FAIL if state else UNKNOWN
        checks.append(_check("bluetooth service", level, f"bluetooth.service is {state or 'unknown'}"))

    adapters = read_adapters(sys_root)
    code, show_out = await run(["bluetoothctl", "show"], 5.0)
    show = parse_key_values(show_out) if code == 0 else {}
    if not adapters:
        checks.append(_check("adapter", FAIL, "the kernel reports no Bluetooth controller"))
    elif not show:
        checks.append(_check("adapter", UNKNOWN, "bluetoothctl could not describe the adapter"))
    elif show.get("Powered") != "yes":
        checks.append(_check("adapter", FAIL, "Bluetooth is turned off"))
    else:
        first = adapters[0]
        checks.append(_check("adapter", OK, f"{first['name']} is powered ({first['driver'] or 'driver unknown'})"))
    if show:
        roles = [line.split(":", 1)[1].strip() for line in show_out.splitlines() if line.strip().startswith("Roles:")]
        if roles and "peripheral" not in roles:
            checks.append(_check("peripheral role", FAIL, f"the adapter does not offer the peripheral role ({roles})"))
        elif roles:
            checks.append(_check("peripheral role", OK, "the adapter can act as a peripheral"))
        instances = show.get("SupportedInstances", "")
        if instances:
            count = int(instances.split()[0], 16) if instances.split()[0].startswith("0x") else 0
            status = OK if count > 0 else FAIL
            checks.append(_check("advertising", status, f"{count} advertising instance(s) supported"))
    _, paired_out = await run(["bluetoothctl", "devices", "Paired"], 5.0)
    report["bluetooth"] = {
        "bluez": bluez_version,
        "service_active": bt_active.strip(),
        "experimental": experimental,
        "main_conf": main_conf,
        "adapters": adapters,
        "show": show,
        "paired_devices": [line.strip() for line in paired_out.splitlines() if line.strip().startswith("Device ")],
    }

    # ---------------------------------------------------------------- our service
    _, active = await run(["systemctl", "is-active", SERVICE_UNIT], 3.0)
    _, enabled = await run(["systemctl", "is-enabled", SERVICE_UNIT], 3.0)
    status: dict[str, Any] | None = None
    problem = ""
    try:
        caller = call or (lambda method: call_sync(paths.socket_path(), method, timeout=5.0))
        status = await asyncio.to_thread(caller, "status")
    except RpcError as exc:
        problem = exc.message
    except Exception as exc:  # noqa: BLE001 - a diagnostic tool reports, it does not crash
        problem = f"{type(exc).__name__}: {exc}"
    report["service"] = {"active": active.strip(), "enabled": enabled.strip(), "status": status, "problem": problem}
    if status is None:
        checks.append(_check("service", FAIL, f"not reachable: {problem}"))
    else:
        checks.append(_check("service", OK, f"running, version {status.get('version', '?')}"))
        bt = status.get("bluetooth", {})
        if not bt.get("registered"):
            checks.append(_check("HID service", FAIL, bt.get("error") or "not registered with BlueZ"))
        else:
            checks.append(_check("HID service", OK, "registered with BlueZ"))
        tv = bt.get("tv_name") or bt.get("tv_address")
        if not bt.get("paired"):
            checks.append(_check("TV pairing", WARN, "no TV is paired yet (run: tcl-me-remo pair)"))
        elif bt.get("ready"):
            via = "direct delivery" if bt.get("delivery") == "raw" else "BlueZ"
            checks.append(_check("TV link", OK, f"{tv} is connected ({via})"))
        else:
            reason = bt.get("error") or "the link comes up when it is needed"
            checks.append(_check("TV link", WARN, f"{tv} is not connected: {reason}"))
        gate = status.get("display", {})
        checks.append(_check("display check", OK if gate.get("gate_ok") else WARN, gate.get("gate_reason", "")))
        for item in status.get("config_problems", []):
            checks.append(_check("config", WARN, item))
        last = status.get("last_action")
        if last and not last.get("ok"):
            what = f"{last.get('name')} ({last.get('trigger')})"
            checks.append(_check("last action", WARN, f"{what} failed: {last.get('detail')}"))

    # ---------------------------------------------------------------- suspend plumbing
    code, inhibit_out = await run(["systemd-inhibit", "--list", "--no-pager", "--no-legend"], 5.0)
    holds = [line for line in inhibit_out.splitlines() if "tcl-me-remo" in line]
    report["inhibitors"] = holds
    if code != 0:
        checks.append(_check("suspend lock", UNKNOWN, "systemd-inhibit is not available"))
    elif status is not None and not holds:
        checks.append(_check("suspend lock", FAIL, "the service holds no suspend/shutdown delay lock"))
    elif holds:
        checks.append(_check("suspend lock", OK, f"{len(holds)} delay lock(s) held"))

    report["display"] = display.read_display_state(sys_root).to_dict()
    report["checks"] = checks
    return report


def render(report: dict[str, Any]) -> str:
    """Human-readable form of :func:`collect`'s result."""
    sysinfo = report["system"]
    bt = report["bluetooth"]
    lines = [
        f"tcl-me-remo {report['tcl_me_remo']} on {sysinfo['os']} {sysinfo['os_version']}".rstrip(),
        f"kernel {sysinfo['kernel']}, Python {sysinfo['python']}, {sysinfo['systemd'] or 'systemd unknown'}",
        f"BlueZ {bt['bluez'] or 'unknown'}"
        + (" (experimental features on)" if bt["experimental"] else "")
        + "".join(f", {a['name']} {a['driver']} {a['id']}".rstrip() for a in bt["adapters"]),
        "",
    ]
    for check in report["checks"]:
        lines.append(f"{_MARK[check['status']]} {check['name']}: {check['detail']}")
    if bt["main_conf"]:
        lines += ["", "BlueZ main.conf settings in effect:"]
        lines += [f"  {key} = {value}" for key, value in sorted(bt["main_conf"].items())]
    if bt["paired_devices"]:
        lines += ["", "Paired Bluetooth devices:"] + [f"  {line}" for line in bt["paired_devices"]]
    connectors = [c["label"] for c in report["display"]["connectors"] if c["connected"]]
    lines += ["", "Connected displays: " + (", ".join(connectors) or "none detected")]
    if not sysinfo["is_root"]:
        lines += ["", "Tip: run with sudo for a complete report (some Bluetooth details need root)."]
    return "\n".join(lines)


def worst(report: dict[str, Any]) -> str:
    statuses = {check["status"] for check in report["checks"]}
    for level in (FAIL, WARN, UNKNOWN):
        if level in statuses:
            return level
    return OK


def main(as_json: bool = False, out: Any = None) -> int:
    out = out if out is not None else sys.stdout
    report = asyncio.run(collect())
    print(json.dumps(report, indent=2) if as_json else render(report), file=out)
    return 1 if worst(report) == FAIL else 0
