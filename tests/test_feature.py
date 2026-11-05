"""Feature screening, the conversion toggle, the HDMI refresh setting and the system routes,
on the mock engine (no GStreamer needed).

    python tests/test_feature.py
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cueplayer.config import Settings  # noqa: E402
from cueplayer.controller import Blocked, Controller, PlayError, parse_time  # noqa: E402
from cueplayer.library import Library  # noqa: E402
from cueplayer.tcp_control import LineServer, feature_line, player_event_line, player_handler  # noqa: E402

failures = 0


def check(name, cond, detail=""):
    global failures
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    failures += 0 if cond else 1


def add(lib, name, kind="video", duration=0.0):
    tmp = lib.incoming_dir / f"{name}.part"
    tmp.write_bytes(b"\0" * 100 + name.encode())
    item = lib.add_file(tmp, f"{name}.{'mp4' if kind == 'video' else 'png'}")
    with lib._lock:
        lib.items[item["id"]]["duration"] = duration
        lib._save()
    return item["id"]


async def wait_for(cond, timeout=5.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


async def main(data: Path):
    # ---- settings defaults & validation -----------------------------------------
    settings = Settings(data)
    st = settings.get()
    check("automatic conversion defaults to off", st["transcode"]["enabled"] is False)
    check("HDMI refresh defaults to auto", st["display"]["refresh"] == 0)
    try:
        settings.update({"display": {"refresh": 500}})
        check("refresh 500 Hz rejected", False)
    except ValueError:
        check("refresh 500 Hz rejected", True)
    check("refresh 50 Hz accepted", "display" in settings.update({"display": {"refresh": "50"}})
          and settings.section("display")["refresh"] == 50)
    settings.update({"display": {"refresh": 0}})
    try:
        settings.update({"feature": {"notify_url": "ftp://x"}})
        check("bad webhook URL rejected", False)
    except ValueError:
        check("bad webhook URL rejected", True)
    check("parse_time h:mm:ss", parse_time("1:02:03") == 3723 and parse_time("90") == 90 and parse_time(12.5) == 12.5)

    lib = Library(data)
    film = add(lib, "Big Film", duration=4.0)
    other = add(lib, "Short Film", duration=30.0)
    loop_a = add(lib, "Loop A", duration=30.0)
    alarm = add(lib, "Fire", duration=30.0)
    lib.set_cue("107", {"label": "Fire alarm", "media": alarm, "lock": True, "loop": True, "group": "scenario"})
    lib.set_cue("5", {"label": "Sponsor", "media": other, "after": "default"})
    settings.update({"playback": {"default_playlist": [loop_a]}})

    # ---- conversion toggle (Transcoder) ------------------------------------------
    ctrl = Controller(settings, lib, force_mock=True)
    events = []
    ctrl.subscribe(events.append)
    await ctrl.start()
    await wait_for(lambda: ctrl.state["mode"] == "default")
    tc = ctrl.transcoder
    with lib._lock:   # a file that would need converting (no ffprobe on a dev machine)
        lib.items[film]["probe"] = {"width": 3840, "height": 2160, "codec": "prores", "fps": 25,
                                    "pix_fmt": "yuv422p10le", "probed": True}
    tc.evaluate(film)
    pb = lib.get(film)["playback"]
    check("conversion off: file plays as uploaded", pb.get("status") == "original" and pb.get("disabled"), pb)
    copy = lib.media_dir / f"{film}.play.mp4"
    copy.write_bytes(b"copy")
    lib.set_playback(film, status="ready", file=copy.name)
    tc.evaluate(film)
    check("conversion off keeps an existing copy", lib.get(film)["playback"]["status"] == "ready")
    lib.drop_playback_copy(film)

    # ---- feature: start, interrupt by emergency, resume ---------------------------
    ctrl.play_feature(film, source="test")
    f = ctrl.feature_status()
    check("feature plays", ctrl.state["mode"] == "feature" and f["state"] == "playing" and f["duration"] == 4.0, f)
    await asyncio.sleep(1.0)
    ctrl.play_cue("107")
    f = ctrl.feature_status()
    check("emergency interrupts the feature", f["state"] == "interrupted" and ctrl.state["lock"], f)
    check("position kept at interruption", 0.8 <= f["position"] <= 1.6, f["position"])
    check("interrupted by names the message", f["interrupted_by"] == "Fire alarm", f["interrupted_by"])
    pos_at_stop = f["position"]
    await asyncio.sleep(0.6)
    check("position frozen while interrupted", ctrl.feature_status()["position"] == pos_at_stop)
    try:
        ctrl.play_feature(other)
        st = ctrl.feature_status()
        check("new feature during emergency waits", st["state"] == "interrupted" and ctrl.state["lock"], st)
        ctrl.play_feature(film, start=pos_at_stop)   # put the first film back, as it was
    except Blocked:
        check("new feature during emergency waits", False, "blocked")
    ctrl.play_default()   # Normal clears the emergency
    f = ctrl.feature_status()
    check("Normal resumes the feature", ctrl.state["mode"] == "feature" and f["state"] == "playing", f)
    start = ctrl.engine.clip_position(ctrl.state["clip"])
    check("resumes at the saved position", abs(start - pos_at_stop) < 0.3, (start, pos_at_stop))
    clip = ctrl.state["clip"]
    ctrl.play_default()
    check("Normal during a playing feature changes nothing", ctrl.state["clip"] == clip and ctrl.state["mode"] == "feature")
    check("status carries feature position", ctrl.status()["feature"]["remaining"] is not None)

    # non-locked cue interrupts, and the feature resumes when the cue ends
    ctrl.play_cue("5", duration=0.5)
    check("event cue pauses the feature", ctrl.feature_status()["state"] == "interrupted")
    await wait_for(lambda: ctrl.state["mode"] == "feature", 3)
    check("feature resumes after the cue ends", ctrl.state["mode"] == "feature"
          and ctrl.feature_status()["state"] == "playing")

    # end of film
    events.clear()
    ok = await wait_for(lambda: (ctrl.feature or {}).get("state") == "finished", 6)
    check("feature finishes", ok, ctrl.feature_status())
    await wait_for(lambda: ctrl.state["mode"] == "default", 2)
    check("then the Normal playlist", ctrl.state["mode"] == "default" and ctrl.state["media"]["id"] == loop_a)
    fin = [e for e in events if e.get("event") == "feature" and e.get("feature_event") == "finished"]
    check("finished event sent once", len(fin) == 1, [e.get("feature_event") for e in events if e.get("event") == "feature"])
    line = player_event_line(fin[0]) if fin else ""
    check("TCP line for Q-SYS", line.startswith("FEATURE event=finished state=finished") and "remain=0.0" in line, line)
    pos_lines = [player_event_line(e) for e in events if e.get("event") == "feature_position"]
    check("position lines while playing", any(l.startswith("FEATURE state=playing pos=") for l in pos_lines), pos_lines[:2])
    ctrl.play_default()
    check("Normal after the end plays the playlist, not the film", ctrl.state["mode"] == "default")

    # ---- TCP commands ---------------------------------------------------------------
    srv = LineServer(player_handler(ctrl), lambda: "")
    sess = {"auth": True}
    r = await srv.execute('FEATURE "Big Film" START=0:00:01', sess)
    check("TCP FEATURE starts it", r.startswith("OK FEATURE state=playing") and ctrl.state["mode"] == "feature", r)
    r = await srv.execute("FEATURE STATUS", sess)
    check("TCP FEATURE STATUS", r.startswith("FEATURE state=playing pos=") and "dur=4.0" in r, r)
    r = await srv.execute("FEATURE SEEK 2", sess)
    check("TCP FEATURE SEEK", r.startswith("OK FEATURE") and abs(ctrl.engine.clip_position(ctrl.state["clip"]) - 2) < 0.3, r)
    r = await srv.execute("FEATURE STOP BLACK", sess)
    check("TCP FEATURE STOP BLACK", "state=stopped" in r and ctrl.state["mode"] == "black", (r, ctrl.state["mode"]))
    r = await srv.execute("FEATURE STOP", sess)
    check("stop with nothing playing errors", r.startswith("ERR"), r)
    r = await srv.execute("STATUS", sess)
    check("STATUS has feature=", "feature=stopped" in r, r)
    try:
        ctrl.play_feature(loop_a, start="5:00")
        check("start past the end rejected", False)
    except PlayError:
        check("start past the end rejected", True)

    # ---- persistence: resume after a restart -------------------------------------------
    ctrl.play_feature(other, start=10)
    await asyncio.sleep(0.3)
    ctrl._feature_save()
    ctrl.shutdown()
    ctrl2 = Controller(Settings(data), Library(data), force_mock=True)
    f2 = ctrl2.feature_status()
    check("restart: feature remembered as interrupted", f2 and f2["state"] == "interrupted"
          and f2["interrupted_by"] == "restart" and f2["position"] >= 10, f2)
    await ctrl2.start()
    await wait_for(lambda: ctrl2.state["mode"] == "feature", 3)
    check("restart: feature carries on", ctrl2.state["mode"] == "feature"
          and ctrl2.engine.clip_position(ctrl2.state["clip"]) >= 10)

    # ---- schedule: a feature entry starts once per occurrence ---------------------------
    ctrl2.feature_stop()
    now = ctrl2.now()
    e = ctrl2.schedule.save_entry(None, {"title": "Screening", "playlist": "feature", "media": film,
                                         "start": (now.replace(second=0)).isoformat(),
                                         "end": (now.replace(second=0).replace(minute=(now.minute + 2) % 60)
                                                 if now.minute < 58 else now.replace(hour=(now.hour + 1) % 24, minute=1)).isoformat()})
    ctrl2._sched_key = ("recheck",)
    ctrl2.check_schedule()
    check("scheduled feature starts", ctrl2.state["mode"] == "feature" and ctrl2.feature["media"]["id"] == film
          and ctrl2.feature["source"] == "schedule: Screening", ctrl2.feature_status())
    ctrl2.feature_stop()
    ctrl2._sched_key = ("recheck",)
    ctrl2.check_schedule()
    check("...and only once per occurrence", ctrl2.state["mode"] == "default")
    try:
        ctrl2.schedule.save_entry(None, {"title": "x", "playlist": "feature", "start": "2026-01-01T10:00",
                                         "end": "2026-01-01T12:00"})
        check("feature entry needs a video", False)
    except ValueError:
        check("feature entry needs a video", True)
    ctrl2.schedule.delete_entry(e["id"])

    # ---- HTTP API ------------------------------------------------------------------------
    from fastapi.testclient import TestClient
    from cueplayer.api import create_player_app
    with TestClient(create_player_app(ctrl2)) as c:
        r = c.post(f"/api/feature/play/{film}", json={"start": "0:00:01"})
        check("POST /api/feature/play", r.status_code == 200 and r.json()["feature"]["state"] == "playing", r.text)
        r = c.get("/api/feature")
        check("GET /api/feature", r.status_code == 200 and r.json()["media"]["id"] == film, r.text)
        r = c.get("/api/feature/stop?then=hold")
        check("GET /api/feature/stop?then=hold", r.status_code == 200 and r.json()["mode"] == "hold", r.text)
        r = c.get("/api/feature/resume")
        check("resume with nothing interrupted is 404", r.status_code == 404, r.status_code)
        r = c.get("/api/system/update")
        check("update status says unavailable here", r.status_code == 200 and r.json()["available"] is False
              and r.json()["build"]["version"], r.text)
        r = c.post("/api/system/update/check")
        check("update check refused without the helper", r.status_code == 409, r.status_code)
        r = c.get("/api/display/modes")
        check("display modes lists sizes and refresh rates", r.status_code == 200
              and "1920x1080" in r.json()["sizes"] and 50 in r.json()["refresh_rates"], r.text)
        r = c.patch("/api/settings", json={"transcode": {"enabled": True}})
        check("conversion can be switched on", r.status_code == 200 and r.json()["transcode"]["enabled"] is True)
    ctrl2.shutdown()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as d:
        asyncio.run(main(Path(d)))
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
