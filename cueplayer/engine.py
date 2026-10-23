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
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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
    """(connector id, name) of the connector most likely to have the screen on it: one
    reporting "connected" with an EDID first, then "connected" without one (some PCs and
    adapters don't pass EDID through). An EDID breaks ties because video=...D on the kernel
    command line can force an empty port to "connected"."""
    best: List[Tuple[int, int, str]] = []
    for path in sorted(Path(drm).glob("card*-*")):
        try:
            status = path.joinpath("status").read_text().strip()
            cid = int(path.joinpath("connector_id").read_text().strip())
        except (OSError, ValueError):
            continue
        try:
            has_edid = path.joinpath("edid").stat().st_size > 0
        except OSError:
            has_edid = False
        if status != "connected" and not has_edid:
            continue
        rank = 0 if (status == "connected" and has_edid) else (1 if has_edid else 2)
        best.append((rank, cid, re.sub(r"^card\d+-", "", path.name)))
    if not best:
        return None
    _, cid, name = min(best)
    return cid, name


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


def connector_snapshot(drm: str = "/sys/class/drm") -> Dict[str, Tuple[str, int]]:
    """{connector: (status, EDID size)} for every display connector - for hotplug checks."""
    out: Dict[str, Tuple[str, int]] = {}
    for path in Path(drm).glob("card*-*"):
        try:
            status = path.joinpath("status").read_text().strip()
        except OSError:
            continue
        try:
            size = path.joinpath("edid").stat().st_size
        except OSError:
            size = 0
        out[re.sub(r"^card\d+-", "", path.name)] = (status, size)
    return out


def newly_connected(before: Dict[str, Tuple[str, int]], after: Dict[str, Tuple[str, int]]) -> List[str]:
    """Connectors that gained a screen: became "connected", or their EDID appeared/changed
    (a different screen). Unplugging alone needs no action."""
    out = []
    for name, (status, size) in after.items():
        old = before.get(name, ("disconnected", 0))
        if status == "connected" and old[0] != "connected":
            out.append(name)
        elif size and size != old[1]:
            out.append(name)
    return out


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


def hdmi_jack_devices(card: str) -> List[int]:
    """PCM devices whose "HDMI/DP,pcm=N Jack" control reads on (a screen is connected),
    from amixer. This is the kernel's own PCM-to-port mapping on Intel/AMD HDA cards."""
    amixer = shutil.which("amixer")
    if not amixer:
        return []
    try:
        listing = subprocess.run([amixer, "-c", card, "controls"], capture_output=True,
                                 text=True, timeout=5).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    on = []
    for numid, dev in re.findall(r"numid=(\d+),iface=CARD,name='HDMI/DP,pcm=(\d+) Jack'", listing):
        try:
            val = subprocess.run([amixer, "-c", card, "cget", f"numid={numid}"], capture_output=True,
                                 text=True, timeout=5).stdout
        except (subprocess.SubprocessError, OSError):
            continue
        if re.search(r":\s*values=on", val):
            on.append(int(dev))
    return on


def eld_alsa_device(asound: str = "/proc/asound",
                    jacks: Optional[Callable[[str], List[int]]] = None) -> Optional[Tuple[str, str]]:
    """(ALSA device, screen name) for the HDMI/DP output whose screen reports audio support
    (ELD valid), on cards like Intel/AMD HDA where several HDMI PCMs share one card.

    Some kernels name a connected HDMI PCM after the screen, which identifies it directly;
    otherwise the "HDMI/DP,pcm=N Jack" controls say which PCM has the screen.
    plughw handles format conversion.
    """
    jacks = jacks or hdmi_jack_devices
    root = Path(asound)
    for card in sorted(root.glob("card[0-9]*")):
        names = []
        for eld in sorted(card.glob("eld#*")):
            try:
                info = dict(line.split(None, 1) for line in eld.read_text().splitlines()
                            if len(line.split(None, 1)) == 2)
            except OSError:
                continue
            if info.get("monitor_present", "").strip() == "1" and info.get("eld_valid", "").strip() == "1":
                names.append(info.get("monitor_name", "").strip())
        if not names:
            continue
        try:
            card_id = card.joinpath("id").read_text().strip()
        except OSError:
            continue
        screen = names[0] or "HDMI"
        for pcm in sorted(card.glob("pcm*p")):
            try:
                info = pcm.joinpath("info").read_text()
            except OSError:
                continue
            m = re.search(r"^name:\s*(.+)$", info, re.M)
            dev = re.search(r"^device:\s*(\d+)$", info, re.M)
            if m and dev and names[0] and m.group(1).strip() in names:
                return f"plughw:CARD={card_id},DEV={dev.group(1)}", m.group(1).strip()
        devs = jacks(card_id)
        if devs:
            return f"plughw:CARD={card_id},DEV={devs[0]}", screen
    return None


