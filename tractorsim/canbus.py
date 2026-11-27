"""CAN interfaces.

- SocketCanBus: Linux SocketCAN (Raspberry Pi with a CAN HAT as can0, or vcan0). Uses only the
  Python standard library (socket.AF_CAN), no python-can needed.
- HubBus / HubClientBus: a virtual CAN bus over UDP for testing without hardware (also on macOS).
  The simulator hosts the hub; other programs (tools/fake_implement.py) connect to it as clients.
- PythonCanBus: any interface of python-can (PCAN, Kvaser, Vector, IXXAT, slcan, ...), only when python-can
  is installed: "pythoncan:<interface>:<channel>".
- McbaBus: the Microchip CAN BUS Analyzer directly over USB with pyusb (optional, needs libusb), for macOS where
  there is no SocketCAN: "mcba" or "mcba:<index or serial number>".

Every bus delivers received frames to the callbacks in `listeners` as (can_id, data, timestamp),
where can_id is the 29-bit identifier without flags. Only extended (29-bit) data frames are passed on.
`raw_listeners` get every frame, 11-bit included, as (direction, can_id, data, timestamp, extended):
that is what the analyzer page (Variabelen) decodes.
With `listen_only` set (tractorsim --listen) send() drops every frame: nothing goes on the bus.
"""
import asyncio
import collections
import errno
import logging
import queue
import socket
import struct
import threading
import time

log = logging.getLogger(__name__)

CAN_EFF_FLAG = 0x80000000
CAN_RTR_FLAG = 0x40000000
CAN_ERR_FLAG = 0x20000000
CAN_EFF_MASK = 0x1FFFFFFF
CAN_SFF_MASK = 0x7FF
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
    listen_only = False

    def __init__(self):
        self.listeners = []
        self.tx_listeners = []
        self.raw_listeners = []
        self.stats = {"rx": 0, "tx": 0, "txDropped": 0, "txBlocked": 0, "errors": 0, "lastError": "", "rxRate": 0, "txRate": 0,
                      "load": 0.0}
        self._txq = collections.deque()
        self._flush_handle = None
        self._rate_t = time.monotonic()
        self._rate_rx = 0
        self._rate_tx = 0
        self._bits = 0

    # ----- receive side
    def _deliver(self, can_id, data, ext=True):
        self.stats["rx"] += 1
        self._bits += 64 + 8 * len(data)
        t = time.time()
        for cb in self.raw_listeners:
            try:
                cb("rx", can_id, data, t, ext)
            except Exception:
                log.exception("fout in raw-listener")
        if not ext:
            return
        for cb in self.listeners:
            try:
                cb(can_id, data, t)
            except Exception:  # a listener must never stop the bus
                log.exception("fout in CAN-listener")

    # ----- transmit side
    def send(self, can_id, data):
        """Queues a 29-bit frame. Never blocks; frames wait in a queue when the driver is busy."""
        data = bytes(data)
        if self.listen_only:
            self.stats["txBlocked"] += 1
            return
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
            for cb in self.raw_listeners:
                try:
                    cb("tx", can_id, data, t, True)
                except Exception:
                    log.exception("fout in raw-listener")
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
            if can_id & CAN_RTR_FLAG:
                continue
            if not can_id & CAN_EFF_FLAG:  # 11-bit: only for the analyzer
                self._deliver(can_id & CAN_SFF_MASK, data[: min(dlc, 8)], ext=False)
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


