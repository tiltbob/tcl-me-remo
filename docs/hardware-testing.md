# Hardware test plan

Nothing in this project had been run against a real TV when it was written: the code was
developed where no Bluetooth hardware exists. This plan settles, in order of importance, the
assumptions it rests on. It is written so that a Claude Code session running **on the SteamOS
machine** can work through it with a person watching the TV, but it works just as well by
hand.

Set-up for the session: `scripts/steamos-dev-setup.sh` (add `--adb` to be able to inspect
the TV), then `./install.sh`.

## How to record results

Create `hardware-results/YYYY-MM-DD-<tv-model>.md` on first use and append to it after every
test. Start it with the facts that identify the set-up:

```sh
tcl-me-remo doctor            # paste the whole report
cat /sys/power/mem_sleep
```

plus, from the TV (Settings > System > About, or adb): model, Android TV OS version and
build, and the Power & Energy settings in effect.

For each test write: what was done, what the TV did, the relevant lines of
`tcl-me-remo logs -n 100`, and the verdict (works / does not work / unclear). Commit the file
when the session ends; it is how the next session, on any machine, learns what is true.

## Tools

```sh
tcl-me-remo status            # one line on the link, e.g. "connected, ready (delivery: bluez)"
tcl-me-remo status --json | grep -E '"(connected|ready|subscribed|delivery|error)"'
                              # the same as separate fields
tcl-me-remo logs -n 100       # what the service did, step by step
journalctl -u tcl-me-remo -b  # the same, for the whole boot
sudo btmon -w /tmp/t3.btsnoop # radio-level trace of one attempt (Ctrl+C to stop)
sudo busctl monitor org.bluez # what BlueZ is told and tells
```

With adb enabled on the TV (`adb connect <tv-ip>`):

```sh
adb shell getprop ro.build.version.release        # Android version
adb shell dumpsys power | grep mWakefulness       # Awake / Asleep / Dozing
adb shell getevent -lt                            # key events as they arrive
adb shell dumpsys input | grep -B2 -A12 "SteamOS" # which key layout the TV applied to us
                                                  # (nothing? the TV may list us under the
                                                  #  machine's own Bluetooth name: look for that)
adb shell dumpsys tv_input | grep -i inputId      # input ids for the Wi-Fi assist
```

adb is the only way for an unattended session to see what the TV did. Without it, ask the
person.

Reading the plain `status` line:

- `connected, ready (delivery: bluez)` or `(delivery: raw)`: keys can be sent, and this is
  how they travel. `raw` is direct delivery (T6).
- `connected (no key sent on this link yet)`: the link is up and the TV has subscribed, but
  nothing was sent on it. This is the normal resting state after a boot or a wake.
- `connected, waiting for the TV to subscribe`: the link is up and the TV has not renewed
  its subscription (yet).
- `calling the TV...`: not connected; the machine is advertising for the TV to connect.

`tcl-me-remo doctor` shows the same on its `TV link` line.

## T1. Pairing

1. `tcl-me-remo pair` (or Pair TV in the panel).
2. On the TV: Settings > Remotes & Accessories > Pair accessory; choose the machine.

Expected: the TV lists the machine, pairing completes without a code, `tcl-me-remo status`
shows the TV's name and `connected`.

Record: the name the TV showed, any prompt (some TVs ask to "press a key"), and whether it
appeared at all.

If the machine never appears in the TV's list: capture `sudo btmon` during pairing mode and
check that advertising is on (look for `LE Set Advertising Enable` / extended advertising).
If the TV starts pairing and fails: run `sudo busctl monitor org.bluez` during the attempt;
the question is whether `RequestAuthorization` reached this service's agent or Steam's.

Two more runs, once plain pairing works. Forget the TV on both sides before each.

**T1a. While the machine itself is discoverable.** The warning this tests is for a machine
that is discoverable over classic Bluetooth while pairing mode is open. Some Bluetooth
settings pages cause that for as long as they are open; whether SteamOS's do is not known,
so find out first. With pairing mode closed:

```sh
bluetoothctl show | grep Discoverable     # "no" is the normal state
```

Open the Bluetooth settings page in Desktop Mode and run it again; do the same with Steam's
Bluetooth page in Game Mode. Record for each page whether it says `yes` while the page is
open. Then start pairing mode with the machine discoverable: with such a page open, or, if
neither page does it, by hand (ask first; it makes the machine visible to nearby devices for
up to three minutes):

