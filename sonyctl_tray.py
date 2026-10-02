"""
System tray icon and desktop notifications for sonyctl, over D-Bus.

Implements the StatusNotifierItem + DBusMenu protocols, which Linux panels use
for tray icons: KDE Plasma (and SteamOS desktop mode), GNOME with the
AppIndicator extension, XFCE, Cinnamon, LXQt, waybar and others.

Needs the small pure-Python package `jeepney` (bundled in the binary). If it is
missing, or no panel offers a tray, the window simply works without one.

All D-Bus traffic runs on this module's own thread. Clicks in the tray are
passed to the `on_event(action, **data)` callback you supply, which must be
thread-safe (the GUI puts them on a queue).
"""
from __future__ import annotations

import logging
import math
import os
import struct
import threading
import zlib

log = logging.getLogger("sonyctl")

try:
    from jeepney import (DBusAddress, HeaderFields, MessageType, new_error, new_method_call,
                         new_method_return, new_signal)
    from jeepney.io.blocking import open_dbus_connection
    HAVE_JEEPNEY = True
except ImportError:  # pragma: no cover - depends on the environment
    HAVE_JEEPNEY = False

ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
SNI_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
WATCHER = "org.kde.StatusNotifierWatcher"

# --------------------------------------------------------------------------- icon drawing

C_OK, C_WARN, C_BAD = (76, 195, 138), (232, 163, 61), (229, 84, 75)
C_BODY, C_BODY_OFF, C_OUTLINE, C_TRACK = (226, 228, 234), (150, 154, 165), (24, 26, 31), (60, 64, 74)


def battery_color(level):
    return C_OK if level > 30 else C_WARN if level > 15 else C_BAD


def _rrect(x, y, x0, y0, x1, y1, r):
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _headphones(x, y, g):
    cx, cy = 0.5, 0.58
    r = math.hypot(x - cx, y - cy)
    band = (0.29 - g) <= r <= (0.39 + g) and y <= cy + g
    cup_l = _rrect(x, y, 0.07 - g, 0.47 - g, 0.31 + g, 0.80 + g, 0.07 + g)
    cup_r = _rrect(x, y, 0.69 - g, 0.47 - g, 0.93 + g, 0.80 + g, 0.07 + g)
    return band or cup_l or cup_r


def _bar(x, y, g, right=0.90):
    return _rrect(x, y, 0.10 - g, 0.86 - g, right + g, 0.98 + g, 0.04 + g)


def render_icon(size: int, level: int | None, connected: bool, ss: int = 3) -> list[list[tuple]]:
    """Return rows of (r, g, b, a) pixels: headphones with a battery bar under them."""
    out_w = 1.3 / size  # outline width ~1.3 px
    layers = [(lambda x, y: _headphones(x, y, out_w), C_OUTLINE, 0.85),
              (lambda x, y: _headphones(x, y, 0), C_BODY if connected else C_BODY_OFF, 1.0)]
    if connected and level is not None:
        fill_to = 0.10 + 0.80 * max(0, min(100, level)) / 100
        layers += [(lambda x, y: _bar(x, y, out_w), C_OUTLINE, 0.85),
                   (lambda x, y: _bar(x, y, 0), C_TRACK, 1.0),
                   (lambda x, y: _bar(x, y, 0, fill_to) and x <= fill_to, battery_color(level), 1.0)]
    rows = []
    n = ss * ss
    for py in range(size):
        row = []
        for px in range(size):
            r = g = b = a = 0.0
            for color, alpha, test in ((c, al, t) for t, c, al in layers):
                hits = 0
                for sy in range(ss):
                    for sx in range(ss):
                        if test((px + (sx + 0.5) / ss) / size, (py + (sy + 0.5) / ss) / size):
                            hits += 1
                if hits:
                    cov = alpha * hits / n
                    # "over" compositing in premultiplied space
                    r = color[0] * cov + r * (1 - cov)
                    g = color[1] * cov + g * (1 - cov)
                    b = color[2] * cov + b * (1 - cov)
                    a = cov + a * (1 - cov)
            if a > 0:
                row.append((round(r / a), round(g / a), round(b / a), round(a * 255)))
            else:
                row.append((0, 0, 0, 0))
        rows.append(row)
    return rows


def to_argb(rows) -> bytes:
    return b"".join(bytes((a, r, g, b)) for row in rows for (r, g, b, a) in row)


