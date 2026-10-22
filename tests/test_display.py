"""HDMI mode detection and frame-cadence checks (no GStreamer needed).

    python tests/test_display.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cueplayer.engine import boot_video_modes, cadence_ok, detect_display_connector, edid_has_audio, hdmi_alsa_device  # noqa: E402
import tempfile  # noqa: E402

failures = 0


def check(name, cond, detail=""):
    global failures
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    failures += 0 if cond else 1


pi = "console=serial0,115200 root=PARTUUID=ab-02 rootwait console=tty3 quiet video=HDMI-A-1:1920x1080@50D"
check("reads --take-hdmi mode", boot_video_modes(pi) == {"HDMI-A-1": (1920, 1080, 50.0)}, boot_video_modes(pi))
check("fractional rate, second port", boot_video_modes("video=HDMI-A-2:3840x2160@29.97")
      == {"HDMI-A-2": (3840, 2160, 29.97)})
check("no video= -> nothing", boot_video_modes("quiet splash") == {})
check("mode without refresh ignored", boot_video_modes("video=HDMI-A-1:1920x1080") == {})
for fps, hz, ok in [(50, 50, True), (25, 50, True), (30, 60, True), (60, 60, True), (59.94, 59.94, True),
                    (50, 60, False), (25, 60, False), (30, 50, False), (60, 50, False)]:
    check(f"{fps} fps on {hz} Hz {'smooth' if ok else 'judders'}", cadence_ok(fps, hz) == ok)

# connector detection: port 1 forced on but empty, display on port 2
with tempfile.TemporaryDirectory() as d:
    for name, edid, cid in (("card1-HDMI-A-1", b"", "32"), ("card1-HDMI-A-2", b"\x00" * 256, "42"),
                            ("card0-Writeback-1", b"", "50")):
        Path(d, name).mkdir()
        Path(d, name, "edid").write_bytes(edid)
        Path(d, name, "connector_id").write_text(cid + "\n")
    check("picks the port with a display, not the forced empty one",
          detect_display_connector(d) == (42, "HDMI-A-2"), detect_display_connector(d))
    Path(d, "card1-HDMI-A-2", "edid").write_bytes(b"")
    check("no display -> None (kmssink chooses)", detect_display_connector(d) is None)

# HDMI audio follows the video port on a Pi
PI_CARDS = """ 0 [vc4hdmi0       ]: vc4-hdmi - vc4-hdmi-0
                      vc4-hdmi-0
 1 [vc4hdmi1       ]: vc4-hdmi - vc4-hdmi-1
                      vc4-hdmi-1
 2 [Headphones     ]: bcm2835_headphonbcm2835 Headphones - bcm2835 Headphones
"""
check("HDMI-A-1 -> vc4hdmi0", hdmi_alsa_device("HDMI-A-1", PI_CARDS) == "hdmi:CARD=vc4hdmi0,DEV=0")
check("HDMI-A-2 -> vc4hdmi1", hdmi_alsa_device("HDMI-A-2", PI_CARDS) == "hdmi:CARD=vc4hdmi1,DEV=0")
check("unknown port -> default", hdmi_alsa_device(None, PI_CARDS) is None and hdmi_alsa_device("HDMI-A-3", PI_CARDS) is None)
check("non-Pi HDMI cards -> default", hdmi_alsa_device("HDMI-A-1", " 0 [PCH ]: HDA-Intel\n 1 [HDMI ]: HDA-Intel\n") is None)

# EDID audio support (CEA-861 extension)
def edid(ext=None):
    base = bytearray(128)
    base[126] = 1 if ext is not None else 0
    return bytes(base) + (bytes(ext) if ext is not None else b"")
cea_basic = bytearray(128); cea_basic[:4] = b"\x02\x03\x10\x40"
cea_adb = bytearray(128); cea_adb[:4] = b"\x02\x03\x10\x00"; cea_adb[4:8] = bytes([(1 << 5) | 3, 0x09, 0x07, 0x07])
cea_none = bytearray(128); cea_none[:4] = b"\x02\x03\x10\x00"; cea_none[4:6] = bytes([(2 << 5) | 1, 0x10])
check("EDID basic-audio flag -> audio", edid_has_audio(edid(cea_basic)) is True)
check("EDID audio data block -> audio", edid_has_audio(edid(cea_adb)) is True)
check("EDID with CEA but no audio -> no audio", edid_has_audio(edid(cea_none)) is False)
check("EDID without extension (DVI-style) -> no audio", edid_has_audio(edid()) is False)
check("unreadable EDID -> unknown", edid_has_audio(b"") is None)

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
