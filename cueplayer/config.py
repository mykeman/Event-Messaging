"""Persistent settings, stored as JSON in the data directory."""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Set

DEFAULTS: Dict[str, Any] = {
    "output": {
        # The content raster. For an LED wall this is the processor's pixel map
        # (e.g. 1152x576). Everything is composed at this size, and NDI carries it.
        "canvas_width": 1920,
        "canvas_height": 1080,
        "framerate": 30,  # 25/30 recommended on Raspberry Pi
        "background": "#000000",
        # How media is fitted into the canvas: fit | fill | stretch | none
        "scale_mode": "fit",
        # Prefer hardware video decoders (Pi V4L2, VA-API, NVIDIA) when available
        "hw_decode": True,
        # Processing time allowed per frame before it is dropped as late (ms). Raise it
        # if the Live tab shows dropped frames; it adds the same delay to every output.
        "latency_ms": 100,
        # Pre-roll: a new item decodes and buffers this long, hidden, before it replaces
        # what's on screen. Raise if a slower unit still shows a glitch on changes.
        "preroll_ms": 300,
    },
    "display": {
        # Physical output (HDMI). The canvas is placed into the display raster at
        # (x, y), optionally rescaled to (out_width x out_height).
        "enabled": True,
        # auto | kms | gl | x | wayland | fb | none
        "sink": "auto",
        "width": 1920,
        "height": 1080,
        "x": 0,
        "y": 0,
        "out_width": 0,   # 0 = same as canvas
        "out_height": 0,
        # kmssink only: DRM connector id (0 = auto: the port with a display plugged in)
        "connector_id": 0,
        # HDMI refresh rate in Hz (0 = auto: the screen's preferred rate, usually 60).
        # width x height @ refresh is also written to the kernel command line, so the port
        # comes up in this mode at boot (needs the system helper, see deploy/update.sh).
        # A changed refresh rate takes effect after a reboot.
        "refresh": 0,
        # kmssink only. auto: kmssink sets the mode (the first one at width x height,
        # usually 60 Hz) - unless the boot mode matches width x height @ refresh, which is
        # then reused. keep: always reuse the mode set at boot.
        "modeset": "auto",
    },
    "ndi": {
        "enabled": True,
        "name": "CuePlayer",
    },
    "audio": {
        "enabled": False,
        "sink": "auto",   # auto | alsa | pulse | none
        "device": "",     # alsa device; blank = the HDMI port the video is on (Pi: hdmi:CARD=vc4hdmiN)
    },
    "aes67": {
        # Native AES67 / SMPTE 2110-30 audio output (RTP multicast, PTP-locked). Needs audio
        # enabled. Off by default; untested on real AES67 hardware - see README.
        "enabled": False,
        # aes67 | st2110-30 (ST 2110 receivers: PTP domain 127 by default, SDP channel-order)
        "profile": "aes67",
        "payload_type": 96,     # RTP payload type, 96-127
        "multicast": "",        # blank = 239.69.<c>.<d> from this unit's a.b.c.d
        "port": 5004,
        "channels": 2,          # 1 or 2 (the player mix is stereo)
        "bit_depth": 24,        # 24 (L24, AES67 standard) or 16 (L16)
        "ptime_ms": 1,          # packet time; 1 ms is what every AES67 receiver supports
        "ptp_domain": 0,        # must match the Q-SYS Core's PTP domain
        "ttl": 32,
        "dscp": 34,             # QoS marking for media packets (AF41)
        "interface": "",        # network interface for PTP + media, e.g. eth0 (blank = default route)
        "session_name": "",     # blank = "<screen name> audio"
        "sap": True,            # announce via SAP so Q-SYS / Dante Controller discover it
    },
    "transition": {
        "type": "fade",   # fade | cut
        "duration_ms": 400,
    },
    "preview": {
        "enabled": True,
        "fps": 5,
        "width": 640,
    },
    "transcode": {
        # Automatic conversion of uploads into playback copies. Off: files play as uploaded
        # (Convert now on a file still makes a copy by hand).
        "enabled": False,
        # When enabled - auto: convert files this unit would struggle to play;
        # always: convert everything
        "mode": "auto",
        "keep_original": True,   # keep the uploaded file alongside the playback copy
        "crf": 20,               # x264 quality (lower = better/larger, 18-23 is typical)
        "preset": "veryfast",    # x264 speed/size trade-off
        "threads": 0,            # 0 = all cores but two (leaves room for playback)
    },
    "playback": {
        "image_duration": 10,
        # What plays when nothing is triggered: "playlist" or "black"
        "default_mode": "playlist",
        "default_playlist": [],
        "default_shuffle": False,
    },
    "feature": {
        # Feature screening: a long video (film) triggered by API, GUI or schedule. Anything
        # else that takes the screen (an emergency message, a cue, Black) pauses it at its
        # position; it carries on from there when that is cleared (Normal).
        # What the screen does when the feature ends: default (Normal playlist) | black | hold
        "after": "default",
        "rewind_s": 0,             # on resume, start this many seconds before the stop point
        "resume_after_restart": True,  # carry on after a power cut / service restart
        # TCP clients (Q-SYS) get a FEATURE position line this often while it plays
        "position_interval_s": 1,
        # Optional webhook: called on started / interrupted / resumed / finished / stopped.
        # {event} in the URL is replaced with the event name. POST sends JSON; GET doesn't.
        "notify_url": "",
        "notify_method": "POST",
    },
    "schedule": {
        # IANA timezone for schedule times, e.g. Australia/Melbourne. Blank = system clock's.
        "timezone": "",
    },
    "control": {
        "http_port": 8080,
        "tcp_port": 7700,
        "udp_port": 7701,
        # When set, HTTP requests need "Authorization: Bearer <token>",
        # "X-API-Key: <token>" or "?token=<token>". TCP/UDP need "AUTH <token>".
        "api_token": "",
    },
    "fleet": {
        # Optional: register this player with a master server for multi-screen control.
        # Leave master_url empty for a standalone unit.
        "master_url": "",
        "master_token": "",
        "screen_name": "",      # defaults to the hostname
        "groups": [],           # suggested groups, e.g. ["east-wall", "atrium"]
        "advertise_url": "",    # how the master should reach this player (default: auto)
    },
}

