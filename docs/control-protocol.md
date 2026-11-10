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

`id` is echoed back untouched; it may be any JSON value that takes at most 1024 bytes (a
longer one is refused with `bad_request`, and that reply has `id` null). `params` may be
omitted. A connection can carry any number of requests, answered in order.

Every request line gets exactly one reply line, whatever it holds. The limits:

- A line is at most 1 MiB, in either direction. A longer request is read to its end,
  thrown away and answered with `bad_request` ("message too large"); the connection stays
  usable. A line that has not ended after 8 MiB gets the same answer and the connection is
  closed.
- Numbers must be finite, wherever in the request they stand (the `id` and parameters that
  no method reads included). `NaN` and `Infinity` are refused (`bad_request`), and so is a
  number that is one in effect, such as `1e999`. Every number a method takes has a range,
  given below.
- At most 32 clients are served at once. The next one is sent one line, an error with the
  code `busy` and `id` null, and is disconnected: once its request has arrived, or after a
  second without one.
- A connection that has not delivered a complete request line for 30 seconds is closed.
  The time a request takes to be answered does not count: `tv_on` and `event` can take
  minutes, and the connection waits for them.
- Replies have to be read. A connection whose client has left so much unread that the
  service cannot write any more is closed, with what it did not read, when that has lasted
  30 seconds.

