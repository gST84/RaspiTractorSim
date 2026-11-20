"""Virtual Terminal server (ISO 11783-6).

Receives object pools from working sets (implements), answers the technical data requests, carries
out the change commands on the stored objects, sends VT Status every second and turns operator
input from the web page into soft key / button activations and value changes.

Function codes follow AgIsoStack++ and the decoder in PLC-VT-Designer/isobus_project_bundle/can_log_tools
(checked against a real bus log). Error bits in responses are a simplified version of the standard.
"""
import asyncio
import logging
import os
import struct
import time

from ..j1939 import GLOBAL, le
from .objects import AIDS, KIND, NONE, PoolError, TYPES, encode_object, parse_pool, type_name, validate

log = logging.getLogger(__name__)

PGN_VT_TO_ECU = 0xE600
PGN_ECU_TO_VT = 0xE700
PGN_LANGUAGE = 0xFE0F

FUNC = {
    0x00: "Soft Key Activation", 0x01: "Button Activation", 0x02: "Pointing Event", 0x03: "VT Select Input Object",
    0x04: "VT ESC", 0x05: "VT Change Numeric Value", 0x06: "VT Change Active Mask", 0x07: "VT Change Soft Key Mask",
    0x08: "VT Change String Value", 0x11: "Object Pool Transfer", 0x12: "End of Object Pool",
    0x20: "Aux Assignment Type 1", 0x21: "Aux Input Status Type 1", 0x22: "Preferred Assignment",
    0x23: "Aux Input Type 2 Maintenance", 0x24: "Aux Assignment Type 2", 0x25: "Aux Input Status Enable",
    0x26: "Aux Input Type 2 Status", 0x27: "Aux Capabilities", 0x92: "ESC",
    0xA0: "Hide/Show Object", 0xA1: "Enable/Disable Object", 0xA2: "Select Input Object", 0xA3: "Control Audio Signal",
    0xA4: "Set Audio Volume", 0xA5: "Change Child Location", 0xA6: "Change Size", 0xA7: "Change Background Colour",
    0xA8: "Change Numeric Value", 0xA9: "Change End Point", 0xAA: "Change Font Attributes", 0xAB: "Change Line Attributes",
    0xAC: "Change Fill Attributes", 0xAD: "Change Active Mask", 0xAE: "Change Soft Key Mask", 0xAF: "Change Attribute",
    0xB0: "Change Priority", 0xB1: "Change List Item", 0xB2: "Delete Object Pool", 0xB3: "Change String Value",
    0xB4: "Change Child Position", 0xB5: "Change Object Label", 0xB6: "Change Polygon Point", 0xB7: "Change Polygon Scale",
    0xB8: "Graphics Context", 0xB9: "Get Attribute Value", 0xBA: "Select Colour Map", 0xBB: "Identify VT",
    0xBC: "Execute Extended Macro", 0xBD: "Lock/Unlock Mask", 0xBE: "Execute Macro", 0xC0: "Get Memory",
    0xC1: "Get Supported Widechars", 0xC2: "Get Number of Soft Keys", 0xC3: "Get Text Font Data",
    0xC4: "Get Window Mask Data", 0xC5: "Get Supported Objects", 0xC7: "Get Hardware", 0xD0: "Store Version",
    0xD1: "Load Version", 0xD2: "Delete Version", 0xD3: "Extended Get Versions", 0xD4: "Extended Store Version",
    0xD5: "Extended Load Version", 0xD6: "Extended Delete Version", 0xDF: "Get Versions", 0xE0: "Get Versions Response",
    0xFD: "Unsupported VT Function", 0xFE: "VT Status", 0xFF: "Working Set Maintenance",
}

# macro events (ISO 11783-6 annex J)
EV_SHOW, EV_HIDE, EV_ENABLE, EV_DISABLE, EV_ACTIVE_MASK, EV_SKM = 3, 4, 5, 6, 7, 8
EV_ATTR, EV_BG, EV_FONT, EV_LINE, EV_FILL, EV_LOCATION, EV_SIZE, EV_VALUE, EV_PRIORITY, EV_ENDPOINT = 9, 10, 11, 12, 13, 14, 15, 16, 17, 18
EV_SELECT, EV_DESELECT, EV_ESC, EV_ENTRY, EV_NEW_ENTRY, EV_KEY_PRESS, EV_KEY_RELEASE, EV_POSITION = 19, 20, 21, 22, 23, 24, 25, 26

ERR_ID, ERR_VALUE, ERR_OTHER = 0x01, 0x02, 0x10
INPUTS = (7, 8, 9, 10)
SUPPORTED_TYPES = list(range(0, 29)) + [34, 35, 36, 37, 38, 39, 40]
LOCAL = 0x100  # pseudo address of a pool opened from a file (no CAN traffic)


def u16(b, i):
    return b[i] | (b[i + 1] << 8)


def u32(b, i):
    return struct.unpack_from("<I", b, i)[0]


