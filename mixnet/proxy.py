"""A local proxy that speaks SOCKS5 and HTTP on the same port.

Every connection an app makes through it is sent out over one of your network
links, chosen by the engine. Many connections at once -> many links at once.
"""

from __future__ import annotations

import asyncio
import errno
import ipaddress
import socket
import struct
from urllib.parse import urlsplit

from .engine import Engine, Flow, Link

HEAD_LIMIT = 64 * 1024
CLIENT_TIMEOUT = 30.0
HALF_CLOSE_GRACE = 300.0
CHUNK = 64 * 1024

HOP_HEADERS = {"proxy-connection", "proxy-authorization", "connection", "keep-alive", "te", "upgrade"}


class ProxyServer:
    def __init__(self, engine: Engine, host: str, port: int, *, pac_text=None):
        self.engine = engine
        self.host = host
        self.port = port
        self.pac_text = pac_text  # callable returning the PAC file, served at /proxy.pac
        self._server: asyncio.base_events.Server | None = None
        self._clients: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._accept, self.host, self.port, limit=HEAD_LIMIT, reuse_address=True
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def rebind(self, host: str) -> None:
        """Move the listener (e.g. 127.0.0.1 <-> 0.0.0.0) without dropping open connections."""
        if self._server is not None:
            self._server.close()
            self._server = None
        try:
            self._server = await asyncio.start_server(
                self._accept, host, self.port, limit=HEAD_LIMIT, reuse_address=True
            )
        except OSError:
            # Put the old listener back so the proxy keeps working, then report.
            self._server = await asyncio.start_server(
                self._accept, self.host, self.port, limit=HEAD_LIMIT, reuse_address=True
            )
            raise
        self.host = host

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        for task in list(self._clients):
            task.cancel()
        await asyncio.gather(*self._clients, return_exceptions=True)

    # ------------------------------------------------------------------ #

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._clients.add(task)
        try:
            first = await asyncio.wait_for(reader.readexactly(1), CLIENT_TIMEOUT)
            if first == b"\x05":
                await self._socks5(reader, writer)
            else:
                await self._http(first, reader, writer)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError, OSError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:  # never let one bad client take the proxy down
            pass
        finally:
            self._clients.discard(task)
            _close(writer)

    # ------------------------------------------------------------------ #
    # SOCKS5 (RFC 1928), CONNECT only, no authentication
    # ------------------------------------------------------------------ #

    async def _socks5(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nmethods = (await reader.readexactly(1))[0]
        methods = await reader.readexactly(nmethods)
        if 0x00 not in methods:
            writer.write(b"\x05\xff")
            await writer.drain()
            return
        writer.write(b"\x05\x00")
        await writer.drain()

        ver, cmd, _rsv, atyp = await asyncio.wait_for(reader.readexactly(4), CLIENT_TIMEOUT)
        if ver != 5:
            return
        if atyp == 1:
            host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
        elif atyp == 3:
            length = (await reader.readexactly(1))[0]
            host = (await reader.readexactly(length)).decode("utf-8", "replace")
        elif atyp == 4:
            host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
        else:
            await _socks_reply(writer, 0x08)
            return
        (port,) = struct.unpack("!H", await reader.readexactly(2))
        if cmd != 1:  # BIND and UDP ASSOCIATE are not supported
            await _socks_reply(writer, 0x07)
            return

        flow = self.engine.new_flow(host, port)
        try:
            link, r_reader, r_writer = await self.engine.open_upstream(flow)
        except Exception as exc:
            self.engine.close_flow(flow, None, _describe(exc))
            await _socks_reply(writer, _socks_error_code(exc))
            return
        try:
            await _socks_reply(writer, 0x00, r_writer.get_extra_info("sockname"))
            await self._relay(flow, link, reader, writer, r_reader, r_writer)
        finally:
            self.engine.close_flow(flow, link, flow.error)

    # ------------------------------------------------------------------ #
    # HTTP proxy: CONNECT tunnels and plain http:// requests
    # ------------------------------------------------------------------ #

    async def _http(self, first: bytes, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = first + await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), CLIENT_TIMEOUT)
        except asyncio.LimitOverrunError:
            await _http_error(writer, 431, "Request header too large")
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            await _http_error(writer, 400, "Bad request")
            return

        if method.upper() == "CONNECT":
            host, port = _split_host_port(target, 443)
            if not host:
                await _http_error(writer, 400, "Bad CONNECT target")
                return
            flow = self.engine.new_flow(host, port)
            try:
                link, r_reader, r_writer = await self.engine.open_upstream(flow)
            except Exception as exc:
                self.engine.close_flow(flow, None, _describe(exc))
                await _http_error(writer, 502, f"MixNet couldn't reach {host}: {_describe(exc)}")
                return
            try:
                writer.write(b"HTTP/1.1 200 Connection Established\r\nProxy-Agent: MixNet\r\n\r\n")
                await writer.drain()
                await self._relay(flow, link, reader, writer, r_reader, r_writer)
            finally:
                self.engine.close_flow(flow, link, flow.error)
            return

        if target.startswith("/"):
            await self._local_page(target, writer)
            return
        url = urlsplit(target)
        if url.scheme != "http" or not url.hostname:
            await _http_error(writer, 400, "MixNet only forwards http:// URLs here; use CONNECT for HTTPS")
            return

        host = url.hostname
        port = url.port or 80
        path = url.path or "/"
        if url.query:
            path += "?" + url.query
        headers = [h for h in lines[1:] if h and h.split(":", 1)[0].strip().lower() not in HOP_HEADERS]
        if not any(h.lower().startswith("host:") for h in headers):
            headers.insert(0, f"Host: {url.netloc}")
        # One request per connection keeps the proxy simple and lets each new
        # request be balanced onto a different link.
        upstream_head = f"{method} {path} {version}\r\n" + "\r\n".join(headers + ["Connection: close"]) + "\r\n\r\n"

        flow = self.engine.new_flow(host, port)
        try:
            link, r_reader, r_writer = await self.engine.open_upstream(flow)
        except Exception as exc:
            self.engine.close_flow(flow, None, _describe(exc))
            await _http_error(writer, 502, f"MixNet couldn't reach {host}: {_describe(exc)}")
            return
        try:
            data = upstream_head.encode("latin-1")
            r_writer.write(data)
            flow.up += len(data)
            link.bytes_up += len(data)
            await r_writer.drain()
            await self._relay(flow, link, reader, writer, r_reader, r_writer)
        finally:
            self.engine.close_flow(flow, link, flow.error)

    async def _local_page(self, target: str, writer: asyncio.StreamWriter) -> None:
        if target.split("?", 1)[0] == "/proxy.pac" and self.pac_text:
            body = self.pac_text().encode()
            ctype = "application/x-ns-proxy-autoconfig"
        else:
            body = b"MixNet proxy is running. Point your apps at this address as an HTTP or SOCKS5 proxy.\n"
            ctype = "text/plain; charset=utf-8"
        writer.write(
            f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n".encode() + body
        )
        await writer.drain()

    # ------------------------------------------------------------------ #
    # moving bytes
    # ------------------------------------------------------------------ #

    async def _relay(
        self,
        flow: Flow,
        link: Link,
        c_reader: asyncio.StreamReader,
        c_writer: asyncio.StreamWriter,
        r_reader: asyncio.StreamReader,
        r_writer: asyncio.StreamWriter,
    ) -> None:
        async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter, upload: bool) -> bool:
            try:
                while True:
                    data = await src.read(CHUNK)
                    if not data:
                        break
                    n = len(data)
                    if upload:
                        flow.up += n
                        link.bytes_up += n
                    else:
                        flow.down += n
                        link.bytes_down += n
                    dst.write(data)
                    await dst.drain()
                if dst.can_write_eof():
                    dst.write_eof()
                return True
            except (ConnectionError, OSError, RuntimeError):
                return False

        up = asyncio.create_task(pump(c_reader, r_writer, True))
        down = asyncio.create_task(pump(r_reader, c_writer, False))
        try:
            done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            clean = all(t.result() for t in done)
            if pending and clean:
                # One side finished cleanly (half-close); give the other time to finish.
                _, pending = await asyncio.wait(pending, timeout=HALF_CLOSE_GRACE)
            if not clean:
                flow.error = flow.error or "connection reset"
        finally:
            for t in (up, down):
                t.cancel()
            await asyncio.gather(up, down, return_exceptions=True)
            _close(r_writer)


