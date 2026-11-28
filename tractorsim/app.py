"""Ties the CAN bus, the control functions (tractor ECU, VT, GNSS), the simulation and the web page together."""
import asyncio
import base64
import collections
import copy
import json
import logging
import os
import time

from . import j1939
from .canbus import open_bus
from .j1939 import ControlFunction, Network, name_decode, name_encode, name_text, split_id
from .tc.prescription import MapError
from .tc.server import BusPosition, TCServer
from .tecu import MESSAGES, MSG_BY_KEY, PARAMS, Sim, Tractor
from .vt.objects import PoolError
from .vt.server import LOCAL, VTServer

log = logging.getLogger(__name__)

CF_DEFAULTS = {
    "tecu": {"enabled": True, "address": 0xF0, "identity": 101, "manufacturer": 0, "functionInstance": 0,
             "selfConfigurable": False, "function": 134, "label": "Tractor-ECU"},
    "vt": {"enabled": True, "address": 0x26, "identity": 102, "manufacturer": 0, "functionInstance": 0,
           "selfConfigurable": True, "function": 29, "label": "Virtual Terminal",
           "version": 4, "dmSize": 480, "skWidth": 80, "skHeight": 80, "skCount": 6, "virtualKeys": 64,
           "strict": False, "memory": 16 * 1024 * 1024},
    "gnss": {"enabled": True, "address": 0x1C, "identity": 103, "manufacturer": 0, "functionInstance": 0,
             "selfConfigurable": True, "function": 23, "label": "GNSS-ontvanger"},
    "tc": {"enabled": True, "address": 0xF7, "identity": 104, "manufacturer": 0, "functionInstance": 0,
           "selfConfigurable": True, "function": 130, "label": "Task controller",
           "version": 4, "booms": 1, "sections": 16, "channels": 1, "memory": 1024 * 1024},
}

PRESETS = {
    "stilstand": {"engine.rpm": 850, "engine.torque": 8, "engine.demandTorque": 8, "engine.throttle": 0, "engine.fuelRate": 3,
                  "drive.speed": 0, "rearPto.engaged": 0, "frontPto.engaged": 0, "rearHitch.position": 100, "frontHitch.position": 100},
    "transport": {"engine.rpm": 1700, "engine.torque": 35, "engine.demandTorque": 38, "engine.throttle": 55, "engine.fuelRate": 16,
                  "drive.speed": 30, "drive.direction": 1, "rearPto.engaged": 0, "frontPto.engaged": 0,
                  "rearHitch.position": 100, "frontHitch.position": 100, "lights.lowBeam": 1, "lights.beacon": 1},
    "veldwerk": {"engine.rpm": 1950, "engine.torque": 72, "engine.demandTorque": 75, "engine.throttle": 85, "engine.fuelRate": 28,
                 "drive.speed": 8, "drive.direction": 1, "rearPto.engaged": 1, "rearPto.speed": 540, "rearPto.mode1000": 0,
                 "rearHitch.position": 20, "rearHitch.draft": 25000, "lights.beacon": 0},
    "kopakker": {"engine.rpm": 1500, "engine.torque": 30, "engine.demandTorque": 30, "drive.speed": 5,
                 "rearPto.engaged": 0, "rearHitch.position": 90, "gnss.turnRate": 0},
}


def cf_name(c):
    return name_encode({"identity": c["identity"], "manufacturer": c["manufacturer"], "functionInstance": c["functionInstance"],
                        "function": c["function"], "deviceClass": 0, "industryGroup": 2, "selfConfigurable": 1 if c["selfConfigurable"] else 0})