class WorkingSet:
    def __init__(self, addr, name):
        self.addr = addr
        self.name = name or 0
        self.state = "verbonden"  # verbonden | uploaden | actief | fout
        self.vt_version = 0
        self.last_maint = time.monotonic()
        self.buf = bytearray()
        self.updating = False
        self.pool = b""
        self.objects = {}
        self.ws_id = None
        self.error = ""
        self.warnings = []
        self.progress = None
        self.label = ""
        self.selected = NONE
        self.locked = False
        self.uploaded_at = None

    @property
    def local(self):
        return self.addr == LOCAL

    def obj(self, oid):
        return self.objects.get(oid)

    @property
    def ws_obj(self):
        return self.objects.get(self.ws_id)

    @property
    def active_mask(self):
        w = self.ws_obj
        return w["activeMask"] if w else NONE

    @property
    def soft_key_mask(self):
        m = self.objects.get(self.active_mask)
        return m.get("softKeyMask", NONE) if m else NONE

    def summary(self):
        return {"addr": self.addr, "name": f"{self.name:016X}", "state": self.state, "vtVersion": self.vt_version,
                "poolSize": len(self.pool), "objects": len(self.objects), "error": self.error,
                "warnings": [w[0] for w in self.warnings[:50]], "warningCount": len(self.warnings),
                "progress": self.progress, "label": self.label, "wsId": self.ws_id, "activeMask": self.active_mask,
                "softKeyMask": self.soft_key_mask, "selected": self.selected, "locked": self.locked, "local": self.local}