# ---------------------------------------------------------------------- #
# helpers
# ---------------------------------------------------------------------- #


def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
    except Exception:
        pass


def _split_host_port(target: str, default_port: int) -> tuple[str, int]:
    target = target.strip()
    if target.startswith("["):  # [IPv6]:port
        host, _, rest = target[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    elif target.count(":") == 1:
        host, port = target.split(":")
    else:
        host, port = target, ""
    try:
        return host, int(port) if port else default_port
    except ValueError:
        return "", 0


def _describe(exc: BaseException) -> str:
    if isinstance(exc, socket.gaierror):
        return "name not found (DNS)"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timed out"
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return str(exc) or exc.__class__.__name__


def _socks_error_code(exc: BaseException) -> int:
    if isinstance(exc, ConnectionRefusedError):
        return 0x05
    if isinstance(exc, socket.gaierror):
        return 0x04
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return 0x04
    if isinstance(exc, OSError) and exc.errno in (errno.ENETUNREACH, errno.EADDRNOTAVAIL):
        return 0x03
    if isinstance(exc, OSError) and exc.errno == errno.EHOSTUNREACH:
        return 0x04
    return 0x01


async def _socks_reply(writer: asyncio.StreamWriter, code: int, bound=None) -> None:
    addr, port = "0.0.0.0", 0
    if bound:
        addr, port = bound[0], bound[1]
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        ip = ipaddress.IPv4Address("0.0.0.0")
    atyp = 1 if ip.version == 4 else 4
    writer.write(bytes([5, code, 0, atyp]) + ip.packed + struct.pack("!H", port))
    await writer.drain()


async def _http_error(writer: asyncio.StreamWriter, status: int, message: str) -> None:
    reasons = {400: "Bad Request", 431: "Request Header Fields Too Large", 502: "Bad Gateway"}
    body = (message + "\n").encode()
    writer.write(
        f"HTTP/1.1 {status} {reasons.get(status, 'Error')}\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body
    )
    await writer.drain()
