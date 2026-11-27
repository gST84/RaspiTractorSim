"""Tests without hardware:  python3 -m unittest discover -s tests -v"""
import asyncio
import base64
import os
import struct
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from tractorsim.canbus import Bus  # noqa: E402
from tractorsim.j1939 import (GLOBAL, ControlFunction, Network, make_id, name_decode, name_encode,  # noqa: E402
                              split_id)
from tractorsim.tecu import Sim, Tractor  # noqa: E402
from tractorsim.vt.objects import AIDS, encode_pool, parse_pool, validate  # noqa: E402
from tractorsim.vt.server import VTServer  # noqa: E402
import demo_pool  # noqa: E402

# the 119-byte pool of PLC-VT-Designer/isobus_project_bundle/vt_test_project (pool_listing.txt)
TEST_POOL = bytes.fromhex("""
00 00 00 01 01 E8 03 01 00 01 F9 2A 00 00 00 00 65 6E
E8 03 01 01 FF FF 01 00 F8 2A 14 00 28 00
F8 2A 0B 90 01 28 00 01 D8 59 00 FF FF 01 20 00 48 61 6C 6C 6F 20 76 61 6E 75 69 74 20 53 37 2D 31 35 30 30 20 20 20 20 20 20 20 20 20 20 20 20 00
F9 2A 0B 3C 00 20 00 01 D9 59 00 FF FF 01 05 00 53 37 20 56 54 00
D8 59 17 00 03 00 00 00
D9 59 17 00 02 00 00 00""".replace("\n", " "))


class LoopBus(Bus):
    """In-process bus: frames go to every other LoopBus of the same group. `drop` loses chosen frames."""

    def __init__(self, group):
        super().__init__()
        self.group = group
        group.append(self)
        self.drop = None

    def _write(self, can_id, data):
        if self.drop and self.drop(can_id, data):
            return
        for b in self.group:
            if b is not self:
                asyncio.get_event_loop().call_soon(b._deliver, can_id, bytes(data))


def run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        tasks = asyncio.all_tasks(loop)
        for t in tasks:
            t.cancel()
        loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        loop.close()


class TestObjects(unittest.TestCase):
    def test_designer_pool_roundtrip(self):
        objs = parse_pool(TEST_POOL)
        self.assertEqual(len(objs), 6)
        self.assertEqual(objs[2]["value"], "Hallo vanuit S7-1500")
        self.assertEqual(encode_pool(objs), TEST_POOL)
        self.assertEqual(validate(objs), ([], []))

    def test_demo_pool(self):
        data = demo_pool.pool_bytes()
        objs = parse_pool(data)
        self.assertEqual(validate(objs), ([], []))
        self.assertEqual(encode_pool(objs), data)
        self.assertGreater(len(data), 1785)  # uploads over ETP

    def test_truncated_pool_names_object(self):
        with self.assertRaises(Exception) as cm:
            parse_pool(TEST_POOL[:60])
        self.assertEqual(cm.exception.obj_id, 11000)

    def test_aids(self):
        self.assertEqual(AIDS[12][0], "width")
        self.assertNotIn("value", AIDS[12])
        self.assertEqual(AIDS[20][:3], ["width", "options", "transparencyColour"])


