"""Master server: one control point for a fleet of players.

Each screen runs a normal, fully autonomous player. The master
  * keeps a list of screens (added by hand, or self-registered via fleet.master_url),
  * polls their status,
  * fans commands out to a screen, a group, or all screens,
  * holds "global cues" that do different things on different screens in one trigger,
  * pushes media (and optionally a shared player cue list) to screens.

If the master is offline every screen keeps playing and can still be driven
directly, so there is no single point of failure for the on-screen content.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from .common import (VERSION, WEB_DIR, Broadcaster, as_bool, as_float, as_int, hostname,
                     make_auth, params, ws_authorised)
from .config import Settings
from .library import CATEGORIES, Library, _atomic_write
from .tcp_control import parse_opts, q, status_line

log = logging.getLogger("cueplayer.master")

ACTIONS = ("cue", "play", "stop", "default", "black", "next")
MANAGED_TAG = "master"


class Fleet:
    POLL_S = 2.0

    def __init__(self, data_dir: Path, settings: Settings, library: Library):
        self.dir = Path(data_dir)
        self.settings = settings
        self.library = library
        self.screens_path = self.dir / "screens.json"
        self.mcues_path = self.dir / "global_cues.json"
        self.screens: Dict[str, Dict[str, Any]] = self._load(self.screens_path)
        self.mcues: Dict[str, Dict[str, Any]] = self._load(self.mcues_path)
        self.live: Dict[str, Dict[str, Any]] = {}  # id -> {online, status, info, error, seen}
        self.listeners: List[Any] = []
        self.client: Optional[httpx.AsyncClient] = None
        self.sync_jobs: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _load(p: Path) -> Dict[str, Any]:
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return {}

    def save(self) -> None:
        _atomic_write(self.screens_path, self.screens)
        _atomic_write(self.mcues_path, self.mcues)

    # ---- events ---------------------------------------------------------------
    def subscribe(self, fn):
        self.listeners.append(fn)
        return lambda: self.listeners.remove(fn) if fn in self.listeners else None

    def notify(self, msg: Dict[str, Any]) -> None:
        for fn in list(self.listeners):
            try:
                fn(msg)
            except Exception:
                log.exception("listener failed")

    # ---- screens ------------------------------------------------------------------
    @staticmethod
    def _clean_screen(d: Dict[str, Any], old: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        s = dict(old or {})
        for k in ("name", "url", "token"):
            if k in d and d[k] is not None:
                s[k] = str(d[k]).strip()
        if "url" in s:
            s["url"] = s["url"].rstrip("/")
            if s["url"] and not s["url"].startswith(("http://", "https://")):
                s["url"] = "http://" + s["url"]
        if "groups" in d and d["groups"] is not None:
            g = d["groups"]
            if isinstance(g, str):
                g = [x for x in re.split(r"[,\s]+", g) if x]
            s["groups"] = sorted({str(x).strip().lower() for x in g if str(x).strip()})
        for k in ("enabled", "sync_cues"):
            if k in d and d[k] is not None:
                s[k] = as_bool(d[k])
        s.setdefault("groups", [])
        s.setdefault("enabled", True)
        s.setdefault("sync_cues", False)
        return s

    def add_screen(self, d: Dict[str, Any]) -> Dict[str, Any]:
        if not d.get("url"):
            raise HTTPException(400, "url is required (e.g. http://10.0.0.21:8080)")
        sid = uuid.uuid4().hex[:8]
        s = self._clean_screen(d)
        s.setdefault("name", sid)
        s["id"] = sid
        self.screens[sid] = s
        self.save()
        return s

    def register(self, d: Dict[str, Any], client_ip: str) -> Dict[str, Any]:
        """Self-registration from a player's heartbeat."""
        url = (d.get("url") or "").rstrip("/") or f"http://{client_ip}:{int(d.get('port') or 8080)}"
        name = str(d.get("name") or client_ip)
        existing = next((s for s in self.screens.values()
                         if s.get("url") == url or (s.get("name") == name and s.get("auto"))), None)
        if existing:
            existing.update(url=url, token=d.get("token", existing.get("token", "")))
            if not existing.get("groups") and d.get("groups"):
                existing["groups"] = self._clean_screen({"groups": d["groups"]})["groups"]
            self.save()
            return existing
        s = self.add_screen({"name": name, "url": url, "token": d.get("token", ""),
                             "groups": d.get("groups") or []})
        s["auto"] = True
        self.save()
        log.info("screen registered: %s (%s)", name, url)
        self.notify({"event": "screens"})
        return s

    def groups(self) -> Dict[str, List[str]]:
        out: Dict[str, List[str]] = {}
        for s in self.screens.values():
            for g in s.get("groups") or []:
                out.setdefault(g, []).append(s["id"])
        return dict(sorted(out.items()))

    def resolve_target(self, target: str) -> List[Dict[str, Any]]:
        """all | group:<g> | screen:<id|name> | <id|name|group>"""
        t = str(target or "all").strip()
        tl = t.lower()
        enabled = [s for s in self.screens.values() if s.get("enabled", True)]
        if tl in ("all", "*", ""):
            return enabled
        kind, _, val = tl.partition(":") if ":" in tl else ("", "", tl)
        if kind in ("", "screen"):
            hits = [s for s in enabled if s["id"] == val or s.get("name", "").lower() == val]
            if hits or kind == "screen":
                return hits
        if kind in ("", "group"):
            return [s for s in enabled if val in (s.get("groups") or [])]
        return []

    def screen_view(self, s: Dict[str, Any]) -> Dict[str, Any]:
        out = {k: v for k, v in s.items() if k != "token"}
        out["has_token"] = bool(s.get("token"))
        out.update(self.live.get(s["id"], {"online": False}))
        out["sync"] = self.sync_jobs.get(s["id"])
        return out

    # ---- HTTP to players ---------------------------------------------------------
    def _headers(self, s: Dict[str, Any]) -> Dict[str, str]:
        return {"Authorization": f"Bearer {s['token']}"} if s.get("token") else {}

    async def call(self, s: Dict[str, Any], method: str, path: str, **kw) -> Any:
        assert self.client is not None
        r = await self.client.request(method, s["url"] + path, headers=self._headers(s), **kw)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:200]
            raise RuntimeError(f"{r.status_code} {detail}")
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return r.content

    async def command(self, target: str, action: str, ref: Optional[str] = None,
                      opts: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if action not in ACTIONS:
            raise HTTPException(400, f"action must be one of {ACTIONS}")
        screens = self.resolve_target(target)
        if not screens:
            raise HTTPException(404, f"no enabled screens match target '{target}'")
        if action in ("cue", "play") and not ref:
            raise HTTPException(400, f"{action} needs a cue number or media reference")
        body = {k: v for k, v in (opts or {}).items() if v is not None}
        path = f"/api/{action}" + (f"/{ref}" if ref else "")

        async def one(s):
            try:
                st = await self.call(s, "POST", path, json=body, timeout=4)
                self.live.setdefault(s["id"], {}).update(online=True, status=st, seen=time.time())
                return s["id"], {"ok": True, "name": s.get("name"), "status": st}
            except Exception as e:
                return s["id"], {"ok": False, "name": s.get("name"), "error": str(e)}

        results = dict(await asyncio.gather(*(one(s) for s in screens)))
        self.notify({"event": "screens"})
        ok = sum(1 for r in results.values() if r["ok"])
        return {"target": target, "action": action, "ref": ref, "ok": ok,
                "failed": len(results) - ok, "results": results}

    async def trigger_global_cue(self, ref: str) -> Dict[str, Any]:
        ref = str(ref).strip()
        cue = self.mcues.get(ref) or next(
            (v for v in self.mcues.values() if v.get("label", "").lower() == ref.lower()), None)
        if cue is None:
            raise HTTPException(404, f"global cue not found: {ref}")
        results = await asyncio.gather(*(
            self.command(a.get("target", "all"), a.get("action", "cue"), a.get("ref"),
                         {k: a.get(k) for k in ("loop", "duration", "after", "transition_ms")})
            for a in cue.get("actions", [])), return_exceptions=True)
        out = []
        for a, r in zip(cue.get("actions", []), results):
            if isinstance(r, Exception):
                out.append({"action": a, "error": getattr(r, "detail", str(r))})
            else:
                out.append(r)
        return {"cue": ref, "label": cue.get("label"), "actions": out}

    @staticmethod
    def clean_mcue(d: Dict[str, Any]) -> Dict[str, Any]:
        actions = []
        for a in d.get("actions") or []:
            action = str(a.get("action") or "cue").lower()
            if action not in ACTIONS:
                continue
            actions.append({
                "target": str(a.get("target") or "all"),
                "action": action,
                "ref": str(a.get("ref") or "") or None,
                "loop": as_bool(a["loop"]) if a.get("loop") not in (None, "") else None,
                "duration": as_float(a.get("duration")),
                "after": a.get("after") or None,
                "transition_ms": as_int(a.get("transition_ms")),
            })
        return {"label": str(d.get("label") or ""), "color": str(d.get("color") or ""),
                "actions": actions}

    # ---- polling -------------------------------------------------------------------
    async def poll_forever(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                log.exception("poll failed")
            await asyncio.sleep(self.POLL_S)

    async def poll_once(self) -> None:
        async def one(s):
            live = self.live.setdefault(s["id"], {"online": False})
            was = (live.get("online"), (live.get("status") or {}).get("clip"),
                   (live.get("status") or {}).get("mode"))
            try:
                st = await self.call(s, "GET", "/api/status", timeout=2)
                live.update(online=True, status=st, seen=time.time(), error=None)
                if not live.get("info") or time.time() - live.get("info_t", 0) > 60:
                    live["info"] = await self.call(s, "GET", "/api/info", timeout=3)
                    live["info_t"] = time.time()
            except Exception as e:
                live.update(online=False, error=str(e) or type(e).__name__)
            now = (live.get("online"), (live.get("status") or {}).get("clip"),
                   (live.get("status") or {}).get("mode"))
            return s, was != now

        results = await asyncio.gather(*(one(s) for s in list(self.screens.values())
                                         if s.get("enabled", True)))
        changed = [s for s, c in results if c]
        for s in changed:
            self.notify({"event": "screen", "screen": self.screen_view(s)})
        self.notify({"event": "screens_tick"})

    # ---- media sync -------------------------------------------------------------------
    def items_for(self, s: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for item in self.library.list():
            targets = item.get("targets") or ["all"]
            for t in targets:
                if s in self.resolve_target(t):
                    out.append(item)
                    break
        return out

    async def sync_screen(self, s: Dict[str, Any]) -> Dict[str, Any]:
        job = {"state": "running", "started": time.time(), "uploaded": 0, "removed": 0,
               "total": 0, "current": None, "error": None}
        self.sync_jobs[s["id"]] = job
        self.notify({"event": "screens"})
        try:
            remote = {i["id"]: i for i in await self.call(s, "GET", "/api/media", timeout=10)}
            wanted = self.items_for(s)
            todo = [i for i in wanted if remote.get(i["id"], {}).get("checksum") != i["checksum"]]
            job["total"] = len(todo)
            for item in todo:
                job["current"] = item["name"]
                self.notify({"event": "screens"})
                path = self.library.path_of(item)
                with open(path, "rb") as f:
                    await self.call(
                        s, "POST", "/api/media", timeout=httpx.Timeout(30, write=None, read=600),
                        files={"file": (item["original_filename"], f)},
                        data={"category": item["category"], "name": item["name"], "id": item["id"],
                              "expires": item.get("expires") or "",
                              "scale_mode": item.get("scale_mode") or ""})
                await self.call(s, "PATCH", f"/api/media/{item['id']}",
                                json={"tags": [MANAGED_TAG]}, timeout=10)
                job["uploaded"] += 1
            # metadata-only changes (rename etc.)
            for item in wanted:
                r = remote.get(item["id"])
                if r and item not in todo and (r.get("name"), r.get("category")) != \
                        (item["name"], item["category"]):
                    await self.call(s, "PATCH", f"/api/media/{item['id']}", timeout=10,
                                    json={"name": item["name"], "category": item["category"]})
            # remove items the master pushed earlier but no longer targets at this screen
            wanted_ids = {i["id"] for i in wanted}
            for rid, r in remote.items():
                if MANAGED_TAG in (r.get("tags") or []) and rid not in wanted_ids:
                    await self.call(s, "DELETE", f"/api/media/{rid}", timeout=10)
                    job["removed"] += 1
            if s.get("sync_cues"):
                await self.call(s, "PUT", "/api/cues", json=self.library.list_cues(), timeout=10)
                job["cues"] = True
            job["state"] = "done"
        except Exception as e:
            job.update(state="error", error=str(e) or type(e).__name__)
            log.warning("sync to %s failed: %s", s.get("name"), e)
        job["current"] = None
        job["finished"] = time.time()
        self.notify({"event": "screens"})
        return job

    async def sync(self, target: str = "all") -> List[str]:
        screens = self.resolve_target(target)
        for s in screens:
            if (self.sync_jobs.get(s["id"]) or {}).get("state") != "running":
                asyncio.create_task(self.sync_screen(s))
        return [s["id"] for s in screens]


def create_master_app(fleet: Fleet) -> FastAPI:
    lib, settings = fleet.library, fleet.settings
    get_token = lambda: settings.section("control").get("api_token", "")  # noqa: E731
    api = Depends(make_auth(get_token))
    bus = Broadcaster()

    def publish(msg):
        if msg.get("event") == "screens_tick":
            return
        bus.publish(msg)
    fleet.subscribe(publish)

    app = FastAPI(title="CuePlayer Master", version=VERSION,
                  description="Fleet control for CuePlayer screens. Commands accept GET or POST.")

    @app.on_event("startup")
    async def _start():
        fleet.client = httpx.AsyncClient()
        asyncio.create_task(fleet.poll_forever())

    @app.get("/api/info", dependencies=[api])
    def info():
        return {"app": "cueplayer", "role": "master", "version": VERSION, "name": hostname(),
                "screens": len(fleet.screens), "disk": lib.disk_usage()}

    # ---- screens ---------------------------------------------------------------
    @app.get("/api/screens", dependencies=[api])
    def screens():
        return [fleet.screen_view(s) for s in sorted(fleet.screens.values(),
                                                     key=lambda s: s.get("name", "").lower())]

    @app.post("/api/screens", dependencies=[api])
    async def screen_add(p: Dict[str, Any] = Depends(params)):
        s = fleet.add_screen(p)
        bus.publish({"event": "screens"})
        return fleet.screen_view(s)

    @app.post("/api/screens/register", dependencies=[api])
    async def screen_register(request: Request, p: Dict[str, Any] = Depends(params)):
        s = fleet.register(p, request.client.host if request.client else "127.0.0.1")
        return {"id": s["id"], "name": s["name"]}

    def get_screen(sid: str) -> Dict[str, Any]:
        s = fleet.screens.get(sid)
        if not s:
            raise HTTPException(404, "screen not found")
        return s

    @app.patch("/api/screens/{sid}", dependencies=[api])
    async def screen_patch(sid: str, p: Dict[str, Any] = Depends(params)):
        s = get_screen(sid)
        fleet.screens[sid] = dict(fleet._clean_screen(p, s), id=sid)
        fleet.save()
        bus.publish({"event": "screens"})
        return fleet.screen_view(fleet.screens[sid])

    @app.delete("/api/screens/{sid}", dependencies=[api])
    def screen_delete(sid: str):
        get_screen(sid)
        fleet.screens.pop(sid)
        fleet.live.pop(sid, None)
        fleet.save()
        bus.publish({"event": "screens"})
        return {"ok": True}

    @app.get("/api/screens/{sid}/preview.jpg", dependencies=[api])
    async def screen_preview(sid: str):
        s = get_screen(sid)
        try:
            data = await fleet.call(s, "GET", "/api/preview.jpg", timeout=3)
        except Exception:
            return Response(status_code=204)
        if not data:
            return Response(status_code=204)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/groups", dependencies=[api])
    def groups():
        return fleet.groups()

    # ---- commands -----------------------------------------------------------------
    def opts(p: Dict[str, Any]) -> Dict[str, Any]:
        o: Dict[str, Any] = {}
        if "loop" in p:
            o["loop"] = as_bool(p["loop"])
        if p.get("duration") not in (None, ""):
            o["duration"] = as_float(p["duration"])
        if p.get("after"):
            o["after"] = p["after"]
        t = p.get("transition", p.get("transition_ms"))
        if t not in (None, ""):
            o["transition_ms"] = as_int(t)
        return o

    @app.api_route("/api/target/{target}/{action}", methods=["GET", "POST"], dependencies=[api])
    async def target_cmd(target: str, action: str, p: Dict[str, Any] = Depends(params)):
        return await fleet.command(target, action, p.get("ref"), opts(p))

    @app.api_route("/api/target/{target}/{action}/{ref}", methods=["GET", "POST"],
                   dependencies=[api])
    async def target_cmd_ref(target: str, action: str, ref: str,
                             p: Dict[str, Any] = Depends(params)):
        return await fleet.command(target, action, ref, opts(p))

    @app.api_route("/api/gcue/{ref}", methods=["GET", "POST"], dependencies=[api])
    async def gcue(ref: str):
        return await fleet.trigger_global_cue(ref)

    # ---- global cues -----------------------------------------------------------
    @app.get("/api/gcues", dependencies=[api])
    def gcues():
        return dict(sorted(fleet.mcues.items(),
                           key=lambda kv: (0, int(kv[0]), "") if kv[0].isdigit() else (1, 0, kv[0])))

    @app.put("/api/gcues/{number}", dependencies=[api])
    async def gcue_set(number: str, request: Request):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", number):
            raise HTTPException(400, "invalid cue number")
        fleet.mcues[number] = fleet.clean_mcue(await request.json())
        fleet.save()
        bus.publish({"event": "gcues"})
        return fleet.mcues[number]

    @app.delete("/api/gcues/{number}", dependencies=[api])
    def gcue_delete(number: str):
        if fleet.mcues.pop(number, None) is None:
            raise HTTPException(404, "not found")
        fleet.save()
        bus.publish({"event": "gcues"})
        return {"ok": True}

    # ---- shared player cue list (pushed to screens with sync_cues) --------------
    @app.get("/api/cues", dependencies=[api])
    def cues():
        return lib.list_cues()

    @app.put("/api/cues/{number}", dependencies=[api])
    async def cue_set(number: str, request: Request):
        try:
            return lib.set_cue(number, await request.json())
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/cues/{number}", dependencies=[api])
    def cue_delete(number: str):
        if not lib.delete_cue(number):
            raise HTTPException(404, "not found")
        return {"ok": True}

    # ---- media ----------------------------------------------------------------
    @app.get("/api/media", dependencies=[api])
    def media_list(category: Optional[str] = None):
        return lib.list(category)

    @app.post("/api/media", dependencies=[api])
    async def media_upload(file: UploadFile = File(...), category: str = Form("library"),
                           name: str = Form(""), expires: str = Form(""),
                           targets: str = Form("all"), sync: str = Form("1")):
        if category not in CATEGORIES:
            raise HTTPException(400, f"category must be one of {CATEGORIES}")
        tmp = lib.incoming_dir / f"{uuid.uuid4().hex}.part"
        try:
            with open(tmp, "wb") as f:
                while True:
                    chunk = await file.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
            item = await asyncio.get_running_loop().run_in_executor(
                None, lambda: lib.add_file(tmp, file.filename or "upload", category,
                                           name or None, None, expires or None))
        except ValueError as e:
            raise HTTPException(400, str(e))
        finally:
            tmp.unlink(missing_ok=True)
        tlist = [t.strip() for t in targets.split(",") if t.strip()] or ["all"]
        _set_targets(item["id"], tlist)
        if as_bool(sync):
            for t in tlist:
                await fleet.sync(t)
        bus.publish({"event": "library"})
        return lib.get(item["id"])

    def _set_targets(item_id: str, targets: List[str]) -> None:
        with lib._lock:
            lib.items[item_id]["targets"] = targets
            lib._save()

    @app.patch("/api/media/{item_id}", dependencies=[api])
    async def media_patch(item_id: str, p: Dict[str, Any] = Depends(params)):
        try:
            lib.update(item_id, p)
        except KeyError:
            raise HTTPException(404, "not found")
        if "targets" in p:
            t = p["targets"]
            _set_targets(item_id, t if isinstance(t, list) else
                         [x.strip() for x in str(t).split(",") if x.strip()] or ["all"])
        bus.publish({"event": "library"})
        return lib.get(item_id)

    @app.delete("/api/media/{item_id}", dependencies=[api])
    async def media_delete(item_id: str, sync: str = "1"):
        if not lib.delete(item_id):
            raise HTTPException(404, "not found")
        if as_bool(sync):
            await fleet.sync("all")
        bus.publish({"event": "library"})
        return {"ok": True}

    @app.get("/api/media/{item_id}/thumb", dependencies=[api])
    def media_thumb(item_id: str):
        p = lib.thumb_of(item_id)
        if not p:
            raise HTTPException(404, "no thumbnail")
        return FileResponse(p, media_type="image/jpeg")

    @app.api_route("/api/sync", methods=["GET", "POST"], dependencies=[api])
    async def sync_all(p: Dict[str, Any] = Depends(params)):
        return {"started": await fleet.sync(p.get("target") or "all")}

    # ---- settings (master only uses "control") ---------------------------------
    @app.get("/api/settings", dependencies=[api])
    def settings_get():
        return {"control": settings.section("control")}

    @app.patch("/api/settings", dependencies=[api])
    async def settings_patch(request: Request):
        body = await request.json()
        settings.update({"control": body.get("control", {})})
        return {"control": settings.section("control")}

    @app.websocket("/api/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        if not await ws_authorised(websocket, get_token):
            return
        await bus.serve(websocket, {"event": "hello", "role": "master"})

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "master.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    return app


# ---- TCP/UDP protocol for the master --------------------------------------------
def master_handler(fleet: Fleet):
    """
    GCUE <n|label>                      trigger a global cue
    <target> CUE|PLAY <ref> [opts]      target = ALL | GROUP <g> | SCREEN <name> | <name/group>
    <target> STOP|DEFAULT|BLACK|NEXT
    CUE <n> [opts]                      shorthand: global cue <n> if defined, else ALL CUE <n>
    STATUS                              one line per screen
    """
    async def handle(tokens: List[str], session: Dict[str, Any]) -> str:
        cmd = tokens[0].upper()
        if cmd == "STATUS":
            lines = []
            for s in fleet.screens.values():
                live = fleet.live.get(s["id"], {})
                if live.get("online"):
                    lines.append(status_line(live.get("status") or {},
                                             f"STATUS screen={q(s.get('name'))} online=1"))
                else:
                    lines.append(f"STATUS screen={q(s.get('name'))} online=0")
            return "\r\n".join(lines) or "STATUS screens=0"
        if cmd == "HELP":
            return "OK commands: GCUE <n> | [ALL|GROUP g|SCREEN s] CUE|PLAY <ref> | STOP | BLACK | DEFAULT | NEXT | STATUS"
        if cmd == "GCUE":
            r = await fleet.trigger_global_cue(tokens[1])
            ok = sum(a.get("ok", 0) for a in r["actions"])
            failed = sum(a.get("failed", 0) for a in r["actions"]) + \
                sum(1 for a in r["actions"] if "error" in a)
            return f"OK gcue={q(tokens[1])} ok={ok} failed={failed}"
        if cmd == "CUE" and len(tokens) > 1 and tokens[1] in fleet.mcues:
            return await handle(["GCUE", tokens[1]], session)
        # target prefix
        target = "all"
        if cmd == "ALL":
            tokens = tokens[1:]
        elif cmd in ("GROUP", "SCREEN") and len(tokens) > 2:
            target = f"{cmd.lower()}:{tokens[1]}"
            tokens = tokens[2:]
        elif cmd not in ACTIONS_UPPER:
            target = tokens[0]
            tokens = tokens[1:]
        if not tokens:
            return "ERR missing command"
        action = tokens[0].lower()
        if action not in ACTIONS:
            return f"ERR unknown command {tokens[0]}"
        ref = tokens[1] if action in ("cue", "play") and len(tokens) > 1 else None
        rest = tokens[2:] if ref else tokens[1:]
        try:
            r = await fleet.command(target, action, ref, parse_opts(rest))
        except HTTPException as e:
            return f"ERR {e.detail}"
        return f"OK target={q(target)} action={action} ok={r['ok']} failed={r['failed']}"
    return handle


ACTIONS_UPPER = {a.upper() for a in ACTIONS}


def master_event_line(msg: Dict[str, Any]) -> Optional[str]:
    if msg.get("event") != "screen":
        return None
    s = msg["screen"]
    if not s.get("online"):
        return f"EVENT screen={q(s.get('name'))} online=0"
    return status_line(s.get("status") or {}, f"EVENT screen={q(s.get('name'))} online=1")