# Changes to these sections require the GStreamer pipeline to be rebuilt.
PIPELINE_SECTIONS = ("output", "display", "ndi", "audio", "aes67", "preview")


def _validators() -> Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]]:
    from . import aes67

    def schedule(section):
        from .schedule import valid_timezone
        tz = str(section.get("timezone") or "").strip()
        if not valid_timezone(tz):
            raise ValueError(f"schedule: unknown timezone '{tz}' (e.g. Australia/Melbourne)")
        return dict(section, timezone=tz)

    def display(section):
        out = dict(section)
        for k in ("width", "height", "x", "y", "out_width", "out_height", "connector_id"):
            try:
                out[k] = int(float(out.get(k) or 0))
            except (TypeError, ValueError):
                raise ValueError(f"display: {k} must be a number")
        if out["width"] < 0 or out["height"] < 0 or out["width"] > 8192 or out["height"] > 8192:
            raise ValueError("display: width/height must be 0-8192")
        try:
            out["refresh"] = round(float(out.get("refresh") or 0), 3)
        except (TypeError, ValueError):
            raise ValueError("display: refresh must be a number of Hz (0 = auto)")
        if not (out["refresh"] == 0 or 10 <= out["refresh"] <= 240):
            raise ValueError("display: refresh must be 0 (auto) or 10-240 Hz")
        return out

    def feature(section):
        out = dict(section)
        if out.get("after") not in ("default", "black", "hold"):
            raise ValueError("feature: after must be default, black or hold")
        try:
            out["rewind_s"] = max(0.0, float(out.get("rewind_s") or 0))
            out["position_interval_s"] = max(0.2, float(out.get("position_interval_s") or 1))
        except (TypeError, ValueError):
            raise ValueError("feature: rewind_s / position_interval_s must be numbers")
        out["notify_url"] = str(out.get("notify_url") or "").strip()
        if out["notify_url"] and not out["notify_url"].startswith(("http://", "https://")):
            raise ValueError("feature: notify_url must start with http:// or https://")
        out["notify_method"] = "GET" if str(out.get("notify_method")).upper() == "GET" else "POST"
        return out
    return {"aes67": aes67.validate, "schedule": schedule, "display": display, "feature": feature}


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "settings.json"
        self._lock = threading.Lock()
        self._data = copy.deepcopy(DEFAULTS)
        if self.path.exists():
            try:
                self._data = _merge(DEFAULTS, json.loads(self.path.read_text()))
            except (ValueError, OSError):
                pass
        self.save()

    def get(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def section(self, name: str) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data[name])

    def update(self, patch: Dict[str, Any]) -> Set[str]:
        """Deep-merge a partial settings dict. Returns the names of changed sections.

        Raises ValueError (and changes nothing) when a section fails validation."""
        with self._lock:
            before = copy.deepcopy(self._data)
            # Only allow known top-level sections
            clean = {k: v for k, v in patch.items() if k in DEFAULTS and isinstance(v, dict)}
            merged = _merge(self._data, clean)
            for name, fn in _validators().items():
                if name in clean:
                    merged[name] = fn(merged[name])  # ValueError -> nothing is changed
            self._data = merged
            changed = {k for k in DEFAULTS if before.get(k) != self._data.get(k)}
        self.save()
        return changed

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            os.replace(tmp, self.path)
