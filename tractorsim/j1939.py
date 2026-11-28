"""ISO 11783 / J1939 network layer: identifiers, NAME, address claim (ISO 11783-5), requests and the
transport protocols TP (≤ 1785 bytes, RTS/CTS and BAM) and ETP (> 1785 bytes) from ISO 11783-3.

One Network runs on one CAN bus and can host several control functions (CF): the simulator has a
tractor ECU, a virtual terminal and a GNSS receiver, each with its own NAME and address.
"""
import asyncio
import logging
import random
import time

log = logging.getLogger(__name__)

GLOBAL = 0xFF
NULL_ADDR = 0xFE

PGN_ACK = 0xE800
PGN_REQUEST = 0xEA00
PGN_ADDRESS_CLAIMED = 0xEE00
PGN_TP_CM = 0xEC00
PGN_TP_DT = 0xEB00
PGN_ETP_CM = 0xC800
PGN_ETP_DT = 0xC700
PGN_SOFTWARE_ID = 0xFEDA

TP_MAX = 1785


def make_id(prio, pgn, sa, da=GLOBAL):
    pf = (pgn >> 8) & 0xFF
    if pf < 240:
        pgn = (pgn & 0x3FF00) | (da & 0xFF)
    return ((prio & 7) << 26) | ((pgn & 0x3FFFF) << 8) | (sa & 0xFF)


def split_id(can_id):
    """→ (priority, pgn, source address, destination address)."""
    prio = (can_id >> 26) & 7
    pgn = (can_id >> 8) & 0x3FFFF
    pf = (pgn >> 8) & 0xFF
    sa = can_id & 0xFF
    if pf < 240:
        da = pgn & 0xFF
        pgn &= 0x3FF00
    else:
        da = GLOBAL
    return prio, pgn, sa, da


def is_pdu1(pgn):
    return ((pgn >> 8) & 0xFF) < 240


def u24(b, i):
    return b[i] | (b[i + 1] << 8) | (b[i + 2] << 16)


def le(v, n):
    return int(v).to_bytes(n, "little")


# ---------------------------------------------------------------- NAME (ISO 11783-5)
NAME_FIELDS = [
    ("identity", 0, 21), ("manufacturer", 21, 11), ("ecuInstance", 32, 3), ("functionInstance", 35, 5),
    ("function", 40, 8), ("reserved", 48, 1), ("deviceClass", 49, 7), ("deviceClassInstance", 56, 4),
    ("industryGroup", 60, 3), ("selfConfigurable", 63, 1),
]

FUNCTIONS = {0: "Engine", 3: "Transmission", 7: "Axle-steering", 16: "Steering controller", 19: "Instrument cluster",
             23: "Vehicle navigation (GNSS)", 25: "Network interconnect ECU", 28: "Off-vehicle gateway",
             29: "Virtual Terminal", 30: "Management computer", 33: "System monitor", 61: "Cab display",
             128: "Non-VT display", 129: "Operator controls", 130: "Task Controller", 131: "Position control",
             132: "Machine control", 133: "Product rate control", 134: "Tractor ECU", 135: "Section on/off control",
             137: "Product treatment", 139: "Datalogger", 140: "Decision support", 141: "Lighting controller"}
MANUFACTURERS = {94: "CNH Industrial", 151: "Raven Industries", 98: "Müller-Elektronik", 97: "Ag Leader", 69: "John Deere",
                 79: "AGCO", 111: "Kverneland", 116: "Amazone", 103: "Lemken", 135: "Claas", 115: "Krone", 99: "Fendt",
                 154: "Pöttinger", 148: "Horsch", 1: "Allied Signal"}
INDUSTRY = {0: "Global", 1: "On-highway", 2: "Landbouw", 3: "Bouw", 4: "Maritiem", 5: "Industrie"}


def name_encode(d):
    n = 0
    for k, pos, bits in NAME_FIELDS:
        n |= (int(d.get(k, 0)) & ((1 << bits) - 1)) << pos
    return n


def name_decode(n):
    return {k: (n >> pos) & ((1 << bits) - 1) for k, pos, bits in NAME_FIELDS}


def name_text(n):
    d = name_decode(n)
    fn = FUNCTIONS.get(d["function"], f"functie {d['function']}")
    man = MANUFACTURERS.get(d["manufacturer"], f"fabrikant {d['manufacturer']}")
    return f"{fn} (inst {d['functionInstance']}) · {man} · klasse {d['deviceClass']} · id {d['identity']}"


