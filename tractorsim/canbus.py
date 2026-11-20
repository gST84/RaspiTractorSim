"""CAN interfaces.

- SocketCanBus: Linux SocketCAN (Raspberry Pi with a CAN HAT as can0, or vcan0). Uses only the
  Python standard library (socket.AF_CAN), no python-can needed.
- HubBus / HubClientBus: a virtual CAN bus over UDP for testing without hardware (also on macOS).
  The simulator hosts the hub; other programs (tools/fake_implement.py) connect to it as clients.

Every bus delivers received frames to the callbacks in `listeners` as (can_id, data, timestamp),
where can_id is the 29-bit identifier without flags. Only extended (29-bit) data frames are passed on.
"""
import asyncio
import collections
import errno
import logging
import socket
import struct
import time

log = logging.getLogger(__name__)

CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_EFF_MASK = 0x1FFFFFFF
_FRAME = struct.Struct("=IB3x8s")  # struct can_frame
_UDP = struct.Struct("<IB8s")  # virtual bus datagram: id, dlc, data
HUB_HELLO = b"TSIM-HELLO"

# Error frame classes (linux/can/error.h), for the bus statistics.
ERR_CLASSES = {
    0x1: "TX-timeout", 0x2: "arbitrage verloren", 0x4: "controllerfout", 0x8: "protocolfout",
    0x10: "transceiverfout", 0x20: "geen ACK", 0x40: "bus-off", 0x80: "busfout", 0x100: "controller herstart",
}


class Bus:
    """Common part: listeners, statistics and a transmit queue that survives a full TX buffer."""

    name = "bus"

    def __init__(self):
        self.listeners = []
        self.tx_listeners = []
        self.stats = {"rx": 0, "tx": 0, "txDropped": 0, "errors": 0, "lastError": "", "rxRate": 0, "txRate": 0, "load": 0.0}
        self._txq = collections.deque()
        self._flush_handle = None
        self._rate_t = time.monotonic()
        self._rate_rx = 0
        self._rate_tx = 0
        self._bits = 0

    # ----- receive side
    def _deliver(self, can_id, data):
        self.stats["rx"] += 1
        self._bits += 64 + 8 * len(data)
        t = time.time()
        for cb in self.listeners:
            try:
                cb(can_id, data, t)
            except Exception:  # a listener must never stop the bus
                log.exception("fout in CAN-listener")

    # ----- transmit side
    def send(self, can_id, data):
        """Queues a 29-bit frame. Never blocks; frames wait in a queue when the driver is busy."""
        data = bytes(data)
        if len(self._txq) > 5000:
            self.stats["txDropped"] += 1
            return
        self._txq.append((can_id & CAN_EFF_MASK, data))
        if self._flush_handle is None:
            self._flush()

    def _flush(self):
        self._flush_handle = None
        while self._txq:
            can_id, data = self._txq[0]
            try:
                self._write(can_id, data)
            except BlockingIOError:
                break
            except OSError as e:
                if e.errno in (errno.ENOBUFS, errno.EAGAIN):
                    break
                self.stats["txDropped"] += 1
                self.stats["lastError"] = f"zenden: {e.strerror or e}"
                self._txq.popleft()
                continue
            self._txq.popleft()
            self.stats["tx"] += 1
            self._bits += 64 + 8 * len(data)
            t = time.time()
            for cb in self.tx_listeners:
                try:
                    cb(can_id, data, t)
                except Exception:
                    log.exception("fout in TX-listener")
        if self._txq:
            self._flush_handle = asyncio.get_event_loop().call_later(0.002, self._flush)

    def update_rates(self, bitrate=250000):
        """Frames per second and estimated bus load since the previous call."""
        now = time.monotonic()
        dt = max(now - self._rate_t, 1e-3)
        self.stats["rxRate"] = round((self.stats["rx"] - self._rate_rx) / dt)
        self.stats["txRate"] = round((self.stats["tx"] - self._rate_tx) / dt)
        self.stats["load"] = round(min(100.0, 100.0 * self._bits / dt / bitrate), 1)
        self._rate_t, self._rate_rx, self._rate_tx, self._bits = now, self.stats["rx"], self.stats["tx"], 0

    def _write(self, can_id, data):
        raise NotImplementedError

    async def start(self):
        pass

    def close(self):
        pass