class TestJ1939(unittest.TestCase):
    def test_ids(self):
        cid = make_id(6, 0xE700, 0x80, 0x26)
        self.assertEqual(cid, 0x18E72680)
        self.assertEqual(split_id(cid), (6, 0xE700, 0x80, 0x26))
        self.assertEqual(split_id(0x0CFE48F0), (3, 0xFE48, 0xF0, GLOBAL))

    def test_name(self):
        d = {"identity": 4242, "manufacturer": 98, "function": 29, "functionInstance": 1, "industryGroup": 2, "selfConfigurable": 1}
        back = name_decode(name_encode(d))
        for k, v in d.items():
            self.assertEqual(back[k], v)

    def _pair(self):
        group = []
        a, b = LoopBus(group), LoopBus(group)
        na, nb = Network(a), Network(b)
        ca = ControlFunction(na, "A", name_encode({"identity": 1}), 0x80)
        cb = ControlFunction(nb, "B", name_encode({"identity": 2}), 0x26)
        return a, b, na, nb, ca, cb

    def test_transport(self):
        async def go():
            a, b, na, nb, ca, cb = self._pair()
            na.start(), nb.start(), ca.start(), cb.start()
            got = []
            cb.handlers.append(lambda m: got.append(m))
            await asyncio.sleep(0.3)
            # lose packet 3 of the first ETP window once: the receiver must ask for it again
            lost = []
            a.drop = lambda i, d: split_id(i)[1] == 0xC700 and d[0] == 3 and not lost and not lost.append(1)
            for size in (20, 1000, 1785, 5000, 40000):
                data = bytes(i & 0xFF for i in range(size))
                ok = await na.transport.send_long(ca.address, 0xE700, data, cb.address)
                self.assertTrue(ok, size)
                await asyncio.sleep(0.01)
                self.assertEqual(got[-1].data, data)
            self.assertTrue(lost)
            await na.transport.send_long(ca.address, 0xFEDA, b"BAM test 123", GLOBAL)
            await asyncio.sleep(0.05)
            self.assertEqual(got[-1].data, b"BAM test 123")
        run(go())

    def test_address_conflict(self):
        async def go():
            a, b, na, nb, ca, cb = self._pair()
            cb.preferred = 0x80  # same address, higher NAME: B must move
            na.start(), nb.start(), ca.start(), cb.start()
            await asyncio.sleep(0.4)
            self.assertEqual(ca.address, 0x80)
            self.assertNotEqual(cb.address, 0x80)
            self.assertEqual(cb.state, "actief")
        run(go())


class FakeEvents:
    def __getattr__(self, name):
        return lambda *a, **k: None


