"""The simulator inside a web page (Pyodide): the online version built by tools/build_demo.py.

Two ways to run, chosen on the page Instellingen (stored in the browser):
- demo: an in-process CAN bus ("memory") with the tractor ECU, VT and GNSS of the App and the demo implement of
  tools/fake_implement.py on the same bus;
- adapter: the App on a real CAN bus through a USB or serial adapter that the page drives (WebUSB / WebSerial,
  web/can/simbus.js), without the demo implement. The page calls `bus_rx` with received frames and `bus_state`
  when the adapter connects or goes away; the PageBus sends through `can_send`.
The page is the only client: instead of the WebSocket server, messages go through `post` (Python → page) and
`Demo.receive` (page → Python), as the same JSON texts the WebSocket carries.
"""
import asyncio
import errno
import json
import logging

from .app import App
from .canbus import Bus, MemoryBus

log = logging.getLogger(__name__)


class PageClient:
    def __init__(self, post):
        self.post = post
        self.subs = set()

    def send(self, obj):
        self.post(json.dumps(obj, separators=(",", ":")))


class PageLink:
    """Stands in for the WebServer of app.py: one client, the page itself."""

    def __init__(self, post):
        self.client = PageClient(post)

    async def start(self):
        pass

    def broadcast(self, obj, sub=None):
        if sub is None or sub in self.client.subs:
            self.client.send(obj)


class PageBus(Bus):
    """The CAN adapter of the page. `can_send(can_id, ext, hex)` returns 1 (queued), 0 (no adapter: the frame is
    dropped) or 2 (queue full: the Bus tries again)."""

    NO_ADAPTER = "geen CAN-adapter verbonden"

    def __init__(self, can_send):
        super().__init__()
        self.can_send = can_send
        self.connected = False
        self.name = self.NO_ADAPTER

    def _write(self, can_id, data):
        r = self.can_send(can_id, 1, data.hex())
        if r == 2:
            raise BlockingIOError(errno.EAGAIN, "zendwachtrij vol")
        if r != 1:
            raise OSError(errno.ENODEV, "geen CAN-adapter verbonden")

    def receive(self, text):
        """Frames from the adapter: JSON [[id, ext, hex], …]."""
        for can_id, ext, hx in json.loads(text):
            self._deliver(int(can_id), bytes.fromhex(hx), bool(ext))


class Demo:
    def __init__(self, post, data_dir="/data", can_send=None, store_config=None):
        """`can_send`: run on the page's CAN adapter instead of the demo bus. `store_config(text)`: keeps the
        settings in the browser, so they survive a reload."""
        self.link = PageLink(post)
        self.bus = PageBus(can_send) if can_send else None
        self.app = App("adapter" if self.bus else "memory", data_dir, None, None, 0, web=self.link, bus=self.bus)
        if store_config:
            self.app.on_saved = store_config
        self.implement = None

    async def start(self, implement=None):
        await self.app.start()
        if implement is None:
            implement = self.bus is None
        if implement:
            asyncio.ensure_future(self._implement())

    async def _implement(self):
        import fake_implement  # tools/ is on sys.path in the demo
        self.implement = await fake_implement.connect(MemoryBus())
        try:
            await self.implement.run()
        except Exception:
            log.exception("demo-werktuig gestopt")

    def hello(self):
        """The page is ready for messages: send it the full state, as on a new WebSocket connection."""
        self.app.on_connect(self.link.client)

    def receive(self, text):
        try:
            self.app.on_client(self.link.client, json.loads(text))
        except Exception:
            log.exception("bericht van de pagina")

    # ----- the page's CAN adapter
    def bus_rx(self, text):
        try:
            self.bus.receive(text)
        except Exception:
            log.exception("frames van de adapter")

    def bus_state(self, connected, label=""):
        """The adapter connected (`label`: its name and bitrate) or went away. On connecting, the control functions
        start again, so their Address Claimed goes out on the real bus."""
        self.bus.connected = bool(connected)
        self.bus.name = f"{label} (via de browser)" if connected else PageBus.NO_ADAPTER
        if connected:
            self.bus.stats["lastError"] = ""  # "no adapter" from before is no longer true
            self.app._build()