class PythonCanBus(Bus):
    """An interface of python-can (optional dependency): PCAN, Kvaser, Vector, IXXAT, slcan, socketcan, ...
    The driver of the manufacturer must be installed; python-can calls it from its own thread and the Notifier
    hands every frame to the asyncio loop."""

    def __init__(self, interface, channel, bitrate=250000, listen=False):
        super().__init__()
        try:
            import can  # noqa: PLC0415 - optional
        except ImportError as e:
            raise RuntimeError("python-can is niet geïnstalleerd: pip install python-can (plus de driver van de fabrikant)") from e
        self.can = can
        self.interface, self.channel = interface, channel
        self.name = f"python-can {interface}:{channel}"
        self.bitrate = bitrate
        self.listen = listen
        self.dev = None
        self.notifier = None

    async def start(self):
        kw = {"interface": self.interface, "channel": self.channel}
        if self.interface not in ("socketcan", "virtual"):
            kw["bitrate"] = self.bitrate
        if self.listen and self.interface == "pcan":
            kw["state"] = self.can.BusState.PASSIVE  # PCAN listen-only
        self.dev = self.can.Bus(**kw)
        self.notifier = self.can.Notifier(self.dev, [self._on_msg], loop=asyncio.get_event_loop())
        log.info("python-can %s:%s geopend", self.interface, self.channel)

    def _on_msg(self, msg):
        if msg.is_error_frame:
            self.stats["errors"] += 1
            self.stats["lastError"] = "foutframe"
            return
        if msg.is_remote_frame:
            return
        self._deliver(msg.arbitration_id, bytes(msg.data[: min(msg.dlc, 8)]), ext=bool(msg.is_extended_id))

    def _write(self, can_id, data):
        try:
            self.dev.send(self.can.Message(arbitration_id=can_id, data=data, is_extended_id=True), timeout=0)
        except self.can.CanError as e:
            raise OSError(errno.ENOBUFS, str(e)) from e

    def close(self):
        if self.notifier:
            self.notifier.stop()
        if self.dev:
            self.dev.shutdown()
        self.dev = self.notifier = None


# ---------------------------------------------------------------- Microchip CAN BUS Analyzer over USB (pyusb)
# Protocol of the Linux driver drivers/net/can/usb/mcba_usb.c: 19-byte messages on two bulk endpoints.
#   0xE3 received frame, 0xA3 frame to send: cmd, EID (BE16), SID (BE16), DLC, data[8], timestamp[4], checksum
#        extended: SID = id bits 28-18 in bits 15-5, 0x08, id bits 17-16 in bits 1-0; EID = id bits 15-0
#        standard: SID = id bits 10-0 in bits 15-5
#   0xF5 keep-alive of the CAN PIC: TX/RX error counter, RX overflow, bus-off, bitrate (kbit/s, BE16), firmware
#   0xF7 keep-alive of the USB PIC: termination (0 = on), firmware
#   0xA1 bitrate (kbit/s, BE16), 0xA8 termination (0 = on, 1 = off), 0xA9 firmware version (1 = USB, 2 = CAN)
MCBA_VID, MCBA_PID = 0x04D8, 0x0A30
MCBA_RX, MCBA_TX, MCBA_KA_CAN, MCBA_KA_USB = 0xE3, 0xA3, 0xF5, 0xF7
MCBA_BITRATE, MCBA_TERMINATION, MCBA_FW = 0xA1, 0xA8, 0xA9
_MCBA_HEAD = struct.Struct(">BHHB")


def mcba_command(cmd, *args):
    """A 19-byte command message."""
    return bytes([cmd, *args]).ljust(19, b"\0")


def mcba_encode(can_id, data, ext=True, cmd=MCBA_TX):
    """A frame as the analyzer's 19-byte message (0xA3 to send; 0xE3 as the analyzer sends it, for tests)."""
    if ext:
        sid = 0x08 | ((can_id & 0x1FFC0000) >> 13) | ((can_id & 0x30000) >> 16)
        eid = can_id & 0xFFFF
    else:
        sid, eid = (can_id & CAN_SFF_MASK) << 5, 0
    data = bytes(data[:8])
    return _MCBA_HEAD.pack(cmd, eid, sid, len(data)) + data.ljust(8, b"\0") + bytes(5)


