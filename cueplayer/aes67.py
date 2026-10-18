"""AES67 audio output helpers: settings validation, SDP generation and SAP announcements.

The GStreamer side (RTP payloader, PTP clock) lives in engine.py. This module has no
GStreamer dependency so it can be unit-tested anywhere.
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import threading
import time
import zlib
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("cueplayer.aes67")

SAMPLE_RATE = 48000
PTIMES_MS = (0.125, 0.25, 1, 4)   # AES67 packet times; 1 ms is the one every receiver must support
BIT_DEPTHS = (16, 24)             # L16 / L24
SAP_GROUP, SAP_PORT = "224.2.127.254", 9875
SAP_INTERVAL_S = 30
PAYLOAD_TYPE = 96
RTP_HEADER = 12


def iface_ip(iface: str) -> Optional[str]:
    """IPv4 address of a network interface by name (Linux), e.g. 'eth0'."""
    try:
        import fcntl
        import struct
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            req = struct.pack("256s", iface.encode()[:15])
            return socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, req)[20:24])  # SIOCGIFADDR
    except (ImportError, OSError):
        return None


def local_ip(dest: str = "239.69.0.1", iface: str = "") -> str:
    """The IPv4 address this host sends to `dest` from (no packet is sent)."""
    if iface:
        ip = iface_ip(iface)
        if ip:
            return ip
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((dest, 5004))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def default_multicast(ip: str) -> str:
    """239.69.c.d from the host's a.b.c.d, so each unit gets its own group by default."""
    try:
        octets = ipaddress.IPv4Address(ip).packed
    except ValueError:
        return "239.69.0.1"
    c, d = octets[2], octets[3]
    if c == 0 and d == 0:
        d = 1
    return f"239.69.{c}.{d}"


def clock_id_str(cid: Optional[int]) -> Optional[str]:
    """64-bit PTP clock identity -> 'AA-BB-CC-FF-FE-DD-EE-FF' as used in SDP."""
    if cid is None or cid <= 0 or cid >= 2 ** 64 - 1:
        return None
    return "-".join(f"{b:02X}" for b in int(cid).to_bytes(8, "big"))


def samples_per_packet(ptime_ms: float) -> int:
    return round(SAMPLE_RATE * ptime_ms / 1000)


