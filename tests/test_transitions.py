"""Transition animation maths, run with stand-in pads (no GStreamer needed).

    python tests/test_transitions.py
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cueplayer.engine as eng  # noqa: E402

failures = 0


def check(name, cond, detail=""):
    global failures
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    failures += 0 if cond else 1


class Pad:
    def __init__(self):
        self.p = {"alpha": 0.0, "xpos": 0, "ypos": 0, "volume": 0.0}
        self.history = []

    def set_property(self, k, v):
        self.p[k] = v
        self.history.append((k, v))


def clip(x=0, y=0):
    return SimpleNamespace(vpad=Pad(), apad=Pad(), geom=(x, y, 1920, 1080), dying=False)


class FakeGLib:  # run the animation to completion synchronously
    @staticmethod
    def timeout_add(_ms, fn):
        for _ in range(10000):
            if not fn():
                break


def run(kind, ms=400, old=True):
    eng.GLib = FakeGLib
    released = []
    faded = []
    me = SimpleNamespace(settings={"output": {"canvas_width": 1920, "canvas_height": 1080}},
                         FADE_STEP_MS=20, _release=released.append,
                         _fade=lambda n, o, m: faded.append((n, o, m)))
    new, prev = clip(), (clip() if old else None)
    eng.GstEngine._transition(me, new, prev, kind, ms)
    return new, prev, released, faded


new, old, rel, faded = run("slide-left")
xs = [v for k, v in new.vpad.history if k == "xpos"]
check("slide-left: new starts one canvas to the right", xs[0] == 1920, xs[:3])
check("slide-left: new ends in place", new.vpad.p["xpos"] == 0 and new.vpad.p["alpha"] == 1.0)
oxs = [v for k, v in old.vpad.history if k == "xpos"]
check("slide-left: old pushed off to the left", oxs[-1] == -1920, oxs[-3:])
check("slide-left: movement is monotonic", all(a >= b for a, b in zip(xs, xs[1:])))
check("slide-left: old released at the end", rel == [old])
check("slide: audio crossfades", new.apad.p["volume"] == 1 and old.apad.p["volume"] == 0)

new, old, rel, _ = run("slide-down")
ys = [v for k, v in new.vpad.history if k == "ypos"]
check("slide-down: new comes from above", ys[0] == -1080 and new.vpad.p["ypos"] == 0, ys[:2])

new, old, rel, _ = run("dip")
na = [v for k, v in new.vpad.history if k == "alpha"]
oa = [v for k, v in old.vpad.history if k == "alpha"]
mid = len(na) // 2
check("dip: halfway is black (both transparent)", na[mid - 1] < 0.1 and oa[mid - 1] < 0.1, (na[mid - 1], oa[mid - 1]))
check("dip: ends on the new item", na[-1] == 1.0 and rel == [old])

_, _, _, faded = run("fade")
check("fade delegates to crossfade", faded and faded[0][2] == 400)
_, _, _, faded = run("cut")
check("cut is instant", faded and faded[0][2] == 0)
_, _, _, faded = run("slide-left", old=False)
check("nothing on screen before: slide becomes a fade in", faded and faded[0][2] == 400)

# blank (green) frame detection
W, H = 1920, 1080
zero = bytes(W * H * 3 // 2)
black = bytes([16]) * (W * H) + bytes([128]) * (W * H // 2)
partial = bytes([90]) * (W * H) + bytes(W * H // 2)            # luma decoded, chroma still zero
green_gfx = bytes([173]) * (W * H) + bytes([42, 26]) * (W * H // 4)  # pure #00FF00 graphic (BT.709)


def rd(buf):
    return lambda off, n: buf[off:off + n]


check("all-zero NV12 frame is blank", eng.looks_blank_yuv(rd(zero), len(zero), "NV12"))
check("partly decoded frame (chroma zero) is caught", eng.looks_blank_yuv(rd(partial), len(partial), "NV12"))
check("real black frame passes", not eng.looks_blank_yuv(rd(black), len(black), "NV12"))
check("pure green graphic passes", not eng.looks_blank_yuv(rd(green_gfx), len(green_gfx), "NV12"))
check("RGB frames are never treated as blank", not eng.looks_blank_yuv(rd(zero), len(zero), "BGRx"))

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
