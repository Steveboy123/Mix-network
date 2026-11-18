"""Settings saved between runs in ~/.mixnet/config.json."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path

MODES = ("speed", "backup", "latency")

DEFAULTS: dict = {
    "mode": "speed",  # speed | backup | latency
    "sticky": False,  # keep each website on one connection
    "lan_share": False,  # let other devices on the LAN use the proxy
    "proxy_port": 1080,
    "ui_port": 8899,
    "links": {},  # adapter name -> {"enabled": bool, "weight": int, "color": int}
    "sysproxy_backup": None,  # previous OS proxy settings while MixNet controls them
}


def default_home() -> Path:
    return Path(os.environ.get("MIXNET_HOME") or Path.home() / ".mixnet")


class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or default_home() / "config.json"
        self.data: dict = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(saved, dict):
            for key, value in saved.items():
                if key in DEFAULTS:
                    self.data[key] = value
        if self.data.get("mode") not in MODES:
            self.data["mode"] = "speed"
        if not isinstance(self.data.get("links"), dict):
            self.data["links"] = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".config-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass  # settings are a convenience; never crash over them

    def __getitem__(self, key: str):
        return self.data[key]

    def __setitem__(self, key: str, value) -> None:
        self.data[key] = value

    def link(self, name: str) -> dict:
        return self.data["links"].setdefault(name, {})
