# How it works

```
            SteamOS machine                                        TCL Google TV
 ┌──────────────────────────────────────┐
 │ Decky panel ─┐                       │
 │ tcl-me-remo ─┼─ control socket ─┐    │
 │ sleep hook  ─┘                  ▼    │
 │   logind ──────────────►  service    │   Bluetooth LE (HID over GATT)
 │   (suspend / shutdown)    (root)  ───┼──────────────────────────────────►  "a remote"
 │                              │       │   Wi-Fi (Google TV remote protocol)
 │                              └───────┼─ ─ ─ ─ ─ ─ ─ opt-in ─ ─ ─ ─ ─ ─ ─ ►  port 6466
 └──────────────────────────────────────┘
```

One root service does everything. The CLI, the Decky panel and the systemd sleep hook are
clients of its control socket ([protocol](control-protocol.md)).

## The machine is a remote control

To the TV, the SteamOS machine is a Bluetooth LE remote: a HID-over-GATT peripheral, paired
once, like the remote that came in the box. The service registers one GATT service (HID,
0x1812) with BlueZ over D-Bus. BlueZ and the kernel do the radio work; nothing about the
machine's normal Bluetooth use (controllers, headsets) changes, the adapter is not renamed,
and BlueZ's configuration is not edited.

The report map follows Google's reference Android TV remote: a consumer-control collection
for navigation and media keys, a keyboard collection without letters (a device with a Q key
is treated as a full keyboard, which hides the on-screen keyboard for the real remote), and a
system-control collection, which exists for one reason: "System Wake Up" is the only standard
usage that Linux, and therefore Android, turns into a wake key.

### Discrete power

| | HID usage | Android key | Behaviour |
|---|---|---|---|
| `WAKE` | Generic Desktop 0x83 | `KEYCODE_WAKEUP` | wakes; does nothing if already awake |
| `SLEEP` | Consumer 0x32 | `KEYCODE_SLEEP` | sleeps on key-up; does nothing if already asleep |
| `POWER` | Generic Desktop 0x81 | `KEYCODE_POWER` | toggle; avoided |

Discrete keys matter because the service cannot see the TV's state. A toggle sent to a TV
that is already off would turn it on. `SLEEP` uses the consumer usage because Android 16
remaps the Generic Desktop sleep usage to "lock".

