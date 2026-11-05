"""Kernel command line editing in deploy/cueplayer-helper (HDMI mode at boot).

    python tests/test_helper.py
"""
import sys
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
loader = SourceFileLoader("cueplayer_helper", str(ROOT / "deploy" / "cueplayer-helper"))
helper = module_from_spec(spec_from_loader("cueplayer_helper", loader))
loader.exec_module(helper)

from cueplayer.engine import boot_video_modes  # noqa: E402

failures = 0


def check(name, cond, detail=""):
    global failures
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    failures += 0 if cond else 1


PI = "console=serial0,115200 console=tty3 root=PARTUUID=abc rootwait quiet video=HDMI-A-2:1920x1080@60D".split()

toks, port = helper.edit_tokens(PI, "1920x1080@50", "HDMI-A-1")
line = " ".join(toks)
check("keeps the installer's port and forced-on flag", "video=HDMI-A-2:1920x1080@50D" in line and port == "HDMI-A-2", line)
check("only one video= token", line.count("video=") == 1, line)
check("other options untouched", line.startswith("console=serial0,115200 console=tty3 root=PARTUUID=abc rootwait quiet"), line)
check("engine reads the new boot mode", boot_video_modes(line) == {"HDMI-A-2": (1920, 1080, 50.0)}, boot_video_modes(line))

toks, port = helper.edit_tokens("root=x quiet".split(), "3840x2160@25", "HDMI-A-1")
check("adds a mode for the detected port", " ".join(toks) == "root=x quiet video=HDMI-A-1:3840x2160@25", toks)

toks, _ = helper.edit_tokens(PI, None, "")
check("auto keeps the port forced on", toks[-1] == "video=HDMI-A-2:D" and boot_video_modes(" ".join(toks)) == {}, toks)

toks, _ = helper.edit_tokens("root=x video=HDMI-A-1:1280x720@60".split(), None, "")
check("auto without force removes the mode", toks == ["root=x"], toks)

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
