"""TC client of the test implement (tools/fake_implement.py): a sprayer with a boom of 8 sections that connects to a
task controller (ISO 11783-10), sends its DDOP and follows the setpoints of the TC.

    ddop = sprayer_ddop(name)            # the device description (bytes)
    tc = TcClient(net, cf, ddop)         # on a ControlFunction that already claimed its address
    await tc.run()                       # connects, and again after the TC went away

Process data the TC sets: work state (DDI 289), the section states (290, condensed), section control on/off (160),
prescription control on/off (158) and the rate (1, L/ha). `step(dt, speed_kmh)` turns them into the actual values the
TC reads back: work state, section states, rate with a little noise, working width, area, volume and tank content.
"""
import asyncio
import logging
import random
import struct
import time

from tractorsim.j1939 import GLOBAL, name_decode
from tractorsim.tc import ddop as DD

log = logging.getLogger("werktuig.tc")
PGN_PD = 0xCB00
PGN_WS_MASTER = 0xFE0D

# element numbers of the sprayer
EL_DEVICE, EL_CONNECTOR, EL_BOOM, EL_TANK, EL_SECTION1 = 0, 1, 2, 3, 4
TANK_ML = 3_000_000


def sprayer_ddop(name, sections=8, width=3000, label="DEMOSP1", lang="nl", designator="Demospuit"):
    """DDOP bytes: device, connector (3-point), boom 1.5 m behind it with `sections` sections of `width` mm, a tank with
    the rate (L/ha) and the totals. Same layout as the pools of PLC-VT-Designer (src/tc/build.js)."""
    objs = []
    pres = {}

    def add(o):
        o["id"] = len(objs)
        objs.append(o)
        return o

    def dvp(unit, scale, decimals):
        key = (unit, scale, decimals)
        if key not in pres:
            pres[key] = {"table": "DVP", "offset": 0, "scale": scale, "decimals": decimals, "unit": unit}
        return pres[key]

    def det(kind, designator, number, parent):
        return add({"table": "DET", "type": kind, "designator": designator, "number": number, "parent": parent, "children": []})

    def dpd(el, designator, ddi, props, trig, p=None):
        el["children"].append(add({"table": "DPD", "ddi": ddi, "props": props, "triggers": trig, "designator": designator, "_p": p})["id"])

    def dpt(el, designator, ddi, value, p=None):
        el["children"].append(add({"table": "DPT", "ddi": ddi, "value": int(value), "designator": designator, "_p": p})["id"])

    S, DEF, T = DD.PROP_SETTABLE, DD.PROP_DEFAULT, DD
    dev = add({"table": "DVC", "designator": designator, "softwareVersion": "raspISO 0.1", "name": name, "serial": "1",
               "structureLabel": label.encode("latin-1")[:7].ljust(7, b" "),
               "localization": lang.encode("ascii")[:2] + bytes([0x40, 0x00, 0x00, 0x00, 0xFF])})
    main = det(1, designator, EL_DEVICE, dev["id"])
    dpd(main, "Werkstand", DD.DDI_ACTUAL_WORK_STATE, DEF, T.TRIG_CHANGE)
    dpd(main, "Standaard procesdata", DD.DDI_DEFAULT_PD, 0, T.TRIG_TOTAL)
    dpd(main, "Werktijd", 119, DEF | S, T.TRIG_TOTAL, dvp("h", 1 / 3600, 1))
    conn = det(6, "Koppeling", EL_CONNECTOR, main["id"])
    dpt(conn, "Koppeling X", DD.DDI_OFFSET_X, 0, dvp("mm", 1.0, 0))
    dpt(conn, "Koppeling Y", DD.DDI_OFFSET_Y, 0, dvp("mm", 1.0, 0))
    dpt(conn, "Type koppeling", 157, 3)
    boom = det(2, "Boom", EL_BOOM, main["id"])
    dpt(boom, "Boom X", DD.DDI_OFFSET_X, -1500, dvp("mm", 1.0, 0))
    dpt(boom, "Boom Y", DD.DDI_OFFSET_Y, 0, dvp("mm", 1.0, 0))
    dpt(boom, "Boom Z", DD.DDI_OFFSET_Z, 0, dvp("mm", 1.0, 0))
    dpd(boom, "Werkbreedte", DD.DDI_WORKING_WIDTH, DEF, T.TRIG_CHANGE, dvp("m", 0.001, 2))
    dpd(boom, "Werkstand instellen", DD.DDI_SETPOINT_WORK_STATE, DEF | S, T.TRIG_CHANGE)
    dpd(boom, "Oppervlakte", DD.DDI_TOTAL_AREA, DEF, T.TRIG_TOTAL, dvp("ha", 0.0001, 2))
    dpd(boom, "Sectiebesturing", DD.DDI_SECTION_CONTROL_STATE, DEF | S, T.TRIG_CHANGE | T.TRIG_TIME)
    tank = det(3, "Tank", EL_TANK, boom["id"])
    rate = dvp("L/ha", 0.0001, 0)
    dpd(tank, "Dosis instellen", 1, DEF | S, T.TRIG_CHANGE, rate)
    dpd(tank, "Dosis", 2, DEF, T.TRIG_CHANGE | T.TRIG_TIME, rate)
    dpd(tank, "Taakkaart", DD.DDI_PRESCRIPTION_STATE, DEF | S, T.TRIG_CHANGE | T.TRIG_TIME)
    dpd(tank, "Totaal", 80, DEF, T.TRIG_TOTAL, dvp("L", 1.0, 0))
    dpd(tank, "Inhoud", 72, DEF, T.TRIG_CHANGE | T.TRIG_TIME, dvp("L", 0.001, 0))
    dpt(tank, "Max. inhoud", 73, TANK_ML, dvp("L", 0.001, 0))
    for s in range(sections):
        sec = det(4, f"Sectie {s + 1}", EL_SECTION1 + s, boom["id"])
        dpt(sec, "X", DD.DDI_OFFSET_X, -1500, dvp("mm", 1.0, 0))
        dpt(sec, "Y", DD.DDI_OFFSET_Y, round(-(sections * width) / 2 + s * width + width / 2), dvp("mm", 1.0, 0))
        dpt(sec, "Breedte", DD.DDI_WORKING_WIDTH, width, dvp("mm", 1.0, 0))
    for g in range((sections + 15) // 16):
        first, last = g * 16 + 1, min(sections, g * 16 + 16)
        dpd(boom, f"Secties {first}-{last}", DD.DDI_ACTUAL_CONDENSED + g, DEF, T.TRIG_CHANGE | T.TRIG_TIME)
        dpd(boom, f"Secties {first}-{last} instellen", DD.DDI_SETPOINT_CONDENSED + g, DEF | S, T.TRIG_CHANGE)
    for p in pres.values():
        add(p)
    for o in objs:
        if "_p" in o:
            p = o.pop("_p")
            o["presentation"] = p["id"] if p else DD.NONE
    return DD.encode(objs)


class TcClient:
    def __init__(self, net, cf, ddop, sections=8, width=3000):
        self.net, self.cf = net, cf
        self.ddop = ddop
        self.pool = DD.Pool(DD.parse(ddop), ddop)
        self.n, self.width = sections, width
        self.tc = None
        self.tc_seen = 0.0
        self.tc_status = 0
        self.tc_version = None
        self.state = "zoeken"
        self.replies = asyncio.Queue()
        self.values = {key: 0 for key in self.pool.dpds}
        self.triggers = {}  # (el, ddi) → {"time": ms, "change": threshold, "t": last send, "v": last value sent}
        # operator side of the sprayer
        self.sc_auto = False
        self.presc_auto = False
        self.work = False  # work state asked by the TC
        self.sections = [False] * sections
        self.setpoint = 0  # raw rate (0.0001 L/ha)
        self.area = 0.0
        self.volume = 0.0
        self.content = float(TANK_ML)
        self.work_time = 0.0
        self.step(0.0, 0.0, False)
        cf.handlers.append(self._msg)
        net.listeners.append(self._bus)

    # ----- CAN
    def _bus(self, m):
        if m.pgn == PGN_PD and m.da == GLOBAL and m.data and m.data[0] == 0xFE:
            name = self.net.addresses.get(m.sa)
            if self.tc is None and (name is None or name_decode(name)["function"] == 130):
                self.tc = m.sa
                log.info("TC gevonden op adres 0x%02X", m.sa)
            if m.sa == self.tc:
                self.tc_seen = time.monotonic()
                self.tc_status = m.data[4]

    def send(self, data):
        data = bytes(data)
        return self.cf.send(PGN_PD, data if len(data) > 8 else data.ljust(8, b"\xff"), da=self.tc, prio=5)

    def pd(self, cmd, el, ddi, value):
        self.send(bytes([(cmd & 0x0F) | ((el & 0x0F) << 4), el >> 4]) + struct.pack("<HI", ddi, int(value) & 0xFFFFFFFF))

    def _msg(self, m):
        if m.pgn != PGN_PD or m.sa != self.tc or not m.data:
            return
        d = m.data
        cmd = d[0] & 0x0F
        if cmd == 0 and d[0] == 0x00:  # the TC asks our version
            self.send(bytes([0x10, 4, 0xFF, 0x00, 0x00, 1, self.n, 1]))
            return
        if cmd in (0, 1):
            self.replies.put_nowait(d)
            return
        if len(d) < 8:
            return
        el = (d[0] >> 4) | (d[1] << 4)
        ddi, v = struct.unpack_from("<Hi", d, 2)
        key = (el, ddi)
        if cmd == 2:
            if key in self.values:
                self.pd(3, el, ddi, self.values[key])
        elif cmd in (3, 10):
            ok = key in self.pool.dpds and self.pool.dpds[key]["props"] & DD.PROP_SETTABLE
            if ok:
                self.apply(el, ddi, v)
            if cmd == 10:
                self.send(bytes([(0x0D) | ((el & 0x0F) << 4), el >> 4]) + struct.pack("<H", ddi) + bytes([0 if ok else 0x04, 0xFA]))
        elif cmd == 4:
            self.triggers.setdefault(key, {"time": 0, "change": None, "t": 0.0, "v": None})["time"] = max(0, v)
        elif cmd == 8:
            self.triggers.setdefault(key, {"time": 0, "change": None, "t": 0.0, "v": None})["change"] = v

    def apply(self, el, ddi, v):
        self.values[(el, ddi)] = v
        if ddi == DD.DDI_SETPOINT_WORK_STATE:
            self.work = (v & 3) == 1
        elif ddi == DD.DDI_SECTION_CONTROL_STATE:
            self.sc_auto = (v & 3) == 1
        elif ddi == DD.DDI_PRESCRIPTION_STATE:
            self.presc_auto = (v & 3) == 1
        elif DD.DDI_SETPOINT_CONDENSED <= ddi < DD.DDI_SETPOINT_CONDENSED + 16:
            for k, s in DD.uncondensed(v, ddi - DD.DDI_SETPOINT_CONDENSED, self.n).items():
                self.sections[k] = bool(s)
        elif ddi == 1:
            self.setpoint = v

    async def ask(self, data, first, timeout=3.0):
        while not self.replies.empty():
            self.replies.get_nowait()
        r = self.send(data)
        if asyncio.isfuture(r) and not await r:
            raise RuntimeError("transport naar de TC mislukt")
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                d = await asyncio.wait_for(self.replies.get(), end - time.monotonic())
            except asyncio.TimeoutError:
                break
            if d[0] == first:
                return d
        raise TimeoutError(f"geen antwoord 0x{first:02X} van de TC")

    async def connect(self):
        """Version, labels, DDOP transfer (only when the TC does not have this one) and activation."""
        self.state = "aanmelden"
        self.cf.send(PGN_WS_MASTER, bytes([1]))
        d = await self.ask([0x00], 0x10)
        self.tc_version = d[1]
        label = (await self.ask([0x01], 0x11))[1:8]
        same = bytes(label) == self.pool.label
        if same:
            same = bytes((await self.ask([0x21], 0x31))[1:8]) == self.pool.localization
        if not same:
            if bytes(label) != b"\xff" * 7:
                await self.ask([0xA1], 0xB1)
            self.state = "beschrijving sturen"
            d = await self.ask(bytes([0x41]) + struct.pack("<I", len(self.ddop)), 0x51)
            if d[1] != 0:
                raise RuntimeError("de TC heeft te weinig geheugen")
            d = await self.ask(b"\x61" + self.ddop, 0x71, timeout=10.0)
            if d[1] != 0:
                raise RuntimeError(f"de TC weigert de beschrijving (0x{d[1]:02X})")
        d = await self.ask([0x81, 0xFF], 0x91)
        if d[1] != 0:
            raise RuntimeError(f"activeren geweigerd (0x{d[1]:02X}, object {d[4] | d[5] << 8})")
        self.state = "verbonden"
        log.info("verbonden met de TC 0x%02X (versie %d)", self.tc, self.tc_version)

    async def run(self):
        while True:
            task = None
            try:
                while self.tc is None or not self.cf.active or time.monotonic() - self.tc_seen > 6.0:
                    await asyncio.sleep(0.2)
                task = asyncio.ensure_future(self._client_task())
                await self.connect()
                while time.monotonic() - self.tc_seen < 6.0:
                    await asyncio.sleep(0.1)
                    self.report()
                log.warning("TC 0x%02X zendt geen status meer", self.tc)
            except (TimeoutError, RuntimeError) as e:
                log.warning("TC: %s", e)
            finally:
                if task:
                    task.cancel()
            self.state, self.tc, self.triggers = "zoeken", None, {}
            await asyncio.sleep(1.0)

    async def _client_task(self):
        while True:
            self.send(bytes([0xFF, 0xFF, 0xFF, 0xFF, 0x00, 0xFF, 0xFF, 0xFF]))
            self.cf.send(PGN_WS_MASTER, bytes([1]))
            await asyncio.sleep(2.0)

    def report(self):
        """Values the TC asked for with a measurement command: every interval and on a change."""
        now = time.monotonic()
        for key, t in self.triggers.items():
            v = self.values.get(key)
            if v is None:
                continue
            due = t["time"] and now - t["t"] >= t["time"] / 1000.0
            changed = t["change"] is not None and (t["v"] is None or abs(v - t["v"]) >= max(1, t["change"]))
            if due or changed:
                t["t"], t["v"] = now, v
                self.pd(3, key[0], key[1], v)

    # ----- the sprayer itself
    def step(self, dt, speed_kmh, spraying):
        """Updates the actual values. `spraying`: the main switch of the operator (VT) or the TC work state."""
        on = [spraying and (s if self.sc_auto else True) for s in self.sections]
        width_mm = sum(self.width for s in on if s)
        v = (speed_kmh or 0) / 3.6
        rate = 0
        if spraying and width_mm and v > 0.1:
            rate = int(self.setpoint * (1 + random.uniform(-0.02, 0.02)))
            dt_area = v * dt * width_mm / 1000.0  # m²
            self.area += dt_area
            litres = rate * 0.0001 * dt_area / 10000.0
            self.volume += litres
            self.content = max(0.0, self.content - litres * 1000)
            self.work_time += dt
        cond = DD.condensed(on, 0)
        V = self.values
        V[(EL_DEVICE, DD.DDI_ACTUAL_WORK_STATE)] = 1 if spraying else 0
        V[(EL_BOOM, DD.DDI_WORKING_WIDTH)] = width_mm
        V[(EL_BOOM, DD.DDI_ACTUAL_CONDENSED)] = cond
        V[(EL_BOOM, DD.DDI_SECTION_CONTROL_STATE)] = 1 if self.sc_auto else 0
        V[(EL_TANK, DD.DDI_PRESCRIPTION_STATE)] = 1 if self.presc_auto else 0
        V[(EL_TANK, 2)] = rate
        V[(EL_TANK, 80)] = int(self.volume)
        V[(EL_TANK, 72)] = int(self.content)
        V[(EL_BOOM, DD.DDI_TOTAL_AREA)] = int(self.area)
        V[(EL_DEVICE, 119)] = int(self.work_time)
        return on
