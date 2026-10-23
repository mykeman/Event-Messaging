"""End-to-end smoke test using the mock engine (no GStreamer needed).

    python tests/smoke_test.py

Starts a player and a master on high ports in temp dirs, uploads media, builds
cues, drives them over HTTP, TCP and UDP, registers the player with the master
and runs a fleet sync + global cue.
"""
import json
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
P_HTTP, P_TCP, P_UDP = 18080, 17700, 17701
M_HTTP, M_TCP = 18090, 17800
PLAYER, MASTER = f"http://127.0.0.1:{P_HTTP}", f"http://127.0.0.1:{M_HTTP}"


def png(w=64, h=32) -> bytes:
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def wait_up(url):
    for _ in range(50):
        try:
            httpx.get(url + "/api/info", timeout=1)
            return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise SystemExit(f"{url} did not start")


def tcp(lines, port=P_TCP, read_events=0.0):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        out = []
        for line in lines:
            s.sendall((line + "\r\n").encode())
            time.sleep(0.15)
        time.sleep(read_events)
        s.settimeout(0.5)
        buf = b""
        try:
            while True:
                d = s.recv(65536)
                if not d:
                    break
                buf += d
        except socket.timeout:
            pass
        out = [l for l in buf.decode().split("\r\n") if l]
        return out


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        global failures
        failures += 1


failures = 0