class VTServer:
    def __init__(self, net, cf, cfg, data_dir, events, language):
        self.net, self.cf, self.cfg, self.ev = net, cf, cfg, events
        self.language = language  # fn() → 8 bytes Language Command
        self.dir = os.path.join(data_dir, "versions")
        self.ws = {}
        self.active = None
        self.held = {}
        self.edit = None  # (addr, oid) being edited from the web page
        self._task = None
        self._macro_depth = 0
        cf.handlers.append(self.on_message)
        cf.requests[PGN_LANGUAGE] = lambda m: self.language()
        net.progress_listeners.append(self._progress)
        self.H = {
            0xFF: self.c_maintenance, 0xC0: self.c_get_memory, 0xC2: self.c_soft_keys, 0xC3: self.c_font_data,
            0xC7: self.c_hardware, 0xC1: self.c_widechars, 0xC4: self.c_window_mask, 0xC5: self.c_supported,
            0xDF: self.c_get_versions, 0xD0: self.c_store, 0xD1: self.c_load, 0xD2: self.c_delete_version,
            0x11: self.c_pool_transfer, 0x12: self.c_end_of_pool, 0xB2: self.c_delete_pool,
            0xA0: self.c_hide_show, 0xA1: self.c_enable, 0xA2: self.c_select_input, 0x92: self.c_esc,
            0xA3: self.c_audio, 0xA4: self.c_volume, 0xA5: self.c_child_location, 0xA6: self.c_size,
            0xA7: self.c_background, 0xA8: self.c_numeric, 0xA9: self.c_end_point, 0xAA: self.c_font,
            0xAB: self.c_line, 0xAC: self.c_fill, 0xAD: self.c_active_mask, 0xAE: self.c_soft_key_mask,
            0xAF: self.c_attribute, 0xB0: self.c_priority, 0xB1: self.c_list_item, 0xB3: self.c_string,
            0xB4: self.c_child_position, 0xB5: self.c_object_label, 0xB6: self.c_polygon_point,
            0xB7: self.c_polygon_scale, 0xB9: self.c_get_attribute, 0xBB: self.c_identify,
            0xBC: self.c_ext_macro, 0xBD: self.c_lock, 0xBE: self.c_macro, 0x22: self.c_preferred_assignment,
        }

    # ------------------------------------------------------------ lifecycle
    def start(self):
        self._task = asyncio.ensure_future(self._loop())

    def stop(self):
        if self._task:
            self._task.cancel()
        if self._progress in self.net.progress_listeners:
            self.net.progress_listeners.remove(self._progress)
        for t in self.held.values():
            t.cancel()
        self.held.clear()

    async def _loop(self):
        while True:
            await asyncio.sleep(1.0)
            try:
                self.send_status()
                now = time.monotonic()
                for addr, ws in list(self.ws.items()):
                    if not ws.local and now - ws.last_maint > 3.0:
                        self.log("in", addr, "—", "geen Working Set Maintenance meer: werkset verwijderd")
                        self.remove(addr)
            except Exception:
                log.exception("VT-lus")

    def remove(self, addr):
        self.ws.pop(addr, None)
        for k in [k for k in self.held if k[0] == addr]:
            self.held.pop(k).cancel()
        if self.active == addr:
            self.active = next((a for a, w in self.ws.items() if w.state == "actief"), None)
        self.ev.vt_changed()

    # ------------------------------------------------------------ sending
    def send(self, ws, data, prio=6):
        if ws.local:
            return
        data = bytes(data)
        if len(data) < 8:
            data = data.ljust(8, b"\xff")
        self.cf.send(PGN_VT_TO_ECU, data, da=ws.addr, prio=prio)
        self.log("uit", ws.addr, FUNC.get(data[0], f"0x{data[0]:02X}"), data[:24].hex(" "))

    def send_status(self):
        if not self.cf.active:
            return
        ws = self.ws.get(self.active)
        if ws and ws.state == "actief":
            data = bytes([0xFE, ws.addr & 0xFF]) + le(ws.active_mask, 2) + le(ws.soft_key_mask, 2) + bytes([0, 0xFF])
        else:
            data = bytes([0xFE, 0xFE, 0xFF, 0xFF, 0xFF, 0xFF, 0, 0xFF])
        if ws and ws.local:
            data = bytes([0xFE, 0xFE, 0xFF, 0xFF, 0xFF, 0xFF, 0, 0xFF])
        self.cf.send(PGN_VT_TO_ECU, data, da=GLOBAL, prio=7)

    def log(self, direction, addr, func, text):
        self.ev.vtlog({"t": time.time(), "dir": direction, "addr": addr, "func": func, "text": text})

    def _progress(self, sa, da, pgn, got, size, done, aborted):
        if da != self.cf.address or pgn != PGN_ECU_TO_VT or size < 64:
            return
        ws = self.ws.get(sa)
        if ws:
            ws.progress = None if done else {"got": got, "size": size, "aborted": aborted}
            if aborted is not None:
                ws.error = f"overdracht afgebroken (reden {aborted})"
            self.ev.vt_changed(throttle=True)

    # ------------------------------------------------------------ receiving
    def on_message(self, msg):
        if msg.pgn != PGN_ECU_TO_VT or msg.da != self.cf.address or not msg.data:
            return
        ws = self.ws.get(msg.sa)
        if ws is None:
            ws = self.ws[msg.sa] = WorkingSet(msg.sa, self.net.addresses.get(msg.sa, 0))
            self.log("in", msg.sa, "—", "nieuwe werkset")
            self.ev.vt_changed()
        self.command(ws, msg.data)

    def command(self, ws, d, from_macro=False):
        f = d[0]
        if not from_macro and f not in (0x11, 0xFF):
            self.log("in", ws.addr, FUNC.get(f, f"0x{f:02X}"), d[:24].hex(" ") + (" …" if len(d) > 24 else ""))
        h = self.H.get(f)
        if h is None:
            if 0x20 <= f <= 0x27 or from_macro:
                return
            self.send(ws, bytes([0xFD, f]) + b"\xff" * 6)
            return
        try:
            resp = h(ws, d)
        except (IndexError, struct.error):
            resp = None
            self.log("in", ws.addr, FUNC.get(f, "?"), "bericht te kort")
        if resp is not None and not from_macro:
            self.send(ws, resp)

    # ----- technical data
    def c_maintenance(self, ws, d):
        ws.last_maint = time.monotonic()
        if len(d) > 2:
            ws.vt_version = d[2]
        if d[1] & 1 and ws.state == "actief" and ws.objects:
            self.log("in", ws.addr, "Working Set Maintenance", "initiating-bit: werkset herstart, pool gewist")
            ws.objects, ws.pool, ws.state, ws.ws_id = {}, b"", "verbonden", None
            self.ev.vt_changed()

    def c_get_memory(self, ws, d):
        need = u32(d, 2)
        ok = need <= int(self.cfg.get("memory", 16 * 1024 * 1024))
        return bytes([0xC0, int(self.cfg.get("version", 4)), 0 if ok else 1])

    def c_soft_keys(self, ws, d):
        c = self.cfg
        return bytes([0xC2, 0, 0xFF, 0xFF, int(c["skWidth"]), int(c["skHeight"]), int(c.get("virtualKeys", 64)), int(c["skCount"])])

    def c_font_data(self, ws, d):
        return bytes([0xC3, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0x7F, 0xFF])

    def c_hardware(self, ws, d):
        s = int(self.cfg["dmSize"])
        return bytes([0xC7, 0xFF, 2, 0x01]) + le(s, 2) + le(s, 2)

    def c_widechars(self, ws, d):
        return bytes([0xC1, d[1]]) + bytes(d[2:6]) + bytes([0x01, 0])

    def c_window_mask(self, ws, d):
        return bytes([0xC4, 1, 7])

    def c_supported(self, ws, d):
        return bytes([0xC5, len(SUPPORTED_TYPES)] + SUPPORTED_TYPES)

    # ----- versions (stored per NAME in data/versions)
    def _vdir(self, ws):
        return os.path.join(self.dir, f"{ws.name:016X}" if ws.name else f"adres_{ws.addr:02X}")

    def labels(self, ws):
        p = self._vdir(ws)
        if not os.path.isdir(p):
            return []
        return sorted(bytes.fromhex(f[:-4]) for f in os.listdir(p) if f.endswith(".iop") and len(f) == 18)

    def c_get_versions(self, ws, d):
        labels = self.labels(ws)[:255]
        return bytes([0xE0, len(labels)]) + b"".join(labels)

    def c_store(self, ws, d):
        label = bytes(d[1:8])
        err = 0
        if not ws.pool:
            err = 0x04
        else:
            try:
                os.makedirs(self._vdir(ws), exist_ok=True)
                with open(os.path.join(self._vdir(ws), label.hex() + ".iop"), "wb") as fh:
                    fh.write(ws.pool)
                ws.label = label.decode("latin-1").rstrip()
            except OSError:
                err = 0x04
        self.ev.vt_changed()
        return bytes([0xD0, 0xFF, 0xFF, 0xFF, 0xFF, err])

    def c_load(self, ws, d):
        label = bytes(d[1:8])
        path = os.path.join(self._vdir(ws), label.hex() + ".iop")
        if not os.path.exists(path):
            return bytes([0xD1, 0xFF, 0xFF, 0xFF, 0xFF, 0x02])
        with open(path, "rb") as fh:
            data = fh.read()
        try:
            self.install(ws, data, parse_pool(data))
        except PoolError as e:
            ws.error = f"opgeslagen versie onleesbaar: {e}"
            return bytes([0xD1, 0xFF, 0xFF, 0xFF, 0xFF, 0x01])
        ws.label = label.decode("latin-1").rstrip()
        self.log("in", ws.addr, "Load Version", f"versie \"{ws.label}\" geladen ({len(data)} bytes)")
        return bytes([0xD1, 0xFF, 0xFF, 0xFF, 0xFF, 0])

    def c_delete_version(self, ws, d):
        path = os.path.join(self._vdir(ws), bytes(d[1:8]).hex() + ".iop")
        if not os.path.exists(path):
            return bytes([0xD2, 0xFF, 0xFF, 0xFF, 0xFF, 0x02])
        os.remove(path)
        return bytes([0xD2, 0xFF, 0xFF, 0xFF, 0xFF, 0])

    # ----- pool
    def c_pool_transfer(self, ws, d):
        if ws.state != "uploaden":
            ws.updating = ws.state == "actief" and bool(ws.objects)
            ws.buf = bytearray()
            ws.state = "uploaden"
            ws.error = ""
            self.ev.vt_changed()
        ws.buf += d[1:]
        self.log("in", ws.addr, "Object Pool Transfer", f"{len(d) - 1} bytes (totaal {len(ws.buf)})")

    def c_end_of_pool(self, ws, d):
        data = bytes(ws.buf)
        ws.buf = bytearray()
        try:
            objs = parse_pool(data)
        except PoolError as e:
            return self._pool_failed(ws, str(e), e.obj_id, e.parent_id, 0x01 if "ondersteund" in str(e) else 0x04)
        if ws.updating:  # partial pool update (version 4): replace / add objects
            merged = dict(ws.objects)
            merged.update({o["id"]: o for o in objs})
            objs = list(merged.values())
            data = ws.pool + data
        errors, warnings = validate(objs)
        if errors:
            t, oid, pid = errors[0]
            return self._pool_failed(ws, t, oid, pid, 0x04)
        if warnings and self.cfg.get("strict"):
            t, oid, pid = warnings[0]
            return self._pool_failed(ws, t, oid, pid, 0x02)
        self.install(ws, data, objs, warnings)
        self.log("in", ws.addr, "End of Object Pool",
                 f"pool OK: {len(objs)} objecten, {len(data)} bytes" + (f", {len(warnings)} waarschuwingen" if warnings else ""))
        return bytes([0x12, 0, 0xFF, 0xFF, 0xFF, 0xFF, 0, 0xFF])

    def _pool_failed(self, ws, text, oid, pid, code):
        ws.state = "fout"
        ws.error = text
        self.log("in", ws.addr, "End of Object Pool", "pool geweigerd: " + text)
        self.ev.vt_changed()
        return bytes([0x12, 0x01]) + le(pid, 2) + le(oid, 2) + bytes([code, 0xFF])

    def install(self, ws, data, objs, warnings=None):
        ws.objects = {o["id"]: o for o in objs}
        ws.ws_id = next(o["id"] for o in objs if o["type"] == 0)
        ws.pool = data
        ws.state = "actief"
        ws.error = ""
        ws.warnings = warnings if warnings is not None else validate(objs)[1]
        ws.progress = None
        ws.updating = False
        ws.uploaded_at = time.time()
        ws.selected = NONE
        if self.active is None or self.ws.get(self.active) is None or self.ws[self.active].state != "actief":
            self.active = ws.addr
        self.ev.pool_installed(ws)
        self.ev.vt_changed()
        self.send_status()

    def c_delete_pool(self, ws, d):
        ws.objects, ws.pool, ws.state, ws.ws_id = {}, b"", "verbonden", None
        self.ev.vt_changed()
        return bytes([0xB2, 0])

    def open_local(self, data, title="bestand"):
        """Shows a pool from a file without an implement (web page 'Pool openen')."""
        objs = parse_pool(data)
        errors, warnings = validate(objs)
        if errors:
            raise PoolError(errors[0][0])
        ws = self.ws.get(LOCAL) or WorkingSet(LOCAL, 0)
        self.ws[LOCAL] = ws
        ws.label = title
        self.active = LOCAL
        self.install(ws, data, objs, warnings)
        self.active = LOCAL
        self.ev.vt_changed()

    # ------------------------------------------------------------ helpers for commands
    def changed(self, ws, *ids):
        self.ev.objects_changed(ws, [i for i in ids if i in ws.objects])

    def fire(self, ws, obj, event):
        if not obj or self._macro_depth > 8:
            return
        for m in obj.get("macros", []):
            if m["event"] == event:
                self.run_macro(ws, m["macro"])

    def run_macro(self, ws, mid):
        mac = ws.objects.get(mid)
        if not mac or mac["type"] != 28:
            return False
        self._macro_depth += 1
        try:
            for cmd in mac.get("commands", []):
                if cmd and self._macro_depth <= 8:
                    self.command(ws, bytes(cmd), from_macro=True)
        finally:
            self._macro_depth -= 1
        return True

    def _set_value(self, ws, obj, value):
        """Numeric value of an object, following its variable reference. → (target object, old value)."""
        var = ws.objects.get(obj.get("variableReference", NONE))
        target = var if var and var["type"] == 21 else obj
        old = target.get("value")
        target["value"] = value
        return target, old

    # ----- change commands (byte layouts: see FUNC / decoder in PLC-VT-Designer)
    def c_hide_show(self, ws, d):
        oid, show = u16(d, 1), d[3]
        o = ws.obj(oid)
        if not o or o["type"] != 3:
            return bytes([0xA0]) + le(oid, 2) + bytes([show, ERR_ID])
        o["hidden"] = 0 if show else 1
        self.fire(ws, o, EV_SHOW if show else EV_HIDE)
        self.changed(ws, oid)
        return bytes([0xA0]) + le(oid, 2) + bytes([show, 0])

    def c_enable(self, ws, d):
        oid, en = u16(d, 1), d[3]
        o = ws.obj(oid)
        err = 0
        if not o:
            err = ERR_ID
        elif o["type"] in (7, 8):
            o["enabled"] = 1 if en else 0
        elif o["type"] == 9:
            o["options2"] = (o["options2"] & ~1) | (1 if en else 0)
        elif o["type"] == 10:
            o["options"] = (o["options"] & ~1) | (1 if en else 0)
        elif o["type"] == 6:
            o["options"] = (o["options"] & ~16) | (0 if en else 16)
        else:
            err = ERR_OTHER
        if not err:
            self.fire(ws, o, EV_ENABLE if en else EV_DISABLE)
            self.changed(ws, oid)
        return bytes([0xA1]) + le(oid, 2) + bytes([en, err])

    def c_select_input(self, ws, d):
        oid, option = u16(d, 1), d[3] if len(d) > 3 else 0xFF
        o = ws.obj(oid)
        if oid == NONE:
            ws.selected = NONE
            self.ev.vt_changed()
            return bytes([0xA2]) + le(oid, 2) + bytes([0, 0])
        if not o or o["type"] not in INPUTS:
            return bytes([0xA2]) + le(oid, 2) + bytes([0, ERR_ID])
        ws.selected = oid
        self.fire(ws, o, EV_SELECT)
        self.ev.vt_changed()
        if option == 0:
            self.ev.edit(ws, oid)
        return bytes([0xA2]) + le(oid, 2) + bytes([2 if option == 0 else 1, 0])

    def c_esc(self, ws, d):
        oid = self.edit[1] if self.edit and self.edit[0] == ws.addr else NONE
        if oid != NONE:
            self.edit = None
            self.ev.edit(ws, None)
        return bytes([0x92]) + le(oid, 2) + bytes([0 if oid != NONE else 1])

    def c_audio(self, ws, d):
        self.ev.audio({"reps": d[1], "freq": u16(d, 2), "on": u16(d, 4), "off": u16(d, 6)})
        return bytes([0xA3, 0])

    def c_volume(self, ws, d):
        self.ev.audio({"volume": d[1]})
        return bytes([0xA4, 0])

    def _children_of(self, ws, pid, cid):
        p = ws.obj(pid)
        if not p or "children" not in p:
            return None
        return [c for c in p["children"] if c["id"] == cid]

    def c_child_location(self, ws, d):
        pid, cid = u16(d, 1), u16(d, 3)
        cs = self._children_of(ws, pid, cid)
        if not cs:
            return bytes([0xA5]) + le(pid, 2) + le(cid, 2) + bytes([ERR_ID])
        for c in cs:
            c["x"] += d[5] - 127
            c["y"] += d[6] - 127
        self.fire(ws, ws.obj(cid), EV_LOCATION)
        self.changed(ws, pid)
        return bytes([0xA5]) + le(pid, 2) + le(cid, 2) + bytes([0])

    def c_child_position(self, ws, d):
        pid, cid = u16(d, 1), u16(d, 3)
        x, y = struct.unpack_from("<hh", d, 5)
        cs = self._children_of(ws, pid, cid)
        if not cs:
            return bytes([0xB4]) + le(pid, 2) + le(cid, 2) + bytes([ERR_ID])
        for c in cs:
            c["x"], c["y"] = x, y
        self.fire(ws, ws.obj(cid), EV_POSITION)
        self.changed(ws, pid)
        return bytes([0xB4]) + le(pid, 2) + le(cid, 2) + bytes([0])

    def c_size(self, ws, d):
        oid, w, h = u16(d, 1), u16(d, 3), u16(d, 5)
        o = ws.obj(oid)
        if not o or "width" not in KIND.get(o["type"], {}):
            return bytes([0xA6]) + le(oid, 2) + bytes([ERR_ID])
        if o["type"] == 16 and o.get("width") and o.get("height"):
            for p in o["points"]:
                p["x"] = round(p["x"] * w / o["width"])
                p["y"] = round(p["y"] * h / o["height"])
        o["width"] = w
        if "height" in KIND[o["type"]]:
            o["height"] = h
        self.fire(ws, o, EV_SIZE)
        self.changed(ws, oid)
        return bytes([0xA6]) + le(oid, 2) + bytes([0])

    def c_background(self, ws, d):
        oid, col = u16(d, 1), d[3]
        o = ws.obj(oid)
        if not o or "bgColour" not in KIND.get(o["type"], {}):
            return bytes([0xA7]) + le(oid, 2) + bytes([col, ERR_ID])
        o["bgColour"] = col
        self.fire(ws, o, EV_BG)
        self.changed(ws, oid)
        return bytes([0xA7]) + le(oid, 2) + bytes([col, 0])

    def c_numeric(self, ws, d):
        oid, value = u16(d, 1), u32(d, 4)
        o = ws.obj(oid)
        ok_types = (7, 9, 10, 12, 17, 18, 19, 21, 27, 37)
        if not o or o["type"] not in ok_types:
            return bytes([0xA8]) + le(oid, 2) + bytes([ERR_ID]) + le(value, 4)
        if o["type"] == 27 and value != NONE and value not in ws.objects:
            return bytes([0xA8]) + le(oid, 2) + bytes([ERR_VALUE]) + le(value, 4)
        if o["type"] in (7, 10, 37):
            value &= 0xFF
        elif o["type"] in (17, 18, 19, 27):
            value &= 0xFFFF
        old = o.get("value")
        o["value"] = value
        if old != value:
            self.fire(ws, o, EV_VALUE)
        self.changed(ws, oid)
        return bytes([0xA8]) + le(oid, 2) + bytes([0]) + le(value, 4)

    def c_string(self, ws, d):
        oid, ln = u16(d, 1), u16(d, 3)
        o = ws.obj(oid)
        if not o or o["type"] not in (8, 11, 22):
            return bytes([0xB3, 0xFF, 0xFF]) + le(oid, 2) + bytes([ERR_ID])
        text = bytes(d[5:5 + ln]).decode("latin-1")
        err = 0
        if len(text) > o.get("length", 0):
            text, err = text[: o.get("length", 0)], ERR_VALUE
        o["value"] = text.rstrip(" ")
        self.fire(ws, o, EV_VALUE)
        self.changed(ws, oid)
        return bytes([0xB3, 0xFF, 0xFF]) + le(oid, 2) + bytes([err])

    def c_end_point(self, ws, d):
        oid = u16(d, 1)
        o = ws.obj(oid)
        if not o or o["type"] != 13:
            return bytes([0xA9]) + le(oid, 2) + bytes([ERR_ID])
        o["width"], o["height"], o["lineDirection"] = u16(d, 3), u16(d, 5), d[7] & 1
        self.fire(ws, o, EV_ENDPOINT)
        self.changed(ws, oid)
        return bytes([0xA9]) + le(oid, 2) + bytes([0])

    def _attr_cmd(self, ws, d, typ, fields, event):
        oid = u16(d, 1)
        o = ws.obj(oid)
        if not o or o["type"] != typ:
            return bytes([d[0]]) + le(oid, 2) + bytes([ERR_ID])
        p = 3
        for name, size in fields:
            if size is None:
                p += 1
                continue
            v = d[p] if size == 1 else u16(d, p)
            if name != "-" and not (size == 1 and v == 0xFF and name in ("fontSize", "fontType", "fontStyle")):
                o[name] = v
            p += size
        self.fire(ws, o, event)
        self.changed(ws, oid)
        return bytes([d[0]]) + le(oid, 2) + bytes([0])

    def c_font(self, ws, d):
        return self._attr_cmd(ws, d, 23, [("fontColour", 1), ("fontSize", 1), ("fontType", 1), ("fontStyle", 1)], EV_FONT)

    def c_line(self, ws, d):
        return self._attr_cmd(ws, d, 24, [("lineColour", 1), ("lineWidth", 1), ("lineArt", 2)], EV_LINE)

    def c_fill(self, ws, d):
        return self._attr_cmd(ws, d, 25, [("fillType", 1), ("fillColour", 1), ("fillPattern", 2)], EV_FILL)

    def c_active_mask(self, ws, d):
        wid, mid = u16(d, 1), u16(d, 3)
        m = ws.obj(mid)
        if wid != ws.ws_id:
            return bytes([0xAD]) + le(mid, 2) + bytes([0x01])
        if not m or m["type"] not in (1, 2):
            return bytes([0xAD]) + le(mid, 2) + bytes([0x02])
        old = ws.objects.get(ws.active_mask)
        if old is not m:
            self.fire(ws, old, EV_HIDE)
        ws.ws_obj["activeMask"] = mid
        self.fire(ws, ws.ws_obj, EV_ACTIVE_MASK)
        self.fire(ws, m, EV_SHOW)
        self.abort_held(ws)
        # a high or medium priority alarm brings its working set to the front
        if m["type"] == 2 and m.get("priority", 2) <= 1 and self.active != ws.addr:
            self.active = ws.addr
        self.ev.vt_changed()
        self.ev.objects_changed(ws, [mid])
        self.send_status()
        return bytes([0xAD]) + le(mid, 2) + bytes([0])

    def c_soft_key_mask(self, ws, d):
        mid, sid = u16(d, 2), u16(d, 4)
        m, s = ws.obj(mid), ws.obj(sid)
        if not m or m["type"] not in (1, 2):
            err = 0x01
        elif sid != NONE and (not s or s["type"] != 4):
            err = 0x02
        else:
            m["softKeyMask"] = sid
            self.abort_held(ws)
            self.fire(ws, m, EV_SKM)
            self.ev.vt_changed()
            self.changed(ws, mid)
            if ws.addr == self.active and mid == ws.active_mask:
                self.send_status()
            err = 0
        return bytes([0xAE]) + le(mid, 2) + le(sid, 2) + bytes([err])

    def c_attribute(self, ws, d):
        oid, aid = u16(d, 1), d[3]
        o = ws.obj(oid)
        if not o:
            return bytes([0xAF]) + le(oid, 2) + bytes([aid, ERR_ID])
        names = AIDS.get(o["type"], [])
        if not 1 <= aid <= len(names) or (o["type"] == 20 and aid > 3):
            return bytes([0xAF]) + le(oid, 2) + bytes([aid, 0x04])
        name = names[aid - 1]
        kind = KIND[o["type"]][name]
        raw = bytes(d[4:8])
        if kind == "f32":
            v = struct.unpack("<f", raw)[0]
        elif kind == "i32":
            v = struct.unpack("<i", raw)[0]
        elif kind in ("u16", "ref"):
            v = u16(raw, 0)
        elif kind == "i16":
            v = struct.unpack_from("<h", raw)[0]
        elif kind == "u8":
            v = raw[0]
        else:
            v = u32(raw, 0)
        if kind == "ref" and v != NONE and v not in ws.objects:
            return bytes([0xAF]) + le(oid, 2) + bytes([aid, ERR_VALUE])
        o[name] = v
        self.fire(ws, o, EV_ATTR)
        self.changed(ws, oid)
        if name == "softKeyMask" or name == "activeMask":
            self.ev.vt_changed()
        return bytes([0xAF]) + le(oid, 2) + bytes([aid, 0])

    def c_get_attribute(self, ws, d):
        oid, aid = u16(d, 1), d[3]
        o = ws.obj(oid)
        names = AIDS.get(o["type"], []) if o else []
        if aid == 0 and o:
            return bytes([0xB9]) + le(oid, 2) + bytes([0]) + le(o["type"], 4)
        if not o or not 1 <= aid <= len(names):
            return bytes([0xB9]) + le(oid, 2) + bytes([0xFF, ERR_ID if not o else 0x04])
        name = names[aid - 1]
        kind = KIND[o["type"]][name]
        v = o.get(name, 0)
        raw = struct.pack("<f", v) if kind == "f32" else struct.pack("<i", v) if kind in ("i32", "i16") else le(int(v) & 0xFFFFFFFF, 4)
        return bytes([0xB9]) + le(oid, 2) + bytes([aid]) + raw

    def c_priority(self, ws, d):
        oid, prio = u16(d, 1), d[3]
        o = ws.obj(oid)
        if not o or o["type"] != 2 or prio > 2:
            return bytes([0xB0]) + le(oid, 2) + bytes([prio, ERR_ID if not o else ERR_VALUE])
        o["priority"] = prio
        self.fire(ws, o, EV_PRIORITY)
        self.changed(ws, oid)
        return bytes([0xB0]) + le(oid, 2) + bytes([prio, 0])

    def c_list_item(self, ws, d):
        lid, idx, new = u16(d, 1), d[3], u16(d, 4)
        o = ws.obj(lid)
        err = 0
        if not o or o["type"] not in (10, 37):
            err = ERR_ID
        elif idx >= len(o["items"]):
            err = ERR_VALUE
        elif new != NONE and new not in ws.objects:
            err = 0x04
        else:
            o["items"][idx] = new
            self.changed(ws, lid)
        return bytes([0xB1]) + le(lid, 2) + bytes([idx]) + le(new, 2) + bytes([err])

    def c_object_label(self, ws, d):
        oid, sv, ft, gr = u16(d, 1), u16(d, 3), d[5], u16(d, 6)
        lst = next((o for o in ws.objects.values() if o["type"] == 40), None)
        if not lst or oid not in ws.objects:
            return bytes([0xB5]) + le(oid, 2) + bytes([ERR_ID])
        lst["labels"] = [x for x in lst["labels"] if x["id"] != oid] + [{"id": oid, "stringVariable": sv, "fontType": ft, "graphic": gr}]
        return bytes([0xB5]) + le(oid, 2) + bytes([0])

    def c_polygon_point(self, ws, d):
        oid, idx, x, y = u16(d, 1), d[3], u16(d, 4), u16(d, 6)
        o = ws.obj(oid)
        if not o or o["type"] != 16 or idx >= len(o["points"]):
            return bytes([0xB6]) + le(oid, 2) + bytes([ERR_ID if not o else ERR_VALUE])
        o["points"][idx] = {"x": x, "y": y}
        self.changed(ws, oid)
        return bytes([0xB6]) + le(oid, 2) + bytes([0])

    def c_polygon_scale(self, ws, d):
        oid, w, h = u16(d, 1), u16(d, 3), u16(d, 5)
        o = ws.obj(oid)
        if not o or o["type"] != 16:
            return bytes([0xB7]) + le(oid, 2) + le(w, 2) + le(h, 2) + bytes([ERR_ID])
        self.c_size(ws, bytes([0xA6]) + le(oid, 2) + le(w, 2) + le(h, 2))
        return bytes([0xB7]) + le(oid, 2) + le(w, 2) + le(h, 2) + bytes([0])

    def c_identify(self, ws, d):
        self.ev.identify()
        return None

    def c_macro(self, ws, d):
        ok = self.run_macro(ws, d[1])
        return bytes([0xBE, d[1], 0 if ok else ERR_ID])

    def c_ext_macro(self, ws, d):
        mid = u16(d, 1)
        ok = self.run_macro(ws, mid)
        return bytes([0xBC]) + le(mid, 2) + bytes([0 if ok else ERR_ID])

    def c_lock(self, ws, d):
        ws.locked = d[1] == 1
        self.ev.vt_changed()
        return bytes([0xBD, 0])

    def c_preferred_assignment(self, ws, d):
        return bytes([0x22, 0])

    # ------------------------------------------------------------ operator input (web page)
    def _ws_for_input(self, addr):
        ws = self.ws.get(addr)
        if not ws or ws.state != "actief" or ws.locked:
            return None
        return ws

    def set_active(self, addr):
        if addr in self.ws and self.ws[addr].state == "actief":
            self.active = addr
            self.ev.vt_changed()
            self.send_status()

    def key(self, addr, oid, action, kind="key"):
        """Soft key (kind 'key') or button: action press | release | abort."""
        ws = self._ws_for_input(addr)
        o = ws.obj(oid) if ws else None
        if not o or o["type"] not in (5, 6):
            return
        fc = 0x00 if o["type"] == 5 else 0x01
        k = (addr, oid)
        if o["type"] == 6 and o["options"] & 16:
            return  # disabled button
        if o["type"] == 6 and o["options"] & 1:  # latchable button: toggles on press
            if action != "press":
                return
            o["options"] ^= 2
            code = 1 if o["options"] & 2 else 0
            self._activation(ws, fc, code, o)
            self.fire(ws, o, EV_KEY_PRESS if code else EV_KEY_RELEASE)
            self.changed(ws, oid)
            return
        if action == "press":
            if k in self.held:
                return
            self._activation(ws, fc, 1, o)
            self.fire(ws, o, EV_KEY_PRESS)
            self.held[k] = asyncio.ensure_future(self._hold(ws, fc, o))
            if o["type"] == 6:
                o["options"] |= 2
                self.changed(ws, oid)
        else:
            t = self.held.pop(k, None)
            if t is None:
                return
            t.cancel()
            self._activation(ws, fc, 0 if action == "release" else 3, o)
            self.fire(ws, o, EV_KEY_RELEASE)
            if o["type"] == 6:
                o["options"] &= ~2
                self.changed(ws, oid)

    def abort_held(self, ws):
        """Keys that are held while the mask changes are reported as aborted (activation code 3)."""
        for k in [k for k in self.held if k[0] == ws.addr]:
            self.held.pop(k).cancel()
            o = ws.obj(k[1])
            if o:
                self._activation(ws, 0x00 if o["type"] == 5 else 0x01, 3, o)
                if o["type"] == 6 and not o["options"] & 1:
                    o["options"] &= ~2
                    self.changed(ws, o["id"])

    async def _hold(self, ws, fc, o):
        while True:
            await asyncio.sleep(0.2)
            self._activation(ws, fc, 2, o, log_it=False)

    def _activation(self, ws, fc, code, o, log_it=True):
        data = bytes([fc, code]) + le(o["id"], 2) + le(ws.active_mask, 2) + bytes([o.get("keyCode", 0), 0xFF])
        if ws.local:
            return
        self.cf.send(PGN_VT_TO_ECU, data, da=ws.addr, prio=5)
        if log_it:
            self.log("uit", ws.addr, FUNC[fc], f"{['losgelaten', 'ingedrukt', 'vastgehouden', 'afgebroken'][code]}, "
                                              f"object {o['id']}, toetscode {o.get('keyCode', 0)}")

    def select(self, addr, oid, open_edit):
        ws = self._ws_for_input(addr)
        if not ws:
            return
        o = ws.obj(oid)
        if open_edit:
            self.edit = (addr, oid)
        elif self.edit and self.edit == (addr, oid):
            self.edit = None
        ws.selected = oid if open_edit else NONE
        if o:
            self.fire(ws, o, EV_SELECT if open_edit else EV_DESELECT)
        self.send(ws, bytes([0x03]) + le(oid, 2) + bytes([1 if open_edit else 0, 1 if open_edit else 0]))
        self.ev.vt_changed()

    def esc(self, addr, oid):
        ws = self._ws_for_input(addr)
        if not ws:
            return
        self.edit = None
        ws.selected = NONE
        self.fire(ws, ws.obj(oid), EV_ESC)
        self.send(ws, bytes([0x04]) + le(oid, 2) + bytes([0]))
        self.ev.vt_changed()

    def enter_number(self, addr, oid, value):
        ws = self._ws_for_input(addr)
        o = ws.obj(oid) if ws else None
        if not o or o["type"] not in (7, 9, 10):
            return
        value = int(value) & 0xFFFFFFFF
        if o["type"] == 9:
            value = max(o["minValue"], min(o["maxValue"], value))
        target, old = self._set_value(ws, o, value)
        self.edit = None
        ws.selected = NONE
        self.send(ws, bytes([0x05]) + le(oid, 2) + b"\xff" + le(value, 4))
        self.fire(ws, o, EV_ENTRY)
        if old != value:
            self.fire(ws, o, EV_NEW_ENTRY)
        self.changed(ws, oid, target["id"])
        self.ev.vt_changed()

    def enter_string(self, addr, oid, text):
        ws = self._ws_for_input(addr)
        o = ws.obj(oid) if ws else None
        if not o or o["type"] != 8:
            return
        var = ws.objects.get(o.get("variableReference", NONE))
        target = var if var and var["type"] == 22 else o
        text = str(text)[: target.get("length", 0)]
        old = target.get("value")
        target["value"] = text.rstrip(" ")
        enc = text.encode("latin-1", "replace")
        self.edit = None
        ws.selected = NONE
        data = bytes([0x08]) + le(oid, 2) + bytes([len(enc)]) + enc
        self.send(ws, data)
        self.fire(ws, o, EV_ENTRY)
        if old != target["value"]:
            self.fire(ws, o, EV_NEW_ENTRY)
        self.changed(ws, oid, target["id"])
        self.ev.vt_changed()

    def pointing(self, addr, x, y, state):
        ws = self._ws_for_input(addr)
        if ws and int(self.cfg.get("version", 4)) >= 4:
            self.send(ws, bytes([0x02]) + le(max(0, int(x)), 2) + le(max(0, int(y)), 2) + bytes([state]))

    def pool_bytes(self, addr):
        ws = self.ws.get(addr)
        return ws.pool if ws else None

    def describe_object(self, ws, oid):
        o = ws.obj(oid)
        return f"{type_name(o['type'])} {oid}" if o else str(oid)


__all__ = ["VTServer", "WorkingSet", "LOCAL", "FUNC", "encode_object", "TYPES"]