class Message:
    __slots__ = ("pgn", "sa", "da", "data", "prio", "t")

    def __init__(self, pgn, sa, da, data, prio=6, t=None):
        self.pgn, self.sa, self.da, self.data, self.prio = pgn, sa, da, bytes(data), prio
        self.t = t if t is not None else time.time()

    def __repr__(self):
        return f"<Msg PGN 0x{self.pgn:05X} {self.sa:02X}→{self.da:02X} {self.data.hex(' ')}>"


# ---------------------------------------------------------------- control function
class ControlFunction:
    """One of our own control functions: claims an address, sends and receives messages."""

    def __init__(self, net, label, name, address, self_configurable=True):
        self.net = net
        self.label = label
        self.name = name  # 64-bit NAME as int
        self.preferred = address
        self.address = NULL_ADDR
        self.self_configurable = self_configurable
        self.state = "uit"  # uit | claimen | actief | geen adres
        self.handlers = []  # fn(msg) for every message to this CF or global
        self.requests = {}  # pgn → fn(msg) returning bytes (or None = do not answer)
        self.on_claimed = []
        self._claim_task = None
        self.requests[PGN_SOFTWARE_ID] = lambda m: b"\x01raspISO*0.1*"

    @property
    def active(self):
        return self.state == "actief"

    def start(self):
        if self not in self.net.cfs:
            self.net.cfs.append(self)
        self.address = self.preferred
        self._begin_claim()

    def stop(self):
        if self._claim_task:
            self._claim_task.cancel()
        if self in self.net.cfs:
            self.net.cfs.remove(self)
        self.state = "uit"
        self.address = NULL_ADDR

    def _begin_claim(self):
        if self._claim_task:
            self._claim_task.cancel()
        self.state = "claimen"
        self.send_claim()
        self._claim_task = asyncio.ensure_future(self._finish_claim())

    async def _finish_claim(self):
        await asyncio.sleep(0.25)
        if self.state == "claimen":
            self.state = "actief"
            log.info("%s: adres 0x%02X geclaimd", self.label, self.address)
            for cb in self.on_claimed:
                cb()

    def send_claim(self):
        sa = self.address if self.state != "geen adres" else NULL_ADDR
        self.net.raw_send(make_id(6, PGN_ADDRESS_CLAIMED, sa, GLOBAL), le(self.name, 8))

    def _lost(self):
        """Another CF with a lower (= higher priority) NAME claimed our address."""
        if self.self_configurable:
            used = set(self.net.addresses) | {cf.address for cf in self.net.cfs if cf is not self}
            free = [a for a in range(128, 248) if a not in used]
            if free:
                log.warning("%s: adres 0x%02X verloren, nieuw adres 0x%02X", self.label, self.address, free[0])
                self.address = free[0]
                self._begin_claim()
                return
        log.warning("%s: geen adres beschikbaar (cannot claim)", self.label)
        self.state = "geen adres"
        self.address = NULL_ADDR
        self.send_claim()

    def send(self, pgn, data, da=GLOBAL, prio=6):
        """Sends a message; longer than 8 bytes goes via TP/ETP (returns a Task then). False when not active."""
        if not self.active:
            return False
        data = bytes(data)
        if len(data) <= 8:
            # Request is the only message with DLC 3; everything else is padded to 8 bytes
            self.net.raw_send(make_id(prio, pgn, self.address, da), data if pgn == PGN_REQUEST else data.ljust(8, b"\xff"))
            return True
        return asyncio.ensure_future(self.net.transport.send_long(self.address, pgn, data, da))

    def receive(self, msg):
        if msg.pgn == PGN_REQUEST:
            self._request(msg)
            return
        for h in self.handlers:
            try:
                h(msg)
            except Exception:
                log.exception("%s: fout bij verwerken van %r", self.label, msg)

    def _request(self, msg):
        if len(msg.data) < 3:
            return
        pgn = u24(msg.data, 0)
        if pgn == PGN_ADDRESS_CLAIMED:
            self.send_claim()
            return
        if not self.active:
            return
        fn = self.requests.get(pgn)
        resp = fn(msg) if fn else None
        if resp is not None:
            self.send(pgn, resp, da=msg.sa if is_pdu1(pgn) else GLOBAL, prio=6)
        elif msg.da != GLOBAL:
            # NACK (ISO 11783-3): control byte 1, group function FF, address of the requester, PGN
            self.send(PGN_ACK, bytes([1, 0xFF, 0xFF, 0xFF, msg.sa]) + le(pgn, 3), da=GLOBAL)


