"""
Multi-machine registry: SSH targets + Wake-on-LAN.

A `MachineRegistry` is a JSON-backed mapping of nicknames to host info,
plus a per-(adapter,user) "active machine" pointer that other parts of
Alfred can read so they know which target to operate against.

  registry.add("prod", host="alice@prod.example.com", mac="AA:BB:CC:DD:EE:FF")
  registry.set_active(ctx, "prod")
  registry.wake("prod")        # broadcasts WoL magic packet to that MAC
"""
from __future__ import annotations

import json
import logging
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .runner import Context

logger = logging.getLogger("alfred.kernel.machines")


_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}([:\-]?[0-9A-Fa-f]{2}){5}$")


def normalise_mac(mac: str) -> str:
    """Return MAC as upper-case colon-separated form. Raises ValueError if invalid."""
    if not _MAC_RE.match(mac):
        raise ValueError(f"Not a valid MAC address: {mac!r}")
    cleaned = re.sub(r"[:\-]", "", mac).upper()
    return ":".join(cleaned[i:i + 2] for i in range(0, 12, 2))


def send_wol(mac: str, *, broadcast: str = "255.255.255.255", port: int = 9) -> None:
    """Broadcast a Wake-on-LAN magic packet to `mac`."""
    mac_norm = normalise_mac(mac)
    mac_bytes = bytes.fromhex(mac_norm.replace(":", ""))
    payload = b"\xff" * 6 + mac_bytes * 16
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(payload, (broadcast, port))
    finally:
        sock.close()


@dataclass
class MachineRegistry:
    state_path: Optional[Path] = None
    _machines: dict[str, dict] = field(default_factory=dict, init=False)
    _active: dict[str, str] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.state_path is None:
            self.state_path = Path(__file__).resolve().parent.parent / "alfred_machines.json"
        self._load()

    # -- Persistence -------------------------------------------------------
    def _load(self) -> None:
        if self.state_path and self.state_path.exists():
            try:
                data = json.loads(self.state_path.read_text())
                self._machines = data.get("machines", {})
                self._active = data.get("active", {})
            except Exception:
                self._machines, self._active = {}, {}

    def _save(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.write_text(json.dumps({
                "machines": self._machines,
                "active": self._active,
            }, indent=2))
        except Exception:
            logger.warning("Failed to persist machines registry")

    # -- Identity ----------------------------------------------------------
    @staticmethod
    def _user_key(ctx: Context) -> str:
        return f"{ctx.adapter.name}:{ctx.user.id}"

    # -- CRUD --------------------------------------------------------------
    def list_machines(self) -> dict[str, dict]:
        return dict(self._machines)

    def get(self, name: str) -> Optional[dict]:
        return self._machines.get(name)

    def add(self, name: str, *, host: str, mac: Optional[str] = None) -> dict:
        info: dict = {"host": host}
        if mac:
            info["mac"] = normalise_mac(mac)
        self._machines[name] = info
        self._save()
        return info

    def remove(self, name: str) -> bool:
        if name in self._machines:
            self._machines.pop(name)
            # Drop active selections that pointed here
            for k in [k for k, v in self._active.items() if v == name]:
                self._active.pop(k, None)
            self._save()
            return True
        return False

    # -- Active selection --------------------------------------------------
    def get_active(self, ctx: Context) -> str:
        return self._active.get(self._user_key(ctx), "local")

    def set_active(self, ctx: Context, name: str) -> bool:
        if name == "local":
            self._active.pop(self._user_key(ctx), None)
            self._save()
            return True
        if name not in self._machines:
            return False
        self._active[self._user_key(ctx)] = name
        self._save()
        return True

    # -- Wake-on-LAN -------------------------------------------------------
    def wake(self, name: str) -> str:
        info = self._machines.get(name)
        if not info:
            raise KeyError(f"Unknown machine: {name}")
        mac = info.get("mac")
        if not mac:
            raise ValueError(f"Machine {name!r} has no MAC address — re-add with /machine add {name} <host> <MAC>")
        send_wol(mac)
        return mac
