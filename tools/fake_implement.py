#!/usr/bin/env python3
"""Test implement: a working set that connects to the VT of the simulator, uploads the demo pool
(over ETP), shows the tractor data on its masks and reacts to soft keys, buttons and inputs.

    python3 tools/fake_implement.py                    # to a simulator started with --bus virtual on this PC
    python3 tools/fake_implement.py --bus vcan0        # SocketCAN (on the Pi, or a second CAN interface)
    python3 tools/fake_implement.py --pool mijn.iop    # upload your own pool instead (no live values then)

Soft keys / buttons "Hef op/neer" and "Aftakas" send Hitch and PTO Commands (PGN 65090); turn on
"Hefcommando's volgen" on the Tractor page to see the simulated tractor follow them.
"""
import argparse
import asyncio
import logging
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tractorsim.canbus import open_bus  # noqa: E402
from tractorsim.j1939 import GLOBAL, ControlFunction, Network, le, name_decode, name_encode  # noqa: E402
import demo_pool as D  # noqa: E402

log = logging.getLogger("werktuig")
PGN_VT_TO_ECU, PGN_ECU_TO_VT, PGN_WS_MASTER = 0xE600, 0xE700, 0xFE0D


class Implement:
    def __init__(self, net, cf, pool_file=None, store=True):
        self.net, self.cf = net, cf
        self.pool_file = pool_file
        self.store = store
        self.vt = None
        self.vt_status = None
        self.replies = asyncio.Queue()
        self.dm, self.sk = 480, 80
        self.active_mask = D.MASK_MAIN
        self.values = {}
        self.sent = {}
        self.tractor = {}
        self.spraying = False
        self.pto_on = False
        self.hitch_cmd = None  # (target %, until)
        self.alarm_ack_until = 0
        self.lang = "?"
        self.note = ("", 0.0)  # message on the status line until a time
        self.status_t = 0.0
        cf.handlers.append(self.on_msg)
        net.listeners.append(self.on_bus)

    # ----- CAN
    def to_vt(self, data):
        data = bytes(data)
        return self.cf.send(PGN_ECU_TO_VT, data if len(data) > 8 else data.ljust(8, b"\xff"), da=self.vt, prio=5)

    async def ask(self, data, func, timeout=3.0):
        while not self.replies.empty():
            self.replies.get_nowait()
        r = self.to_vt(data)
        if asyncio.isfuture(r):
            ok = await r
            if not ok:
                raise RuntimeError("transport mislukt")
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                d = await asyncio.wait_for(self.replies.get(), end - time.monotonic())
            except asyncio.TimeoutError:
                break
            if d[0] == func:
                return d
        raise TimeoutError(f"geen antwoord op functie 0x{func:02X}")

    def on_msg(self, msg):
        if msg.pgn != PGN_VT_TO_ECU or msg.sa != self.vt:
            return
        d = msg.data
        if d[0] == 0xFE:
            return
        if d[0] in (0x00, 0x01):
            self.on_key(d[0], d[1], d[2] | d[3] << 8, d[6])
        elif d[0] == 0x05:
            self.on_value(d[1] | d[2] << 8, struct.unpack_from("<I", d, 4)[0])
        elif d[0] == 0x08:
            oid = d[1] | d[2] << 8
            text = d[4:4 + d[3]].decode("latin-1")
            log.info("VT: tekst van object %d = %r", oid, text)
            if oid == D.IN_FIELD:
                self.say(f"Perceel: {text}")
        elif d[0] in (0x03, 0x04, 0x02):
            log.info("VT-event %s", d.hex(" "))
        else:
            self.replies.put_nowait(d)

    def on_bus(self, msg):
        d = msg.data
        if msg.pgn == PGN_VT_TO_ECU and d and d[0] == 0xFE and msg.da == GLOBAL:
            if self.vt is None:
                name = self.net.addresses.get(msg.sa)
                if name is None or name_decode(name)["function"] == 29:
                    self.vt = msg.sa
                    log.info("VT gevonden op adres 0x%02X", msg.sa)
            if msg.sa == self.vt:
                self.status_t = time.monotonic()
                self.vt_status = (d[1], d[2] | d[3] << 8)
                if d[1] == self.cf.address:
                    self.active_mask = d[2] | d[3] << 8
            return
        t = self.tractor
        if msg.pgn == 0xF004:
            v = d[3] | d[4] << 8
            t["rpm"] = v * 0.125 if v < 0xFB00 else None
        elif msg.pgn == 0xFE49:
            v = d[0] | d[1] << 8
            t["speed"] = v * 0.0036 if v < 0xFB00 else None
            t["dir"] = d[7] & 3
        elif msg.pgn == 0xFE43:
            v = d[0] | d[1] << 8
            t["pto"] = v * 0.125 if v < 0xFB00 else None
            t["ptoOn"] = d[4] >> 6 & 3
        elif msg.pgn == 0xFE45:
            t["hitch"] = d[0] * 0.4 if d[0] <= 250 else None
            t["inWork"] = d[1] >> 6 & 3
        elif msg.pgn == 0xFD02:
            t["stop"] = d[7] & 3
        elif msg.pgn == 0xFE0F:
            self.lang = d[:2].decode("ascii", "replace")
        elif msg.pgn == 0x1F801:
            t["lat"], t["lon"] = (v * 1e-7 for v in struct.unpack_from("<ii", d, 0))
        elif msg.pgn == 0xFEE6:
            t["time"] = f"{d[2]:02d}:{d[1]:02d}:{d[0] // 4:02d} UTC"

    # ----- connect & upload
    async def run(self):
        """Connects, and connects again when the VT disappears (no VT Status for 3 s)."""
        while True:
            maint = None
            try:
                while self.vt is None or not self.cf.active:
                    await asyncio.sleep(0.2)
                self.status_t = time.monotonic()
                maint = asyncio.ensure_future(self.maintenance())
                await self.session()
            except (TimeoutError, RuntimeError) as e:
                log.warning("%s", e)
            finally:
                if maint:
                    maint.cancel()
            log.info("opnieuw verbinden met een VT")
            self.vt, self.sent, self.active_mask = None, {}, D.MASK_MAIN
            await asyncio.sleep(1.0)

    def vt_lost(self):
        return time.monotonic() - self.status_t > 3.0

    async def session(self):
        self.net.request(self.cf, 0xFE0F)  # Language Command (answered by the TECU and the VT)
        self.cf.send(PGN_WS_MASTER, bytes([1]))
        self.to_vt([0xFF, 0x01, 4])
        mem = (os.path.getsize(self.pool_file) if self.pool_file else 20000)
        d = await self.ask([0xC0, 0xFF] + list(le(mem, 4)), 0xC0)
        log.info("Get Memory: VT-versie %d, geheugen %s", d[1], "OK" if d[2] == 0 else "te klein")
        d = await self.ask([0xC2], 0xC2)
        self.sk = d[4]
        log.info("softkeys %d×%d, %d fysiek, %d virtueel", d[4], d[5], d[7], d[6])
        await self.ask([0xC3], 0xC3)
        d = await self.ask([0xC7], 0xC7)
        self.dm = d[4] | d[5] << 8
        log.info("datamasker %d×%d px", self.dm, d[6] | d[7] << 8)
        self.label = f"DEMO{self.dm % 1000:03d}".encode()  # the pool depends on the mask size
        loaded = False
        if self.store and not self.pool_file:
            d = await self.ask([0xDF], 0xE0)
            labels = [bytes(d[2 + 7 * i:9 + 7 * i]) for i in range(d[1])]
            log.info("opgeslagen versies: %s", [x.decode("latin-1") for x in labels])
            if self.label in labels:
                d = await self.ask(bytes([0xD1]) + self.label, 0xD1)
                loaded = d[5] == 0
                log.info("Load Version: %s", "OK" if loaded else f"fout 0x{d[5]:02X}")
        if not loaded:
            pool = open(self.pool_file, "rb").read() if self.pool_file else D.pool_bytes(self.dm, self.sk)
            log.info("pool uploaden: %d bytes (%s)", len(pool), "ETP" if len(pool) + 1 > 1785 else "TP")
            t0 = time.monotonic()
            ok = await self.net.transport.send_long(self.cf.address, PGN_ECU_TO_VT, b"\x11" + pool, self.vt,
                                                    progress=lambda got, size: None)
            if not ok:
                raise RuntimeError("pooloverdracht mislukt")
            log.info("pool verzonden in %.1f s", time.monotonic() - t0)
            d = await self.ask([0x12], 0x12, timeout=10)
            if d[1]:
                raise RuntimeError(f"VT weigert de pool: fout 0x{d[1]:02X}, object {d[5] | d[6] << 8}, poolfout 0x{d[7]:02X}")
            log.info("pool aanvaard")
            if self.store and not self.pool_file:
                d = await self.ask(bytes([0xD0]) + self.label, 0xD0)
                log.info("Store Version: %s", "OK" if d[5] == 0 else f"fout 0x{d[5]:02X}")
        if self.pool_file:
            log.info("eigen pool: geen live waarden")
            while not self.vt_lost():
                await asyncio.sleep(0.5)
        else:
            await self.live()
        log.warning("VT 0x%02X geeft geen VT Status meer", self.vt)

    async def maintenance(self):
        while True:
            await asyncio.sleep(1.0)
            self.to_vt([0xFF, 0x00, 4])
            self.cf.send(PGN_WS_MASTER, bytes([1]))

    # ----- live values
    def num(self, oid, value):
        value = int(value) & 0xFFFFFFFF
        if self.sent.get(oid) != value:
            self.sent[oid] = value
            self.to_vt(bytes([0xA8]) + le(oid, 2) + b"\xff" + le(value, 4))

    def text(self, oid, s, length=48):
        s = s[:length]
        if self.sent.get(oid) != s:
            self.sent[oid] = s
            enc = s.encode("latin-1", "replace")
            self.to_vt(bytes([0xB3]) + le(oid, 2) + le(len(enc), 2) + enc)

    def show(self, oid, on):
        if self.sent.get(("show", oid)) != on:
            self.sent[("show", oid)] = on
            self.to_vt(bytes([0xA0]) + le(oid, 2) + bytes([1 if on else 0]))

    def mask(self, mid):
        self.to_vt(bytes([0xAD]) + le(D.WS, 2) + le(mid, 2))
        self.active_mask = mid

    async def live(self):
        while not self.vt_lost():
            await asyncio.sleep(0.2)
            t = self.tractor
            self.num(D.VAR_RPM, t.get("rpm") or 0)
            self.num(D.VAR_SPEED, round((t.get("speed") or 0) * 100))
            self.num(D.VAR_PTO, t.get("pto") or 0)
            self.num(D.VAR_HITCH, t.get("hitch") or 0)
            stop = t.get("stop") == 0
            self.show(D.STOP_BOX, stop)
            fill = 12 if stop else 2 if self.spraying else 7
            if self.sent.get("led") != fill:
                self.sent["led"] = fill
                self.to_vt(bytes([0xAC]) + le(D.LED_FILL, 2) + bytes([2, fill, 0xFF, 0xFF]))
            if time.monotonic() < self.note[1]:
                status = self.note[0]
            elif not t:
                status = "Wacht op tractor..."
            elif stop:
                status = "Stopschakelaar: spuiten gestopt"
            else:
                status = f"{'Spuiten' if self.spraying else 'Niet spuiten'}, hef {'in' if t.get('inWork') == 1 else 'uit'} werkstand"
            self.text(D.STATUS, status)
            if self.spraying and stop:
                self.spraying = False
            if (t.get("speed") or 0) > 15 and self.spraying and self.active_mask == D.MASK_MAIN and time.monotonic() > self.alarm_ack_until:
                self.mask(D.MASK_ALARM)
            if self.active_mask == D.MASK_INFO:
                lat, lon = t.get("lat"), t.get("lon")
                lines = [f"Positie {lat:.6f}, {lon:.6f}" if lat is not None else "Positie: geen GNSS",
                         f"Tijd {t.get('time', '-')}", f"Taal tractor/VT: {self.lang}",
                         f"Motor {t.get('rpm') or 0:.0f} rpm, aftakas {t.get('pto') or 0:.0f} rpm",
                         f"Rijrichting {['achteruit', 'vooruit', 'fout', 'n.b.'][t.get('dir', 3)]}",
                         f"VT-adres 0x{self.vt:02X}, eigen adres 0x{self.cf.address:02X}"]
                for oid, s in zip(D.INFO_LINES, lines):
                    self.text(oid, s, 40)
            # Hitch and PTO Commands (PGN 65090) while a command is active
            if self.hitch_cmd and time.monotonic() < self.hitch_cmd[1]:
                self.cf.send(0xFE42, bytes([0xFF, int(self.hitch_cmd[0] / 0.4), 0xFF, 0xFF]) + le(int(540 / 0.125), 2)
                             + bytes([0xF3 | ((1 if self.pto_on else 0) << 2), 0xFF]), prio=3)

    def on_key(self, fc, code, oid, keycode):
        what = "softkey" if fc == 0 else "knop"
        if code == 1:
            log.info("%s %d ingedrukt (toetscode %d)", what, oid, keycode)
        if code != 1:
            return
        now = time.monotonic()
        if keycode == D.KEY_UP:
            self.hitch_cmd = (100, now + 4)
        elif keycode == D.KEY_DOWN:
            self.hitch_cmd = (20, now + 4)
        elif keycode == D.KEY_PTO:
            self.pto_on = not self.pto_on
            self.hitch_cmd = (self.tractor.get("hitch") or 0, now + 3)
        elif keycode == D.KEY_ALARM:
            self.mask(D.MASK_ALARM)
        elif keycode == D.KEY_SPRAY:
            self.spraying = not self.spraying
        elif keycode == D.KEY_RESET:
            self.say("Teller op nul gezet")
        elif keycode == D.KEY_OK:
            self.alarm_ack_until = now + 10
            self.mask(D.MASK_MAIN)

    def on_value(self, oid, value):
        log.info("VT: nieuwe waarde voor object %d = %d", oid, value)
        if oid == D.IN_MODE:
            self.say(["Modus: automatisch", "Modus: manueel", "Modus: uit"][min(value, 2)])
        elif oid == D.IN_RATE:
            self.say(f"Dosering {value} l/ha")
        elif oid == D.IN_SECTIONS:
            self.say(f"Secties {'aan' if value else 'uit'}")

    def say(self, text):
        self.note = (text, time.monotonic() + 3)
        self.text(D.STATUS, text)


async def connect(bus, address=0x80, identity=4242, pool=None, store=True):
    """Starts the implement on `bus` (an opened Bus) and returns it; `await imp.run()` keeps it going."""
    await bus.start()
    net = Network(bus)
    net.start()
    name = name_encode({"identity": identity, "manufacturer": 0, "function": 132, "deviceClass": 6, "industryGroup": 2,
                        "selfConfigurable": 1})
    cf = ControlFunction(net, "Demo-werktuig", name, address)
    imp = Implement(net, cf, pool, store=store)
    cf.start()
    return imp


async def main(a):
    imp = await connect(open_bus(a.bus), a.address, a.identity, a.pool, store=not a.no_store)
    await imp.run()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bus", default="client:127.0.0.1:29536", help="client:HOST:POORT (virtuele bus) of can0/vcan0")
    ap.add_argument("--address", type=lambda s: int(s, 0), default=0x80)
    ap.add_argument("--identity", type=int, default=4242)
    ap.add_argument("--pool", help="eigen .iop-bestand uploaden")
    ap.add_argument("--no-store", action="store_true", help="geen Store/Load Version (altijd uploaden)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    try:
        asyncio.run(main(a))
    except KeyboardInterrupt:
        pass
