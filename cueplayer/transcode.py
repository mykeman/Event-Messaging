"""Convert media into a format this unit plays smoothly.

Each video/image is checked against the hardware (hwinfo.detect) and the canvas. If it
would be expensive to play - an unsupported codec, 10-bit/4:2:2 colour, interlaced,
larger than the canvas, a higher frame rate than the canvas, or an extreme bitrate - a
"playback copy" is made in the background:

    video -> H.264 (8-bit 4:2:0) MP4, scaled to the canvas, at the canvas frame rate
    image -> PNG/JPEG scaled to the canvas

The original keeps playing until the copy is ready, so freshly uploaded event files are
usable immediately. ffmpeg runs at the lowest CPU and I/O priority so it does not disturb
playback.
"""
from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import hwinfo

log = logging.getLogger("cueplayer.transcode")

OK_PIX_FMTS = {"yuv420p", "yuvj420p", "nv12"}
OK_IMAGE_CODECS = {"png", "mjpeg"}  # what the image path decodes cheaply
ALPHA_PIX_FMTS = ("rgba", "argb", "bgra", "abgr", "ya8", "ya16", "rgba64", "pal8")


def _even(n: float) -> int:
    return max(2, int(round(n / 2)) * 2)


def target_size(sw: int, sh: int, cw: int, ch: int, mode: str,
                cap: Optional[Tuple[int, int]] = None) -> Tuple[int, int]:
    """Size a source should be stored at: no larger than it will be shown on the canvas
    (never upscaled), and within the decoder's limit."""
    if mode == "stretch":
        w, h = min(sw, cw), min(sh, ch)
    elif mode == "none":
        w, h = sw, sh
    else:
        s = min(cw / sw, ch / sh) if mode == "fit" else max(cw / sw, ch / sh)
        s = min(1.0, s)
        w, h = sw * s, sh * s
    if cap and (w > cap[0] or h > cap[1]):
        s = min(cap[0] / w, cap[1] / h)
        w, h = w * s, h * s
    return _even(w), _even(h)


def analyse(meta: Dict[str, Any], kind: str, canvas: Tuple[int, int, int], scale_mode: str,
            hw: Dict[str, Any], mode: str = "auto") -> Dict[str, Any]:
    """Decide whether a file needs a playback copy. Returns {needed, reasons, target}."""
    cw, ch, cfps = canvas
    sw, sh = int(meta.get("width") or 0), int(meta.get("height") or 0)
    codec = meta.get("codec") or ""
    pix = meta.get("pix_fmt") or ""
    reasons: List[str] = []
    if not sw or not sh:
        return {"needed": False, "reasons": ["could not read the file's details"], "target": None}

    if kind == "image":
        tw, th = target_size(sw, sh, cw, ch, scale_mode)
        if codec not in OK_IMAGE_CODECS:
            reasons.append(f"{codec or 'this'} images decode slowly - converting to PNG/JPEG")
        if tw < sw * 0.95:
            reasons.append(f"{sw}×{sh} is larger than shown on the {cw}×{ch} canvas")
        alpha = pix.startswith(ALPHA_PIX_FMTS) or codec == "png"
        target = {"kind": "image", "width": tw, "height": th, "format": "png" if alpha else "jpg"}
    else:
        playable = hw.get("playable_codecs") or ["h264"]
        cap = tuple(hw.get("decode_max", {}).get("h264") or ()) or None
        if codec not in playable:
            where = "hardware-decoded here" if hw.get("hw_decoders") else "decoded efficiently here"
            reasons.append(f"{codec or 'unknown'} video is not {where}")
        if pix and pix not in OK_PIX_FMTS:
            reasons.append(f"pixel format {pix} (10-bit or 4:2:2/4:4:4) is not hardware-decodable")
        if meta.get("interlaced"):
            reasons.append("interlaced - deinterlacing for a clean picture")
        tw, th = target_size(sw, sh, cw, ch, scale_mode, cap if codec in ("h264", "") or reasons else None)
        if tw < sw * 0.95 or th < sh * 0.95:
            if cap and (sw > cap[0] or sh > cap[1]):
                reasons.append(f"{sw}×{sh} is above the hardware decoder limit ({cap[0]}×{cap[1]})")
            else:
                reasons.append(f"{sw}×{sh} is larger than shown on the {cw}×{ch} canvas")
        fps = float(meta.get("fps") or 0)
        tfps = cfps if fps > cfps * 1.05 else (fps or cfps)
        if fps > cfps * 1.05:
            reasons.append(f"{fps:g} fps is above the {cfps} fps canvas")
        br = int(meta.get("bit_rate") or 0)
        limit = max(25_000_000, int(sw * sh * (fps or cfps) * 0.25))
        if br > limit:
            reasons.append(f"very high bitrate ({br / 1e6:.0f} Mbit/s)")
        target = {"kind": "video", "codec": "h264", "width": tw, "height": th,
                  "fps": round(tfps, 3), "deinterlace": bool(meta.get("interlaced"))}

    if mode == "off":
        return {"needed": False, "reasons": reasons, "target": target, "disabled": True}
    if mode == "always" and not reasons:
        reasons.append("conversion forced by settings")
    return {"needed": bool(reasons), "reasons": reasons, "target": target}