```sh
bluetoothctl discoverable on              # and "bluetoothctl discoverable off" to end it
```

Expected: the pairing dialog and `tcl-me-remo status` show a Bluetooth problem that says the
machine is also discoverable over classic Bluetooth; the warning goes away within a second or
two of the machine no longer being discoverable (page closed, or `discoverable off`), and
comes back if it is again.

Record: which pages make the machine discoverable, whether the warning appeared, what the
TV's list showed meanwhile (the machine once, twice, as what kind of device), and whether
pairing from the TV worked in that state. If it failed, a `btmon` trace shows whether the TV
tried classic pairing (`Connect Request` / `IO Capability Request` instead of an LE
connection). `bluetoothctl show | grep Discoverable` after pairing mode has closed confirms
that the service did not change the setting; leave it at `no`.

**T1b. A pairing aborted half-way, then pairing again.** Start pairing mode, choose the
machine on the TV, and as soon as the TV says it is pairing or connecting, leave the TV's
pairing screen (Back), or switch the TV off at the wall. Then either let pairing mode time
out or cancel it.

Expected: no TV is stored (`tcl-me-remo status` shows none). If the TV got as far as bonding,
the log has `removed the bond of … which paired without completing`, `bluetoothctl devices
Paired` no longer lists the TV, and cancelling took up to about two seconds instead of
returning at once. A second, normal pairing right afterwards works; on the TV, remove the
half-made entry first if it shows one.

Record: how far the TV got (the log says whether it bonded), how long cancel took, whether
the bond was gone afterwards, and whether the second pairing worked. If the second pairing
is refused without any prompt, check `bluetoothctl devices Paired`: a leftover bond is the
cause, and that is a bug to report with the log.

## T2. Keys arrive and mean the right thing

With the TV on:

```sh
for key in HOME DOWN DOWN UP RIGHT LEFT OK BACK VOL_UP VOL_DOWN; do tcl-me-remo key $key; sleep 1; done
```

Expected: the TV reacts to each as its own remote would. With adb, `getevent -lt` shows
`KEY_HOMEPAGE`, `KEY_DOWN`, `KEY_SELECT`, `KEY_BACK` and so on.

If nothing reacts but `status --json` says `"ready": true`: this is the subscription problem of T6 showing
up early, or the attributes' encryption requirement being refused. Check
`dumpsys input` on the TV for the device, and try
`tcl-me-remo config bluetooth.gatt_security plain` followed by `sudo systemctl restart tcl-me-remo`
and a fresh pairing.

## T3. Discrete power

```sh
tcl-me-remo key SLEEP     # TV turns off
tcl-me-remo key SLEEP     # stays off (must not toggle back on)
tcl-me-remo key WAKE      # TV turns on
tcl-me-remo key WAKE      # stays on
```

This is the assumption the whole design leans on: Android's `KEYCODE_SLEEP` and
`KEYCODE_WAKEUP` are discrete, but TCL patches the key policy and nobody has reported trying
them on a TCL set.

If `SLEEP` does nothing: try `tcl-me-remo key S:82` (the other sleep usage). If only a
toggle works (`tcl-me-remo key POWER`), note it; that result matters more than anything
else in this plan, because nothing automatic is safe with a toggle. To go on testing you can
set `power.on_key` and `power.off_key` to `POWER`, knowing what then happens:

- The service cannot see the TV, and a toggle does the opposite of what was meant whenever
  the TV is not in the state the service assumes. That is the case every time the TV was
  switched with its own remote: the next suspend turns a TV that is off **on**, and it
  stays on while the machine sleeps.
- At boot and wake the toggle is sent only if the last power key the service sent turned the
  TV off. After a reboot (which leaves the TV on), on a wake that follows a suspend without
  "TV off", and before the service has turned the TV off for the first time, "TV on" is
  skipped and `tcl-me-remo status` shows the reason. `tcl-me-remo on` always sends the key.
- A toggle is sent once, whatever `power.on_repeat` and `power.off_repeat` say, and however
  it is written (`POWER`, `S:81`, `C:0030`, `K:66`).

Record how often the TV ended up in the wrong state over a day of normal use, and what
preceded it.

Also record what `SLEEP` leaves behind: the home screen or the previous input when the TV
comes back.

## T4. Can the TV be woken, and from which standby state

For each Power & Energy configuration (start with Quick start on + Screenless service on):
turn the TV off with `tcl-me-remo off`, then after 10 seconds, 2 minutes, 45 minutes and (if
possible) overnight run:

```sh
tcl-me-remo on --no-input ; tcl-me-remo logs -n 20
```

Record for each point: did the TV connect (`Bluetooth: TV connected after …`), did the screen
turn on, and how long it took.

This decides what `docs/tv-setup.md` must say. If the TV connects but stays dark, the wake key
is not honoured in that state. If it never connects, the TV is suspended: see T11.

Also note whether the TV ever turns on **by itself** while the machine is running and calling
it. If it does, set `bluetooth.reconnect_on_drop` to `false` and repeat.

## T5. Suspend and resume

Ask first. With the TV on and showing the machine:

1. Suspend from the Steam power menu.
2. Wake the machine with a controller or its power button.

Expected: the TV turns off before the machine sleeps and comes back when it wakes.

```sh
journalctl -u tcl-me-remo -b | tail -40
journalctl -b | grep -E "Delay lock is active|will suspend now|PM: suspend entry"
```

Record the time between the service's `tv_off (suspend)` line and the kernel's suspend entry,
and whether `TV off` came through `Bluetooth` or failed. Repeat with the TV link down (turn
the TV off and on with its own remote first): the service then has about 3 seconds to
reconnect.

With the default settings that is all the time there is. If `bluetooth.reconnect_on_drop`
is off (T4 may have had you turn it off) or `bluetooth.idle_disconnect_seconds` is set, the
link is down because the settings have it down, and a first attempt that did not get the TV
back is followed by a second one from the sleep hook, with 6 seconds of its own. The log
then shows two `tv_off (suspend)` records, the second with the step `second attempt: the
first one could not reach the TV in its time`, and the suspend may be held for up to about
10 seconds. Record which settings were in effect, both records, and the time from the
*first* `tv_off` line to the kernel's suspend entry.

**T5a. How the link ends, and whether the TV comes back.** The service ends the link itself
just before the machine sleeps, so that the TV is told "remote user terminated" (0x13) and
not "power off" (0x15), which the kernel would send. Neither that this happens nor that it
makes a difference has been seen on hardware. Record a trace across the suspend and read it
afterwards. Run the recorder as a unit of its own: one started in an ssh session dies with
the session, and an ssh session over Wi-Fi rarely survives a suspend.

```sh
sudo systemd-run --unit=t5-trace -p StandardOutput=null btmon -w /tmp/t5.btsnoop
#   suspend and wake as above, wait 30 s, then:
sudo systemctl stop t5-trace
btmon -r /tmp/t5.btsnoop | grep -E -A6 "MGMT Command: Disconnect|HCI Command: Disconnect|Disconnect Complete|LE.*Connection Complete"
```

In the trace, before the suspend:

- `@ MGMT Command: Disconnect (0x0014)` with the TV's address: the service's request. A line
  `could not end the TV link through the kernel` in `tcl-me-remo logs` instead means it fell
  back to BlueZ; record the reason it gives.
- `< HCI Command: Disconnect` with `Reason: Remote User Terminated Connection (0x13)`, and
  `> HCI Event: Disconnect Complete` with `Reason: Connection Terminated By Local Host
  (0x16)`, both before the machine sleeps. A Disconnect with `Remote Device Terminated due
  to Power Off (0x15)` for the TV's handle means the kernel got there first: record the
  times of both.
- The key-up of `SLEEP` (an `ATT: Handle Value Notification` with an all-zero value) goes
  out before the Disconnect, and the TV turns off.

After the wake, without touching anything: is the link back within about ten seconds (`Keep
calling the TV` on), and does the trace show the TV's `LE Connection Complete`? In
`tcl-me-remo status --json`, `"connected": true` is the answer; the plain `status` line then
reads `connected` after a wake that sent a key, and `connected, waiting for the TV to
subscribe` after one that did not, which is the link being up and unused, not a fault. This
is the question the reason code is about. Record it together with the TV's Android version.

Also try a wake on which nothing is sent to the TV (`tcl-me-remo config events.resume_tv_on
false`, and `events.resume_switch_input false`): the log should show `the machine is awake
again: calling the TV in the background`, and the link should be back without any action
having run. Do it once with the TV on and once with the TV in standby, and for the second
record whether the TV **turned on**: it is called on such a wake although nothing is sent,
and a TV that wakes from that alone needs `bluetooth.reconnect_on_drop` off. Set the options
back afterwards.