class TestVT(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        group = []
        self.bus, self.ibus = LoopBus(group), LoopBus(group)
        self.net, self.inet = Network(self.bus), Network(self.ibus)
        self.cf = ControlFunction(self.net, "VT", name_encode({"identity": 5, "function": 29}), 0x26)
        self.icf = ControlFunction(self.inet, "WS", name_encode({"identity": 9}), 0x80)
        cfg = {"version": 4, "dmSize": 480, "skWidth": 80, "skHeight": 80, "skCount": 6}
        self.vt = VTServer(self.net, self.cf, cfg, self.tmp, FakeEvents(), lambda: b"nl" + b"\xff" * 6)
        self.replies = []
        self.icf.handlers.append(lambda m: m.pgn == 0xE600 and m.data[0] != 0xFE and self.replies.append(m.data))

    async def cmd(self, data):
        self.replies.clear()
        r = self.icf.send(0xE700, bytes(data).ljust(8, b"\xff"), da=0x26)
        if asyncio.isfuture(r):
            await r
        await asyncio.sleep(0.02)
        return self.replies[-1] if self.replies else None

    def test_session(self):
        async def go():
            self.net.start(), self.inet.start(), self.cf.start(), self.icf.start()
            await asyncio.sleep(0.3)
            r = await self.cmd([0xC0, 0xFF, 0x00, 0x10, 0, 0])
            self.assertEqual(r[:3], bytes([0xC0, 4, 0]))
            r = await self.cmd([0xC7])
            self.assertEqual(struct.unpack_from("<HH", r, 4), (480, 480))
            pool = demo_pool.pool_bytes()
            await self.cmd(b"\x11" + pool)
            r = await self.cmd([0x12])
            self.assertEqual(r[1], 0, r.hex())
            ws = self.vt.ws[0x80]
            self.assertEqual(ws.state, "actief")
            # Change Numeric Value on a number variable
            r = await self.cmd([0xA8]) if False else await self.cmd(bytes([0xA8]) + struct.pack("<H", demo_pool.VAR_RPM) + b"\xff" + struct.pack("<I", 1234))
            self.assertEqual(r[3], 0)
            self.assertEqual(ws.objects[demo_pool.VAR_RPM]["value"], 1234)
            # unknown object → error bit
            r = await self.cmd(bytes([0xA8, 0x39, 0x30, 0xFF, 1, 0, 0, 0]))
            self.assertNotEqual(r[3], 0)
            # Change String Value (via TP: more than 8 bytes)
            text = b"Nieuwe status"
            r = await self.cmd(bytes([0xB3]) + struct.pack("<HH", demo_pool.STATUS, len(text)) + text)
            self.assertEqual(r[5], 0)
            self.assertEqual(ws.objects[demo_pool.STATUS]["value"], "Nieuwe status")
            # Hide/Show and Change Active Mask
            r = await self.cmd(bytes([0xA0]) + struct.pack("<H", demo_pool.STOP_BOX) + b"\x01")
            self.assertEqual(ws.objects[demo_pool.STOP_BOX]["hidden"], 0)
            r = await self.cmd(bytes([0xAD]) + struct.pack("<HH", demo_pool.WS, demo_pool.MASK_ALARM))
            self.assertEqual(r[3], 0)
            self.assertEqual(ws.active_mask, demo_pool.MASK_ALARM)
            # Change Attribute (Output Number scale = float) and Get Attribute Value
            r = await self.cmd(bytes([0xAF]) + struct.pack("<HB", 12002, AIDS[12].index("scale") + 1) + struct.pack("<f", 0.5))
            self.assertEqual(r[4], 0)
            self.assertEqual(ws.objects[12002]["scale"], 0.5)
            r = await self.cmd(bytes([0xB9]) + struct.pack("<HB", 12002, AIDS[12].index("width") + 1))
            self.assertEqual(struct.unpack_from("<I", r, 4)[0], ws.objects[12002]["width"])
            # macro 1 switches to the info mask
            r = await self.cmd([0xBE, 1])
            self.assertEqual(ws.active_mask, demo_pool.MASK_INFO)
            # versions
            r = await self.cmd(bytes([0xD0]) + b"TEST001")
            self.assertEqual(r[5], 0)
            r = await self.cmd([0xDF])
            self.assertEqual(r[1], 1)
            self.assertEqual(bytes(r[2:9]), b"TEST001")
            r = await self.cmd(bytes([0xD1]) + b"NOTHERE")
            self.assertNotEqual(r[5], 0)
            # soft key from the operator
            self.vt.key(0x80, 5010, "press")
            await asyncio.sleep(0.02)
            self.assertEqual(self.replies[-1][:4], bytes([0x00, 1]) + struct.pack("<H", 5010))
            self.vt.key(0x80, 5010, "release")
            await asyncio.sleep(0.02)
            self.assertEqual(self.replies[-1][1], 0)
            # a bad pool is refused with the faulty object
            await self.cmd(b"\x11" + TEST_POOL[:60])
            r = await self.cmd([0x12])
            self.assertEqual(r[1], 1)
            self.assertEqual(struct.unpack_from("<H", r, 4)[0], 11000)
            self.vt.stop()
        run(go())


class TestTractor(unittest.TestCase):
    def test_encoders(self):
        s = Sim()
        s.set("drive.speed", 10)
        s.set("engine.rpm", 2000)
        self.assertEqual(struct.unpack_from("<H", s.eec1(), 3)[0] * 0.125, 2000)
        self.assertAlmostEqual(struct.unpack_from("<H", s.ground_based(), 0)[0] * 0.0036, 10, places=2)
        for name in ("eec1", "etc2", "wheel_based", "ground_based", "machine_selected", "lighting", "stop_switch", "language",
                     "time_date", "et1", "eflp1", "lfe1", "vep1", "dd1", "amb", "hours", "ccvs1", "vehicle_position",
                     "vehicle_dir_speed", "n2k_position", "n2k_cog_sog", "n2k_attitude"):
            self.assertEqual(len(getattr(s, name)()), 8, name)
        self.assertEqual(len(s.hitch("rearHitch")), 8)
        self.assertEqual(len(s.pto("rearPto")), 8)
        self.assertEqual(len(s.n2k_gnss()), 43)

    def test_dynamics(self):
        s = Sim()
        s.set("rearPto.engaged", 1)
        s.set("drive.speed", 36)
        for _ in range(40):
            s.step(0.05)
        self.assertAlmostEqual(s.live["groundDist"], 20, places=3)
        self.assertGreater(s.live["rearPto"], 0)


class TestEndToEnd(unittest.TestCase):
    """Simulator (VT + TECU) and the demo implement on an in-process bus: connect, upload, live values."""

    def test_demo_implement(self):
        import fake_implement

        async def go():
            group = []
            sbus, ibus = LoopBus(group), LoopBus(group)
            snet, inet = Network(sbus), Network(ibus)
            snet.start(), inet.start()
            vtcf = ControlFunction(snet, "VT", name_encode({"identity": 5, "function": 29}), 0x26)
            tecu = ControlFunction(snet, "TECU", name_encode({"identity": 6, "function": 134}), 0xF0, False)
            sim = Sim()
            tractor = Tractor(snet, {"tecu": tecu}, sim, {}, FakeEvents())
            cfg = {"version": 4, "dmSize": 400, "skWidth": 64, "skHeight": 64, "skCount": 6}
            vt = VTServer(snet, vtcf, cfg, tempfile.mkdtemp(), FakeEvents(), sim.language)
            wcf = ControlFunction(inet, "WS", name_encode({"identity": 9}), 0x80)
            imp = fake_implement.Implement(inet, wcf)
            for x in (vtcf, tecu, wcf):
                x.start()
            vt.start()
            tractor.start()
            task = asyncio.ensure_future(imp.run())
            for _ in range(60):
                await asyncio.sleep(0.1)
                ws = vt.ws.get(0x80)
                if ws and ws.state == "actief" and ws.objects[demo_pool.VAR_RPM]["value"] == 1800:
                    break
            ws = vt.ws[0x80]
            self.assertEqual(ws.state, "actief")
            self.assertEqual(imp.dm, 400)  # the implement scaled its pool to the reported mask size
            self.assertEqual(ws.objects[demo_pool.VAR_RPM]["value"], 1800)
            self.assertEqual(imp.lang, "nl")
            task.cancel()
            vt.stop()
            tractor.stop()
        run(go())


class FakeWeb:
    def __init__(self):
        self.sent = []

    async def start(self):
        pass

    def broadcast(self, obj, sub=None):
        self.sent.append((obj, sub))


class FakeClient:
    def __init__(self):
        self.subs = set()
        self.got = []

    def send(self, obj):
        self.got.append(obj)


class TestAnalyzer(unittest.TestCase):
    """--listen and the lossless "raw" subscription of the page Variabelen."""

    def _app(self, listen, group):
        from tractorsim.app import App
        return App(f"memory:{group}", tempfile.mkdtemp(), None, None, None, web=FakeWeb(), listen=listen)

    def test_listen_sends_nothing(self):
        from tractorsim.canbus import MemoryBus

        async def go():
            app = self._app(True, "listen")
            peer = MemoryBus("listen")
            await app.start()
            peer.send(make_id(6, 0xEE00, 0x80), name_encode({"identity": 9, "function": 130}).to_bytes(8, "little"))
            await asyncio.sleep(1.2)
            app.on_client(FakeClient(), {"t": "send", "id": "18EAFFF0", "data": "00EE00"})
            await asyncio.sleep(0.1)
            self.assertEqual(app.cfs, {})
            self.assertEqual(app.bus.stats["tx"], 0)
            self.assertTrue(app.bus_state()["listen"])
            self.assertIn(0x80, app.claims)
            app.bus.close()
            peer.close()
        run(go())

    def test_raw_subscription(self):
        from tractorsim.canbus import MemoryBus

        async def go():
            app = self._app(True, "raw")
            peer = MemoryBus("raw")
            await app.start()
            claim = name_encode({"identity": 9, "function": 130}).to_bytes(8, "little")
            peer.send(make_id(6, 0xEE00, 0x81), claim)
            await asyncio.sleep(0.05)
            app.bus._deliver(0x123, b"\x01\x02", ext=False)  # what SocketCAN delivers for an 11-bit frame
            c = FakeClient()
            app.on_client(c, {"t": "sub", "name": "raw", "on": True})
            first = c.got[-1]
            self.assertEqual(first["t"], "raw")
            self.assertTrue(first["reset"])
            self.assertEqual(first["rows"][0][2], make_id(6, 0xEE00, 0x81))  # the NAMEs come first
            await asyncio.sleep(0.25)
            raws = [o for o, sub in app.web.sent if o.get("t") == "raw"]
            rows = [r for o in raws for r in o["rows"]]
            self.assertIn((0x123, "0102", 0), [(r[2], r[3], r[4]) for r in rows])
            self.assertTrue(all(sub == "raw" for o, sub in app.web.sent if o.get("t") == "raw"))
            app.bus.close()
            peer.close()
        run(go())

    def test_pythoncan_spec(self):
        from tractorsim.canbus import open_bus
        with self.assertRaises(ValueError):
            open_bus("pythoncan:pcan")
        try:
            import can  # noqa: F401
        except ImportError:
            with self.assertRaises(RuntimeError):
                open_bus("pythoncan:pcan:PCAN_USBBUS1")


if __name__ == "__main__":
    unittest.main()
