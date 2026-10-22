"""Playback engine.

A single always-running GStreamer pipeline renders a fixed-size *canvas*:

    videotestsrc(live, bg colour) ─┐
    clip bin (uridecodebin) ───────┼─ compositor ─ tee ─┬─ display (videobox → kms/gl/x sink)
    clip bin (next clip) ──────────┘                    ├─ NDI (ndisink)
                                                        ├─ preview (jpegenc → appsink, for the web GUI)
                                                        └─ fakesink (clock)

    audiotestsrc(silence) ─┐
    clip audio ────────────┴─ audiomixer ─ tee ─┬─ local audio sink
                                                ├─ NDI (ndisinkcombiner)
                                                └─ AES67 (rtpL24pay → udpsink multicast)

With AES67 on, the pipeline runs on a PTP clock (GstNet) so RTP timestamps follow the
PTP media clock (a=mediaclk:direct=0).

Each clip is a bin added to the running pipeline. Its pad offset is set when its
first frame arrives so it starts "now"; the previous clip keeps repeating its last
frame until then, giving clean cuts and crossfades without black gaps.

All GStreamer work happens on a dedicated GLib main loop thread. Public methods are
thread-safe and return immediately.
"""
from __future__ import annotations

import itertools
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from . import aes67
from .common import hostname
from .hwinfo import HW_DECODERS

log = logging.getLogger("cueplayer.engine")

EventCallback = Callable[[Dict[str, Any]], None]


def fit_rect(sw: int, sh: int, cw: int, ch: int, mode: str) -> Tuple[int, int, int, int]:
    """Place a sw x sh source into a cw x ch canvas. Returns (x, y, w, h)."""
    if sw <= 0 or sh <= 0 or mode == "stretch":
        return 0, 0, cw, ch
    if mode == "none":
        w, h = sw, sh
    else:
        s = min(cw / sw, ch / sh) if mode == "fit" else max(cw / sw, ch / sh)
        w, h = max(1, round(sw * s)), max(1, round(sh * s))
    return (cw - w) // 2, (ch - h) // 2, w, h


def boot_video_modes(cmdline: Optional[str] = None) -> Dict[str, Tuple[int, int, float]]:
    """Display modes forced on the kernel command line (video=HDMI-A-1:1920x1080@50D, as
    set by install.sh --take-hdmi): {"HDMI-A-1": (1920, 1080, 50.0)}."""
    if cmdline is None:
        try:
            cmdline = Path("/proc/cmdline").read_text()
        except OSError:
            return {}
    out = {}
    for tok in cmdline.split():
        m = re.fullmatch(r"video=([A-Za-z0-9-]+):(\d+)x(\d+)M?R?(?:-\d+)?@(\d+(?:\.\d+)?)\S*", tok)
        if m:
            out[m.group(1)] = (int(m.group(2)), int(m.group(3)), float(m.group(4)))
    return out


def detect_display_connector(drm: str = "/sys/class/drm") -> Optional[Tuple[int, str]]:
    """(connector id, name) of the first connector with a display actually attached (it has
    an EDID). kmssink's own choice is the first connector reporting "connected", which can be
    an empty port that video=...D on the kernel command line forced on."""
    for path in sorted(Path(drm).glob("card*-*")):
        try:
            if path.joinpath("edid").stat().st_size == 0:
                continue
            cid = int(path.joinpath("connector_id").read_text().strip())
        except (OSError, ValueError):
            continue
        return cid, re.sub(r"^card\d+-", "", path.name)
    return None


def hdmi_alsa_device(port: Optional[str], cards: Optional[str] = None) -> Optional[str]:
    """ALSA device for the HDMI port the video is on, e.g. HDMI-A-2 -> hdmi:CARD=vc4hdmi1,DEV=0.

    On a Pi each HDMI port has its own sound card (vc4hdmi0, vc4hdmi1) and ALSA's default is
    the first one (or the headphone jack), so audio otherwise goes to the wrong port. The
    hdmi: device does the IEC958 framing these cards require."""
    if not port:
        return None
    m = re.fullmatch(r"HDMI-A-(\d+)", port)
    if not m:
        return None
    index = int(m.group(1)) - 1
    if cards is None:
        try:
            cards = Path("/proc/asound/cards").read_text()
        except OSError:
            return None
    ids = re.findall(r"^\s*\d+\s+\[(\S+)\s*\]", cards, re.M)
    # Only the Pi's per-port vc4hdmiN cards map 1:1 to connectors; elsewhere (Intel/AMD
    # HDA) HDMI outputs are devices on one card, so leave those to the system default.
    card = next((c for c in ids if c.lower() == f"vc4hdmi{index}"), None)
    return f"hdmi:CARD={card},DEV=0" if card else None


def edid_has_audio(edid: bytes) -> Optional[bool]:
    """Whether a display's EDID says it accepts audio over HDMI (CEA-861 extension: the
    basic-audio flag or an audio data block). None if the EDID can't be read."""
    if len(edid) < 128:
        return None
    for i in range(1, min(edid[126], 4) + 1):
        blk = edid[128 * i:128 * (i + 1)]
        if len(blk) < 4 or blk[0] != 0x02:
            continue
        if blk[3] & 0x40:          # basic audio
            return True
        end, pos = blk[2], 4        # data blocks run from byte 4 to the DTD offset
        while 4 <= pos < min(end, 127):
            tag, length = blk[pos] >> 5, blk[pos] & 0x1F
            if tag == 1 and length:  # audio data block
                return True
            pos += 1 + length
    return False                    # no CEA extension or no audio: DVI-style input


def display_accepts_audio(port: Optional[str], drm: str = "/sys/class/drm") -> Optional[bool]:
    if not port:
        return None
    for path in Path(drm).glob(f"card*-{port}"):
        try:
            return edid_has_audio(path.joinpath("edid").read_bytes())
        except OSError:
            return None
    return None


def audio_server_running() -> bool:
    """A PipeWire/PulseAudio server this process can use (desktop sessions)."""
    run = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return bool(os.environ.get("PULSE_SERVER")) or any(
        Path(run, n).exists() for n in ("pulse/native", "pipewire-0"))