**The comparison with 0x15. Do this last in a session, and only once:** it costs the TV's
subscription. `sudo systemctl stop tcl-me-remo` with the TV connected takes the HID service
away from BlueZ, and after `start` BlueZ no longer knows that the TV subscribed. An Android
11 to 13 TV then gets no keys until it is paired again (or `bluetooth.raw_notify` allows
direct delivery, see T6), so nothing that depends on keys should follow this step.

With the trace running: `sudo systemctl stop tcl-me-remo`, suspend, wake, `sudo systemctl
start tcl-me-remo`. The expectation is that the link stays up without the service and that
the kernel ends it at suspend with 0x15. Neither is certain: the TV may close the link
itself when the HID service disappears. So read the trace before drawing any conclusion.
Only if it shows the TV's link still up at suspend entry and a Disconnect with reason 0x15
for its handle does this run say anything about 0x15; otherwise record what ended the link
instead, and leave the question open. If the run counts: does the TV connect after this
wake as well? Reconnecting after 0x13 and not after 0x15 (or the other way round, or always,
or never) is worth a line in `hardware-results/` on its own.

## T6. After a reboot (the subscription problem)

BlueZ forgets which keys the TV subscribed to whenever `bluetoothd` restarts, which includes
every boot. Android 14 TVs subscribe again; Android 11–13 do not, and for those the service
delivers keys straight to the radio instead ("raw" delivery). That path was built from
source reading alone.

First without rebooting (ask first: this disconnects every Bluetooth controller briefly):

```sh
sudo systemctl restart bluetooth
sleep 5
tcl-me-remo connect ; tcl-me-remo status --json | grep -E '"(ready|subscribed|delivery)"' ; tcl-me-remo key HOME
```

Record: `delivery` (`bluez` means the TV re-subscribed, `raw` means direct delivery; it is
empty when `connect` failed), whether the log shows a reconnect ("bounce") and whether HOME
reached the TV. Take a `sudo btmon -w /tmp/t6.btsnoop` trace of this test; it shows the
Service Changed indication, any CCC writes by the TV, and the injected notification.

