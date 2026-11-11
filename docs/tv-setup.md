# Setting up the TV

tcl-me-remo can only reach the TV while the TV's Android system is running. A TCL Google TV
that is "off" can be in one of three states, and only the first is reachable over Bluetooth
with ordinary remote keys:

| State | How the TV gets there | Reachable? |
|---|---|---|
| Screen off, system running | Power button, with **Screenless service** (or the equivalent standby mode) on | Expected, from Android's source: the TV reconnects and obeys the wake key |
| Suspended (quick start) | Power button, with Quick start on but Screenless service off | Only by the stock remote, the TV's own button or HDMI-CEC (see [wake beacon](#if-the-tv-does-not-wake)) |
| Fully off | Quick start off, or "Shut down" chosen from the long-press power menu | No |

None of this has been confirmed by this project on real hardware; it is put together from
TCL's manuals, Android's source and owners' reports. The first thing to test after pairing
is whether your TV wakes.

## Settings to change on the TV

Menu names differ between firmware versions; the 2025 models fold the first two into a single
"Standby Mode" setting.

1. **Settings > System > Power & Energy**
   - **Quick start: On.**
   - **Screenless service: On** (2025 firmware: set *Standby Mode* to the screenless / network
     standby option). This keeps Android and Bluetooth alive while the screen is off. It uses
     a little more standby power.
2. **Settings > System > Power & Energy > Power On Behavior: Last Input Used.**
   With this the TV comes back on the input it was showing, so if you last used the SteamOS
   machine there is nothing to switch. It is the most reliable way to land on the right
   input, and it makes the input macro a fallback rather than a necessity.
3. **Settings > Channels & Inputs > External Inputs** (older firmware: *Inputs*): hide the
   inputs you do not use and set the others to **On** rather than Auto. The input macro
   counts positions in the source list, so the list must not change length.
4. Never confirm the **Shut down** choice in the dialog a long press of the power button
   opens. It puts the TV in the fully-off state, from which only the stock remote or the
   button on the set can bring it back.

## Pairing

1. In the tcl-me-remo panel choose **Pair TV** (it is on the Setup page too). Pairing mode
   stays open for two minutes.
2. On the TV: **Settings > Remotes & Accessories > Pair accessory** (older firmware: *Add
   accessory*).
3. Pick the SteamOS machine in the list (it is called *SteamOS Remote* unless you renamed it)
   and confirm.

The panel shows the TV's name once the bond is made. Pair once; the bond survives reboots of
both sides.

If the machine does not show up in the TV's list:

- Make sure Bluetooth is on in SteamOS and pairing mode is still open.
- Close other Bluetooth settings or pairing pages on the SteamOS machine. While one of them
  is open the machine can also be visible as an ordinary Bluetooth device; the TV may then
  try to pair the wrong way and give up. The pairing dialog warns when it sees this.
- If the TV was ever paired with this machine as an ordinary Bluetooth device (for audio, for
  instance), remove that pairing on the TV first.
- Move the machine closer for the pairing step.

To start over, choose **Forget TV** in the panel *and* remove this machine under Remotes &
Accessories on the TV (it is listed as *SteamOS Remote*, or possibly under the machine's own
Bluetooth name). Both sides have to forget before pairing again. Cancelling
a pairing that got halfway can take a couple of seconds while the half-made bond is removed.

## Check that it works

In the panel:

1. On the **Remote** page press *Home*: the TV should go to its home screen.
2. **TV off** in the Quick Access panel: the TV should turn off.
3. **TV on**: ...and back on.
4. On the **Setup** page, **Test sleep** and **Test wake** run exactly what a real suspend
   and wake would run, without SteamOS going to sleep.

Then let the TV sit in standby for an hour and choose **TV on** again. Some sets change
standby state a while after they are switched off.