def mcba_decode(buf):
    """Splits a USB transfer into ("frame", can_id, data, ext), ("ka_can", dict) and ("ka_usb", dict)."""
    out = []
    for i in range(0, len(buf) - 18, 19):
        m = buf[i:i + 19]
        if m[0] == MCBA_RX:
            _, eid, sid, dlc = _MCBA_HEAD.unpack_from(m)
            if dlc & 0x40:  # remote frame
                continue
            data = bytes(m[6:6 + min(dlc & 0x0F, 8)])
            if sid & 0x08:
                out.append(("frame", ((sid & 0xFFE0) << 13) | ((sid & 0x03) << 16) | eid, data, True))
            else:
                out.append(("frame", (sid & 0xFFE0) >> 5, data, False))
        elif m[0] == MCBA_KA_CAN:
            out.append(("ka_can", {"txErr": m[1], "rxErr": m[2], "rxOverflow": m[3], "busOff": m[4],
                                   "kbps": (m[5] << 8) | m[6], "fw": f"{m[10]}.{m[11]}"}))
        elif m[0] == MCBA_KA_USB:
            out.append(("ka_usb", {"termination": m[1] == 0, "fw": f"{m[2]}.{m[3]}"}))
    return out


class _PyUsbLink:
    """The analyzer's bulk endpoints through pyusb: write(msg), read(timeout_ms) → bytes (b"" on a time-out), close().
    `select` is empty (the first analyzer), an index ("1") or a serial number."""

    def __init__(self, select=""):
        try:
            import usb.core  # noqa: PLC0415 - optional
            import usb.util  # noqa: PLC0415
        except ImportError as e:
            raise RuntimeError("pyusb is niet geïnstalleerd: zie de README (brew install libusb, pyusb in een venv)") from e
        self.usb = usb
        try:
            devs = list(usb.core.find(find_all=True, idVendor=MCBA_VID, idProduct=MCBA_PID))
        except usb.core.NoBackendError as e:
            raise RuntimeError("libusb ontbreekt: brew install libusb") from e
        if not devs:
            raise RuntimeError("geen Microchip CAN BUS Analyzer gevonden (USB 04D8:0A30): is hij ingeplugd?")
        if select.isdigit():
            if int(select) >= len(devs):
                raise RuntimeError(f"analyzer {select} niet gevonden: er zijn er {len(devs)} aangesloten (vanaf 0)")
            dev = devs[int(select)]
        elif select:
            dev = next((d for d in devs if self._serial(d) == select), None)
            if dev is None:
                raise RuntimeError(f"geen analyzer met serienummer {select}")
        else:
            dev = devs[0]
        self.dev = dev
        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass  # already configured
        intf = dev.get_active_configuration()[(0, 0)]
        self.intf = intf.bInterfaceNumber
        try:
            if dev.is_kernel_driver_active(self.intf):  # Linux: mcba_usb has it (there --bus can0 is simpler)
                dev.detach_kernel_driver(self.intf)
        except (NotImplementedError, usb.core.USBError):
            pass
        usb.util.claim_interface(dev, self.intf)

        def bulk(direction):
            return usb.util.find_descriptor(intf, custom_match=lambda e: (
                usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK
                and usb.util.endpoint_direction(e.bEndpointAddress) == direction))

        self.ep_in, self.ep_out = bulk(usb.util.ENDPOINT_IN), bulk(usb.util.ENDPOINT_OUT)
        if self.ep_in is None or self.ep_out is None:
            self.close()
            raise RuntimeError("het toestel 04D8:0A30 heeft geen bulk-endpoints: geen CAN BUS Analyzer?")
        self.size = max(64, self.ep_in.wMaxPacketSize)

    def _serial(self, dev):
        try:
            return self.usb.util.get_string(dev, dev.iSerialNumber) or ""
        except (self.usb.core.USBError, ValueError, NotImplementedError):
            return ""

    def _error(self, e):
        """None for a time-out, else an OSError."""
        if isinstance(e, getattr(self.usb.core, "USBTimeoutError", ())) or e.errno == errno.ETIMEDOUT \
                or getattr(e, "backend_error_code", None) == -7:
            return None
        return OSError(e.errno or errno.EIO, str(e))

    def read(self, timeout_ms):
        try:
            return bytes(self.ep_in.read(self.size, timeout=timeout_ms))
        except self.usb.core.USBError as e:
            err = self._error(e)
            if err is None:
                return b""
            raise err from e

    def write(self, msg, timeout_ms=1000):
        try:
            self.ep_out.write(msg, timeout=timeout_ms)
        except self.usb.core.USBError as e:
            raise (self._error(e) or TimeoutError(errno.ETIMEDOUT, "time-out van de analyzer")) from e

    def close(self):
        try:
            self.usb.util.release_interface(self.dev, self.intf)
        except Exception:  # unplugged
            pass
        self.usb.util.dispose_resources(self.dev)


