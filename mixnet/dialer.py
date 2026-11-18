"""Open TCP connections that are forced through one specific adapter."""

from __future__ import annotations

import asyncio
import errno
import socket
import ssl as ssl_mod

from .interfaces import Adapter, pin_socket


async def resolve(host: str, port: int, timeout: float = 8.0) -> list[tuple]:
    """Resolve once; IPv4 first (it is the most reliable on home networks)."""
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(
        loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP), timeout
    )
    infos.sort(key=lambda i: 0 if i[0] == socket.AF_INET else 1)
    return infos


async def dial(
    adapter: Adapter,
    host: str,
    port: int,
    *,
    timeout: float = 8.0,
    ssl: ssl_mod.SSLContext | None = None,
    addrinfo: list[tuple] | None = None,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to host:port through `adapter`. Raises OSError / TimeoutError."""
    loop = asyncio.get_running_loop()
    infos = addrinfo if addrinfo is not None else await resolve(host, port, timeout)
    families = set()
    if adapter.ipv4:
        families.add(socket.AF_INET)
    if adapter.ipv6:
        families.add(socket.AF_INET6)
    candidates = [i for i in infos if i[0] in families][:3]
    if not candidates:
        raise OSError(errno.EADDRNOTAVAIL, f"{adapter.label} can't reach {host} (no matching IP version)")

    last_error: BaseException | None = None
    for family, socktype, proto, _canon, sockaddr in candidates:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.setblocking(False)
            pin_socket(sock, adapter, family)
            await asyncio.wait_for(loop.sock_connect(sock, sockaddr), timeout)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return await asyncio.wait_for(
                asyncio.open_connection(
                    sock=sock, ssl=ssl, server_hostname=host if ssl else None, limit=1 << 18
                ),
                timeout,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            last_error = exc
            sock.close()
    assert last_error is not None
    raise last_error