def cadence_ok(fps: float, refresh: float) -> bool:
    """True when every frame is shown for the same number of refreshes (e.g. 25 or 50 fps
    on 50 Hz, 30 or 60 fps on 60 Hz). Otherwise motion judders even with no dropped frames."""
    if fps <= 0 or refresh <= 0:
        return True
    ratio = refresh / fps
    return round(ratio) >= 1 and abs(ratio - round(ratio)) < 0.01


def parse_colour(hex_colour: str) -> int:
    """'#rrggbb' -> 0xAARRGGBB as used by videotestsrc."""
    h = (hex_colour or "#000000").lstrip("#")
    try:
        rgb = int(h[:6], 16)
    except ValueError:
        rgb = 0
    return 0xFF000000 | rgb


class BaseEngine:
    """Interface shared by the GStreamer engine and the mock engine."""

    def __init__(self, on_event: EventCallback):
        self.on_event = on_event
        self._ids = itertools.count(1)
        self._preview: Optional[bytes] = None
        self.status: Dict[str, Any] = {"backend": "none", "running": False, "errors": []}
        self._aes67_cfg: Optional[Dict[str, Any]] = None  # resolved config while streaming

    def start(self, settings: Dict[str, Any]) -> None: ...
    def rebuild(self, settings: Dict[str, Any]) -> None: ...
    def stop(self) -> None: ...

    def play(self, path: str, kind: str, scale_mode: str, fade_ms: int,
             duration_hint: float = 0, sw_decode: bool = False) -> int:
        """sw_decode: don't use hardware decoders for this clip (after one failed on it)."""
        raise NotImplementedError

    def black(self, fade_ms: int) -> None: ...

    def preview_jpeg(self) -> Optional[bytes]:
        return self._preview

    def aes67_sdp(self) -> Optional[str]:
        """SDP of the AES67 stream, or None while it isn't running / PTP isn't synced."""
        st = self.status.get("aes67") or {}
        if self._aes67_cfg is None or not st.get("synced"):
            return None
        return aes67.build_sdp(self._aes67_cfg, st.get("grandmaster"), st.get("sdp_version", 0))

    @staticmethod
    def _screen_name(settings: Dict[str, Any]) -> str:
        return (settings.get("fleet") or {}).get("screen_name") or hostname()

    def _emit(self, **event: Any) -> None:
        try:
            self.on_event(event)
        except Exception:  # pragma: no cover
            log.exception("event callback failed")


# ---------------------------------------------------------------------------
# Mock engine: used when GStreamer is not available (development on macOS etc.)
# ---------------------------------------------------------------------------
class MockEngine(BaseEngine):
    def __init__(self, on_event: EventCallback):
        super().__init__(on_event)
        self.status.update(backend="mock")
        self._timers: Dict[int, threading.Timer] = {}

    def start(self, settings):
        out = settings["output"]
        self.status.update(running=True, display="mock", ndi="mock",
                           canvas=f"{out['canvas_width']}x{out['canvas_height']}@{out['framerate']}")
        cfg = settings.get("aes67") or {}
        st = aes67.status_template(cfg)
        self._aes67_cfg = None
        if cfg.get("enabled"):
            # Nothing is streamed; this lets the SDP endpoint and GUI be exercised.
            self._aes67_cfg = aes67.resolve(cfg, self._screen_name(settings))
            st.update(state="mock (not streaming)", synced=True,
                      grandmaster="00-00-00-FF-FE-00-00-00",
                      multicast=self._aes67_cfg["multicast"])
        self.status["aes67"] = st
        self._emit(type="engine_started")

    def rebuild(self, settings):
        self.start(settings)

    def play(self, path, kind, scale_mode, fade_ms, duration_hint=0, sw_decode=False):
        cid = next(self._ids)
        for t in self._timers.values():
            t.cancel()
        self._timers.clear()
        # Test hooks: files starting with these markers fail like a broken file, or like a
        # hardware decoder rejecting the stream (plays fine in software)
        try:
            with open(path, "rb") as f:
                head = f.read(16)
        except OSError:
            head = b""
        if head.startswith(b"MOCK:BROKEN") or (head.startswith(b"MOCK:HWFAIL") and not sw_decode):
            hw = head.startswith(b"MOCK:HWFAIL")
            threading.Timer(0.05, lambda: self._emit(
                type="clip_error", clip=cid, error="Internal data stream error.",
                decoder="v4l2h264dec" if hw else "avdec_h264", hw_decoder=hw)).start()
            return cid
        self.status["video_decoder"] = "avdec_h264 (mock)" if sw_decode else "mock"
        threading.Timer(0.05, lambda: self._emit(type="clip_started", clip=cid)).start()
        if kind == "video":
            t = threading.Timer(duration_hint or 10.0,
                                lambda: self._emit(type="clip_ended", clip=cid))
            t.start()
            self._timers[cid] = t
        return cid

    def black(self, fade_ms):
        for t in self._timers.values():
            t.cancel()
        self._timers.clear()


# ---------------------------------------------------------------------------
# GStreamer engine
# ---------------------------------------------------------------------------
try:
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import GLib, Gst  # type: ignore

    HAVE_GST = True
except (ImportError, ValueError):  # pragma: no cover - depends on host
    HAVE_GST = False

try:
    gi.require_version("GstNet", "1.0")
    from gi.repository import GstNet  # type: ignore

    HAVE_GSTNET = True
except (NameError, ImportError, ValueError):  # pragma: no cover - depends on host
    HAVE_GSTNET = False


class _Clip:
    def __init__(self, cid: int, bin_: Any, kind: str, scale_mode: str, fade_ms: int):
        self.id = cid
        self.bin = bin_
        self.kind = kind
        self.scale_mode = scale_mode
        self.fade_ms = fade_ms
        self.vpad = None  # compositor request pad
        self.apad = None  # audiomixer request pad
        self.vghost = None
        self.aghost = None
        self.started = False
        self.offset_set = False
        self.dying = False
        self.decoder: Optional[str] = None
        self.path = ""
        self.lock = threading.Lock()


