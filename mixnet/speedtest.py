"""Measure each link on its own, then all of them mixed together."""

from __future__ import annotations

import asyncio
import ssl
import time

from .dialer import dial
from .engine import Engine, Link

HOST = "speed.cloudflare.com"
PATH = "/__down?bytes=50000000"
WARMUP = 1.0  # seconds ignored at the start of each phase (TCP slow start)


class SpeedTest:
    def __init__(self, engine: Engine, *, seconds: float = 6.0, streams: int = 3):
        self.engine = engine
        self.seconds = seconds
        self.streams = streams
        self.running = False
        self.phase = ""
        self.progress = 0.0
        self.results: dict | None = None
        self.error: str | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> bool:
        if self.running:
            return False
        self.running = True
        self._task = asyncio.create_task(self._run())
        return True

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def snapshot(self) -> dict:
        return {
            "running": self.running,
            "phase": self.phase,
            "progress": round(self.progress, 3),
            "results": self.results,
            "error": self.error,
        }

    def suggested_weights(self) -> dict[str, int]:
        """Turn the last results into 1-10 weights proportional to measured speed."""
        per_link = (self.results or {}).get("links") or {}
        best = max(per_link.values(), default=0)
        if best <= 0:
            return {}
        return {name: max(1, min(10, round(10 * mbps / best))) for name, mbps in per_link.items() if mbps > 0}

    # ------------------------------------------------------------------ #

    async def _run(self) -> None:
        self.error = None
        self.progress = 0.0
        results: dict = {"links": {}, "mixed": None, "mixed_parts": {}, "finished": None}
        self.results = results
        try:
            links = [l for l in self.engine.links.values() if l.usable]
            if not links:
                raise RuntimeError("No connection is online to test.")
            phases = [(l.adapter.label, [l]) for l in links]
            if len(links) > 1:
                phases.append(("all connections mixed", links))
            for i, (label, group) in enumerate(phases):
                self.phase = f"Testing {label}…"
                measured = await self._measure(group, i, len(phases))
                if len(group) == 1:
                    results["links"][group[0].name] = round(measured[group[0].name], 1)
                else:
                    results["mixed"] = round(sum(measured.values()), 1)
                    results["mixed_parts"] = {k: round(v, 1) for k, v in measured.items()}
            if not any(results["links"].values()):
                raise RuntimeError(f"Couldn't download test data from {HOST}. Is it blocked on your network?")
        except ssl.SSLCertVerificationError:
            self.error = (
                "Python couldn't verify HTTPS certificates. On macOS, run "
                "'Install Certificates.command' from your Python folder, then try again."
            )
        except asyncio.CancelledError:
            self.error = "Cancelled"
            raise
        except Exception as exc:
            self.error = str(exc) or exc.__class__.__name__
        finally:
            results["finished"] = time.time()
            self.running = False
            self.phase = ""
            self.progress = 1.0

    async def _measure(self, group: list[Link], index: int, phases: int) -> dict[str, float]:
        loop = asyncio.get_running_loop()
        counts = {l.name: 0 for l in group}

        def on_bytes(link: Link, n: int) -> None:
            counts[link.name] += n

        start = loop.time()
        end = start + self.seconds
        tasks = [
            asyncio.create_task(self._stream(link, end, on_bytes))
            for link in group
            for _ in range(self.streams)
        ]
        try:
            base = dict(counts)
            t0 = loop.time()
            while loop.time() < end:
                await asyncio.sleep(0.2)
                if loop.time() - start >= WARMUP and t0 - start < WARMUP:
                    base, t0 = dict(counts), loop.time()
                self.progress = (index + min(1.0, (loop.time() - start) / self.seconds)) / phases
            elapsed = max(loop.time() - t0, 1e-3)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return {name: (counts[name] - base[name]) * 8 / elapsed / 1e6 for name in counts}

    async def _stream(self, link: Link, end: float, on_bytes) -> None:
        loop = asyncio.get_running_loop()
        ctx = ssl.create_default_context()
        request = (
            f"GET {PATH} HTTP/1.1\r\nHost: {HOST}\r\nUser-Agent: MixNet-speedtest\r\n"
            "Accept: */*\r\nConnection: close\r\n\r\n"
        ).encode()
        while loop.time() < end:
            try:
                reader, writer = await dial(link.adapter, HOST, 443, ssl=ctx, timeout=6.0)
            except ssl.SSLCertVerificationError:
                raise
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(0.5)
                continue
            link.active += 1
            try:
                writer.write(request)
                await writer.drain()
                head = await reader.readuntil(b"\r\n\r\n")
                if b" 200 " not in head.split(b"\r\n", 1)[0]:
                    await asyncio.sleep(0.5)
                    continue
                while True:
                    chunk = await reader.read(1 << 16)
                    if not chunk:
                        break
                    link.bytes_down += len(chunk)
                    on_bytes(link, len(chunk))
            except (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                pass
            finally:
                link.active -= 1
                writer.close()
