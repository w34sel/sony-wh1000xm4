#!/usr/bin/env python3
"""
sonyctl - a debug-friendly command-line controller for Sony WH-1000XM4
headphones on Linux / SteamOS, speaking Sony's Bluetooth RFCOMM protocol.

* Python standard library only (runs on a stock Steam Deck).
* Every byte sent and received is logged, so problems can be diagnosed.
* Protocol details come from community reverse-engineering (Gadgetbridge,
  SonyHeadphonesClient). They are best-effort: if a command misbehaves,
  the log shows exactly what went over the wire.

Run `python3 sonyctl.py doctor` first.
"""
from __future__ import annotations

import argparse
import errno
import json
import logging
import logging.handlers
import os
import platform
import re
import select
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

VERSION = "0.1.0"

# Vendor service the Headphones Connect app talks to.
SONY_UUID = "96cc203e-5068-46ad-b32d-e316f5e069ba"
NAME_HINTS = ("WH-1000XM4", "WH-1000XM", "LE_WH-1000XM")

# Framing: 3e <escaped body> 3c, where body = type, seq, len(4, BE), payload, checksum
START, END, ESC = 0x3E, 0x3C, 0x3D

DT_DATA, DT_ACK, DT_MDR, DT_MDR2 = 0x00, 0x01, 0x0C, 0x0E
DATA_TYPES = {DT_DATA: "DATA", DT_ACK: "ACK", DT_MDR: "MDR", DT_MDR2: "MDR2"}

CMD_NAMES = {
    0x00: "INIT_REQUEST", 0x01: "INIT_REPLY",
    0x04: "FW_REQUEST", 0x05: "FW_REPLY",
    0x10: "BATTERY_REQUEST", 0x11: "BATTERY_REPLY", 0x13: "BATTERY_NOTIFY",
    0x66: "ASM_GET", 0x67: "ASM_REPLY", 0x68: "ASM_SET", 0x69: "ASM_NOTIFY",
}

EXIT_OK, EXIT_ERROR, EXIT_TIMEOUT, EXIT_ENV = 0, 1, 2, 3

# How to number our ACKs: "flip" = 1 - received seq (usual), "same" = echo it. Set by --ack-seq.
ACK_MODE = "flip"

log = logging.getLogger("sonyctl")


# --------------------------------------------------------------------------- paths

def state_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "sonyctl")


DEFAULT_LOG = os.path.join(state_dir(), "sonyctl.log")
CACHE_FILE = os.path.join(state_dir(), "channels.json")


# --------------------------------------------------------------------------- helpers

def hexs(data: bytes) -> str:
    return " ".join(f"{b:02x}" for b in data)


def parse_hex(text: str) -> bytes:
    cleaned = re.sub(r"0x|[\s,:]", "", text, flags=re.I)
    if len(cleaned) % 2 or not re.fullmatch(r"[0-9a-fA-F]*", cleaned):
        raise ValueError(f"not a valid hex string: {text!r}")
    return bytes.fromhex(cleaned)


class LinkError(Exception):
    pass


class LinkTimeout(LinkError):
    pass


class FrameError(Exception):
    def __init__(self, msg: str, raw: bytes = b""):
        super().__init__(msg)
        self.raw = raw


# --------------------------------------------------------------------------- framing

def escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b in (START, END, ESC):
            out += bytes([ESC, b & 0xEF])
        else:
            out.append(b)
    return bytes(out)