class McbaBus(Bus):
    """Microchip CAN BUS Analyzer (APGDT002) directly over USB, without a kernel driver: for macOS. Linux has the
    kernel driver mcba_usb, use --bus can0 there.
    A reader thread hands every USB transfer to the asyncio loop and a writer thread sends the frames, so a full
    buffer of the analyzer (no ACK on the bus, bus-off) never blocks the loop. After unplugging, the bus connects
    again as soon as the analyzer is back. The analyzer has no listen-only mode: it ACKs frames, also with
    --listen (then the simulator sends nothing). The termination stays as it is set in the analyzer."""

    RECONNECT_S = 2.0

    def __init__(self, select="", bitrate=250000, link_factory=None):
        super().__init__()
        self.select = select
        self.bitrate = bitrate
        self.name = "Microchip CAN BUS Analyzer" + (f" {select}" if select else "")
        self._open_link = link_factory or (lambda: _PyUsbLink(select))
        self.link = None
        self.ka = {}  # last keep-alive values: error counters, bus-off, bitrate, firmware, termination
        self._out = queue.Queue(maxsize=64)
        self._stop = threading.Event()
        self._threads = []
        self._loop = None
        self._state = ""

    def _connect(self):
        link = self._open_link()
        kbps = round(self.bitrate / 1000)
        try:
            for msg in (mcba_command(MCBA_BITRATE, kbps >> 8, kbps & 0xFF), mcba_command(MCBA_FW, 1), mcba_command(MCBA_FW, 2)):
                link.write(msg)
        except Exception:
            link.close()
            raise
        return link

    async def start(self):
        self._loop = asyncio.get_running_loop()
        self.link = self._connect()
        if self.listen_only:
            log.warning("luistermodus: de simulator zendt niets, maar de Microchip-analyzer bevestigt wel frames (ACK)")
        for target in (self._reader, self._writer):
            th = threading.Thread(target=target, name=f"mcba{target.__name__}", daemon=True)
            th.start()
            self._threads.append(th)
        log.info("%s geopend op %d kbit/s", self.name, round(self.bitrate / 1000))

    def _post(self, fn, *args):
        try:
            self._loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:  # loop closed
            pass

    def _reader(self):
        while not self._stop.is_set():
            link = self.link
            if link is None:
                if self._stop.wait(self.RECONNECT_S):
                    return
                try:
                    self.link = self._connect()
                except Exception:  # still unplugged
                    continue
                self._post(self._back)
                continue
            try:
                data = link.read(100)
            except OSError as e:
                self.link = None
                try:
                    link.close()
                except Exception:
                    pass
                self._post(self._lost, f"USB: {e.strerror or e} (losgekoppeld?)")
                continue
            if data:
                self._post(self._packet, data)

    def _writer(self):
        while not self._stop.is_set():
            try:
                msg = self._out.get(timeout=0.2)
            except queue.Empty:
                continue
            link = self.link
            if link is None:
                continue
            try:
                link.write(msg)
            except OSError as e:
                self._post(self._tx_failed, f"zenden: {e.strerror or e}")

    # ----- on the asyncio loop
    def _packet(self, data):
        for kind, *rest in mcba_decode(data):
            if kind == "frame":
                self._deliver(*rest)
            elif kind == "ka_can":
                self._keepalive(rest[0])
            else:
                if "usbFw" not in self.ka:
                    log.info("%s: firmware USB %s, afsluitweerstand %s", self.name, rest[0]["fw"],
                             "aan" if rest[0]["termination"] else "uit")
                self.ka.update(usbFw=rest[0]["fw"], termination=rest[0]["termination"])

    def _keepalive(self, ka):
        kbps = round(self.bitrate / 1000)
        if "canFw" not in self.ka and ka["kbps"] and ka["kbps"] != kbps:
            log.warning("%s meldt %d kbit/s, ingesteld is %d kbit/s", self.name, ka["kbps"], kbps)
        self.ka.update(txErr=ka["txErr"], rxErr=ka["rxErr"], busOff=ka["busOff"], kbps=ka["kbps"], canFw=ka["fw"])
        worst = max(ka["txErr"], ka["rxErr"])
        state = "bus-off" if ka["busOff"] else "error passive" if worst > 127 else "error warning" if worst > 95 else ""
        if state != self._state:
            self._state = state
            if state:
                self.stats["errors"] += 1
                self.stats["lastError"] = f"{state} (TEC {ka['txErr']}, REC {ka['rxErr']})"

    def _lost(self, why):
        self.stats["errors"] += 1
        self.stats["lastError"] = why
        log.warning("%s: %s; elke %g s opnieuw proberen", self.name, why, self.RECONNECT_S)

    def _back(self):
        self.stats["lastError"] = ""
        log.info("%s opnieuw verbonden", self.name)

    def _tx_failed(self, why):
        self.stats["txDropped"] += 1
        self.stats["lastError"] = why

    def _write(self, can_id, data):
        if self.link is None:
            raise OSError(errno.ENODEV, "analyzer niet verbonden")
        try:
            self._out.put_nowait(mcba_encode(can_id, data))
        except queue.Full:
            raise BlockingIOError(errno.EAGAIN, "zendbuffer vol") from None

    def close(self):
        self._stop.set()
        for th in self._threads:
            th.join(timeout=1.0)
        self._threads = []
        link, self.link = self.link, None
        if link:
            try:
                link.close()
            except Exception:
                log.debug("analyzer sluiten mislukt", exc_info=True)


