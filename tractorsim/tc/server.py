"""Task controller server (ISO 11783-10): TC-BAS (documentation), TC-SC (section control) and TC-GEO (prescription).

Process data, PGN 0xCB00. Byte 1: command (bits 1-4) and the low nibble of the element number (bits 5-8); byte 2: rest
of the element number; bytes 3-4: DDI; bytes 5-8: value (int32). Commands: 0 technical data (version), 1 device
descriptor (labels, DDOP transfer, activation), 2 request value, 3 value, 4-8 measurement commands (time, distance,
minimum/maximum threshold, change), 10 set value and acknowledge, 13 process data acknowledge, 14 TC status (every
2 s), 15 client task (every 2 s from each client; a client that stays silent for 6 s is dropped).

The DDOP of a client is kept per NAME (data_dir/tc/<NAME>.ddop): a client with the same structure and localization
labels activates it again without a transfer. After activation the TC sets the measurement triggers of every process
data object from its trigger methods and asks every value once.
"""
import asyncio
import json
import logging
import math
import os
import struct
import time

from ..j1939 import GLOBAL, name_text
from . import ddop as D
from .field import Autopilot, Coverage, Field, Frame
from .prescription import MapError, Prescription
from .section import SectionControl

log = logging.getLogger(__name__)

PGN_PD = 0xCB00
PGN_WS_MASTER = 0xFE0D
PGN_LANGUAGE = 0xFE0F
PGN_VEHICLE_POSITION = 0xFEF3
PGN_VEHICLE_DIR_SPEED = 0xFEE8
PGN_N2K_POSITION = 0x1F801
PGN_N2K_COG_SOG = 0x1F802

CMD = {0: "Technische gegevens", 1: "Device descriptor", 2: "Request Value", 3: "Value", 4: "Meting: tijd",
       5: "Meting: afstand", 6: "Meting: min. drempel", 7: "Meting: max. drempel", 8: "Meting: bij verandering",
       9: "Peer control", 10: "Set Value and Acknowledge", 13: "Process Data Acknowledge", 14: "TC Status", 15: "Client Task"}
DD = {0x01: "Request Structure Label", 0x11: "Structure Label", 0x21: "Request Localization Label", 0x31: "Localization Label",
      0x41: "Request Object-pool Transfer", 0x51: "Request Object-pool Transfer Response", 0x61: "Object-pool Transfer",
      0x71: "Object-pool Transfer Response", 0x81: "Object-pool Activate/Deactivate", 0x91: "Object-pool Activate/Deactivate Response",
      0xA1: "Object-pool Delete", 0xB1: "Object-pool Delete Response", 0xC1: "Change Designator", 0xD1: "Change Designator Response"}
TECH = {0x00: "Request Version", 0x10: "Version", 0x20: "Identify TC"}

# option bits of the version message
OPT_DOC, OPT_GEO_NO_POS, OPT_GEO_POS, OPT_PEER, OPT_SC = 0x01, 0x02, 0x04, 0x08, 0x10
STRUCTURAL = {D.DDI_SECTION_CONTROL_STATE, D.DDI_PRESCRIPTION_STATE, D.DDI_SETPOINT_WORK_STATE, 119}


def _pd(cmd, element, ddi, value):
    return bytes([(cmd & 0x0F) | ((element & 0x0F) << 4), (element >> 4) & 0xFF]) + struct.pack("<HI", ddi & 0xFFFF, int(value) & 0xFFFFFFFF)


class Client:
    """A TC client (working set master) and its DDOP."""

    def __init__(self, addr, name):
        self.addr, self.name = addr, name
        self.state = "verbonden"  # verbonden | overdracht | actief | fout
        self.version = None
        self.last_task = time.monotonic()
        self.task_byte = 0
        self.pending = bytearray()
        self.pool = None  # activated D.Pool
        self.stored = None  # D.Pool kept for this NAME
        self.values = {}  # (element, DDI) → value
        self.times = {}  # (element, DDI) → time of the value
        self.sent = {}  # (element, DDI) → last value the TC sent
        self.acks = {}  # (element, DDI) → error code of the last PDACK
        self.totals0 = {}
        self.dirty = set()
        self.progress = None
        self.error = ""
        self.geometry = None

    def summary(self):
        p = self.pool or self.stored
        return {"addr": self.addr, "name": f"{self.name:016X}", "nameText": name_text(self.name) if self.name else "",
                "state": self.state, "designator": p.designator if p else "", "active": self.pool is not None,
                "progress": self.progress, "error": self.error,
                "version": self.version, "sections": len(self.geometry["sections"]) if self.geometry else 0}


