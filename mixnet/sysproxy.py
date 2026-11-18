"""Point the operating system's proxy setting at MixNet (and put it back).

MixNet installs a PAC file ("proxy auto-config") rather than a fixed proxy.
The PAC file says "use MixNet, and if it isn't answering, go DIRECT", so if
MixNet is closed or crashes your apps fall back to the normal connection
instead of losing internet.

The previous settings are saved in the config file before anything is
changed, so they can be restored on exit or on the next start after a crash.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess

from .config import Config

SYSTEM = platform.system()


def pac_script(proxy_host: str, proxy_port: int) -> str:
    target = f"PROXY {proxy_host}:{proxy_port}; SOCKS5 {proxy_host}:{proxy_port}; DIRECT"
    return f"""// MixNet proxy auto-config
function FindProxyForURL(url, host) {{
  if (isPlainHostName(host) || host === "localhost" || shExpMatch(host, "*.local") ||
      shExpMatch(host, "127.*") || shExpMatch(host, "10.*") || shExpMatch(host, "192.168.*") ||
      shExpMatch(host, "169.254.*") || /^172\\.(1[6-9]|2[0-9]|3[01])\\./.test(host) ||
      host === "::1" || shExpMatch(host, "[::1]")) {{
    return "DIRECT";
  }}
  return "{target}";
}}
"""


def _run(*args: str) -> str:
    proc = subprocess.run(list(args), capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or f"{args[0]} failed")
    return proc.stdout


class SystemProxy:
    def __init__(self, config: Config, pac_url: str, *, simulated: bool = False):
        self.config = config
        self.pac_url = pac_url
        self.simulated = simulated
        self._simulated_on = False
        self.message: str | None = None

    # ------------------------------------------------------------------ #

    def supported(self) -> tuple[bool, str]:
        if self.simulated:
            return True, ""
        if SYSTEM == "Windows":
            return True, ""
        if SYSTEM == "Darwin":
            return (shutil.which("networksetup") is not None, "networksetup was not found")
        if SYSTEM == "Linux":
            if shutil.which("gsettings") is None:
                return False, "Automatic setup works on GNOME-based desktops. Set the proxy in your desktop settings instead."
            if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
                return False, "MixNet is running with sudo, so it can't change your desktop's proxy setting. Set it manually."
            return True, ""
        return False, f"Not supported on {SYSTEM}"

    @property
    def enabled(self) -> bool:
        if self.simulated:
            return self._simulated_on
        return self.config["sysproxy_backup"] is not None

    def snapshot(self) -> dict:
        ok, why = self.supported()
        return {"supported": ok, "reason": why, "enabled": self.enabled, "message": self.message, "pac_url": self.pac_url}

    def enable(self) -> None:
        if self.enabled:
            return
        if self.simulated:
            self._simulated_on = True
            return
        ok, why = self.supported()
        if not ok:
            raise RuntimeError(why)
        backup = {"Windows": _win_get, "Darwin": _mac_get, "Linux": _gnome_get}[SYSTEM]()
        self.config["sysproxy_backup"] = {"system": SYSTEM, "settings": backup}
        self.config.save()
        try:
            {"Windows": _win_enable, "Darwin": _mac_enable, "Linux": _gnome_enable}[SYSTEM](self.pac_url)
        except Exception:
            self.disable()
            raise

    def disable(self) -> None:
        if self.simulated:
            self._simulated_on = False
            return
        saved = self.config["sysproxy_backup"]
        if not saved:
            return
        restore = {"Windows": _win_restore, "Darwin": _mac_restore, "Linux": _gnome_restore}.get(saved.get("system"))
        if restore is not None and saved.get("system") == SYSTEM:
            restore(saved.get("settings") or {})
        self.config["sysproxy_backup"] = None
        self.config.save()

    def restore_stale(self) -> bool:
        """If a previous run didn't get to clean up, put the old settings back."""
        if self.simulated or not self.config["sysproxy_backup"]:
            return False
        try:
            self.disable()
            return True
        except Exception as exc:
            self.message = f"Couldn't restore your previous proxy settings: {exc}"
            return False


# --------------------------------------------------------------------------- #
# Windows: WinINet per-connection options (what the Settings app changes)
# --------------------------------------------------------------------------- #

_WIN_FLAGS, _WIN_SERVER, _WIN_BYPASS, _WIN_PAC = 1, 2, 3, 4
_PROXY_TYPE_DIRECT, _PROXY_TYPE_AUTO_PROXY_URL = 0x1, 0x4


def _win_api():
    import ctypes
    from ctypes import wintypes

    class Value(ctypes.Union):
        _fields_ = [("dwValue", wintypes.DWORD), ("pszValue", wintypes.LPWSTR), ("ftValue", wintypes.FILETIME)]

    class Option(ctypes.Structure):
        _fields_ = [("dwOption", wintypes.DWORD), ("Value", Value)]

    class OptionList(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("pszConnection", wintypes.LPWSTR),
            ("dwOptionCount", wintypes.DWORD),
            ("dwOptionError", wintypes.DWORD),
            ("pOptions", ctypes.POINTER(Option)),
        ]

    wininet = ctypes.WinDLL("wininet", use_last_error=True)
    wininet.InternetQueryOptionW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    wininet.InternetSetOptionW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    return ctypes, Option, OptionList, wininet