The mapping holds while the TV applies its generic key layout to us, which it does because
our Bluetooth identity (BlueZ's own vendor and product id) matches no vendor layout.
Pretending to be the TCL remote would switch the TV to TCL's layout, which has no discrete
power keys.

### The table is frozen

A TV stores the attribute handles and report map of a bonded remote and, before Android 14,
never reads them again. The GATT table is therefore pinned to fixed handles and must not
change between versions. A change would not produce an error; keys would just stop arriving
until the TV is paired again.

## Lifecycle events

| Event | Action |
|---|---|
| boot, resume | connect, send `WAKE`, wait, switch input (if configured) |
| suspend, shutdown | send `SLEEP` (reconnecting first if needed), then let the machine go |
| reboot | nothing by default (`events.reboot_tv_off`) |

Events reach the service by three independent routes, and it acts once per real event:

1. **logind delay locks.** The service holds a "sleep" and a "shutdown" inhibitor in delay
   mode. logind announces the transition, waits (5 s at most by default) for the lock to be
   released, then proceeds. This is the normal route; Steam's power menu goes through logind.
2. **systemd units.** `tcl-me-remo-sleep.service` is hooked to `sleep.target` and reports
   suspend and resume. For shutdown the service is ordered after `bluetooth.service`, so
   systemd stops it before Bluetooth; on being stopped it checks whether the system is going
   down and whether that is a power-off or a reboot.
3. **A clock watchdog.** `CLOCK_BOOTTIME` advances during suspend and `CLOCK_MONOTONIC` does
   not; a growing gap proves a resume even if nothing announced the suspend.

The time before sleep is short. The service spends at most `InhibitDelayMaxSec` minus 1.5 s
(3.5 s by default). If the Bluetooth link is up, `SLEEP` goes out immediately. If it is
down, the service calls the TV and, when the Wi-Fi assist is set up, tries that at the same
time; the first to succeed wins. The kernel drops every Bluetooth link as part of suspend,
so the lock is held a little longer than the key itself.

A handheld that wakes on the sofa must not turn the TV on. The **display check** looks at the
kernel's display connectors: with a built-in panel (eDP, DSI, LVDS), TV actions run only
while an external display is connected, optionally only one whose EDID name matches. A
machine without a built-in panel is assumed to be on the TV.

## The link

The service keeps calling the TV (connectable advertising) whenever a TV is bonded and not
connected, and stops as soon as it connects. A bonded Android TV keeps a background
connection request for its remotes and connects when it hears one, so the link is normally
up whenever both sides are awake. That is what lets `SLEEP` go out instantly at suspend.

If your TV wakes up because of this, turn `bluetooth.reconnect_on_drop` off; the service
then only calls the TV when it has something to send.

Readiness cannot be observed: BlueZ reports that the TV connected, not that the link is
encrypted or that the TV's HID driver is listening. The service therefore waits
`bluetooth.settle_ms` after a connection before the first key.

### After a reboot

BlueZ keeps the record of which characteristics a peer subscribed to only in memory. It
survives disconnects and suspend, but not a restart of `bluetoothd`, and so not a reboot.
A notification for a peer without a subscription record is dropped silently.

- **Android 14 and newer** notice that the service set may have changed (BlueZ sends a
  Service Changed indication), rediscover and subscribe again. The service waits up to
  `bluetooth.resubscribe_wait` for that.
- **Android 11 to 13** load their stored description of the remote and never write the
  subscription again. They are listening; BlueZ will not talk.

For the second case the service has a **direct delivery** path (`bluetooth.raw_notify`):
it writes the notification itself through a raw HCI socket, addressed to the connection and
attribute handle the TV already knows. The controller encrypts it like any other packet.
One detail makes this work: the Service Changed indication makes those Android versions
drop their notification registrations for the rest of that connection, so the first time
after each start the service disconnects once and lets the TV reconnect before using direct
delivery. `tcl-me-remo status` shows `delivery: raw` when this path is in use. If the TV
subscribes later after all, delivery switches back at once.

This path was designed from kernel and Android source and has not been confirmed on a TV.
Without it, an Android 11–13 TV could only be controlled until the next reboot of the
SteamOS machine. The alternatives are a patched `bluetoothd` or a dedicated adapter with
its own Bluetooth stack.

### Pairing

Pairing mode is explicit and time-limited. While it is open the service registers a pairing
agent and asks BlueZ to make it the default (Steam registers its own agent, which would
otherwise receive the request), advertises as discoverable with the appearance of a
keyboard, and accepts exactly one new peer. Outside pairing mode no agent of ours is
registered and the advertisement is not discoverable, so the machine does not show up in
anyone's Bluetooth list.

Discoverability is put on the advertisement only. Making the adapter discoverable would turn
on classic Bluetooth inquiry, and a TV that finds the machine that way pairs over classic
Bluetooth, finds no HID service and gives up.

### Wake beacon

A TV whose system is suspended does not accept connections. MediaTek-based sets arm a filter
in the Bluetooth controller before suspending and wake when an advertisement with matching
manufacturer data arrives, which is what their own remote sends when Power is pressed.

`bluetooth.wake_beacon` (off by default, experimental) makes the service broadcast such an
advertisement while it is trying to turn the TV on: `mediatek` sends MediaTek's default
pattern built from the TV's address, `custom` sends the bytes in `bluetooth.wake_beacon_data`,
for a pattern captured from your own remote (see the hardware test plan). TCL's actual
pattern is not public; treat this as an experiment.

## Getting to the right input

1. **Let the TV do it.** With "Power On Behavior: Last Input Used" the TV returns to the
   input it showed last. This needs no key presses and is the recommended set-up.
2. **A key macro over Bluetooth.** Android has no remote key for "HDMI 2" that a generic
   remote can send. A macro opens TCL's source list and walks to the entry. The list does not
   wrap around, so a macro can run to one end and count back, which makes it independent of
   where focus started. Presets cover the reported layouts; `F3` is reported to open the
   list on one TCL model. Input switching is off until you have watched a macro work.
3. **The Wi-Fi assist** asks the TV to open the input directly by its internal id.

## Wi-Fi assist

Opt-in. It speaks the protocol the Google TV phone app uses (pairing on TCP 6467 with a code
shown on the TV, commands on 6466), implemented with the standard library and the `openssl`
tool. It is used for input switching and, when Bluetooth cannot reach the TV, as a fallback
for power. TCL ignores the direct HDMI key codes, so by default the assist opens the TV-input
link; `tvdata.py` holds the link sets owners have reported.

It only works while the TV's network is up: on, or in standby with Screenless service.

## Files

| Path | Contents |
|---|---|
| `/opt/tcl-me-remo/` | the code (root-owned) and `bin/tcl-me-remo` |
| `/etc/systemd/system/tcl-me-remo.service` | the service |
| `/etc/systemd/system/tcl-me-remo-sleep.service` | the suspend/resume hook |
| `/var/lib/tcl-me-remo/config.json` | settings |
| `/var/lib/tcl-me-remo/` | state: Wi-Fi pairing certificate, what was learned about the TV |
| `/run/tcl-me-remo/control.sock` | control socket (`root:wheel`, `0660`) |
| `/var/lib/bluetooth/` | the Bluetooth bond itself (BlueZ's, not ours) |

The choice of places follows what SteamOS keeps across OS updates: `/opt`, `/var` and systemd
units under `/etc` survive, the rest of `/etc` and all of `/usr` do not. If an update does
remove the units, the plugin puts them back the next time it loads.

## Security notes

- The service runs as root (it needs the system bus, logind locks and a raw HCI socket) and
  runs only code from a root-owned directory.
- The control socket is limited to root and the `wheel` group. Anyone who can use it can
  send keys to the TV and change settings.
- Any device can open a Bluetooth LE link to the machine while it is calling the TV, but the
  HID attributes require an encrypted link and pairing is refused outside pairing mode.
- Signals from logind and BlueZ are accepted only from the bus names' current owners.
- The Wi-Fi assist's client key is stored in `/var/lib/tcl-me-remo/atv` and readable by root
  only.

## What is not confirmed

Until `hardware-results/` says otherwise:

- that a TCL Google TV honours `KEYCODE_SLEEP` / `KEYCODE_WAKEUP` as stock Android does;
- that it reconnects to a bonded remote and wakes while in standby, and in which standby
  modes;
- direct delivery after a reboot on Android 11–13;
- which key opens the source list from a generic remote, and the list's layout;
- the wake beacon pattern on TCL sets.

[The hardware test plan](hardware-testing.md) settles each of these.
