# sonyctl — Sony WH-1000XM4 control & debugging for Linux / SteamOS

A single Python file, standard library only. Runs on a stock Steam Deck (Desktop Mode → Konsole) or any Linux PC with BlueZ.

## Setup

1. Pair the headphones normally (Steam Deck: Settings → Bluetooth).
2. Copy `sonyctl.py` to your home folder, e.g. `~/sonyctl/sonyctl.py`.
3. Run the checks:

```bash
python3 ~/sonyctl/sonyctl.py doctor
```

`doctor` walks through every layer and prints PASS / WARN / FAIL for each:
Python Bluetooth support → rfkill → adapter powered → headphones paired/connected →
Sony service advertised → control channel found → live connect → protocol handshake.
The first FAIL is where to look.

**Tip:** close the Sony Headphones Connect app on your phone (or turn the phone's Bluetooth off) while testing. It can hold the control channel.

## Standalone binary

`sonyctl` is a single executable for x86_64 Linux. Python and Tk are bundled inside it, so nothing else needs to be installed. It works on CachyOS/Arch, Ubuntu 24.04+, and SteamOS in Desktop Mode.

```bash
tar xzf sonyctl-linux-x86_64.tar.gz && cd sonyctl
./install.sh            # copies it to ~/.local/bin and adds "Sony XM4 Control" to the app menu
```

```bash
sonyctl                 # opens the window
sonyctl gui --demo      # window with simulated headphones
sonyctl battery         # any command-line command works the same as python3 sonyctl.py ...
```

The first start takes a second or two while the bundled files unpack.

### Building it yourself

```bash
./build.sh        # creates dist/sonyctl (needs Python with Tk; installs PyInstaller into a local venv)
./install.sh      # installs dist/sonyctl
```

## Repository layout

| File | Purpose |
|---|---|
| `sonyctl.py` | Protocol, Bluetooth link, diagnostics and the command-line tool |
| `sonyctl_gui.py` | The Tk window (runs the link on a background thread) |
| `sonyctl_main.py` | Entry point for the binary: no args / `gui` → window, otherwise CLI |
| `build.sh` | Builds the single-file binary with PyInstaller |
| `install.sh` | Installs the binary to `~/.local/bin` and adds an app-menu entry |

Run `python3 sonyctl.py selftest` after changing protocol code.

## The window (GUI)

Keep `sonyctl_gui.py` in the same folder as `sonyctl.py`, then:

```bash
python3 sonyctl_gui.py          # real headphones (press Connect)
python3 sonyctl_gui.py --demo   # simulated headphones, to try the window first
python3 sonyctl_gui.py --connect  # connect automatically on start
```

- **Battery** bar (green/amber/red) with charging state. It updates by itself when the headphones report a change.
- **Sound control:** Noise canceling / Ambient sound / Off. The ambient level slider and "Focus on voice" are active in Ambient mode. Pressing the NC button on the headphones updates the window too.
- **Log tab:** live log, with *Detail* switching between normal and raw-byte view, plus Clear / Copy (to paste to me) / Open log (opens the log folder).
- **Raw command tab:** send any payload and see the decoded reply.
- MAC and channel fields: leave blank to auto-detect.

The window needs Tk. If it says Tk is missing: on Debian/Ubuntu run `sudo apt install python3-tk`. On Arch run `sudo pacman -S tk`. On SteamOS the system folder is read-only by default, so if Tk isn't already there, use the command-line tool or ask me for a browser-based version.

**On Steam Deck in Game Mode:** in Desktop Mode, right-click the file → *Add to Steam*. Then in its Steam properties set the target to `python3` and the launch options to the full path of `sonyctl_gui.py`. Or use the MagicPods-style Decky route later.

## Commands

| Command | What it does |
|---|---|
| `doctor [--quick]` | Step-by-step diagnosis (`--quick` skips the live connection) |
| `devices` | List paired devices, flag those with the Sony service |
| `find-channel` | Locate and cache the Sony RFCOMM control channel |
| `battery` | Battery level and charging state |
| `status` | Current noise canceling / ambient mode |
| `anc nc` / `anc ambient --level 15 --voice` / `anc off` | Change mode |
| `firmware` | Firmware version |
| `listen [--seconds N]` | Print everything the headphones send (press buttons, change modes, watch) |
| `raw "10 00" --expect 0x11` | Send any payload, for experiments |
| `decode "<hex>"` | Decode hex copied from a log, offline |
| `logs [-n 100]` | Show the end of the debug log |
| `selftest` | Offline tests of the protocol code |

Global options: `--mac AA:BB:..`, `--channel N`, `--timeout S`, `--no-init`, `-v`, `-vv`, `--log-file PATH`, `--no-log-file`.

## Logging and debugging

- **Console:** quiet by default. `-v` shows protocol steps (TX/RX decoded), `-vv` shows every raw byte.
- **Log file:** always written at full detail to `~/.local/state/sonyctl/sonyctl.log` (rotates at 1 MB, keeps 3 old files). Each run starts with a header line (version, Python, OS, arguments).
- **What gets logged:** every command run against `bluetoothctl`/`sdptool` and its output, connect timing, every frame sent and received (raw hex + type, sequence, checksum, decoded meaning), ACKs, stray bytes, checksum errors, reply latency, and link statistics on close.
- **Errors** come with a plain-language hint (e.g. "refused on this channel: wrong channel, or the phone app holds the connection").
- **Exit codes:** 0 ok, 1 error, 2 timeout (headphones didn't answer), 3 environment problem.
- The detected control channel is cached in `~/.local/state/sonyctl/channels.json`. Delete it to force re-detection.

### When something doesn't work

```bash
python3 sonyctl.py -vv battery        # watch the bytes live
python3 sonyctl.py logs -n 150        # or read the log afterwards
```

Send me the log output. It shows exactly what was sent and what came back, which is what I need to fix the protocol bytes.

## Status of the protocol

Framing and the init/battery/mode commands follow community reverse-engineering of Sony's protocol (Gadgetbridge, SonyHeadphonesClient). The code is tested offline, but **it hasn't been run against real WH-1000XM4 headphones yet**. Expect to adjust command bytes after the first real test. `listen` and `raw` exist for exactly that. `anc wind` is experimental and may not be supported on the XM4.