def to_png(rows) -> bytes:
    h, w = len(rows), len(rows[0])
    raw = b"".join(b"\x00" + b"".join(bytes(p) for p in row) for row in rows)

    def chunk(kind, data):
        c = kind + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


_cache: dict = {}


def icon_rows(size, level, connected):
    key = (size, None if level is None else int(level), connected)
    if key not in _cache:
        _cache[key] = render_icon(size, level, connected)
    return _cache[key]


# --------------------------------------------------------------------------- tray

INTROSPECT = {
    ITEM_PATH: """<node><interface name="org.kde.StatusNotifierItem">
<property name="Category" type="s" access="read"/><property name="Id" type="s" access="read"/>
<property name="Title" type="s" access="read"/><property name="Status" type="s" access="read"/>
<property name="WindowId" type="i" access="read"/><property name="IconName" type="s" access="read"/>
<property name="IconPixmap" type="a(iiay)" access="read"/><property name="OverlayIconName" type="s" access="read"/>
<property name="AttentionIconName" type="s" access="read"/><property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
<property name="ItemIsMenu" type="b" access="read"/><property name="Menu" type="o" access="read"/>
<method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
<method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
<signal name="NewTitle"/><signal name="NewIcon"/><signal name="NewToolTip"/><signal name="NewStatus"><arg type="s"/></signal>
</interface></node>""",
    MENU_PATH: """<node><interface name="com.canonical.dbusmenu">
<property name="Version" type="u" access="read"/><property name="TextDirection" type="s" access="read"/>
<property name="Status" type="s" access="read"/><property name="IconThemePath" type="as" access="read"/>
<method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
<arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
<method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/>
<arg type="a(ia{sv})" direction="out"/></method>
<method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
<method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/></method>
<method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
<method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
<method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
<signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
<signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
</interface></node>""",
}

MODE_ITEMS = {10: "nc", 11: "ambient", 12: "off"}