def unmute_digital_outputs(card: str) -> List[str]:
    """Switch on every IEC958 (S/PDIF / HDMI) control of an ALSA card. On Intel/AMD HDA they
    start muted, and without a desktop sound server nothing turns them on - HDMI audio is
    then silent even though the right device is playing. Returns the controls switched on."""
    amixer = shutil.which("amixer")
    if not amixer:
        return []
    try:
        out = subprocess.run([amixer, "-c", card, "scontrols"], capture_output=True, text=True,
                             timeout=5).stdout
    except (subprocess.SubprocessError, OSError):
        return []
    done = []
    for name, idx in re.findall(r"Simple mixer control '(IEC958[^']*)',(\d+)", out):
        try:
            subprocess.run([amixer, "-c", card, "-q", "sset", f"{name},{idx}", "on"],
                           capture_output=True, timeout=5)
            done.append(f"{name},{idx}")
        except (subprocess.SubprocessError, OSError):
            pass
    return done


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


# Test patterns: name -> (videotestsrc pattern nick, label). Generated at the exact canvas
# size, so every pixel of the LED map is exercised.
TEST_PATTERNS: Dict[str, Tuple[str, str]] = {
    "smpte": ("smpte", "SMPTE bars"),
    "smpte-hd": ("smpte-rp-219", "SMPTE HD bars (RP 219)"),
    "smpte75": ("smpte75", "SMPTE 75% bars"),
    "smpte100": ("smpte100", "SMPTE 100% bars"),
    "checkers": ("checkers-8", "Checkerboard (8 px)"),
    "checkers1": ("checkers-1", "Checkerboard (1 px)"),
    "gradient": ("gradient", "Gradient"),
    "white": ("white", "White"),
    "red": ("red", "Red"),
    "green": ("green", "Green"),
    "blue": ("blue", "Blue"),
    "black": ("black", "Black"),
    "ball": ("ball", "Moving ball (motion / cadence)"),
}


TRANSITIONS = ("cut", "fade", "dip", "slide-left", "slide-right", "slide-up", "slide-down")

YUV_FORMATS = {"NV12", "NV21", "I420", "YV12", "Y42B", "Y444", "NV16", "NV24"}


CHROMA_START = {"NV12": 2 / 3, "NV21": 2 / 3, "I420": 2 / 3, "YV12": 2 / 3,
                "Y42B": 1 / 2, "NV16": 1 / 2, "Y444": 1 / 3, "NV24": 1 / 3}


def looks_blank_yuv(read: Callable[[int, int], bytes], size: int, fmt: str) -> bool:
    """True for a frame whose colour (chroma) data is zero, which displays as green - fully
    if the brightness is zero too, as a green cast if the frame is only partly decoded.
    Some hardware decoders hand these out around the start of a stream. Reads three 512-byte
    chunks across the chroma plane; a real picture never has chroma 0 (that would be
    beyond-saturated green) in nearly all of them - even pure green graphics don't."""
    if fmt not in YUV_FORMATS or size < 8192:
        return False
    start = int(size * CHROMA_START.get(fmt, 2 / 3))
    span = size - start
    zeros = total = 0
    for frac in (0.25, 0.5, 0.75):
        chunk = read(start + int(span * frac) - 256, 512)
        zeros += chunk.count(0)
        total += len(chunk)
    return total > 0 and zeros >= 0.95 * total


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
             duration_hint: float = 0, sw_decode: bool = False, transition: str = "fade",
             preroll_ms: Optional[int] = None) -> int:
        """sw_decode: don't use hardware decoders for this clip (after one failed on it)."""
        raise NotImplementedError

    def black(self, fade_ms: int) -> None: ...

    def play_pattern(self, pattern: str, fade_ms: int, label: Optional[str] = None,
                     tone: bool = False, transition: str = "fade") -> int:
        """Show a test pattern (TEST_PATTERNS key) on the whole canvas."""
        raise NotImplementedError

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

    def play(self, path, kind, scale_mode, fade_ms, duration_hint=0, sw_decode=False,
             transition="fade", preroll_ms=None):
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

    def play_pattern(self, pattern, fade_ms, label=None, tone=False, transition="fade"):
        cid = next(self._ids)
        for t in self._timers.values():
            t.cancel()
        self._timers.clear()
        threading.Timer(0.05, lambda: self._emit(type="clip_started", clip=cid)).start()
        return cid


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
        self.checked = 0          # frames checked for blank (green) output
        self.due_rt = 0           # running time its first frame is scheduled for
        self.preroll_ms: Optional[int] = None  # None = the output.preroll_ms setting
        self.blank_dropped = 0
        self.transition = "fade"
        self.geom = (0, 0, 0, 0)  # x, y, w, h on the canvas
        self.lock = threading.Lock()


