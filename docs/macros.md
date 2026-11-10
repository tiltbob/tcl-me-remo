# Macros and key names

A macro is a list of steps, separated by spaces or commas, that the machine plays to the TV
as if someone pressed the remote.

| Step | Meaning |
|---|---|
| `HOME` | press and release a key |
| `DOWN*3` | press it three times |
| `HOME~1.5` | hold it for 1.5 seconds (a long press) |
| `2s`, `300ms` | wait |

`{port}` stands for the configured HDMI port number, and `{port-1}` / `{port+1}` for one less
or more, so one macro can serve any port: `RIGHT*{port}`.

Example: `F3 1.5s LEFT*8 RIGHT*2 OK` — press F3, wait a second and a half, press Left eight
times, Right twice, then OK.

Try a macro before saving it:

```sh
tcl-me-remo macro "F3 1.5s LEFT*8 RIGHT*{port} OK"
```

Between keys the machine pauses for `bluetooth.key_gap_ms` (350 ms by default) so the TV's
menus can keep up. A macro may have at most 64 steps and run for two minutes.

## Switching inputs with a macro

Android TVs have no key for "go to HDMI 2" that a generic remote can send, so a macro has to
walk through the TV's source list. TCL's list does not wrap around at the ends, which makes
a reliable recipe possible:

1. open the list;
2. press Left (or Up) more times than the list has entries, so the cursor is at the first
   entry wherever it started;
3. press Right (or Down) the right number of times;
4. press OK.

Built-in presets (`input.preset`):

| Preset | Macro | For |
|---|---|---|
| `tcl-row` | `F3 1.5s LEFT*8 RIGHT*{port} OK` | 2023 and newer firmware: a horizontal row with one entry (TV) before HDMI 1 |
| `tcl-row-home` | `HOME 2s F3 1.5s LEFT*8 RIGHT*{port} OK` | the same, starting from the home screen |
| `tcl-row-first` | `F3 1.5s LEFT*8 RIGHT*{port-1} OK` | a row that starts with HDMI 1 |
| `tcl-column` | `F3 1.5s UP*8 DOWN*{port} OK` | 2022 firmware: a vertical list |
| `custom` | whatever is in `input.macro` | everything else |

These are starting points taken from owners' reports, not confirmed layouts. To find out
what your TV needs:

1. Switch the TV to some other input with its own remote.
2. Find the key that opens the source list: `tcl-me-remo key F3`. If nothing opens, try
   `tcl-me-remo key TV`.
3. Look at the list: is it a row or a column, what comes before HDMI 1, and are there
   entries you never use? Hiding unused inputs in the TV's settings (and setting the others
   to *On* rather than *Auto*) keeps the list from changing length.
4. Adjust the counts and test with `tcl-me-remo macro "…"` until it lands on your input from
   any starting point.
5. Save it: `tcl-me-remo config input.macro "…"`, `tcl-me-remo config input.preset custom`,
   `tcl-me-remo config input.method macro`.

If no key opens the list from this remote on your TV, use the Wi-Fi assist for input
switching, or rely on the TV's *Power On Behavior: Last Input Used*.

`input.delay_after_wake` (8 s by default) is how long the machine waits after turning the TV
on before it starts the macro. A TV that has just woken ignores keys for a few seconds.

## Key names

`tcl-me-remo keys` prints this list.

| Name | Sent as | TV sees |
|---|---|---|
| `WAKE` | System Wake Up | wake (does nothing if already on) |
| `SLEEP` | Consumer Sleep | sleep (does nothing if already off) |
| `POWER` | System Power Down | power toggle; avoid in anything automatic |
| `UP` `DOWN` `LEFT` `RIGHT` `OK` | Consumer menu keys | D-pad and select |
| `BACK` `HOME` `MENU` | Consumer | Back, Home, Menu |
| `ENTER` `ESC` | Keyboard | Enter, Escape |
| `TV` `GUIDE` `SEARCH` `CH_UP` `CH_DOWN` | Consumer | TV, Guide, Search, channel keys |
| `VOL_UP` `VOL_DOWN` `MUTE` | Consumer | volume |
| `PLAY_PAUSE` `STOP` `NEXT` `PREVIOUS` `REWIND` `FAST_FORWARD` | Consumer | media keys |
| `NUM0` … `NUM9` | Keyboard | digits |
| `F1` … `F12` | Keyboard | function keys (`F3` is reported to open TCL's source list) |

Some friendlier spellings are accepted too (`SELECT`, `VOL+`, `WAKEUP`, …).

### Raw usages

Any other HID usage can be sent without changing the software, which helps when hunting for
the key a particular firmware responds to:

| Form | Page | Range |
|---|---|---|
| `C:0223` | Consumer | `0001`–`03FF` |
| `K:3C` | Keyboard | `1E`–`F1` (no letters) |
| `S:83` | System Control | `81`–`83` |

For example `C:01BB` is the "Input" key of Google's reference remote and `C:0055` the
settings key of some TCL remotes. Whether the TV maps them depends on its key layout; an
unmapped usage is ignored.

On an Android TV every key from a remote also wakes the screen. Nothing is ever sent on its
own apart from the power keys and the macro you configured.