class Tray:
    """Tray icon + notifications. start() returns quickly; check .available / .reason."""

    def __init__(self, on_event, app_name="Sony XM4 Control"):
        self.on_event = on_event
        self.app_name = app_name
        self.conn = None
        self.bus_ok = False          # session bus reachable (notifications work)
        self.available = False       # icon registered with a panel
        self.reason = ""
        self.running = False
        self.send_lock = threading.Lock()
        self.state_lock = threading.Lock()
        self.revision = 1
        self.service = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.state = {"connected": False, "waiting": False, "battery": None, "charging": False,
                      "mode": None, "window_visible": True, "name": "WH-1000XM4"}

    # -- lifecycle
    def start(self, register: bool = True) -> bool:
        """Connect to the session bus; with register=False only notifications are set up."""
        if not HAVE_JEEPNEY:
            self.reason = "Python package 'jeepney' is not installed"
            return False
        try:
            self.conn = open_dbus_connection(bus="SESSION")
        except Exception as e:
            self.reason = f"no D-Bus session bus ({e})"
            return False
        self.bus_ok = True
        if not register:
            self.reason = "tray icon turned off in settings"
            self.running = True
            threading.Thread(target=self._loop, name="notify", daemon=True).start()
            return False
        bus = DBusAddress("/org/freedesktop/DBus", bus_name="org.freedesktop.DBus",
                          interface="org.freedesktop.DBus")
        try:
            self.conn.send_and_get_reply(new_method_call(bus, "RequestName", "su", (self.service, 4)), timeout=3)
            self.conn.send_and_get_reply(new_method_call(
                bus, "AddMatch", "s",
                (f"type='signal',sender='org.freedesktop.DBus',member='NameOwnerChanged',arg0='{WATCHER}'",)),
                timeout=3)
            has = self.conn.send_and_get_reply(new_method_call(bus, "NameHasOwner", "s", (WATCHER,)), timeout=3)
            watcher_present = bool(has.body and has.body[0])
        except Exception as e:
            self.reason = f"D-Bus setup failed ({e})"
            watcher_present = False
        self.running = True
        threading.Thread(target=self._loop, name="tray", daemon=True).start()
        if watcher_present:
            self._register()
        else:
            self.reason = ("no system tray found (GNOME needs the 'AppIndicator and KStatusNotifierItem' "
                           "extension); waiting for one to appear")
            log.info("tray: %s", self.reason)
        return self.available

    def stop(self):
        self.running = False
        try:
            if self.conn:
                self.conn.close()
        except Exception:
            pass

    def _register(self):
        watcher = DBusAddress("/StatusNotifierWatcher", bus_name=WATCHER, interface=WATCHER)
        self._send(new_method_call(watcher, "RegisterStatusNotifierItem", "s", (self.service,)))
        was = self.available
        self.available, self.reason = True, ""
        log.info("tray: icon registered as %s", self.service)
        if not was:
            self.on_event("tray_ready")

    # -- outgoing
    def _send(self, msg):
        if not self.conn:
            return
        try:
            with self.send_lock:
                self.conn.send(msg)
        except Exception as e:
            log.debug("tray: send failed: %s", e)

    def notify(self, summary: str, body: str = "", urgency: int = 1, icon: str = "audio-headphones"):
        if not self.bus_ok:
            log.info("notification (no D-Bus): %s %s", summary, body)
            return
        addr = DBusAddress("/org/freedesktop/Notifications", bus_name="org.freedesktop.Notifications",
                           interface="org.freedesktop.Notifications")
        self._send(new_method_call(addr, "Notify", "susssasa{sv}i",
                                   (self.app_name, 0, icon, summary, body, [], {"urgency": ("y", urgency)}, 8000)))
        log.info("notification: %s %s", summary, body)

    def update(self, **changes):
        """Change displayed state (thread-safe); refreshes icon, tooltip and menu."""
        with self.state_lock:
            before = dict(self.state)
            self.state.update(changes)
            if self.state == before:
                return
            self.revision += 1
            rev = self.revision
        if not self.available:
            return
        self._send(new_signal(DBusAddress(ITEM_PATH, interface=SNI_IFACE), "NewIcon"))
        self._send(new_signal(DBusAddress(ITEM_PATH, interface=SNI_IFACE), "NewToolTip"))
        self._send(new_signal(DBusAddress(MENU_PATH, interface=MENU_IFACE), "LayoutUpdated", "ui", (rev, 0)))

    # -- what we show
    def _snapshot(self):
        with self.state_lock:
            return dict(self.state), self.revision

    def _title_desc(self, s):
        if s["connected"]:
            batt = f"{s['battery']}%" if s["battery"] is not None else "battery unknown"
            if s["charging"]:
                batt += ", charging"
            mode = {"nc": "Noise canceling", "ambient": "Ambient sound", "off": "NC off"}.get(s["mode"], "")
            return s["name"], f"{batt}" + (f" · {mode}" if mode else "")
        if s["waiting"]:
            return s["name"], "Waiting for headphones to connect…"
        return s["name"], "Not connected"

    def _item_props(self):
        s, _ = self._snapshot()
        title, desc = self._title_desc(s)
        pix = [(n, n, to_argb(icon_rows(n, s["battery"], s["connected"]))) for n in (32, 64)]
        return {
            "Category": ("s", "Hardware"), "Id": ("s", "sonyctl"), "Title": ("s", f"{title} – {desc}"),
            "Status": ("s", "Active"), "WindowId": ("i", 0), "IconName": ("s", ""),
            "IconPixmap": ("a(iiay)", pix), "OverlayIconName": ("s", ""), "AttentionIconName": ("s", ""),
            "AttentionIconPixmap": ("a(iiay)", []), "OverlayIconPixmap": ("a(iiay)", []),
            "IconThemePath": ("s", ""),
            "ToolTip": ("(sa(iiay)ss)", ("", [], title, desc)),
            "ItemIsMenu": ("b", False), "Menu": ("o", MENU_PATH),
        }

    def _menu_items(self):
        s, _ = self._snapshot()
        title, desc = self._title_desc(s)
        on = s["connected"]

        def item(label, enabled=True, **extra):
            p = {"label": ("s", label), "enabled": ("b", enabled), "visible": ("b", True)}
            for k, v in extra.items():
                p[k.replace("_", "-")] = v
            return p
        sep = {"type": ("s", "separator"), "visible": ("b", True)}
        items = [(1, item(f"{title}: {desc}", enabled=False)), (2, sep)]
        for iid, (key, label) in zip(MODE_ITEMS, [("nc", "Noise canceling"), ("ambient", "Ambient sound"),
                                                   ("off", "Noise canceling off")]):
            items.append((iid, item(label, enabled=on, toggle_type=("s", "radio"),
                                    toggle_state=("i", 1 if on and s["mode"] == key else 0))))
        items += [(3, sep),
                  (20, item("Disconnect" if (on or s["waiting"]) else "Connect")),
                  (21, item("Hide window" if s["window_visible"] else "Show window")),
                  (4, sep), (30, item("Quit"))]
        return items

    def _layout(self):
        children = [("(ia{sv}av)", (iid, props, [])) for iid, props in self._menu_items()]
        return (0, {"children-display": ("s", "submenu")}, children)

    # -- incoming
    def _loop(self):
        while self.running:
            try:
                msg = self.conn.receive(timeout=1.0)
            except TimeoutError:
                continue
            except Exception as e:
                if self.running:
                    log.warning("tray: D-Bus connection lost: %s", e)
                    self.available = False
                    self.on_event("tray_lost")
                return
            try:
                self._dispatch(msg)
            except Exception:
                log.exception("tray: error while handling a D-Bus message")

    def _reply(self, msg, sig="", body=()):
        self._send(new_method_return(msg, sig, body))

    def _dispatch(self, msg):
        h = msg.header
        f = h.fields
        if h.message_type == MessageType.error:
            log.debug("tray: D-Bus error reply: %s %s", f.get(HeaderFields.error_name), msg.body)
            return
        if h.message_type == MessageType.signal:
            if f.get(HeaderFields.member) == "NameOwnerChanged" and msg.body and msg.body[0] == WATCHER:
                if msg.body[2]:
                    log.info("tray: panel appeared, registering icon")
                    self._register()
                else:
                    log.info("tray: panel went away")
                    self.available = False
            return
        if h.message_type != MessageType.method_call:
            return
        path, iface, member = f.get(HeaderFields.path), f.get(HeaderFields.interface), f.get(HeaderFields.member)
        body = msg.body

        if member == "Introspect":
            xml = INTROSPECT.get(path, "<node><node name='StatusNotifierItem'/><node name='MenuBar'/></node>")
            return self._reply(msg, "s", (xml,))
        if member == "Ping":
            return self._reply(msg)

        if iface == PROPS_IFACE:
            props = self._item_props() if path == ITEM_PATH else self._menu_props() if path == MENU_PATH else {}
            if member == "GetAll":
                return self._reply(msg, "a{sv}", (props,))
            if member == "Get":
                if body[1] in props:
                    return self._reply(msg, "v", (props[body[1]],))
                return self._send(new_error(msg, "org.freedesktop.DBus.Error.UnknownProperty"))
            return self._send(new_error(msg, "org.freedesktop.DBus.Error.PropertyReadOnly"))

        if path == ITEM_PATH:
            self._reply(msg)
            if member == "Activate":
                self.on_event("toggle_window")
            elif member == "SecondaryActivate":
                self.on_event("cycle_mode")
            elif member == "Scroll":
                self.on_event("scroll", delta=int(body[0]), orientation=str(body[1]))
            return

        if path == MENU_PATH:
            if member == "GetLayout":
                return self._reply(msg, "u(ia{sv}av)", (self._snapshot()[1], self._layout()))
            if member == "GetGroupProperties":
                ids, items = set(body[0]), self._menu_items()
                return self._reply(msg, "a(ia{sv})", ([(i, p) for i, p in items if not ids or i in ids],))
            if member == "GetProperty":
                props = dict(self._menu_items()).get(body[0], {})
                if body[1] in props:
                    return self._reply(msg, "v", (props[body[1]],))
                return self._send(new_error(msg, "org.freedesktop.DBus.Error.InvalidArgs"))
            if member == "Event":
                self._reply(msg)
                if body[1] == "clicked":
                    self._clicked(body[0])
                return
            if member == "EventGroup":
                self._reply(msg, "ai", ([],))
                for ev in body[0]:
                    if ev[1] == "clicked":
                        self._clicked(ev[0])
                return
            if member == "AboutToShow":
                return self._reply(msg, "b", (False,))
            if member == "AboutToShowGroup":
                return self._reply(msg, "aiai", ([], []))
        self._send(new_error(msg, "org.freedesktop.DBus.Error.UnknownMethod"))

    def _menu_props(self):
        return {"Version": ("u", 3), "TextDirection": ("s", "ltr"), "Status": ("s", "normal"),
                "IconThemePath": ("as", [])}

    def _clicked(self, iid):
        log.debug("tray: menu item %s clicked", iid)
        if iid in MODE_ITEMS:
            self.on_event("mode", mode=MODE_ITEMS[iid])
        elif iid == 20:
            self.on_event("connect_toggle")
        elif iid == 21:
            self.on_event("toggle_window")
        elif iid == 30:
            self.on_event("quit")