def _win_get() -> dict:
    ctypes, Option, OptionList, wininet = _win_api()
    opts = (Option * 4)()
    for i, code in enumerate((_WIN_FLAGS, _WIN_SERVER, _WIN_BYPASS, _WIN_PAC)):
        opts[i].dwOption = code
    lst = OptionList(ctypes.sizeof(OptionList), None, 4, 0, opts)
    size = ctypes.c_ulong(ctypes.sizeof(lst))
    if not wininet.InternetQueryOptionW(None, 75, ctypes.byref(lst), ctypes.byref(size)):
        raise OSError(ctypes.get_last_error(), "Couldn't read Windows proxy settings")
    return {
        "flags": int(opts[0].Value.dwValue),
        "server": opts[1].Value.pszValue,
        "bypass": opts[2].Value.pszValue,
        "pac": opts[3].Value.pszValue,
    }


def _win_set(flags: int, server: str | None, bypass: str | None, pac: str | None) -> None:
    ctypes, Option, OptionList, wininet = _win_api()
    opts = (Option * 4)()
    opts[0].dwOption, opts[0].Value.dwValue = _WIN_FLAGS, flags
    opts[1].dwOption, opts[1].Value.pszValue = _WIN_SERVER, server
    opts[2].dwOption, opts[2].Value.pszValue = _WIN_BYPASS, bypass
    opts[3].dwOption, opts[3].Value.pszValue = _WIN_PAC, pac
    lst = OptionList(ctypes.sizeof(OptionList), None, 4, 0, opts)
    if not wininet.InternetSetOptionW(None, 75, ctypes.byref(lst), ctypes.sizeof(lst)):
        raise OSError(ctypes.get_last_error(), "Couldn't change Windows proxy settings")
    wininet.InternetSetOptionW(None, 39, None, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
    wininet.InternetSetOptionW(None, 37, None, 0)  # INTERNET_OPTION_REFRESH


def _win_enable(pac_url: str) -> None:
    current = _win_get()
    _win_set(_PROXY_TYPE_DIRECT | _PROXY_TYPE_AUTO_PROXY_URL, current["server"], current["bypass"], pac_url)


def _win_restore(saved: dict) -> None:
    _win_set(int(saved.get("flags", _PROXY_TYPE_DIRECT)), saved.get("server"), saved.get("bypass"), saved.get("pac"))


# --------------------------------------------------------------------------- #
# macOS: networksetup, for every enabled network service
# --------------------------------------------------------------------------- #


def _mac_services() -> list[str]:
    lines = _run("networksetup", "-listallnetworkservices").splitlines()[1:]
    return [l for l in lines if l.strip() and not l.startswith("*")]


def _mac_get() -> dict:
    saved = {}
    for svc in _mac_services():
        info = {}
        for line in _run("networksetup", "-getautoproxyurl", svc).splitlines():
            key, _, value = line.partition(":")
            info[key.strip().lower()] = value.strip()
        url = info.get("url", "")
        saved[svc] = {"url": "" if url in ("", "(null)") else url, "enabled": info.get("enabled", "No") == "Yes"}
    return saved


def _mac_enable(pac_url: str) -> None:
    for svc in _mac_services():
        _run("networksetup", "-setautoproxyurl", svc, pac_url)
        _run("networksetup", "-setautoproxystate", svc, "on")


def _mac_restore(saved: dict) -> None:
    for svc, info in saved.items():
        try:
            if info.get("url"):
                _run("networksetup", "-setautoproxyurl", svc, info["url"])
            _run("networksetup", "-setautoproxystate", svc, "on" if info.get("enabled") else "off")
        except (OSError, RuntimeError, subprocess.SubprocessError):
            continue  # the service may have been removed since


# --------------------------------------------------------------------------- #
# Linux (GNOME, Cinnamon, Budgie, ...): gsettings
# --------------------------------------------------------------------------- #

_SCHEMA = "org.gnome.system.proxy"


def _gnome_get() -> dict:
    return {
        "mode": _run("gsettings", "get", _SCHEMA, "mode").strip(),
        "autoconfig-url": _run("gsettings", "get", _SCHEMA, "autoconfig-url").strip(),
    }


def _gnome_enable(pac_url: str) -> None:
    _run("gsettings", "set", _SCHEMA, "autoconfig-url", pac_url)
    _run("gsettings", "set", _SCHEMA, "mode", "auto")


def _gnome_restore(saved: dict) -> None:
    # Values were saved in gsettings' own printed form, e.g. 'none'.
    if "autoconfig-url" in saved:
        _run("gsettings", "set", _SCHEMA, "autoconfig-url", saved["autoconfig-url"])
    _run("gsettings", "set", _SCHEMA, "mode", saved.get("mode", "'none'"))
