"""Small HTTP/1.1 + WebSocket server on asyncio (standard library only, so nothing to install on the Pi).

Static files come from the web/ folder; live data goes over one WebSocket (/ws) as JSON messages.
"""
import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import struct
import urllib.parse

log = logging.getLogger(__name__)
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_BODY = 32 * 1024 * 1024
TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css", ".html": "text/html",
         ".svg": "image/svg+xml", ".json": "application/json", ".png": "image/png", ".ico": "image/x-icon"}


class Client:
    def __init__(self, writer, peer):
        self.writer = writer
        self.peer = peer
        self.subs = set()
        self.closed = False

    def send(self, obj):
        self.send_text(json.dumps(obj, separators=(",", ":")))

    def send_text(self, text):
        if self.closed:
            return
        if self.writer.transport.get_write_buffer_size() > 8 * 1024 * 1024:
            log.warning("webclient %s te traag, verbinding gesloten", self.peer)
            self.close()
            return
        payload = text.encode()
        n = len(payload)
        if n < 126:
            head = bytes([0x81, n])
        elif n < 65536:
            head = bytes([0x81, 126]) + struct.pack(">H", n)
        else:
            head = bytes([0x81, 127]) + struct.pack(">Q", n)
        try:
            self.writer.write(head + payload)
        except Exception:
            self.close()

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.writer.close()
            except Exception:
                pass


class WebServer:
    def __init__(self, app, host, port, root):
        self.app, self.host, self.port, self.root = app, host, port, os.path.abspath(root)
        self.clients = set()
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self._handle, self.host, self.port)
        log.info("webinterface op http://%s:%d", "localhost" if self.host in ("0.0.0.0", "") else self.host, self.port)

    def broadcast(self, obj, sub=None):
        if not self.clients:
            return
        text = json.dumps(obj, separators=(",", ":"))
        for c in list(self.clients):
            if sub is None or sub in c.subs:
                c.send_text(text)

    async def _handle(self, reader, writer):
        peer = writer.get_extra_info("peername")
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                parts = line.decode("latin-1").strip().split(" ")
                if len(parts) < 2:
                    break
                method, target = parts[0].upper(), parts[1]
                headers = {}
                while True:
                    h = await reader.readline()
                    if h in (b"\r\n", b"\n", b""):
                        break
                    k, _, v = h.decode("latin-1").partition(":")
                    headers[k.strip().lower()] = v.strip()
                n = int(headers.get("content-length") or 0)
                if n > MAX_BODY:
                    await self._respond(writer, 413, "text/plain", b"te groot")
                    break
                body = await reader.readexactly(n) if n else b""
                url = urllib.parse.urlsplit(target)
                if url.path == "/ws" and "websocket" in headers.get("upgrade", "").lower():
                    await self._websocket(reader, writer, headers, peer)
                    return
                status, ctype, data, extra = await self._route(method, url.path, urllib.parse.parse_qs(url.query), body)
                await self._respond(writer, status, ctype, data, extra, head=method == "HEAD")
                if headers.get("connection", "").lower() == "close":
                    break
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception:
            log.exception("HTTP-fout")
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _respond(self, writer, status, ctype, data, extra=None, head=False):
        reason = {200: "OK", 404: "Not Found", 400: "Bad Request", 413: "Payload Too Large", 405: "Method Not Allowed"}.get(status, "OK")
        hdr = [f"HTTP/1.1 {status} {reason}", f"Content-Type: {ctype}", f"Content-Length: {len(data)}", "Cache-Control: no-cache"]
        hdr += [f"{k}: {v}" for k, v in (extra or {}).items()]
        writer.write(("\r\n".join(hdr) + "\r\n\r\n").encode("latin-1") + (b"" if head else data))
        await writer.drain()

    async def _route(self, method, path, query, body):
        if path.startswith("/api/"):
            try:
                r = self.app.http_api(method, path[5:], query, body)
            except Exception as e:
                log.exception("API-fout")
                return 400, "text/plain; charset=utf-8", str(e).encode(), None
            if r is None:
                return 404, "text/plain", b"niet gevonden", None
            return r
        if method not in ("GET", "HEAD"):
            return 405, "text/plain", b"", None
        rel = urllib.parse.unquote(path).lstrip("/") or "index.html"
        full = os.path.abspath(os.path.join(self.root, rel))
        if not full.startswith(self.root + os.sep) or not os.path.isfile(full):
            return 404, "text/plain", b"niet gevonden", None
        with open(full, "rb") as fh:
            data = fh.read()
        ext = os.path.splitext(full)[1].lower()
        ctype = TYPES.get(ext) or mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        return 200, ctype, data, None

    async def _websocket(self, reader, writer, headers, peer):
        key = headers.get("sec-websocket-key", "")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        await writer.drain()
        client = Client(writer, peer)
        self.clients.add(client)
        log.info("webclient verbonden: %s", peer)
        try:
            self.app.on_connect(client)
            buf, op0 = b"", 0
            while not client.closed:
                b0, b1 = await reader.readexactly(2)
                op = b0 & 0x0F
                n = b1 & 0x7F
                if n == 126:
                    n = struct.unpack(">H", await reader.readexactly(2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", await reader.readexactly(8))[0]
                if n > MAX_BODY:
                    break
                mask = await reader.readexactly(4) if b1 & 0x80 else None
                payload = await reader.readexactly(n) if n else b""
                if mask:
                    payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload)) if n < 4096 else _unmask(payload, mask)
                if op == 8:  # close
                    writer.write(b"\x88\x00")
                    break
                if op == 9:  # ping → pong
                    writer.write(bytes([0x8A, len(payload)]) + payload if len(payload) < 126 else b"\x8a\x00")
                    continue
                if op in (1, 2, 0):
                    if op:
                        op0, buf = op, payload
                    else:
                        buf += payload
                    if b0 & 0x80 and op0 == 1:
                        try:
                            msg = json.loads(buf.decode())
                        except ValueError:
                            continue
                        try:
                            self.app.on_client(client, msg)
                        except Exception:
                            log.exception("fout bij verwerken van webbericht %s", str(msg)[:200])
                        buf = b""
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        finally:
            self.clients.discard(client)
            client.close()
            log.info("webclient weg: %s", peer)


def _unmask(payload, mask):
    n = len(payload)
    m = int.from_bytes((mask * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(payload, "big") ^ m).to_bytes(n, "big")
