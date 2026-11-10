# tcl-me-remo

A SteamOS machine controls a TCL Google TV by acting as a Bluetooth LE remote control
(HID over GATT), paired once with the TV. A root systemd service turns the TV on (and
optionally switches its input) when the machine boots or wakes, and off when it suspends or
shuts down. A Decky Loader plugin is the Game Mode UI and the installer: people install,
update and remove everything from inside Decky, with no terminal. An opt-in Wi-Fi assist
speaks the Google TV remote protocol.

Read `docs/how-it-works.md` before changing behaviour, and `docs/control-protocol.md` before
touching the CLI or the plugin.

## Layout

- `py_modules/tcl_me_remo/` — the service (Python 3.11+, asyncio, standard library only).
  - `service.py` — every decision: lifecycle events, TV on/off/input sequences. Talks to the
    transports only through `interfaces.py`, so it is fully tested with fakes.
  - `bluez/` — the Bluetooth transport (BlueZ over D-Bus, plus a raw HCI delivery path and
    a kernel management socket for ending the link at once before suspend). `kernel.py`
    there is dependency-free on purpose: the doctor imports it.
  - `atv/` — the Wi-Fi assist.
  - `lifecycle.py`, `daemon.py`, `system.py` — logind locks, wiring, systemd helpers.
  - `config.py`, `keys.py`, `hid.py`, `macro.py`, `tvdata.py` — settings and tables.
  - `installer.py`, `doctor.py`, `cli.py`, `ipc.py` — install, diagnostics, CLI, control socket.
  - `_vendor/dbus_fast/` — vendored, pure Python. Refresh only with `scripts/vendor.sh`.
- `main.py`, `src/`, `plugin.json`, `package.json` — the Decky plugin (backend bridge, React UI).
- `preview/` — the same UI rendered in a browser against mocks and a fake backend; it
  produces `docs/images/` and is the only way to see the panel without a Deck.
- `tests/` — pytest; `fake_bluez.py` and `fake_logind.py` stand in for the system services on
  a private D-Bus daemon.
- `scripts/build_zip.py`, `scripts/release_plan.py`, `.github/workflows/release.yml` — the
  plugin archive and its GitHub release. `release_plan.py` holds every decision the workflow
  makes (`tests/test_release_plan.py`).
- `hardware-results/` — what was observed on real hardware. Add to it, never rewrite history.

## Commands

```sh
python3 -m pytest -q --timeout 60    # needs dbus-daemon; no Bluetooth hardware needed
ruff check .
pnpm install && pnpm run typecheck && pnpm run build    # the plugin frontend
#   dist/index.js is committed (SteamOS has no Node.js); rebuild and commit it with src/ changes
pnpm run preview                     # renders every screen with mocked Decky modules, runs UI checks
python3 scripts/build_zip.py         # out/tcl-me-remo.zip (the name must not change)
./install.sh                         # install this checkout as the running service (sudo)
tcl-me-remo doctor                   # read-only health report
```

## Rules that are easy to break

- **The HID report map and GATT table are frozen** (`hid.py`, `bluez/gatt.py`). A TV caches
  them for a bonded remote and Android before 14 never re-reads them. Changing a byte breaks
  every existing pairing without any error. New keys must fit the declared usage ranges.
- **Do not restart the service or re-register the GATT application without need.** Either one
  makes BlueZ forget the TV's subscription. Settings changes are applied live (the two exceptions,
  `bluetooth.adapter` and `bluetooth.gatt_security`, say so in their help text).
- **Standard library only**, plus the vendored D-Bus library. SteamOS has no pip and a
  read-only root. The plugin backend runs in Decky's own Python, so `ipc.py`, `paths.py`
  and `installer.py` must not import anything heavier.
- **Nothing under `/etc` except systemd units.** SteamOS wipes the rest on OS updates.
  Never edit `/etc/bluetooth/main.conf`, never rename the adapter, never make it discoverable.
- **The install address ends in `tcl-me-remo.zip`.** People type
  `github.com/tiltbob/tcl-me-remo/releases/latest/download/tcl-me-remo.zip` into Decky's
  "Install Plugin from URL". Decky takes the plugin's name from the last part of the address
  and tells an update from a fresh install by it, so the release asset is always the `name`
  in `plugin.json` plus `.zip`, with no version in it. The address is also in the README,
  `src/text.ts` and the docs; a test holds them together.
- **A release is made by raising the version on `main`** (`package.json` and
  `py_modules/tcl_me_remo/__init__.py`, which must agree). The Release workflow publishes
  `v<version>` once CI has passed for that commit. Nobody pushes tags.
- **The Decky route must not need a terminal.** Whatever the panel can show (installer and
  doctor messages included) names the panel's way first and a command second. Removal is
  uninstalling the plugin: the service removes itself when the plugin that installed it is
  gone (`installer.uninstall_if_orphaned`), and never when that cannot be told for certain.
- **Power keys are discrete** (`WAKE`, `SLEEP`). Do not introduce a toggle into an automatic
  path; a toggle sent twice undoes itself and a long or repeated press opens TCL's shutdown
  dialog.
- Facts about TVs in `tvdata.py`, `macro.py` presets and the docs come from owners' reports.
  Keep "reported", "inferred" and "confirmed here" apart, and record confirmations in
  `hardware-results/`.

## If you are running on the SteamOS machine itself

You can do what the cloud sessions that wrote this code could not: test against the real TV.
Follow `docs/hardware-testing.md` from the first test that has no result yet, and write what
you observe to `hardware-results/` as you go.

On this machine:

- Ask before anything that interrupts the person: suspending, rebooting, restarting
  `bluetooth.service` (it disconnects their controllers), or turning the TV off while they
  are using it.
- You cannot see the TV. Ask the person what it shows, or use `adb` if they enabled it.
- Do not run `steamos-readonly disable`, do not install packages with pacman, and do not
  change anything under `/etc` by hand. Everything this project needs goes through
  `./install.sh`.
- SteamOS has no pytest. `scripts/steamos-dev-setup.sh` makes `.venv/` in the checkout with
  the test tools; run the tests with `.venv/bin/python -m pytest -q --timeout 60` before
  installing a change. If there is no `.venv`, say so instead of installing untested code.
- Keep test key presses short and never send `POWER` twice in a row.
- When something does not work, capture evidence before changing code: `tcl-me-remo doctor`,
  `tcl-me-remo logs -n 200`, and a `btmon` trace of the attempt.
