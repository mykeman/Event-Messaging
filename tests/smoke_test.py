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

        # --- TCP ---
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
