# tcl-me-remo

Let a SteamOS machine control a TCL Google TV over Bluetooth.

The machine pairs with the TV once, as a Bluetooth remote control. From then on:

- when SteamOS **boots or wakes from suspend**, the TV turns on (and can switch to the
  machine's input);
- when SteamOS **suspends or shuts down**, the TV turns off;
- a panel in **Game Mode** (a Decky Loader plugin) installs, sets up, updates and removes all
  of it. On a machine that has Decky Loader, no terminal and no password are needed at any
  point. (There is a command line tool as well, for those who want one.)

<p>
  <img src="docs/images/panel.png" alt="The Quick Access panel: status, TV on, TV off, switch input, and the automatic toggles" width="230">
  <img src="docs/images/setup.png" alt="The Setup page of the settings: service, TV preparation, pairing" width="560">
</p>

*The panel and the settings page, rendered in a browser preview (the plugin has not been run
inside Steam yet).*

> **Not yet confirmed on real hardware.** This was written without access to a TV. The
> design follows Android's and BlueZ's source code and TV owners' reports, and the software
> is tested against simulated system services, but which standby modes a TCL TV can be woken
> from, and a few other things listed in [how it works](docs/how-it-works.md#what-is-not-confirmed),
> still need to be observed. [`docs/hardware-testing.md`](docs/hardware-testing.md) is the
> plan for doing that.

## Is this what you want?

If your machine and TV can talk over **HDMI-CEC**, use that: it wakes the TV and switches
the input in one step, and SteamOS 3.8 and newer support it natively on hardware that has
it (the Steam Machine, or a USB CEC adapter). Most PCs and graphics cards do not pass CEC
through, and docks often do not either. This project is for those: it needs nothing but the
Bluetooth both sides already have.

## What you need

- SteamOS 3.7 or newer (Bazzite and similar systems should work the same way), with Bluetooth
  turned on. The Bluetooth adapter must support the LE peripheral role, which common Intel,
  MediaTek, Realtek and Qualcomm adapters do.
- A TCL Google TV. Other Android / Google TV sets may work; the built-in input-switching
  presets are TCL-specific.
- [Decky Loader](https://decky.xyz), already installed. (Installing Decky itself is done
  once, in Desktop Mode, with its own installer; that step is Decky's, not this project's.)
  Without Decky there is a [terminal route](#without-decky-and-for-development).

## Install

Everything here is done in Game Mode with a controller.

1. Press the **…** (Quick Access) button, open the **Decky** tab (the plug icon) and then
   its settings (the gear icon). On the **General** page turn on **Developer mode**. A page called
   **Developer** appears in the same settings.
2. On the **Developer** page, under *Third-Party Plugins*, find **Install Plugin from URL**.
   Select the *URL* field and type this address with the on-screen keyboard:

   ```
   github.com/tiltbob/tcl-me-remo/releases/latest/download/tcl-me-remo.zip
   ```

   It has to be typed exactly: all lower case, three times `tcl-me-remo` with hyphens, no
   spaces, and nothing after `.zip`. Decky puts `https://` in front by itself. The ending
   matters most: Decky takes the plugin's name from it.
3. Choose **Install**. Decky warns that it has not reviewed the plugin (the first time, the
   *Continue* button takes five seconds to become available) and then asks whether to
   install the plugin. Its question must read **Install tcl-me-remo**, letter for letter:
   Decky shows there the name it took from the end of the address. If it shows anything
   else, cancel and correct the address. Otherwise confirm.
4. Open the Decky tab again. The plugin is in the list as **tcl-me-remo**; its panel is
   titled *TV remote*.

The plugin installs a background service that runs as root, because two of the things it
does need kernel Bluetooth sockets that only root may open (sending keys to a TV that did
not subscribe again after a reboot, and ending the link at once before the machine sleeps);
this happens through Decky Loader, which runs as root itself, so nobody is asked for a
password. The first time the panel opens it may say *Installing the background service* for
a moment.

None of this has been run inside Steam yet (see the note at the top): the steps follow
Decky Loader's source code and its own labels. Going by that source, an installation that
fails says nothing. With an address that is wrong somewhere before its end, Decky goes back
to its list of plugins and the plugin is simply not in it. On a machine that is offline, or
when Decky cannot reach its own plugin server (it contacts that server on every install, to
count it), nothing is installed either, and the dialog may stay open: press **B**. In each
case type the address again. A copy that is already installed is not touched by a failed
attempt.

The plugin is not in Decky's plugin store, and will not be: the store does not take plugins
written mostly with AI assistance, which this one was.

## Set up

Still in Game Mode. Open the panel and choose **Setup** (or the gear icon in its title). The
Setup page is a checklist; these are its steps:

1. **The background service** has a tick when it runs.
2. **Prepare the TV** ([details](docs/tv-setup.md)). With the TV's own remote, in Settings >
   System > Power & Energy, turn on **Quick start** and **Screenless service** (on 2025
   models: the screenless *Standby Mode*), and set **Power On Behavior** to **Last Input
   Used**.
3. **Pair.** Choose **Pair TV**. Then on the TV open Settings > Remotes & Accessories > Pair
   accessory and pick *SteamOS Remote*. The page shows the TV's name once it is paired.
4. **Test.** **Test sleep** turns the TV off and **Test wake** turns it on, exactly as a real
   suspend and wake would, without SteamOS going to sleep.

That is all that is needed for power. Suspend the machine and the TV should go off with it.
The Quick Access panel has *TV on*, *TV off* and the switches for what happens automatically;
the other settings pages are for the cases below.

## Getting to the right input

With *Power On Behavior: Last Input Used* the TV comes back on the input it showed last,
which is usually the SteamOS machine. If you want the machine to switch the input itself,
there are two ways, both off until you turn them on (the **Input** page):

- **Key macro over Bluetooth.** Android TVs have no remote key for "HDMI 2", so the machine
  presses the keys you would: open the source list, move to the entry, OK. Pick the HDMI
  port, choose a preset, press *Try* and watch the TV. If the preset misses, adjust the
  macro; `F3 1.5s LEFT*8 RIGHT*2 OK` reads "F3, wait, eight times left, twice right, OK".
  See [macros](docs/macros.md).
- **Wi-Fi assist** (its own page). Opt-in. The machine pairs with the TV over the network,
  the way the Google TV phone app does, and asks for the input directly. More reliable than
  a macro, but it needs the TV's network to be up.

## Update

Install the plugin again from the same address, exactly as in [Install](#install) (Developer
mode stays on, so it is steps 2 and 3): the address always gives the newest release. Decky
replaces the plugin, and the new plugin updates the background service when it loads.
Settings and the pairing with the TV are kept.

Have the address in front of you on a phone or on paper. The panel's **Diagnostics** page
shows it too, but that page and Decky's cannot be on screen together, and Decky's field
starts empty every time.

An update that changes the background service restarts it, which for the TV is the same as
a restart of SteamOS: see *Keys stopped working after a reboot* under
[When something does not work](#when-something-does-not-work) if keys do not arrive
afterwards.

Nothing looks for updates by itself, and the *Reinstall* button next to the plugin in Decky's
list does not help: it looks in Decky's store, where this plugin is not. The panel's
**Diagnostics** page shows the version that is installed. Which version is the newest is on
the [releases page](https://github.com/tiltbob/tcl-me-remo/releases), a web page; if in
doubt, install again. Installing the version you already have does no harm: the service is
left running as it is.

## Remove

Uninstall the plugin in Decky: its settings, the **Plugins** page, the **…** button next to
`tcl-me-remo`, **Uninstall**. The background service follows by itself about three minutes
later (if the machine is restarted or switched off before that, a few minutes after it next
starts). The wait is there because Decky runs the same step when it only updates a plugin;
the service checks that the plugin is really gone before it removes itself. Settings and the
pairing stay on the machine, in case you install again.

**Disable**, in the same menu, is not a removal: it only takes the panel away. The service
keeps running and the TV keeps following the machine. (To stop that and keep everything
installed, turn off **Automatic actions** in the panel instead.) Decky also does not tell a
disabled plugin that it is being uninstalled. The service notices by itself then: it looks
into Decky's plugins folder a few minutes after each of its starts and once an hour while
it runs, and removes itself when the plugin is no longer there. So after *Disable* and then
*Uninstall* it can take an hour or so of the machine being on.

To remove the settings and the pairing as well, do this *before* uninstalling the plugin:
in the panel's settings open **Diagnostics**, choose **Remove**, turn on **Also delete
settings and the pairing** and confirm. Then uninstall the plugin (it would put the service
back the next time it loads). On the TV, remove this machine under Remotes & Accessories as
well (it is listed as *SteamOS Remote*, or possibly under the machine's own Bluetooth name),
or the two cannot be paired again.

One case is not covered: if **Decky Loader itself** is removed while the plugin is still
installed, Decky does not tell its plugins, so the service stays and keeps turning the TV on
and off. So uninstall the plugin first. If it has already happened, install Decky Loader
and the plugin again and remove them in that order, or see
[removing from a terminal](#remove-from-a-terminal).

## When something does not work

In the panel's settings, open **Diagnostics**:

- **Check the set-up** > *Run check* looks at Bluetooth, the service, the pairing and the
  suspend hooks and says what is wrong in plain words. It changes nothing.
- **What happened** lists every action with the steps that were tried.
- **Log** shows what the service wrote. When the service does not answer, the same place
  shows what the system kept of it, which is where a service that cannot start says why.
- **Repair** puts the service back in order; **Restart** restarts it.

For a bug report, take screenshots of the check and of the failed action (on a Steam Deck:
Steam + R1).

- **The TV does not turn on.** Almost always the TV's standby mode: see
  [setting up the TV](docs/tv-setup.md#if-the-tv-does-not-wake).
- **The TV turns on by itself.** Turn off *Keep calling the TV* on the Bluetooth page. With
  it on, the machine calls the TV whenever the two are not connected, also after a wake on
  which it does not turn the TV on; whether a TCL set switches its screen on from being
  called has not been observed.
- **Keys stopped working after a reboot.** The Bluetooth page shows under *Right now* whether
  the TV has subscribed and how keys are being delivered, and the set-up check says whether
  direct delivery, the way around a TV that does not re-subscribe after a reboot, is in use.
  On Linux kernels before 6.17 it is not used unless you set *Direct delivery* (Bluetooth
  page, Advanced) to *On*; the error message says so when that is the reason. See
  [after a reboot](docs/how-it-works.md#after-a-reboot); pairing again always restores things.
- **The machine does not show up on the TV when pairing.** Pairing mode closes after two
  minutes; open it again. Remove any earlier pairing between the TV and this machine first.
- **Settings are back to their defaults and the TV shows as unpaired.** Diagnostics then has
  a *Settings problems* section: the settings file could not be read and was kept under
  another name. Set things up again in the panel and pair the TV again; the notice ends when
  the service next restarts. (This is what a slip while editing the file by hand does. To
  mend the old file instead, see [the terminal section](#a-settings-file-that-was-moved-aside).)
- **A Bluetooth LE controller no longer reconnects.** On the Bluetooth page set *Disconnect
  when idle* to *After 30 seconds*. The link to the TV is then closed whenever no key was sent for
  that long and made again when needed, so turning the TV off at suspend depends on the TV
  answering within a few seconds.

## Settings worth knowing

Every setting is on one of the panel's settings pages. The names in the first column are
what the command line and the settings file call them.

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | on | Master switch for everything automatic |
| `events.boot_tv_on`, `events.resume_tv_on` | on | Turn the TV on at boot / wake |
| `events.suspend_tv_off`, `events.shutdown_tv_off` | on | Turn the TV off at suspend / power-off |
| `events.reboot_tv_off` | off | Also turn it off when the machine merely restarts |
| `display.require_external` | `auto` | On a handheld, act only while an external display is connected |
| `display.edid_match` | empty | Act only while a display with this name is connected (for example `TCL`) |
| `input.method` | `none` | `macro`, `wifi` or `none` |
| `input.hdmi_port` | 1 | The HDMI port the machine is on |
| `input.wifi_fallback_to_macro` | off | With `input.method` `wifi`: play the key macro when the Wi-Fi request fails |
| `bluetooth.reconnect_on_drop` | on | Keep calling the TV while it is not connected |
| `bluetooth.raw_notify` | `auto` | Direct key delivery for TVs that do not re-subscribe after a reboot: `auto` (Linux 6.17 and later only), `on`, `off` |
| `power.on_key`, `power.off_key` | `WAKE`, `SLEEP` | The keys used for power |

## Without Decky, and for development

Everything from here on uses a terminal. With Decky Loader none of it is needed.

### Install from a checkout

In Desktop Mode, open Konsole:

```sh
git clone https://github.com/tiltbob/tcl-me-remo
cd tcl-me-remo
./install.sh
```

`install.sh` needs `sudo`. On SteamOS, set a password for your user first with `passwd`.
Running it again after `git pull` updates the service.

The service ends up in `/opt/tcl-me-remo`, its settings in `/var/lib/tcl-me-remo`, and the
`tcl-me-remo` command is linked into `~/.local/bin` (the plugin does the same). These places
survive SteamOS updates.

On SteamOS `~/.local/bin` is not necessarily on your `PATH`. If the shell answers
`command not found`, type the full name, `~/.local/bin/tcl-me-remo` (or
`/opt/tcl-me-remo/bin/tcl-me-remo`), or add the directory once and open a new terminal:

```sh
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
```

Then prepare the TV as in [Set up](#set-up), run `tcl-me-remo pair`, pick *SteamOS Remote*
on the TV, and try `tcl-me-remo off` and `tcl-me-remo on`.

### Command line

```
tcl-me-remo status                 what the service is doing
tcl-me-remo on [--no-input]        turn the TV on (and switch input)
tcl-me-remo off                    turn the TV off
tcl-me-remo input                  switch the TV to this machine
tcl-me-remo key HOME               send one key          (tcl-me-remo keys lists them)
tcl-me-remo macro "F3 1.5s LEFT*8 RIGHT*2 OK"
tcl-me-remo pair | forget          pair with the TV / remove the pairing
tcl-me-remo config                 show all settings
tcl-me-remo config input.hdmi_port 2
tcl-me-remo wifi discover | pair <ip> | unpair
tcl-me-remo doctor                 check the set-up; attach this to bug reports
tcl-me-remo logs -n 100
tcl-me-remo uninstall [--purge]    remove the service (asks sudo)
```

`tcl-me-remo doctor` is the panel's *Check the set-up*, `tcl-me-remo logs` its *Log*, and
`tcl-me-remo config` lists every setting with its limits.

### A settings file that was moved aside

A settings file that cannot be read at all (a slip while editing it by hand, usually) is
kept as `/var/lib/tcl-me-remo/config.json.broken`, and the service runs on defaults;
`tcl-me-remo status` then has a `config:` line. To get the old settings back, mend the file,
move it back to `config.json` and restart the service (`sudo systemctl restart tcl-me-remo`).

### Remove from a terminal

However the service was installed:

```sh
tcl-me-remo uninstall            # stops and removes the service; settings and the pairing stay
tcl-me-remo uninstall --purge    # also deletes the settings and the Bluetooth pairing
```

It asks `sudo` (on SteamOS, set a password first with `passwd`). If the shell does not find
the command, it is `/opt/tcl-me-remo/bin/tcl-me-remo`. `./install.sh uninstall` in a
checkout does the same. If the Decky plugin is still installed, uninstall it in Decky too:
it puts the service back whenever it loads. This is also the way out when Decky Loader
itself was removed and left the service behind.

### Build the plugin archive yourself

The archive Decky installs can be made from a checkout with Python alone, which SteamOS has
(the built panel is part of the repository):

```sh
python3 scripts/build_zip.py      # writes out/tcl-me-remo.zip
```

In Decky's Developer page choose **Install Plugin from ZIP File** and pick
`out/tcl-me-remo.zip`. Keep the file name exactly as it is: Decky tells an update from a
fresh install by it.

### Releases

Nobody pushes tags. When a push to `main` carries a version in `package.json` (and
`py_modules/tcl_me_remo/__init__.py`) for which no release exists, and the CI workflow has
passed for that push, the Release workflow publishes release `v<version>` with the one asset
`tcl-me-remo.zip` and its SHA-256 in the notes. The address in [Install](#install) serves the
newest release that is not a draft or a pre-release. So a release is made by changing the
version number in a pull request.

Until the first release exists, that address answers "not found" and the Decky route above
installs nothing. If the Release workflow fails: when CI was the cause, run CI's failed jobs
again and the release follows; otherwise start the Release workflow by hand (*Actions* >
*Release* > *Run workflow*). A release can also be made by hand on GitHub's web page, as
long as its asset is called exactly `tcl-me-remo.zip`; build it as described above.

### Development

```sh
python3 -m pytest -q --timeout 60     # the service; needs dbus-daemon, no Bluetooth
ruff check .
pnpm install && pnpm run build        # the Decky panel; commit the resulting dist/index.js
pnpm run preview                      # UI checks and screenshots in a browser, no Deck needed
python3 scripts/build_zip.py          # out/tcl-me-remo.zip
```

The service is plain Python 3.11+ with one vendored dependency. Tests run it against fake
BlueZ and logind services on a private D-Bus daemon.

## More

- [How it works](docs/how-it-works.md)
- [Setting up the TV](docs/tv-setup.md)
- [Macros and key names](docs/macros.md)
- [Control protocol](docs/control-protocol.md) for scripting
- [Hardware test plan](docs/hardware-testing.md), including how to run a Claude Code session
  on the machine to work through it

## Licence

MIT. The vendored [dbus-fast](https://github.com/Bluetooth-Devices/dbus-fast) is MIT as well.