# ---------------------------------------------------------------- transport protocol
class _Rx:
    __slots__ = ("kind", "sa", "da", "pgn", "size", "total", "buf", "next", "window_end", "last", "offset",
                 "gap", "retries", "t0")

    def __init__(self, kind, sa, da, pgn, size):
        self.kind, self.sa, self.da, self.pgn, self.size = kind, sa, da, pgn, size
        self.total = (size + 6) // 7
        self.buf = bytearray(self.total * 7)
        self.next = 1  # next expected packet number (absolute, 1-based)
        self.window_end = 0
        self.offset = 0  # ETP data packet offset
        self.last = time.monotonic()
        self.t0 = self.last
        self.gap = False
        self.retries = 0


class Transport:
    """Receives TP/ETP/BAM sessions addressed to our CFs and sends long messages."""

    def __init__(self, net, window=64):
        self.net = net
        self.window = window
        self.rx = {}  # (proto, sa, da) → _Rx
        self.tx = {}  # (proto, our sa, peer) → asyncio.Queue of CM frames
        self.locks = {}

    # ----- receive
    def on_frame(self, pgn, sa, da, data):
        ours = self.net.is_ours(da)
        if pgn == PGN_TP_CM:
            self._tp_cm(sa, da, data, ours)
        elif pgn == PGN_ETP_CM:
            self._etp_cm(sa, da, data, ours)
        elif pgn == PGN_TP_DT:
            s = self.rx.get(("tp", sa, da))
            if s:
                self._dt(s, data[0], data[1:8])
        elif pgn == PGN_ETP_DT:
            s = self.rx.get(("etp", sa, da))
            if s:
                self._dt(s, s.offset + data[0], data[1:8])

    def _tp_cm(self, sa, da, d, ours):
        c = d[0]
        if c == 0x10 and ours:  # RTS
            size, total, maxp, pgn = d[1] | (d[2] << 8), d[3], d[4], u24(d, 5)
            s = _Rx("tp", sa, da, pgn, size)
            s.total = total
            s.buf = bytearray(total * 7)
            self.rx[("tp", sa, da)] = s
            self._progress(s)
            self._cts(s, maxp)
        elif c == 0x20 and da == GLOBAL:  # BAM
            s = _Rx("bam", sa, da, u24(d, 5), d[1] | (d[2] << 8))
            s.total = d[3]
            s.buf = bytearray(s.total * 7)
            self.rx[("tp", sa, da)] = s
        elif c in (0x11, 0x13):  # CTS / EoMA for a session we send
            q = self.tx.get(("tp", da, sa))
            if q:
                q.put_nowait(d)
        elif c == 0xFF:
            q = self.tx.get(("tp", da, sa))
            if q:
                q.put_nowait(d)
            s = self.rx.pop(("tp", sa, da), None)
            if s:
                self._progress(s, aborted=d[1])

    def _etp_cm(self, sa, da, d, ours):
        c = d[0]
        if c == 0x14 and ours:  # RTS
            s = _Rx("etp", sa, da, u24(d, 5), d[1] | (d[2] << 8) | (d[3] << 16) | (d[4] << 24))
            self.rx[("etp", sa, da)] = s
            self._progress(s)
            self._cts(s, 255)
        elif c == 0x16:  # DPO
            s = self.rx.get(("etp", sa, da))
            if s:
                s.offset = u24(d, 2)
                s.last = time.monotonic()
        elif c in (0x15, 0x17, 0xFF):
            q = self.tx.get(("etp", da, sa))
            if q:
                q.put_nowait(d)
            if c == 0xFF:
                s = self.rx.pop(("etp", sa, da), None)
                if s:
                    self._progress(s, aborted=d[1])

    def _cts(self, s, maxp=255):
        n = min(self.window, maxp or 255, 255, s.total - s.next + 1)
        s.window_end = s.next + n - 1
        s.gap = False
        s.last = time.monotonic()
        if s.kind == "tp":
            data = bytes([0x11, n, s.next, 0xFF, 0xFF]) + le(s.pgn, 3)
            self.net.raw_send(make_id(7, PGN_TP_CM, s.da, s.sa), data)
        else:
            data = bytes([0x15, n]) + le(s.next, 3) + le(s.pgn, 3)
            self.net.raw_send(make_id(7, PGN_ETP_CM, s.da, s.sa), data)

    def _dt(self, s, seq, chunk):
        s.last = time.monotonic()
        if seq != s.next:
            if seq > s.next:
                s.gap = True  # a packet got lost: ask again once this window has passed (see tick)
            return
        i = (seq - 1) * 7
        s.buf[i:i + 7] = bytes(chunk).ljust(7, b"\xff")
        s.next += 1
        if s.next > s.total:
            key = ("etp" if s.kind == "etp" else "tp", s.sa, s.da)
            self.rx.pop(key, None)
            if s.kind == "tp":
                self.net.raw_send(make_id(7, PGN_TP_CM, s.da, s.sa),
                                  bytes([0x13, s.size & 0xFF, s.size >> 8, s.total, 0xFF]) + le(s.pgn, 3))
            elif s.kind == "etp":
                self.net.raw_send(make_id(7, PGN_ETP_CM, s.da, s.sa), bytes([0x17]) + le(s.size, 4) + le(s.pgn, 3))
            self._progress(s, done=True)
            self.net.dispatch(Message(s.pgn, s.sa, s.da, bytes(s.buf[: s.size]), 7))
            return
        if s.kind != "bam" and s.next > s.window_end:
            self._cts(s)
        elif s.next % 64 == 0:
            self._progress(s)

    def _progress(self, s, done=False, aborted=None):
        if s.kind == "bam":
            return
        for cb in self.net.progress_listeners:
            cb(s.sa, s.da, s.pgn, min(s.size, (s.next - 1) * 7), s.size, done, aborted)

    def tick(self):
        """Timeouts (T1/T2) and retransmission requests; called every 50 ms."""
        now = time.monotonic()
        for key, s in list(self.rx.items()):
            idle = now - s.last
            if s.kind == "bam":
                if idle > 0.75:
                    del self.rx[key]
                continue
            if s.gap and idle > 0.05:
                self._cts(s)
            elif idle > 0.75:
                if s.retries < 3:
                    s.retries += 1
                    self._cts(s)
                else:
                    del self.rx[key]
                    pgn = PGN_TP_CM if s.kind == "tp" else PGN_ETP_CM
                    self.net.raw_send(make_id(7, pgn, s.da, s.sa), bytes([0xFF, 3, 0xFF, 0xFF, 0xFF]) + le(s.pgn, 3))
                    self._progress(s, aborted=3)

    # ----- send
    async def send_long(self, sa, pgn, data, da, progress=None):
        """Sends > 8 bytes: BAM (global), TP (≤ 1785 bytes) or ETP. Returns True when the receiver confirmed."""
        proto = "tp" if len(data) <= TP_MAX else "etp"
        if da == GLOBAL and proto == "etp":
            raise ValueError("ETP kan niet naar het globale adres")
        lock = self.locks.setdefault((proto, sa, da), asyncio.Lock())
        async with lock:
            if da == GLOBAL:
                return await self._bam(sa, pgn, data)
            q = asyncio.Queue()
            self.tx[(proto, sa, da)] = q
            try:
                if proto == "tp":
                    return await self._tp(sa, pgn, data, da, q, progress)
                return await self._etp(sa, pgn, data, da, q, progress)
            finally:
                self.tx.pop((proto, sa, da), None)

    async def _bam(self, sa, pgn, data):
        total = (len(data) + 6) // 7
        self.net.raw_send(make_id(7, PGN_TP_CM, sa, GLOBAL),
                          bytes([0x20, len(data) & 0xFF, len(data) >> 8, total, 0xFF]) + le(pgn, 3))
        for i in range(total):
            await asyncio.sleep(0.05)
            self.net.raw_send(make_id(7, PGN_TP_DT, sa, GLOBAL), bytes([i + 1]) + data[i * 7:i * 7 + 7].ljust(7, b"\xff"))
        return True

    async def _wait(self, q, timeout=1.25):
        try:
            return await asyncio.wait_for(q.get(), timeout)
        except asyncio.TimeoutError:
            return None

    async def _tp(self, sa, pgn, data, da, q, progress):
        total = (len(data) + 6) // 7
        self.net.raw_send(make_id(7, PGN_TP_CM, sa, da), bytes([0x10, len(data) & 0xFF, len(data) >> 8, total, 0xFF]) + le(pgn, 3))
        while True:
            d = await self._wait(q)
            if d is None:
                self.net.raw_send(make_id(7, PGN_TP_CM, sa, da), bytes([0xFF, 3, 0xFF, 0xFF, 0xFF]) + le(pgn, 3))
                return False
            if d[0] == 0x13:
                return True
            if d[0] == 0xFF:
                return False
            if d[0] == 0x11:
                n, nxt = d[1], d[2]
                for p in range(nxt, min(nxt + n, total + 1)):
                    self.net.raw_send(make_id(7, PGN_TP_DT, sa, da), bytes([p]) + data[(p - 1) * 7:p * 7].ljust(7, b"\xff"))
                if progress:
                    progress(min(len(data), (nxt + n - 1) * 7), len(data))
                await asyncio.sleep(0)

    async def _etp(self, sa, pgn, data, da, q, progress):
        size = len(data)
        total = (size + 6) // 7
        self.net.raw_send(make_id(7, PGN_ETP_CM, sa, da), bytes([0x14]) + le(size, 4) + le(pgn, 3))
        while True:
            d = await self._wait(q)
            if d is None:
                self.net.raw_send(make_id(7, PGN_ETP_CM, sa, da), bytes([0xFF, 3, 0xFF, 0xFF, 0xFF]) + le(pgn, 3))
                return False
            if d[0] == 0x17:
                return True
            if d[0] == 0xFF:
                return False
            if d[0] == 0x15:
                n, nxt = d[1], u24(d, 2)
                if n == 0:
                    continue
                n = min(n, total - nxt + 1)
                self.net.raw_send(make_id(7, PGN_ETP_CM, sa, da), bytes([0x16, n]) + le(nxt - 1, 3) + le(pgn, 3))
                for k in range(1, n + 1):
                    p = nxt - 1 + k
                    self.net.raw_send(make_id(7, PGN_ETP_DT, sa, da), bytes([k]) + data[(p - 1) * 7:p * 7].ljust(7, b"\xff"))
                    if k % 32 == 0:
                        await asyncio.sleep(0)
                if progress:
                    progress(min(size, (nxt + n - 1) * 7), size)


