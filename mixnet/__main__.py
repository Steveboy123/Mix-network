"""Start MixNet:  python -m mixnet  [--demo] [--proxy-port 1080] [--ui-port 8899]"""

from __future__ import annotations

import argparse
import asyncio
import sys
import webbrowser

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mixnet",
        description="Combine Wi-Fi, Ethernet, USB tethering and other connections into one.",
    )
    parser.add_argument("--proxy-port", type=int, help="port for the HTTP/SOCKS5 proxy (default 1080)")
    parser.add_argument("--ui-port", type=int, help="port for the dashboard (default 8899)")
    parser.add_argument("--no-browser", action="store_true", help="don't open the dashboard automatically")
    parser.add_argument("--demo", action="store_true", help="simulated connections, to try the dashboard")
    parser.add_argument("--version", action="version", version=f"MixNet {__version__}")
    args = parser.parse_args(argv)

    if not args.demo:
        try:
            import psutil  # noqa: F401
        except ImportError:
            print("MixNet needs the 'psutil' package. Install it with:\n\n    python -m pip install psutil\n")
            return 1

    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"\nMixNet couldn't start: {exc}")
        if getattr(exc, "errno", None) in (48, 98, 10048):
            print("A port is already in use. Is MixNet already running? "
                  "Otherwise pick another port with --proxy-port / --ui-port.")
        return 1
    return 0


async def _run(args: argparse.Namespace) -> None:
    from .app import App
    from .config import Config

    app = App(Config(), demo=args.demo, proxy_port=args.proxy_port, ui_port=args.ui_port)
    await app.start()
    try:
        _banner(app)
        if not args.no_browser:
            webbrowser.open(app.web.url)
        await asyncio.Event().wait()  # run until Ctrl+C
    finally:
        await app.stop()
        print("\nMixNet stopped. Your system proxy settings were restored.")


def _banner(app) -> None:
    lines = [
        "",
        f"  MixNet {__version__}{'  (demo mode)' if app.engine.demo else ''}",
        "",
        f"  Dashboard : {app.web.url}",
        f"  Proxy     : 127.0.0.1:{app.proxy.port}  (HTTP and SOCKS5)",
        "",
    ]
    for link in app.engine.links.values():
        if link.present and not link.adapter.virtual:
            lines.append(f"  found     : {link.adapter.label}  {link.adapter.ipv4 or link.adapter.ipv6}")
    lines += ["", "  Press Ctrl+C to stop.", ""]
    print("\n".join(lines))
    sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
