"""Tests without hardware:  python3 -m unittest discover -s tests -v"""
import asyncio
import base64
import errno
import json
import os
import struct
import sys
import tempfile
import time
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


class FakeMcbaLink:
    """A Microchip CAN BUS Analyzer as McbaBus sees it: write(msg), read(timeout_ms), close()."""

    def __init__(self):
        import queue
        self.written = []
        self.inbox = queue.Queue()
        self.fail = None
        self.closed = False

    def write(self, msg, timeout_ms=1000):
        self.written.append(bytes(msg))

    def read(self, timeout_ms):
        import queue
        if self.fail:
            raise self.fail
        try:
            return self.inbox.get(timeout=timeout_ms / 1000)
        except queue.Empty:
            return b""

    def close(self):
        self.closed = True


class TestMcba(unittest.TestCase):
    """Microchip CAN BUS Analyzer over USB (--bus mcba), with a made-up analyzer: the byte layout of mcba_usb.c."""

    def test_codec(self):
        from tractorsim.canbus import MCBA_KA_CAN, MCBA_RX, mcba_command, mcba_decode, mcba_encode
        tx = mcba_encode(0x18FEF100, b"\x01\x02")
        self.assertEqual(tx.hex(), "a3f100c7ea02" + "0102" + "00" * 11)  # SID C7EA: id 28-18, 0x08, id 17-16
        self.assertEqual(len(mcba_encode(0x123, b"\xaa", ext=False)), 19)
        rx = (mcba_encode(0x18FEF100, b"\x01\x02", cmd=MCBA_RX) + mcba_encode(0x123, b"\xaa", ext=False, cmd=MCBA_RX)
              + mcba_command(MCBA_KA_CAN, 3, 130, 0, 0, 0, 250, 0, 0, 0, 2, 5))
        self.assertEqual(mcba_decode(rx), [
            ("frame", 0x18FEF100, b"\x01\x02", True), ("frame", 0x123, b"\xaa", False),
            ("ka_can", {"txErr": 3, "rxErr": 130, "rxOverflow": 0, "busOff": 0, "kbps": 250, "fw": "2.5"})])
        remote = bytearray(mcba_encode(0x18EA00F0, b"", cmd=MCBA_RX))
        remote[5] |= 0x40
        self.assertEqual(mcba_decode(bytes(remote)), [])

    def test_bus(self):
        from tractorsim.canbus import MCBA_KA_CAN, MCBA_RX, McbaBus, mcba_command, mcba_encode
        links = [FakeMcbaLink()]

        def plug():
            if links[-1].fail:
                raise RuntimeError("geen Microchip CAN BUS Analyzer gevonden")
            return links[-1]

        bus = McbaBus("", 250000, link_factory=plug)
        bus.RECONNECT_S = 0.05
        got, raw = [], []
        bus.listeners.append(lambda i, d, t: got.append((i, d)))
        bus.raw_listeners.append(lambda direction, i, d, t, ext: raw.append((direction, i, ext)))
        init = [mcba_command(0xA1, 0x00, 0xFA), mcba_command(0xA9, 1), mcba_command(0xA9, 2)]

        async def go():
            await bus.start()
            try:
                self.assertEqual(links[0].written, init)  # 250 kbit/s, firmware versions
                links[0].inbox.put(mcba_encode(0x18FEF100, b"\x01\x02", cmd=MCBA_RX)
                                   + mcba_encode(0x123, b"\xaa", ext=False, cmd=MCBA_RX))
                bus.send(0x0CFE4980, b"\x11" * 8)
                await asyncio.sleep(0.3)
                self.assertEqual(got, [(0x18FEF100, b"\x01\x02")])  # 11-bit only for the analyzer
                self.assertIn(("rx", 0x123, False), raw)
                self.assertEqual(links[0].written[-1], mcba_encode(0x0CFE4980, b"\x11" * 8))
                links[0].inbox.put(mcba_command(MCBA_KA_CAN, 0, 0, 0, 1, 0, 250))
                await asyncio.sleep(0.3)
                self.assertTrue(bus.stats["lastError"].startswith("bus-off"), bus.stats["lastError"])
                # unplugged: sending is refused, then the analyzer comes back
                links[0].fail = OSError(errno.ENODEV, "No such device")
                await asyncio.sleep(0.3)
                self.assertIsNone(bus.link)
                self.assertIn("losgekoppeld", bus.stats["lastError"])
                dropped = bus.stats["txDropped"]
                bus.send(0x0CFE4980, b"\x22" * 8)
                self.assertEqual(bus.stats["txDropped"], dropped + 1)
                links.append(FakeMcbaLink())
                await asyncio.sleep(0.4)
                self.assertIs(bus.link, links[1])
                self.assertEqual((links[1].written, bus.stats["lastError"], links[0].closed), (init, "", True))
            finally:
                bus.close()
            self.assertTrue(links[1].closed)
            self.assertFalse(any(th.is_alive() for th in bus._threads))

        run(go())

    def test_listen_and_spec(self):
        from tractorsim.canbus import McbaBus, open_bus
        link = FakeMcbaLink()
        bus = McbaBus("", 500000, link_factory=lambda: link)
        bus.listen_only = True

        async def go():
            await bus.start()
            try:
                bus.send(0x0CFE4980, b"\x11" * 8)
                await asyncio.sleep(0.2)
            finally:
                bus.close()

        run(go())
        self.assertEqual([m[0] for m in link.written], [0xA1, 0xA9, 0xA9])  # only the setup, no frame
        self.assertEqual(link.written[0][1:3], b"\x01\xf4")  # 500 kbit/s
        self.assertEqual((open_bus("mcba").select, open_bus("mcba:A1B2").select), ("", "A1B2"))
        try:
            import usb.core  # noqa: F401
        except ImportError:
            with self.assertRaises(RuntimeError):
                run(open_bus("mcba").start())


