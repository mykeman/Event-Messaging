"""HDMI mode detection and frame-cadence checks (no GStreamer needed).

    python tests/test_display.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cueplayer.engine import boot_video_modes, cadence_ok  # noqa: E402

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

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