def build_command(src: Path, dest: Path, target: Dict[str, Any], opts: Dict[str, Any],
                  cpus: int) -> List[str]:
    nice = []
    if shutil.which("nice"):
        nice += ["nice", "-n", "19"]
    if shutil.which("ionice"):
        nice += ["ionice", "-c", "3"]
    base = nice + ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error"]
    w, h = target["width"], target["height"]
    if target["kind"] == "image":
        q = ["-compression_level", "6"] if target["format"] == "png" else ["-q:v", "2"]
        return base + ["-i", str(src), "-frames:v", "1", "-vf", f"scale={w}:{h}:flags=lanczos",
                       *q, str(dest)]
    threads = int(opts.get("threads") or 0) or max(1, cpus - 2)  # leave cores for playback
    fps = target["fps"]
    vf = (["yadif=mode=send_frame"] if target.get("deinterlace") else []) + [
        f"scale={w}:{h}:flags=lanczos", f"fps={fps:g}", "format=yuv420p"]
    gop = max(1, int(round(fps * 2)))
    return base + [
        "-progress", "pipe:1", "-nostats", "-i", str(src),
        "-map", "0:v:0", "-map", "0:a:0?", "-sn", "-dn",
        "-vf", ",".join(vf),
        "-c:v", "libx264", "-preset", str(opts.get("preset") or "veryfast"),
        # No forced -level: x264 picks the lowest level the stream needs (4.0 for 1080p30),
        # like typical camera/web files. A forced 4.2 made Pi hardware decoders refuse it.
        "-crf", str(int(opts.get("crf") or 20)), "-profile:v", "high",
        "-g", str(gop), "-threads", str(threads),
        "-c:a", "aac", "-b:a", "192k", "-ac", "2", "-ar", "48000",
        "-movflags", "+faststart", "-f", "mp4", str(dest)]