def payload_bytes(channels: int, bit_depth: int, ptime_ms: float) -> int:
    return samples_per_packet(ptime_ms) * channels * (bit_depth // 8)


def validate(section: Dict[str, Any]) -> Dict[str, Any]:
    """Normalise an aes67 settings section. Raises ValueError listing every problem."""
    errors: List[str] = []
    out = dict(section)

    def num(key, cast, lo, hi, allowed=None):
        try:
            v = cast(out.get(key))
        except (TypeError, ValueError):
            errors.append(f"{key} must be a number")
            return
        if allowed is not None and v not in allowed:
            errors.append(f"{key} must be one of {', '.join(str(a) for a in allowed)}")
        elif not lo <= v <= hi:
            errors.append(f"{key} must be between {lo} and {hi}")
        out[key] = int(v) if cast is int or float(v).is_integer() else v

    out["enabled"] = bool(out.get("enabled"))
    out["sap"] = bool(out.get("sap", True))
    mc = str(out.get("multicast") or "").strip()
    if mc:
        try:
            addr = ipaddress.IPv4Address(mc)
            if not addr.is_multicast:
                errors.append("multicast must be an IPv4 multicast address (224.0.0.0 - 239.255.255.255)")
            elif mc.startswith("224.0.0.") or mc == SAP_GROUP:
                errors.append("multicast must not be a reserved address (224.0.0.x / the SAP group)")
        except ValueError:
            errors.append("multicast must be an IPv4 address, or blank for automatic")
    out["multicast"] = mc
    iface = str(out.get("interface") or "").strip()
    if iface:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", iface):
            errors.append("interface must be a network interface name such as eth0, or blank")
    out["interface"] = iface
    num("port", int, 1024, 65534)
    if isinstance(out.get("port"), int) and out["port"] % 2:
        errors.append("port must be even (RTP convention; the next port is RTCP)")
    num("channels", int, 1, 2)
    num("bit_depth", int, 16, 24, BIT_DEPTHS)
    num("ptime_ms", float, 0.125, 4, PTIMES_MS)
    num("ptp_domain", int, 0, 127)
    num("ttl", int, 1, 255)
    num("dscp", int, 0, 63)
    out["session_name"] = str(out.get("session_name") or "").strip()[:64]
    if errors:
        raise ValueError("aes67: " + "; ".join(errors))
    return out


def resolve(cfg: Dict[str, Any], screen_name: str = "CuePlayer") -> Dict[str, Any]:
    """Fill in the automatic values (source address, multicast group, session name)."""
    src = local_ip(cfg.get("multicast") or "239.69.0.1", cfg.get("interface", ""))
    r = dict(cfg)
    r["source_ip"] = src
    r["multicast"] = cfg.get("multicast") or default_multicast(src)
    r["session_name"] = cfg.get("session_name") or f"{screen_name} audio"
    return r


def build_sdp(cfg: Dict[str, Any], grandmaster: Optional[str], version: int = 0) -> str:
    """AES67 / SMPTE ST 2110-30 SDP for the stream described by a resolved config."""
    src, mc = cfg["source_ip"], cfg["multicast"]
    ch, depth, ptime = int(cfg["channels"]), int(cfg["bit_depth"]), cfg["ptime_ms"]
    domain = int(cfg["ptp_domain"])
    sess_id = zlib.crc32(f"{src}/{mc}:{cfg['port']}".encode()) & 0x7FFFFFFF
    name = cfg["session_name"].replace("\r", " ").replace("\n", " ")
    ptime_s = f"{ptime:g}"
    refclk = (f"ptp=IEEE1588-2008:{grandmaster}:{domain}" if grandmaster
              else "ptp=IEEE1588-2008:traceable")
    lines = [
        "v=0",
        f"o=- {sess_id} {version} IN IP4 {src}",
        f"s={name}",
        f"c=IN IP4 {mc}/{int(cfg['ttl'])}",
        "t=0 0",
        f"a=clock-domain:PTPv2 {domain}",
        f"m=audio {int(cfg['port'])} RTP/AVP {PAYLOAD_TYPE}",
        f"i={ch} channels: " + ("Left, Right" if ch == 2 else "Mono"),
        f"a=rtpmap:{PAYLOAD_TYPE} L{depth}/{SAMPLE_RATE}/{ch}",
        f"a=source-filter: incl IN IP4 {mc} {src}",
        "a=recvonly",
        f"a=ptime:{ptime_s}",
        f"a=maxptime:{ptime_s}",
        f"a=framecount:{samples_per_packet(ptime)}",
        f"a=ts-refclk:{refclk}",
        "a=mediaclk:direct=0",
    ]
    return "\r\n".join(lines) + "\r\n"


def sap_packet(sdp: str, source_ip: str, delete: bool = False) -> bytes:
    """RFC 2974 SAP announcement (or deletion) carrying an SDP."""
    flags = 0x20 | (0x04 if delete else 0)            # V=1, IPv4, T=announce/delete
    msg_hash = zlib.crc32(sdp.encode()) & 0xFFFF
    head = bytes([flags, 0]) + msg_hash.to_bytes(2, "big") + socket.inet_aton(source_ip)
    return head + b"application/sdp\x00" + sdp.encode()


class SapAnnouncer:
    """Sends the SDP to the SAP group every 30 s, and a deletion when stopped."""

    def __init__(self, sdp_fn: Callable[[], Optional[str]], source_ip: str, ttl: int = 32):
        self.sdp_fn = sdp_fn
        self.source_ip = source_ip
        self.ttl = ttl
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sap", daemon=True)
        self._last: Optional[str] = None

    def start(self) -> "SapAnnouncer":
        self._thread.start()
        return self

    def _socket(self) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, self.ttl)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.source_ip))
        except OSError:
            pass
        return s

    def _send(self, sdp: str, delete: bool = False) -> None:
        try:
            with self._socket() as s:
                s.sendto(sap_packet(sdp, self.source_ip, delete), (SAP_GROUP, SAP_PORT))
        except OSError as e:
            log.warning("SAP send failed: %s", e)

    def _run(self) -> None:
        while not self._stop.is_set():
            sdp = self.sdp_fn()
            if sdp:
                if self._last and self._last != sdp:
                    self._send(self._last, delete=True)   # SDP changed: withdraw the old one
                self._send(sdp)
                self._last = sdp
            self._stop.wait(SAP_INTERVAL_S if sdp else 2)

    def stop(self) -> None:
        self._stop.set()
        if self._last:
            self._send(self._last, delete=True)
            self._last = None


def status_template(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {"state": "disabled", "synced": False, "grandmaster": None, "domain": cfg.get("ptp_domain", 0),
            "multicast": None, "port": cfg.get("port"), "sdp_version": int(time.time())}
