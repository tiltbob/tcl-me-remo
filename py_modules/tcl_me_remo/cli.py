"""Command line interface: ``tcl-me-remo <command>``.

Everything except ``daemon`` is a thin client of the running service's control socket, so
the CLI, the Decky panel and the systemd sleep hook all drive the exact same code paths.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any, Callable

from . import __version__, keys, paths
from .ipc import RpcError, ServiceUnavailable, call_sync

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3

Caller = Callable[..., Any]


def _default_caller(method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> Any:
    return call_sync(paths.socket_path(), method, params, timeout=timeout)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tcl-me-remo",
        description="Control a TCL Google TV from a SteamOS machine acting as a Bluetooth remote.",
    )
    parser.add_argument("--version", action="version", version=f"tcl-me-remo {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    sub.add_parser("daemon", help="run the background service (started by systemd)")

    p = sub.add_parser("status", help="show what the service is doing")
    p.add_argument("--json", action="store_true", help="print the raw status as JSON")

    p = sub.add_parser("on", help="turn the TV on and switch to this machine's input")
    p.add_argument("--no-input", action="store_true", help="only turn the TV on")
    sub.add_parser("off", help="turn the TV off")
    sub.add_parser("input", help="switch the TV to this machine's input")

    p = sub.add_parser("key", help="send one remote key, e.g. HOME, DOWN, OK, C:0223")
    p.add_argument("key")
    p.add_argument("--hold", type=float, metavar="SECONDS", help="hold the key down (long press)")

    p = sub.add_parser("macro", help="play a key macro, e.g. 'HOME 1s DOWN*2 OK'")
    p.add_argument("macro", nargs="+")

    sub.add_parser("keys", help="list the key names a macro can use")

    p = sub.add_parser("pair", help="open Bluetooth pairing mode and wait for the TV")
    p.add_argument("--timeout", type=int, default=120, metavar="SECONDS")
    sub.add_parser("forget", help="remove the Bluetooth bond with the TV")
    sub.add_parser("connect", help="ask the TV to connect now (without sending a key)")
    sub.add_parser("disconnect", help="drop the Bluetooth link to the TV")

    p = sub.add_parser("config", help="show settings, or change one: config input.hdmi_port 2")
    p.add_argument("name", nargs="?", help="dotted setting name, e.g. input.hdmi_port")
    p.add_argument("value", nargs="?", help="new value (true/false, a number, or text)")

    p = sub.add_parser("event", help="tell the service about a lifecycle event (used by systemd hooks)")
    p.add_argument("name", choices=["boot", "resume", "suspend", "shutdown", "reboot"])
    p.add_argument("--simulate", action="store_true", help="run the actions without affecting event bookkeeping")
    p.add_argument("--budget", type=float, metavar="SECONDS", help="time limit for suspend/shutdown actions")

    p = sub.add_parser("wifi", help="set up the optional Wi-Fi assist")
    wsub = p.add_subparsers(dest="wifi_command", metavar="action")
    wsub.add_parser("discover", help="look for Google TVs on the network")
    wp = wsub.add_parser("pair", help="pair with the TV at HOST (it shows a code to type in)")
    wp.add_argument("host")
    wp.add_argument("--code", help="the code shown on the TV (otherwise you are asked)")
    wsub.add_parser("unpair", help="forget the Wi-Fi pairing")

    p = sub.add_parser("logs", help="show recent service log lines")
    p.add_argument("-n", "--lines", type=int, default=50)
    return parser


def _parse_value(text: str) -> Any:
    """``true``/``2``/``1.5`` become JSON values; anything else stays text."""
    try:
        value = json.loads(text)
    except ValueError:
        return text
    return value if isinstance(value, (bool, int, float)) else text


def _lookup(tree: Any, dotted: str) -> Any:
    node = tree
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise RpcError("invalid_config", f"{dotted}: unknown setting")
        node = node[part]
    return node


def _nest(dotted: str, value: Any) -> dict[str, Any]:
    patch: dict[str, Any] = {}
    cursor = patch
    parts = dotted.split(".")
    for part in parts[:-1]:
        cursor[part] = {}
        cursor = cursor[part]
    cursor[parts[-1]] = value
    return patch


def _flatten(tree: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []
    for key, value in tree.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            rows.extend(_flatten(value, name + "."))
        else:
            rows.append((name, value))
    return rows


def _print_record(record: dict[str, Any], out: Any) -> int:
    """Print an action record; the exit code reflects whether the action worked."""
    for step in record.get("steps", []):
        print(f"  {step}", file=out)
    if record.get("skipped"):
        print(f"skipped: {record.get('detail', '')}", file=out)
        return EXIT_OK
    if record.get("ok"):
        print(f"ok: {record.get('detail', '')}", file=out)
        return EXIT_OK
    print(f"failed: {record.get('detail', '')}", file=out)
    return EXIT_FAILED


def _yes_no(flag: Any) -> str:
    return "yes" if flag else "no"


def _print_status(status: dict[str, Any], out: Any) -> None:
    bt = status.get("bluetooth", {})
    wifi = status.get("wifi", {})
    disp = status.get("display", {})
    inp = status.get("input", {})
    automatic = "on" if status.get("enabled") else "OFF"
    print(f"tcl-me-remo {status.get('version', '?')}   automatic actions: {automatic}", file=out)
    if status.get("busy"):
        print(f"busy:       {status['busy']}", file=out)

    if not bt.get("available"):
        link = "no Bluetooth adapter"
    elif not bt.get("powered"):
        link = "Bluetooth is turned off"
    elif not bt.get("paired"):
        link = "no TV paired (run: tcl-me-remo pair)"
    elif bt.get("ready"):
        link = "connected"
    elif bt.get("connected"):
        link = "connected, waiting for the TV to subscribe"
    elif bt.get("advertising"):
        link = "calling the TV..."
    else:
        link = "idle (connects when needed)"
    tv = bt.get("tv_name") or bt.get("tv_address") or "-"
    print(f"bluetooth:  {link}   tv: {tv}   adapter: {bt.get('adapter') or '-'}", file=out)
    if bt.get("pairing"):
        print(f"            pairing mode is open for {bt.get('pairing_seconds_left', 0)}s more", file=out)
    if bt.get("error"):
        print(f"            problem: {bt['error']}", file=out)

    if wifi.get("enabled"):
        print(f"wi-fi:      {wifi.get('host') or '-'}   paired: {_yes_no(wifi.get('paired'))}", file=out)
    else:
        print("wi-fi:      assist off", file=out)

    print(f"display:    {'ok' if disp.get('gate_ok') else 'BLOCKED'} - {disp.get('gate_reason', '')}", file=out)
    macro_text = inp.get("macro") or "(none)"
    print(f"input:      method {inp.get('method')}   HDMI {inp.get('hdmi_port')}   macro: {macro_text}", file=out)
    if inp.get("macro_error"):
        print(f"            macro problem: {inp['macro_error']}", file=out)
    for problem in status.get("config_problems", []):
        print(f"config:     {problem}", file=out)

    last = status.get("last_action")
    if last:
        when = time.strftime("%H:%M:%S", time.localtime(last.get("time", 0)))
        outcome = "skipped" if last.get("skipped") else "ok" if last.get("ok") else "FAILED"
        what = f"{last.get('name')} ({last.get('trigger')})"
        print(f"last:       {when} {what} {outcome}: {last.get('detail', '')}", file=out)


def _cmd_pair(args: argparse.Namespace, call: Caller, out: Any) -> int:
    call("pair_start", {"timeout": args.timeout})
    print("Pairing mode is open.", file=out)
    print("On the TV: Settings > Remotes & Accessories > Pair accessory, then pick this machine.", file=out)
    deadline = time.monotonic() + args.timeout + 5
    try:
        while time.monotonic() < deadline:
            time.sleep(1.0)
            bt = call("status")["bluetooth"]
            if bt.get("paired") and not bt.get("pairing"):
                print(f"Paired with {bt.get('tv_name') or bt.get('tv_address')}.", file=out)
                return EXIT_OK
            if not bt.get("pairing"):
                break
    except KeyboardInterrupt:
        call("pair_cancel")
        print("Pairing cancelled.", file=out)
        return EXIT_FAILED
    print("No TV paired before pairing mode closed.", file=out)
    return EXIT_FAILED


def _cmd_config(args: argparse.Namespace, call: Caller, out: Any) -> int:
    if args.name is None:
        for name, value in _flatten(call("get_config")["config"]):
            print(f"{name} = {json.dumps(value)}", file=out)
        return EXIT_OK
    if args.value is None:
        value = _lookup(call("get_config")["config"], args.name)
        if isinstance(value, dict):
            for name, leaf in _flatten(value, args.name + "."):
                print(f"{name} = {json.dumps(leaf)}", file=out)
        else:
            print(json.dumps(value), file=out)
        return EXIT_OK
    current = _lookup(call("get_config")["config"], args.name)
    # Text settings take the argument verbatim, so a TV called "1234" or a macro "5s" is fine.
    value = args.value if isinstance(current, str) else _parse_value(args.value)
    new = call("set_config", {"patch": _nest(args.name, value)})["config"]
    print(f"{args.name} = {json.dumps(_lookup(new, args.name))}", file=out)
    return EXIT_OK


def _cmd_wifi(args: argparse.Namespace, call: Caller, out: Any, ask: Callable[[str], str]) -> int:
    if args.wifi_command == "discover":
        devices = call("wifi_discover", timeout=15.0)["devices"]
        if not devices:
            print("No Google TV found. You can still pair by IP address: tcl-me-remo wifi pair <ip>", file=out)
            return EXIT_FAILED
        for device in devices:
            print(f"{device.get('host')}\t{device.get('name', '')}", file=out)
        return EXIT_OK
    if args.wifi_command == "pair":
        call("wifi_pair_start", {"host": args.host}, timeout=20.0)
        code = args.code or ask("Code shown on the TV: ")
        try:
            call("wifi_pair_finish", {"code": code.strip()}, timeout=20.0)
        except RpcError:
            call("wifi_pair_cancel")
            raise
        print(f"Wi-Fi assist paired with {args.host} and enabled.", file=out)
        return EXIT_OK
    if args.wifi_command == "unpair":
        call("wifi_unpair")
        print("Wi-Fi assist unpaired and disabled.", file=out)
        return EXIT_OK
    print("usage: tcl-me-remo wifi {discover,pair,unpair}", file=out)
    return EXIT_USAGE


def run(
    argv: list[str] | None = None,
    *,
    call: Caller = _default_caller,
    out: Any = None,
    err: Any = None,
    ask: Callable[[str], str] = input,
) -> int:
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help(out)
        return EXIT_USAGE
    if args.command == "daemon":
        from . import daemon  # imported late: the client commands need no D-Bus machinery

        return daemon.main()
    if args.command == "keys":
        for row in keys.catalog():
            print(f"{row['name']:<13} {row['label']:<22} {row['android']}", file=out)
        print("\nRaw usages also work: K:52 (keyboard page), C:0223 (consumer page), S:82 (system control).", file=out)
        return EXIT_OK

    try:
        if args.command == "status":
            status = call("status")
            if args.json:
                print(json.dumps(status, indent=2), file=out)
            else:
                _print_status(status, out)
            return EXIT_OK
        if args.command == "on":
            return _print_record(call("tv_on", {"switch_input": not args.no_input}, timeout=180.0), out)
        if args.command == "off":
            return _print_record(call("tv_off", timeout=40.0), out)
        if args.command == "input":
            return _print_record(call("switch_input", timeout=180.0), out)
        if args.command == "key":
            params: dict[str, Any] = {"key": args.key}
            if args.hold is not None:
                params["hold"] = args.hold
            print(f"sent {call('send_key', params, timeout=30.0)['key']}", file=out)
            return EXIT_OK
        if args.command == "macro":
            return _print_record(call("run_macro", {"macro": " ".join(args.macro)}, timeout=180.0), out)
        if args.command == "pair":
            return _cmd_pair(args, call, out)
        if args.command == "forget":
            call("forget_tv")
            print("The TV has been forgotten. Also remove this machine from the TV's accessory list.", file=out)
            return EXIT_OK
        if args.command == "connect":
            result = call("connect", timeout=150.0)
            print("connected" if result.get("ready") else "the TV did not connect", file=out)
            return EXIT_OK if result.get("ready") else EXIT_FAILED
        if args.command == "disconnect":
            call("disconnect")
            print("disconnected", file=out)
            return EXIT_OK
        if args.command == "config":
            return _cmd_config(args, call, out)
        if args.command == "event":
            params = {"name": args.name, "simulate": args.simulate}
            if args.budget is not None:
                params["budget"] = args.budget
            # Up events can legitimately take minutes (connect, wait, macro).
            timeout = (args.budget + 3.0) if args.budget is not None else 240.0
            return _print_record(call("event", params, timeout=timeout), out)
        if args.command == "wifi":
            return _cmd_wifi(args, call, out, ask)
        if args.command == "logs":
            for line in call("logs", {"lines": max(1, min(1000, args.lines))})["lines"]:
                print(line, file=out)
            return EXIT_OK
    except ServiceUnavailable as exc:
        print(f"tcl-me-remo: {exc.message}", file=err)
        print("Is the service running?  Check: systemctl status tcl-me-remo", file=err)
        return EXIT_UNAVAILABLE
    except RpcError as exc:
        print(f"tcl-me-remo: {exc.message}", file=err)
        return EXIT_FAILED
    parser.print_help(out)
    return EXIT_USAGE


def main() -> int:
    return run()
