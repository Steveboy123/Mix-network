"""End-to-end tests: real sockets on the loopback adapter.

Run with:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mixnet.config import Config  # noqa: E402
from mixnet.engine import Engine  # noqa: E402
from mixnet.interfaces import Adapter  # noqa: E402
from mixnet.proxy import ProxyServer  # noqa: E402
from mixnet.sysproxy import pac_script  # noqa: E402

LOOPBACK = "lo" if sys.platform.startswith("linux") else ("lo0" if sys.platform == "darwin" else "Loopback")


def adapter(name: str, ipv4: str = "127.0.0.1", kind: str = "ethernet") -> Adapter:
    index = 0
    try:
        index = socket.if_nametoindex(name)
    except OSError:
        pass
    return Adapter(name, name, kind, ipv4, None, True, 1000, index, False)


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name) / "config.json")
        self.engine = Engine(self.config, discover=False)
        self.proxy = ProxyServer(self.engine, "127.0.0.1", 0)
        await self.proxy.start()
        self.echo = await asyncio.start_server(self._echo, "127.0.0.1", 0)
        self.echo_port = self.echo.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        await self.proxy.stop()
        self.echo.close()
        self.tmp.cleanup()

    async def _echo(self, reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()

    def add_link(self, name: str, *, weight: int = 5, status: str = "online", **kw):
        link = self.engine.add_adapter(adapter(name, **kw))
        link.weight = weight
        link.status = status
        return link

    async def open_proxy(self):
        return await asyncio.open_connection("127.0.0.1", self.proxy.port)

    async def socks5(self, host: str, port: int, atyp: int = 1):
        reader, writer = await self.open_proxy()
        writer.write(b"\x05\x01\x00")
        self.assertEqual(await reader.readexactly(2), b"\x05\x00")
        if atyp == 1:
            addr = socket.inet_aton(host)
        else:
            addr = bytes([len(host)]) + host.encode()
        writer.write(bytes([5, 1, 0, atyp]) + addr + struct.pack("!H", port))
        reply = await reader.readexactly(4)
        if reply[3] == 1:
            await reader.readexactly(6)
        elif reply[3] == 4:
            await reader.readexactly(18)
        return reply[1], reader, writer


@unittest.skipUnless(sys.platform.startswith("linux"), "loopback pinning tests run on Linux")
class ProxyTests(Base):
    async def test_socks5_ip_roundtrip_is_counted(self):
        link = self.add_link(LOOPBACK)
        code, reader, writer = await self.socks5("127.0.0.1", self.echo_port)
        self.assertEqual(code, 0)
        writer.write(b"hello mix")
        self.assertEqual(await reader.readexactly(9), b"hello mix")
        writer.close()
        await asyncio.sleep(0.05)
        self.assertEqual(link.bytes_up, 9)
        self.assertEqual(link.bytes_down, 9)
        self.assertEqual(link.total, 1)
        self.assertEqual(self.engine.flows[-1].link, LOOPBACK)

    async def test_socks5_domain_name(self):
        self.add_link(LOOPBACK)
        code, reader, writer = await self.socks5("localhost", self.echo_port, atyp=3)
        self.assertEqual(code, 0)
        writer.write(b"x" * 100_000)
        self.assertEqual(len(await reader.readexactly(100_000)), 100_000)
        writer.close()

    async def test_http_connect_tunnel(self):
        self.add_link(LOOPBACK)
        reader, writer = await self.open_proxy()
        writer.write(f"CONNECT 127.0.0.1:{self.echo_port} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        head = await reader.readuntil(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 200"))
        writer.write(b"ping")
        self.assertEqual(await reader.readexactly(4), b"ping")
        writer.close()

    async def test_plain_http_is_rewritten(self):
        self.add_link(LOOPBACK)
        seen = {}

        async def origin(reader, writer):
            seen["head"] = (await reader.readuntil(b"\r\n\r\n")).decode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(origin, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            reader, writer = await self.open_proxy()
            writer.write(
                f"GET http://127.0.0.1:{port}/path?q=1 HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                "Proxy-Connection: keep-alive\r\n\r\n".encode()
            )
            response = await asyncio.wait_for(reader.read(), 5)
            writer.close()
            self.assertTrue(response.startswith(b"HTTP/1.1 200 OK"))
            self.assertTrue(response.endswith(b"ok"))
            self.assertTrue(seen["head"].startswith("GET /path?q=1 HTTP/1.1"))
            self.assertIn("Connection: close", seen["head"])
            self.assertNotIn("Proxy-Connection", seen["head"])
        finally:
            server.close()

    async def test_fails_over_to_a_working_link(self):
        self.config["mode"] = "backup"
        broken = self.add_link("nolink0", weight=10)  # no such adapter: pinning fails
        good = self.add_link(LOOPBACK, weight=1)
        code, reader, writer = await self.socks5("127.0.0.1", self.echo_port)
        self.assertEqual(code, 0)
        writer.write(b"ok")
        self.assertEqual(await reader.readexactly(2), b"ok")
        writer.close()
        self.assertEqual(broken.failed_conns, 1)
        self.assertEqual(good.total, 1)

    async def test_reports_failure_when_nothing_works(self):
        self.add_link("nolink0")
        code, _reader, writer = await self.socks5("127.0.0.1", self.echo_port)
        self.assertNotEqual(code, 0)
        writer.close()
        self.assertIsNotNone(self.engine.flows[-1].error)

    async def test_refused_destination_is_not_retried(self):
        link = self.add_link(LOOPBACK)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]
        sock.close()
        code, _reader, writer = await self.socks5("127.0.0.1", closed_port)
        self.assertEqual(code, 0x05)
        self.assertEqual(link.failed_conns, 0)
        writer.close()


class BalancerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(Path(self.tmp.name) / "config.json")
        self.engine = Engine(self.config, discover=False)

    def tearDown(self):
        self.tmp.cleanup()

    def link(self, name, weight, kind="ethernet", latency=None, status="online"):
        l = self.engine.add_adapter(adapter(name, kind=kind))
        l.weight, l.latency_ms, l.status = weight, latency, status
        return l

    def test_speed_mode_follows_weights(self):
        a, b = self.link("a", 8), self.link("b", 2)
        for _ in range(100):
            self.engine.pick("example.com").active += 1
        self.assertEqual((a.active, b.active), (80, 20))

    def test_speed_mode_skips_offline_and_disabled(self):
        a = self.link("a", 5)
        self.link("b", 5, status="offline")
        c = self.link("c", 5)
        c.enabled = False
        for _ in range(10):
            self.assertIs(self.engine.pick(), a)

    def test_backup_mode_uses_heaviest_then_fails_over(self):
        self.config["mode"] = "backup"
        main, spare = self.link("main", 9), self.link("spare", 3)
        self.assertIs(self.engine.pick(), main)
        main.status = "offline"
        self.assertIs(self.engine.pick(), spare)

    def test_latency_mode_picks_lowest_ping(self):
        self.config["mode"] = "latency"
        self.link("slow", 9, latency=80.0)
        fast = self.link("fast", 1, latency=12.0)
        self.assertIs(self.engine.pick(), fast)

    def test_sticky_keeps_a_site_on_one_link(self):
        self.config["sticky"] = True
        self.link("a", 5)
        self.link("b", 5)
        first = self.engine.pick("bank.example")
        for _ in range(20):
            first.active += 1
            self.assertIs(self.engine.pick("bank.example"), first)

    def test_settings_persist(self):
        self.link("a", 5)
        self.engine.update_link("a", enabled=False, weight=12)
        self.engine.update_settings(mode="latency", sticky=True)
        saved = json.loads(self.config.path.read_text())
        self.assertEqual(saved["links"]["a"]["weight"], 10)
        self.assertFalse(saved["links"]["a"]["enabled"])
        self.assertEqual(saved["mode"], "latency")
        with self.assertRaises(ValueError):
            self.engine.update_settings(mode="turbo")


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_rejects_foreign_hosts_and_cross_site_posts(self):
        import os

        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MIXNET_HOME"] = tmp
            from mixnet.app import App

            app = App(Config(), demo=True, proxy_port=0, ui_port=0)
            await app.start()
            try:
                port = app.web.port

                async def request(raw: str) -> bytes:
                    r, w = await asyncio.open_connection("127.0.0.1", port)
                    w.write(raw.encode())
                    data = await asyncio.wait_for(r.read(), 5)
                    w.close()
                    return data

                ok = await request(f"GET /api/state HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n")
                self.assertIn(b"200 OK", ok.split(b"\r\n", 1)[0])
                rebinding = await request(f"GET /api/state HTTP/1.1\r\nHost: evil.example:{port}\r\nConnection: close\r\n\r\n")
                self.assertIn(b"403", rebinding.split(b"\r\n", 1)[0])
                body = '{"mode":"backup"}'
                csrf = await request(
                    f"POST /api/settings HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Length: {len(body)}\r\n"
                    f"Connection: close\r\n\r\n{body}"
                )
                self.assertIn(b"403", csrf.split(b"\r\n", 1)[0])
                good = await request(
                    f"POST /api/settings HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nX-MixNet: 1\r\n"
                    f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}"
                )
                self.assertIn(b'"mode":"backup"', good)
                pac = await request(f"GET /proxy.pac HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n")
                self.assertIn(f"PROXY 127.0.0.1:{app.proxy.port}".encode(), pac)
            finally:
                await app.stop()
                os.environ.pop("MIXNET_HOME", None)


class PacTests(unittest.TestCase):
    def test_pac_falls_back_to_direct(self):
        pac = pac_script("127.0.0.1", 1080)
        self.assertIn('return "PROXY 127.0.0.1:1080; SOCKS5 127.0.0.1:1080; DIRECT"', pac)
        self.assertIn('return "DIRECT"', pac)


if __name__ == "__main__":
    unittest.main()
