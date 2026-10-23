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

# ---- pre-roll sequencing: keep the old item until the new one's first frame is due ----
import types  # noqa: E402

eng.Gst = SimpleNamespace(SECOND=10 ** 9, MSECOND=10 ** 6)
timers = []


class TimerGLib:
    @staticmethod
    def timeout_add(ms, fn):
        timers.append((ms, fn))


def engine_stub():
    me = SimpleNamespace(settings={"output": {"framerate": 30, "canvas_width": 1920, "canvas_height": 1080}},
                         clips={}, current=None, visible=None, released=[], shown=[], now=0)
    me._running_time = lambda: me.now
    me._emit = lambda **k: None

    def release(c):
        c.dying = True
        me.clips.pop(c.id, None)
        me.released.append(c.id)
        if me.visible is c:
            me.visible = None
    me._release = release
    me._transition = lambda new, old, kind, ms: me.shown.append((new.id, old.id if old else None))
    me._clip_started = types.MethodType(eng.GstEngine._clip_started, me)
    me._show = types.MethodType(eng.GstEngine._show, me)
    return me


def mk(me, cid, due):
    c = SimpleNamespace(id=cid, dying=False, started=False, due_rt=due, transition="cut", fade_ms=0)
    me.clips[cid] = c
    return c


eng.GLib = TimerGLib
me = engine_stub()
a = mk(me, 1, 0); me.visible = a; me.current = a
b = mk(me, 2, 300 * 10 ** 6)                 # due 300 ms from now
me._clip_started(b)
check("pre-roll: old item stays on screen while the new one buffers", me.visible is a and me.shown == [])
check("pre-roll: switch scheduled for the first frame + 1 frame", timers and 330 <= timers[-1][0] <= 334, timers)
timers.pop()[1]()
check("pre-roll: switches once due", me.visible is b and me.shown == [(2, 1)])

me = engine_stub(); timers.clear()
a = mk(me, 1, 0); me.visible = a; me.current = a
b = mk(me, 2, 300 * 10 ** 6); c = mk(me, 3, 400 * 10 ** 6)
me._clip_started(b)
me._clip_started(c)                          # second trigger before b was shown
check("rapid triggers: the older pending clip is dropped", 2 in me.released and 1 not in me.released)
for _, fn in list(timers):
    fn()
check("rapid triggers: only the newest is shown, straight from the old one", me.shown == [(3, 1)], me.shown)

me = engine_stub(); timers.clear()
a = mk(me, 1, 0); me.visible = a; me.current = a
b = mk(me, 2, 100 * 10 ** 6); newer = mk(me, 3, 0)  # 3 created, still loading (not started)
me._clip_started(b)
check("a newer clip still loading is not released", 3 in me.clips, me.released)

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
