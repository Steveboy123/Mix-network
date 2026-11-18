"""The mixer: watches every link, picks one for each new connection, keeps stats."""

from __future__ import annotations

import asyncio
import errno
import random
import time
from collections import deque
from dataclasses import dataclass
from itertools import count

from . import interfaces
from .config import MODES, Config
from .dialer import dial, resolve
from .interfaces import KIND_LABEL, KIND_RANK, Adapter

HISTORY = 90  # seconds of rate history kept for the charts
PROBE_TARGETS = (("1.1.1.1", 443), ("8.8.8.8", 443), ("9.9.9.9", 443))
PROBE_INTERVAL = 5.0  # seconds between health checks per link
RETRY_AFTER_FAIL = 1.5  # re-check a link this soon after a failed check
SCAN_INTERVAL = 3.0  # seconds between adapter rescans (plug / unplug detection)
CONNECT_TIMEOUT = 8.0
STICKY_SECONDS = 600
PALETTE_SIZE = 8
DEFAULT_WEIGHT = 5


class Link:
    """One adapter plus everything MixNet knows about it at runtime."""

    def __init__(self, adapter: Adapter, *, enabled: bool, weight: int, color: int, order: int):
        self.adapter = adapter
        self.enabled = enabled
        self.weight = weight
        self.color = color
        self.order = order
        self.present = True
        self.status = "checking"  # checking | online | offline
        self.latency_ms: float | None = None
        self.fails = 0
        self.next_probe = 0.0
        self.probe_task: asyncio.Task | None = None
        self.active = 0
        self.total = 0
        self.failed_conns = 0
        self.bytes_down = 0
        self.bytes_up = 0
        self._sampled = (0, 0)
        self.rate_down = 0.0  # bits per second through MixNet
        self.rate_up = 0.0
        self.os_rate_down: float | None = None  # bits per second on the whole adapter
        self.os_rate_up: float | None = None
        self.hist_down: deque[float] = deque([0.0] * HISTORY, maxlen=HISTORY)
        self.hist_up: deque[float] = deque([0.0] * HISTORY, maxlen=HISTORY)

    @property
    def name(self) -> str:
        return self.adapter.name

    @property
    def usable(self) -> bool:
        return self.present and self.enabled and self.status != "offline"


@dataclass
class Flow:
    """One proxied connection, kept for the live activity list."""

    id: int
    host: str
    port: int
    started: float
    link: str | None = None
    up: int = 0
    down: int = 0
    closed: float | None = None
    error: str | None = None