class TestOnlineAdapter(unittest.TestCase):
    """The online version (tractorsim/browser.py) on the page's CAN adapter instead of the demo bus."""

    def test_adapter_mode(self):
        import json
        from tractorsim.browser import Demo
        sent, posted, stored = [], [], []
        reply = {"code": 0}  # 0: no adapter, 1: queued, 2: queue full

        def can_send(can_id, ext, hx):
            if reply["code"] == 1:
                sent.append((can_id, ext, hx))
            return reply["code"]

        async def go():
            demo = Demo(posted.append, tempfile.mkdtemp(), can_send=can_send, store_config=stored.append)
            await demo.start()
            await asyncio.sleep(0.4)
            self.assertIsNone(demo.implement)  # no demo implement on a real bus
            self.assertEqual(sent, [])
            self.assertGreater(demo.bus.stats["txDropped"], 0)  # no adapter yet: dropped, not queued
            reply["code"] = 1
            demo.bus_state(True, "Microchip CAN BUS Analyzer · 250 kbit/s")
            self.assertIn("Microchip", demo.bus.name)
            await asyncio.sleep(0.6)
            claimed = {i & 0xFF for i, ext, hx in sent if (i >> 16) & 0xFF == 0xEE}
            self.assertIn(0xF0, claimed)  # the tractor ECU claims its address on the real bus
            rx = demo.bus.stats["rx"]
            demo.bus_rx(json.dumps([[0x18EAFFFE, 1, "00ee00"], [0x123, 0, "aa"]]))
            self.assertEqual(demo.bus.stats["rx"], rx + 2)
            reply["code"] = 2  # queue of the page full: the frame waits in the Bus
            demo.bus.send(0x18FF00F0, b"\x01")
            self.assertTrue(demo.bus._txq)
            reply["code"] = 1
            await asyncio.sleep(0.1)
            self.assertIn((0x18FF00F0, 1, "01"), sent)
            demo.bus_state(False)
            self.assertEqual(demo.bus.name, "geen CAN-adapter verbonden")
            demo.receive(json.dumps({"t": "cf", "key": "gnss", "values": {"enabled": False}}))
            self.assertFalse(json.loads(stored[-1])["cf"]["gnss"]["enabled"])  # kept in the browser

        run(go())

    def test_demo_mode_unchanged(self):
        from tractorsim.browser import Demo
        demo = Demo(lambda t: None, tempfile.mkdtemp())
        self.assertIsNone(demo.bus)
        self.assertEqual(demo.app.bus_spec, "memory")