def hw_decode_check(path: Path) -> Tuple[Optional[bool], str]:
    """Decode the first frames of an H.264 MP4 with this unit's hardware decoder.
    Returns (True, "") if it works, (False, reason) if not, (None, "") if there is no
    hardware H.264 decoder or GStreamer tools to test with."""
    dec = hwinfo.detect().get("hw_decoders", {}).get("h264")
    tool = shutil.which("gst-launch-1.0")
    if not dec or not tool:
        return None, ""
    cmd = [tool, "-q", "filesrc", f"location={path}", "!", "qtdemux", "name=d", "d.video_0",
           "!", "queue", "!", "h264parse", "!", dec, "!", "fakesink", "num-buffers=30"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (subprocess.SubprocessError, OSError) as e:
        return None, str(e)
    if r.returncode == 0:
        return True, ""
    err = (r.stderr or r.stdout or "").strip().splitlines()
    why = next((l for l in reversed(err) if "reason" in l or "ERROR" in l), err[-1] if err else "")
    return False, why[-200:]


# Bump when the video encoder settings change in a way old copies should be redone for.
# 2: no forced H.264 level (4.2 made Pi hardware decoders refuse copies).
ENCODER_REV = 2


class Transcoder:
    """Single background worker; one conversion at a time."""

    def __init__(self, library, get_settings: Callable[[], Dict[str, Any]]):
        self.lib = library
        self.get_settings = get_settings
        self.q: "queue.Queue[str]" = queue.Queue()
        self.pending: set = set()
        self.current: Optional[str] = None
        self.proc: Optional[subprocess.Popen] = None
        self.on_change: Callable[[], None] = lambda: None
        self._last_notify = 0.0
        self._cancel: set = set()
        threading.Thread(target=self._run, name="transcode", daemon=True).start()

    # ---- decisions -------------------------------------------------------------
    def _context(self):
        st = self.get_settings()
        out, tc = st["output"], st.get("transcode", {})
        canvas = (int(out["canvas_width"]), int(out["canvas_height"]), int(out["framerate"]))
        return canvas, out.get("scale_mode", "fit"), tc

    def evaluate(self, item_id: str, force: bool = False) -> Optional[Dict[str, Any]]:
        """(Re)analyse one item and queue it if it needs a playback copy."""
        item = self.lib.get(item_id)
        if not item:
            return None
        canvas, gmode, tc = self._context()
        meta = item.get("probe") or {}
        if not meta.get("pix_fmt") and not meta.get("probed"):
            meta = self.lib.reprobe(item_id)
        mode = "always" if force else tc.get("mode", "auto")
        a = analyse(meta, item["kind"], canvas, item.get("scale_mode") or gmode, hwinfo.detect(), mode)
        pb = item.get("playback") or {}
        if pb.get("skip") and not force:
            a = dict(a, needed=False)  # operator chose to play the original
        if not a["needed"]:
            if pb.get("status") == "ready" and pb.get("target") != a["target"] and not pb.get("original_removed"):
                self.lib.drop_playback_copy(item_id)   # copy no longer wanted (e.g. setting changed)
            self.lib.set_playback(item_id, status="original", reasons=a["reasons"],
                                  target=a["target"], disabled=a.get("disabled", False),
                                  skip=bool(pb.get("skip")) and not force)
        elif pb.get("status") == "ready" and pb.get("target") == a["target"] and not force \
                and (item["kind"] != "video" or pb.get("rev", 1) >= ENCODER_REV
                     or pb.get("original_removed")):
            pass  # already have the right copy (video copies from older settings are redone)
        elif not hwinfo.detect().get("encoder") and item["kind"] == "video":
            self.lib.set_playback(item_id, status="failed", reasons=a["reasons"], target=a["target"],
                                  error="ffmpeg with libx264 is not installed")
        else:
            self.lib.set_playback(item_id, status="queued", reasons=a["reasons"], target=a["target"],
                                  progress=0, error=None, skip=False)
            self.enqueue(item_id)
        self.notify(force=True)
        return self.lib.get(item_id)

    def use_original(self, item_id: str) -> None:
        """Operator override: play the uploaded file, discard any playback copy."""
        self.cancel(item_id)
        self.lib.drop_playback_copy(item_id)
        self.lib.set_playback(item_id, status="original", skip=True)
        self.notify(force=True)

    def evaluate_all(self) -> None:
        for item in self.lib.list():
            try:
                self.evaluate(item["id"])
            except Exception:
                log.exception("analysis failed for %s", item.get("id"))

    def enqueue(self, item_id: str) -> None:
        if item_id not in self.pending:
            self.pending.add(item_id)
            self.q.put(item_id)

    def cancel(self, item_id: str) -> None:
        self._cancel.add(item_id)
        if self.current == item_id and self.proc:
            self.proc.terminate()

    def notify(self, force: bool = False) -> None:
        now = time.time()
        if force or now - self._last_notify > 2:
            self._last_notify = now
            try:
                self.on_change()
            except Exception:
                log.exception("notify failed")

    # ---- worker ----------------------------------------------------------------
    def _run(self) -> None:
        while True:
            item_id = self.q.get()
            self.pending.discard(item_id)
            if item_id in self._cancel:
                self._cancel.discard(item_id)
                continue
            try:
                self._convert(item_id)
            except Exception as e:
                log.exception("conversion failed for %s", item_id)
                self.lib.set_playback(item_id, status="failed", error=str(e))
                self.notify(force=True)
            finally:
                self.current = None
                self.proc = None

    def _convert(self, item_id: str) -> None:
        item = self.lib.get(item_id)
        pb = (item or {}).get("playback") or {}
        if not item or pb.get("status") != "queued" or not pb.get("target"):
            return
        target = pb["target"]
        _, _, tc = self._context()
        src = self.lib.path_of(item)
        ext = ".mp4" if target["kind"] == "video" else "." + target["format"]
        name = f"{item_id}.play{ext}"
        dest = self.lib.media_dir / name
        part = self.lib.media_dir / f"{item_id}.play.part{ext}"
        cmd = build_command(src, part, target, tc, hwinfo.detect()["cpus"])
        log.info("converting %s: %s", item["name"], "; ".join(pb.get("reasons") or []))
        self.current = item_id
        self.lib.set_playback(item_id, status="converting", progress=0, started=time.time())
        self.notify(force=True)
        duration_us = float(item.get("duration") or 0) * 1e6
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        block: Dict[str, str] = {}
        for line in self.proc.stdout:  # -progress emits key=value blocks ending in progress=
            key, _, value = line.strip().partition("=")
            block[key] = value
            if key != "progress" or not duration_us:
                continue
            try:
                done_us = int(block.get("out_time_us") or 0)
            except ValueError:
                done_us = 0
            pct = min(99.0, max(0.0, done_us / duration_us * 100))
            try:
                speed = float((block.get("speed") or "").rstrip("x"))
            except ValueError:
                speed = 0.0
            eta = (duration_us - done_us) / 1e6 / speed if speed > 0 else None
            self.lib.set_playback(item_id, progress=round(pct, 1), speed=round(speed, 2) or None,
                                  eta=round(eta) if eta is not None else None)
            self.notify()
            block = {}
        err = self.proc.stderr.read()
        rc = self.proc.wait()
        if item_id in self._cancel or self.lib.get(item_id) is None:
            self._cancel.discard(item_id)
            part.unlink(missing_ok=True)
            return
        if rc != 0 or not part.exists() or part.stat().st_size == 0:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg failed ({rc}): {err.strip()[-300:]}")
        os.replace(part, dest)
        keep = tc.get("keep_original", True)
        self.lib.finish_playback_copy(item_id, name, target, keep_original=keep)
        self.lib.set_playback(item_id, rev=ENCODER_REV)
        log.info("converted %s -> %s (%dx%d)", item["name"], name, target["width"], target["height"])
        if target["kind"] == "video":
            ok, why = hw_decode_check(dest)
            if ok is False:
                log.warning("hardware decoder can't play %s (%s) - using software decoding", name, why)
                self.lib.set_decode(item_id, "software")
                self.lib.set_playback(item_id, hw_check=f"hardware decoder refused this copy: {why}")
            elif ok:
                self.lib.set_playback(item_id, hw_check="ok")
        self.notify(force=True)
