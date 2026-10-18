"""Persistent settings, stored as JSON in the data directory."""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, Set

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
    "transition": {
        "type": "fade",   # fade | cut
        "duration_ms": 400,
    },
    "preview": {
        "enabled": True,
        "fps": 5,
        "width": 640,
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
PIPELINE_SECTIONS = ("output", "display", "ndi", "audio", "preview")


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
        """Deep-merge a partial settings dict. Returns the names of changed sections."""
        with self._lock:
            before = copy.deepcopy(self._data)
            # Only allow known top-level sections
            clean = {k: v for k, v in patch.items() if k in DEFAULTS and isinstance(v, dict)}
            self._data = _merge(self._data, clean)
            changed = {k for k in DEFAULTS if before.get(k) != self._data.get(k)}
        self.save()
        return changed

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2))
            os.replace(tmp, self.path)