class BusPosition:
    """Position of another GNSS receiver on the bus: Vehicle Position / Direction-Speed (ISO 11783-7) or NMEA 2000
    Position Rapid Update / COG & SOG Rapid Update."""

    def __init__(self, net):
        self.pos = None
        self.heading = 0.0
        self.speed = 0.0
        self.t = 0.0
        self.source = None
        net.listeners.append(self._msg)

    def _msg(self, m):
        d = m.data
        if m.pgn == PGN_VEHICLE_POSITION and len(d) >= 8:
            lat, lon = struct.unpack_from("<II", d)
            if lat < 0xFB000000 and lon < 0xFB000000:
                self.pos = (lat * 1e-7 - 210, lon * 1e-7 - 210)
                self.t, self.source = time.monotonic(), m.sa
        elif m.pgn == PGN_N2K_POSITION and len(d) >= 8:
            lat, lon = struct.unpack_from("<ii", d)
            if lat != 0x7FFFFFFF and lon != 0x7FFFFFFF:
                self.pos = (lat * 1e-7, lon * 1e-7)
                self.t, self.source = time.monotonic(), m.sa
        elif m.pgn == PGN_VEHICLE_DIR_SPEED and len(d) >= 4:
            h, v = struct.unpack_from("<HH", d)
            if h < 0xFB00:
                self.heading = h / 128.0
            if v < 0xFB00:
                self.speed = v / 256.0 / 3.6
        elif m.pgn == PGN_N2K_COG_SOG and len(d) >= 6:
            cog, sog = struct.unpack_from("<HH", d, 2)
            if cog < 0xFFFD:
                self.heading = math.degrees(cog * 1e-4) % 360.0
            if sog < 0xFFFD:
                self.speed = sog * 0.01

    def get(self):
        if self.pos is None or time.monotonic() - self.t > 3.0:
            return None
        return {"lat": self.pos[0], "lon": self.pos[1], "heading": self.heading, "speed": self.speed,
                "source": f"bus 0x{self.source:02X}"}