Read the trace for the TV's **subscription writes** in particular (`btmon -r
/tmp/t6.btsnoop | less`). A TV subscribes to each of the three key reports with an `ATT:
Write Request` to the handle right after the report's value handle, which is where BlueZ
puts the subscription descriptor (going by its source): `0x010b` for the keyboard report
(value handle `0x010a`), `0x010f` for the consumer keys (`0x010e`) and `0x0113` for system
control, the report `WAKE` travels in (`0x0112`). The value written is `01 00`. For the
first link after the restart, and again for the link after a "bounce" if there was one,
record:

- how many of the three writes there are (three, some, none),
- in which order they come,
- how long after the link came up each one arrives (the time stamps of the `LE Connection
  Complete` or `LE Enhanced Connection Complete` event and of each write),
- and anything between them: a disconnect, an error response, a write with `00 00`.

The service counts the TV as ready when all three are there, and waits for the rest for a
while when only some are. What a real TV does here (all at once, one per connection event,
only after its own service discovery, some never) has not been observed, and the waiting
times in the code are guesses until it has.

What happens next depends on the kernel. `tcl-me-remo doctor` has a `direct delivery` line
that names the kernel and says what the setting comes to; `uname -r` is the same version.

**If the TV subscribes again** (`"delivery": "bluez"`): Android 14 or newer, nothing more
to test here. Record it.

**If it does not, on Linux 6.17 or newer** (direct delivery is used by default):

- In the trace, each key is two `ATT: Handle Value Notification` packets (press, release)
  on the TV's connection handle that BlueZ did not send: handle `0x010a`, `0x010e` or
  `0x0112`, the second with an all-zero value. HOME is `0x010e` with value `23 02 00 00`.
- The TV reacts to the key. With adb, `getevent -lt` shows it arriving.
- `sudo dmesg | grep "sent 0 < count"` shows one kernel warning per injected packet. That
  is expected on these kernels and is the whole of the side effect; record that the lines
  are there and that nothing else is.

**If it does not, on a kernel before 6.17** (SteamOS 3.7's 6.11, for one): with the default
`auto`, `tcl-me-remo connect` fails with a message that names the kernel and says direct
delivery is not used automatically. Record the message. Then, with the person's agreement
(the next steps touch the Bluetooth accounting their controllers depend on; have a second
way to control the machine at hand):

```sh
tcl-me-remo config bluetooth.raw_notify on
tcl-me-remo connect ; tcl-me-remo status --json | grep -E '"(ready|subscribed|delivery)"' ; tcl-me-remo key HOME
sudo dmesg | grep "sent 0 < count"
```

- **First find out which kind of kernel this is.** The version number does not say: the
  part of the 6.17 fix that matters was also put into older stable kernels (6.12.45,
  6.6.104 and 6.1.150 among them), and a vendor kernel may or may not carry it. If `dmesg`
  shows `sent 0 < count` lines after the key, the kernel has the fix: nothing was
  miscounted, the service's repair has nothing to repair, and controllers that keep working
  prove nothing about it. Record the result of the steps below as "kernel with the fix
  (clamped), repair not exercised", never as a confirmation of the repair. Only without
  those lines do the steps below test the repair.
- As above: the injected notifications in the trace, and the TV reacting.
- Send about ten keys, then close the link (`tcl-me-remo disconnect`) with the trace still
  running. Right after the TV's `Disconnect Complete` the trace must show `< HCI Command: LE
  Read Buffer Size` that BlueZ did not send, and its `Command Complete`. Record the `Num
  data packets` (LE buffer count) in that answer. If it is 0, the controller shares its
  buffers with classic Bluetooth and a `Read Buffer Size` command must follow; record
  whether it did.
- For the shared-buffer case, compare with the classic figure `hciconfig hci0` prints
  (`ACL MTU: <size>:<count>`). The LE figure is only in the controller's own answer; do not
  ask for it by hand with `hcitool cmd`, the question itself resets the kernel's count.
- Then the part that matters to the user. Connect a Bluetooth LE controller (an Xbox
  Series or 8BitDo pad in LE mode) and a Bluetooth headset, and use both for a few minutes:
  inputs arrive without lag or drop-outs, audio plays without stutter, and turning the
  controller off and on reconnects it. Do this once after a single key and once after
  thirty or more (`for i in $(seq 15); do tcl-me-remo key DOWN; tcl-me-remo key UP; done`,
  then disconnect): the count of transmit slots the repair has to restore grows with the
  number of keys.
- If anything stalls: `tcl-me-remo doctor`, the trace, `sudo dmesg | tail -50`, then switch
  Bluetooth off and on (that resets the controller's counters) and set `bluetooth.raw_notify`
  back to `auto`. This is the result that decides whether `on` may ever become the default
  on these kernels; write it down either way.

Then the real thing: put the TV in standby, reboot the machine, and see whether the TV comes
on. `tcl-me-remo logs -n 60` afterwards shows the boot action.

If keys do not arrive with `"delivery": "raw"`, this is the most valuable bug report this project
can get: keep the btmon trace, the logs and the TV's Android version.

## T7. Shutdown and reboot

Ask first. Power the machine off from the Steam menu: the TV should turn off. Restart it
from the Steam menu: the TV should stay on (`events.reboot_tv_off` is off by default).

```sh
journalctl -u tcl-me-remo -b -1 | tail -20      # the previous boot's last lines
```

## T8. Input switching

First set the TV's Power On Behavior to "Last Input Used" and see whether that alone is
enough after `tcl-me-remo off` / `tcl-me-remo on`.

For the macro, switch the TV to another input with its own remote, then find the key that
opens the source list:

```sh
tcl-me-remo key F3          # reported to work on a QM8
tcl-me-remo key TV
tcl-me-remo key C:01BB      # Google reference remote "Input"; unlikely with our identity
```

With adb, `adb shell input keyevent 178` shows what the stock Input key would do, for
comparison. Once a key opens the list, note its layout (row or column, what comes before
HDMI 1, where focus starts) and pick or write the macro:

```sh
tcl-me-remo config input.hdmi_port 2
tcl-me-remo config input.preset tcl-row       # or tcl-row-first, tcl-column, custom
tcl-me-remo macro "F3 1.5s LEFT*8 RIGHT*{port} OK"   # try before saving
tcl-me-remo config input.method macro
tcl-me-remo input
```

Record the macro that works and the list layout. If no key opens the list from this remote,
say so; the Wi-Fi assist (T10) is then the way to switch inputs.

## T9. Controllers alongside the TV link

With the TV connected (`tcl-me-remo status` shows `connected`): connect a Bluetooth controller,
play for a minute, turn the controller off and on again. Try one classic controller and one
Bluetooth LE controller if available.

Record whether the controller reconnects while the TV link is up. If an LE controller does
not, set `bluetooth.idle_disconnect_seconds` to `30` and repeat.

## T10. Wi-Fi assist

```sh
tcl-me-remo wifi discover
tcl-me-remo wifi pair <tv-ip>        # type the code the TV shows
tcl-me-remo input --via wifi         # with the TV showing some other input
```

`--via wifi` tries the Wi-Fi route alone, whatever `input.method` says, so nothing else is
sent if it fails. The service reports `ok` as soon as the TV accepted the request; the
protocol has no answer that says the input really changed, so watch the TV. Only when it
works: `tcl-me-remo config input.method wifi`.

If the input does not change, find the TV's input ids
(`adb shell dumpsys tv_input | grep -i inputId`), try the other built-in link sets
(`tcl-me-remo config wifi.link_preset tcl-realtek`) or set a custom link. Record the ids:
they are worth adding to `tvdata.py`.

Also check whether the assist answers while the TV is in standby (T4's time points).

## T11. Wake beacon (only if T4 shows the TV cannot be woken)

MediaTek's reference design lets a suspended set wake on an advertisement that its own
remote sends. Whether TCL's sets and remotes do this is not known.

First try the built-in pattern, which is MediaTek's default with the TV's address in it:

```sh
tcl-me-remo config bluetooth.wake_beacon mediatek
```

Put the TV in the standby state that would not wake and repeat T4 (`tcl-me-remo on`). Record
whether it wakes. If it does not, capture what the TV's own remote sends. Cover the remote's
infrared LED with tape, then in one terminal start a scan (btmon only listens; without a
scan nothing is reported) and in another record everything:

```sh
bluetoothctl scan le                 # terminal 1; leave it running
sudo btmon -w /tmp/t11.btsnoop       # terminal 2
```

Press Power on the remote, wait ten seconds, stop both, and look through the trace:

```sh
btmon -r /tmp/t11.btsnoop | grep -B4 -A14 "Advertising Report" | less
```

("Advertising Report" matches both the old and the extended form.) Record the reports that
appear only when the button is pressed: the event type, the address, and the full data,
especially any "Company" / manufacturer-specific field. If the TV wakes with the infrared
LED covered, the remote woke it over Bluetooth. Keep the `.btsnoop` file with the results.
Seeing nothing is not proof of the opposite: a remote that advertises directly at the TV
is filtered out by this machine's controller.

To replay a captured pattern, give its manufacturer data as hex in the order it is sent,
company id first. For a btmon line `Company: MediaTek, Inc. (70)` followed by
`Data: 00ffeeddccbbaa0000000043524b544d` that is `4600` (70 as two bytes, low byte first)
followed by the data:

```sh
tcl-me-remo config bluetooth.wake_beacon_data 460000ffeeddccbbaa0000000043524b544d
tcl-me-remo config bluetooth.wake_beacon custom
```

Then repeat T4 and record the result.

## T12. The plugin in Game Mode, and removal

Nothing of the Decky plugin has run inside Steam; only a browser preview of its screens
exists. Record each step:

1. Build the ZIP (`python3 scripts/build_zip.py`) and install it through Decky's developer
   settings (*Install plugin from ZIP file*). Does the panel appear, and does it install and
   start the service by itself (the Setup page says so; `systemctl is-active tcl-me-remo`)?
2. Walk through every page of the settings and the Quick Access panel. Note anything that
   does not fit the screen, cannot be reached with a controller, or shows an error.
3. Pair, TV off, TV on from the panel (as T1 to T3, but with the buttons).
4. Uninstall the plugin in Decky and leave the machine on. After about three minutes the
   service should be gone (`systemctl status tcl-me-remo`, `ls /opt/tcl-me-remo`).
5. Install it again, uninstall it, and restart the machine within a minute. A few minutes
   after the restart the service should be gone as well.
6. Install it once more, then `tcl-me-remo uninstall --purge`, install again and pair the
   same TV. Pairing must work: the purge is meant to remove the Bluetooth bond too.

## When a test fails

Capture first, change code second: `tcl-me-remo doctor`, `tcl-me-remo logs -n 200`, the btmon
trace of the failing attempt, and what the TV did. Put them in the results file. If the fix
is small and local (a timing value, a default, a macro), make it, run the tests, reinstall
with `./install.sh`, and re-run the test.

SteamOS itself has no pytest. `scripts/steamos-dev-setup.sh` makes a private environment in
the checkout for it, so on the machine the tests are run with

```sh
.venv/bin/python -m pytest -q --timeout 60
```

If that environment could not be made, do not install an untested change as a root
service: commit it to a branch and have it tested elsewhere first.