class GstEngine(BaseEngine):
    FADE_STEP_MS = 20

    def __init__(self, on_event: EventCallback):
        super().__init__(on_event)
        Gst.init(None)
        self.status.update(backend="gstreamer", gst_version=Gst.version_string())
        self.settings: Dict[str, Any] = {}
        self.pipeline = None
        self.comp = None
        self.amix = None
        self.clips: Dict[int, _Clip] = {}
        self.current: Optional[_Clip] = None
        self._zorder = itertools.count(1)
        self._ctx = GLib.MainContext.default()
        self._loop = GLib.MainLoop.new(self._ctx, False)
        self._thread = threading.Thread(target=self._loop.run, name="glib", daemon=True)
        self._thread.start()
        self._restart_timer = None
        self._disabled_outputs: set = set()   # outputs switched off after repeated failures
        self._orig_ranks: Dict[str, int] = {}
        self._element_clip: Dict[str, int] = {}  # element name -> clip id
        self._modeset_fallback = False  # keeping the boot mode failed: let kmssink set one
        self._display_port: Optional[str] = None   # e.g. HDMI-A-2, for matching HDMI audio
        self._audio_note = "on"
        self._fail_counts: Dict[str, int] = {}
        self._ptp_init: Optional[bool] = None   # gst_ptp_init() result (it can only run once)
        self._ptp_clocks: Dict[int, Any] = {}   # domain -> GstPtpClock
        self._ptp_timer = None
        self._ptp_waiting = False               # pipeline built without AES67 until PTP syncs
        self._sap: Optional[aes67.SapAnnouncer] = None

    # ---- thread marshalling ------------------------------------------------
    def _idle(self, fn, *args):
        def run():
            try:
                fn(*args)
            except Exception as e:
                log.exception("engine task failed")
                self._error(str(e))
            return False
        GLib.idle_add(run)

    def _error(self, msg: str) -> None:
        errs = self.status.setdefault("errors", [])
        errs.append({"t": time.time(), "msg": msg})
        del errs[:-20]
        self._emit(type="engine_error", error=msg)

    # ---- public API --------------------------------------------------------
    def start(self, settings):
        self._idle(self._safe_build, settings)

    def rebuild(self, settings):
        self._disabled_outputs.clear()  # new settings: give every output another chance
        self._modeset_fallback = False
        self._idle(self._safe_build, settings)

    def _safe_build(self, settings) -> None:
        try:
            self._build(settings)
        except Exception as e:
            log.exception("pipeline build failed")
            self._error(f"pipeline build failed: {e}")
            self.status["running"] = False
            self._schedule_restart()

    def stop(self):
        self._idle(self._teardown)

    def play(self, path, kind, scale_mode, fade_ms, duration_hint=0, sw_decode=False):
        cid = next(self._ids)
        self._idle(self._play, cid, path, kind, scale_mode, fade_ms, sw_decode)
        return cid

    def black(self, fade_ms):
        self._idle(self._black, fade_ms)

    # ---- pipeline construction ----------------------------------------------
    @staticmethod
    def _make(factory: str, name: Optional[str] = None, **props) -> Any:
        el = Gst.ElementFactory.make(factory, name)
        if el is None:
            raise RuntimeError(f"GStreamer element '{factory}' not available")
        for k, v in props.items():
            el.set_property(k.replace("_", "-"), v)
        return el

    @staticmethod
    def _has(factory: str) -> bool:
        return Gst.ElementFactory.find(factory) is not None

    @staticmethod
    def _req(el) -> Any:
        if hasattr(el, "request_pad_simple"):
            return el.request_pad_simple("sink_%u")
        return el.get_request_pad("sink_%u")

    def _caps(self, s: str) -> Any:
        return self._make("capsfilter", caps=Gst.Caps.from_string(s))

    def _chain(self, *els) -> None:
        for el in els:
            self.pipeline.add(el)
        for a, b in zip(els, els[1:]):
            if not a.link(b):
                raise RuntimeError(f"failed to link {a.get_name()} -> {b.get_name()}")

    def _teardown(self) -> None:
        if self._ptp_timer:
            GLib.source_remove(self._ptp_timer)
            self._ptp_timer = None
        if self._sap is not None:
            self._sap.stop()
            self._sap = None
        self._aes67_cfg = None
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            bus = self.pipeline.get_bus()
            bus.remove_signal_watch()
        self.pipeline = self.comp = self.amix = None
        self.clips.clear()
        self.current = None
        self.status["running"] = False

    # Hardware decoders that are safe to prefer when present. Most are registered below
    # the software decoders by default, so decodebin would otherwise pick avdec_*.
    HW_DECODERS = tuple(HW_DECODERS)

    def _apply_decoder_ranks(self, enabled: bool) -> None:
        reg = Gst.Registry.get()
        found = []
        for name in self.HW_DECODERS:
            f = reg.lookup_feature(name)
            if f is None:
                continue
            self._orig_ranks.setdefault(name, f.get_rank())
            f.set_rank(Gst.Rank.PRIMARY + 100 if enabled else self._orig_ranks[name])
            found.append(name)
        self.status["hw_decoders"] = found if enabled else []

    def _build(self, settings: Dict[str, Any]) -> None:
        self._teardown()
        self.settings = settings
        self._apply_decoder_ranks(bool(settings["output"].get("hw_decode", True)))
        self._display_port = None
        self.status.update(video_decoder=None, frames_rendered=0, frames_dropped=0)
        out, disp, ndi = settings["output"], settings["display"], settings["ndi"]
        audio, prev = settings["audio"], settings["preview"]
        cw, ch, fps = int(out["canvas_width"]), int(out["canvas_height"]), int(out["framerate"])
        vcaps = f"video/x-raw,width={cw},height={ch},framerate={fps}/1"

        self.pipeline = Gst.Pipeline.new("cueplayer")
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)

        # --- video core ---
        bg = self._make("videotestsrc", is_live=True, pattern=17,  # solid-color
                        foreground_color=parse_colour(out.get("background")))
        self.comp = self._make("compositor", "comp", background=1)
        if self.comp.find_property("ignore-inactive-pads"):
            self.comp.set_property("ignore-inactive-pads", True)
        tee = self._make("tee", "vtee", allow_not_linked=True)
        # Compose in I420, the format decoders produce. Decoded frames then pass through
        # without conversion, and the compositor skips the background whenever an opaque
        # full-canvas clip covers it. (Fades still work through the pad alpha.)
        bgcaps = self._caps(vcaps + ",format=I420")
        outcaps = self._caps(vcaps + ",format=I420")
        self._chain(bg, bgcaps)
        self.pipeline.add(self.comp)
        bgpad = self._req(self.comp)
        bgpad.set_property("zorder", 0)
        bgcaps.get_static_pad("src").link(bgpad)
        self._chain(outcaps, tee)
        self.comp.link(outcaps)

        # clock branch: keeps the pipeline paced even with no other outputs
        self._branch(tee, self._make("queue", leaky=2, max_size_buffers=2), self._fakesink())

        # --- display ---
        self.status["display"] = "disabled"
        if "display" in self._disabled_outputs:
            self.status["display"] = "disabled after repeated errors (save settings to retry)"
        elif disp.get("enabled") and disp.get("sink") != "none":
            try:
                self._build_display(tee, disp, cw, ch)
            except Exception as e:
                self.status["display"] = f"error: {e}"
                self._error(f"display: {e}")

        # --- audio core (optional) ---
        aes = settings.get("aes67") or {}
        atee = None
        if audio.get("enabled") and "audio" in self._disabled_outputs:
            self.status["audio"] = "disabled after repeated errors (save settings to retry)"
        elif audio.get("enabled"):
            try:
                atee = self._build_audio(audio, low_latency=bool(aes.get("enabled")))
                self.status["audio"] = self._audio_note
            except Exception as e:
                self.status["audio"] = f"error: {e}"
                self._error(f"audio: {e}")
                atee = None
        else:
            self.status["audio"] = "disabled"

        # --- NDI ---
        self.status["ndi"] = "disabled"
        if ndi.get("enabled") and "ndi" in self._disabled_outputs:
            self.status["ndi"] = "disabled after repeated errors (save settings to retry)"
        elif ndi.get("enabled"):
            if not self._has("ndisink"):
                self.status["ndi"] = "unavailable (gst NDI plugin / NDI runtime not installed)"
            else:
                try:
                    self._build_ndi(tee, atee, ndi)
                    self.status["ndi"] = f"on ({ndi.get('name')})"
                except Exception as e:
                    self.status["ndi"] = f"error: {e}"
                    self._error(f"ndi: {e}")

        # --- preview ---
        if prev.get("enabled"):
            pw = int(prev.get("width", 640))
            ph = max(2, round(pw * ch / cw / 2) * 2)
            sink = self._make("appsink", emit_signals=True, max_buffers=1, drop=True, sync=False)
            sink.connect("new-sample", self._on_preview)
            self._branch(tee,
                         self._make("queue", leaky=2, max_size_buffers=1),
                         self._make("videorate", drop_only=True, max_rate=int(prev.get("fps", 5))),
                         self._make("videoscale"),
                         self._make("videoconvert"),
                         self._caps(f"video/x-raw,width={pw},height={ph}"),
                         self._make("jpegenc", quality=70),
                         sink)

        # --- AES67 (optional; sets the pipeline clock, so it goes last) ---
        self.status["aes67"] = aes67.status_template(aes)
        if aes.get("enabled"):
            self._setup_aes67(atee, aes, fps)

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("pipeline failed to start")
        self.status.update(running=True, canvas=f"{cw}x{ch}@{fps}")
        log.info("pipeline started %s display=%s ndi=%s", self.status["canvas"],
                 self.status["display"], self.status["ndi"])
        self._emit(type="engine_started")

    def _fakesink(self):
        el = self._make("fakesink", sync=True)
        el.set_property("async", False)
        return el

    def _branch(self, tee, *els) -> None:
        self._chain(*els)
        tee.link(els[0])

    def _build_display(self, tee, disp, cw, ch) -> None:
        dw, dh = int(disp["width"]), int(disp["height"])
        x, y = int(disp.get("x", 0)), int(disp.get("y", 0))
        ow = int(disp.get("out_width") or cw)
        oh = int(disp.get("out_height") or ch)
        kind = disp.get("sink", "auto")
        if kind == "auto":
            if os.environ.get("WAYLAND_DISPLAY"):
                kind = "wayland"
            elif os.environ.get("DISPLAY"):
                kind = "auto-desktop"
            else:
                kind = "kms"
        fps = float(self.settings["output"]["framerate"])
        mode_note = ""
        self.status.pop("display_warning", None)
        self.status.pop("display_refresh", None)
        if kind == "kms":
            # kmssink's own modesetting picks the first mode with the right resolution,
            # which is nearly always 60 Hz. "keep" reuses the mode set at boot
            # (install.sh --take-hdmi) so its refresh rate is honoured - experimental: on
            # some Pi setups kmssink then draws nothing, so it's opt-in.
            boot = [m for m in boot_video_modes().values() if m[:2] == (dw, dh)]
            keep = disp.get("modeset", "auto") == "keep" and not self._modeset_fallback
            sink = self._make("kmssink", force_modesetting=not keep)
            port_note = ""
            if int(disp.get("connector_id") or 0) > 0:
                sink.set_property("connector-id", int(disp["connector_id"]))
                port_note = f"connector {int(disp['connector_id'])}"
            else:
                found = detect_display_connector()
                if found:
                    sink.set_property("connector-id", found[0])
                    port_note = found[1]
                    self._display_port = found[1]
            if keep:
                refresh = boot[0][2] if boot else 0.0
                mode_note = f"@{refresh:g}Hz (boot mode)" if refresh else "(current mode)"
            else:
                refresh = 60.0
                mode_note = "(mode picked by kmssink, usually 60 Hz)"
            if port_note:
                mode_note = f"on {port_note} {mode_note}"
            if refresh:
                self.status["display_refresh"] = refresh
            if not cadence_ok(fps, refresh):
                self.status["display_warning"] = (
                    f"A {fps:g} fps canvas on a {refresh:g} Hz output judders. "
                    + (f"Set the canvas (and content) to "
                       f"{int(refresh) if fps * 2 > refresh else int(refresh / 2)} fps, or try "
                       f"Display mode 'keep' with install.sh --take-hdmi --hdmi-mode {dw}x{dh}@{fps:g}."
                       if not keep else
                       f"Re-run install.sh --take-hdmi --hdmi-mode {dw}x{dh}@{fps:g} and reboot."))
        elif kind == "gl":
            sink = self._make("glimagesink")
        elif kind == "x":
            sink = self._make("ximagesink")
        elif kind == "wayland":
            sink = self._make("waylandsink", fullscreen=True) if self._has("waylandsink") \
                else self._make("autovideosink")
        elif kind == "fb":
            sink = self._make("fbdevsink")
        else:
            sink = self._make("autovideosink")
        sink.set_property("sync", True)
        sink.set_name("display_sink")
        # Time allowed for compositing/conversion before a frame counts as late. Without it a
        # live pipeline gives ~20 ms, and on slower hardware nearly every frame is dropped
        # (the sink then shows only one late frame per second).
        if sink.find_property("processing-deadline"):
            sink.set_property("processing-deadline",
                              int(self.settings["output"].get("latency_ms", 100)) * Gst.MSECOND)

        els = [self._make("queue", max_size_buffers=3, leaky=2), self._make("videoconvert")]
        if (ow, oh) != (cw, ch):
            els += [self._make("videoscale"), self._caps(f"video/x-raw,width={ow},height={oh}")]
        # videobox: negative = add border, positive = crop. Places the canvas
        # at (x, y) inside a dw x dh raster.
        left, top = -x, -y
        right, bottom = -(dw - x - ow), -(dh - y - oh)
        if (left, top, right, bottom) != (0, 0, 0, 0):
            els += [self._make("videobox", left=left, top=top, right=right, bottom=bottom, fill=0),
                    self._make("videoconvert")]
        els.append(sink)
        self._branch(tee, *els)
        self.status["display"] = f"{kind} {dw}x{dh}{' ' + mode_note if mode_note else ''} · canvas at {x},{y} ({ow}x{oh})"

    def _build_audio(self, audio, low_latency: bool = False) -> Any:
        rate_caps = "audio/x-raw,rate=48000,channels=2"
        src = self._make("audiotestsrc", is_live=True, wave=4)  # silence
        if low_latency:  # default 1024-sample buffers add ~21 ms of latency
            src.set_property("samplesperbuffer", 240)
        self.amix = self._make("audiomixer", "amix")
        atee = self._make("tee", "atee", allow_not_linked=True)
        cf = self._caps(rate_caps + ",format=F32LE")
        self._chain(src, cf)
        self.pipeline.add(self.amix)
        pad = self._req(self.amix)
        cf.get_static_pad("src").link(pad)
        self.pipeline.add(atee)
        self.amix.link(atee)
        kind = audio.get("sink", "auto")
        self._audio_note = "on"
        if kind != "none":
            device = audio.get("device") or ""
            hdmi = hdmi_alsa_device(self._display_port)
            if kind == "auto" and not audio_server_running() and (device or hdmi):
                kind = "alsa"  # no desktop sound server: talk to ALSA directly
            if kind == "alsa" and not device and hdmi and display_accepts_audio(self._display_port) is False:
                # vc4 refuses to open HDMI audio (error -524) when the display has none
                self._audio_note = (f"off: the display on {self._display_port} doesn't accept audio "
                                    f"over HDMI (per its EDID). Use its audio input or set an ALSA device.")
                self._error(self._audio_note)
                return atee
            if kind == "alsa":
                sink = self._make("alsasink", "audio_sink")
                device = device or hdmi or ""
                if device:
                    sink.set_property("device", device)
                # Keep the system clock as the pipeline clock, same as without audio
                sink.set_property("provide-clock", False)
                self._audio_note = f"on (alsa {device or 'default'}" + (
                    f", follows video on {self._display_port})" if device == hdmi and hdmi else ")")
            elif kind == "pulse":
                sink = self._make("pulsesink", "audio_sink")
                self._audio_note = "on (pulse)"
            else:
                sink = self._make("autoaudiosink", "audio_sink")
                self._audio_note = "on (auto)"
            self._branch(atee, self._make("queue", leaky=2), self._make("audioconvert"),
                         self._make("audioresample"), sink)
        else:
            self._audio_note = "on (no local output)"
        return atee

    def _build_ndi(self, tee, atee, ndi) -> None:
        name = ndi.get("name") or "CuePlayer"
        sink = self._make("ndisink", "ndi_sink", ndi_name=name)
        # videoconvert is a passthrough when ndisink accepts I420 (it does), so no per-frame cost
        vq = [self._make("queue", leaky=2, max_size_buffers=2), self._make("videoconvert")]
        if atee is not None and self._has("ndisinkcombiner"):
            comb = self._make("ndisinkcombiner")
            self._branch(tee, *vq)
            self.pipeline.add(comb)
            self.pipeline.add(sink)
            vq[-1].get_static_pad("src").link(comb.get_static_pad("video"))
            comb.link(sink)
            aq = [self._make("queue", leaky=2), self._make("audioconvert"),
                  self._caps("audio/x-raw,format=F32LE,layout=interleaved")]
            self._branch(atee, *aq)
            aq[-1].get_static_pad("src").link((comb.request_pad_simple("audio") if hasattr(comb, "request_pad_simple") else comb.get_request_pad("audio")))
        else:
            self._branch(tee, *vq, sink)

    # ---- AES67 ----------------------------------------------------------------
    def _ptp_clock(self, domain: int, iface: str) -> Any:
        """GstPtpClock for `domain`, initialising PTP on first use. Raises on failure."""
        if not HAVE_GSTNET:
            raise RuntimeError("GstNet (gir1.2-gstreamer-1.0) not available")
        if self._ptp_init is None:
            if hasattr(GstNet, "ptp_is_supported") and not GstNet.ptp_is_supported():
                self._ptp_init = False
            else:
                none = getattr(GstNet, "PTP_CLOCK_ID_NONE", 2 ** 64 - 1)
                # Needs gst-ptp-helper with CAP_NET_BIND_SERVICE (ports 319/320), see install.sh
                self._ptp_init = bool(GstNet.ptp_init(none, [iface] if iface else None))
        if not self._ptp_init:
            raise RuntimeError("PTP init failed - gst-ptp-helper needs cap_net_bind_service,"
                               "cap_net_admin (sudo deploy/install.sh sets it); restart after fixing")
        clock = self._ptp_clocks.get(domain)
        if clock is None:
            clock = GstNet.PtpClock.new(f"ptp-clock-{domain}", domain)
            if clock is None:
                raise RuntimeError(f"could not create a PTP clock for domain {domain}")
            self._ptp_clocks[domain] = clock
        return clock

    def _ptp_ids(self, clock) -> Tuple[Optional[str], Optional[str]]:
        try:
            return (aes67.clock_id_str(clock.get_property("grandmaster-clock-id")),
                    aes67.clock_id_str(clock.get_property("master-clock-id")))
        except Exception:
            return None, None

    def _setup_aes67(self, atee, cfg: Dict[str, Any], fps: int) -> None:
        st = self.status["aes67"]
        if atee is None:
            st["state"] = "needs Audio enabled"
            return
        if "aes67" in self._disabled_outputs:
            st["state"] = "disabled after repeated errors (save settings to retry)"
            return
        domain = int(cfg.get("ptp_domain", 0))
        try:
            clock = self._ptp_clock(domain, cfg.get("interface", ""))
        except Exception as e:
            st["state"] = f"error: {e}"
            self._error(f"aes67: {e}")
            return
        # Poll sync state / grandmaster for status, and to finish a pending start.
        self._ptp_timer = GLib.timeout_add_seconds(1, self._ptp_poll, clock)
        if not clock.is_synced():
            # The pipeline clock can't be swapped while running, so play without AES67 now
            # and rebuild once PTP has locked (one short blank on the screen).
            self._ptp_waiting = True
            st["state"] = f"waiting for PTP sync (domain {domain})"
            return
        self._ptp_waiting = False
        try:
            self._build_aes67(atee, cfg, clock, fps)
        except Exception as e:
            st["state"] = f"error: {e}"
            self._error(f"aes67: {e}")

    def _build_aes67(self, atee, cfg: Dict[str, Any], clock, fps: int) -> None:
        r = aes67.resolve(cfg, self._screen_name(self.settings))
        ch, depth, ptime = int(r["channels"]), int(r["bit_depth"]), float(r["ptime_ms"])
        ptime_ns = int(ptime * Gst.MSECOND)
        grid = 125 * Gst.USECOND  # 6 samples at 48 kHz: keeps the RTP maths exact

        # Fixed pipeline latency, so the time a packet leaves is known in advance. It covers
        # the display's processing deadline plus a couple of frames of compositing.
        lat = (int(self.settings["output"].get("latency_ms", 100)) + 20) * Gst.MSECOND \
            + 2 * Gst.SECOND // max(1, fps)
        lat = -(-lat // grid) * grid
        # Run the whole pipeline on PTP time. A buffer with running time t is sent at PTP time
        # base + t + latency; that is also its RTP timestamp, so rtptime = PTP time x 48 kHz
        # (mod 2^32) at the moment it leaves - what direct=0 receivers expect.
        base = clock.get_time()
        base -= base % grid
        ts_offset = ((base + lat) * aes67.SAMPLE_RATE // Gst.SECOND) % (1 << 32)

        fmt, pay_name = ("S24BE", "rtpL24pay") if depth == 24 else ("S16BE", "rtpL16pay")
        pay = self._make(pay_name, pt=aes67.PAYLOAD_TYPE,
                         mtu=aes67.RTP_HEADER + aes67.payload_bytes(ch, depth, ptime),
                         min_ptime=ptime_ns, max_ptime=ptime_ns, ptime_multiple=ptime_ns,
                         timestamp_offset=ts_offset)
        sink = self._make("udpsink", "aes67_sink", host=r["multicast"], port=int(r["port"]),
                          auto_multicast=True, ttl_mc=int(r["ttl"]), qos_dscp=int(r["dscp"]),
                          sync=True)
        sink.set_property("async", False)
        if r.get("interface"):
            sink.set_property("multicast-iface", r["interface"])
        self._branch(atee, self._make("queue", leaky=2), self._make("audioconvert"),
                     self._make("audioresample"),
                     self._caps(f"audio/x-raw,format={fmt},rate={aes67.SAMPLE_RATE},"
                                f"channels={ch},layout=interleaved"),
                     pay, sink)
        self.pipeline.use_clock(clock)
        self.pipeline.set_start_time(Gst.CLOCK_TIME_NONE)
        self.pipeline.set_base_time(base)
        self.pipeline.set_latency(lat)

        gm, master = self._ptp_ids(clock)
        self._aes67_cfg = r
        self.status["aes67"].update(
            state=f"on ({r['multicast']}:{r['port']} L{depth}/{ch}ch/{ptime:g}ms)", synced=True,
            grandmaster=gm, master=master, multicast=r["multicast"], latency_ms=lat / Gst.MSECOND,
            sdp_version=int(time.time()))
        if r.get("sap", True):
            self._sap = aes67.SapAnnouncer(self.aes67_sdp, r["source_ip"], int(r["ttl"])).start()
        log.info("AES67 stream %s:%s PTP domain %s grandmaster %s", r["multicast"], r["port"],
                 r["ptp_domain"], gm)

    def _ptp_poll(self, clock) -> bool:
        st = self.status.get("aes67") or {}
        synced = bool(clock.is_synced())
        gm, master = self._ptp_ids(clock)
        if self._ptp_waiting:
            if synced:
                log.info("PTP synced (grandmaster %s) - starting AES67 output", gm)
                self._ptp_timer = None
                self._safe_build(self.settings)
                return False
            return True
        if gm != st.get("grandmaster") and gm:
            st["sdp_version"] = int(time.time())  # SDP changes with the grandmaster
        st.update(synced=synced, grandmaster=gm or st.get("grandmaster"), master=master)
        if self._aes67_cfg is not None:
            r = self._aes67_cfg
            st["state"] = (f"on ({r['multicast']}:{r['port']})" if synced
                           else "on, PTP sync lost (free-running)")
        return True

    # ---- preview -------------------------------------------------------------
    def _on_preview(self, sink) -> Any:
        sample = sink.emit("pull-sample")
        if sample:
            buf = sample.get_buffer()
            ok, info = buf.map(Gst.MapFlags.READ)
            if ok:
                self._preview = bytes(info.data)
                buf.unmap(info)
        return Gst.FlowReturn.OK

    # ---- clips ----------------------------------------------------------------
    def _running_time(self) -> int:
        clock = self.pipeline.get_clock()
        if clock is None:
            return 0
        return max(0, clock.get_time() - self.pipeline.get_base_time())

    def _play(self, cid, path, kind, scale_mode, fade_ms, sw_decode=False) -> None:
        if self.pipeline is None:
            self._emit(type="clip_error", clip=cid, error="pipeline not running")
            return
        out = self.settings["output"]
        fps = int(out["framerate"])
        use_audio = self.amix is not None and kind == "video"

        b = Gst.Bin.new(f"clip{cid}")
        clip = _Clip(cid, b, kind, scale_mode, fade_ms)
        clip.path = path
        src = self._make("uridecodebin", uri=Path(path).absolute().as_uri())
        if sw_decode:
            # GST_AUTOPLUG_SELECT_SKIP (2) for hardware decoders, TRY (0) for the rest
            hw = set(HW_DECODERS)
            src.connect("autoplug-select",
                        lambda _b, _p, _c, f: 2 if f.get_name() in hw else 0)
        b.add(src)
        self._own(src.get_name(), cid)

        # video chain
        vconv = self._make("videoconvert")
        vchain = [vconv]
        if kind == "image":
            freeze = self._make("imagefreeze")
            vchain += [freeze, self._caps(f"video/x-raw,framerate={fps}/1"),
                       self._make("videoconvert")]
        vq = self._make("queue", max_size_buffers=4, max_size_bytes=0, max_size_time=0)
        vchain.append(vq)
        for el in vchain:
            b.add(el)
        for a, c in zip(vchain, vchain[1:]):
            a.link(c)
        clip.vghost = Gst.GhostPad.new("vsrc", vq.get_static_pad("src"))
        b.add_pad(clip.vghost)

        achain = []
        if use_audio:
            achain = [self._make("audioconvert"), self._make("audioresample"),
                      self._caps("audio/x-raw,rate=48000,channels=2,format=F32LE"),
                      self._make("queue")]
            for el in achain:
                b.add(el)
            for a, c in zip(achain, achain[1:]):
                a.link(c)
            clip.aghost = Gst.GhostPad.new("asrc", achain[-1].get_static_pad("src"))
            b.add_pad(clip.aghost)

        linked = {"video": False, "audio": False}

        def on_pad(_src, pad):
            caps = pad.get_current_caps() or pad.query_caps(None)
            name = caps.get_structure(0).get_name() if caps and caps.get_size() else ""
            if name.startswith("video/") and not linked["video"]:
                linked["video"] = pad.link(vconv.get_static_pad("sink")) == Gst.PadLinkReturn.OK
                return
            if name.startswith("audio/") and achain and not linked["audio"]:
                linked["audio"] = pad.link(achain[0].get_static_pad("sink")) == Gst.PadLinkReturn.OK
                return
            # Anything else (extra streams, unwanted audio) is discarded
            sink = self._make("fakesink", sync=False)
            sink.set_property("async", False)
            b.add(sink)
            sink.sync_state_with_parent()
            pad.link(sink.get_static_pad("sink"))

        def no_more_pads(_src):
            if not linked["video"]:
                vconv.get_static_pad("sink").send_event(Gst.Event.new_eos())
            if achain and not linked["audio"]:
                achain[0].get_static_pad("sink").send_event(Gst.Event.new_eos())

        def on_element(_bin, _sub, el):
            self._own(el.get_name(), cid)
            f = el.get_factory()
            if f and "Decoder/Video" in (f.get_metadata("klass") or ""):
                clip.decoder = f.get_name()
                self.status["video_decoder"] = f.get_name()

        b.connect("deep-element-added", on_element)
        src.connect("pad-added", on_pad)
        src.connect("no-more-pads", no_more_pads)

        # Hold the new clip invisible until its first frame arrives
        self.pipeline.add(b)
        clip.vpad = self._req(self.comp)
        clip.vpad.set_property("zorder", next(self._zorder))
        clip.vpad.set_property("alpha", 0.0)
        if clip.vpad.find_property("repeat-after-eos"):
            clip.vpad.set_property("repeat-after-eos", True)
        clip.vghost.link(clip.vpad)
        if use_audio:
            clip.apad = self._req(self.amix)
            clip.apad.set_property("volume", 0.0)
            clip.aghost.link(clip.apad)

        vsrcpad = vq.get_static_pad("src")

        # Probes: first buffer -> set offset + geometry; EOS -> clip ended
        def first_buffer(pad, info):
            with clip.lock:
                if not clip.offset_set:
                    buf = info.get_buffer()
                    rt = 0
                    ev = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
                    if ev is not None and buf.pts != Gst.CLOCK_TIME_NONE:
                        seg = ev.parse_segment()
                        r = seg.to_running_time(Gst.Format.TIME, buf.pts)
                        rt = r if r != Gst.CLOCK_TIME_NONE else 0
                    # Start the clip slightly in the future so its first frame is not late
                    offset = self._running_time() - rt + 40 * Gst.MSECOND
                    clip.vghost.set_offset(offset)
                    if clip.aghost is not None:
                        clip.aghost.set_offset(offset)
                    clip.offset_set = True
            if pad is vsrcpad:
                caps = pad.get_current_caps()
                if caps:
                    s = caps.get_structure(0)
                    sw, sh = s.get_value("width"), s.get_value("height")
                    x, y, w, h = fit_rect(sw, sh, int(out["canvas_width"]),
                                          int(out["canvas_height"]), clip.scale_mode)
                    for k, v in (("xpos", x), ("ypos", y), ("width", w), ("height", h)):
                        clip.vpad.set_property(k, v)
                GLib.idle_add(lambda: (self._clip_started(clip), False)[1])
            return Gst.PadProbeReturn.REMOVE

        def eos_probe(pad, info):
            ev = info.get_event()
            if ev.type == Gst.EventType.EOS and not clip.dying:
                GLib.idle_add(lambda: (self._emit(type="clip_ended", clip=cid), False)[1])
            return Gst.PadProbeReturn.OK

        vsrcpad.add_probe(Gst.PadProbeType.BUFFER, first_buffer)
        vsrcpad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, eos_probe)
        if achain:
            achain[-1].get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, first_buffer)

        self.clips[cid] = clip
        b.sync_state_with_parent()

    def _clip_started(self, clip: _Clip) -> None:
        if clip.dying or clip.id not in self.clips:
            return
        clip.started = True
        old = self.current
        self.current = clip
        self._emit(type="clip_started", clip=clip.id)
        # Any clip other than the new one is now obsolete
        stale = [c for c in self.clips.values() if c is not clip and c is not old]
        for c in stale:
            self._release(c)
        self._fade(clip, old, clip.fade_ms)

    def _fade(self, new: Optional[_Clip], old: Optional[_Clip], fade_ms: int) -> None:
        steps = max(1, int(fade_ms / self.FADE_STEP_MS))
        state = {"i": 0}

        def tick():
            state["i"] += 1
            a = min(1.0, state["i"] / steps)
            if new is not None and not new.dying:
                new.vpad.set_property("alpha", a)
                if new.apad is not None:
                    new.apad.set_property("volume", a)
            if old is not None and not old.dying:
                # The new clip sits on top, so the old one only needs to fade
                # when we are fading to black.
                if new is None:
                    old.vpad.set_property("alpha", 1.0 - a)
                if old.apad is not None:
                    old.apad.set_property("volume", 1.0 - a)
            if a >= 1.0:
                if old is not None:
                    self._release(old)
                return False
            return True

        if fade_ms <= 0:
            state["i"] = steps - 1
            tick()
        else:
            GLib.timeout_add(self.FADE_STEP_MS, tick)

    def _black(self, fade_ms: int) -> None:
        old = self.current
        self.current = None
        for c in list(self.clips.values()):
            if c is not old:
                self._release(c)
        if old is not None:
            self._fade(None, old, fade_ms)

    def _release(self, clip: _Clip) -> None:
        if clip.dying:
            return
        clip.dying = True
        self.clips.pop(clip.id, None)
        if self.current is clip:
            self.current = None
        try:
            for ghost, req, owner in ((clip.vghost, clip.vpad, self.comp),
                                      (clip.aghost, clip.apad, self.amix)):
                if req is not None and owner is not None:
                    ghost.unlink(req)
                    owner.release_request_pad(req)
            clip.bin.set_state(Gst.State.NULL)
            if self.pipeline is not None:
                self.pipeline.remove(clip.bin)
        except Exception:
            log.exception("failed to release clip %s", clip.id)

    # ---- bus ------------------------------------------------------------------
    def _own(self, name: str, cid: int) -> None:
        """Remember which clip an element belongs to: once a clip is torn down its elements
        have no parent, but their last error messages can still be on the bus."""
        self._element_clip[name] = cid
        while len(self._element_clip) > 4000:
            self._element_clip.pop(next(iter(self._element_clip)))

    def _clip_for(self, obj) -> Optional[_Clip]:
        top = obj
        while obj is not None:
            name = obj.get_name() if hasattr(obj, "get_name") else ""
            if name.startswith("clip") and name[4:].isdigit():
                return self.clips.get(int(name[4:])) or _Clip(int(name[4:]), None, "", "", 0)
            top = obj
            obj = obj.get_parent()
        cid = self._element_clip.get(top.get_name() if hasattr(top, "get_name") else "")
        if cid is not None:
            return self.clips.get(cid) or _Clip(cid, None, "", "", 0)
        if top is not None and top is not self.pipeline:
            # Detached element that isn't part of the running pipeline: a clip already torn
            # down. Never treat it as a pipeline failure (that would restart everything).
            return _Clip(-1, None, "", "", 0)
        return None

    def _on_bus(self, _bus, msg) -> None:
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            clip = self._clip_for(msg.src)
            if clip is not None:
                if clip.bin is None or clip.dying:
                    return  # error from a clip being torn down
                hw = clip.decoder in HW_DECODERS
                log.warning("clip %s error: %s (decoder %s, %s) %s", clip.id, err.message,
                            clip.decoder or "?", Path(clip.path).name, (dbg or "").replace("\n", " "))
                self._release(clip)
                self._emit(type="clip_error", clip=clip.id, error=err.message,
                           decoder=clip.decoder, hw_decoder=hw, detail=(dbg or "")[-300:])
                return
            log.error("pipeline error: %s (%s)", err.message, dbg)
            self._error(f"{msg.src.get_name()}: {err.message}")
            self.status["running"] = False
            # A sink that keeps failing (unplugged HDMI, NDI runtime problem) is switched
            # off so the other outputs keep running instead of restart-looping.
            name = msg.src.get_name()
            parent = msg.src.get_parent()
            if parent is not None and parent.get_name() == "audio_sink":
                name = "audio_sink"  # the real sink inside autoaudiosink
            out = {"display_sink": "display", "ndi_sink": "ndi", "aes67_sink": "aes67",
                   "audio_sink": "audio"}.get(name)
            if out == "display" and not self._modeset_fallback and \
                    msg.src.find_property("force-modesetting") and \
                    not msg.src.get_property("force-modesetting"):
                log.warning("kmssink could not use the current display mode - letting it set one")
                self._modeset_fallback = True
            if out:
                self._fail_counts[out] = self._fail_counts.get(out, 0) + 1
                if self._fail_counts[out] >= 3:
                    log.error("%s output failed repeatedly - disabling it", out)
                    self._disabled_outputs.add(out)
                    self._fail_counts[out] = 0
            self._schedule_restart()
        elif t == Gst.MessageType.QOS and msg.src.get_name() == "display_sink":
            _fmt, processed, dropped = msg.parse_qos_stats()
            self.status.update(frames_rendered=processed, frames_dropped=dropped)
        elif t == Gst.MessageType.WARNING:
            w, _ = msg.parse_warning()
            log.warning("gst warning from %s: %s", msg.src.get_name(), w.message)

    def _schedule_restart(self) -> None:
        if self._restart_timer:
            return

        def restart():
            self._restart_timer = None
            log.info("restarting pipeline after error")
            self._safe_build(self.settings)
            return False

        self._restart_timer = GLib.timeout_add_seconds(5, restart)


def create_engine(on_event: EventCallback, force_mock: bool = False) -> BaseEngine:
    if HAVE_GST and not force_mock:
        return GstEngine(on_event)
    log.warning("GStreamer (python3-gi) not available - using mock engine")
    return MockEngine(on_event)
