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
        # kmssink only: DRM connector id (0 = first connected)
        "connector_id": 0,
        # kmssink only. auto: keep the mode set at boot (install.sh --take-hdmi) when it
        # matches width x height, so its refresh rate is used; otherwise kmssink picks one
        # (usually 60 Hz). Or "keep" / "force".
        "modeset": "auto",
    },
    "ndi": {
        "enabled": True,
        "name": "CuePlayer",
    },
    "audio": {
        "enabled": False,
        "sink": "auto",   # auto | alsa | pulse | none
        "device": "",     # alsa device, e.g. hw:0,0 (HDMI)
    },
    "aes67": {
        # Native AES67 / SMPTE 2110-30 audio output (RTP multicast, PTP-locked). Needs audio
        # enabled. Off by default; untested on real AES67 hardware - see README.
        "enabled": False,
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
        # auto: convert files this unit would struggle to play; always: convert everything;
        # off: play files as uploaded
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
    return {"aes67": aes67.validate}


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