def open_bus(spec, bitrate=250000, listen=False):
    """'can0' / 'vcan0' → SocketCAN; 'virtual' or 'virtual:HOST:PORT' → hub; 'client:HOST:PORT' → hub client;
    'memory' or 'memory:GROUP' → in-process bus; 'pythoncan:INTERFACE:CHANNEL' → python-can;
    'mcba' or 'mcba:INDEX|SERIAL' → Microchip CAN BUS Analyzer over USB (pyusb)."""
    kind, _, rest = spec.partition(":")
    if kind == "mcba":
        return McbaBus(rest, bitrate)
    if kind == "pythoncan":
        interface, _, channel = rest.partition(":")
        if not interface or not channel:
            raise ValueError("gebruik pythoncan:INTERFACE:KANAAL, bv. pythoncan:pcan:PCAN_USBBUS1 of pythoncan:kvaser:0")
        return PythonCanBus(interface, channel, bitrate, listen)
    if kind == "memory":
        return MemoryBus(rest or "default")
    if kind == "virtual":
        host, _, port = rest.partition(":")
        return HubBus(host or "127.0.0.1", int(port or 29536))
    if kind == "client":
        host, _, port = rest.partition(":")
        return HubClientBus(host or "127.0.0.1", int(port or 29536))
    return SocketCanBus(spec)