def unescape(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == ESC:
            i += 1
            if i >= len(data):
                raise FrameError("escape byte at end of frame", data)
            out.append(data[i] | 0x10)
        else:
            out.append(b)
        i += 1
    return bytes(out)


def encode_frame(dtype: int, seq: int, payload: bytes) -> bytes:
    body = bytes([dtype, seq]) + len(payload).to_bytes(4, "big") + payload
    checksum = sum(body) & 0xFF
    return bytes([START]) + escape(body + bytes([checksum])) + bytes([END])


@dataclass
class Frame:
    dtype: int
    seq: int
    payload: bytes
    checksum_ok: bool
    raw: bytes
    expected_checksum: int = 0
    got_checksum: int = 0

    @property
    def type_name(self) -> str:
        return DATA_TYPES.get(self.dtype, f"0x{self.dtype:02x}")


def decode_frame(raw: bytes) -> Frame:
    if len(raw) < 2 or raw[0] != START or raw[-1] != END:
        raise FrameError("missing start/end marker", raw)
    body = unescape(raw[1:-1])
    if len(body) < 7:
        raise FrameError(f"frame too short ({len(body)} bytes after unescaping)", raw)
    size = int.from_bytes(body[2:6], "big")
    payload = body[6:-1]
    if len(payload) != size:
        raise FrameError(f"length field says {size} bytes but payload has {len(payload)}", raw)
    expected = sum(body[:-1]) & 0xFF
    return Frame(body[0], body[1], payload, expected == body[-1], raw, expected, body[-1])


class FrameReader:
    """Reassembles frames from a byte stream that may arrive in pieces."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list:
        self.buf += data
        out: list = []
        while True:
            start = self.buf.find(bytes([START]))
            if start == -1:
                if self.buf:
                    log.warning("RX discarding %d stray bytes: %s", len(self.buf), hexs(self.buf))
                    self.buf.clear()
                break
            if start > 0:
                log.warning("RX discarding %d stray bytes before frame: %s", start, hexs(self.buf[:start]))
                del self.buf[:start]
            end = self.buf.find(bytes([END]), 1)
            if end == -1:
                break  # wait for more data
            raw = bytes(self.buf[: end + 1])
            del self.buf[: end + 1]
            try:
                out.append(decode_frame(raw))
            except FrameError as e:
                out.append(e)
        return out


# --------------------------------------------------------------------------- payloads

ASM_MODES = {"nc": 0x02, "wind": 0x01, "ambient": 0x00, "off": 0x00}


def build_asm(mode: str, level: int, voice: bool) -> bytes:
    effect = 0x00 if mode == "off" else 0x11
    return bytes([0x68, 0x02, effect, 0x02, ASM_MODES[mode], 0x01, 0x01 if voice else 0x00, level])


def describe_frame(f: "Frame") -> str:
    if f.dtype == DT_MDR:
        return describe_payload(f.payload)
    return f"[{f.type_name} frame, not MDR] {hexs(f.payload)}"


def describe_payload(payload: bytes) -> str:
    if not payload:
        return "(empty payload)"
    cmd = payload[0]
    name = CMD_NAMES.get(cmd, f"UNKNOWN_0x{cmd:02x}")
    try:
        if cmd in (0x11, 0x13) and len(payload) >= 4:
            kind = {0x00: "headphones", 0x01: "left/right", 0x02: "case"}.get(payload[1], f"type 0x{payload[1]:02x}")
            if payload[1] == 0x01 and len(payload) >= 6:
                return f"{name}: L {payload[2]}%, R {payload[4]}%"
            charging = "charging" if payload[3] == 0x01 else "not charging"
            return f"{name}: {kind} battery {payload[2]}% ({charging})"
        if cmd in (0x67, 0x68, 0x69) and len(payload) >= 8:
            if payload[2] == 0x00:
                mode = "off"
            else:
                mode = {0x02: "noise canceling", 0x01: "wind noise reduction", 0x00: "ambient sound"}.get(
                    payload[4], f"mode 0x{payload[4]:02x}")
            voice = "on" if payload[6] else "off"
            return f"{name}: {mode}, ambient level {payload[7]}, focus on voice {voice}"
        if cmd == 0x05 and len(payload) >= 3:
            n = payload[2]
            text = payload[3:3 + n].decode("ascii", errors="replace")
            return f"{name}: firmware {text}"
    except IndexError:
        pass
    return f"{name}: {hexs(payload)}"


# --------------------------------------------------------------------------- errors

def explain_oserror(e: OSError) -> str:
    hints = {
        errno.EHOSTDOWN: "headphones not reachable: powered off, out of range, or not paired with this machine",
        errno.ECONNREFUSED: "refused on this channel: wrong RFCOMM channel, or another device holds the Sony "
                            "service (close Headphones Connect on your phone or turn the phone's Bluetooth off)",
        errno.EBUSY: "adapter or device busy: retry in a few seconds",
        errno.ETIMEDOUT: "timed out: wake the headphones and keep them close",
        errno.EACCES: "permission denied: check that your user may use Bluetooth",
        errno.EPERM: "operation not permitted: check that your user may use Bluetooth",
        errno.ENODEV: "no Bluetooth adapter found or it is turned off",
        errno.EADDRNOTAVAIL: "no Bluetooth adapter found or it is turned off",
        errno.ECONNRESET: "the headphones dropped the connection",
        errno.EAFNOSUPPORT: "this kernel/environment has no Bluetooth support (container, VM or WSL?)",
    }
    if isinstance(e, socket.timeout) or e.errno is None:
        return f"{e or 'timed out'}: timed out; wake the headphones and keep them close"
    return f"{e.strerror or e} [errno {e.errno}]: {hints.get(e.errno, 'see log for details')}"


# --------------------------------------------------------------------------- link

class SonyLink:
    def __init__(self, mac: str, channel: int, timeout: float = 5.0, retries: int = 3):
        self.mac, self.channel, self.timeout, self.retries = mac, channel, timeout, max(1, retries)
        self.sock: socket.socket | None = None
        self.seq = 0
        self.reader = FrameReader()
        self.inbox: list[Frame] = []
        self.stats = {"tx": 0, "rx": 0, "bad_frames": 0, "acks_sent": 0}

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self) -> None:
        log.info("connecting to %s, RFCOMM channel %d", self.mac, self.channel)
        t0 = time.monotonic()
        s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        s.settimeout(self.timeout)
        try:
            s.connect((self.mac, self.channel))
        except OSError as e:
            s.close()
            log.debug("connect failed after %.0f ms: %r", (time.monotonic() - t0) * 1000, e)
            raise LinkError(explain_oserror(e)) from e
        log.info("connected in %.0f ms", (time.monotonic() - t0) * 1000)
        self.sock = s
        self.settle()

    def settle(self, max_wait: float = 1.0, quiet: float = 0.3) -> None:
        """Read and acknowledge anything the headphones send right after connecting,
        before we send our first command."""
        t0 = time.monotonic()
        end = t0 + max_wait
        got = 0
        while time.monotonic() < end:
            frames = self._read_some(min(end, time.monotonic() + quiet))
            if not frames:
                if got:
                    break
                continue
            got += len(frames)
            self.inbox += [f for f in frames if f.dtype != DT_ACK]
        log.debug("settle: %d greeting frame(s) in %.0f ms", got, (time.monotonic() - t0) * 1000)

    def close(self) -> None:
        if self.sock:
            log.debug("closing link; stats %s", self.stats)
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _write(self, data: bytes, what: str) -> None:
        log.debug("TX %s | raw: %s", what, hexs(data))
        try:
            self.sock.sendall(data)
        except OSError as e:
            raise LinkError(explain_oserror(e)) from e
        self.stats["tx"] += 1

    def _ack(self, frame: Frame) -> None:
        seq = 1 - frame.seq if ACK_MODE == "flip" else frame.seq
        self._write(encode_frame(DT_ACK, seq, b""), f"ACK seq={seq}")
        self.stats["acks_sent"] += 1

    def _read_some(self, deadline: float | None) -> list[Frame]:
        wait = None if deadline is None else max(0.0, deadline - time.monotonic())
        ready, _, _ = select.select([self.sock], [], [], wait)
        if not ready:
            return []
        try:
            data = self.sock.recv(4096)
        except OSError as e:
            raise LinkError(explain_oserror(e)) from e
        if not data:
            raise LinkError("the headphones closed the connection")
        log.debug("RX bytes: %s", hexs(data))
        frames = []
        for item in self.reader.feed(data):
            if isinstance(item, FrameError):
                self.stats["bad_frames"] += 1
                log.warning("RX bad frame (%s): %s", item, hexs(item.raw))
                continue
            self.stats["rx"] += 1
            log.debug("RX frame type=%s seq=%d len=%d payload=%s checksum=%s",
                      item.type_name, item.seq, len(item.payload), hexs(item.payload),
                      "ok" if item.checksum_ok else f"BAD (expected {item.expected_checksum:02x}, "
                                                    f"got {item.got_checksum:02x})")
            if not item.checksum_ok:
                log.warning("RX checksum mismatch; frame kept for inspection: %s", hexs(item.raw))
            if item.dtype == DT_ACK:
                log.debug("RX ACK seq=%d", item.seq)
            else:
                log.info("RX %s", describe_frame(item))
                if item.checksum_ok:
                    self._ack(item)
            frames.append(item)
        return frames

    def send(self, payload: bytes, dtype: int = DT_MDR, wait_ack: bool = True) -> None:
        log.info("TX %s", describe_payload(payload))
        frame = encode_frame(dtype, self.seq, payload)
        what = f"type={DATA_TYPES.get(dtype, hex(dtype))} seq={self.seq} payload={hexs(payload)}"
        if not wait_ack:
            self._write(frame, what)
            return
        t0 = time.monotonic()
        per_try = max(0.5, self.timeout / self.retries)
        for attempt in range(1, self.retries + 1):
            self._write(frame, what + (f" (retry {attempt - 1})" if attempt > 1 else ""))
            deadline = time.monotonic() + per_try
            while time.monotonic() < deadline:
                acked = False
                for f in self._read_some(deadline):
                    if f.dtype == DT_ACK and not acked:
                        acked = True
                        self.seq = f.seq
                    else:
                        self.inbox.append(f)
                if acked:
                    log.debug("ACK received after %.0f ms (attempt %d); next seq=%d",
                              (time.monotonic() - t0) * 1000, attempt, self.seq)
                    return
            log.debug("no ACK after %.1fs (attempt %d of %d)", per_try, attempt, self.retries)
        raise LinkTimeout(f"no ACK after {self.retries} attempts ({self.timeout:.1f}s) for payload {hexs(payload)}; "
                          "the headphones ignored the frame (wrong channel? try `services`)")

    def request(self, payload: bytes, expect: int, dtype: int = DT_MDR) -> Frame:
        self.send(payload, dtype)
        t0 = time.monotonic()
        deadline = t0 + self.timeout
        while True:
            for i, f in enumerate(self.inbox):
                if f.payload[:1] == bytes([expect]):
                    log.debug("reply 0x%02x after %.0f ms", expect, (time.monotonic() - t0) * 1000)
                    return self.inbox.pop(i)
            if time.monotonic() >= deadline:
                break
            for f in self._read_some(deadline):
                if f.dtype != DT_ACK:
                    self.inbox.append(f)
        raise LinkTimeout(f"no {CMD_NAMES.get(expect, hex(expect))} within {self.timeout:.1f}s "
                          f"(request {hexs(payload)}); the model may use a different command")

    def listen(self, seconds: float | None):
        deadline = None if seconds is None else time.monotonic() + seconds
        while deadline is None or time.monotonic() < deadline:
            for f in self._read_some(deadline):
                if f.dtype != DT_ACK:
                    yield f

    def init(self) -> None:
        try:
            self.request(bytes([0x00, 0x00]), expect=0x01)
            log.info("init handshake ok")
        except LinkTimeout as e:
            log.warning("init handshake got no reply (%s); continuing anyway", e)


# --------------------------------------------------------------------------- system tools

def run(cmd: list[str], timeout: float = 10.0) -> tuple[int, str]:
    log.debug("exec: %s", " ".join(cmd))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        log.debug("exec: %s not found", cmd[0])
        return 127, ""
    except subprocess.TimeoutExpired:
        log.debug("exec: %s timed out", cmd[0])
        return 124, ""
    out = (p.stdout or "") + (p.stderr or "")
    log.debug("exec rc=%d output:\n%s", p.returncode, out.strip())
    return p.returncode, out


MAC_RE = re.compile(r"([0-9A-F]{2}(?::[0-9A-F]{2}){5})", re.I)


def paired_devices() -> list[tuple[str, str]]:
    for cmd in (["bluetoothctl", "devices", "Paired"], ["bluetoothctl", "paired-devices"]):
        rc, out = run(cmd)
        devs = []
        for line in out.splitlines():
            m = re.match(r"\s*Device\s+" + MAC_RE.pattern + r"\s+(.*)", line, re.I)
            if m:
                devs.append((m.group(1).upper(), m.group(2).strip()))
        if rc == 0 and devs:
            return devs
    return []


def device_info(mac: str) -> dict:
    rc, out = run(["bluetoothctl", "info", mac])
    info: dict = {"rc": rc, "uuids": []}
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("UUID:"):
            info["uuids"].append(line[5:].strip().lower())
        elif ":" in line:
            k, v = line.split(":", 1)
            info[k.strip()] = v.strip()
    info["sony_service"] = any(SONY_UUID in u for u in info["uuids"])
    return info


def auto_mac() -> str | None:
    devs = paired_devices()
    log.debug("paired devices: %s", devs)
    for mac, name in devs:
        if any(h.lower() in name.lower() for h in NAME_HINTS):
            log.info("auto-selected %s (%s)", mac, name)
            return mac
    for mac, name in devs:
        if device_info(mac)["sony_service"]:
            log.info("auto-selected %s (%s), advertises the Sony service", mac, name)
            return mac
    return None


def load_cache() -> dict:
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_cache(mac: str, channel: int) -> None:
    data = load_cache()
    data[mac] = channel
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        log.debug("cached channel %d for %s", channel, mac)
    except OSError as e:
        log.debug("could not write channel cache: %s", e)


def channel_from_sdptool(mac: str) -> int | None:
    if not shutil.which("sdptool"):
        log.debug("sdptool not installed; skipping SDP lookup")
        return None
    rc, out = run(["sdptool", "records", mac], timeout=20)
    for block in re.split(r"\n\s*\n", out):
        if SONY_UUID in block.lower():
            m = re.search(r"Channel:\s*(\d+)", block)
            if m:
                log.info("SDP: Sony service on channel %s", m.group(1))
                return int(m.group(1))
    log.debug("SDP lookup did not find the Sony service (rc=%d)", rc)
    return None


def channel_from_bluez_cache(mac: str) -> int | None:
    """Read the channel from BlueZ's on-disk copy of the device's SDP records.

    BlueZ stores them in /var/lib/bluetooth/<adapter>/cache/<device> as hex.
    That folder is often readable only by root, so this can fail quietly.
    """
    import glob
    sony_hex = SONY_UUID.replace("-", "")
    paths = glob.glob(f"/var/lib/bluetooth/*/cache/{mac.upper()}")
    if not paths:
        log.debug("BlueZ cache: no file for %s (or folder not readable)", mac)
        return None
    for path in paths:
        try:
            with open(path, encoding="ascii", errors="replace") as f:
                text = f.read()
        except OSError as e:
            log.debug("BlueZ cache: cannot read %s: %s", path, e)
            continue
        for line in text.splitlines():
            if "=" not in line:
                continue
            record = line.split("=", 1)[1].strip().lower()
            if sony_hex not in record:
                continue
            # SDP encoding of "RFCOMM (UUID16 0x0003), channel uint8": 19 00 03 08 <ch>
            m = re.search(r"19000308([0-9a-f]{2})", record)
            if m:
                ch = int(m.group(1), 16)
                log.info("BlueZ cache: Sony service on channel %d (%s)", ch, path)
                return ch
    log.debug("BlueZ cache: Sony service record not found in %s", paths)
    return None


KNOWN_UUIDS = {
    SONY_UUID: "Sony MDR control (the one we want)",
    "956c7b26-d49a-4ba8-b03f-b17d393cb6e2": "Sony MDR v2 control",
    "df21fe2c-2515-4fdb-8886-f12c4d67927c": "Google Fast Pair message stream",
    "1101": "Serial Port", "111e": "Hands-Free", "111f": "Hands-Free Gateway",
    "1108": "Headset", "110b": "Audio Sink", "110e": "AV Remote Control",
}


def _fmt_uuid128(h: str) -> str:
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


def bluez_services(mac: str) -> tuple[list[dict], str]:
    """List every SDP record BlueZ cached for the device: UUIDs and RFCOMM channel."""
    import glob
    paths = glob.glob(f"/var/lib/bluetooth/*/cache/{mac.upper()}")
    if not paths:
        exists = os.path.isdir("/var/lib/bluetooth")
        return [], ("no cache file found" if exists and os.access("/var/lib/bluetooth", os.R_OK)
                    else "BlueZ cache not readable; run with sudo")
    out = []
    for path in paths:
        try:
            with open(path, encoding="ascii", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError as e:
            return [], f"cannot read {path}: {e}"
        for line in lines:
            if not line.startswith("0x") or "=" not in line:
                continue
            handle, rec = line.split("=", 1)
            rec = rec.strip().lower()
            uuids = [_fmt_uuid128(m) for m in re.findall(r"1c([0-9a-f]{32})", rec)]
            uuids += re.findall(r"19([0-9a-f]{4})", rec)
            m = re.search(r"19000308([0-9a-f]{2})", rec)
            out.append({"handle": handle, "channel": int(m.group(1), 16) if m else None,
                        "uuids": [u for u in dict.fromkeys(uuids) if u not in ("0003", "0100", "1002")]})
    return out, paths[0]


def cmd_services(args) -> int:
    mac = (args.mac or "").upper() or auto_mac()
    if not mac:
        print("No headphones found; pass --mac")
        return EXIT_ENV
    recs, where = bluez_services(mac)
    if not recs:
        print(f"No service list: {where}")
        print(f"Try: sudo python3 {sys.argv[0]} --mac {mac} services")
        return EXIT_ENV
    print(f"Services of {mac} (from {where}):")
    for r in recs:
        if r["channel"] is None and not any(len(u) > 4 for u in r["uuids"]):
            continue
        names = [f"{u} ({KNOWN_UUIDS[u]})" if u in KNOWN_UUIDS else u for u in r["uuids"]]
        ch = f"RFCOMM {r['channel']:>2}" if r["channel"] is not None else "no RFCOMM"
        print(f"  {ch}  {', '.join(names) or '?'}")
    return EXIT_OK


def lookup_channel(mac: str) -> int | None:
    """Fast lookups first (BlueZ cache, sdptool); no probing."""
    return channel_from_bluez_cache(mac) or channel_from_sdptool(mac)


def probe_channels(mac: str, channels=range(1, 31)) -> int | None:
    """Try each RFCOMM channel; keep the first that answers the init handshake with
    INIT_REPLY. Other Sony services use the same framing but don't reply to init,
    so only the real control channel passes."""
    for ch in channels:
        link = SonyLink(mac, ch, timeout=2.0, retries=1)
        try:
            link.connect()
        except LinkError as e:
            log.debug("probe ch %2d: %s", ch, e)
            continue
        try:
            link.request(bytes([0x00, 0x00]), expect=0x01)
            log.info("probe: channel %d answers the init handshake", ch)
            return ch
        except LinkError as e:
            log.info("probe ch %2d: connects but is not the control service (%s)", ch, str(e).split(";")[0])
        finally:
            link.close()
    return None


def resolve_target(args) -> tuple[str, int]:
    mac = (args.mac or "").upper() or auto_mac()
    if not mac:
        raise LinkError("no headphones found; pair them first or pass --mac XX:XX:XX:XX:XX:XX")
    if not MAC_RE.fullmatch(mac):
        raise LinkError(f"not a valid MAC address: {mac}")
    if args.channel:
        return mac, args.channel
    cached = load_cache().get(mac)
    if cached:
        log.info("using cached channel %d for %s (delete %s to reset)", cached, mac, CACHE_FILE)
        return mac, int(cached)
    ch = lookup_channel(mac)
    if ch is None:
        log.warning("looking for the Sony channel by probing 1-30; this can take a minute")
        ch = probe_channels(mac)
    if ch is None:
        raise LinkError("could not find the Sony control channel; run `doctor`, then retry with --channel N")
    save_cache(mac, ch)
    return mac, ch


def open_link(args) -> SonyLink:
    mac, ch = resolve_target(args)
    link = SonyLink(mac, ch, args.timeout, args.retries)
    link.connect()
    if not args.no_init:
        link.init()
    return link


# --------------------------------------------------------------------------- commands

def cmd_doctor(args) -> int:
    results: list[tuple[str, str, str]] = []

    def add(status, name, detail=""):
        results.append((status, name, detail))
        log.debug("doctor: %s %s %s", status, name, detail)

    osr = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip().split("=", 1)
                    osr[k] = v.strip('"')
    except OSError:
        pass
    add("INFO", "System", f"{osr.get('PRETTY_NAME', platform.platform())}, Python {platform.python_version()}")

    if platform.system() != "Linux":
        add("FAIL", "Linux required", "this tool uses Linux Bluetooth sockets")
    if hasattr(socket, "AF_BLUETOOTH") and hasattr(socket, "BTPROTO_RFCOMM"):
        try:
            socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM).close()
            add("PASS", "Bluetooth sockets", "Python can open RFCOMM sockets")
        except OSError as e:
            add("FAIL", "Bluetooth sockets", explain_oserror(e))
    else:
        add("FAIL", "Bluetooth sockets", "this Python build has no AF_BLUETOOTH support")

    if shutil.which("rfkill"):
        rc, out = run(["rfkill", "list", "bluetooth"])
        if re.search(r"(Soft|Hard) blocked:\s*yes", out):
            add("FAIL", "rfkill", "Bluetooth is blocked; run `rfkill unblock bluetooth`")
        else:
            add("PASS", "rfkill", "Bluetooth not blocked")

    if not shutil.which("bluetoothctl"):
        add("FAIL", "bluetoothctl", "not installed; cannot inspect adapter or devices")
    else:
        rc, out = run(["bluetoothctl", "show"])
        if "Powered: yes" in out:
            add("PASS", "Adapter", "present and powered on")
        elif "Powered: no" in out:
            add("FAIL", "Adapter", "powered off; run `bluetoothctl power on`")
        else:
            add("FAIL", "Adapter", "no controller found")

        mac = (args.mac or "").upper() or auto_mac()
        if not mac:
            add("FAIL", "Headphones", "no paired WH-1000XM4 found; pair it first or pass --mac")
        else:
            info = device_info(mac)
            name = info.get("Name", "unknown")
            add("PASS" if info.get("Paired") == "yes" else "FAIL", "Paired", f"{name} ({mac})")
            add("PASS" if info.get("Connected") == "yes" else "WARN", "Connected",
                info.get("Connected", "unknown") + ("" if info.get("Connected") == "yes"
                                                    else "; connecting may still work"))
            add("PASS" if info["sony_service"] else "WARN", "Sony service advertised",
                "yes" if info["sony_service"] else f"not in UUID list ({len(info['uuids'])} UUIDs seen)")

            ch = args.channel or load_cache().get(mac) or lookup_channel(mac)
            if ch:
                add("PASS", "Control channel", f"RFCOMM channel {ch}")
            else:
                add("WARN", "Control channel",
                    "not found yet (normal on SteamOS: no sdptool, BlueZ cache root-only). "
                    "Run `find-channel` once to probe and remember it")

            if ch and not args.quick:
                link = SonyLink(mac, int(ch), args.timeout, args.retries)
                try:
                    link.connect()
                    add("PASS", "RFCOMM connect", f"channel {ch}")
                    try:
                        link.request(bytes([0x00, 0x00]), expect=0x01)
                        add("PASS", "Protocol handshake", "headphones answered init")
                    except LinkError as e:
                        add("FAIL", "Protocol handshake", str(e))
                except LinkError as e:
                    add("FAIL", "RFCOMM connect", str(e))
                finally:
                    link.close()

    width = max(len(r[1]) for r in results)
    for status, name, detail in results:
        print(f"[{status:4}] {name:<{width}}  {detail}")
    print(f"\nFull log: {args.log_file or '(file logging disabled)'}")
    return EXIT_ENV if any(r[0] == "FAIL" for r in results) else EXIT_OK


def cmd_devices(args) -> int:
    devs = paired_devices()
    if not devs:
        print("No paired devices found (is bluetoothctl installed and the adapter on?)")
        return EXIT_ENV
    for mac, name in devs:
        info = device_info(mac)
        flag = "  <- Sony service" if info["sony_service"] else ""
        print(f"{mac}  {name}  connected={info.get('Connected', '?')}{flag}")
    return EXIT_OK


def cmd_find_channel(args) -> int:
    mac = (args.mac or "").upper() or auto_mac()
    if not mac:
        print("No headphones found; pass --mac")
        return EXIT_ENV
    ch = lookup_channel(mac)
    if ch is None:
        print("Probing RFCOMM channels 1-30, this can take up to a minute...", flush=True)
        ch = probe_channels(mac)
    if ch is None:
        print("Sony control channel not found")
        return EXIT_ERROR
    save_cache(mac, ch)
    print(f"{mac}: Sony control channel {ch}")
    return EXIT_OK


def with_link(fn):
    def wrapper(args) -> int:
        link = open_link(args)
        try:
            return fn(args, link)
        finally:
            link.close()
    return wrapper


@with_link
def cmd_init(args, link) -> int:
    print("Connected and initialised")
    return EXIT_OK


@with_link
def cmd_battery(args, link) -> int:
    f = link.request(bytes([0x10, 0x00]), expect=0x11)
    print(describe_payload(f.payload).split(": ", 1)[1])
    return EXIT_OK


@with_link
def cmd_firmware(args, link) -> int:
    f = link.request(bytes([0x04, 0x02]), expect=0x05)
    print(describe_payload(f.payload).split(": ", 1)[1])
    return EXIT_OK


@with_link
def cmd_status(args, link) -> int:
    f = link.request(bytes([0x66, 0x02]), expect=0x67)
    print(describe_payload(f.payload).split(": ", 1)[1])
    return EXIT_OK


@with_link
def cmd_anc(args, link) -> int:
    if args.mode == "wind":
        log.warning("wind noise reduction may not exist on the WH-1000XM4; watch the reply")
    link.send(build_asm(args.mode, max(0, min(20, args.level)), args.voice))
    for f in link.listen(1.5):
        if f.payload[:1] == b"\x69":
            print(describe_payload(f.payload).split(": ", 1)[1])
            return EXIT_OK
    print(f"Sent {args.mode} (no confirmation notification received)")
    return EXIT_OK


@with_link
def cmd_raw(args, link) -> int:
    payload = parse_hex(args.payload)
    if args.expect is not None:
        f = link.request(payload, expect=int(args.expect, 0), dtype=int(args.type, 0))
        print(f"reply: {hexs(f.payload)}\n       {describe_payload(f.payload)}")
    else:
        link.send(payload, dtype=int(args.type, 0), wait_ack=not args.no_ack)
        print("sent; listening 2 s for anything the headphones say back")
        for f in link.listen(2.0):
            print(f"  {f.type_name} seq={f.seq}: {hexs(f.payload)}  ({describe_payload(f.payload)})")
    return EXIT_OK


@with_link
def cmd_listen(args, link) -> int:
    print("Listening (Ctrl+C to stop). Press buttons or change modes on the headphones.")
    try:
        for f in link.listen(args.seconds):
            stamp = time.strftime("%H:%M:%S")
            print(f"{stamp}  {describe_payload(f.payload)}    [{hexs(f.payload)}]", flush=True)
    except KeyboardInterrupt:
        pass
    print(f"\nlink stats: {link.stats}")
    return EXIT_OK


def cmd_decode(args) -> int:
    data = parse_hex(args.hex)
    if START not in data:
        print(f"payload: {describe_payload(data)}")
        return EXIT_OK
    for item in FrameReader().feed(data):
        if isinstance(item, FrameError):
            print(f"BAD FRAME: {item}  [{hexs(item.raw)}]")
        else:
            chk = "ok" if item.checksum_ok else f"BAD (expected {item.expected_checksum:02x})"
            print(f"type={item.type_name} seq={item.seq} len={len(item.payload)} checksum={chk}")
            print(f"  payload: {hexs(item.payload)}")
            print(f"  meaning: {describe_payload(item.payload)}")
    return EXIT_OK


def cmd_logs(args) -> int:
    path = args.log_file or DEFAULT_LOG
    print(f"Log file: {path}")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError as e:
        print(f"(cannot read: {e})")
        return EXIT_ERROR
    print("".join(lines[-args.lines:]), end="")
    return EXIT_OK


def cmd_selftest(args) -> int:
    failures = 0

    def check(name, cond):
        nonlocal failures
        print(f"[{'PASS' if cond else 'FAIL'}] {name}")
        failures += 0 if cond else 1

    check("escape/unescape round-trips all byte values",
          unescape(escape(bytes(range(256)))) == bytes(range(256)))
    check("escaped data contains no frame markers",
          not any(b in (START, END) for b in escape(bytes(range(256)))))
    init = encode_frame(DT_MDR, 0, b"\x00\x00")
    check("init frame encodes as 3e 0c 00 00 00 00 02 00 00 0e 3c",
          hexs(init) == "3e 0c 00 00 00 00 02 00 00 0e 3c")
    for payload in (b"", b"\x3c\x3d\x3e", bytes(range(64)), build_asm("nc", 20, True)):
        f = decode_frame(encode_frame(DT_MDR, 1, payload))
        check(f"encode/decode round-trip ({len(payload)}-byte payload)",
              f.payload == payload and f.seq == 1 and f.checksum_ok)
    bad = bytearray(init)
    bad[-2] ^= 0xFF
    check("corrupted checksum is detected", not decode_frame(bytes(bad)).checksum_ok)
    stream = b"\x99\x98" + init + encode_frame(DT_ACK, 1, b"")
    r = FrameReader()
    got = r.feed(stream[:5]) + r.feed(stream[5:])
    check("reader reassembles split frames and skips garbage",
          len(got) == 2 and all(isinstance(g, Frame) for g in got) and got[1].dtype == DT_ACK)
    check("battery reply is described",
          describe_payload(bytes([0x11, 0x00, 70, 0x01])) == "BATTERY_REPLY: headphones battery 70% (charging)")
    check("noise-cancel setting is described",
          describe_payload(build_asm("nc", 0, False)).startswith("ASM_SET: noise canceling"))
    print(f"\n{'All tests passed' if not failures else f'{failures} test(s) failed'}")
    return EXIT_OK if not failures else EXIT_ERROR


# --------------------------------------------------------------------------- CLI

def setup_logging(verbosity: int, log_file: str | None) -> None:
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    console = logging.StreamHandler(sys.stderr)
    console.setLevel([logging.WARNING, logging.INFO, logging.DEBUG][min(verbosity, 2)])
    console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    log.addHandler(console)
    if log_file:
        try:
            os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
            fh = logging.handlers.RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3,
                                                      encoding="utf-8")
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(message)s",
                                              "%Y-%m-%d %H:%M:%S"))
            log.addHandler(fh)
        except OSError as e:
            log.warning("file logging disabled: %s", e)
    log.debug("=== sonyctl %s | Python %s | %s | argv=%s", VERSION, platform.python_version(),
              platform.platform(), sys.argv[1:])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sonyctl", description="Control and debug Sony WH-1000XM4 headphones on Linux. (Standalone binary: run with no arguments or `gui` for the window.)")
    p.add_argument("--version", action="version", version=f"sonyctl {VERSION}")
    p.add_argument("--mac", help="headphone address (auto-detected from paired devices if omitted)")
    p.add_argument("--channel", type=int, help="RFCOMM channel (auto-detected and cached if omitted)")
    p.add_argument("--timeout", type=float, default=5.0, help="seconds to wait for replies (default 5)")
    p.add_argument("--no-init", action="store_true", help="skip the init handshake after connecting")
    p.add_argument("--retries", type=int, default=3, help="send attempts per command (default 3)")
    p.add_argument("--ack-seq", choices=["flip", "same"], default="flip",
                   help="sequence number in our ACKs (debugging; default flip)")
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v shows protocol steps, -vv shows raw bytes")
    p.add_argument("--log-file", default=DEFAULT_LOG, help=f"debug log path (default {DEFAULT_LOG})")
    p.add_argument("--no-log-file", action="store_true", help="do not write a log file")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    d = sub.add_parser("doctor", help="check Bluetooth setup and connectivity step by step")
    d.add_argument("--quick", action="store_true", help="skip the live connection test")
    d.set_defaults(func=cmd_doctor)
    sub.add_parser("devices", help="list paired devices and flag Sony ones").set_defaults(func=cmd_devices)
    sub.add_parser("services", help="list the headphones' services and channels (may need sudo)"
                   ).set_defaults(func=cmd_services)
    sub.add_parser("find-channel", help="locate and cache the Sony control channel").set_defaults(func=cmd_find_channel)
    sub.add_parser("init", help="connect and run the handshake only").set_defaults(func=cmd_init)
    sub.add_parser("battery", help="show battery level").set_defaults(func=cmd_battery)
    sub.add_parser("firmware", help="show firmware version").set_defaults(func=cmd_firmware)
    sub.add_parser("status", help="show current noise canceling / ambient mode").set_defaults(func=cmd_status)

    a = sub.add_parser("anc", help="set noise canceling mode")
    a.add_argument("mode", choices=["nc", "ambient", "off", "wind"])
    a.add_argument("--level", type=int, default=20, help="ambient sound level 0-20 (default 20)")
    a.add_argument("--voice", action="store_true", help="enable focus on voice (ambient mode)")
    a.set_defaults(func=cmd_anc)

    l = sub.add_parser("listen", help="print everything the headphones send")
    l.add_argument("--seconds", type=float, help="stop after N seconds (default: until Ctrl+C)")
    l.set_defaults(func=cmd_listen)

    r = sub.add_parser("raw", help="send an arbitrary payload (for experiments)")
    r.add_argument("payload", help='payload hex, e.g. "10 00"')
    r.add_argument("--type", default="0x0c", help="frame data type (default 0x0c)")
    r.add_argument("--expect", help="wait for a reply whose first byte is this, e.g. 0x11")
    r.add_argument("--no-ack", action="store_true", help="do not wait for an ACK")
    r.set_defaults(func=cmd_raw)

    dc = sub.add_parser("decode", help="decode hex copied from a log (offline)")
    dc.add_argument("hex")
    dc.set_defaults(func=cmd_decode)

    lg = sub.add_parser("logs", help="show the end of the debug log")
    lg.add_argument("-n", "--lines", type=int, default=60)
    lg.set_defaults(func=cmd_logs)

    sub.add_parser("selftest", help="offline tests of the protocol code").set_defaults(func=cmd_selftest)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.no_log_file:
        args.log_file = None
    setup_logging(args.verbose, args.log_file)
    try:
        global ACK_MODE
        ACK_MODE = args.ack_seq
        rc = args.func(args)
    except LinkTimeout as e:
        log.error("%s", e)
        rc = EXIT_TIMEOUT
    except LinkError as e:
        log.error("%s", e)
        rc = EXIT_ERROR
    except ValueError as e:
        log.error("%s", e)
        rc = EXIT_ERROR
    except KeyboardInterrupt:
        rc = EXIT_ERROR
    except Exception:
        log.exception("unexpected error")
        rc = EXIT_ERROR
    if rc and args.log_file:
        print(f"(details in {args.log_file}; rerun with -vv to see raw bytes)", file=sys.stderr)
    log.debug("exit code %d", rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
