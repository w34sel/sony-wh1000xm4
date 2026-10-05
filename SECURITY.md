# Security Policy

## Supported Versions

The current release line is supported for security fixes.

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

If you believe you have found a security vulnerability in sonyctl, please
report it responsibly.

**Preferred:** open a private [Security Advisory](https://github.com/w34sel/sony-wh1000xm4/security/advisories/new)
in this repository. This keeps the issue private until a fix is available.

**Alternative:** if you'd rather not use the advisory flow, email the
maintainer with a clear, non-public description of the issue.

When reporting, please include:

- A summary of the vulnerability and its impact.
- Steps to reproduce, or a minimal example.
- The version you tested on (from `python3 sonyctl.py --version` or the
  release tag).

Please **do not** open a public issue for an unpatched vulnerability.

### Scope

This tool talks to Bluetooth and the Sony WH-1000XM4 protocol over a local,
user-initiated connection. Areas most likely to have meaningful security
properties are:

- The Bluetooth pairing / connection handling in `sonyctl.py`.
- Any handling of data returned by the device (parsing untrusted bytes).
- The settings/autostart and file writes in `sonyctl_gui.py` / `sonyctl_tray.py`.

## What we consider a vulnerability

- Remote or local code execution reachable from parsing device data.
- Unauthorized writes to the user's filesystem via the settings/autostart paths.
- Anything that lets a malicious or compromised Bluetooth peer do more than
  it should against the connected user.

## Response

Aim to acknowledge a valid report within 5 business days and to publish a fix
and advisory within 14 days for high-severity issues. Timing may vary for
lower-severity findings.
