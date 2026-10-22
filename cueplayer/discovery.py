"""mDNS / DNS-SD: players announce themselves, the master lists the ones it can see.

Service type _cueplayer._tcp.local. TXT records: role (player|master), name, version,
groups. Uses python-zeroconf, which coexists with avahi-daemon on Raspberry Pi OS.
All zeroconf work runs on its own threads, never on the asyncio loop.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("cueplayer.discovery")

SERVICE = "_cueplayer._tcp.local."

try:
    import ifaddr
    from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, ServiceStateChange, Zeroconf

    HAVE_ZEROCONF = True
except ImportError:  # pragma: no cover - optional dependency
    HAVE_ZEROCONF = False


def local_ipv4() -> List[str]:
    if not HAVE_ZEROCONF:
        return []
    out = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if isinstance(ip.ip, str) and not ip.ip.startswith(("127.", "169.254.")):
                out.append(ip.ip)
    return out


def best_addresses(addrs: List[str], mine: Optional[List[str]] = None) -> List[str]:
    """Order a unit's addresses by how likely this machine can reach them: same /24 as one
    of our own addresses first, then other private addresses, link-local and loopback last."""
    mine = local_ipv4() if mine is None else mine
    nets = {a.rsplit(".", 1)[0] for a in mine}

    def rank(a: str) -> int:
        if a.startswith("127."):
            return 4
        if a.startswith("169.254."):
            return 3
        return 0 if a.rsplit(".", 1)[0] in nets else 1
    return sorted(dict.fromkeys(addrs), key=rank)


def _instance(name: str) -> str:
    # DNS-SD instance names may contain spaces; dots would split the label
    return (name or socket.gethostname()).replace(".", "-")[:60]


class Discovery:
    """One zeroconf instance per process: advertises this unit, optionally browses."""

    def __init__(self, role: str, info: Callable[[], Dict[str, Any]], browse: bool = False,
                 on_change: Optional[Callable[[], None]] = None):
        self.role = role
        self.info = info            # -> {"name", "port", "groups", "version"}
        self.browse = browse
        self.on_change = on_change or (lambda: None)
        self.found: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._zc = None
        self._service = None
        self._advertised: Optional[tuple] = None
        self.error: Optional[str] = None

    # ---- lifecycle (call from any non-asyncio thread or via run_in_executor) ------
    def start(self) -> None:
        if not HAVE_ZEROCONF:
            self.error = "zeroconf not installed (pip install zeroconf)"
            log.warning("mDNS disabled: %s", self.error)
            return
        try:
            self._zc = Zeroconf(ip_version=IPVersion.V4Only)
            self.refresh()
            if self.browse:
                ServiceBrowser(self._zc, SERVICE, handlers=[self._on_state])
            log.info("mDNS: advertising %s as %s%s", SERVICE, self._advertised and self._advertised[0],
                     ", browsing for players" if self.browse else "")
        except Exception as e:  # never stop the app because discovery failed
            self.error = str(e)
            log.warning("mDNS unavailable: %s", e)

    def stop(self) -> None:
        if self._zc is None:
            return
        try:
            self._zc.unregister_all_services()
            self._zc.close()
        except Exception:
            pass
        self._zc = None

    def refresh(self) -> None:
        """(Re)advertise when the name, port, groups or addresses changed."""
        if self._zc is None:
            return
        i = self.info()
        addrs = local_ipv4()
        key = (i.get("name"), int(i.get("port") or 8080), tuple(i.get("groups") or []), tuple(addrs))
        if key == self._advertised:
            return
        props = {"role": self.role, "name": i.get("name") or "", "version": i.get("version") or "",
                 "groups": ",".join(i.get("groups") or [])}
        host = socket.gethostname().split(".")[0]
        svc = ServiceInfo(SERVICE, f"{_instance(i.get('name'))}.{SERVICE}",
                          addresses=[socket.inet_aton(a) for a in addrs], port=key[1],
                          properties=props, server=f"{host}.local.")
        try:
            if self._service is not None:
                self._zc.unregister_service(self._service)
            self._zc.register_service(svc, allow_name_change=True)
            self._service = svc
            self._advertised = key
        except Exception as e:
            self.error = str(e)
            log.warning("mDNS register failed: %s", e)

    # ---- browsing -----------------------------------------------------------------
    def _on_state(self, zeroconf, service_type, name, state_change) -> None:
        if state_change is ServiceStateChange.Removed:
            with self._lock:
                gone = self.found.pop(name, None)
            if gone:
                self.on_change()
            return
        # Resolve off the zeroconf thread (resolution waits for answers)
        threading.Thread(target=self._resolve, args=(name,), daemon=True).start()

    def _resolve(self, name: str) -> None:
        zc = self._zc
        if zc is None:
            return
        info = ServiceInfo(SERVICE, name)
        if not info.request(zc, 3000):
            return
        props = {k.decode(errors="ignore"): (v or b"").decode(errors="ignore")
                 for k, v in (info.properties or {}).items()}
        if props.get("role") != "player":
            return
        addrs = best_addresses([a for a in info.parsed_addresses() if ":" not in a])
        if not addrs or (self._service is not None and name == self._service.name):
            return
        entry = {"service": name, "name": props.get("name") or name.split(".")[0],
                 "addresses": addrs, "port": info.port, "version": props.get("version"),
                 "groups": [g for g in props.get("groups", "").split(",") if g],
                 "host": (info.server or "").rstrip("."), "url": f"http://{addrs[0]}:{info.port}",
                 "seen": time.time()}
        with self._lock:
            changed = self.found.get(name, {}).get("url") != entry["url"] or name not in self.found
            self.found[name] = entry
        if changed:
            self.on_change()

    def players(self) -> List[Dict[str, Any]]:
        with self._lock:
            return sorted(self.found.values(), key=lambda e: e["name"].lower())