class Engine:
    def __init__(self, config: Config, *, discover: bool = True):
        self.config = config
        self.discover = discover
        self.links: dict[str, Link] = {}
        self.flows: deque[Flow] = deque(maxlen=200)
        self._flow_ids = count(1)
        self._sticky: dict[str, tuple[str, float]] = {}
        self._tasks: list[asyncio.Task] = []
        self.scan_error: str | None = None
        self.demo = False
        self.speedtest = None  # set by the app

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if self.discover:
            self.rescan()
            self._tasks.append(asyncio.create_task(self._scan_loop()))
            self._tasks.append(asyncio.create_task(self._probe_loop()))
        self._tasks.append(asyncio.create_task(self._sample_loop()))

    async def stop(self) -> None:
        tasks = list(self._tasks) + [l.probe_task for l in self.links.values() if l.probe_task]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    # ------------------------------------------------------------------ #
    # adapters
    # ------------------------------------------------------------------ #

    def add_adapter(self, adapter: Adapter) -> Link:
        saved = self.config.link(adapter.name)
        used = {l.color for l in self.links.values() if l.present}
        color = saved.get("color")
        if not isinstance(color, int) or not 0 <= color < PALETTE_SIZE or color in used:
            free = [c for c in range(PALETTE_SIZE) if c not in used]
            color = free[0] if free else len(self.links) % PALETTE_SIZE
        weight = saved.get("weight", DEFAULT_WEIGHT)
        link = Link(
            adapter,
            enabled=bool(saved.get("enabled", not adapter.virtual)),
            weight=max(1, min(10, int(weight))) if isinstance(weight, (int, float)) else DEFAULT_WEIGHT,
            color=color,
            order=len(self.links),
        )
        self.links[adapter.name] = link
        saved["color"] = color
        self.config.save()
        return link

    def rescan(self) -> None:
        try:
            found = interfaces.scan()
            self.scan_error = None
        except Exception as exc:  # psutil missing or OS hiccup
            self.scan_error = str(exc)
            return
        for name, adapter in found.items():
            link = self.links.get(name)
            if link is None:
                self.add_adapter(adapter)
                continue
            changed = (link.adapter.ipv4, link.adapter.ipv6) != (adapter.ipv4, adapter.ipv6)
            link.adapter = adapter
            if not link.present or changed:
                link.present = True
                link.status = "checking"
                link.fails = 0
                link.next_probe = 0.0
        for name, link in self.links.items():
            if name not in found and link.present:
                link.present = False
                link.status = "offline"
                link.latency_ms = None

    async def _scan_loop(self) -> None:
        while True:
            await asyncio.sleep(SCAN_INTERVAL)
            self.rescan()

    # ------------------------------------------------------------------ #
    # health checks
    # ------------------------------------------------------------------ #

    def recheck(self, link: Link) -> None:
        """Ask for a health check as soon as possible (e.g. after a failed connection)."""
        link.next_probe = 0.0

    async def _probe_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            now = loop.time()
            for link in list(self.links.values()):
                busy = link.probe_task is not None and not link.probe_task.done()
                wanted = link.enabled or not link.adapter.virtual
                if link.present and wanted and not busy and now >= link.next_probe:
                    link.probe_task = asyncio.create_task(self.probe(link))
            await asyncio.sleep(0.5)

    async def probe(self, link: Link) -> bool:
        loop = asyncio.get_running_loop()
        link.next_probe = loop.time() + PROBE_INTERVAL
        for host, port in PROBE_TARGETS:
            t0 = loop.time()
            try:
                _, writer = await dial(link.adapter, host, port, timeout=4.0)
            except (OSError, asyncio.TimeoutError):
                continue
            ms = (loop.time() - t0) * 1000
            writer.close()
            link.latency_ms = ms if link.latency_ms is None else link.latency_ms * 0.6 + ms * 0.4
            link.status = "online"
            link.fails = 0
            return True
        link.fails += 1
        link.next_probe = loop.time() + RETRY_AFTER_FAIL
        if link.fails >= 2 or link.status == "checking":
            link.status = "offline"
            link.latency_ms = None
        return False

    # ------------------------------------------------------------------ #
    # choosing a link for a new connection
    # ------------------------------------------------------------------ #

    def pick(self, host: str | None = None, exclude: tuple | list = ()) -> Link | None:
        candidates = [l for l in self.links.values() if l.name not in exclude]
        pool = [l for l in candidates if l.usable]
        if not pool:
            # Health checks can be blocked by a firewall: still try whatever is plugged in.
            pool = [l for l in candidates if l.present and l.enabled]
        if not pool:
            return None

        sticky = bool(self.config["sticky"]) and bool(host)
        now = time.monotonic()
        if sticky:
            hit = self._sticky.get(host)
            if hit and hit[1] > now:
                for link in pool:
                    if link.name == hit[0]:
                        return link

        mode = self.config["mode"]
        if mode == "backup":
            # The heaviest-weighted link carries everything; the rest wait as backups.
            choice = max(pool, key=lambda l: (l.weight, KIND_RANK.get(l.adapter.kind, 0), -l.order))
        elif mode == "latency":
            choice = min(
                pool,
                key=lambda l: (l.latency_ms if l.latency_ms is not None else 1e9, -l.weight, l.order),
            )
        else:
            # Weighted least-connections: a link with weight 8 carries ~4x the
            # connections of a link with weight 2, and a slow link that is still
            # busy with old transfers gets fewer new ones.
            choice = min(pool, key=lambda l: ((l.active + 1) / max(1, l.weight), random.random()))

        if sticky:
            self._sticky[host] = (choice.name, now + STICKY_SECONDS)
            if len(self._sticky) > 5000:
                self._sticky = {k: v for k, v in self._sticky.items() if v[1] > now}
        return choice

    def new_flow(self, host: str, port: int) -> Flow:
        flow = Flow(id=next(self._flow_ids), host=host, port=port, started=time.time())
        self.flows.append(flow)
        return flow

    async def open_upstream(self, flow: Flow):
        """Connect to flow.host:flow.port over the best link, failing over to others."""
        infos = await resolve(flow.host, flow.port)
        tried: list[str] = []
        last_error: BaseException | None = None
        for _ in range(4):
            link = self.pick(flow.host, exclude=tried)
            if link is None:
                break
            tried.append(link.name)
            try:
                reader, writer = await dial(
                    link.adapter, flow.host, flow.port, timeout=CONNECT_TIMEOUT, addrinfo=infos
                )
            except ConnectionRefusedError:
                raise  # the website said no; another link won't change that
            except (OSError, asyncio.TimeoutError) as exc:
                last_error = exc
                link.failed_conns += 1
                self.recheck(link)
                continue
            flow.link = link.name
            link.active += 1
            link.total += 1
            return link, reader, writer
        if last_error is not None:
            raise last_error
        raise OSError(errno.ENETUNREACH, "No network connection is available")

    def close_flow(self, flow: Flow, link: Link | None, error: str | None = None) -> None:
        flow.closed = time.time()
        flow.error = error
        if link is not None:
            link.active = max(0, link.active - 1)

    # ------------------------------------------------------------------ #
    # stats
    # ------------------------------------------------------------------ #

    async def _sample_loop(self) -> None:
        loop = asyncio.get_running_loop()
        last = loop.time()
        os_prev = interfaces.io_counters() if self.discover else {}
        while True:
            await asyncio.sleep(1.0)
            now = loop.time()
            dt = max(now - last, 1e-3)
            last = now
            os_now = interfaces.io_counters() if self.discover else {}
            for link in list(self.links.values()):
                d = link.bytes_down - link._sampled[0]
                u = link.bytes_up - link._sampled[1]
                link._sampled = (link.bytes_down, link.bytes_up)
                link.rate_down = d * 8 / dt
                link.rate_up = u * 8 / dt
                link.hist_down.append(link.rate_down)
                link.hist_up.append(link.rate_up)
                a, b = os_now.get(link.name), os_prev.get(link.name)
                if a and b:
                    link.os_rate_down = max(0, a[0] - b[0]) * 8 / dt
                    link.os_rate_up = max(0, a[1] - b[1]) * 8 / dt
            os_prev = os_now

    # ------------------------------------------------------------------ #
    # settings
    # ------------------------------------------------------------------ #

    def update_settings(self, *, mode: str | None = None, sticky: bool | None = None) -> None:
        if mode is not None:
            if mode not in MODES:
                raise ValueError(f"unknown mode {mode!r}")
            self.config["mode"] = mode
        if sticky is not None:
            self.config["sticky"] = bool(sticky)
            self._sticky.clear()
        self.config.save()

    def update_link(self, name: str, *, enabled: bool | None = None, weight: int | None = None) -> None:
        link = self.links.get(name)
        if link is None:
            raise KeyError(name)
        saved = self.config.link(name)
        if enabled is not None:
            link.enabled = bool(enabled)
            saved["enabled"] = link.enabled
            if link.enabled:
                self.recheck(link)
        if weight is not None:
            link.weight = max(1, min(10, int(weight)))
            saved["weight"] = link.weight
        self.config.save()

    # ------------------------------------------------------------------ #
    # snapshot for the UI
    # ------------------------------------------------------------------ #

    def snapshot(self) -> dict:
        links = sorted(self.links.values(), key=lambda l: (l.adapter.virtual, not l.present, l.order))
        recent = {l.name: sum(list(l.hist_down)[-10:]) + sum(list(l.hist_up)[-10:]) for l in links}
        recent_total = sum(recent.values())
        now = time.time()
        out = []
        for l in links:
            a = l.adapter
            out.append({
                "name": l.name,
                "label": a.label,
                "kind": a.kind,
                "kind_label": KIND_LABEL.get(a.kind, "Network"),
                "virtual": a.virtual,
                "ipv4": a.ipv4,
                "ipv6": a.ipv6,
                "speed_mbps": a.speed_mbps,
                "present": l.present,
                "enabled": l.enabled,
                "usable": l.usable,
                "weight": l.weight,
                "color": l.color,
                "status": l.status if l.present else "disconnected",
                "latency_ms": round(l.latency_ms, 1) if l.latency_ms is not None else None,
                "active": l.active,
                "total": l.total,
                "failed": l.failed_conns,
                "bytes_down": l.bytes_down,
                "bytes_up": l.bytes_up,
                "rate_down": round(l.rate_down),
                "rate_up": round(l.rate_up),
                "os_rate_down": None if l.os_rate_down is None else round(l.os_rate_down),
                "os_rate_up": None if l.os_rate_up is None else round(l.os_rate_up),
                "share": recent[l.name] / recent_total if recent_total else 0.0,
                "hist_down": [round(v) for v in l.hist_down],
                "hist_up": [round(v) for v in l.hist_up],
            })
        flows = []
        for f in list(self.flows)[-14:][::-1]:
            flows.append({
                "id": f.id,
                "host": f.host,
                "port": f.port,
                "link": f.link,
                "down": f.down,
                "up": f.up,
                "open": f.closed is None,
                "age": round((f.closed or now) - f.started, 1),
                "ago": round(now - (f.closed or now), 1),
                "error": f.error,
            })
        return {
            "mode": self.config["mode"],
            "sticky": bool(self.config["sticky"]),
            "links": out,
            "flows": flows,
            "totals": {
                "rate_down": round(sum(l.rate_down for l in links)),
                "rate_up": round(sum(l.rate_up for l in links)),
                "active": sum(l.active for l in links),
                "bytes_down": sum(l.bytes_down for l in links),
                "bytes_up": sum(l.bytes_up for l in links),
                "usable": sum(1 for l in links if l.usable and l.status == "online"),
                "flows_total": self.flows[-1].id if self.flows else 0,
            },
            "scan_error": self.scan_error,
        }
