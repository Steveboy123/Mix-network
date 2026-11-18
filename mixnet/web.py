"""A tiny local web server for the dashboard and its JSON API (stdlib only)."""

from __future__ import annotations

import asyncio
import json
import platform
from pathlib import Path
from typing import TYPE_CHECKING

from . import __version__, interfaces

if TYPE_CHECKING:
    from .app import App

STATIC = Path(__file__).with_name("static")
# Content types are fixed here: on some Windows installs the registry maps .js to
# text/plain, which browsers refuse to run when nosniff is set.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
MAX_BODY = 64 * 1024


class WebUI:
    def __init__(self, app: "App", port: int):
        self.app = app
        self.port = port
        self._server: asyncio.base_events.Server | None = None
        self._clients: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", self.port, reuse_address=True)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
        for t in list(self._clients):
            t.cancel()
        await asyncio.gather(*self._clients, return_exceptions=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    # ------------------------------------------------------------------ #

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._clients.add(task)
        try:
            while True:  # keep-alive
                try:
                    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 60)
                except (asyncio.IncompleteReadError, asyncio.TimeoutError, asyncio.LimitOverrunError):
                    break
                lines = head.decode("latin-1").split("\r\n")
                try:
                    method, target, _version = lines[0].split(" ", 2)
                except ValueError:
                    break
                headers = {}
                for line in lines[1:]:
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip().lower()] = v.strip()
                length = int(headers.get("content-length") or 0)
                if length > MAX_BODY:
                    await self._send(writer, 413, {"error": "too large"})
                    break
                body = await reader.readexactly(length) if length else b""
                status, payload, ctype = await self._route(method, target.split("?", 1)[0], headers, body)
                await self._send(writer, status, payload, ctype)
                if headers.get("connection", "").lower() == "close":
                    break
        except (ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        finally:
            self._clients.discard(task)
            writer.close()

    async def _send(self, writer, status: int, payload, ctype: str = "application/json") -> None:
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload, separators=(",", ":")).encode()
            ctype = "application/json"
        elif isinstance(payload, str):
            data = payload.encode()
        else:
            data = payload
        reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found", 409: "Conflict", 413: "Too Large", 500: "Error"}
        writer.write(
            (
                f"HTTP/1.1 {status} {reason.get(status, 'OK')}\r\nContent-Type: {ctype}\r\n"
                f"Content-Length: {len(data)}\r\nCache-Control: no-store\r\n"
                "X-Content-Type-Options: nosniff\r\nReferrer-Policy: no-referrer\r\n\r\n"
            ).encode()
            + data
        )
        await writer.drain()

    def _host_ok(self, headers: dict) -> bool:
        # Guard against DNS rebinding: only answer requests addressed to localhost.
        host = headers.get("host", "")
        allowed = {f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"[::1]:{self.port}"}
        return host in allowed

    async def _route(self, method: str, path: str, headers: dict, body: bytes):
        app = self.app
        if path == "/proxy.pac" and method == "GET":
            return 200, app.pac_text(), "application/x-ns-proxy-autoconfig"
        if not self._host_ok(headers):
            return 403, {"error": "forbidden host"}, "application/json"

        if method == "GET":
            if path in STATIC_FILES:
                name, ctype = STATIC_FILES[path]
                return 200, (STATIC / name).read_bytes(), ctype
            if path == "/api/state":
                return 200, self.state(), "application/json"
            return 404, {"error": "not found"}, "application/json"

        if method != "POST":
            return 404, {"error": "not found"}, "application/json"
        # A custom header can't be sent cross-site without a CORS preflight, which
        # this server never approves - so other websites can't change settings.
        if headers.get("x-mixnet") != "1":
            return 403, {"error": "missing X-MixNet header"}, "application/json"
        try:
            data = json.loads(body or b"{}")
            if not isinstance(data, dict):
                raise ValueError("expected an object")
        except ValueError as exc:
            return 400, {"error": f"bad JSON: {exc}"}, "application/json"

        try:
            if path == "/api/settings":
                await app.update_settings(data)
            elif path == "/api/link":
                app.engine.update_link(
                    str(data.get("name")),
                    enabled=data.get("enabled"),
                    weight=data.get("weight"),
                )
            elif path == "/api/speedtest":
                if not app.speedtest.start():
                    return 409, {"error": "A speed test is already running"}, "application/json"
            elif path == "/api/speedtest/apply":
                for name, weight in app.speedtest.suggested_weights().items():
                    if name in app.engine.links:
                        app.engine.update_link(name, weight=weight)
            elif path == "/api/sysproxy":
                await app.set_system_proxy(bool(data.get("enabled")))
            else:
                return 404, {"error": "not found"}, "application/json"
        except KeyError as exc:
            return 404, {"error": f"unknown connection {exc}"}, "application/json"
        except (ValueError, TypeError) as exc:
            return 400, {"error": str(exc)}, "application/json"
        except Exception as exc:
            return 500, {"error": str(exc) or exc.__class__.__name__}, "application/json"
        return 200, self.state(), "application/json"

    def state(self) -> dict:
        app = self.app
        snap = app.engine.snapshot()
        warnings = []
        msg = interfaces.binding_warning()
        if msg:
            warnings.append(msg)
        if snap["scan_error"]:
            warnings.append(f"Couldn't list network adapters: {snap['scan_error']}")
        if app.sysproxy.message:
            warnings.append(app.sysproxy.message)
        snap.update({
            "app": {
                "version": __version__,
                "platform": platform.system(),
                "demo": app.engine.demo,
                "warnings": warnings,
            },
            "proxy": {
                "host": app.proxy.host,
                "port": app.proxy.port,
                "lan_share": bool(app.config["lan_share"]),
                "lan_addresses": app.lan_addresses(),
                "error": app.proxy_error,
            },
            "speedtest": app.speedtest.snapshot(),
            "sysproxy": app.sysproxy.snapshot(),
        })
        return snap
