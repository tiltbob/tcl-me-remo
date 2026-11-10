# Control protocol

The service listens on a Unix stream socket (default `/run/tcl-me-remo/control.sock`,
owner `root`, group `wheel`, mode `0660`). The CLI and the Decky plugin are both plain
clients of it, so anything they do can be scripted.

One JSON object per line in each direction:

```
-> {"id": 1, "method": "status", "params": {}}
<- {"id": 1, "ok": true, "result": {...}}
<- {"id": 1, "ok": false, "error": {"code": "busy", "message": "busy with 'tv_on' (boot); try again shortly"}}
```

`id` is echoed back untouched. `params` may be omitted. A connection can carry any number of
requests, answered in order.

From a shell:

```sh
echo '{"id":1,"method":"status"}' | sudo socat - UNIX-CONNECT:/run/tcl-me-remo/control.sock
```

## Methods

| Method | Params | Result |
|---|---|---|
| `status` | | [Status](#status) |
| `get_config` | | `{config, schema, presets, keys, link_presets}` |
| `set_config` | `{patch}` nested object of changes, e.g. `{"input": {"hdmi_port": 2}}` | `{config}` |
| `tv_on` | `{switch_input?: bool}` (default `true`) | [Action record](#action-record) |
| `tv_off` | | Action record |
| `switch_input` | `{via?}` `macro` or `wifi`: try exactly that method, whatever `input.method` is, with no fallback | Action record |
| `send_key` | `{key, hold?}` key name or raw usage; `hold` in seconds | `{key}` |
| `run_macro` | `{macro}` macro text; `{port}` placeholders allowed | Action record |
| `check_macro` | `{macro, port?}` port defaults to the configured HDMI port | `{ok, normalized, rendered, seconds}` or `{ok: false, error}` |
| `event` | `{name, simulate?, budget?, wait?}` name is `boot`, `resume`, `suspend`, `shutdown` or `reboot` | Action record, or `{queued, name}` with `wait: false` |
| `pair_start` | `{timeout?}` 10..600 seconds, default 120 | Bluetooth state |
| `pair_cancel` | | Bluetooth state |
| `forget_tv` | | Bluetooth state |
| `connect` | | `{ready, ...Bluetooth state}` |
| `disconnect` | | Bluetooth state |
| `wifi_discover` | | `{devices: [{name, host, port}]}` |
| `wifi_pair_start` | `{host}` | Wi-Fi status |
| `wifi_pair_finish` | `{code, name?}` the code shown on the TV | Wi-Fi status |
| `wifi_pair_cancel` | | Wi-Fi status |
| `wifi_test` | `{host?}` defaults to the configured TV | `{reachable, on, name}`; changes nothing on the TV |
| `wifi_unpair` | | Wi-Fi status |
| `logs` | `{lines?}` 1..1000, default 100 | `{lines: [string]}` |

`event` with `simulate: true` runs exactly what the real event would run, without touching
the bookkeeping that de-duplicates real events. It is what "test wake" / "test sleep" in the
UI use.

## Error codes

| Code | Meaning |
|---|---|
| `bad_request` | Malformed request or parameter |
| `unknown_method` | No such method |
| `busy` | Another action is running; manual requests are not queued |
| `invalid_config` | A setting was rejected; the message names it (`input.hdmi_port: must be at most 4`) |
| `save_failed` | The setting is in effect but could not be written to disk |
| `bad_key`, `bad_macro` | Unknown key name / invalid macro text |
| `not_configured` | The requested action has nothing configured (no macro, input switching off) |
| `tv_unreachable` | No transport could reach the TV |
| `not_paired`, `no_adapter`, `bluetooth_off`, `not_connected`, `not_subscribed`, `bluetooth_error` | Bluetooth problems |
| `wifi_unavailable`, `wifi_not_configured`, `wifi_not_paired`, `wifi_unreachable`, `wifi_error`, `pairing_failed` | Wi-Fi assist problems |
| `timeout`, `unavailable`, `bad_reply` | Raised by the client library when the service does not answer |
| `internal` | A bug in the service; details are in the log |

Actions (`tv_on`, `tv_off`, `switch_input`, `run_macro`, `event`) do not fail the request
when the TV cannot be reached: they return an action record with `ok: false`, so the caller
always gets the trace of what was tried.

## Action record

```json
{
  "name": "tv_on",
  "trigger": "resume",
  "ok": true,
  "skipped": false,
  "detail": "TV on (Bluetooth), input switched (Bluetooth macro)",
  "steps": [
    "display check: HDMI-A-1 (TCL TCL TV) is connected",
    "Bluetooth: TV connected after 2.3s",
    "Bluetooth: sent WAKE",
    "waiting 8s for the TV to start up",
    "Bluetooth: playing macro: F3 1.5s LEFT*8 RIGHT*2 OK"
  ],
  "time": 1794300000.0,
  "seconds": 14.2
}
```

`trigger` is the lifecycle event that caused the action, or `manual`. `skipped` means the
action decided there was nothing to do (disabled, display gate closed, duplicate event) and
`detail` says why; a skipped action has `ok: true`.

## Status

```json
{
  "version": "0.1.0",
  "uptime": 512.3,
  "enabled": true,
  "phase": "running",
  "busy": "",
  "bluetooth": {
    "available": true, "adapter": "hci0", "adapter_address": "…", "powered": true,
    "registered": true, "advertising": false,
    "pairing": false, "pairing_seconds_left": 0,
    "paired": true, "tv_address": "…", "tv_name": "Living Room TV",
    "connected": true, "ready": true, "subscribed": true, "delivery": "bluez",
    "error": ""
  },
  "wifi": {"available": true, "enabled": false, "host": "", "name": "", "paired": false, "pairing": false, "pairing_host": ""},
  "display": {
    "gate_ok": true, "gate_reason": "HDMI-A-1 (TCL TCL TV) is connected", "has_internal": false,
    "connectors": [{"name": "HDMI-A-1", "card": "card0", "status": "connected", "internal": false, "maker": "TCL", "model": "TCL TV", "connected": true, "label": "HDMI-A-1 (TCL TCL TV)"}]
  },
  "input": {"method": "macro", "hdmi_port": 2, "wifi_link": "", "macro": "F3 1.5s LEFT*8 RIGHT*2 OK", "macro_error": "", "macro_seconds": 6.2},
  "last_action": { "…": "action record" },
  "history": [],
  "config_problems": []
}
```

- `phase` is `running`, `going_down` (a suspend/shutdown is being handled) or `down`.
- `busy` names the running action (`"tv_on (boot)"`) or is empty.
- `bluetooth.ready` means keys can be sent right now. `delivery` says how they travel:
  `bluez` (normal GATT notifications) or `raw` (see [how-it-works](how-it-works.md)).
- `input.macro` is the macro that will run, with the preset resolved and `{port}` filled in.

## `get_config`

- `config` is the full settings document (the same JSON as the config file).
- `schema` mirrors its shape; every leaf is `{type, default, help, min?, max?, maxlen?, choices?}`.
- `presets` lists the built-in input macros: `{name, title, description, template}`.
- `keys` lists the named keys: `{name, label, page, usage, android}`.
- `link_presets` lists the built-in TV-input links for the Wi-Fi assist:
  `{name, title, description, links}` where `links` maps the HDMI port number to an app link.