class SocketCanBus(Bus):
    def __init__(self, channel="can0"):
        super().__init__()
        self.channel = channel
        self.name = f"SocketCAN {channel}"
        self.sock = None

    async def start(self):
        if not hasattr(socket, "AF_CAN"):
            raise RuntimeError("SocketCAN is enkel beschikbaar onder Linux; gebruik --bus virtual om te testen")
        s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        try:
            # also receive error frames (bus-off, missing ACK, ...) for the statistics
            s.setsockopt(getattr(socket, "SOL_CAN_RAW", 101), getattr(socket, "CAN_RAW_ERR_FILTER", 2),
                         struct.pack("=I", 0x1FFFFFFF))
        except OSError:
            pass
        s.bind((self.channel,))
        s.setblocking(False)
        self.sock = s
        asyncio.get_event_loop().add_reader(s.fileno(), self._readable)
        log.info("SocketCAN %s geopend", self.channel)

    def _readable(self):
        for _ in range(200):
            try:
                raw = self.sock.recv(16)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as e:
                self.stats["lastError"] = f"ontvangen: {e.strerror or e}"
                return
            if len(raw) < 16:
                continue
            can_id, dlc, data = _FRAME.unpack(raw)
            if can_id & CAN_ERR_FLAG:
                self.stats["errors"] += 1
                cls = can_id & 0x1FFFFFFF
                self.stats["lastError"] = ", ".join(v for k, v in ERR_CLASSES.items() if cls & k) or f"foutframe 0x{cls:X}"
                continue
            if not can_id & CAN_EFF_FLAG or can_id & CAN_RTR_FLAG:
                continue
            self._deliver(can_id & CAN_EFF_MASK, data[: min(dlc, 8)])

    def _write(self, can_id, data):
        self.sock.send(_FRAME.pack(can_id | CAN_EFF_FLAG, len(data), data.ljust(8, b"\0")))

    def close(self):
        if self.sock:
            try:
                asyncio.get_event_loop().remove_reader(self.sock.fileno())
            except Exception:
                pass
            self.sock.close()
            self.sock = None


class _Udp(asyncio.DatagramProtocol):
    def __init__(self, owner):
        self.owner = owner

    def datagram_received(self, data, addr):
        self.owner._datagram(data, addr)


class HubBus(Bus):
    """Virtual bus. This process is the hub: clients send frames to it, it passes them on to all other clients."""

    def __init__(self, host="127.0.0.1", port=29536):
        super().__init__()
        self.host, self.port = host, port
        self.name = f"virtuele bus (UDP {host}:{port})"
        self.peers = {}  # addr -> last seen
        self.transport = None

    async def start(self):
        loop = asyncio.get_event_loop()
        self.transport, _ = await loop.create_datagram_endpoint(lambda: _Udp(self), local_addr=(self.host, self.port))
        log.info("virtuele CAN-bus luistert op UDP %s:%d", self.host, self.port)

    def _datagram(self, data, addr):
        now = time.monotonic()
        self.peers[addr] = now
        if data == HUB_HELLO:
            return
        if len(data) != _UDP.size:
            return
        for p, seen in list(self.peers.items()):
            if now - seen > 5:
                del self.peers[p]
            elif p != addr:
                self.transport.sendto(data, p)
        can_id, dlc, payload = _UDP.unpack(data)
        self._deliver(can_id & CAN_EFF_MASK, payload[: min(dlc, 8)])

    def _write(self, can_id, data):
        pkt = _UDP.pack(can_id, len(data), data.ljust(8, b"\xff"))
        for p in self.peers:
            self.transport.sendto(pkt, p)

    def close(self):
        if self.transport:
            self.transport.close()


class HubClientBus(Bus):
    """Connects to the virtual bus of a running simulator."""

    def __init__(self, host="127.0.0.1", port=29536):
        super().__init__()
        self.host, self.port = host, port
        self.name = f"virtuele bus-client ({host}:{port})"
        self.transport = None
        self._hello = None

    async def start(self):
        loop = asyncio.get_event_loop()
        self.transport, _ = await loop.create_datagram_endpoint(lambda: _Udp(self), remote_addr=(self.host, self.port))
        self._say_hello()

    def _say_hello(self):
        if self.transport:
            self.transport.sendto(HUB_HELLO)
            self._hello = asyncio.get_event_loop().call_later(1.0, self._say_hello)

    def _datagram(self, data, addr):
        if len(data) == _UDP.size:
            can_id, dlc, payload = _UDP.unpack(data)
            self._deliver(can_id & CAN_EFF_MASK, payload[: min(dlc, 8)])

    def _write(self, can_id, data):
        self.transport.sendto(_UDP.pack(can_id, len(data), data.ljust(8, b"\xff")))

    def close(self):
        if self._hello:
            self._hello.cancel()
        if self.transport:
            self.transport.close()
            self.transport = None


_MEMORY_GROUPS = collections.defaultdict(list)


class MemoryBus(Bus):
    """In-process bus: a frame goes to every other MemoryBus of the same group in this program.
    No hardware and no network; used by the browser demo (tractorsim/browser.py)."""

    def __init__(self, group="default"):
        super().__init__()
        self.name = "geheugenbus (in de browser)" if group == "default" else f"geheugenbus {group}"
        self.group = _MEMORY_GROUPS[group]
        self.group.append(self)

    def _write(self, can_id, data):
        loop = asyncio.get_event_loop()
        for b in self.group:
            if b is not self:
                loop.call_soon(b._deliver, can_id, data)

    def close(self):
        if self in self.group:
            self.group.remove(self)


def open_bus(spec):
    """'can0' / 'vcan0' → SocketCAN; 'virtual' or 'virtual:HOST:PORT' → hub; 'client:HOST:PORT' → hub client;
    'memory' or 'memory:GROUP' → in-process bus."""
    kind, _, rest = spec.partition(":")
    if kind == "memory":
        return MemoryBus(rest or "default")
    if kind == "virtual":
        host, _, port = rest.partition(":")
        return HubBus(host or "127.0.0.1", int(port or 29536))
    if kind == "client":
        host, _, port = rest.partition(":")
        return HubClientBus(host or "127.0.0.1", int(port or 29536))
    return SocketCanBus(spec)