class TCServer:
    def __init__(self, net, cf, cfg, data_dir, events, language, position, opts=None, sim=None):
        """`cfg`: the control function settings (version, booms, sections, channels, memory); `opts`: the settings of
        the TC page (field, section control, prescription, …), kept by the App; `position()` → dict or None."""
        self.net, self.cf, self.cfg, self.ev = net, cf, cfg, events
        self.language = language
        self.position = position
        self.sim = sim
        self.opts = opts if opts is not None else {}
        self.dir = os.path.join(data_dir, "tc")
        os.makedirs(self.dir, exist_ok=True)
        self.clients = {}
        self.task = "uit"  # uit | actief | pauze
        self.task_t0 = None
        self.task_time = 0.0
        self.rows = []  # log, one row per second while the task runs
        self.track = []  # (e, n) every metre
        self.track_sent = 0
        self.field = None
        self.coverage = None
        self.presc = None
        self.sc = SectionControl()
        self.autopilot = None
        self.pose = None  # {"e", "n", "heading", "speed", "lat", "lon", "source"}
        self.states = []  # section states painted (actual)
        self.rate = None  # current prescription rate
        self._task = None
        self._setup_tasks = set()
        self._last = {}
        self._map_dirty = True
        cf.handlers.append(self.on_message)
        cf.requests[PGN_LANGUAGE] = lambda m: self.language()
        net.progress_listeners.append(self._progress)
        self._load_opts()

    # ------------------------------------------------------------ settings
    def _load_opts(self):
        o = self.opts
        o.setdefault("sc", True)
        o.setdefault("scMode", "auto")
        o.setdefault("manual", [])
        o.setdefault("geo", True)
        o.setdefault("lookaheadOn", 0.6)
        o.setdefault("lookaheadOff", 0.3)
        o.setdefault("overlap", 50)
        o.setdefault("hitchX", -2.0)
        o.setdefault("hitchY", 0.0)
        o.setdefault("spacing", 0)
        o.setdefault("target", None)
        self._apply_sc()
        if o.get("field"):
            try:
                self.set_field(Field.from_json(o["field"]), save=False)
            except (KeyError, TypeError, ValueError):
                o["field"] = None
        path = os.path.join(self.dir, "taakkaart.geojson")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    self.presc = Prescription.from_geojson(fh.read(), prop="rate")
            except (OSError, MapError) as e:
                log.warning("taakkaart niet geladen: %s", e)

    def _apply_sc(self):
        o = self.opts
        self.sc.hitch = (float(o["hitchX"]), float(o["hitchY"]))
        self.sc.lookahead_on = float(o["lookaheadOn"])
        self.sc.lookahead_off = float(o["lookaheadOff"])
        self.sc.overlap = max(0.01, min(1.0, float(o["overlap"]) / 100.0))

    def options_byte(self):
        b = OPT_DOC
        if self.opts.get("geo"):
            b |= OPT_GEO_NO_POS | OPT_GEO_POS
        if self.opts.get("sc"):
            b |= OPT_SC
        return b

    # ------------------------------------------------------------ lifecycle
    def start(self):
        self._task = asyncio.ensure_future(self._loop())

    def stop(self):
        if self._task:
            self._task.cancel()
        for t in self._setup_tasks:
            t.cancel()
        if self._progress in self.net.progress_listeners:
            self.net.progress_listeners.remove(self._progress)
        if self.sim is not None and self.sim.autopilot is self.autopilot and self.autopilot is not None:
            self.sim.autopilot = None

    async def _loop(self):
        last = time.monotonic()
        n = 0
        while True:
            await asyncio.sleep(0.1)
            now = time.monotonic()
            dt, last = now - last, now
            n += 1
            try:
                if n % 20 == 0:
                    self.send_status()
                for addr, c in list(self.clients.items()):
                    if now - c.last_task > 6.0:
                        self.log("in", addr, "—", "geen Client Task meer: client verwijderd")
                        self.clients.pop(addr, None)
                        self.ev.tc_changed()
                self.tick(dt, n)
            except Exception:
                log.exception("TC-lus")

    # ------------------------------------------------------------ sending
    def send(self, c, data, what=None):
        data = bytes(data)
        if len(data) < 8:
            data = data.ljust(8, b"\xff")
        self.cf.send(PGN_PD, data, da=c.addr, prio=5)
        if what:
            self.log("uit", c.addr, what, data[:16].hex(" "))

    def pd(self, c, cmd, element, ddi, value, quiet=False):
        self.send(c, _pd(cmd, element, ddi, value), None if quiet else f"{CMD.get(cmd, cmd)} el {element} DDI {ddi} = {value}")
        if cmd in (3, 10):
            c.sent[(element, ddi)] = int(value)
            c.dirty.add((element, ddi))

    def send_status(self):
        if not self.cf.active:
            return
        status = 0x01 if self.task == "actief" else 0x00
        self.cf.send(PGN_PD, bytes([0xFE, 0xFF, 0xFF, 0xFF, status, 0xFF, 0xFF, 0xFF]), da=GLOBAL, prio=6)

    def log(self, direction, addr, func, text):
        self.ev.tclog({"t": time.time(), "dir": direction, "addr": addr, "func": func, "text": text})

    def _progress(self, sa, da, pgn, got, size, done, aborted):
        if da != self.cf.address or pgn != PGN_PD:
            return
        c = self.clients.get(sa)
        if c:
            c.progress = None if done else {"got": got, "size": size, "aborted": aborted}
            if aborted is not None:
                c.error = f"overdracht afgebroken (reden {aborted})"
            self.ev.tc_changed()

    # ------------------------------------------------------------ receiving
    def client(self, sa):
        c = self.clients.get(sa)
        if c is None:
            name = self.net.addresses.get(sa, 0)
            c = self.clients[sa] = Client(sa, name)
            path = self._path(name)
            if name and os.path.exists(path):
                try:
                    with open(path, "rb") as fh:
                        data = fh.read()
                    c.stored = D.Pool(D.parse(data), data)
                except (OSError, D.DdopError, StopIteration) as e:
                    log.warning("bewaarde DDOP van %016X niet leesbaar: %s", name, e)
            self.log("in", sa, "—", "nieuwe client")
            self.send(c, bytes([0x00]) + b"\xff" * 7, "Request Version")  # the version of the client
            self.ev.tc_changed()
        return c

    def _path(self, name):
        return os.path.join(self.dir, f"{name:016X}.ddop")

    def on_message(self, msg):
        if msg.pgn == PGN_WS_MASTER and msg.da in (GLOBAL, self.cf.address):
            if msg.sa in self.clients:
                self.clients[msg.sa].last_task = time.monotonic()
            return
        if msg.pgn != PGN_PD or msg.da != self.cf.address or not msg.data:
            return
        c = self.client(msg.sa)
        d = msg.data
        cmd = d[0] & 0x0F
        try:
            if cmd == 15:  # Client Task
                c.last_task = time.monotonic()
                c.task_byte = d[4] if len(d) > 4 else 0
            elif cmd == 0:
                self.technical(c, d)
            elif cmd == 1:
                self.descriptor(c, d)
            elif cmd in (2, 3, 13) and len(d) >= 8:
                element = (d[0] >> 4) | (d[1] << 4)
                ddi, value = struct.unpack_from("<Hi", d, 2)
                if cmd == 3:
                    self.value(c, element, ddi, value)
                elif cmd == 13:
                    c.acks[(element, ddi)] = d[4]
                    if d[4]:
                        self.log("in", c.addr, "Process Data Acknowledge", f"el {element} DDI {ddi}: fout 0x{d[4]:02X}")
        except (IndexError, struct.error):
            self.log("in", c.addr, CMD.get(cmd, "?"), "bericht te kort")

    def technical(self, c, d):
        sub = d[0]
        self.log("in", c.addr, TECH.get(sub, f"0x{sub:02X}"), d[:8].hex(" "))
        if sub == 0x00:  # the client asks our version
            cfg = self.cfg
            self.send(c, bytes([0x10, int(cfg.get("version", 4)), 0xFF, self.options_byte(), 0x00, int(cfg.get("booms", 1)),
                                int(cfg.get("sections", 16)), int(cfg.get("channels", 1))]), "Version")
        elif sub == 0x10:
            c.version = {"version": d[1], "options": d[3], "booms": d[5], "sections": d[6], "channels": d[7]}
            self.ev.tc_changed()
        elif sub == 0x20:
            self.ev.tc_identify(c.addr)

    def descriptor(self, c, d):
        sub = d[0]
        if sub != 0x61:
            self.log("in", c.addr, DD.get(sub, f"0x{sub:02X}"), d[:16].hex(" "))
        p = c.pool or c.stored
        if sub == 0x01:  # structure label
            if p is None:
                self.send(c, b"\x11" + b"\xff" * 7, "Structure Label (geen)")
            else:
                ext = p.device.get("extendedLabel") or b""
                self.send(c, b"\x11" + p.label + (bytes(ext) if int(self.cfg.get("version", 4)) >= 4 else b""), "Structure Label")
        elif sub == 0x21:
            self.send(c, b"\x31" + (p.localization if p else b"\xff" * 7), "Localization Label")
        elif sub == 0x41:
            size = struct.unpack_from("<I", d, 1)[0]
            ok = size <= int(self.cfg.get("memory", 1 << 20))
            c.pending = bytearray()
            c.state = "overdracht" if ok else c.state
            self.send(c, bytes([0x51, 0 if ok else 1]), f"Request Object-pool Transfer Response ({'ok' if ok else 'te groot'})")
            self.ev.tc_changed()
        elif sub == 0x61:
            part = d[1:]
            self.log("in", c.addr, "Object-pool Transfer", f"{len(part)} bytes")
            c.pending += part
            err = 0
            if len(c.pending) > int(self.cfg.get("memory", 1 << 20)):
                c.pending = bytearray()
                err = 1
            c.progress = None
            self.send(c, bytes([0x71, err]) + struct.pack("<I", len(part)), "Object-pool Transfer Response")
            self.ev.tc_changed()
        elif sub == 0x81:
            self.activate(c, d[1] == 0xFF)
        elif sub == 0xA1:
            c.pending = bytearray()
            existed = c.stored is not None
            c.stored = None
            if c.pool is None and c.name:
                try:
                    os.remove(self._path(c.name))
                except OSError:
                    pass
            self.send(c, bytes([0xB1, 0 if existed else 1, 0xFF]), "Object-pool Delete Response")
            self.ev.tc_changed()
        elif sub == 0xC1:
            oid = struct.unpack_from("<H", d, 1)[0]
            n = d[3]
            text = bytes(d[4:4 + n]).decode("latin-1")
            o = p.by_id.get(oid) if p else None
            if o and "designator" in o:
                o["designator"] = text
                err = 0
                self.ev.tc_pool(c)
            else:
                err = 1
            self.send(c, bytes([0xD1]) + struct.pack("<H", oid) + bytes([err]), "Change Designator Response")

    def activate(self, c, on):
        if not on:
            c.pool = None
            c.state = "verbonden"
            self.send(c, bytes([0x91, 0, 0xFF, 0xFF, 0xFF, 0xFF, 0]), "Object-pool Deactivate Response")
            self.ev.tc_changed()
            return
        data = bytes(c.pending) if c.pending else (c.stored.data if c.stored else b"")
        fail = None
        objects = None
        if not data:
            fail = (D.NONE, D.NONE, D.ERR_OTHER, "geen beschrijving ontvangen")
        else:
            try:
                objects = D.parse(data)
                errs = D.check(objects)
                if errs:
                    fail = errs[0]
            except D.DdopError as e:
                fail = (D.NONE, D.NONE, D.ERR_OTHER, str(e))
        if fail:
            parent, oid, code, text = fail
            c.state, c.error = "fout", f"DDOP geweigerd: {text}"
            self.send(c, bytes([0x91, 0x01]) + struct.pack("<HH", parent, oid) + bytes([code]), f"Activate Response: fout ({text})")
            self.ev.tc_changed()
            return
        pool = D.Pool(objects, data)
        c.pool = c.stored = pool
        c.pending = bytearray()
        c.state, c.error = "actief", ""
        c.values, c.times, c.sent = {}, {}, {}
        c.geometry = pool.sections()
        if c.name:
            try:
                with open(self._path(c.name), "wb") as fh:
                    fh.write(data)
            except OSError as e:
                log.warning("DDOP niet bewaard: %s", e)
        self.send(c, bytes([0x91, 0, 0xFF, 0xFF, 0xFF, 0xFF, 0]), f"Activate Response: ok ({pool.designator})")
        self._configure_sc()
        self.ev.tc_pool(c)
        self.ev.tc_changed()
        t = asyncio.ensure_future(self._setup(c, pool))
        self._setup_tasks.add(t)
        t.add_done_callback(self._setup_tasks.discard)

    async def _setup(self, c, pool):
        """Measurement triggers from the trigger methods of every DPD, then every value once. Spaced, so a PLC that reads
        one frame per cycle can keep up."""
        await asyncio.sleep(0.3)
        for (el, ddi), o in list(pool.dpds.items()):
            if c.pool is not pool:
                return
            if ddi == D.DDI_DEFAULT_PD:
                continue
            t = o["triggers"]
            if t & D.TRIG_TIME:
                self.pd(c, 4, el, ddi, 1000, quiet=True)
                await asyncio.sleep(0.03)
            elif t & D.TRIG_DISTANCE:
                self.pd(c, 5, el, ddi, 1000, quiet=True)
                await asyncio.sleep(0.03)
            if t & D.TRIG_CHANGE:
                self.pd(c, 8, el, ddi, 1, quiet=True)
                await asyncio.sleep(0.03)
        for (el, ddi) in list(pool.dpds):
            if c.pool is not pool:
                return
            if ddi != D.DDI_DEFAULT_PD:
                self.pd(c, 2, el, ddi, 0, quiet=True)
                await asyncio.sleep(0.03)
        self.log("uit", c.addr, "Meetopdrachten", f"{len(pool.dpds)} procesdata ingesteld en opgevraagd")
        self.sync_modes(c)

    def value(self, c, element, ddi, value):
        key = (element, ddi)
        old = c.values.get(key)
        c.values[key] = value
        c.times[key] = time.time()
        c.dirty.add(key)
        if old != value and ddi in (D.DDI_SECTION_CONTROL_STATE, D.DDI_PRESCRIPTION_STATE, D.DDI_WORKING_WIDTH):
            if ddi == D.DDI_WORKING_WIDTH:
                self._configure_sc()
            self.ev.tc_changed()

    # ------------------------------------------------------------ the implement used for SC and GEO
    def implement(self):
        """The active client with sections (or else the first active one)."""
        act = [c for c in self.clients.values() if c.pool is not None]
        return next((c for c in act if c.geometry), act[0] if act else None)

    def _configure_sc(self):
        c = self.implement()
        if c is None or not c.geometry:
            self.sc.configure(None)
            self._map_dirty = True
            return
        boom_w = c.values.get((c.geometry["boom"], D.DDI_WORKING_WIDTH))
        old = [(s["x"], s["y"], s["w"]) for s in self.sc.secs]
        self.sc.configure(c.geometry, boom_w)
        if [(s["x"], s["y"], s["w"]) for s in self.sc.secs] != old:
            self._map_dirty = True

    def sc_auto(self, c):
        """Does the implement follow the section control (DDI 160 = 1, or no such DDI in its DDOP)?"""
        el = c.pool.first(D.DDI_SECTION_CONTROL_STATE) if c.pool else None
        if el is None:
            return True
        return c.values.get((el, D.DDI_SECTION_CONTROL_STATE)) == 1

    def sync_modes(self, c):
        """Sends the section control and prescription control state the operator chose to the implement."""
        if c.pool is None:
            return
        for el, o in c.pool.find(D.DDI_SECTION_CONTROL_STATE):
            if o["props"] & D.PROP_SETTABLE:
                self.pd(c, 3, el, D.DDI_SECTION_CONTROL_STATE, 1 if self.opts.get("sc") else 0)
        target = self.target(c)
        for el, o in c.pool.find(D.DDI_PRESCRIPTION_STATE):
            if o["props"] & D.PROP_SETTABLE and (target is None or el == target["element"]):
                self.pd(c, 3, el, D.DDI_PRESCRIPTION_STATE, 1 if (self.opts.get("geo") and self.presc) else 0)

    def setpoint_candidates(self, c):
        if not c or not c.pool:
            return []
        out = []
        for (el, ddi), o in c.pool.dpds.items():
            if o["props"] & D.PROP_SETTABLE and ddi not in STRUCTURAL and not (D.DDI_SETPOINT_CONDENSED <= ddi < D.DDI_SETPOINT_CONDENSED + 16) \
                    and ddi != D.DDI_DEFAULT_PD:
                dvp = c.pool.presentation(o)
                out.append({"element": el, "ddi": ddi, "designator": o["designator"],
                            "scale": dvp["scale"] if dvp else None, "offset": dvp["offset"] if dvp else 0, "unit": dvp["unit"] if dvp else ""})
        return out

    def target(self, c=None):
        """Element and setpoint DDI the prescription map drives: the operator's choice, else the first candidate."""
        c = c or self.implement()
        cands = self.setpoint_candidates(c)
        t = self.opts.get("target")
        if t:
            for x in cands:
                if x["element"] == t.get("element") and x["ddi"] == t.get("ddi"):
                    return {**x, "scale": x["scale"] or t.get("scale") or 1.0, "unit": x["unit"] or t.get("unit", "")}
        return {**cands[0], "scale": cands[0]["scale"] or 1.0} if cands else None

    # ------------------------------------------------------------ every 0.1 s: position, SC, GEO, documentation
    def tick(self, dt, n):
        pos = self.position()
        if pos is None:
            self.pose = None
            return
        if self.field is None:  # a field where the tractor is, so the coverage has a place
            h = pos["heading"]
            self.set_field(Field.rectangle(pos["lat"], pos["lon"], h, 200, 96, "Perceel"))
        e, nn = self.field.frame.to_local(pos["lat"], pos["lon"])
        speed = pos["speed"]
        self.pose = {"e": e, "n": nn, "heading": pos["heading"], "speed": speed, "lat": pos["lat"], "lon": pos["lon"],
                     "source": pos.get("source", "")}
        if not self.track or math.hypot(e - self.track[-1][0], nn - self.track[-1][1]) >= 1.0:
            if len(self.track) > 30000:
                del self.track[:10000]
                self.track_sent = max(0, self.track_sent - 10000)
            self.track.append((round(e, 2), round(nn, 2)))
        c = self.implement()
        working = self.task == "actief"
        if c and self.sc.secs:
            self._section_control(c, e, nn, pos["heading"], speed, working, n)
        if c and working:
            self._prescription(c, e, nn, pos["heading"], speed, n)
        if working:
            self.task_time += dt
            if n % 10 == 0:
                self._log_row(pos)
            if n % 50 == 0:
                self._request_totals()

    def _section_control(self, c, e, n, heading, speed, working, tick):
        o = self.opts
        count = len(self.sc.secs)
        if o.get("sc") and working and o.get("scMode") == "auto":
            want = self.sc.decide(e, n, heading, speed, self.coverage, self.field)
        elif o.get("sc") and working:
            man = list(o.get("manual") or [])
            want = [bool(man[i]) if i < len(man) else True for i in range(count)]
            self.sc.decided = want
        else:
            want = [False] * count
            self.sc.decided = want
        # send the wanted states (condensed, 16 sections per DDI) on a change and every 2 s
        if o.get("sc") and c.pool and self.sc_auto(c):
            for g in range((count + 15) // 16):
                ddi = D.DDI_SETPOINT_CONDENSED + g
                for el, _ in c.pool.find(ddi):
                    v = D.condensed(want, g)
                    if c.sent.get((el, ddi)) != v or tick % 20 == 0:
                        self.pd(c, 3, el, ddi, v, quiet=c.sent.get((el, ddi)) == v)
        # what the sections really do: the implement's actual condensed work state, else the work state
        actual = [None] * count
        for g in range(16):
            el = c.pool.first(D.DDI_ACTUAL_CONDENSED + g) if c.pool else None
            if el is not None and (el, D.DDI_ACTUAL_CONDENSED + g) in c.values:
                for k, s in D.uncondensed(c.values[(el, D.DDI_ACTUAL_CONDENSED + g)], g, count).items():
                    actual[k] = s
        if all(a is None for a in actual):
            el = c.pool.first(D.DDI_ACTUAL_WORK_STATE) if c.pool else None
            ws = c.values.get((el, D.DDI_ACTUAL_WORK_STATE)) if el is not None else None
            if ws is not None:
                actual = [ws == 1] * count
            else:
                actual = list(want)
        self.states = [bool(a) for a in actual]
        if working:
            self.sc.paint(e, n, heading, self.states, self.coverage)
        else:
            self.sc.reset()

    def _prescription(self, c, e, n, heading, speed, tick):
        if not (self.opts.get("geo") and self.presc and self.field):
            self.rate = None
            return
        t = self.target(c)
        if t is None:
            return
        pe, pn = self.sc.centre(e, n, heading, max(0.0, speed * self.sc.lookahead_on))
        rate = self.presc.rate_at(self.field.frame, pe, pn)
        self.rate = rate
        if rate is None:
            return
        raw = int(round(rate / (t["scale"] or 1.0) - (t.get("offset") or 0)))
        key = (t["element"], t["ddi"])
        if c.sent.get(key) != raw or tick % 20 == 0:
            self.pd(c, 3, t["element"], t["ddi"], raw, quiet=c.sent.get(key) == raw)

    def _request_totals(self):
        for c in self.clients.values():
            if c.pool:
                for (el, ddi), o in c.pool.dpds.items():
                    if o["triggers"] & D.TRIG_TOTAL and ddi != D.DDI_DEFAULT_PD:
                        self.pd(c, 2, el, ddi, 0, quiet=True)

    def _log_row(self, pos):
        if len(self.rows) >= 50000:
            del self.rows[:10000]
        vals = {}
        for c in self.clients.values():
            if c.pool:
                for key in c.pool.dpds:
                    if key in c.values:
                        vals[f"{c.addr:02X}:{key[0]}:{key[1]}"] = c.values[key]
        self.rows.append({"t": time.time(), "lat": round(pos["lat"], 8), "lon": round(pos["lon"], 8), "speed": round(pos["speed"] * 3.6, 2),
                          "heading": round(pos["heading"], 1), "area": round(self.coverage.area() if self.coverage else 0.0, 1),
                          "rate": self.rate, "sections": "".join("1" if s else "0" for s in self.states), "values": vals})

    # ------------------------------------------------------------ operator actions (web page)
    def set_task(self, action):
        prev = self.task
        if action == "start":
            if self.task == "uit":
                self.task_t0 = time.time()
                self.task_time = 0.0
                for c in self.clients.values():
                    c.totals0 = dict(c.values)
            self.task = "actief"
        elif action == "pause" and self.task == "actief":
            self.task = "pauze"
        elif action == "stop":
            self.task = "uit"
        elif action == "reset":
            self.task = "uit"
            self.task_time = 0.0
            self.task_t0 = None
            self.rows = []
            self.track = []
            self.track_sent = 0
            if self.field:
                self.coverage = Coverage.for_field(self.field)
            self._map_dirty = True
        if prev != self.task:
            self.log("uit", GLOBAL, "Taak", {"actief": "gestart", "pauze": "gepauzeerd", "uit": "gestopt"}[self.task])
            on = 1 if self.task == "actief" else 0
            for c in self.clients.values():
                if c.pool:
                    for el, o in c.pool.find(D.DDI_SETPOINT_WORK_STATE):
                        if o["props"] & D.PROP_SETTABLE:
                            self.pd(c, 3, el, D.DDI_SETPOINT_WORK_STATE, on)
            self._request_totals()
            self.send_status()
        self.ev.tc_changed()

    def set_options(self, values):
        o = self.opts
        modes_changed = False
        for k, v in values.items():
            if k in ("sc", "geo"):
                if bool(v) != bool(o.get(k)):
                    modes_changed = True
                o[k] = bool(v)
            elif k == "scMode" and v in ("auto", "handmatig"):
                o[k] = v
            elif k in ("lookaheadOn", "lookaheadOff", "overlap", "hitchX", "hitchY", "spacing"):
                o[k] = float(v)
            elif k == "manual":
                o[k] = [bool(x) for x in v][:256]
            elif k == "target":
                o[k] = v if isinstance(v, dict) else None
                modes_changed = True
        self._apply_sc()
        self._configure_sc()
        self._map_dirty = True
        if modes_changed:
            for c in self.clients.values():
                self.sync_modes(c)
        self.ev.tc_saved()
        self.ev.tc_changed()

    def set_value(self, addr, element, ddi, value):
        c = self.clients.get(addr)
        if c:
            self.pd(c, 3, int(element), int(ddi), int(value))

    def set_field(self, field, save=True):
        self.field = field
        self.coverage = Coverage.for_field(field)
        self.track = []
        self.track_sent = 0
        self.sc.reset()
        if self.autopilot is not None:
            self.set_autopilot(False)
        self.opts["field"] = field.to_json()
        self._map_dirty = True
        if save:
            self.ev.tc_saved()
        self.ev.tc_changed()

    def field_here(self, length, width):
        pos = self.position()
        if pos is None:
            raise ValueError("geen positie: zet de GNSS-ontvanger aan of sluit er een aan")
        self.set_field(Field.rectangle(pos["lat"], pos["lon"], pos["heading"], max(10.0, float(length)), max(5.0, float(width))))

    def field_geojson(self, text):
        obj = json.loads(text)
        feats = obj.get("features", [obj]) if obj.get("type") == "FeatureCollection" else [obj]
        for f in feats:
            g = f.get("geometry") or f
            if g.get("type") == "Polygon":
                ring = g["coordinates"][0]
            elif g.get("type") == "MultiPolygon":
                ring = g["coordinates"][0][0]
            else:
                continue
            fr = Frame(ring[0][1], ring[0][0])
            self.set_field(Field(fr, [fr.to_local(p[1], p[0]) for p in ring[:-1] if p], (f.get("properties") or {}).get("name", "Perceel")))
            return
        raise ValueError("geen polygoon in de GeoJSON")

    def lane_spacing(self):
        s = float(self.opts.get("spacing") or 0)
        return s if s > 0 else (self.sc.width or 24.0)

    def set_autopilot(self, on):
        if self.sim is None:
            raise ValueError("automatisch rijden kan enkel met de eigen GNSS-ontvanger")
        if on:
            if self.field is None or not self.field.rect:
                raise ValueError("automatisch rijden vraagt een rechthoekig perceel (Perceel hier)")
            self.autopilot = Autopilot(self.field, self.lane_spacing())
            self.autopilot.start(self.sim)
            self.sim.autopilot = self.autopilot
            self.sc.reset()
        else:
            if self.sim.autopilot is self.autopilot:
                self.sim.autopilot = None
            self.autopilot = None
        self.ev.tc_changed()

    def load_map(self, kind, data, name, prop=None):
        if kind == "shp":
            p = Prescription.from_shapefile(data, name, prop)
        else:
            p = Prescription.from_geojson(data, name, prop)
        self._set_presc(p)
        return p

    def demo_map(self, low, high):
        if self.field is None:
            raise ValueError("eerst een perceel")
        t = self.target()
        self._set_presc(Prescription.demo(self.field, float(low), float(high), unit=t["unit"] if t else ""))

    def clear_map(self):
        self.presc = None
        try:
            os.remove(os.path.join(self.dir, "taakkaart.geojson"))
        except OSError:
            pass
        for c in self.clients.values():
            self.sync_modes(c)
        self._map_dirty = True
        self.ev.tc_changed()

    def _set_presc(self, p):
        self.presc = p
        try:
            with open(os.path.join(self.dir, "taakkaart.geojson"), "w", encoding="utf-8") as fh:
                fh.write(p.save())  # the rate is always in the property "rate" there
        except OSError as e:
            log.warning("taakkaart niet bewaard: %s", e)
        for c in self.clients.values():
            self.sync_modes(c)
        self._map_dirty = True
        self.ev.tc_changed()

    # ------------------------------------------------------------ state for the web page
    def totals(self, c):
        out = []
        if c.pool:
            for (el, ddi), o in c.pool.dpds.items():
                if o["triggers"] & D.TRIG_TOTAL and ddi != D.DDI_DEFAULT_PD and (el, ddi) in c.values:
                    v = c.values[(el, ddi)]
                    out.append({"element": el, "ddi": ddi, "designator": o["designator"], "value": v,
                                "task": v - c.totals0.get((el, ddi), v) if self.task_t0 else None})
        return out

    def state(self):
        c = self.implement()
        return {
            "clients": [x.summary() for x in self.clients.values()],
            "task": {"state": self.task, "start": self.task_t0, "time": round(self.task_time, 1), "rows": len(self.rows)},
            "opts": {k: v for k, v in self.opts.items() if k != "field"},
            "implement": c.addr if c else None,
            "sections": [{"element": s["element"], "x": s["x"], "y": s["y"], "w": s["w"]} for s in self.sc.secs],
            "states": self.states, "decided": self.sc.decided, "scAuto": self.sc_auto(c) if c else None,
            "candidates": self.setpoint_candidates(c), "target": self.target(c) if c else None,
            "rate": self.rate, "map": {"name": self.presc.name, "unit": self.presc.unit, "zones": len(self.presc.zones),
                                       "min": min(self.presc.rates()), "max": max(self.presc.rates())} if self.presc else None,
            "area": round(self.coverage.area(), 1) if self.coverage else 0.0,
            "overlapArea": round(self.coverage.overlap_area(), 1) if self.coverage else 0.0,
            "fieldArea": round(self.field.area, 1) if self.field else None,
            "autopilot": {"on": self.autopilot is not None, "finished": bool(self.autopilot and self.autopilot.finished),
                          "spacing": self.lane_spacing(), "possible": self.sim is not None},
            "position": None if self.pose is None else {"source": self.pose["source"], "lat": self.pose["lat"], "lon": self.pose["lon"],
                                                         "speed": round(self.pose["speed"] * 3.6, 2), "heading": round(self.pose["heading"], 1)},
            "totals": {x.addr: self.totals(x) for x in self.clients.values() if x.pool},
        }

    def map_full(self, consume=True):
        """The whole map. `consume`: as the next update for every page (else a copy for one page, nothing changes)."""
        cov = self.coverage
        if consume:
            self._map_dirty = False
            self.track_sent = len(self.track)
            if cov:
                cov.dirty.clear()
        return {"field": self.field.to_json() if self.field else None, "coverage": cov.header() if cov else None,
                "blocks": cov.all_blocks() if cov else [], "track": self.track[:], "presc": self.presc.to_json(self.field.frame)
                if self.presc and self.field else None, "pose": self._pose_json(), "full": True}

    def _pose_json(self):
        if self.pose is None:
            return None
        p = self.pose
        ends = self.sc.ends(p["e"], p["n"], p["heading"]) if self.sc.secs else []
        return {"e": round(p["e"], 2), "n": round(p["n"], 2), "heading": round(p["heading"], 1), "hitch": list(self.sc.hitch),
                "sections": [[round(a[0], 2), round(a[1], 2), round(b[0], 2), round(b[1], 2), bool(self.states[i]) if i < len(self.states) else False]
                             for i, (a, b) in enumerate(ends)]}

    def map_update(self):
        """Changes since the last update, or None; the whole map after a change of field, map or reset."""
        if self._map_dirty:
            return self.map_full()
        track = self.track[self.track_sent:]
        self.track_sent = len(self.track)
        blocks = self.coverage.take_dirty() if self.coverage else []
        return {"track": track, "blocks": blocks, "pose": self._pose_json(), "full": False}

    def values_update(self):
        out = []
        for c in self.clients.values():
            if c.dirty:
                out.append({"addr": c.addr, "values": [[el, ddi, c.values.get((el, ddi)), c.sent.get((el, ddi))] for el, ddi in c.dirty]})
                c.dirty = set()
        return out

    def values_all(self, c):
        keys = set(c.values) | set(c.sent)
        return {"addr": c.addr, "values": [[el, ddi, c.values.get((el, ddi)), c.sent.get((el, ddi))] for el, ddi in keys], "reset": True}

    # ------------------------------------------------------------ downloads
    def log_csv(self):
        keys = []
        seen = set()
        for r in self.rows:
            for k in r["values"]:
                if k not in seen:
                    seen.add(k)
                    keys.append(k)
        names = {}
        for c in self.clients.values():
            if c.pool:
                for (el, ddi), o in c.pool.dpds.items():
                    names[f"{c.addr:02X}:{el}:{ddi}"] = f"{o['designator']} ({c.addr:02X} el {el} DDI {ddi})"
        head = ["tijd", "breedtegraad", "lengtegraad", "snelheid km/h", "koers", "bewerkt m2", "dosis taakkaart", "secties"]
        lines = [";".join(head + [names.get(k, k) for k in keys])]
        for r in self.rows:
            t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["t"]))
            vals = [t, f"{r['lat']:.8f}", f"{r['lon']:.8f}", f"{r['speed']}", f"{r['heading']}", f"{r['area']}",
                    "" if r["rate"] is None else f"{r['rate']}", r["sections"]]
            lines.append(";".join(vals + ["" if r["values"].get(k) is None else str(r["values"][k]) for k in keys]))
        return "\n".join(lines) + "\n"

    def coverage_geojson(self):
        if not self.coverage or not self.field:
            return json.dumps({"type": "FeatureCollection", "features": []})
        fr = self.field.frame
        polys = []
        for e0, n0, e1, n1 in self.coverage.runs():
            ring = [fr.to_geo(e0, n0), fr.to_geo(e1, n0), fr.to_geo(e1, n1), fr.to_geo(e0, n1), fr.to_geo(e0, n0)]
            polys.append([[[round(lon, 8), round(lat, 8)] for lat, lon in ring]])
        feats = [{"type": "Feature", "properties": {"name": "bewerkt", "m2": round(self.coverage.area(), 1)},
                  "geometry": {"type": "MultiPolygon", "coordinates": polys}}]
        feats.append({"type": "Feature", "properties": {"name": self.field.name},
                      "geometry": {"type": "Polygon", "coordinates": [[[round(lon, 8), round(lat, 8)] for lat, lon in self.field.geo_ring() + self.field.geo_ring()[:1]]]}})
        return json.dumps({"type": "FeatureCollection", "features": feats})

    def ddop_bytes(self, addr):
        c = self.clients.get(addr)
        p = c and (c.pool or c.stored)
        return p.data if p else None
