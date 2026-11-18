"""Wires the engine, proxy, dashboard and OS integration together."""

from __future__ import annotations

import asyncio
import atexit

from .config import Config
from .engine import Engine
from .proxy import ProxyServer
from .speedtest import SpeedTest
from .sysproxy import SystemProxy, pac_script
from .web import WebUI


class App:
    def __init__(self, config: Config, *, demo: bool = False, proxy_port: int | None = None, ui_port: int | None = None):
        self.config = config
        self.engine = Engine(config, discover=not demo)
        self.demo_driver = None
        if demo:
            from .demo import DemoDriver, DemoSpeedTest

            self.demo_driver = DemoDriver(self.engine)
            self.demo_driver.install()
            self.speedtest: SpeedTest = DemoSpeedTest(self.engine, self.demo_driver)
        else:
            self.speedtest = SpeedTest(self.engine)
        self.engine.speedtest = self.speedtest
        listen = "0.0.0.0" if config["lan_share"] else "127.0.0.1"
        self.proxy = ProxyServer(self.engine, listen, proxy_port or config["proxy_port"], pac_text=self.pac_text)
        self.web = WebUI(self, ui_port or config["ui_port"])
        self.sysproxy = SystemProxy(config, "", simulated=demo)
        self.proxy_error: str | None = None

    def pac_text(self) -> str:
        return pac_script("127.0.0.1", self.proxy.port)

    def lan_addresses(self) -> list[str]:
        return sorted({l.adapter.ipv4 for l in self.engine.links.values()
                       if l.present and l.adapter.ipv4 and not l.adapter.virtual})

    async def start(self) -> None:
        await self.engine.start()
        if self.demo_driver:
            await self.demo_driver.start()
        await self.proxy.start()
        await self.web.start()
        self.sysproxy.pac_url = f"http://127.0.0.1:{self.web.port}/proxy.pac"
        self.sysproxy.restore_stale()
        atexit.register(self._restore_system_proxy)

    async def stop(self) -> None:
        self._restore_system_proxy()
        await self.speedtest.stop()
        if self.demo_driver:
            await self.demo_driver.stop()
        await self.web.stop()
        await self.proxy.stop()
        await self.engine.stop()

    def _restore_system_proxy(self) -> None:
        try:
            self.sysproxy.disable()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # actions from the dashboard
    # ------------------------------------------------------------------ #

    async def update_settings(self, data: dict) -> None:
        self.engine.update_settings(mode=data.get("mode"), sticky=data.get("sticky"))
        if "lan_share" in data:
            share = bool(data["lan_share"])
            try:
                await self.proxy.rebind("0.0.0.0" if share else "127.0.0.1")
                self.proxy_error = None
            except OSError as exc:
                self.proxy_error = f"Couldn't change who can use the proxy: {exc.strerror or exc}"
                raise RuntimeError(self.proxy_error) from exc
            self.config["lan_share"] = share
            self.config.save()

    async def set_system_proxy(self, enabled: bool) -> None:
        loop = asyncio.get_running_loop()
        # OS tools can take a moment; keep the dashboard responsive meanwhile.
        await loop.run_in_executor(None, self.sysproxy.enable if enabled else self.sysproxy.disable)
        self.sysproxy.message = None
