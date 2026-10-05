# Contributing to sonyctl

Thanks for considering a contribution! sonyctl is a small, single-purpose tool:
control and diagnose a Sony WH-1000XM4 over Bluetooth on Linux / SteamOS. Keep
that in mind when proposing changes — small, focused, well-scoped patches are
the easiest to review and merge.

## How to get set up

```bash
git clone https://github.com/w34sel/sony-wh1000xm4.git
cd sony-wh1000xm4

# Run the protocol self-test — it must pass before you open a PR.
python3 sonyctl.py selftest

# Diagnose your local stack (Python Bluetooth support, rfkill, adapter, pairing).
python3 sonyctl.py doctor
```

There is no build step required to develop against the source. The standalone
binary is built with PyInstaller by `./build.sh` and released via GitHub
Actions (`.github/workflows/release.yml`) — you don't need to build it locally
for a normal contribution.

## The code, in one screen

| File | What it owns |
|---|---|
| `sonyctl.py` | The Sony protocol, the Bluetooth link, diagnostics, and the CLI |
| `sonyctl_gui.py` | The Tk window, settings, autostart, auto-reconnect |
| `sonyctl_tray.py` | Tray icon + menu and desktop notifications (D-Bus) |
| `sonyctl_main.py` | Entry point: no args / `gui` → window, otherwise CLI |

Most protocol work lives in `sonyctl.py`. If you touch it, run
`python3 sonyctl.py selftest` and, if you have the headphones handy,
`python3 sonyctl.py doctor` and a live `python3 sonyctl.py battery`.

## Opening a pull request

1. Fork the repo and create a feature branch off `main`.
2. Make your change. Keep the commit messages clear and scoped.
3. Run the self-test: `python3 sonyctl.py selftest`.
4. Push the branch and open a PR. Fill in the PR template — it tells reviewers
   what they need to know.

A good PR:
- Changes one thing, and does it well.
- Includes the output of `python3 sonyctl.py selftest` (and `doctor` if you
  touched diagnostics or the link).
- Notes any new dependency. sonyctl is intentionally standard-library-only for
  the source tool — if a change adds a runtime dependency, say so explicitly.
- Updates the README if user-visible behavior changed.

## Style

- Match the surrounding code. Python 3, standard library, no type-annotation
  theater, no new dependencies unless they earn their keep.
- Log through the module logger (see how `sonyctl.py` does it); don't sprinkle
  `print` into the link/protocol path.
- Keep it readable. This codebase is meant to be read by someone debugging their
  own Bluetooth stack at 1 a.m.

## Reporting a bug

Open an issue using the **Bug report** template. The single most useful thing
you can include is the full output of:

```bash
python3 sonyctl.py doctor
```

It reports PASS/WARN/FAIL for each layer (Python Bluetooth support → rfkill →
adapter powered → headphones paired → Sony service advertised → control channel
→ live connect → handshake), which is almost always enough to locate the problem.

## Questions

Open an issue with the "question" label, or just start a PR and mark it a draft.
