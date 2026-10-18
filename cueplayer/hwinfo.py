"""What this machine can decode efficiently, used to decide whether media needs converting."""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

# GStreamer hardware decoders -> codec (ffprobe codec_name). Most are registered below the
# software decoders, so the engine raises their rank when output.hw_decode is on.
HW_DECODERS: Dict[str, str] = {
    "v4l2h264dec": "h264", "v4l2h265dec": "hevc",        # Raspberry Pi 4 and older (stateful V4L2)
    "v4l2slh264dec": "h264", "v4l2slh265dec": "hevc",    # stateless V4L2 (Pi 5 HEVC, others)
    "vah264dec": "h264", "vah265dec": "hevc", "vavp9dec": "vp9", "vaav1dec": "av1",  # Intel/AMD
    "nvh264dec": "h264", "nvh265dec": "hevc", "nvvp9dec": "vp9", "nvav1dec": "av1",  # NVIDIA
}

# Largest frame each hardware decoder handles, where it is smaller than the canvas could be.
PI_H264_MAX = (1920, 1088)

_cache: Optional[Dict[str, Any]] = None


def _read(path: str) -> str:
    try:
        return Path(path).read_text(errors="ignore").strip("\x00\n ")
    except OSError:
        return ""


def _gst_decoders() -> Dict[str, str]:
    """codec -> decoder element, for hardware decoders present in the GStreamer registry."""
    try:
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst  # type: ignore
        Gst.init(None)
    except (ImportError, ValueError):
        return {}
    reg = Gst.Registry.get()
    out: Dict[str, str] = {}
    for name, codec in HW_DECODERS.items():
        if reg.lookup_feature(name) is not None:
            out.setdefault(codec, name)
    return out


def _ffmpeg_encoders() -> set:
    if not shutil.which("ffmpeg"):
        return set()
    try:
        txt = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True,
                             text=True, timeout=15).stdout
    except (subprocess.SubprocessError, OSError):
        return set()
    return {m.group(1) for m in re.finditer(r"^\s*V\S*\s+(\S+)", txt, re.M)}


def detect(refresh: bool = False) -> Dict[str, Any]:
    global _cache
    if _cache is not None and not refresh:
        return _cache
    model = _read("/proc/device-tree/model")
    if not model:
        cpu = _read("/proc/cpuinfo")
        m = re.search(r"model name\s*:\s*(.+)", cpu)
        model = m.group(1).strip() if m else platform.processor() or platform.machine()
    if "Raspberry Pi 5" in model:
        family = "pi5"
    elif "Raspberry Pi 4" in model or "Compute Module 4" in model:
        family = "pi4"
    elif "Raspberry Pi" in model:
        family = "pi"
    elif platform.machine() in ("x86_64", "AMD64", "i686"):
        family = "x86"
    else:
        family = "other"
    try:
        mem_mb = int(re.search(r"MemTotal:\s+(\d+)", _read("/proc/meminfo")).group(1)) // 1024
    except (AttributeError, ValueError):
        mem_mb = 0

    decoders = _gst_decoders()
    decode_max: Dict[str, Any] = {}
    if family in ("pi4", "pi") and "h264" in decoders:
        decode_max["h264"] = list(PI_H264_MAX)

    # Codecs this unit plays smoothly. H.264 is always accepted: it has hardware decode on
    # almost everything, and is the cheapest codec to decode in software where it doesn't
    # (Pi 5, plain x86).
    playable = sorted(set(decoders) | {"h264"})
    encoders = _ffmpeg_encoders()
    names = {"h264": "H.264", "hevc": "HEVC", "vp9": "VP9", "av1": "AV1"}
    hw = ", ".join(names.get(c, c) for c in sorted(decoders)) or "none"
    _cache = {
        "model": model,
        "family": family,
        "arch": platform.machine(),
        "cpus": os.cpu_count() or 1,
        "mem_mb": mem_mb,
        "hw_decoders": decoders,
        "decode_max": decode_max,
        "playable_codecs": playable,
        "encoder": "libx264" if "libx264" in encoders else None,
        "summary": f"{model} · hardware decode: {hw}",
    }
    return _cache