(From a terminal the same three are `tcl-me-remo key HOME`, `tcl-me-remo off` and
`tcl-me-remo on`. Every step in this document has a command as well; the
[README](../README.md#command-line) lists them. None is needed with the panel.)

## Wi-Fi assist (optional)

The Wi-Fi assist is a second way to reach the TV: over your home network, with the protocol
the Google TV phone app uses as a remote. It is off until you pair it, and Bluetooth stays
the main route. It is good for two things: asking the TV for an HDMI input directly instead
of walking through the source list with a macro, and as a fallback for power when the TV
does not answer over Bluetooth.

1. The TV and the machine must be on the same network. Give the TV a fixed address (a DHCP
   reservation in your router): the service keeps using the address you pair with.
2. Find the TV: on the **Wi-Fi assist** page of the panel's settings choose *Search the
   network* and pick the TV. If nothing is found, look the address up on the TV under
   Settings > Network & Internet and enter it under *TV address*.
   (Terminal: `tcl-me-remo wifi discover`.)
3. Pair: choose *Pair with this address*. The TV shows a code of six characters; type it
   in. The TV has to be on for this. (Terminal: `tcl-me-remo wifi pair 192.168.1.50`, with
   your TV's address.)
4. To switch inputs this way, choose the set of links that matches your TV (on the same
   page, under *Switching the input*; the setting is `wifi.link_preset`) and, once it is
   shown to work, set *Switch the input* on the **Input** page to *Wi-Fi* (`input.method`
   `wifi`):

   | Preset | For |
   |---|---|
   | `tcl-mediatek` | reported for C805, C849, C855, C7K, 98P74; the likely match for QM6K, QM7, QM8 and Q7 |
   | `tcl-realtek` | reported for C825 and C645; the likely match for Q5 and Q6 |
   | `tcl-realtek-alt` | reported for C635 |
   | `custom` | your own link in `wifi.app_link`, with `{port}` for the port number |

   Try it first, while the TV shows some other input: *Test: switch to HDMI … now* on the
   Wi-Fi assist page (terminal: `tcl-me-remo input --via wifi`), and watch the TV: the
   service can only tell that the TV accepted the request, not that the input changed.
   These links come from owners' reports of other models, not from tests on your TV: if the
   first preset does nothing, try the next. Change *Switch the input* only once one works.

   When the TV's network is not up in time after a wake, the request fails. The service
   falls back to the Bluetooth key macro only if you turn on *Fall back to the Bluetooth
   macro* on the Input page (`input.wifi_fallback_to_macro`); watch that macro work first
   ([macros](macros.md)), because it presses keys blindly.

Pairing stores a key for this machine in `/var/lib/tcl-me-remo/atv` and remembers the TV's
certificate; later connections are refused unless the TV presents the same one, so after a
factory reset of the TV you pair again. *Unpair* on the Wi-Fi assist page (terminal:
`tcl-me-remo wifi unpair`) deletes the key and the record, which is enough to stop the
pairing being used. (This project knows of no screen in
the TV's settings that lists paired network remotes.)

Over Wi-Fi the TV can only be reached while its network is up, which in standby again
depends on *Screenless service* / the network standby mode.

## If the TV does not wake

- Re-check Screenless service / Standby Mode. This is the cause in most cases.
- MediaTek's reference design, which sets with a MediaTek chipset (Q7, QM6K, QM7, QM8 and
  others) are built on, wakes a suspended TV on a special Bluetooth advertisement from its
  own remote, not on a connection. Whether TCL's sets and remotes use it is not known.
  tcl-me-remo has an experimental, off-by-default **wake beacon** setting that broadcasts
  MediaTek's default pattern, and a custom mode for a pattern captured from your own remote.
  See [how it works](how-it-works.md#wake-beacon).
- Turn on the **Wi-Fi assist**: with network standby enabled the TV may answer a wake request
  over the network when it does not answer over Bluetooth.
- HDMI-CEC ("Power on the TV in sync" in the TV's settings) is the TV's own way of waking when
  a connected device starts. It is outside this project, but nothing stops you from using
  both.

## If the TV wakes when it should not

The service keeps calling the TV whenever the link is down, so that the link is ready when
it is needed. If your TV turns its screen on because of that, turn off **Keep calling the
TV** on the Bluetooth page (`bluetooth.reconnect_on_drop`). The service then only calls the
TV at the moment it has something to send.

Note that on Android TVs any key from a remote wakes the screen. tcl-me-remo never sends keys
on its own apart from the power keys and the input macro you configured.