# ---------------------------------------------------------------- network
class Network:
    def __init__(self, bus):
        self.bus = bus
        self.cfs = []
        self.addresses = {}  # address → NAME of other CFs (from Address Claimed)
        self.nodes = {}  # address → {name, frames, first, last}
        self.listeners = []  # fn(msg) for every complete message on the bus (monitor)
        self.progress_listeners = []
        self.transport = Transport(self)
        bus.listeners.append(self._frame)
        self._tick = None

    def start(self):
        self._tick = asyncio.ensure_future(self._ticker())

    async def _ticker(self):
        while True:
            await asyncio.sleep(0.05)
            try:
                self.transport.tick()
            except Exception:
                log.exception("transport tick")

    def raw_send(self, can_id, data):
        self.bus.send(can_id, data)

    def is_ours(self, da):
        return any(cf.address == da and cf.state in ("actief", "claimen") for cf in self.cfs)

    def cf_at(self, addr):
        for cf in self.cfs:
            if cf.address == addr and cf.state in ("actief", "claimen"):
                return cf
        return None

    def _frame(self, can_id, data, t):
        prio, pgn, sa, da = split_id(can_id)
        if sa < NULL_ADDR:
            n = self.nodes.get(sa)
            if n is None:
                n = self.nodes[sa] = {"name": self.addresses.get(sa), "frames": 0, "first": t, "last": t}
            n["frames"] += 1
            n["last"] = t
        if pgn in (PGN_TP_CM, PGN_TP_DT, PGN_ETP_CM, PGN_ETP_DT):
            self.transport.on_frame(pgn, sa, da, data)
            return
        if pgn == PGN_ADDRESS_CLAIMED and len(data) >= 8:
            self._claim(sa, int.from_bytes(data[:8], "little"))
        self.dispatch(Message(pgn, sa, da, data, prio, t))

    def _claim(self, sa, name):
        if sa == NULL_ADDR:
            return
        # the same NAME at another address: forget the old address
        for a, n in list(self.addresses.items()):
            if n == name and a != sa:
                del self.addresses[a]
        self.addresses[sa] = name
        node = self.nodes.setdefault(sa, {"name": name, "frames": 0, "first": time.time(), "last": time.time()})
        node["name"] = name
        cf = self.cf_at(sa)
        if cf and cf.name != name:
            if name < cf.name:
                cf._lost()
            else:
                cf.send_claim()  # we have priority: claim again

    def dispatch(self, msg):
        for cb in self.listeners:
            try:
                cb(msg)
            except Exception:
                log.exception("fout in monitor")
        if msg.da == GLOBAL:
            for cf in list(self.cfs):
                if cf.state in ("actief", "claimen") or msg.pgn == PGN_REQUEST:
                    cf.receive(msg)
        else:
            cf = self.cf_at(msg.da)
            if cf:
                cf.receive(msg)

    def request(self, cf, pgn, da=GLOBAL):
        return cf.send(PGN_REQUEST, le(pgn, 3), da=da, prio=6)


def random_identity():
    return random.randint(1, 0x1FFFFF)
