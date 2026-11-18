"""Discover network adapters and pin outgoing sockets to a specific one.

Pinning is what makes mixing possible: every upstream connection the proxy
opens is forced out through one chosen adapter, so different connections can
travel over Wi-Fi, Ethernet, a tethered phone, and so on at the same time.

How a socket is pinned depends on the operating system:

* Linux   - SO_BINDTODEVICE (unprivileged since kernel 5.7; root before that).
            Falls back to binding the adapter's source address.
* macOS   - IP_BOUND_IF / IPV6_BOUND_IF with the adapter's index.
* Windows - bind to the adapter's source address. Windows uses the strong host
            model, so packets leave through the adapter that owns that address.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import platform
import re
import socket
import subprocess
from dataclasses import dataclass

try:
    import psutil
except ImportError:  # pragma: no cover - reported nicely by __main__
    psutil = None

SYSTEM = platform.system()  # "Windows", "Darwin" or "Linux"

SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)
IP_BOUND_IF = 25  # macOS
IPV6_BOUND_IF = 125  # macOS

# Kinds, in the order the "Backup" mode prefers them when weights tie.
KIND_RANK = {"ethernet": 6, "wifi": 5, "usb": 4, "cellular": 3, "bluetooth": 2, "other": 1, "vpn": 0, "virtual": 0}
KIND_LABEL = {
    "ethernet": "Ethernet",
    "wifi": "Wi-Fi",
    "usb": "USB / phone tether",
    "cellular": "Cellular",
    "bluetooth": "Bluetooth",
    "vpn": "VPN",
    "virtual": "Virtual",
    "other": "Network",
}


@dataclass
class Adapter:
    name: str  # OS name: "eth0", "en0", "Wi-Fi"
    label: str  # what the UI shows
    kind: str  # see KIND_RANK
    ipv4: str | None
    ipv6: str | None
    is_up: bool
    speed_mbps: int  # negotiated link speed, 0 when unknown
    index: int  # OS interface index, 0 when unknown
    virtual: bool

    @property
    def has_address(self) -> bool:
        return bool(self.ipv4 or self.ipv6)


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

_VIRTUAL_NAME = re.compile(
    r"^(lo\d*$|docker|veth|br-|virbr|vmnet|vboxnet|ifb|dummy|bond|team|cni|flannel|"
    r"kube|cali|vnet|gif|stf|awdl|llw|anpi|ap\d|bridge|p2p|ipsec|gre|sit|ip6tnl)",
    re.I,
)
_VPN_NAME = re.compile(r"^(tun|tap|wg|utun|ppp\d.*vpn|tailscale|zt|nordlynx|proton|mullvad|outline)", re.I)
_VIRTUAL_WORDS = (
    "vethernet", "virtualbox", "vmware", "hyper-v", "loopback", "pseudo-interface",
    "wsl", "npcap", "teredo", "isatap", "local area connection*", "docker",
)
_VPN_WORDS = (
    "tailscale", "zerotier", "wireguard", "openvpn", "tap-windows", "nordlynx",
    "proton", "mullvad", "vpn", "speedify", "cloudflare warp", "wintun",
)


def _classify_windows(name: str) -> tuple[str, bool]:
    low = name.lower()
    if any(w in low for w in _VPN_WORDS):
        return "vpn", True
    if any(w in low for w in _VIRTUAL_WORDS):
        return "virtual", True
    if "wi-fi" in low or "wifi" in low or "wlan" in low or "wireless" in low:
        return "wifi", False
    if "cellular" in low or "mobile" in low or "wwan" in low:
        return "cellular", False
    if "bluetooth" in low:
        return "bluetooth", False
    if "ethernet" in low or "local area connection" in low or "lan" in low:
        return "ethernet", False
    return "other", False


def _classify_linux(name: str) -> tuple[str, bool]:
    base = f"/sys/class/net/{name}"
    if _VPN_NAME.match(name):
        return "vpn", True
    if _VIRTUAL_NAME.match(name):
        return "virtual", True
    if os.path.exists(base + "/wireless") or os.path.exists(base + "/phy80211") or name.startswith("wl"):
        return "wifi", False
    if name.startswith(("ww", "rmnet", "ppp")):
        return "cellular", False
    if name.startswith(("usb", "rndis", "enx")):
        return "usb", False
    if os.path.isdir("/sys/class/net") and not os.path.exists(base + "/device"):
        return "virtual", True
    try:
        if "/usb" in os.path.realpath(base + "/device"):
            return "usb", False
    except OSError:
        pass
    if name.startswith(("en", "eth")):
        return "ethernet", False
    return "other", False


_mac_ports: dict[str, str] | None = None


def _mac_hardware_ports() -> dict[str, str]:
    """Map device ("en0") -> hardware port name ("Wi-Fi") using networksetup."""
    global _mac_ports
    if _mac_ports is None:
        _mac_ports = {}
        try:
            out = subprocess.run(
                ["networksetup", "-listallhardwareports"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            port = None
            for line in out.splitlines():
                if line.startswith("Hardware Port:"):
                    port = line.split(":", 1)[1].strip()
                elif line.startswith("Device:") and port:
                    _mac_ports[line.split(":", 1)[1].strip()] = port
        except (OSError, subprocess.SubprocessError):
            pass
    return _mac_ports


def _classify_mac(name: str) -> tuple[str, bool, str | None]:
    port = _mac_hardware_ports().get(name)
    if _VPN_NAME.match(name):
        return "vpn", True, port
    if port:
        low = port.lower()
        if "wi-fi" in low or "airport" in low:
            return "wifi", False, port
        if "iphone" in low or "ipad" in low or "android" in low or ("usb" in low and "lan" not in low):
            return "usb", False, port
        if "bluetooth" in low:
            return "bluetooth", False, port
        if "bridge" in low:
            return "virtual", True, port
        if "ethernet" in low or "lan" in low:
            return "ethernet", False, port
        return "other", False, port
    if _VIRTUAL_NAME.match(name):
        return "virtual", True, None
    if name.startswith("en"):
        return "ethernet", False, None
    return "other", False, None


def classify(name: str) -> tuple[str, bool, str]:
    """Return (kind, is_virtual, label) for an OS interface name."""
    if SYSTEM == "Windows":
        kind, virtual = _classify_windows(name)
        return kind, virtual, name
    if SYSTEM == "Darwin":
        kind, virtual, port = _classify_mac(name)
        return kind, virtual, f"{port or KIND_LABEL[kind]} ({name})"
    kind, virtual = _classify_linux(name)
    return kind, virtual, f"{KIND_LABEL[kind]} ({name})"


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


def _usable_ipv4(addr: str) -> bool:
    try:
        ip = ipaddress.IPv4Address(addr)
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast)


def _usable_ipv6(addr: str) -> bool:
    try:
        ip = ipaddress.IPv6Address(addr.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_global


def _if_index(name: str) -> int:
    if SYSTEM == "Windows":
        return 0
    try:
        return socket.if_nametoindex(name)
    except OSError:
        return 0


def scan() -> dict[str, Adapter]:
    """Return every adapter that currently has a usable IP address."""
    if psutil is None:
        raise RuntimeError("psutil is not installed (pip install psutil)")
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    found: dict[str, Adapter] = {}
    for name, entries in addrs.items():
        ipv4 = next((a.address for a in entries if a.family == socket.AF_INET and _usable_ipv4(a.address)), None)
        ipv6 = next(
            (a.address.split("%", 1)[0] for a in entries
             if a.family == socket.AF_INET6 and _usable_ipv6(a.address)),
            None,
        )
        st = stats.get(name)
        if st is not None and not st.isup:
            continue
        if not ipv4 and not ipv6:
            continue
        kind, virtual, label = classify(name)
        found[name] = Adapter(
            name=name,
            label=label,
            kind=kind,
            ipv4=ipv4,
            ipv6=ipv6,
            is_up=True,
            speed_mbps=int(st.speed) if st and st.speed and st.speed < 1_000_000 else 0,
            index=_if_index(name),
            virtual=virtual,
        )
    return found


def io_counters() -> dict[str, tuple[int, int]]:
    """Total bytes (received, sent) per adapter, including traffic not routed by MixNet."""
    if psutil is None:
        return {}
    try:
        return {k: (v.bytes_recv, v.bytes_sent) for k, v in psutil.net_io_counters(pernic=True).items()}
    except Exception:  # some platforms raise on adapters that vanish mid-call
        return {}


# --------------------------------------------------------------------------- #
# Pinning sockets to an adapter
# --------------------------------------------------------------------------- #

# Linux only: set to False after SO_BINDTODEVICE is refused for lack of privileges.
linux_device_binding: bool | None = None


def pin_socket(sock: socket.socket, adapter: Adapter, family: int) -> None:
    """Force `sock` to send through `adapter`. Raises OSError if impossible."""
    global linux_device_binding
    if SYSTEM == "Linux":
        if linux_device_binding is not False:
            try:
                sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, adapter.name.encode())
                linux_device_binding = True
                return
            except PermissionError:
                linux_device_binding = False
        _bind_source(sock, adapter, family)
    elif SYSTEM == "Darwin":
        if not adapter.index:
            raise OSError(errno.ENODEV, f"{adapter.name} has no interface index")
        if family == socket.AF_INET:
            sock.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, adapter.index)
        else:
            sock.setsockopt(socket.IPPROTO_IPV6, IPV6_BOUND_IF, adapter.index)
    else:
        _bind_source(sock, adapter, family)


def _bind_source(sock: socket.socket, adapter: Adapter, family: int) -> None:
    addr = adapter.ipv4 if family == socket.AF_INET else adapter.ipv6
    if not addr:
        raise OSError(errno.EADDRNOTAVAIL, f"{adapter.label} has no address for this destination")
    sock.bind((addr, 0))


def binding_warning() -> str | None:
    """A message for the UI when pinning can't be fully trusted on this machine."""
    if SYSTEM == "Linux" and linux_device_binding is False:
        return (
            "Linux refused to pin connections to an adapter (needs kernel 5.7+ or root), so MixNet "
            "is binding by source address instead. Run it with sudo, or add source-based routing "
            "rules, if traffic doesn't spread across your connections."
        )
    return None

