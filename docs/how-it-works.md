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
time; the first to succeed wins. The lock is held a little longer than the key itself: the
key-up must be on the air, and the service then closes the link on its own terms (up to
another half second, see [The link](#the-link)) before it lets the machine go.

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

A link the service closes on purpose (Disconnect in the panel, the idle time-out, the last
step before the machine sleeps) is **parked**: the TV is not called again just because the
link is down, or closing it would be pointless. Parking ends when there is something to send,
and when the machine boots or wakes: at every boot and resume the service lets calling
resume before it decides anything else, so the link comes back even on a wake where nothing
is sent to the TV (handheld without the TV attached, automatic actions off). Otherwise the
next suspend would have to get the TV back within its three seconds.

This means the TV is called, though not sent a key, also on a wake the display check keeps
it out of. A TV that turns on merely because it is called would then turn on for a handheld
that woke on the sofa; whether a TCL set does that has not been observed. If yours does,
`bluetooth.reconnect_on_drop` is the setting to turn off, as above.

With `bluetooth.idle_disconnect_seconds` set, one rule covers every link: a link that has
carried no key for that long is closed and parked, whether it was used before or only came
up because the TV was called (after a boot, a wake, a drop, or pairing). The link is then up
for that long after each boot and wake and whenever something is sent, and down otherwise;
a suspend that finds it down has to get the TV back within its three seconds.

### Before the machine sleeps

How a link ends is visible to the TV as a reason code, and it may matter. If the machine
went to sleep or powered off with the link up, the kernel would end it with reason 0x15,
"remote device terminated the connection due to power off". A real remote sends 0x13,
"remote user terminated the connection". What Android's source says about the two, none of
it observed on a TV:

- **Android 13 and 14.** The Bluetooth HID host re-arms its background connection to a
  remote after a disconnect for a few reasons only; 0x13 is among them, 0x15 is not. A lower
  layer of the same versions puts a device that is still on its background list back after
  any disconnect, so these versions may well reconnect after either code.
- **Android 11 and 12.** The HID host re-arms only after a link that timed out. Going by the
  source, neither code makes it call back. (TVs with these versions do reconnect their own
  remotes, so vendor builds presumably differ.)
- **Android 16** re-arms for any reason.

So 0x13 is nowhere worse than 0x15 and in one place better. Whether the TV connects by
itself after the machine wakes may depend on the code, or may not; the hardware test plan
compares the two.

BlueZ's own disconnect sends 0x13 but only after a fixed two seconds, more than the service
has. So before suspend and shutdown the service asks the kernel directly, through the same
management interface BlueZ uses: the link ends at once, with 0x13. BlueZ learns of it the way
it learns of any disconnect. If that is not possible (the kernel refuses, the link cannot be
identified), the service falls back to asking BlueZ, as before.

This was built from kernel and BlueZ source and has not been confirmed with a radio trace;
that the TV then reconnects after a wake has not been confirmed either.

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
delivery. While the service keeps calling the TV (`bluetooth.reconnect_on_drop`), that
one-off reconnect happens in the background as soon as the TV has connected and stayed
silent, not when the first key is due: before a suspend there would be no time for it.
`tcl-me-remo status --json` shows `"delivery": "raw"` when this path is in use, and
`tcl-me-remo doctor` says "direct delivery" on its TV link line. If the TV subscribes later
after all, delivery switches back at once.

Direct delivery has a side effect on Linux kernels before 6.17. (To be exact: on mainline
kernels up to 6.17-rc3, and on the stable releases before 6.16.5, 6.12.45, 6.6.104, 6.1.150
and 5.15.191, which received the part of the fix that matters. A vendor kernel's version
does not say whether it has it; upstream 6.11.11, which SteamOS 3.7's kernel is built on,
does not.) The kernel counts the packets
it has handed to the controller; packets written past it are not counted on the way out but
are counted when the controller reports them done, so the count ends up short by one transmit
slot per key report once the link closes. Slots lost this way are shared by every Bluetooth
LE device (on some controllers by classic devices too), and in the worst case sending stalls
until Bluetooth is switched off and on. The service therefore repairs the count each time
such a link has closed, by asking the controller for its buffer size again, which makes the
kernel start counting afresh. Linux 6.17 and later (and the patched stable kernels) refuse
to miscount in the first place; there the only trace is a kernel log line per key report,
`sent 0 < count 1`, which is also how such a kernel can be recognised.

The repair, like the path itself, comes from reading source code. Because a stalled
controller or headset would be a worse failure than a TV that did not turn on, and much
harder to trace back to this service, the default is careful:

| `bluetooth.raw_notify` | Direct delivery is used |
|---|---|
| `auto` (default) | when the TV needs it, on Linux 6.17 and later only. On an older kernel it is not used; the error then names the kernel and says that `on` enables it. The rule goes by the version number alone, so on an older kernel that has the fix it is more careful than it needs to be |
| `on` | when the TV needs it, on any kernel, with the repair described above |
| `off` | never |
| `always` | for every key, even when the TV has subscribed (for testing) |

`tcl-me-remo doctor` says in one line what the setting comes to on the running kernel.

This path was designed from kernel and Android source and has not been confirmed on a TV.
Without it, an Android 11–13 TV can only be controlled until the next reboot of the SteamOS
machine and then has to be paired again (or reached through the Wi-Fi assist). The
alternatives are a patched `bluetoothd` or a dedicated adapter with its own Bluetooth stack.

### Pairing

Pairing mode is explicit and time-limited. While it is open the service registers a pairing
agent and asks BlueZ to make it the default (Steam registers its own agent, which would
otherwise receive the request), advertises as discoverable with the appearance of a
keyboard, and accepts exactly one new peer. Outside pairing mode no agent of ours is
registered and the advertisement is not discoverable, so the machine does not show up in
anyone's Bluetooth list.

Discoverability is put on the advertisement only. Making the adapter discoverable would turn
on classic Bluetooth inquiry, and a TV that finds the machine that way pairs over classic
Bluetooth, finds no HID service and gives up. The service never changes that setting, but
other software can: `bluetoothctl discoverable on` does (for three minutes by default), and
so do some Bluetooth settings pages for as long as they are open. GNOME's does, going by its
source; whether the page in SteamOS's Desktop Mode or Steam's own does has not been checked.
While pairing mode is open and the adapter is discoverable, the service says so (in the
pairing dialog, in `status`, in the log), because undoing it is something only the user can
do.

Pairing is complete when a newly bonded TV has also used the HID service (read it, or
subscribed to it). A TV that bonds and stops there (pairing aborted on the TV half-way, the window timing out in between) is
not adopted, and its bond is removed again when the window closes: BlueZ refuses to pair
with a device it still holds a bond for, without asking anybody, so a bond left behind would
block that TV's next attempt. Removing the bond of a TV that is still connected takes BlueZ
about two seconds, so cancelling pairing mode can take that long in this one case.

### Wake beacon

A TV whose system is suspended does not accept connections. MediaTek-based sets arm a filter
in the Bluetooth controller before suspending and wake when an advertisement with matching
manufacturer data arrives, which is what their own remote sends when Power is pressed.

`bluetooth.wake_beacon` (off by default, experimental) makes the service broadcast such an
advertisement while it is trying to turn the TV on: `mediatek` sends MediaTek's default
pattern built from the TV's address, `custom` sends the bytes in `bluetooth.wake_beacon_data`,
for a pattern captured from your own remote (see the hardware test plan). TCL's actual
pattern is not public; treat this as an experiment.

The beacon is a second advertisement next to the one that calls the TV: a broadcast that
cannot be connected to and carries nothing but the pattern. It is on the air only while the
service waits for the TV in order to turn it on, and is taken down as soon as the TV connects
or the wait ends. It is never sent for anything else: waking a suspended TV in order to tell
it to sleep would leave it on.

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
| `/var/lib/tcl-me-remo/` | state: Wi-Fi pairing certificate, what was learned about the TV, the installer's notes (links it made, "the plugin was removed") |
| `/run/tcl-me-remo/control.sock` | control socket (`root:wheel`, `0660`) |
| `/var/lib/bluetooth/` | the Bluetooth bond itself (BlueZ's, not ours) |

The choice of places follows what SteamOS keeps across OS updates: `/opt`, `/var` and systemd
units under `/etc` survive, the rest of `/etc` and all of `/usr` do not. If an update does
remove the units, the plugin puts them back the next time it loads.

## Security notes

- The service runs as root (it needs the system bus, logind locks, a raw HCI socket and the
  kernel's Bluetooth management socket) and runs only code from a root-owned directory.
  The installer runs as root too, and does not write inside a home directory with that
  identity: the `tcl-me-remo` link in `~/.local/bin` is made by a child process running as
  the home's owner.
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
- direct delivery after a reboot on Android 11–13, and on kernels before 6.17 the repair of
  the kernel's packet count that goes with it;
- that the link is ended with reason 0x13 before suspend and shutdown, and that the TV then
  reconnects after the machine wakes (or that the reason code makes any difference to it);
- whether the TV turns on when it is merely called after a wake;
- which Bluetooth settings pages on SteamOS make the adapter discoverable while they are
  open (the situation the pairing warning is for);
- which key opens the source list from a generic remote, and the list's layout;
- the wake beacon pattern on TCL sets.

[The hardware test plan](hardware-testing.md) settles each of these.