# ---------------------------------------------------------------- task controller
class RecordEvents(FakeEvents):
    def __init__(self):
        self.log = []

    def tclog(self, entry):
        self.log.append(entry)


def _shapefile(polys, rates, prj='GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137,298.257223563]]]'):
    """A tiny shapefile (.zip): polygons [(lon, lat), …] (clockwise) with a numeric field RATE."""
    import io
    import zipfile
    recs = b""
    for k, ring in enumerate(polys):
        pts = ring + ring[:1]
        content = struct.pack("<i4dii", 5, 0, 0, 0, 0, 1, len(pts)) + struct.pack("<i", 0) + b"".join(struct.pack("<dd", x, y) for x, y in pts)
        recs += struct.pack(">ii", k + 1, len(content) // 2) + content
    head = struct.pack(">i5ii", 9994, 0, 0, 0, 0, 0, (100 + len(recs)) // 2) + struct.pack("<ii4d4d", 1000, 5, 0, 0, 0, 0, 0, 0, 0, 0)
    fields = b"RATE".ljust(11, b"\0") + b"N" + b"\0" * 4 + bytes([10, 2]) + b"\0" * 14
    hlen = 32 + len(fields) + 1
    dbf = struct.pack("<BBBBIHH", 3, 126, 1, 1, len(rates), hlen, 11) + b"\0" * 20 + fields + b"\x0d"
    dbf += b"".join(b" " + f"{r:10.2f}".encode() for r in rates) + b"\x1a"
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("kaart.shp", head + recs)
        f.writestr("kaart.dbf", dbf)
        if prj:
            f.writestr("kaart.prj", prj)
    return z.getvalue()


class TestTC(unittest.TestCase):
    NAME = name_encode({"identity": 9, "function": 132})

    def test_ddop(self):
        from tractorsim.tc import ddop as TD
        import demo_tc
        data = demo_tc.sprayer_ddop(self.NAME.to_bytes(8, "little"))
        objs = TD.parse(data)
        self.assertEqual(TD.encode(objs), data)
        self.assertEqual(TD.check(objs), [])
        pool = TD.Pool(objs, data)
        self.assertEqual(pool.label, b"DEMOSP1")
        geo = pool.sections()
        self.assertEqual(geo["boom"], demo_tc.EL_BOOM)
        self.assertEqual([s["y"] for s in geo["sections"]][:2], [-10500, -7500])
        self.assertEqual(geo["sections"][0]["x"], -1500)
        self.assertEqual(geo["sections"][0]["w"], 3000)
        v = TD.condensed([True, False, True], 0)
        self.assertEqual(v & 0x3F, 0b010001)
        self.assertEqual((v >> 6) & 3, 3)  # section 4 does not exist
        self.assertEqual(TD.uncondensed(v, 0, 3), {0: True, 1: False, 2: True})
        bad = [dict(o) for o in objs]
        det = next(o for o in bad if o["table"] == "DET")
        det["children"] = det["children"] + [999]
        self.assertEqual(TD.check(bad)[0][1], 999)
        with self.assertRaises(TD.DdopError):
            TD.parse(data[:-3])

    def _setup(self, tmp, position=lambda: None, sections=8):
        import demo_tc
        from tractorsim.tc.server import TCServer
        group = []
        sbus, ibus = LoopBus(group), LoopBus(group)
        snet, inet = Network(sbus), Network(ibus)
        snet.start(), inet.start()
        tcf = ControlFunction(snet, "TC", name_encode({"identity": 7, "function": 130}), 0xF7)
        wcf = ControlFunction(inet, "WS", self.NAME, 0x80)
        ev = RecordEvents()
        cfg = {"version": 4, "booms": 1, "sections": 16, "channels": 1, "memory": 1 << 20}
        tc = TCServer(snet, tcf, cfg, tmp, ev, lambda: b"nl" + b"\xff" * 6, position)
        client = demo_tc.TcClient(inet, wcf, demo_tc.sprayer_ddop(self.NAME.to_bytes(8, "little"), sections), sections)
        tcf.start(), wcf.start()
        tc.start()
        return tc, client, ev, (snet, inet, wcf)

    async def _until(self, cond, t=5.0):
        end = time.monotonic() + t
        while time.monotonic() < end:
            if cond():
                return True
            await asyncio.sleep(0.05)
        return False

    def test_session(self):
        import demo_tc

        async def go():
            tmp = tempfile.mkdtemp()
            tc, client, ev, (snet, inet, wcf) = self._setup(tmp)
            task = asyncio.ensure_future(client.run())
            self.assertTrue(await self._until(lambda: client.state == "verbonden"), client.state)
            c = tc.clients[0x80]
            self.assertEqual(c.state, "actief")
            self.assertEqual(c.pool.designator, "Demospuit")
            self.assertEqual(client.tc_version, 4)
            self.assertEqual(c.version["sections"], 8)
            self.assertTrue(os.path.exists(os.path.join(tmp, "tc", f"{self.NAME:016X}.ddop")))
            # measurement commands from the trigger methods, then every value requested once
            self.assertTrue(await self._until(lambda: (demo_tc.EL_TANK, 2) in client.triggers and (demo_tc.EL_TANK, 72) in c.values))
            self.assertEqual(client.triggers[(demo_tc.EL_TANK, 2)]["time"], 1000)
            self.assertEqual(c.values[(demo_tc.EL_TANK, 72)], demo_tc.TANK_ML)
            # section control on: the TC asks automatic mode, the implement reports it back on change
            self.assertTrue(await self._until(lambda: client.sc_auto))
            client.step(0.1, 0, False)
            self.assertTrue(await self._until(lambda: tc.sc_auto(c)))
            # a setpoint from the page
            tc.set_value(0x80, demo_tc.EL_TANK, 1, 2_000_000)
            self.assertTrue(await self._until(lambda: client.setpoint == 2_000_000))
            # the task: work state on, status bit
            tc.set_task("start")
            self.assertTrue(await self._until(lambda: client.work and client.tc_status & 1))
            tc.set_task("stop")
            self.assertTrue(await self._until(lambda: not client.work))
            transfers = sum(1 for e in ev.log if e["func"] == "Object-pool Transfer")
            self.assertEqual(transfers, 1)
            # connect again: same labels → no transfer
            task.cancel()
            tc.clients.clear()
            client2 = demo_tc.TcClient(inet, wcf, client.ddop)
            client2.tc, client2.tc_seen = 0xF7, time.monotonic()
            await client2.connect()
            self.assertEqual(sum(1 for e in ev.log if e["func"] == "Object-pool Transfer"), 1)
            self.assertEqual(tc.clients[0x80].state, "actief")
            # a client that stops sending Client Task is dropped after 6 s
            tc.clients[0x80].last_task -= 7
            self.assertTrue(await self._until(lambda: 0x80 not in tc.clients, 1.0))
            tc.stop()
        run(go())

    def test_etp_and_refused_pool(self):
        from tractorsim.tc import ddop as TD

        async def go():
            tc, client, ev, (snet, inet, wcf) = self._setup(tempfile.mkdtemp(), sections=40)
            self.assertGreater(len(client.ddop), 1785)  # extended transport protocol
            self.assertTrue(await self._until(lambda: wcf.active and tc.cf.active))
            client.tc = 0xF7
            client.tc_seen = time.monotonic()
            await client.connect()
            c = tc.clients[0x80]
            self.assertEqual(len(c.geometry["sections"]), 40)
            # a pool with a broken reference is refused with the faulty object
            objs = TD.parse(client.ddop)
            det = next(o for o in objs if o["table"] == "DET")
            det["children"].append(4321)
            client.ddop = TD.encode(objs)
            client.pool = TD.Pool(objs, client.ddop)
            client.pool.device["structureLabel"] = b"OTHER01"
            objs[0]["structureLabel"] = b"OTHER01"
            client.ddop = TD.encode(objs)
            client.pool = TD.Pool(objs, client.ddop)
            with self.assertRaises(RuntimeError) as cm:
                await client.connect()
            self.assertIn("4321", str(cm.exception))
            self.assertEqual(c.state, "fout")
            tc.stop()
        run(go())

    def test_section_control(self):
        from tractorsim.tc import ddop as TD
        from tractorsim.tc.field import Coverage, Field
        from tractorsim.tc.section import SectionControl
        import demo_tc
        data = demo_tc.sprayer_ddop(self.NAME.to_bytes(8, "little"))
        f = Field.rectangle(51.0, 4.0, 0.0, 100.0, 48.0)
        cov = Coverage.for_field(f)
        sc = SectionControl()
        sc.hitch = (0.0, 0.0)
        sc.configure(TD.Pool(TD.parse(data), data).sections())
        self.assertAlmostEqual(sc.width, 24.0)

        def drive(y, n0, n1, speed=3.0, dt=0.1):
            seen = []
            n = n0
            sc.reset()
            while n <= n1:
                want = sc.decide(y, n, 0.0, speed, cov, f)
                sc.paint(y, n, 0.0, want, cov)
                seen.append((n, list(want)))
                n += speed * dt
            return seen

        first = drive(12.0, -10.0, 110.0)
        inside = [w for n, w in first if 5 < n < 95]
        self.assertTrue(all(all(w) for w in inside))
        self.assertFalse(any(any(w) for n, w in first if n < -3 or n > 102))  # outside the field
        self.assertAlmostEqual(cov.area(), 100 * 24, delta=100)
        again = drive(12.0, 0.0, 100.0)  # the same lane: everything is worked already
        self.assertFalse(any(any(w) for n, w in again if 5 < n < 95))
        shifted = drive(33.0, 0.0, 100.0)  # next lane 3 m too close: the leftmost section overlaps fully
        mid = [w for n, w in shifted if 20 < n < 80]
        self.assertTrue(all(not w[0] and all(w[1:]) for w in mid), mid[:2])

    def test_prescription(self):
        from tractorsim.tc.field import Field
        from tractorsim.tc.prescription import MapError, Prescription
        f = Field.rectangle(51.0, 4.0, 0.0, 100.0, 50.0)
        fr = f.frame
        sq = lambda e0, n0, e1, n1: [fr.to_geo(e, n)[::-1] for e, n in ((e0, n0), (e1, n0), (e1, n1), (e0, n1), (e0, n0))]
        gj = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"naam": "a", "dosis": 150}, "geometry": {"type": "Polygon", "coordinates": [sq(0, 0, 50, 50)]}},
            {"type": "Feature", "properties": {"naam": "b", "dosis": "250,5"}, "geometry": {"type": "Polygon", "coordinates": [sq(0, 50, 50, 100)]}}]}
        p = Prescription.from_geojson(json.dumps(gj))
        self.assertEqual(p.prop, "dosis")
        self.assertEqual(p.rate_at(fr, 10, 10), 150)
        self.assertEqual(p.rate_at(fr, 10, 60), 250.5)
        self.assertIsNone(p.rate_at(fr, 80, 60))
        with self.assertRaises(MapError):
            Prescription.from_geojson(json.dumps(gj), prop="bestaatniet")
        # shapefile: clockwise rings (x = lon, y = lat)
        cw = lambda e0, n0, e1, n1: [fr.to_geo(e, n)[::-1] for e, n in ((e0, n0), (e0, n1), (e1, n1), (e1, n0))]
        shp = _shapefile([cw(0, 0, 50, 50), cw(0, 50, 50, 100)], [120.0, 180.0])
        p = Prescription.from_shapefile(shp)
        self.assertEqual(p.rate_at(fr, 25, 25), 120.0)
        self.assertEqual(p.rate_at(fr, 25, 75), 180.0)
        with self.assertRaises(MapError):
            Prescription.from_shapefile(_shapefile([cw(0, 0, 50, 50)], [1.0], 'PROJCS["Belge_Lambert_1972",GEOGCS["GCS_Belge_1972"]]'))
        d = Prescription.demo(f, 100, 200)
        rates = d.rates()
        self.assertTrue(rates and min(rates) >= 100 and max(rates) <= 200 and max(rates) > min(rates))
        self.assertIsNotNone(d.rate_at(fr, 25, 50))
        # saved and read again keeps the rates
        again = Prescription.from_geojson(p.save(), prop="rate")
        self.assertEqual(again.rate_at(fr, 25, 75), 180.0)

    def test_field_work(self):
        """Autopilot, section control and the prescription map together, with the demo sprayer."""
        import demo_tc
        from tractorsim.tc.prescription import Prescription

        async def go():
            sim = Sim()
            sim.set("drive.speed", 36)  # 10 m/s: a quick test
            pos = lambda: {"lat": sim.live["lat"], "lon": sim.live["lon"], "heading": sim.live["heading"], "speed": sim.ground_speed, "source": "sim"}
            tc, client, ev, nets = self._setup(tempfile.mkdtemp(), pos)
            tc.stop()  # the test drives tick() itself
            tc.sim = sim
            tc.opts["hitchX"] = 0.0
            tc._apply_sc()
            self.assertTrue(await self._until(lambda: nets[2].active and tc.cf.active))
            client.tc, client.tc_seen = 0xF7, time.monotonic()
            await client.connect()
            self.assertTrue(await self._until(lambda: client.sc_auto))
            tc.field_here(120, 48)
            tc._set_presc(Prescription.demo(tc.field, 100, 300))
            tc.set_autopilot(True)
            self.assertEqual(tc.autopilot.lanes, 2)
            tc.set_task("start")
            switched = set()
            for k in range(400):
                sim.step(0.1)
                client.step(0.1, sim.ground_speed * 3.6, client.work)
                client.report()
                tc.tick(0.1, k + 1)
                switched.add(tuple(client.sections))
                await asyncio.sleep(0.002)
                if tc.autopilot.finished:
                    break
            self.assertTrue(tc.autopilot.finished)
            self.assertGreater(tc.coverage.area(), 0.8 * 120 * 48)
            self.assertLess(tc.coverage.overlap_area(), 0.1 * 120 * 48)
            self.assertIn(tuple([False] * 8), switched)  # off outside the field and in the turn
            self.assertIn(tuple([True] * 8), switched)
            self.assertTrue(100 * 10000 <= client.setpoint <= 300 * 10000)  # rate from the map, raw 0.0001 L/ha
            self.assertTrue(client.presc_auto)
            self.assertGreater(len(tc.rows), 5)
            self.assertIn("Dosis instellen (80 el 3 DDI 1)", tc.log_csv())
            gj = json.loads(tc.coverage_geojson())
            self.assertEqual(gj["features"][0]["properties"]["name"], "bewerkt")
            sim.autopilot = None
        run(go())


if __name__ == "__main__":
    unittest.main()
