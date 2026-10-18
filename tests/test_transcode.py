"""Checks the convert / don't-convert decisions and the ffmpeg command.

    python tests/test_transcode.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cueplayer.transcode import analyse, build_command, target_size  # noqa: E402

PI4 = {"hw_decoders": {"h264": "v4l2h264dec", "hevc": "v4l2slh265dec"}, "playable_codecs": ["h264", "hevc"],
       "decode_max": {"h264": [1920, 1088]}}
PI5 = {"hw_decoders": {"hevc": "v4l2slh265dec"}, "playable_codecs": ["h264", "hevc"], "decode_max": {}}
X86 = {"hw_decoders": {}, "playable_codecs": ["h264"], "decode_max": {}}
HD50 = (1920, 1080, 50)
LED = (1152, 576, 50)

failures = 0


def check(name, cond, detail=""):
    global failures
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    failures += 0 if cond else 1


def vid(**kw):
    m = {"width": 1920, "height": 1080, "codec": "h264", "pix_fmt": "yuv420p", "fps": 50,
         "bit_rate": 12_000_000, "interlaced": False}
    m.update(kw)
    return m


r = analyse(vid(), "video", HD50, "fit", PI4)
check("1080p50 H.264 on Pi 4 plays as-is", not r["needed"], r)

r = analyse(vid(codec="prores", pix_fmt="yuv422p10le", bit_rate=180_000_000), "video", HD50, "fit", PI4)
check("ProRes converted", r["needed"] and any("prores" in x for x in r["reasons"]), r)
check("ProRes target is 1080p50 H.264", r["target"]["width"] == 1920 and r["target"]["fps"] == 50, r["target"])

r = analyse(vid(width=3840, height=2160), "video", HD50, "fit", PI4)
check("4K H.264 on Pi 4 converted to 1080p", r["needed"] and r["target"]["width"] == 1920, r)

r = analyse(vid(width=3840, height=2160), "video", (3840, 2160, 30), "fit", PI4)
check("4K canvas on Pi 4 capped at decoder limit", r["needed"] and r["target"]["width"] <= 1920
      and any("decoder limit" in x for x in r["reasons"]), r)

r = analyse(vid(codec="hevc", width=3840, height=2160, fps=30), "video", (3840, 2160, 30), "fit", PI4)
check("4K HEVC on 4K canvas, Pi 4 has HEVC hw: as-is", not r["needed"], r)

r = analyse(vid(codec="hevc"), "video", HD50, "fit", X86)
check("HEVC on x86 without hw decoder converted", r["needed"], r)

r = analyse(vid(codec="hevc"), "video", HD50, "fit", PI5)
check("HEVC on Pi 5 plays as-is", not r["needed"], r)

r = analyse(vid(fps=59.94), "video", HD50, "fit", PI4)
check("59.94 fps on a 50 fps canvas converted to 50", r["needed"] and r["target"]["fps"] == 50, r)

r = analyse(vid(fps=25), "video", HD50, "fit", PI4)
check("25 fps on a 50 fps canvas left alone", not r["needed"], r)

r = analyse(vid(interlaced=True), "video", HD50, "fit", PI4)
check("interlaced deinterlaced", r["needed"] and r["target"]["deinterlace"], r)

r = analyse(vid(), "video", LED, "fit", PI4)
check("1080p on 1152x576 LED canvas scaled down", r["needed"] and (r["target"]["width"], r["target"]["height"]) == (1024, 576), r["target"])

r = analyse(vid(), "video", LED, "fill", PI4)
check("fill mode keeps enough pixels to cover", (r["target"]["width"], r["target"]["height"]) == (1152, 648), r["target"])

r = analyse(vid(width=1280, height=720), "video", HD50, "fit", PI4)
check("never upscales", not r["needed"], r)

r = analyse(vid(codec="prores"), "video", HD50, "fit", PI4, mode="off")
check("mode off never converts", not r["needed"] and r.get("disabled"), r)

r = analyse(vid(), "video", HD50, "fit", PI4, mode="always")
check("mode always converts", r["needed"], r)

r = analyse({"width": 6000, "height": 4000, "codec": "mjpeg", "pix_fmt": "yuvj420p"}, "image", HD50, "fit", PI4)
check("24 MP photo scaled to canvas as JPEG", r["needed"] and r["target"]["format"] == "jpg"
      and r["target"]["height"] == 1080, r["target"])

r = analyse({"width": 1920, "height": 1080, "codec": "png", "pix_fmt": "rgba"}, "image", HD50, "fit", PI4)
check("1080p PNG left alone", not r["needed"], r)

r = analyse({"width": 1920, "height": 1080, "codec": "webp", "pix_fmt": "yuv420p"}, "image", HD50, "fit", PI4)
check("WebP converted", r["needed"], r)

check("target sizes are even and never upscaled",
      all(v % 2 == 0 for v in target_size(1001, 563, 1152, 576, "fit") + target_size(1333, 777, 1000, 1000, "fit"))
      and target_size(1001, 563, 1152, 576, "fit")[0] <= 1002, target_size(1001, 563, 1152, 576, "fit"))

cmd = build_command(Path("in.mov"), Path("out.mp4"),
                    {"kind": "video", "width": 1920, "height": 1080, "fps": 50, "deinterlace": True},
                    {"crf": 20, "preset": "veryfast"}, cpus=4)
s = " ".join(cmd)
check("ffmpeg: H.264 high, crf, preset", "-c:v libx264" in s and "-crf 20" in s and "-preset veryfast" in s, s)
check("ffmpeg: scale/fps/yuv420p/deinterlace filter",
      "yadif=mode=send_frame,scale=1920:1080:flags=lanczos,fps=50,format=yuv420p" in s, s)
check("ffmpeg: leaves 2 cores for playback on a 4-core Pi", "-threads 2" in s, s)
check("ffmpeg: optional audio, faststart", "0:a:0?" in s and "+faststart" in s, s)

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
