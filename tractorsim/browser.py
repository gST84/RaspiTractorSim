"""The simulator inside a web page (Pyodide), for the online demo built by tools/build_demo.py.

Everything runs in the browser: an in-process CAN bus ("memory"), the tractor ECU, VT and GNSS of the App,
and the demo implement of tools/fake_implement.py on the same bus. The page is the only client: instead of
the WebSocket server, messages go through `post` (Python → page) and `Demo.receive` (page → Python),
as the same JSON texts the WebSocket carries.
"""
import asyncio
import json
import logging

from .app import App
from .canbus import MemoryBus

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


class Demo:
    def __init__(self, post, data_dir="/data"):
        self.link = PageLink(post)
        self.app = App("memory", data_dir, None, None, 0, web=self.link)
        self.implement = None

    async def start(self, implement=True):
        await self.app.start()
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