A second service started by hand (`tcl-me-remo daemon`) while one is answering on the socket
exits at once, and leaves the socket to the first.

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
| `send_key` | `{key, hold?}` key name or raw usage; `hold` in seconds, more than 0 and at most 10 | `{key}` |
| `run_macro` | `{macro}` macro text of at most 1024 characters; `{port}` placeholders allowed | Action record |
| `check_macro` | `{macro, port?}` port 1..4, defaults to the configured HDMI port | `{ok, normalized, rendered, seconds}` or `{ok: false, error}` |
| `event` | `{name, simulate?, budget?, wait?}` name is `boot`, `resume`, `suspend`, `shutdown` or `reboot`; `budget` in seconds, more than 0 and at most 120 | Action record, or `{queued, name}` with `wait: false` |
| `pair_start` | `{timeout?}` 10..600 seconds, default 120 | Bluetooth state |
| `pair_cancel` | | Bluetooth state |
| `forget_tv` | | Bluetooth state |
| `connect` | | `{ready, ...Bluetooth state}` |
| `disconnect` | | Bluetooth state |
| `wifi_discover` | | `{devices: [{name, host, port}]}` |
| `wifi_pair_start` | `{host}` | [Wi-Fi pairing state](#wi-fi-pairing-state) |
| `wifi_pair_finish` | `{code, name?}` the code shown on the TV | Wi-Fi pairing state |
| `wifi_pair_cancel` | | Wi-Fi pairing state |
| `wifi_test` | `{host?}` defaults to the configured TV | `{reachable, on, name}`; changes nothing on the TV |
| `wifi_unpair` | | Wi-Fi pairing state |
| `logs` | `{lines?}` 1..1000, default 100 | `{lines: [string]}`, the newest lines; each at most 500 characters, and fewer lines than asked for if they would not fit into one message |

"Bluetooth state" is the `bluetooth` object of the [status](#status).

`set_config` checks every value by itself, and the message of `invalid_config` names the
one it refused. One check takes several settings together: the input macro that would run
must not take longer than 120 seconds with the key timings that are set. It is made when
the patch touches `input.macro`, `input.preset`, `input.hdmi_port`, `bluetooth.key_press_ms`
or `bluetooth.key_gap_ms`, and only then, so that settings which already break it (an older
file) do not get in the way of other changes; the status shows that case in
`input.macro_error`.

`check_macro` answers `ok: false` for everything that would keep the macro from being
played: text that is not a macro, more than 1024 characters, and a running time over 120
seconds with the key timings that are set.

`event` with `simulate: true` runs exactly what the real event would run, without touching
the bookkeeping that de-duplicates real events. It is what "test wake" / "test sleep" in the
UI use, and the form to use by hand (`tcl-me-remo event suspend --simulate`).

Without `simulate`, `event` is for the systemd units: it tells the service that the machine
*is* booting, waking, suspending or going down, and the service believes it. After a
`suspend`, `shutdown` or `reboot` it considers the machine down: further down events are
duplicates and are skipped, until a `resume` arrives, the clocks show that the machine
slept, or two minutes have passed with the service still running and systemd saying that
no shutdown is under way (it then concludes that the machine stayed up; while systemd says
the system is stopping, or gives no answer, the down event stands). A real suspend inside
those two minutes would leave the TV on.

`budget` (seconds, more than 0 and at most 120) limits the attempt to turn the TV off on a
down event; closing the Bluetooth link afterwards takes up to half a second more. Without
it the attempt has 3.5 s.
A second `suspend` or `shutdown` for the same event is normally a duplicate. It makes one
more attempt, within its own `budget`, only when the first could not reach the TV in time
and the Bluetooth link was down by the settings' choice (`bluetooth.reconnect_on_drop` off
or `bluetooth.idle_disconnect_seconds` set); see [how it works](how-it-works.md#lifecycle-events).

`send_key` fails with `busy` when a suspend or shutdown cuts the key short: a key that is
being held is released so that the "TV off" key does not have to wait for it.

## Error codes

A code reaches a client in one of two places: as the `error.code` of a reply ("reply" below),
or as the `error_code` of an [action record](#action-record) ("record"). Actions (`tv_on`,
`tv_off`, `switch_input`, `run_macro`, `event`) do not fail the request when the TV cannot
be reached or nothing is set up: they return a record with `ok: false`, so the caller
always gets the trace of what was tried, and the record's `error_code` says why in a form a
program can test.

| Code | Where | Meaning |
|---|---|---|
| `bad_request` | reply | Malformed request, a parameter that is missing, of the wrong kind or outside its range, a line over 1 MiB |
| `unknown_method` | reply | No such method |
| `busy` | reply | Another action is running; manual requests are not queued. Also: a key was cut short because the machine is suspending or shutting down, and: too many clients are connected |
| `invalid_config` | reply | A setting was rejected; the message names it (`input.hdmi_port: must be at most 4`) |
| `save_failed` | reply | The setting is in effect but could not be written to disk |
| `bad_key` | reply | `send_key`: unknown key name |
| `bad_macro` | record; reply | The macro text is invalid or would run for too long. As a reply only from `run_macro`, for text over 1024 characters, which is not looked at |
| `not_configured` | record | The action has nothing configured (input switching off, no macro, no app link) |
| `tv_unreachable` | record; reply | Turning the TV on or off: no transport reached it (the detail says what each one answered), or the time for it ran out (`ran out of time (3.5s)`). A macro: the TV did not connect in time. As a reply from `send_key` |
| `not_paired`, `no_adapter`, `bluetooth_off`, `not_connected`, `not_subscribed`, `bluetooth_error` | reply; record | Bluetooth problems. In a record when a macro (or switching the input with one) could not start; a failed power action reports `tv_unreachable` |
| `wifi_not_configured`, `wifi_not_paired`, `wifi_unreachable`, `wifi_error` | reply; record | Wi-Fi assist problems. In a record when the input was to be switched over Wi-Fi |
| `wifi_unavailable`, `pairing_failed` | reply | The Wi-Fi assist is not part of this build / pairing with the TV failed |
| `interrupted` | record | A suspend or shutdown (or the service stopping) ended the action before it was done |
| `internal` | reply; record | A bug in the service; details are in the log |

The message of an error is a sentence for a person. What the request held is quoted in it
cut short (a key, a method, a setting's name or value), and no message is longer than 2000
characters.

The client library (`ipc.call`, `ipc.call_sync`) adds three of its own, for the cases in
which there is no reply to pass on: `unavailable` (the socket cannot be reached or the
connection broke), `timeout` (no answer in the time the caller allowed) and `bad_reply`
(the answer could not be read). The service itself never sends `timeout`: an action that
runs out of time fails with `tv_unreachable`. The CLI ends with exit code 3 for
`unavailable`, 1 for every other error and for an action that failed, 2 for a mistake in
its arguments.

## Action record

```json
{
  "name": "tv_on",
  "trigger": "resume",
  "ok": true,
  "skipped": false,
  "detail": "TV on (Bluetooth), input switched (Bluetooth macro)",
  "error_code": "",
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
action decided there was nothing to do (disabled, display gate closed, duplicate event, an
on key that is a toggle and may not be sent automatically) and
`detail` says why; a skipped action has `ok: true`.

`error_code` is empty when `ok` is true, and otherwise one of the codes marked "record" in
[error codes](#error-codes); `detail` is the sentence for people that goes with it.
`detail` has at most 600 characters, a step at most 400, and a record at most 40 steps;
what is longer is cut and ends in `...`.

## Wi-Fi pairing state

What the `wifi_pair_*` methods and `wifi_unpair` return. It is the Wi-Fi assist's own view,
three of the seven fields of the status' `wifi` object:

```json
{"paired": false, "pairing": true, "pairing_host": "192.168.1.50"}
```

`pairing` is true between `wifi_pair_start` and `wifi_pair_finish` or `wifi_pair_cancel`,
while the TV shows its code; `pairing_host` is the TV that is being paired, or empty.

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
    "gate_ok": true, "gate_reason": "HDMI-A-1 (TCL TCL TV) is connected", "has_internal": false, "unreadable": "",
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
  Through `bluez` that normally means the TV has subscribed to all three key reports. The
  exception is a TV that renewed only some of its subscriptions while direct delivery is
  not in use: `ready` stays true, keys of the subscribed reports are sent, and `send_key`
  (and a macro or power action through it) answers `not_subscribed` for a key in one of
  the others; the message ends in "(it renewed its subscription for some keys only, and
  this key is not among them)".
- `bluetooth.error` is one sentence for the user, empty when there is nothing to say: the
  last problem that kept the TV from being reached, or else a note about pairing mode. While
  `pairing` is true it can hold a warning that needs acting on at that moment (the machine
  is also discoverable over classic Bluetooth); after pairing mode closed without a TV it
  says how it ended. It is cleared when the TV becomes ready and when pairing mode opens.
  While `ready` is true it can still hold the sentence of a single key that failed for
  want of its subscription; it goes when the missing subscriptions arrive.
  A request that is refused only because pairing mode is open gets `not_paired` as its
  answer and leaves this field alone.
- `bluetooth.subscribed` is about the paired TV; it is `false` whenever no TV is paired,
  and true from the first of the TV's three per-report subscriptions, so it does not say
  that every key has a way to the TV.
- A key that was being held and had to be let go early is answered with `not_subscribed`
  ("the TV ended a subscription to key reports while HOME was held"; the link is still up)
  or `not_connected` ("the Bluetooth link dropped ...", "... was closed ...", "key delivery
  was changed in the settings while HOME was held"). The release reaches the TV first
  wherever it still can.
- `wifi.available` says whether this build has the Wi-Fi assist; `enabled`, `host` and
  `name` are the settings; `paired`, `pairing` and `pairing_host` are the
  [Wi-Fi pairing state](#wi-fi-pairing-state).
- `display.unreadable` is empty, or says why the display connectors could not be read
  (`/sys/class/drm` is there, but reading it failed). The display check then stays closed
  (`gate_ok` false) unless it is turned off: connectors that could not be read are no
  evidence that the machine has no screen of its own. A machine without `/sys/class/drm`
  has no connectors and an empty `unreadable`.
- `input.macro` is the macro that will run, with the preset resolved and `{port}` filled in.
  `input.macro_error` is empty, or says why it could not be played: it is not a valid
  macro, or it would take longer than 120 seconds with the key timings that are set.
- `history` holds the last 20 action records, oldest first; `last_action` is the newest of
  them, or null.
- `config_problems` lists what was wrong with the settings file when the service started.
  Single values that could not be used (the default is in effect) are dropped from the
  list when the settings are next saved. A file of which nothing could be read is moved
  aside as `config.json.broken` next to it, and the entry that says so stays until the
  service restarts. A later start that finds the kept file and no settings file (nothing
  was changed in between) has an entry that names the kept file again; it is over when the
  file has been mended and moved back, deleted, or new settings have been saved, at the
  start after that.

## `get_config`

- `config` is the full settings document (the same JSON as the config file).
- `schema` mirrors its shape; every leaf is `{type, default, help, min?, max?, maxlen?, choices?}`.
  `type` is `bool`, `int`, `float` or `str`; every number has `min` and `max`. `maxlen`
  counts characters. Some text settings have a form the schema does not describe (a key, a
  macro, an address); a value of the wrong form is refused with `invalid_config`, and the
  message says what is expected. `bluetooth.device_name` may be 26 bytes of UTF-8 at most
  and takes no control characters; `bluetooth.tv_name` and `wifi.name`, which come from the
  TV, are never refused but stored without control characters and cut to 64 characters.
  Clients should offer what `choices` lists rather than a fixed set: `bluetooth.raw_notify`,
  for one, takes `auto`, `on`, `off` and `always`.
  With `auto` on a Linux kernel before 6.17 direct delivery is not used; a TV that needs it
  then fails with `not_subscribed`, and the message names the kernel and the way out.
- `presets` lists the built-in input macros: `{name, title, description, template}`.
- `keys` lists the named keys: `{name, label, page, usage, android}`.
- `link_presets` lists the built-in TV-input links for the Wi-Fi assist:
  `{name, title, description, links}` where `links` maps the HDMI port number to an app link.
