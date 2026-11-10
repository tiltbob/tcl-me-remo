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
tcl-me-remo status            # link state: paired / connected / ready, delivery bluez or raw
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
adb shell dumpsys input | grep -A12 "SteamOS"     # which key layout the TV applied to us
adb shell dumpsys tv_input | grep -i inputId      # input ids for the Wi-Fi assist
```

adb is the only way for an unattended session to see what the TV did. Without it, ask the
person.

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

## T2. Keys arrive and mean the right thing

With the TV on:

```sh
for key in HOME DOWN DOWN UP RIGHT LEFT OK BACK VOL_UP VOL_DOWN; do tcl-me-remo key $key; sleep 1; done
```

Expected: the TV reacts to each as its own remote would. With adb, `getevent -lt` shows
`KEY_HOMEPAGE`, `KEY_DOWN`, `KEY_SELECT`, `KEY_BACK` and so on.

If nothing reacts but `status` says `ready`: this is the subscription problem of T6 showing
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
toggle works (`tcl-me-remo key POWER`), set `power.on_key` and `power.off_key` to `POWER` and
note it; the service never repeats a toggle.

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

## T6. After a reboot (the subscription problem)

BlueZ forgets which keys the TV subscribed to whenever `bluetoothd` restarts, which includes
every boot. Android 14 TVs subscribe again; Android 11–13 do not, and for those the service
delivers keys straight to the radio instead ("raw" delivery). That path was built from
source reading alone.

First without rebooting (ask first: this disconnects every Bluetooth controller briefly):

```sh
sudo systemctl restart bluetooth
sleep 5
tcl-me-remo connect ; tcl-me-remo status ; tcl-me-remo key HOME
```

Record: `delivery` (`bluez` means the TV re-subscribed, `raw` means direct delivery), whether
the log shows a reconnect ("bounce") and whether HOME reached the TV. Take a
`sudo btmon -w /tmp/t6.btsnoop` trace of this test; it shows the Service Changed indication,
any CCC writes by the TV, and the injected notification.

Then the real thing: put the TV in standby, reboot the machine, and see whether the TV comes
on. `tcl-me-remo logs -n 60` afterwards shows the boot action.

If keys do not arrive with `delivery: raw`, this is the most valuable bug report this project
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

With the TV connected (`tcl-me-remo status` shows `ready`): connect a Bluetooth controller,
play for a minute, turn the controller off and on again. Try one classic controller and one
Bluetooth LE controller if available.

Record whether the controller reconnects while the TV link is up. If an LE controller does
not, set `bluetooth.idle_disconnect_seconds` to `30` and repeat.

## T10. Wi-Fi assist

```sh
tcl-me-remo wifi discover
tcl-me-remo wifi pair <tv-ip>        # type the code the TV shows
tcl-me-remo config input.method wifi
tcl-me-remo input
```

If the input does not change, find the TV's input ids
(`adb shell dumpsys tv_input | grep -i inputId`), try the other built-in link sets
(`tcl-me-remo config wifi.link_preset tcl-realtek`) or set a custom link. Record the ids:
they are worth adding to `tvdata.py`.

Also check whether the assist answers while the TV is in standby (T4's time points).

## T11. Wake beacon (only if T4 shows the TV cannot be woken)

MediaTek-based sets listen, while suspended, for an advertisement their own remote sends.
Capture it: put the TV in the standby state that would not wake, cover the remote's infrared
LED with tape, then

```sh
sudo btmon | grep -B2 -A12 "LE Advertising Report"
```

and press Power on the remote. Record the advertising reports that appear only when the
button is pressed: the event type, the address, and the full data, especially any
"Company" / manufacturer-specific field. If the TV wakes with the infrared LED covered, the
remote woke it over Bluetooth and that data is the pattern to replay.

## When a test fails

Capture first, change code second: `tcl-me-remo doctor`, `tcl-me-remo logs -n 200`, the btmon
trace of the failing attempt, and what the TV did. Put them in the results file. If the fix
is small and local (a timing value, a default, a macro), make it, run
`python3 -m pytest -q --timeout 60`, reinstall with `./install.sh`, and re-run the test.
