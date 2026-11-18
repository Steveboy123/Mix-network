"""Demo mode: simulated links and traffic so you can try the dashboard anywhere.

Run `python -m mixnet --demo`. Nothing touches your real network: three fake
links get synthetic traffic, and the traffic is still placed using the real
balancer, so the mode, on/off switches and weights in the UI behave as they
would for real.
"""

from __future__ import annotations

import asyncio
import random

from .engine import Engine, Link
from .interfaces import Adapter
from .speedtest import SpeedTest

DEMO_LINKS = [
    # adapter, capacity in Mbps, base latency in ms
    (Adapter("Ethernet", "Ethernet", "ethernet", "192.168.1.42", None, True, 1000, 0, False), 310.0, 9.0),
    (Adapter("Wi-Fi", "Wi-Fi", "wifi", "192.168.0.17", None, True, 866, 0, False), 140.0, 21.0),
    (Adapter("iPhone USB", "iPhone USB", "usb", "172.20.10.2", None, True, 0, 0, False), 48.0, 38.0),
    (Adapter("vEthernet (WSL)", "vEthernet (WSL)", "virtual", "172.28.80.1", None, True, 10000, 0, True), 0.0, 0.0),
]

HOSTS = [
    "www.youtube.com", "rr4---sn-ab5l6nrz.googlevideo.com", "github.com", "objects.githubusercontent.com",
    "steamcdn-a.akamaihd.net", "www.reddit.com", "i.redd.it", "fonts.gstatic.com", "api.spotify.com",
    "download.windowsupdate.com", "dl.google.com", "cdn.discordapp.com", "www.wikipedia.org",
    "upload.wikimedia.org", "news.ycombinator.com", "registry.npmjs.org", "pypi.org",
]


class _FakeFlow:
    __slots__ = ("flow", "link", "rate", "left")

    def __init__(self, flow, link: Link, rate: float, left: float):
        self.flow, self.link, self.rate, self.left = flow, link, rate, left


class DemoDriver:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.capacity: dict[str, float] = {}
        self.latency: dict[str, float] = {}
        self.flows: list[_FakeFlow] = []
        self._task: asyncio.Task | None = None

    def install(self) -> None:
        self.engine.demo = True
        for adapter, capacity, latency in DEMO_LINKS:
            link = self.engine.add_adapter(adapter)
            link.status = "online" if capacity else "offline"
            link.latency_ms = latency or None
            self.capacity[adapter.name] = capacity
            self.latency[adapter.name] = latency

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        tick = 0.2
        t = 0.0
        while True:
            await asyncio.sleep(tick)
            t += tick
            self._wobble_latency()
            # Demand comes in waves: browsing most of the time, a big download now and then.
            spawn = 1.2 + 1.0 * (1 + random.random()) * (0.5 + 0.5 * abs(((t / 25) % 2) - 1))
            if random.random() < spawn * tick:
                self._spawn(bulk=random.random() < 0.18)
            self._move_bytes(tick)

    def _wobble_latency(self) -> None:
        for name, base in self.latency.items():
            link = self.engine.links.get(name)
            if link and base and link.enabled:
                jitter = base * random.uniform(-0.15, 0.35)
                link.latency_ms = (link.latency_ms or base) * 0.8 + (base + jitter) * 0.2

    def _spawn(self, bulk: bool) -> None:
        host = random.choice(HOSTS)
        link = self.engine.pick(host)
        if link is None or not self.capacity.get(link.name):
            return
        flow = self.engine.new_flow(host, 443)
        flow.link = link.name
        link.active += 1
        link.total += 1
        if bulk:
            rate, left = 1e9, random.uniform(8, 22)  # as fast as the link allows
        else:
            rate, left = random.uniform(2, 25) * 1e6, random.uniform(0.6, 4)
        self.flows.append(_FakeFlow(flow, link, rate, left))

    def _move_bytes(self, tick: float) -> None:
        by_link: dict[str, list[_FakeFlow]] = {}
        for f in self.flows:
            by_link.setdefault(f.link.name, []).append(f)
        for name, flows in by_link.items():
            link = self.engine.links[name]
            cap = self.capacity[name] * 1e6 * random.uniform(0.82, 1.0) if link.enabled else 0
            want = sum(f.rate for f in flows)
            scale = min(1.0, cap / want) if want else 0
            for f in flows:
                down = int(f.rate * scale * tick / 8)
                up = int(down * random.uniform(0.02, 0.08))
                f.flow.down += down
                f.flow.up += up
                link.bytes_down += down
                link.bytes_up += up
                f.left -= tick
        alive = []
        for f in self.flows:
            if f.left > 0 and f.link.enabled:
                alive.append(f)
            else:
                self.engine.close_flow(f.flow, f.link)
        self.flows = alive


class DemoSpeedTest(SpeedTest):
    """Same flow as the real test, with simulated downloads at each link's capacity."""

    def __init__(self, engine: Engine, driver: DemoDriver):
        super().__init__(engine, seconds=4.0)
        self.driver = driver

    async def _stream(self, link: Link, end: float, on_bytes) -> None:
        loop = asyncio.get_running_loop()
        per_stream = self.driver.capacity.get(link.name, 0) * 1e6 / 8 / self.streams
        link.active += 1
        try:
            while loop.time() < end:
                await asyncio.sleep(0.1)
                n = int(per_stream * 0.1 * random.uniform(0.8, 1.02))
                link.bytes_down += n
                on_bytes(link, n)
        finally:
            link.active -= 1