class GstEngine(BaseEngine):
    FADE_STEP_MS = 20
    BLANK_CHECK_FRAMES = 180  # frame checks at the start of each clip (~3 s; each frame is checked twice)

    def __init__(self, on_event: EventCallback):
        super().__init__(on_event)
        Gst.init(None)
        self.status.update(backend="gstreamer", gst_version=Gst.version_string())
        self.settings: Dict[str, Any] = {}
        self.pipeline = None
        self.comp = None
        self.amix = None
        self.clips: Dict[int, _Clip] = {}
        self.current: Optional[_Clip] = None   # newest started clip (may still be pre-rolling)
        self.visible: Optional[_Clip] = None   # clip actually on screen
        self._zorder = itertools.count(1)
        self._ctx = GLib.MainContext.default()
        self._loop = GLib.MainLoop.new(self._ctx, False)
        self._thread = threading.Thread(target=self._loop.run, name="glib", daemon=True)
        self._thread.start()
        self._restart_timer = None
        self._disabled_outputs: set = set()   # outputs switched off after repeated failures
        self._orig_ranks: Dict[str, int] = {}
        self._element_clip: Dict[str, int] = {}  # element name -> clip id
        self._frames: Dict[str, int] = {}        # buffers seen per stage (diagnostics)
        self._frames_prev: Dict[str, int] = {}
        self._rate_timer = None
        self._hotplug_timer = None
        self._hotplug_snap: Dict[str, Tuple[str, int]] = {}
        self._hotplug_pending: Optional[Dict[str, Tuple[str, int]]] = None
        self._display_attempt = 0       # index into the display start-up strategies
        self._display_attempts = 1
        self._display_port: Optional[str] = None   # e.g. HDMI-A-2, for matching HDMI audio
        self._audio_note = "on"
        self._fail_counts: Dict[str, int] = {}
        self._ptp_init: Optional[bool] = None   # gst_ptp_init() result (it can only run once)
        self._ptp_iface = ""                    # interface PTP was started on
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
        self._display_attempt = 0
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

    def play(self, path, kind, scale_mode, fade_ms, duration_hint=0, sw_decode=False,
             transition="fade", preroll_ms=None):
        cid = next(self._ids)
        self._idle(self._play, cid, path, kind, scale_mode, fade_ms, sw_decode, None, False,
                   transition, preroll_ms)
        return cid

    def play_pattern(self, pattern, fade_ms, label=None, tone=False, transition="fade"):
        cid = next(self._ids)
        self._idle(self._play, cid, pattern, "pattern", "stretch", fade_ms, False, label, tone,
                   transition)
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
        # Converters run single-threaded by default; a Pi has 4 cores, and a 1080p
        # format conversion on one ARM core is slow enough to halve the HDMI frame rate.
        if factory in ("videoconvert", "videoscale", "videoconvertscale") and el.find_property("n-threads") \
                and "n_threads" not in props:
            el.set_property("n-threads", max(1, os.cpu_count() or 1))
        # Cheaper resampling (only used when a file isn't 48 kHz); default 4 is costly on ARM
        if factory == "audioresample" and el.find_property("quality") and "quality" not in props:
            el.set_property("quality", 2)
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

    # ---- HDMI hotplug ----------------------------------------------------------------
    @staticmethod
    def _uses_kms(disp: Dict[str, Any]) -> bool:
        kind = disp.get("sink", "auto")
        return kind == "kms" or (kind == "auto" and not os.environ.get("WAYLAND_DISPLAY")
                                 and not os.environ.get("DISPLAY"))

    def _waiting_for_screen(self, disp: Dict[str, Any]) -> bool:
        """KMS output, connector auto, and the kernel reports connectors but none with a
        screen. (No connector info at all, e.g. in a VM: try anyway.)"""
        if not self._uses_kms(disp) or int(disp.get("connector_id") or 0) > 0:
            return False
        return bool(connector_snapshot()) and detect_display_connector() is None

    def _hotplug_poll(self) -> bool:
        """Every 2 s: when a screen is plugged in (or swapped), reconnect HDMI to it. A change
        must be seen twice in a row first, so the EDID has been read."""
        snap = connector_snapshot()
        if snap != self._hotplug_snap:
            if snap != self._hotplug_pending:
                self._hotplug_pending = snap  # wait for it to settle
                return True
            gained = newly_connected(self._hotplug_snap, snap)
            self._hotplug_snap, self._hotplug_pending = snap, None
            disp = (self.settings or {}).get("display") or {}
            if gained and disp.get("enabled") and self._uses_kms(disp) and self.pipeline is not None:
                log.info("screen connected on %s - reconnecting HDMI", ", ".join(gained))
                self._display_attempt = 0
                self._fail_counts.pop("display", None)
                self._disabled_outputs.discard("display")
                self._disabled_outputs.discard("audio")  # audio follows the HDMI port
                self._emit(type="notice", message=f"Screen connected on {gained[0]} - reconnecting HDMI")
                self._safe_build(self.settings)
        else:
            self._hotplug_pending = None
        return True

    # ---- diagnostics: frames per second at each stage -----------------------------
    def _count(self, pad, key: str) -> None:
        def probe(_pad, _info):
            self._frames[key] = self._frames.get(key, 0) + 1
            return Gst.PadProbeReturn.OK
        pad.add_probe(Gst.PadProbeType.BUFFER, probe)

    def _update_rates(self) -> bool:
        """Once a second: frames that passed each stage in the last second. A stage at 0
        while the one before it is running shows where the pipeline is stuck."""
        now = dict(self._frames)
        self.status["fps"] = {k: now.get(k, 0) - self._frames_prev.get(k, 0)
                              for k in ("clip", "composited", "display", "display_drops")}
        self._frames_prev = now
        self.status["blank_frames_hidden"] = now.get("blank", 0)
        return True

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
        self.current = self.visible = None
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
        # Always run on the system clock. GStreamer otherwise prefers an audio sink's clock
        # (autoaudiosink -> alsasink/pulsesink), and a sound device that plays in bursts or
        # stalls (e.g. HDMI audio to a screen without EDID) then makes the video judder or
        # freeze. Audio sinks slave to this clock instead. AES67 replaces it with PTP below.
        self.pipeline.use_clock(Gst.SystemClock.obtain())

        # --- video core ---
        bg = self._make("videotestsrc", is_live=True, pattern=17,  # solid-color
                        foreground_color=parse_colour(out.get("background")))
        self.comp = self._make("compositor", "comp", background=1)
        if self.comp.find_property("ignore-inactive-pads"):
            self.comp.set_property("ignore-inactive-pads", True)
        # Wait this long for a clip frame that's running late (e.g. two decoders busy during
        # a change) instead of drawing the background for it - that showed as black frames
        if self.comp.find_property("latency"):
            self.comp.set_property("latency", int(out.get("latency_ms", 100)) * Gst.MSECOND)
        tee = self._make("tee", "vtee", allow_not_linked=True)
        # Compose in NV12: hardware decoders (Pi V4L2, VA-API) produce it and display planes
        # (Intel, Pi vc4) scan it out directly, so the HDMI path needs no per-frame
        # conversion. The compositor skips the background whenever an opaque full-canvas
        # clip covers it. (Fades still work through the pad alpha.)
        bgcaps = self._caps(vcaps + ",format=NV12")
        outcaps = self._caps(vcaps + ",format=NV12")
        self._chain(bg, bgcaps)
        self.pipeline.add(self.comp)
        bgpad = self._req(self.comp)
        bgpad.set_property("zorder", 0)
        bgcaps.get_static_pad("src").link(bgpad)
        self._chain(outcaps, tee)
        self._count(outcaps.get_static_pad("src"), "composited")
        self.comp.link(outcaps)

        # clock branch: keeps the pipeline paced even with no other outputs
        self._branch(tee, self._make("queue", leaky=2, max_size_buffers=2), self._fakesink())

        # --- display ---
        self.status["display"] = "disabled"
        if "display" in self._disabled_outputs:
            self.status["display"] = "disabled after repeated errors (save settings to retry)"
        elif disp.get("enabled") and disp.get("sink") != "none" and self._waiting_for_screen(disp):
            # Nothing to drive yet (e.g. powered on before the screen was connected): run
            # without HDMI; the hotplug check rebuilds as soon as a screen appears.
            self.status["display"] = "waiting for a screen to be connected"
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
        if self._rate_timer is None:
            self._rate_timer = GLib.timeout_add_seconds(1, self._update_rates)
        if self._hotplug_timer is None:
            self._hotplug_snap = connector_snapshot()
            self._hotplug_timer = GLib.timeout_add_seconds(2, self._hotplug_poll)
        log.info("pipeline started %s display=%s ndi=%s", self.status["canvas"],
                 self.status["display"], self.status["ndi"])
        self._emit(type="engine_started")

    def _sink_queue(self) -> Any:
        """Queue in front of a synchronised output (HDMI, NDI). Frames wait there until their
        display time - the pipeline latency, which grows to ~1/3 s with an audio sink - so it
        must hold that much time. A 3-frame limit dropped half the frames without audio and
        most of them with audio on. Still leaky, so a stuck output can't stall the others."""
        # latency budget is used twice (compositor wait + sink processing deadline), plus
        # the audio sink's buffer and headroom
        ms = 2 * int(self.settings["output"].get("latency_ms", 100)) + 400
        return self._make("queue", leaky=2, max_size_buffers=0, max_size_bytes=0,
                          max_size_time=ms * Gst.MSECOND)

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
        self.status.pop("display_format", None)
        self.status.pop("display_refresh", None)
        if kind == "kms":
            # kmssink's own modesetting picks the first mode with the right resolution,
            # which is nearly always 60 Hz. "keep" reuses the mode set at boot
            # (install.sh --take-hdmi) so its refresh rate is honoured - experimental: on
            # some Pi setups kmssink then draws nothing, so it's opt-in.
            boot = [m for m in boot_video_modes().values() if m[:2] == (dw, dh)]
            # Ways to drive the screen, tried in turn when the display output fails to start
            # (e.g. a screen without EDID has no mode list, so setting a mode is impossible
            # but reusing the console's current mode works).
            if disp.get("modeset", "auto") == "keep":
                attempts = [(False, True), (True, True), (True, False), (False, False)]
            else:
                attempts = [(True, True), (True, False), (False, False), (False, True)]
            self._display_attempts = len(attempts)
            modeset, pick_port = attempts[min(self._display_attempt, len(attempts) - 1)]
            keep = not modeset
            sink = self._make("kmssink", force_modesetting=modeset)
            port_note = ""
            found = detect_display_connector()
            if found:
                self._display_port = found[1]
            if int(disp.get("connector_id") or 0) > 0:
                sink.set_property("connector-id", int(disp["connector_id"]))
                port_note = f"connector {int(disp['connector_id'])}"
            elif pick_port and found:
                sink.set_property("connector-id", found[0])
                port_note = found[1]
            if self._display_attempt:
                port_note = (port_note + " " if port_note else "") + f"(fallback {self._display_attempt})"
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

        dq = self._sink_queue()
        # A leaky queue drops silently when the sink can't keep up: count it
        dq.connect("overrun", lambda *_: self._frames.__setitem__(
            "display_drops", self._frames.get("display_drops", 0) + 1))
        els = [dq, self._make("videoconvert")]
        if (ow, oh) != (cw, ch):
            els += [self._make("videoscale"), self._caps(f"video/x-raw,width={ow},height={oh}")]
        # videobox: negative = add border, positive = crop. Places the canvas
        # at (x, y) inside a dw x dh raster.
        left, top = -x, -y
        right, bottom = -(dw - x - ow), -(dh - y - oh)
        if (left, top, right, bottom) != (0, 0, 0, 0):
            els += [self._make("videobox", left=left, top=top, right=right, bottom=bottom, fill=0),
                    self._make("videoconvert")]
        if kind == "kms":
            # Prefer formats display planes take as-is (NV12 first) ...
            els.append(self._caps("video/x-raw,format={ NV12, YUY2, UYVY, BGRx, BGRA, RGBx, xRGB }"))
            # ... and keep converters from writing straight into kmssink's dumb buffers.
            # Those are uncached (write-combined), so converting into them pixel by pixel is
            # very slow - HDMI ran at half rate while NDI was fine. kmssink instead copies
            # each finished frame across in one sequential pass.
            ident = self._make("identity")
            if ident.find_property("drop-allocation"):
                ident.set_property("drop-allocation", True)
                els.append(ident)
        els.append(sink)
        self._branch(tee, *els)
        self._count(sink.get_static_pad("sink"), "display")

        def caps_probe(_pad, info):
            ev = info.get_event()
            if ev.type == Gst.EventType.CAPS:
                st = ev.parse_caps().get_structure(0)
                fmt = st.get_value("format") or "?"
                canvas = "NV12"
                self.status["display_format"] = fmt if fmt == canvas and (ow, oh) == (cw, ch) \
                    else f"{fmt} (converted from {canvas} {cw}x{ch} on the CPU)"
            return Gst.PadProbeReturn.OK
        sink.get_static_pad("sink").add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, caps_probe)
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
            hdmi = hdmi_alsa_device(self._display_port)   # Pi: one card per HDMI port
            hdmi_note = f"follows video on {self._display_port}" if hdmi else ""
            if not hdmi:
                found = eld_alsa_device()                    # Intel/AMD: the port with a screen
                if found:
                    hdmi, hdmi_note = found[0], f"HDMI to screen '{found[1]}'"
                    card = re.search(r"CARD=([^,]+)", hdmi)
                    if card and kind in ("auto", "alsa") and not device:
                        switched = unmute_digital_outputs(card.group(1))
                        if switched:
                            log.info("unmuted %s on card %s", ", ".join(switched), card.group(1))
            if kind == "auto" and not audio_server_running() and (device or hdmi):
                kind = "alsa"  # no desktop sound server: talk to ALSA directly
            if kind == "alsa" and not device and hdmi and hdmi.startswith("hdmi:CARD=vc4") \
                    and display_accepts_audio(self._display_port) is False:
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
                    f", {hdmi_note})" if device == hdmi and hdmi else ")")
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
        # videoconvert is a passthrough when ndisink accepts the canvas format (NV12), so no per-frame cost
        vq = [self._sink_queue(), self._make("videoconvert")]
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
                self._ptp_iface = iface
        if self._ptp_init and iface != self._ptp_iface:
            # GStreamer's PTP can only be set up once per process
            raise RuntimeError(f"PTP is running on {self._ptp_iface or 'the default interface'}; "
                               f"restart the CuePlayer service to move it to {iface or 'the default'} "
                               "(sudo systemctl restart cueplayer)")
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
        pay = self._make(pay_name, pt=int(r.get("payload_type") or aes67.PAYLOAD_TYPE),
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

    def _play(self, cid, path, kind, scale_mode, fade_ms, sw_decode=False,
              label: Optional[str] = None, tone: bool = False, transition: str = "fade",
              preroll_ms: Optional[int] = None) -> None:
        if self.pipeline is None:
            self._emit(type="clip_error", clip=cid, error="pipeline not running")
            return
        out = self.settings["output"]
        fps = int(out["framerate"])
        pattern = kind == "pattern"
        use_audio = self.amix is not None and (kind == "video" or (pattern and tone))

        b = Gst.Bin.new(f"clip{cid}")
        clip = _Clip(cid, b, kind, scale_mode, fade_ms)
        clip.path = path
        clip.transition = transition if transition in TRANSITIONS else "fade"
        clip.preroll_ms = preroll_ms
        if pattern:
            src = self._pattern_source(b, path, label, out)
        else:
            src = self._make("uridecodebin", uri=Path(path).absolute().as_uri())
        if sw_decode and not pattern:
            # GST_AUTOPLUG_SELECT_SKIP (2) for hardware decoders, TRY (0) for the rest
            hw = set(HW_DECODERS)
            src.connect("autoplug-select",
                        lambda _b, _p, _c, f: 2 if f.get_name() in hw else 0)
        if not pattern:
            b.add(src)
        self._own(src.get_name(), cid)

        # video chain
        vconv = self._make("videoconvert")
        vchain = [vconv]
        if kind == "image":
            freeze = self._make("imagefreeze")
            vchain += [freeze, self._caps(f"video/x-raw,framerate={fps}/1"),
                       self._make("videoconvert")]
        # No QoS frame-dropping inside clips: the pad offset makes lateness reports from the
        # compositor misleading, and a converter could then discard every frame (a test
        # pattern froze after a second this way). The compositor handles late frames itself.
        for el in vchain:
            if el.find_property("qos"):
                el.set_property("qos", False)
        vq = self._make("queue", max_size_buffers=4, max_size_bytes=0, max_size_time=0)
        vchain.append(vq)
        for el in vchain:
            b.add(el)
        for a, c in zip(vchain, vchain[1:]):
            a.link(c)
        vout = vq
        if kind == "video":
            # Gap filler: a frame that's missing (late, or dropped as green/corrupt) is
            # replaced by a repeat of the previous one rather than leaving a hole
            rate = self._make("videorate", skip_to_first=True)
            for prop, val in (("qos", False), ("max-duplication-time", 500 * Gst.MSECOND)):
                if rate.find_property(prop):
                    rate.set_property(prop, val)
            b.add(rate)
            vq.link(rate)
            vout = rate
        clip.vghost = Gst.GhostPad.new("vsrc", vout.get_static_pad("src"))
        b.add_pad(clip.vghost)
        if pattern:
            src.link(vconv)

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
            if pattern:  # 1 kHz line-up tone at -20 dBFS
                tsrc = self._make("audiotestsrc", wave=0, freq=1000.0, volume=0.1)
                b.add(tsrc)
                tsrc.link(achain[0])

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
        if not pattern:
            src.connect("pad-added", on_pad)
            src.connect("no-more-pads", no_more_pads)

        # Hold the new clip invisible until its first frame arrives
        self.pipeline.add(b)
        clip.vpad = self._req(self.comp)
        # Let the compositor convert/scale this input on all cores too
        if clip.vpad.find_property("converter-config"):
            try:
                clip.vpad.set_property("converter-config", Gst.Structure.new_from_string(
                    f"config, GstVideoConverter.threads=(uint){max(1, os.cpu_count() or 1)}"))
            except (TypeError, ValueError):
                pass
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
        def bad_frame(pad, buf) -> bool:
            if buf.get_flags() & Gst.BufferFlags.CORRUPTED:
                return True
            # Check the start of every clip, and every clip while another is starting (two
            # decoders at once is when damaged frames appear in the one already on screen)
            if clip.checked >= self.BLANK_CHECK_FRAMES and len(self.clips) <= 1:
                return False
            clip.checked += 1
            caps = pad.get_current_caps()
            fmt = caps.get_structure(0).get_value("format") if caps and caps.get_size() else ""
            return looks_blank_yuv(lambda off, n: buf.extract_dup(off, n), buf.get_size(), fmt or "")

        def first_buffer(pad, info):
            if pad is vsrcpad and bad_frame(pad, info.get_buffer()):
                clip.blank_dropped += 1
                self._frames["blank"] = self._frames.get("blank", 0) + 1
                return Gst.PadProbeReturn.DROP  # keep waiting for a real picture
            with clip.lock:
                if not clip.offset_set:
                    buf = info.get_buffer()
                    rt = 0
                    ev = pad.get_sticky_event(Gst.EventType.SEGMENT, 0)
                    if ev is not None and buf.pts != Gst.CLOCK_TIME_NONE:
                        seg = ev.parse_segment()
                        r = seg.to_running_time(Gst.Format.TIME, buf.pts)
                        rt = r if r != Gst.CLOCK_TIME_NONE else 0
                    # Pre-roll: schedule the first frame this far ahead, so the decoder can
                    # settle and buffer while the current item keeps playing. The switch
                    # happens once that frame is due (see _clip_started).
                    want = clip.preroll_ms if clip.preroll_ms is not None else out.get("preroll_ms", 300)
                    preroll = max(40, min(3000, int(want))) * Gst.MSECOND
                    now_rt = self._running_time()
                    offset = now_rt - rt + preroll
                    clip.due_rt = now_rt + preroll
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
                    clip.geom = (x, y, w, h)
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

        def frame_filter(pad, info):
            # Keeps dropping blank (green) or decoder-flagged corrupt frames after the start
            if bad_frame(pad, info.get_buffer()):
                clip.blank_dropped += 1
                self._frames["blank"] = self._frames.get("blank", 0) + 1
                return Gst.PadProbeReturn.DROP
            return Gst.PadProbeReturn.OK

        vsrcpad.add_probe(Gst.PadProbeType.BUFFER, frame_filter)
        clip_pad = vsrcpad

        def clip_frames(_pad, _info):
            if self.visible is clip or not self.visible:
                self._frames["clip"] = self._frames.get("clip", 0) + 1
            return Gst.PadProbeReturn.OK

        clip_pad.add_probe(Gst.PadProbeType.BUFFER, clip_frames)
        vsrcpad.add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, eos_probe)
        if achain:
            achain[-1].get_static_pad("src").add_probe(Gst.PadProbeType.BUFFER, first_buffer)

        self.clips[cid] = clip
        b.sync_state_with_parent()

    def _pattern_source(self, b, name: str, label: Optional[str], out: Dict[str, Any]):
        """videotestsrc at the canvas size (+ optional text label) as the clip's source.
        Adds its elements to bin b and returns the last one."""
        nick = TEST_PATTERNS.get(name, TEST_PATTERNS["smpte"])[0]
        # Not live: frames are made when the compositor takes them, so the pattern runs at
        # exactly the canvas rate. (A live source drifted against the compositor and
        # delivered 25 of 30 fps; the earlier freeze was QoS dropping, now disabled.)
        src = self._make("videotestsrc")
        Gst.util_set_object_arg(src, "pattern", nick)
        cw, ch, fps = int(out["canvas_width"]), int(out["canvas_height"]), int(out["framerate"])
        els = [src, self._caps(f"video/x-raw,width={cw},height={ch},framerate={fps}/1")]
        if label and self._has("textoverlay"):
            txt = self._make("textoverlay", text=label, shaded_background=True,
                             font_desc=f"Sans Bold {max(10, ch // 30)}px")
            Gst.util_set_object_arg(txt, "valignment", "bottom")
            Gst.util_set_object_arg(txt, "halignment", "left")
            els.append(txt)
        for el in els:
            b.add(el)
        for a, c in zip(els, els[1:]):
            a.link(c)
        return els[-1]

    def _clip_started(self, clip: _Clip) -> None:
        """First frame decoded (and scheduled pre-roll ms ahead). Keep showing what's on
        screen until that frame is due, then switch - no black or half-ready frames."""
        if clip.dying or clip.id not in self.clips:
            return
        clip.started = True
        self.current = clip
        self._emit(type="clip_started", clip=clip.id)
        # Older clips that never made it on screen are obsolete (newer ones still loading are not)
        for c in [c for c in self.clips.values() if c.id < clip.id and c is not self.visible]:
            self._release(c)
        fps = max(1, int(self.settings["output"]["framerate"]))
        wait_ns = clip.due_rt + Gst.SECOND // fps - self._running_time()
        GLib.timeout_add(max(1, int(wait_ns / Gst.MSECOND)), lambda: (self._show(clip), False)[1])

    def _show(self, clip: _Clip) -> None:
        if clip.dying or clip.id not in self.clips or self.current is not clip:
            return  # superseded by a newer clip before it was due
        old = self.visible
        if old is clip:
            return
        self.visible = clip
        for c in [c for c in self.clips.values() if c.id < clip.id and c is not old]:
            self._release(c)
        self._transition(clip, old, clip.transition, clip.fade_ms)

    def _transition(self, new: _Clip, old: Optional[_Clip], kind: str, ms: int) -> None:
        """Animate from old to new. fade = crossfade; dip = old fades to black, then new fades
        in; slide-* = new pushes old off the canvas in that direction; cut = instant."""
        if kind == "cut" or ms <= 0 or old is None or old.dying:
            if kind.startswith("slide") or kind == "dip":
                kind = "fade" if old is None and ms > 0 else "cut"
            self._fade(new, old, 0 if kind == "cut" else ms)
            return
        if kind == "fade":
            self._fade(new, old, ms)
            return
        out = self.settings["output"]
        cw, ch = int(out["canvas_width"]), int(out["canvas_height"])
        vec = {"slide-left": (-1, 0), "slide-right": (1, 0),
               "slide-up": (0, -1), "slide-down": (0, 1)}.get(kind, (0, 0))
        steps = max(1, int(ms / self.FADE_STEP_MS))
        state = {"i": 0}

        def ease(p: float) -> float:  # smoothstep: gentle start and stop
            return p * p * (3 - 2 * p)

        def place(clip: _Clip, dx: float, dy: float) -> None:
            x, y = clip.geom[0], clip.geom[1]
            clip.vpad.set_property("xpos", int(round(x + dx)))
            clip.vpad.set_property("ypos", int(round(y + dy)))

        def tick():
            state["i"] += 1
            p = min(1.0, state["i"] / steps)
            live_new = not new.dying
            live_old = old is not None and not old.dying
            if kind == "dip":
                if live_old:
                    old.vpad.set_property("alpha", max(0.0, 1.0 - 2 * p))
                if live_new:
                    new.vpad.set_property("alpha", max(0.0, 2 * p - 1.0))
            else:  # slides
                e = ease(p)
                if live_new:
                    new.vpad.set_property("alpha", 1.0)
                    place(new, -vec[0] * (1 - e) * cw, -vec[1] * (1 - e) * ch)
                if live_old:
                    place(old, vec[0] * e * cw, vec[1] * e * ch)
            if live_new and new.apad is not None:
                new.apad.set_property("volume", p)
            if live_old and old.apad is not None:
                old.apad.set_property("volume", 1.0 - p)
            if p >= 1.0:
                if live_new:
                    place(new, 0, 0)
                    new.vpad.set_property("alpha", 1.0)
                if old is not None:
                    self._release(old)
                return False
            return True

        if kind.startswith("slide") and not new.dying:
            place(new, -vec[0] * cw, -vec[1] * ch)  # start fully off-canvas
            new.vpad.set_property("alpha", 1.0)
        GLib.timeout_add(self.FADE_STEP_MS, tick)

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
        old = self.visible
        self.current = self.visible = None
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
        if self.visible is clip:
            self.visible = None
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
            if out == "display" and self._display_attempt < self._display_attempts - 1:
                self._display_attempt += 1
                log.warning("display output failed to start - trying another way (%d of %d)",
                            self._display_attempt + 1, self._display_attempts)
                self._schedule_restart(1)
                return
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
        elif t == Gst.MessageType.LATENCY and self.pipeline is not None:
            self.pipeline.recalculate_latency()  # a live clip (test pattern) was added
        elif t == Gst.MessageType.WARNING:
            w, _ = msg.parse_warning()
            log.warning("gst warning from %s: %s", msg.src.get_name(), w.message)

    def _schedule_restart(self, delay: int = 5) -> None:
        if self._restart_timer:
            return

        def restart():
            self._restart_timer = None
            log.info("restarting pipeline after error")
            self._safe_build(self.settings)
            return False

        self._restart_timer = GLib.timeout_add_seconds(delay, restart)


def create_engine(on_event: EventCallback, force_mock: bool = False) -> BaseEngine:
    if HAVE_GST and not force_mock:
        return GstEngine(on_event)
    log.warning("GStreamer (python3-gi) not available - using mock engine")
    return MockEngine(on_event)