def main():
    pdir, mdir = tempfile.mkdtemp(), tempfile.mkdtemp()
    # pre-seed player settings: fast image duration, register with master
    Path(pdir, "settings.json").write_text(json.dumps({
        "control": {"http_port": P_HTTP, "tcp_port": P_TCP, "udp_port": P_UDP},
        "playback": {"image_duration": 1},
        "transition": {"type": "cut"},
        "fleet": {"master_url": MASTER, "screen_name": "east-led", "groups": ["east"]}}))
    Path(mdir, "settings.json").write_text(json.dumps({
        "control": {"http_port": M_HTTP, "tcp_port": M_TCP, "udp_port": 0}}))
    procs = [
        subprocess.Popen([sys.executable, "-m", "cueplayer", "--mock", "--data", pdir], cwd=ROOT),
        subprocess.Popen([sys.executable, "-m", "cueplayer", "--role", "master", "--data", mdir],
                         cwd=ROOT),
    ]
    try:
        wait_up(PLAYER)
        wait_up(MASTER)
        c = httpx.Client(base_url=PLAYER, timeout=5)

        # --- upload ---
        r = c.post("/api/media", files={"file": ("Welcome Slide.png", png())},
                   data={"category": "library"})
        check(r.status_code == 200 and r.json()["kind"] == "image", f"upload image {r.status_code}")
        img = r.json()
        r = c.post("/api/media", files={"file": ("gala-loop.mp4", b"\x00" * 1000)},
                   data={"category": "event", "name": "Gala Loop"})
        check(r.status_code == 200 and r.json()["kind"] == "video", "upload video")
        vid = r.json()
        r = c.post("/api/media", files={"file": ("evil.exe", b"x")})
        check(r.status_code == 400, "reject unsupported type")

        # --- default playlist ---
        c.patch("/api/settings", json={"playback": {"default_playlist": [img["id"]]}})
        time.sleep(0.3)
        st = c.get("/api/status").json()
        check(st["mode"] == "default" and st["media"]["id"] == img["id"], f"default plays {st['mode']}")

        # --- AES67 settings + SDP (mock engine: nothing is streamed) ---
        check(c.get("/api/aes67.sdp").status_code == 404, "aes67 sdp 404 when off")
        r = c.patch("/api/settings", json={"aes67": {"enabled": True, "port": 5005}})
        check(r.status_code == 400 and "port" in r.json()["detail"], f"bad aes67 settings rejected ({r.status_code})")
        r = c.patch("/api/settings", json={"aes67": {"enabled": True, "multicast": "239.69.9.9"}})
        check(r.status_code == 200 and r.json()["aes67"]["enabled"], "aes67 enabled")
        time.sleep(0.3)
        r = c.get("/api/aes67.sdp")
        check(r.status_code == 200 and r.headers["content-type"].startswith("application/sdp")
              and "a=rtpmap:96 L24/48000/2" in r.text and "c=IN IP4 239.69.9.9/32" in r.text, f"aes67 sdp served ({r.status_code})")
        check(c.get("/api/status").json()["engine"]["aes67"]["synced"], "aes67 status in engine status")
        c.patch("/api/settings", json={"aes67": {"enabled": False}})
        time.sleep(0.3)
        st = c.get("/api/status").json()
        check(st["mode"] == "default", f"still playing default after aes67 toggles ({st['mode']})")

        # --- play by name (GET, Q-SYS style) ---
        st = c.get("/api/play/gala loop", params={"duration": 1}).json()
        check(st["mode"] == "adhoc" and st["media"]["name"] == "Gala Loop", "play by name via GET")
        time.sleep(1.4)
        st = c.get("/api/status").json()
        check(st["mode"] == "default", f"returns to default after duration ({st['mode']})")

        # --- cues ---
        r = c.put("/api/cues/1", json={"label": "Gala", "media": vid["id"], "loop": True})
        check(r.status_code == 200, "create cue 1")
        c.put("/api/cues/2", json={"label": "Sequence", "media": [img["id"], vid["id"]],
                                   "after": "black", "duration": 0.5})
        st = c.post("/api/cue/1").json()
        check(st["mode"] == "cue" and st["cue"] == "1" and st["loop"], "trigger cue 1")
        st = c.post("/api/cue/sequence").json()
        check(st["cue"] == "2" and st["media"]["id"] == img["id"], "trigger cue by label")
        time.sleep(1.5)
        st = c.get("/api/status").json()
        check(st["mode"] == "black", f"sequence then black ({st['mode']}, {st.get('media')})")
        r = c.post("/api/cue/99")
        check(r.status_code == 404, "unknown cue -> 404")

        # --- standard scenarios + locking ---
        cues = c.get("/api/cues").json()
        check(cues.get("107", {}).get("label") == "Fire alarm" and cues["107"]["lock"]
              and cues["107"]["group"] == "scenario", "standard scenarios seeded")
        c.put("/api/cues/107", json=dict(cues["107"], media=img["id"]))
        st = c.get("/api/cue/107").json()
        check(st["lock"] and st["label"] == "Fire alarm", "fire alarm scenario locks")
        r = c.get("/api/cue/1")
        check(r.status_code == 409 and "Fire alarm" in r.json()["detail"], f"event cue blocked by fire alarm ({r.status_code})")
        check(c.get("/api/next").status_code == 409, "next blocked while locked")
        st = c.get("/api/cue/1", params={"force": 1}).json()
        check(st["cue"] == "1" and not st["lock"], "force overrides lock")
        c.get("/api/cue/107")
        st = c.get("/api/normal").json()
        check(st["mode"] == "default" and not st["lock"], "NORMAL clears the lock")
        r = c.post("/api/cues/scenarios").json()
        check(r["added"] == [], "re-adding scenarios doesn't duplicate")

        # --- decoder failures: software fallback, no endless retry loop ---
        r = c.post("/api/media", files={"file": ("hw.mp4", b"MOCK:HWFAIL" + b"\0" * 100)}).json()
        st = c.get(f"/api/play/{r['id']}", params={"after": "hold"}).json()
        time.sleep(0.5)
        st = c.get("/api/status").json()
        check(st["media"]["id"] == r["id"] and st["mode"] == "adhoc"
              and c.get(f"/api/media/{r['id']}").json().get("decode") == "software",
              f"hardware decoder failure retried in software ({st['mode']})")
        bad = c.post("/api/media", files={"file": ("broken.mp4", b"MOCK:BROKEN" + b"\0" * 100)}).json()
        c.patch("/api/settings", json={"playback": {"default_playlist": [bad["id"]]}})
        c.get("/api/normal")
        time.sleep(3.5)
        st = c.get("/api/status").json()
        check(st["mode"] == "default" and st["media"] is None,
              f"broken-only normal playlist goes black instead of looping ({st['mode']}, {st['media']})")
        c.patch("/api/settings", json={"playback": {"default_playlist": [img["id"]]}})
        c.delete(f"/api/media/{bad['id']}")
        c.delete(f"/api/media/{r['id']}")

        # --- transitions ---
        st = c.get("/api/play/gala loop", params={"transition": "dip", "after": "hold"})
        check(st.status_code == 200, "play with transition=dip")
        check(c.get("/api/play/gala loop", params={"transition": "wobble"}).status_code == 400, "unknown transition rejected")
        r = c.put("/api/cues/5", json={"label": "Slide", "media": img["id"], "transition": "slide-left", "transition_ms": 800}).json()
        check(r["transition"] == "slide-left" and r["transition_ms"] == 800, "cue stores its transition")
        check(c.get("/api/cue/5").status_code == 200, "cue with slide transition plays")
        c.delete("/api/cues/5")
        c.patch("/api/settings", json={"transition": {"type": "slide-up", "duration_ms": 500}})
        check(c.get("/api/settings").json()["transition"]["type"] == "slide-up", "default transition setting")
        c.patch("/api/settings", json={"transition": {"type": "cut"}})
        c.get("/api/normal")

        # --- playlists + schedule ---
        import datetime as _dt
        pl = c.post("/api/playlists", json={"name": "Evening", "items": [{"media": vid["id"]}, {"media": img["id"], "duration": 2}]}).json()
        check(pl["name"] == "Evening" and len(pl["items"]) == 2, "create playlist")
        now = _dt.datetime.now().replace(second=0, microsecond=0)
        f = lambda d: d.strftime("%Y-%m-%dT%H:%M")  # noqa: E731
        ent = c.post("/api/schedule", json={"title": "Show", "playlist": pl["id"], "start": f(now - _dt.timedelta(minutes=5)),
                                             "end": f(now + _dt.timedelta(hours=1))}).json()
        time.sleep(0.5)
        st = c.get("/api/status").json()
        check(st["mode"] == "default" and st.get("source") == "schedule: Show" and st["media"]["id"] == vid["id"],
              f"scheduled playlist replaces normal ({st.get('source')})")
        check(st["schedule"]["active"]["title"] == "Show", "status reports active entry")
        check(c.delete(f"/api/playlists/{pl['id']}").status_code == 409, "playlist in use can't be deleted")
        occ = c.get("/api/schedule/occurrences", params={"start": f(now - _dt.timedelta(days=1)), "end": f(now + _dt.timedelta(days=1))}).json()
        check(len(occ) == 1 and occ[0]["title"] == "Show", "occurrences for calendar")
        check(c.patch("/api/settings", json={"schedule": {"timezone": "Mars/Base"}}).status_code == 400, "bad timezone rejected")
        c.delete(f"/api/schedule/{ent['id']}")
        time.sleep(0.5)
        st = c.get("/api/status").json()
        check(st.get("source") == "normal playlist", f"entry removed: back to normal playlist ({st['mode']}, {st.get('source')}, {st['media']})")
        out = tcp(["PLAYLIST Evening", "NORMAL"])
        check(any(l.startswith("OK mode=adhoc") for l in out), f"tcp PLAYLIST {out[:1]}")
        check(c.delete(f"/api/playlists/{pl['id']}").status_code == 200, "delete unused playlist")

        # --- test patterns ---
        pats = c.get("/api/test").json()
        check("smpte" in pats and "smpte-hd" in pats, "test pattern list")
        st = c.get("/api/test/smpte", params={"tone": 1}).json()
        check(st["mode"] == "test" and st["media"]["kind"] == "pattern" and st["pattern"]["tone"],
              f"SMPTE bars with tone ({st['mode']})")
        check(c.get("/api/test/nosuch").status_code == 404, "unknown pattern rejected")
        c.get("/api/cue/107")
        check(c.get("/api/test/smpte").status_code == 409, "test pattern blocked by locked scenario")
        check(c.get("/api/test/smpte", params={"force": 1}).json()["mode"] == "test", "force shows it anyway")
        st = c.get("/api/test/off").json()
        check(st["mode"] == "default", "TEST off returns to normal")

        # --- TCP ---
        out = tcp(["CUE 1 TRANSITION=slide-right FADE=600", "CUE 1 CUT", "NORMAL"])
        oks = [l for l in out if l.startswith("OK ")]
        check(len(oks) == 3 and all(l.startswith("OK mode=cue") for l in oks[:2]), f"tcp transitions {oks}")
        out = tcp(["TEST checkers NOLABEL", "TEST OFF"])
        check(out[0].startswith("OK mode=test") and "kind=pattern" in out[0], f"tcp TEST pattern {out[:1]}")
        check(any(l.startswith("OK mode=default") for l in out[1:]), "tcp TEST OFF")
        out = tcp(["PING", "CUE 1", "STATUS", "PLAY \"Welcome Slide\" HOLD", "BLACK FADE=0",
                   "BOGUS", "CUE 107", "CUE 1", "NORMAL"])
        check(out[0] == "PONG", "tcp ping")
        check(any(l.startswith("OK mode=cue cue=1") for l in out), "tcp cue")
        check(any(l.startswith("STATUS mode=cue") for l in out), "tcp status")
        check(any(l.startswith("EVENT mode=") for l in out), "tcp push events")
        check(any("ERR unknown command BOGUS" in l for l in out), "tcp unknown cmd")
        check(any(l.startswith("OK mode=cue cue=107") and "lock=1" in l for l in out), "tcp scenario lock=1")
        check(any(l.startswith("ERR blocked") for l in out), "tcp trigger blocked while locked")
        check(any(l.startswith("OK mode=default") and "lock=0" in l for l in out), "tcp NORMAL clears")

        # --- UDP ---
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.settimeout(2)
        u.sendto(b"CUE 1\r\n", ("127.0.0.1", P_UDP))
        reply = u.recv(4096).decode()
        check(reply.startswith("OK mode=cue cue=1"), f"udp cue ({reply.strip()})")

        # --- auth ---
        c.patch("/api/settings", json={"control": {"api_token": "s3cret"}})
        check(c.get("/api/status").status_code == 401, "token enforced")
        check(c.get("/api/status", params={"token": "s3cret"}).status_code == 200, "token query ok")
        out = tcp(["STATUS", "AUTH s3cret", "STATUS"])
        check(out[0].startswith("ERR AUTH") and out[1] == "OK AUTH" and out[2].startswith("STATUS"),
              "tcp auth")
        c.headers["Authorization"] = "Bearer s3cret"

        # --- master ---
        m = httpx.Client(base_url=MASTER, timeout=10)
        for _ in range(40):  # heartbeat registers within ~1s of start
            screens = m.get("/api/screens").json()
            if screens and screens[0].get("online"):
                break
            time.sleep(0.25)
        check(len(screens) == 1 and screens[0]["name"] == "east-led", "player self-registered")
        check(screens and screens[0].get("online"), f"master sees player online {screens and screens[0].get('error')}")
        check(m.get("/api/groups").json() == {"east": [screens[0]["id"]]}, "groups from player")

        r = m.post("/api/media", files={"file": ("Evacuate.png", png(32, 32))},
                   data={"category": "library", "targets": "group:east"})
        check(r.status_code == 200, "master upload")
        mitem = r.json()
        for _ in range(40):
            ids = [i["id"] for i in c.get("/api/media").json()]
            if mitem["id"] in ids:
                break
            time.sleep(0.25)
        check(mitem["id"] in ids, "master synced media to player")
        pitem = c.get(f"/api/media/{mitem['id']}").json()
        check("master" in pitem.get("tags", []), "synced media tagged as managed")

        r = m.post(f"/api/target/east/play/{mitem['id']}", json={"after": "hold"}).json()
        check(r["ok"] == 1, f"master group play {r}")
        check(c.get("/api/status").json()["media"]["id"] == mitem["id"], "player shows master item")

        m.put("/api/gcues/10", json={"label": "Emergency", "actions": [
            {"target": "all", "action": "play", "ref": mitem["id"], "after": "hold"}]})
        r = m.post("/api/gcue/emergency").json()
        check(r["actions"][0]["ok"] == 1, "global cue")
        out = tcp(["GCUE 10", "east BLACK", "STATUS"], port=M_TCP)
        check(out[0].startswith("OK gcue=10 ok=1"), f"master tcp gcue {out[:1]}")
        check(any(l.startswith("OK target=east action=black ok=1") for l in out), "master tcp target")
        check(any(l.startswith("STATUS screen=east-led online=1") for l in out), "master tcp status")
        out = tcp(["ALL TEST smpte-hd TONE", "east TEST OFF"], port=M_TCP)
        check(out[0].startswith("OK target=all action=test ok=1"), f"master tcp test pattern {out[:1]}")
        r = m.post("/api/target/group:east/test/ball", json={"tone": False}).json()
        check(r["ok"] == 1 and c.get("/api/status").json()["media"]["id"] == "pattern:ball",
              "master http test pattern on a group")

        # deleting on master removes from player
        m.delete(f"/api/media/{mitem['id']}")
        time.sleep(1.5)
        ids = [i["id"] for i in c.get("/api/media").json()]
        check(mitem["id"] not in ids, "master delete propagates")
        check(img["id"] in ids, "local player media untouched by sync")

        check(c.get("/").status_code == 200 and m.get("/").status_code == 200, "GUI pages served")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            p.wait(5)
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