class App:
    def __init__(self, bus_spec, data_dir, web_dir, host, port, bitrate=250000, web=None, listen=False, bus=None):
        """`web` replaces the HTTP/WebSocket server (the browser demo passes its own link to the page).
        `listen`: analyzer only (--listen): no control functions and nothing is ever sent on the bus.
        `bus`: a ready bus instead of opening `bus_spec` (the online version on the page's CAN adapter)."""
        self.bus_spec = bus_spec
        self.data_dir = data_dir
        self.cfg_path = os.path.join(data_dir, "config.json")
        os.makedirs(data_dir, exist_ok=True)
        self.cfg = self._load()
        self.bitrate = bitrate
        self.listen = listen
        self.bus = bus if bus is not None else open_bus(bus_spec, bitrate, listen)
        self.bus.listen_only = listen
        self.net = Network(self.bus)
        self.sim = Sim(self.cfg.get("params"))
        self.cfs = {}
        self.tractor = None
        self.vt = None
        self.tc = None
        self.bus_pos = BusPosition(self.net)  # the position of another GNSS receiver, when ours is off
        if web is None:
            from .web import WebServer
            web = WebServer(self, host, port, web_dir)
        self.web = web
        self.frames = collections.deque(maxlen=3000)
        self.frames_pending = []
        self.raw_pending = []  # every frame for the "raw" subscription (page Variabelen), 11-bit included
        self.raw_lost = 0
        self.claims = {}  # source address → last Address Claimed frame, so a late analyzer still knows the NAMEs
        self.vtlog_rows = collections.deque(maxlen=400)
        self.vtlog_pending = []
        self.tclog_rows = collections.deque(maxlen=400)
        self.tclog_pending = []
        self.dirty = {"params": set(), "vt": False, "rx": False, "tc": False}
        self.dirty_objs = {}
        self._save_at = None
        self.on_saved = None  # callable(config text) after every save (the online version keeps it in the browser)
        self.bus.listeners.append(lambda i, d, t: self._frame("rx", i, d, t))
        self.bus.tx_listeners.append(lambda i, d, t: self._frame("tx", i, d, t))
        self.bus.raw_listeners.append(self._raw)

    # ------------------------------------------------------------ config
    def _load(self):
        cfg = {"params": {}, "messages": {}, "cf": copy.deepcopy(CF_DEFAULTS), "tcOpts": {}}
        try:
            with open(self.cfg_path) as fh:
                saved = json.load(fh)
            cfg["params"].update(saved.get("params", {}))
            cfg["messages"].update(saved.get("messages", {}))
            if isinstance(saved.get("tcOpts"), dict):
                cfg["tcOpts"].update(saved["tcOpts"])
            for k, v in saved.get("cf", {}).items():
                if k in cfg["cf"]:
                    cfg["cf"][k].update(v)
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as e:
            log.warning("config.json niet leesbaar (%s): standaardinstellingen", e)
        return cfg

    def save_soon(self):
        self._save_at = time.monotonic() + 2.0

    def _save(self):
        self.cfg["params"] = dict(self.sim.p)
        tmp = self.cfg_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.cfg, fh, indent=1)
        os.replace(tmp, self.cfg_path)
        if self.on_saved:
            try:
                self.on_saved(json.dumps(self.cfg))
            except Exception:
                log.exception("instellingen bewaren in de browser")

    # ------------------------------------------------------------ start / restart
    async def start(self):
        await self.bus.start()
        self.net.start()
        self._build()
        await self.web.start()
        asyncio.ensure_future(self._flusher())

    def _make_cf(self, c):
        return ControlFunction(self.net, c["label"], cf_name(c), int(c["address"]), bool(c["selfConfigurable"]))

    def _build(self, section=None):
        """(Re)creates the control functions: everything at start-up, else only the changed part.
        A restarted VT stays silent for 3.5 s, so implements see a VT that went away and connect again."""
        if section in (None, "tecu", "gnss"):
            if self.tractor:
                self.tractor.stop()
            for key in ("tecu", "gnss"):
                old = self.cfs.pop(key, None)
                if old:
                    old.stop()
                if self.cfg["cf"][key]["enabled"] and not self.listen:
                    self.cfs[key] = self._make_cf(self.cfg["cf"][key])
            self.tractor = Tractor(self.net, {k: v for k, v in self.cfs.items() if k in ("tecu", "gnss")},
                                   self.sim, self.cfg["messages"], self)
            for key in ("tecu", "gnss"):
                if key in self.cfs:
                    self.cfs[key].start()
            self.tractor.start()
        if section in (None, "vt"):
            restart = self.vt is not None
            if self.vt:
                self.vt.stop()
            old = self.cfs.pop("vt", None)
            if old:
                old.stop()
            self.vt = None
            c = self.cfg["cf"]["vt"]
            if c["enabled"] and not self.listen:
                self.cfs["vt"] = self._make_cf(c)
                self.vt = VTServer(self.net, self.cfs["vt"], c, self.data_dir, self, self.sim.language)
                asyncio.get_event_loop().call_later(3.5 if restart else 0, self._start_vt, self.vt)
        if section in (None, "tc"):
            restart = self.tc is not None
            if self.tc:
                self.tc.stop()
            old = self.cfs.pop("tc", None)
            if old:
                old.stop()
            self.tc = None
            c = self.cfg["cf"]["tc"]
            if c["enabled"] and not self.listen:
                self.cfs["tc"] = self._make_cf(c)
                self.tc = TCServer(self.net, self.cfs["tc"], c, self.data_dir, self, self.sim.language, self.position,
                                   self.cfg["tcOpts"], self.sim)
                asyncio.get_event_loop().call_later(3.5 if restart else 0, self._start_tc, self.tc)
            self.dirty["tc"] = True
        self.dirty["vt"] = True

    def _start_vt(self, vt):
        if self.vt is vt:
            vt.cf.start()
            vt.start()
            self.dirty["vt"] = True

    def _start_tc(self, tc):
        if self.tc is tc:
            tc.cf.start()
            tc.start()
            self.dirty["tc"] = True

    def position(self):
        """Position for the task controller: our own GNSS receiver (the simulation), else one on the bus."""
        if "gnss" in self.cfs:
            live = self.sim.live
            return {"lat": live["lat"], "lon": live["lon"], "heading": live["heading"], "speed": self.sim.ground_speed,
                    "source": "eigen GNSS"}
        return self.bus_pos.get()

    # ------------------------------------------------------------ events (from Tractor / VTServer)
    def params_changed(self, keys):
        self.dirty["params"] |= set(keys)

    def rx_changed(self):
        self.dirty["rx"] = True

    def vt_changed(self, throttle=False):
        self.dirty["vt"] = True

    def objects_changed(self, ws, ids):
        self.dirty_objs.setdefault(ws.addr, set()).update(ids)

    def pool_installed(self, ws):
        self.dirty_objs.pop(ws.addr, None)
        self.web.broadcast(self._pool_msg(ws))

    def audio(self, d):
        self.web.broadcast({"t": "vt.audio", **d})

    def vtlog(self, entry):
        self.vtlog_rows.append(entry)
        self.vtlog_pending.append(entry)

    def edit(self, ws, oid):
        self.web.broadcast({"t": "vt.edit", "addr": ws.addr, "id": oid})

    def identify(self):
        c = self.cfg["cf"]["vt"]
        self.web.broadcast({"t": "vt.identify", "instance": c["functionInstance"]})

    # ------------------------------------------------------------ events (from TCServer)
    def tc_changed(self):
        self.dirty["tc"] = True

    def tc_pool(self, client):
        self.web.broadcast(self._tc_pool_msg(client))

    def tc_saved(self):
        self.save_soon()

    def tc_identify(self, addr):
        c = self.cfg["cf"]["tc"]
        self.web.broadcast({"t": "toast", "text": f"Werktuig 0x{addr:02X} vraagt het TC-nummer: {c['functionInstance'] + 1}"})

    def tclog(self, entry):
        self.tclog_rows.append(entry)
        self.tclog_pending.append(entry)

    def _tc_pool_msg(self, client):
        p = client.pool or client.stored
        return {"t": "tc.pool", "addr": client.addr, "objects": p.to_json() if p else []}

    def tc_state(self):
        cf = self.cfs.get("tc")
        if not self.tc:
            return {"t": "tc", "enabled": False}
        return {"t": "tc", "enabled": True, "address": cf.address if cf else None, "state": cf.state if cf else "uit",
                "cfg": {k: self.cfg["cf"]["tc"][k] for k in ("version", "booms", "sections", "channels", "functionInstance")},
                **self.tc.state()}

    def _raw(self, direction, can_id, data, t, ext):
        row = (round(t, 6), direction, can_id, data.hex(), 1 if ext else 0)
        if len(self.raw_pending) < 20000:
            self.raw_pending.append(row)
        else:
            self.raw_lost += 1
        if ext and (can_id >> 8) & 0x3FF00 == j1939.PGN_ADDRESS_CLAIMED:
            self.claims[can_id & 0xFF] = row

    def _frame(self, direction, can_id, data, t):
        row = (round(t, 4), direction, can_id, data.hex())
        self.frames.append(row)
        if self.frames_pending is not None and len(self.frames_pending) < 2000:
            self.frames_pending.append(row)

    # ------------------------------------------------------------ state for the web page
    def _pool_msg(self, ws):
        return {"t": "pool", "addr": ws.addr, "objects": ws.objects}

    def vt_state(self):
        c = self.cfg["cf"]["vt"]
        cf = self.cfs.get("vt")
        if not self.vt:
            return {"t": "vt", "enabled": False}
        return {"t": "vt", "enabled": True, "active": self.vt.active, "ws": [w.summary() for w in self.vt.ws.values()],
                "cfg": {k: c[k] for k in ("dmSize", "skWidth", "skHeight", "skCount", "version", "functionInstance")},
                "address": cf.address if cf else None, "state": cf.state if cf else "uit",
                "edit": list(self.vt.edit) if self.vt.edit else None}

    def cf_state(self):
        out = []
        for key, c in self.cfg["cf"].items():
            cf = self.cfs.get(key)
            out.append({"key": key, "label": c["label"], "enabled": c["enabled"], "address": cf.address if cf else None,
                        "state": cf.state if cf else "uit", "name": f"{cf_name(c):016X}", "nameText": name_text(cf_name(c))})
        return out

    def bus_state(self):
        self.bus.update_rates(self.bitrate)
        now = time.time()
        nodes = []
        own = {cf.address for cf in self.cfs.values()}
        for addr, n in sorted(self.net.nodes.items()):
            name = n.get("name") or self.net.addresses.get(addr)
            nodes.append({"addr": addr, "name": f"{name:016X}" if name else None, "text": name_text(name) if name else "",
                          "fn": name_decode(name)["function"] if name else None, "frames": n["frames"],
                          "age": round(now - n["last"], 1), "own": addr in own})
        for cf in self.cfs.values():
            if cf.address not in self.net.nodes and cf.address < 0xFE:
                nodes.append({"addr": cf.address, "name": f"{cf.name:016X}", "text": name_text(cf.name), "fn": name_decode(cf.name)["function"],
                              "frames": 0, "age": 0, "own": True})
        return {"t": "bus", "stats": self.bus.stats, "busName": self.bus.name, "nodes": nodes, "cfs": self.cf_state(),
                "listen": self.listen}

    def on_connect(self, client):
        client.send({"t": "hello", "params": PARAMS, "values": self.sim.p, "live": self.sim.live,
                     "presets": list(PRESETS), "cfCfg": self.cfg["cf"], "busSpec": self.bus_spec})
        client.send({"t": "messages", "rows": self.tractor.status()})
        client.send({"t": "rx", "rows": list(self.tractor.rx.values())})
        client.send(self.vt_state())
        if self.vt:
            for ws in self.vt.ws.values():
                if ws.state == "actief":
                    client.send(self._pool_msg(ws))
            client.send({"t": "vt.log", "rows": list(self.vtlog_rows)})
        client.send(self.tc_state())
        if self.tc:
            for c in self.tc.clients.values():
                if c.pool or c.stored:
                    client.send(self._tc_pool_msg(c))
                client.send({"t": "tc.values", "rows": [self.tc.values_all(c)]})
            client.send({"t": "tc.map", **self.tc.map_full(consume=False)})
            client.send({"t": "tc.log", "rows": list(self.tclog_rows)})
        client.send(self.bus_state())

    async def _flusher(self):
        n = 0
        while True:
            await asyncio.sleep(0.1)
            n += 1
            try:
                self._flush(n)
            except Exception:
                log.exception("flush")

    def _flush(self, n):
        w = self.web
        if self.dirty["params"]:
            keys = self.dirty["params"]
            self.dirty["params"] = set()
            w.broadcast({"t": "values", "values": {k: self.sim.p[k] for k in keys}})
            self.save_soon()
        if n % 3 == 0:
            w.broadcast({"t": "live", "live": self.sim.live, "hours": self.sim.p["engine.hours"]})
        if self.dirty["vt"]:
            self.dirty["vt"] = False
            w.broadcast(self.vt_state())
        if self.dirty_objs and self.vt:
            for addr, ids in self.dirty_objs.items():
                ws = self.vt.ws.get(addr)
                if ws:
                    w.broadcast({"t": "objs", "addr": addr, "objects": [ws.objects[i] for i in ids if i in ws.objects]})
            self.dirty_objs = {}
        if self.vtlog_pending:
            w.broadcast({"t": "vt.log", "rows": self.vtlog_pending[-200:], "append": True})
            self.vtlog_pending = []
        if self.dirty["rx"] and n % 3 == 0:
            self.dirty["rx"] = False
            w.broadcast({"t": "rx", "rows": list(self.tractor.rx.values())})
        if (self.dirty["tc"] and n % 2 == 0) or n % 10 == 5:
            self.dirty["tc"] = False
            w.broadcast(self.tc_state())
        if self.tc:
            if n % 3 == 2:
                rows = self.tc.values_update()
                if rows:
                    w.broadcast({"t": "tc.values", "rows": rows})
            if n % 5 == 3:
                w.broadcast({"t": "tc.map", **self.tc.map_update()})
        if self.tclog_pending:
            w.broadcast({"t": "tc.log", "rows": self.tclog_pending[-200:], "append": True})
            self.tclog_pending = []
        if self.raw_pending or self.raw_lost:
            w.broadcast({"t": "raw", "rows": self.raw_pending, "lost": self.raw_lost}, sub="raw")
            self.raw_pending = []
            self.raw_lost = 0
        if n % 3 == 1 and self.frames_pending:
            w.broadcast({"t": "frames", "rows": self.frames_pending[-600:]}, sub="frames")
            self.frames_pending = []
        if n % 10 == 0:
            w.broadcast({"t": "messages", "rows": self.tractor.status()})
            w.broadcast(self.bus_state())
        if self._save_at and time.monotonic() > self._save_at:
            self._save_at = None
            try:
                self._save()
            except OSError as e:
                log.warning("opslaan mislukt: %s", e)

    # ------------------------------------------------------------ messages from the web page
    def on_client(self, client, m):
        t = m.get("t")
        vt = self.vt
        if t == "set":
            if self.sim.set(m["key"], m["value"]):
                self.sim.targets.pop(m["key"], None)  # the operator wins from an implement command
                self.params_changed([m["key"]])
                if m["key"].startswith("sys.") and m["key"] not in ("sys.stop", "sys.noise"):
                    self.tractor.send("lang")
        elif t == "preset":
            for k, v in PRESETS.get(m.get("name"), {}).items():
                self.sim.set(k, v)
            self.params_changed(self.sim.p.keys())
        elif t == "resetDist":
            self.sim.live["wheelDist"] = self.sim.live["groundDist"] = 0.0
        elif t == "resetPos":
            self.sim.live.update(lat=self.sim.p["gnss.lat"], lon=self.sim.p["gnss.lon"], heading=self.sim.p["gnss.heading"])
        elif t == "msg":
            c = self.cfg["messages"].setdefault(m["key"], {})
            if "enabled" in m:
                c["enabled"] = bool(m["enabled"])
            if "interval" in m:
                c["interval"] = max(0, int(m["interval"]))
            if m.get("now"):
                self.tractor.send(m["key"])
            self.save_soon()
        elif t == "cf":
            c = self.cfg["cf"][m["key"]]
            for k, v in m["values"].items():
                if k in c and k != "label":
                    c[k] = type(c[k])(v) if not isinstance(c[k], bool) else bool(v)
            self._save()
            self._build(m["key"])
            self.web.broadcast({"t": "hello", "params": PARAMS, "values": self.sim.p, "live": self.sim.live,
                                "presets": list(PRESETS), "cfCfg": self.cfg["cf"], "busSpec": self.bus_spec})
        elif t == "sub":
            (client.subs.add if m.get("on") else client.subs.discard)(m["name"])
            if m["name"] == "frames" and m.get("on"):
                client.send({"t": "frames", "rows": list(self.frames)[-500:], "reset": True})
            if m["name"] == "raw" and m.get("on"):  # the NAMEs first, then the recent history
                rows = sorted(self.claims.values()) + [(t, d, i, h, 1) for t, d, i, h in self.frames]
                client.send({"t": "raw", "rows": rows, "reset": True})
        elif t in ("send", "request") and self.listen:
            client.send({"t": "toast", "error": True, "text": "Luistermodus (--listen): de simulator zendt niets"})
        elif t == "send":
            can_id = int(str(m["id"]), 16)
            data = bytes.fromhex(str(m["data"]).replace(" ", ""))[:8]
            self.bus.send(can_id, data)
        elif t == "request":
            cf = self.cfs.get(m.get("cf", "tecu")) or next(iter(self.cfs.values()), None)
            if cf:
                self.net.request(cf, int(str(m["pgn"]), 16), int(m.get("da", 255)))
        elif t.startswith("tc."):
            self._tc_client(client, t, m)
        elif vt is None:
            return
        elif t == "vt.key":
            vt.key(m["addr"], m["id"], m["action"])
        elif t == "vt.select":
            vt.select(m["addr"], m["id"], m.get("open", True))
        elif t == "vt.esc":
            vt.esc(m["addr"], m["id"])
        elif t == "vt.number":
            vt.enter_number(m["addr"], m["id"], m["value"])
        elif t == "vt.string":
            vt.enter_string(m["addr"], m["id"], m["value"])
        elif t == "vt.active":
            vt.set_active(m["addr"])
        elif t == "vt.pointing":
            vt.pointing(m["addr"], m["x"], m["y"], m.get("state", 1))
        elif t == "vt.open":
            try:
                vt.open_local(base64.b64decode(m["data"]), m.get("name", "bestand"))
            except (PoolError, ValueError, StopIteration) as e:
                client.send({"t": "toast", "error": True, "text": f"Pool niet geladen: {e}"})
        elif t == "vt.close":
            if m["addr"] == LOCAL:
                vt.remove(LOCAL)

    def _tc_client(self, client, t, m):
        tc = self.tc
        if tc is None:
            client.send({"t": "toast", "error": True, "text": "De task controller staat uit (Instellingen)"})
            return
        try:
            if t == "tc.task":
                tc.set_task(m["action"])
            elif t == "tc.opts":
                tc.set_options(m["values"])
            elif t == "tc.value":
                tc.set_value(int(m["addr"]), int(m["element"]), int(m["ddi"]), int(m["value"]))
            elif t == "tc.field":
                if m.get("geojson"):
                    tc.field_geojson(m["geojson"])
                else:
                    tc.field_here(float(m.get("length", 200)), float(m.get("width", 96)))
            elif t == "tc.autopilot":
                tc.set_autopilot(bool(m.get("on")))
            elif t == "tc.map":
                kind = "shp" if str(m.get("name", "")).lower().endswith(".zip") else "geojson"
                data = base64.b64decode(m["data"])
                p = tc.load_map(kind, data, str(m.get("name", "Taakkaart")).rsplit(".", 1)[0], m.get("prop") or None)
                client.send({"t": "toast", "text": f"Taakkaart {p.name}: {len(p.zones)} zones, dosis uit '{p.prop}'"})
            elif t == "tc.demoMap":
                tc.demo_map(float(m.get("low", 150)), float(m.get("high", 250)))
            elif t == "tc.clearMap":
                tc.clear_map()
        except (MapError, ValueError, KeyError, TypeError) as e:
            client.send({"t": "toast", "error": True, "text": f"Task controller: {e}"})

    # ------------------------------------------------------------ HTTP API
    def http_api(self, method, path, query, body):
        if path.startswith("tc/") and self.tc:
            if path == "tc/log.csv":
                return 200, "text/csv; charset=utf-8", self.tc.log_csv().encode(), {"Content-Disposition": 'attachment; filename="taak.csv"'}
            if path == "tc/dekking.geojson":
                return 200, "application/geo+json", self.tc.coverage_geojson().encode(), {"Content-Disposition": 'attachment; filename="dekking.geojson"'}
            if path.startswith("tc/ddop/"):
                addr = int(path[8:].split(".")[0])
                data = self.tc.ddop_bytes(addr)
                if data is None:
                    return None
                return 200, "application/octet-stream", data, {"Content-Disposition": f'attachment; filename="ddop_{addr:02X}.bin"'}
            return None
        if path.startswith("pool/") and self.vt:
            addr = int(path[5:].split(".")[0])
            data = self.vt.pool_bytes(addr)
            if data is None:
                return None
            ws = self.vt.ws[addr]
            fname = f"pool_{addr:02X}_{ws.name:016X}.iop" if addr != LOCAL else "pool.iop"
            return 200, "application/octet-stream", data, {"Content-Disposition": f'attachment; filename="{fname}"'}
        if path == "config":
            self.cfg["params"] = dict(self.sim.p)
            return 200, "application/json", json.dumps(self.cfg, indent=1).encode(), {"Content-Disposition": 'attachment; filename="tractorsim-config.json"'}
        if path == "frames.csv":
            rows = ["tijd;richting;id;pgn;bron;doel;data"]
            for t, d, i, h in self.frames:
                _, pgn, sa, da = split_id(i)
                rows.append(f"{t:.4f};{d};{i:08X};{pgn:05X};{sa:02X};{da:02X};{h}")
            return 200, "text/csv; charset=utf-8", "\n".join(rows).encode(), {"Content-Disposition": 'attachment; filename="canlog.csv"'}
        return None


__all__ = ["App", "PRESETS", "MESSAGES", "MSG_BY_KEY", "j1939"]
