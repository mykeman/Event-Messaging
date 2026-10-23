"""HDMI mode detection and frame-cadence checks (no GStreamer needed).

    python tests/test_display.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cueplayer.engine import (boot_video_modes, cadence_ok, connector_snapshot, detect_display_connector,  # noqa: E402
                              edid_has_audio, eld_alsa_device, hdmi_alsa_device, newly_connected)
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

# connector detection
def fake_drm(d, ports):
    for name, status, edid, cid in ports:
        Path(d, name).mkdir()
        Path(d, name, "status").write_text(status + "\n")
        Path(d, name, "edid").write_bytes(edid)
        Path(d, name, "connector_id").write_text(f"{cid}\n")


with tempfile.TemporaryDirectory() as d:  # Pi: port 1 forced on (video=...D), screen on port 2
    fake_drm(d, [("card1-HDMI-A-1", "connected", b"", 32), ("card1-HDMI-A-2", "connected", b"\x00" * 256, 42),
                 ("card0-Writeback-1", "unknown", b"", 50)])
    check("forced-on empty port loses to the port with EDID",
          detect_display_connector(d) == (42, "HDMI-A-2"), detect_display_connector(d))
with tempfile.TemporaryDirectory() as d:  # Giada F106D: screen connected but no EDID read
    fake_drm(d, [("card1-DP-1", "disconnected", b"", 269), ("card1-HDMI-A-1", "disconnected", b"", 281),
                 ("card1-HDMI-A-2", "connected", b"", 287)])
    check("connected port without EDID is still chosen",
          detect_display_connector(d) == (287, "HDMI-A-2"), detect_display_connector(d))
with tempfile.TemporaryDirectory() as d:
    fake_drm(d, [("card1-HDMI-A-1", "disconnected", b"", 32), ("card1-HDMI-A-2", "disconnected", b"", 42)])
    check("nothing connected -> None (kmssink chooses)", detect_display_connector(d) is None)

# hotplug: what counts as "a screen was plugged in"
with tempfile.TemporaryDirectory() as d:
    fake_drm(d, [("card1-HDMI-A-1", "disconnected", b"", 32), ("card1-HDMI-A-2", "disconnected", b"", 42)])
    before = connector_snapshot(d)
    check("snapshot reads status and EDID size", before == {"HDMI-A-1": ("disconnected", 0), "HDMI-A-2": ("disconnected", 0)}, before)
    Path(d, "card1-HDMI-A-2", "status").write_text("connected\n")
    Path(d, "card1-HDMI-A-2", "edid").write_bytes(b"\x00" * 256)
    after = connector_snapshot(d)
    check("plugging a screen in is detected", newly_connected(before, after) == ["HDMI-A-2"], newly_connected(before, after))
    check("unplugging needs no action", newly_connected(after, before) == [])
    swapped = dict(after, **{"HDMI-A-2": ("connected", 384)})
    check("a different screen (new EDID) is detected", newly_connected(after, swapped) == ["HDMI-A-2"])
    check("no change -> nothing", newly_connected(after, after) == [])

# Intel HDA: pick the HDMI PCM whose screen reports audio (Giada F106D layout)
with tempfile.TemporaryDirectory() as d:
    card = Path(d, "card0"); card.mkdir()
    card.joinpath("id").write_text("PCH\n")
    for i in range(15):
        valid = i == 3
        card.joinpath(f"eld#2.{i}").write_text(
            f"monitor_present\t\t{1 if valid else 0}\neld_valid\t\t{1 if valid else 0}\n"
            + ("monitor_name\t\tDisplay\n" if valid else ""))
    for dev, ident, name in ((0, "ALC897 Analog", "ALC897 Analog"), (1, "ALC897 Digital", "ALC897 Digital"),
                             (3, "HDMI 0", "Display"), (7, "HDMI 1", "HDMI 1"), (8, "HDMI 2", "HDMI 2")):
        pcm = card / f"pcm{dev}p"; pcm.mkdir()
        pcm.joinpath("info").write_text(f"card: 0\ndevice: {dev}\nsubdevice: 0\nstream: PLAYBACK\nid: {ident}\nname: {name}\n")
    check("Intel: HDMI PCM of the screen with valid ELD", eld_alsa_device(d) == ("plughw:CARD=PCH,DEV=3", "Display"), eld_alsa_device(d))
    card.joinpath("eld#2.3").write_text("monitor_present\t\t0\neld_valid\t\t0\n")
    check("Intel: no screen reporting audio -> None", eld_alsa_device(d) is None)

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
