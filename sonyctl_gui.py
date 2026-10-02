#!/usr/bin/env python3
"""
sonyctl_gui - a small desktop window for sonyctl (Sony WH-1000XM4 on Linux / SteamOS).

Keep this file next to sonyctl.py and run:
    python3 sonyctl_gui.py            # real headphones
    python3 sonyctl_gui.py --demo     # simulated headphones, to try the window

All Bluetooth work happens on a background thread; the window only talks to
it through queues, so it never freezes while waiting for the headphones.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import queue
import random
import subprocess
import sys
import threading
import time

try:
    import tkinter as tk
    from tkinter import ttk
except ImportError:
    sys.exit("This window needs Tk (tkinter). Install it with your package manager, e.g.\n"
             "  Debian/Ubuntu: sudo apt install python3-tk\n"
             "  Arch/SteamOS:  sudo pacman -S tk\n"
             "or use the command-line tool: python3 sonyctl.py --help")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sonyctl as sc  # noqa: E402
import sonyctl_tray as traymod  # noqa: E402

log = sc.log
MODES = [("nc", "Noise canceling"), ("ambient", "Ambient sound"), ("off", "Off")]

# --------------------------------------------------------------------------- settings

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"))
SETTINGS_FILE = os.path.join(CONFIG_DIR, "sonyctl", "settings.json")
AUTOSTART_FILE = os.path.join(CONFIG_DIR, "autostart", "sonyctl.desktop")

DEFAULTS = {
    "tray": True,                # show an icon in the system tray
    "close_to_tray": True,       # window close button hides to the tray
    "start_minimized": False,    # start hidden in the tray
    "autostart": False,          # start on login (minimized)
    "auto_connect": True,        # connect on start when the headphones are connected to this PC
    "auto_reconnect": True,      # reconnect when they come back
    "low_battery_notify": True,
    "low_battery_level": 20,
    "full_notify": True,
    "connection_notify": False,
    "poll_minutes": 5,
    "tray_hint_shown": False,
}


def load_settings() -> dict:
    data = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            stored = json.load(f)
        data.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    data["autostart"] = os.path.exists(AUTOSTART_FILE)  # the file is the truth
    return data


def save_settings(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except OSError as e:
        log.warning("could not save settings: %s", e)


def launch_command() -> str:
    if getattr(sys, "frozen", False):  # the standalone binary
        return f'"{sys.executable}" gui --minimized'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}" --minimized'


def set_autostart(on: bool) -> str | None:
    """Create or remove the login autostart entry. Returns an error message or None."""
    try:
        if on:
            os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
            with open(AUTOSTART_FILE, "w", encoding="utf-8") as f:
                f.write("[Desktop Entry]\nType=Application\nName=Sony XM4 Control\n"
                        "Comment=Battery and noise canceling for Sony WH-1000XM4\n"
                        f"Exec={launch_command()}\nIcon=audio-headphones\nTerminal=false\n"
                        "X-GNOME-Autostart-enabled=true\nX-KDE-autostart-after=panel\n")
            log.info("autostart enabled: %s", AUTOSTART_FILE)
        elif os.path.exists(AUTOSTART_FILE):
            os.remove(AUTOSTART_FILE)
            log.info("autostart disabled")
    except OSError as e:
        return str(e)
    return None


# --------------------------------------------------------------------------- payload parsing

def parse_battery(p: bytes):
    if len(p) >= 4 and p[0] in (0x11, 0x13) and p[1] == 0x00:
        return p[2], p[3] == 0x01
    return None


def parse_asm(p: bytes):
    if len(p) >= 8 and p[0] in (0x67, 0x68, 0x69):
        if p[2] == 0x00:
            mode = "off"
        else:
            mode = {0x02: "nc", 0x00: "ambient", 0x01: "wind"}.get(p[4], "nc")
        return mode, p[7], bool(p[6])
    return None


def parse_fw(p: bytes):
    if len(p) >= 3 and p[0] == 0x05:
        return p[3:3 + p[2]].decode("ascii", errors="replace")
    return None


# --------------------------------------------------------------------------- demo headphones

class FakeLink:
    """Behaves like SonyLink but simulates a WH-1000XM4."""

    def __init__(self):
        self.mac, self.channel = "00:00:00:00:00:00", 9
        self.inbox: list[sc.Frame] = []
        self.batt, self.charging = 72, False
        self.mode, self.level, self.voice = "nc", 12, False
        self.next_drain = time.monotonic() + 15

    def _frame(self, payload: bytes) -> sc.Frame:
        log.info("RX %s", sc.describe_payload(payload))
        return sc.Frame(sc.DT_MDR, 0, payload, True, b"")

    def _asm(self, cmd: int) -> bytes:
        return bytes([cmd, 0x02, 0x00 if self.mode == "off" else 0x11, 0x02,
                      sc.ASM_MODES[self.mode], 0x01, int(self.voice), self.level])

    def connect(self):
        log.info("demo: connected to simulated WH-1000XM4")

    def init(self):
        self.request(b"\x00\x00", 0x01)

    def close(self):
        log.info("demo: disconnected")

    def request(self, payload: bytes, expect: int, dtype: int = sc.DT_MDR) -> sc.Frame:
        log.info("TX %s", sc.describe_payload(payload))
        time.sleep(0.15)
        replies = {
            0x01: bytes([0x01, 0x00]),
            0x11: bytes([0x11, 0x00, self.batt, int(self.charging)]),
            0x67: self._asm(0x67),
            0x05: bytes([0x05, 0x02, 5]) + b"2.0.1",
        }
        if expect not in replies:
            raise sc.LinkTimeout(f"demo: no reply for {sc.hexs(payload)}")
        return self._frame(replies[expect])

    def send(self, payload: bytes, dtype: int = sc.DT_MDR, wait_ack: bool = True):
        log.info("TX %s", sc.describe_payload(payload))
        state = parse_asm(payload)
        if state:
            self.mode, self.level, self.voice = state
            self.inbox.append(self._frame(self._asm(0x69)))

    def _read_some(self, deadline):
        time.sleep(max(0.0, min(0.2, deadline - time.monotonic())))
        if time.monotonic() > self.next_drain:
            self.next_drain = time.monotonic() + 15
            self.batt = max(5, self.batt - 1)
            return [self._frame(bytes([0x13, 0x00, self.batt, int(self.charging)]))]
        if random.random() < 0.01:  # someone presses the headphones' NC button
            order = ["nc", "ambient", "off"]
            self.mode = order[(order.index(self.mode) + 1) % 3] if self.mode in order else "nc"
            return [self._frame(self._asm(0x69))]
        return []


# --------------------------------------------------------------------------- worker thread

class Worker(threading.Thread):
    def __init__(self, ui: queue.Queue, demo: bool, timeout: float):
        super().__init__(daemon=True)
        self.ui, self.demo, self.timeout = ui, demo, timeout
        self.cmds: queue.Queue = queue.Queue()
        self.link = None
        # auto-reconnect: keep trying while the user wants a connection
        self.want_connected = False
        self.auto_reconnect = True
        self.retry_every = 15.0
        self.next_retry = 0.0
        self.last_target = ("", "")
        self.waiting_logged = False
        # battery polling (the XM4 doesn't always report changes by itself)
        self.poll_every = 300.0
        self.next_poll = 0.0

    def submit(self, name: str, *args):
        self.cmds.put((name, args))

    def post(self, kind: str, **data):
        self.ui.put((kind, data))

    def run(self):
        while True:
            try:
                if self.link:
                    wait = 0.25
                elif self.want_connected and self.auto_reconnect:
                    wait = 1.0
                else:
                    wait = None
                name, args = self.cmds.get(timeout=wait)
            except queue.Empty:
                name, args = None, ()
            if name == "quit":
                self._drop()
                return
            try:
                if name:
                    getattr(self, "do_" + name)(*args)
                elif self.link:
                    for f in self.link._read_some(time.monotonic() + 0.2):
                        self.handle(f)
                    if self.link and time.monotonic() >= self.next_poll:
                        self.next_poll = time.monotonic() + self.poll_every
                        log.debug("periodic battery check")
                        self.handle(self.link.request(bytes([0x10, 0x00]), expect=0x11))
                elif self.want_connected and self.auto_reconnect and time.monotonic() >= self.next_retry:
                    self.try_reconnect()
                self.drain()
            except sc.LinkTimeout as e:
                log.error("%s", e)
                self.post("error", msg=str(e))
            except ValueError as e:  # bad input, e.g. malformed hex
                log.error("%s", e)
                self.post("error", msg=str(e))
            except (sc.LinkError, OSError) as e:  # connection problem: drop the link
                if self.waiting_logged and not self.link:
                    log.info("reconnect attempt failed: %s", e)
                else:
                    log.error("%s", e)
                    self.post("error", msg=str(e))
                self._drop()
                self._schedule_retry()
            except Exception as e:  # keep the thread alive no matter what
                log.exception("unexpected error in worker")
                self.post("error", msg=f"unexpected error: {e}")

    # -- frame handling
    def handle(self, f: sc.Frame):
        if f.dtype == sc.DT_ACK:
            return
        b = parse_battery(f.payload)
        if b:
            self.post("battery", level=b[0], charging=b[1])
        a = parse_asm(f.payload)
        if a:
            self.post("asm", mode=a[0], level=a[1], voice=a[2])
        fw = parse_fw(f.payload)
        if fw:
            self.post("firmware", version=fw)

    def drain(self):
        while self.link and self.link.inbox:
            self.handle(self.link.inbox.pop(0))

    def _drop(self, by_user: bool = False):
        if self.link:
            try:
                self.link.close()
            finally:
                self.link = None
                self.post("disconnected", by_user=by_user)

    def _schedule_retry(self, delay: float | None = None):
        if self.want_connected and self.auto_reconnect:
            self.next_retry = time.monotonic() + (self.retry_every if delay is None else delay)
            if not self.waiting_logged:
                log.info("will reconnect automatically when the headphones are back")
                self.waiting_logged = True
            self.post("waiting")

    def try_reconnect(self):
        self.next_retry = time.monotonic() + self.retry_every
        mac, ch = self.last_target
        if not self.demo:
            mac = mac.strip() or sc.auto_mac() or ""
            if not mac:
                return
            # Only open the control channel when the headphones are already connected to this
            # computer; otherwise we could pull them away from a phone they're playing on.
            if sc.device_info(mac).get("Connected") != "yes":
                log.debug("reconnect: %s not connected to this computer yet", mac)
                return
        log.info("headphones are back, reconnecting")
        self.do_connect(mac, ch, auto=False, remember=False)

    def need_link(self):
        if not self.link:
            raise sc.LinkError("not connected")
        return self.link

    # -- commands
    def do_connect(self, mac: str, channel: str, auto: bool = False, remember: bool = True):
        self._drop()
        self.want_connected = True
        if remember:
            self.last_target = (mac, channel)
        if auto and not self.demo:
            # Started automatically: wait until the headphones are connected to this computer.
            target = mac.strip() or sc.auto_mac()
            if not target:
                self.post("status", text="No paired WH-1000XM4 found")
                self.want_connected = False
                self.post("disconnected", by_user=True)
                return
            if sc.device_info(target).get("Connected") != "yes":
                self._schedule_retry(delay=self.retry_every)
                return
        if self.demo:
            link = FakeLink()
            name = "WH-1000XM4 (demo)"
        else:
            self.post("status", text="Looking for headphones…")
            ns = argparse.Namespace(mac=mac.strip() or None, channel=int(channel) if channel.strip() else None)
            mac, ch = sc.resolve_target(ns)
            self.post("status", text=f"Connecting to {mac}…")
            link = sc.SonyLink(mac, ch, self.timeout)
            try:
                name = sc.device_info(mac).get("Name", "Sony headphones")
            except Exception:
                name = "Sony headphones"
        link.connect()
        link.init()
        self.link = link
        self.waiting_logged = False
        self.next_poll = time.monotonic() + self.poll_every
        self.post("connected", name=name, mac=link.mac, channel=link.channel)
        self.do_refresh()

    def do_detect(self):
        """Find the paired headphones and the saved channel, without connecting."""
        if self.demo:
            self.post("detected", mac="00:00:00:00:00:00", channel=9, source="demo")
            return
        mac = sc.auto_mac()
        if not mac:
            self.post("detected", mac="", channel=None, source="no paired XM4 found")
            return
        ch = sc.load_cache().get(mac)
        self.post("detected", mac=mac, channel=ch,
                  source="saved channel" if ch else "channel will be found on connect")

    def do_disconnect(self):
        self.want_connected = False
        self.waiting_logged = False
        if self.link:
            self._drop(by_user=True)
        else:
            self.post("disconnected", by_user=True)

    def do_configure(self, auto_reconnect: bool, poll_minutes: float):
        self.auto_reconnect = auto_reconnect
        self.poll_every = max(30.0, poll_minutes * 60)
        self.next_poll = min(self.next_poll, time.monotonic() + self.poll_every)

    def do_refresh(self):
        link = self.need_link()
        self.handle(link.request(bytes([0x10, 0x00]), expect=0x11))
        self.handle(link.request(bytes([0x66, 0x02]), expect=0x67))
        try:
            self.handle(link.request(bytes([0x04, 0x02]), expect=0x05))
        except sc.LinkTimeout as e:
            log.warning("firmware query: %s", e)

    def do_set_asm(self, mode: str, level: int, voice: bool):
        self.need_link().send(sc.build_asm(mode, level, voice))

    def do_raw(self, hex_text: str, expect: str, dtype: str):
        link = self.need_link()
        payload = sc.parse_hex(hex_text)
        dt = int(dtype or "0x0c", 0)
        if expect.strip():
            f = link.request(payload, expect=int(expect, 0), dtype=dt)
            self.handle(f)
            self.post("raw", text=f"reply: {sc.hexs(f.payload)}  →  {sc.describe_payload(f.payload)}")
        else:
            link.send(payload, dtype=dt)
            self.post("raw", text="sent and acknowledged")


# --------------------------------------------------------------------------- logging into the window

class QueueHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__(logging.INFO)
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.q.put(("log", {"text": self.format(record), "level": record.levelno}))
        except Exception:
            pass


# --------------------------------------------------------------------------- window

BG, CARD, FG, MUTED, ACCENT, OK, WARN, BAD = (
    "#16181d", "#20232b", "#e8e9ed", "#9095a3", "#e8a33d", "#4cc38a", "#e8a33d", "#e5544b")


class App:
    def __init__(self, root: tk.Tk, worker: Worker, ui: queue.Queue, handler: QueueHandler, log_file,
                 settings: dict | None = None):
        self.root, self.worker, self.ui, self.handler, self.log_file = root, worker, ui, handler, log_file
        self.settings = settings if settings is not None else dict(DEFAULTS)
        self.tray = None
        self.visible = True
        self.waiting = False
        self.battery = None
        self.charging = False
        self.low_notified = False
        self.full_notified = False
        self.icon_img = None
        self.connected = False
        self.suppress = False
        self.level_job = None

        root.title("Sony XM4 Control")
        root.geometry("560x740")
        root.minsize(500, 640)
        root.configure(bg=BG)
        self._style()

        outer = ttk.Frame(root, padding=16, style="Bg.TFrame")
        outer.pack(fill="both", expand=True)

        # header
        head = ttk.Frame(outer, style="Bg.TFrame")
        head.pack(fill="x")
        self.title = ttk.Label(head, text="WH-1000XM4", style="Title.TLabel")
        self.title.pack(side="left")
        self.connect_btn = ttk.Button(head, text="Connect", style="Accent.TButton", command=self.toggle_connect)
        self.connect_btn.pack(side="right")
        statusrow = ttk.Frame(outer, style="Bg.TFrame")
        statusrow.pack(fill="x", pady=(2, 10))
        self.dot = tk.Label(statusrow, text="●", fg=MUTED, bg=BG, font=("TkDefaultFont", 12))
        self.dot.pack(side="left")
        self.status = ttk.Label(statusrow, text="Not connected", style="Muted.TLabel")
        self.status.pack(side="left", padx=(4, 0))

        # connection settings
        conn = ttk.Frame(outer, style="Bg.TFrame")
        conn.pack(fill="x", pady=(0, 10))
        ttk.Label(conn, text="MAC", style="Muted.TLabel").pack(side="left")
        self.mac = ttk.Entry(conn, width=19)
        self.mac.pack(side="left", padx=(4, 12))
        ttk.Label(conn, text="Channel", style="Muted.TLabel").pack(side="left")
        self.channel = ttk.Entry(conn, width=4)
        self.channel.pack(side="left", padx=(4, 0))
        self.conn_hint = ttk.Label(conn, text="detecting…", style="Muted.TLabel")
        self.conn_hint.pack(side="left", padx=8)

        # battery card
        bc = self._card(outer, "Battery")
        row = ttk.Frame(bc, style="Card.TFrame")
        row.pack(fill="x")
        self.batt_bar = ttk.Progressbar(row, maximum=100, style="Batt.Horizontal.TProgressbar")
        self.batt_bar.pack(side="left", fill="x", expand=True)
        self.batt_lbl = ttk.Label(row, text="—", style="Big.TLabel", width=5, anchor="e")
        self.batt_lbl.pack(side="left", padx=(10, 0))
        row2 = ttk.Frame(bc, style="Card.TFrame")
        row2.pack(fill="x", pady=(6, 0))
        self.charge_lbl = ttk.Label(row2, text="", style="CardMuted.TLabel")
        self.charge_lbl.pack(side="left")
        self.refresh_btn = ttk.Button(row2, text="Refresh", command=lambda: self.worker.submit("refresh"))
        self.refresh_btn.pack(side="right")

        # sound card
        snd = self._card(outer, "Sound control")
        self.mode = tk.StringVar(value="nc")
        mrow = ttk.Frame(snd, style="Card.TFrame")
        mrow.pack(fill="x")
        self.mode_btns = []
        for key, label in MODES:
            b = ttk.Radiobutton(mrow, text=label, value=key, variable=self.mode,
                                style="Seg.Toolbutton", command=self.mode_changed)
            b.pack(side="left", fill="x", expand=True, padx=2)
            self.mode_btns.append(b)
        lrow = ttk.Frame(snd, style="Card.TFrame")
        lrow.pack(fill="x", pady=(12, 0))
        ttk.Label(lrow, text="Ambient level", style="Card.TLabel").pack(side="left")
        self.level_val = ttk.Label(lrow, text="20", style="Card.TLabel", width=3, anchor="e")
        self.level_val.pack(side="right")
        self.level = tk.IntVar(value=20)
        self.level_scale = ttk.Scale(snd, from_=1, to=20, variable=self.level, command=self.level_moved)
        self.level_scale.pack(fill="x", pady=(4, 0))
        self.voice = tk.BooleanVar(value=False)
        self.voice_chk = ttk.Checkbutton(snd, text="Focus on voice", variable=self.voice,
                                         style="Card.TCheckbutton", command=self.apply_asm)
        self.voice_chk.pack(anchor="w", pady=(8, 0))
        self.fw_lbl = ttk.Label(outer, text="", style="Muted.TLabel")
        self.fw_lbl.pack(anchor="w", pady=(0, 8))

        # debug panel
        nb = ttk.Notebook(outer)
        nb.pack(fill="both", expand=True)
        logtab = ttk.Frame(nb, padding=6, style="Card.TFrame")
        rawtab = ttk.Frame(nb, padding=10, style="Card.TFrame")
        settab = ttk.Frame(nb, padding=(12, 8), style="Card.TFrame")
        nb.add(settab, text="Settings")
        nb.add(logtab, text="Log")
        nb.add(rawtab, text="Raw command")
        self._build_settings(settab)

        tools = ttk.Frame(logtab, style="Card.TFrame")
        tools.pack(fill="x", pady=(0, 6))
        ttk.Label(tools, text="Detail", style="Card.TLabel").pack(side="left")
        self.detail = ttk.Combobox(tools, values=["Info", "Debug (raw bytes)"], state="readonly", width=15)
        self.detail.current(0)
        self.detail.bind("<<ComboboxSelected>>", self.detail_changed)
        self.detail.pack(side="left", padx=6)
        ttk.Button(tools, text="Open log", style="Small.TButton", command=self.open_log).pack(side="right")
        ttk.Button(tools, text="Copy", style="Small.TButton", command=self.copy_log).pack(side="right", padx=4)
        ttk.Button(tools, text="Clear", style="Small.TButton", command=lambda: self.logbox.delete("1.0", "end")).pack(side="right")
        box = ttk.Frame(logtab, style="Card.TFrame")
        box.pack(fill="both", expand=True)
        self.logbox = tk.Text(box, height=10, bg="#111317", fg=FG, insertbackground=FG, relief="flat",
                              font=("monospace", 9), wrap="word", padx=6, pady=4)
        sb = ttk.Scrollbar(box, command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.logbox.pack(side="left", fill="both", expand=True)
        self.logbox.tag_configure("DEBUG", foreground=MUTED)
        self.logbox.tag_configure("WARNING", foreground=WARN)
        self.logbox.tag_configure("ERROR", foreground=BAD)

        for r, (lbl, default) in enumerate([("Payload (hex)", "10 00"), ("Expect reply", "0x11"),
                                            ("Frame type", "0x0c")]):
            ttk.Label(rawtab, text=lbl, style="Card.TLabel").grid(row=r, column=0, sticky="w", pady=3)
            e = ttk.Entry(rawtab, width=28)
            e.insert(0, default)
            e.grid(row=r, column=1, sticky="we", padx=8, pady=3)
            setattr(self, ["raw_payload", "raw_expect", "raw_type"][r], e)
        rawtab.columnconfigure(1, weight=1)
        self.raw_btn = ttk.Button(rawtab, text="Send", style="Accent.TButton", command=self.send_raw)
        self.raw_btn.grid(row=3, column=1, sticky="e", pady=(6, 0))
        self.raw_out = ttk.Label(rawtab, text="Leave “Expect reply” empty to send without waiting for an answer.",
                                 style="CardMuted.TLabel", wraplength=440)
        self.raw_out.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        self.set_controls(False)
        self.set_window_icon()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.bind("<Control-q>", lambda _e: self.quit())
        root.bind("<Escape>", lambda _e: self.on_close())
        root.after(100, self.pump)

    # -- settings tab
    def _build_settings(self, tab):
        self.vars: dict = {}
        self.setting_widgets = {}
        cols = [("Tray & startup", [("tray", "Tray icon"), ("close_to_tray", "Close to tray"),
                                     ("start_minimized", "Start minimized"), ("autostart", "Start on login"),
                                     ("auto_connect", "Connect on start")]),
                ("Connection & alerts", [("auto_reconnect", "Auto-reconnect"), ("low_battery_notify", "Low battery at"),
                                         ("full_notify", "Fully charged alert"),
                                         ("connection_notify", "Connect / disconnect alerts")])]
        for c, (heading, rows) in enumerate(cols):
            frame = ttk.Frame(tab, style="Card.TFrame")
            frame.grid(row=0, column=c, sticky="nw", padx=(0, 18))
            ttk.Label(frame, text=heading.upper(), style="CardTitle.TLabel").pack(anchor="w", pady=(0, 4))
            for key, text in rows:
                line = ttk.Frame(frame, style="Card.TFrame")
                line.pack(anchor="w", pady=1)
                v = tk.BooleanVar(value=bool(self.settings[key]))
                self.vars[key] = v
                w = ttk.Checkbutton(line, text=text, variable=v, style="Card.TCheckbutton",
                                    command=lambda k=key: self.setting_changed(k))
                w.pack(side="left")
                self.setting_widgets[key] = w
                if key == "low_battery_notify":
                    self.low_level = tk.IntVar(value=int(self.settings["low_battery_level"]))
                    sp = ttk.Spinbox(line, from_=5, to=50, increment=5, width=3, textvariable=self.low_level,
                                     command=lambda: self.setting_changed("low_battery_level"))
                    sp.pack(side="left", padx=(6, 2))
                    sp.bind("<FocusOut>", lambda _e: self.setting_changed("low_battery_level"))
                    ttk.Label(line, text="%", style="Card.TLabel").pack(side="left")
            if c == 1:
                line = ttk.Frame(frame, style="Card.TFrame")
                line.pack(anchor="w", pady=(3, 0))
                ttk.Label(line, text="Check battery every", style="Card.TLabel").pack(side="left")
                self.poll = tk.IntVar(value=int(self.settings["poll_minutes"]))
                sp2 = ttk.Spinbox(line, from_=1, to=60, width=3, textvariable=self.poll,
                                  command=lambda: self.setting_changed("poll_minutes"))
                sp2.pack(side="left", padx=6)
                sp2.bind("<FocusOut>", lambda _e: self.setting_changed("poll_minutes"))
                ttk.Label(line, text="min", style="Card.TLabel").pack(side="left")
        self.tray_lbl = ttk.Label(tab, text="", style="CardMuted.TLabel", wraplength=500)
        self.tray_lbl.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))

    def _int(self, var, default, lo, hi):
        try:
            return max(lo, min(hi, int(var.get())))
        except (tk.TclError, ValueError):
            return default

    def setting_changed(self, key):
        for k, v in self.vars.items():
            self.settings[k] = bool(v.get())
        self.settings["low_battery_level"] = self._int(self.low_level, 20, 5, 50)
        self.settings["poll_minutes"] = self._int(self.poll, 5, 1, 60)
        if key == "autostart":
            err = set_autostart(self.settings["autostart"])
            if err:
                log.error("autostart: %s", err)
                self.settings["autostart"] = not self.settings["autostart"]
                self.vars["autostart"].set(self.settings["autostart"])
        if key == "tray":
            self.start_tray()
            if not self.settings["tray"] and not self.visible:
                self.show_window()
        if key in ("auto_reconnect", "poll_minutes"):
            self.apply_worker_settings()
        if key == "low_battery_level":
            self.low_notified = False
        save_settings(self.settings)
        self.refresh_tray_label()

    def apply_worker_settings(self):
        self.worker.submit("configure", self.settings["auto_reconnect"], float(self.settings["poll_minutes"]))

    # -- tray and window
    def start_tray(self):
        if self.tray:
            self.tray.stop()
        self.tray = traymod.Tray(lambda action, **kw: self.ui.put(("tray", dict(action=action, **kw))))
        self.tray.start(register=bool(self.settings["tray"]))
        self.push_tray_state()
        self.refresh_tray_label()

    def tray_ok(self) -> bool:
        return bool(self.tray and self.tray.available)

    def refresh_tray_label(self):
        if self.tray_ok():
            text = "Tray icon active. Left-click it to show/hide this window, middle-click to switch mode, " \
                   "scroll on it to change the ambient level."
        elif self.settings["tray"]:
            text = f"Tray icon not available: {self.tray.reason if self.tray else 'not started'}."
        else:
            text = "Tray icon is off."
        self.tray_lbl.configure(text=text)
        tray_on = self.settings["tray"]
        for k in ("close_to_tray", "start_minimized"):
            self.setting_widgets[k].state(["!disabled"] if tray_on else ["disabled"])

    def push_tray_state(self):
        if self.tray:
            self.tray.update(connected=self.connected, waiting=self.waiting, battery=self.battery,
                             charging=self.charging, mode=self.mode.get() if self.connected else None,
                             window_visible=self.visible, name=self.title.cget("text"))

    def notify(self, summary, body="", urgency=1):
        if self.tray:
            self.tray.notify(summary, body, urgency)

    def set_window_icon(self):
        try:
            png = traymod.to_png(traymod.icon_rows(48, self.battery, self.connected))
            self.icon_img = tk.PhotoImage(data=base64.b64encode(png))
            self.root.iconphoto(True, self.icon_img)
        except tk.TclError as e:
            log.debug("window icon: %s", e)
        title = "Sony XM4 Control"
        if self.connected and self.battery is not None:
            title = f"Sony XM4 · {self.battery}%"
        self.root.title(title)

    def hide_window(self):
        self.root.withdraw()
        self.visible = False
        self.push_tray_state()

    def show_window(self):
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass
        self.visible = True
        self.push_tray_state()

    def toggle_window(self):
        self.hide_window() if self.visible else self.show_window()

    def ensure_visible_without_tray(self):
        if not self.visible and not self.tray_ok():
            log.info("no tray icon available, showing the window")
            self.show_window()

    def on_close(self):
        if self.tray_ok() and self.settings["close_to_tray"]:
            self.hide_window()
            if not self.settings["tray_hint_shown"]:
                self.notify("Still running in the tray",
                            "Click the headphones icon to open the window. Quit from its menu.")
                self.settings["tray_hint_shown"] = True
                save_settings(self.settings)
        else:
            self.quit()

    def on_tray(self, action, **kw):
        if action in ("tray_ready", "tray_lost"):
            if action == "tray_lost":
                self.ensure_visible_without_tray()
            self.push_tray_state()
            self.refresh_tray_label()
        elif action == "toggle_window":
            self.toggle_window()
        elif action == "quit":
            self.quit()
        elif action == "connect_toggle":
            self.toggle_connect()
        elif action == "mode" and self.connected:
            self.mode.set(kw["mode"])
            self.mode_changed()
        elif action == "cycle_mode" and self.connected:
            order = [m for m, _ in MODES]
            cur = self.mode.get()
            self.mode.set(order[(order.index(cur) + 1) % len(order)] if cur in order else "nc")
            self.mode_changed()
        elif action == "scroll" and self.connected and self.mode.get() == "ambient":
            if kw.get("orientation", "vertical") == "vertical" and kw.get("delta"):
                new = max(1, min(20, int(self.level.get()) + (1 if kw["delta"] > 0 else -1)))
                self.level.set(new)
                self.level_moved(new)

    # -- styling
    def _style(self):
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure(".", background=CARD, foreground=FG, fieldbackground="#2a2e38", bordercolor="#2f3340",
                    lightcolor=CARD, darkcolor=CARD, troughcolor="#2a2e38", font=("TkDefaultFont", 10))
        s.configure("Bg.TFrame", background=BG)
        s.configure("Card.TFrame", background=CARD)
        s.configure("Title.TLabel", background=BG, foreground=FG, font=("TkDefaultFont", 18, "bold"))
        s.configure("Muted.TLabel", background=BG, foreground=MUTED)
        s.configure("CardTitle.TLabel", background=CARD, foreground=MUTED, font=("TkDefaultFont", 9, "bold"))
        s.configure("Card.TLabel", background=CARD, foreground=FG)
        s.configure("CardMuted.TLabel", background=CARD, foreground=MUTED)
        s.configure("Big.TLabel", background=CARD, foreground=FG, font=("TkDefaultFont", 16, "bold"))
        s.configure("TButton", background="#2f3340", foreground=FG, padding=(10, 5), borderwidth=0)
        s.map("TButton", background=[("disabled", "#262931"), ("active", "#3a3f4d")],
              foreground=[("disabled", MUTED)])
        s.configure("Accent.TButton", background=ACCENT, foreground="#1b1406", font=("TkDefaultFont", 10, "bold"))
        s.map("Accent.TButton", background=[("disabled", "#5a4a30"), ("active", "#f0b453")])
        s.configure("Seg.Toolbutton", background="#2a2e38", foreground=FG, padding=(8, 10), anchor="center")
        s.map("Seg.Toolbutton", background=[("disabled", "#23262e"), ("selected", ACCENT), ("active", "#3a3f4d")],
              foreground=[("disabled", MUTED), ("selected", "#1b1406")])
        s.configure("Card.TCheckbutton", background=CARD, foreground=FG)
        s.map("Card.TCheckbutton", background=[("active", CARD)], foreground=[("disabled", MUTED)])
        s.configure("Batt.Horizontal.TProgressbar", background=OK, thickness=18)
        s.configure("TNotebook", background=BG, borderwidth=0)
        s.configure("TNotebook.Tab", background="#2a2e38", foreground=MUTED, padding=(12, 5),
                    lightcolor="#2a2e38", bordercolor=BG, focuscolor=CARD)
        s.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", FG)],
              lightcolor=[("selected", CARD)])
        s.configure("TEntry", foreground=FG, insertcolor=FG)
        s.configure("TCombobox", foreground=FG, arrowcolor=FG, background="#2f3340")
        s.map("TCombobox", fieldbackground=[("readonly", "#2a2e38")], foreground=[("readonly", FG)],
              selectbackground=[("readonly", "#2a2e38")], selectforeground=[("readonly", FG)])
        s.configure("Small.TButton", padding=(8, 3))
        self.root.option_add("*TCombobox*Listbox.background", "#2a2e38")
        self.root.option_add("*TCombobox*Listbox.foreground", FG)

    def _card(self, parent, title):
        card = ttk.Frame(parent, padding=14, style="Card.TFrame")
        card.pack(fill="x", pady=(0, 10))
        ttk.Label(card, text=title.upper(), style="CardTitle.TLabel").pack(anchor="w", pady=(0, 8))
        return card

    # -- state helpers
    def set_status(self, text, color=MUTED):
        self.status.configure(text=text)
        self.dot.configure(fg=color)

    def set_controls(self, on: bool):
        state = ["!disabled"] if on else ["disabled"]
        for w in self.mode_btns + [self.refresh_btn, self.raw_btn, self.voice_chk]:
            w.state(state)
        self.update_ambient_controls(on)

    def update_ambient_controls(self, on=None):
        on = self.connected if on is None else on
        amb = on and self.mode.get() == "ambient"
        self.level_scale.state(["!disabled"] if amb else ["disabled"])
        self.voice_chk.state(["!disabled"] if amb else ["disabled"])

    # -- user actions
    def toggle_connect(self):
        if self.connected or self.waiting:
            self.worker.submit("disconnect")
        else:
            self.start_connect(auto=False)

    def start_connect(self, auto: bool):
        self.connect_btn.state(["disabled"])
        self.set_status("Connecting…", WARN)
        self.worker.submit("connect", self.mac.get(), self.channel.get(), auto)

    def mode_changed(self):
        self.update_ambient_controls()
        self.apply_asm()
        self.push_tray_state()

    def level_moved(self, _value):
        self.level_val.configure(text=str(int(float(self.level.get()))))
        if self.suppress:
            return
        if self.level_job:
            self.root.after_cancel(self.level_job)
        self.level_job = self.root.after(350, self.apply_asm)  # debounce slider drags

    def apply_asm(self):
        self.level_job = None
        if self.suppress or not self.connected:
            return
        self.worker.submit("set_asm", self.mode.get(), int(float(self.level.get())), bool(self.voice.get()))

    def send_raw(self):
        self.raw_out.configure(text="sending…")
        self.worker.submit("raw", self.raw_payload.get(), self.raw_expect.get(), self.raw_type.get())

    def detail_changed(self, _e=None):
        self.handler.setLevel(logging.DEBUG if self.detail.current() == 1 else logging.INFO)

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.logbox.get("1.0", "end"))
        self.set_status("Log copied to clipboard", self.dot.cget("fg"))

    def open_log(self):
        if not self.log_file:
            return
        try:
            subprocess.Popen(["xdg-open", os.path.dirname(self.log_file)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            log.warning("could not open %s; the full log is at that path", self.log_file)

    def quit(self):
        if self.tray:
            self.tray.stop()
        self.worker.submit("quit")
        self.root.after(150, self.root.destroy)

    # -- messages from the worker
    def pump(self):
        try:
            while True:
                kind, d = self.ui.get_nowait()
                getattr(self, "on_" + kind)(**d)
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def on_log(self, text, level):
        tag = logging.getLevelName(level)
        self.logbox.insert("end", text + "\n", tag)
        if int(self.logbox.index("end-1c").split(".")[0]) > 3000:
            self.logbox.delete("1.0", "500.0")
        self.logbox.see("end")

    def on_status(self, text):
        self.set_status(text, WARN)

    def _fill(self, entry, value):
        entry.delete(0, "end")
        if value not in (None, ""):
            entry.insert(0, str(value))

    def on_detected(self, mac, channel, source):
        if not self.mac.get().strip():
            self._fill(self.mac, mac)
        if not self.channel.get().strip():
            self._fill(self.channel, channel)
        self.conn_hint.configure(text=source)

    def on_connected(self, name, mac, channel):
        self._fill(self.mac, mac)
        self._fill(self.channel, channel)
        self.conn_hint.configure(text="in use")
        self.connected = True
        self.waiting = False
        self.title.configure(text=name)
        self.set_status(f"Connected · {mac} · channel {channel}", OK)
        self.connect_btn.configure(text="Disconnect")
        self.connect_btn.state(["!disabled"])
        self.set_controls(True)
        if self.settings["connection_notify"]:
            self.notify("Headphones connected", name)
        self.set_window_icon()
        self.push_tray_state()

    def on_disconnected(self, by_user: bool = True):
        was = self.connected
        self.connected = False
        if by_user:
            self.waiting = False
        self.set_status("Not connected", MUTED)
        self.connect_btn.configure(text="Connect")
        self.connect_btn.state(["!disabled"])
        self.set_controls(False)
        if was and not by_user and self.settings["connection_notify"]:
            self.notify("Headphones disconnected", "Will reconnect when they're back."
                        if self.settings["auto_reconnect"] else "")
        self.set_window_icon()
        self.push_tray_state()

    def on_waiting(self):
        self.waiting = True
        self.set_status("Waiting for the headphones to connect to this computer…", WARN)
        self.connect_btn.configure(text="Stop")
        self.connect_btn.state(["!disabled"])
        self.push_tray_state()

    def on_error(self, msg):
        if not self.connected:
            self.on_disconnected()
        self.set_status(msg if len(msg) < 90 else msg[:87] + "…", BAD)
        self.raw_out.configure(text=msg)

    def on_battery(self, level, charging):
        self.batt_bar.configure(value=level)
        self.batt_lbl.configure(text=f"{level}%")
        self.charge_lbl.configure(text="Charging" if charging else "On battery")
        color = OK if level > 30 else WARN if level > 15 else BAD
        ttk.Style(self.root).configure("Batt.Horizontal.TProgressbar", background=color)
        changed = (level, charging) != (self.battery, self.charging)
        self.battery, self.charging = level, charging
        threshold = int(self.settings["low_battery_level"])
        if charging or level > threshold + 5:
            self.low_notified = False
        elif level <= threshold and not self.low_notified and self.settings["low_battery_notify"]:
            self.notify("Headphones battery low", f"{level}% left", urgency=2)
            self.low_notified = True
        if not charging:
            self.full_notified = False
        elif level >= 100 and not self.full_notified and self.settings["full_notify"]:
            self.notify("Headphones fully charged", "100%")
            self.full_notified = True
        if changed:
            self.set_window_icon()
            self.push_tray_state()

    def on_asm(self, mode, level, voice):
        self.suppress = True
        try:
            self.mode.set(mode if mode in dict(MODES) else "nc")
            self.level.set(max(1, level))
            self.level_val.configure(text=str(max(1, level)))
            self.voice.set(voice)
            self.update_ambient_controls()
        finally:
            self.suppress = False
        self.push_tray_state()

    def on_firmware(self, version):
        self.fw_lbl.configure(text=f"Firmware {version}")

    def on_raw(self, text):
        self.raw_out.configure(text=text)


def main():
    p = argparse.ArgumentParser(description="Small window for controlling Sony WH-1000XM4 headphones.")
    p.add_argument("--demo", action="store_true", help="simulate headphones (no Bluetooth needed)")
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("--log-file", default=sc.DEFAULT_LOG)
    p.add_argument("--no-log-file", action="store_true")
    p.add_argument("--connect", action="store_true", help="connect on start")
    p.add_argument("--minimized", action="store_true", help="start hidden in the tray (used by autostart)")
    p.add_argument("--no-tray", action="store_true", help="don't show a tray icon this time")
    args = p.parse_args()

    log_file = None if args.no_log_file else args.log_file
    sc.setup_logging(0, log_file)  # file gets everything; console stays quiet
    ui: queue.Queue = queue.Queue()
    handler = QueueHandler(ui)
    log.addHandler(handler)

    settings = load_settings()
    if args.no_tray:
        settings["tray"] = False
    worker = Worker(ui, args.demo, args.timeout)
    worker.start()
    root = tk.Tk()
    app = App(root, worker, ui, handler, log_file, settings)
    app.apply_worker_settings()
    app.start_tray()
    log.info("sonyctl GUI %s started%s; full log: %s", sc.VERSION, " in demo mode" if args.demo else "",
             log_file or "(disabled)")
    if (args.minimized or settings["start_minimized"]) and settings["tray"]:
        app.hide_window()
        # if no tray shows up (e.g. the panel isn't running), don't stay invisible
        root.after(10000, app.ensure_visible_without_tray)
    worker.submit("detect")
    if args.connect or args.demo:
        root.after(300, lambda: app.start_connect(auto=False))
    elif settings["auto_connect"]:
        root.after(300, lambda: app.start_connect(auto=True))
    root.mainloop()


if __name__ == "__main__":
    main()
