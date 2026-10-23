"""HTTP API + web GUI for a player (screen endpoint)."""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .common import (VERSION, WEB_DIR, Broadcaster, transition_params, no_cache_gui, as_bool, as_float, as_int, hostname,
                     make_auth, params, ws_authorised)
from .controller import Blocked, Controller, PlayError
from .library import CATEGORIES, Library

log = logging.getLogger("cueplayer.api")


def system_info() -> Dict[str, Any]:
    info: Dict[str, Any] = {"hostname": hostname()}
    try:
        info["load"] = os.getloadavg()
    except OSError:
        pass
    for p in ("/sys/class/thermal/thermal_zone0/temp",):
        try:
            info["cpu_temp_c"] = round(int(Path(p).read_text()) / 1000, 1)
        except (OSError, ValueError):
            pass
    try:
        info["uptime_s"] = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return info


def create_player_app(ctrl: Controller) -> FastAPI:
    lib: Library = ctrl.library
    settings = ctrl.settings
    get_token = lambda: settings.section("control").get("api_token", "")  # noqa: E731
    auth = make_auth(get_token)
    bus = Broadcaster()
    ctrl.subscribe(bus.publish)
    tc = ctrl.transcoder
    tc.on_change = lambda: ctrl.loop.call_soon_threadsafe(bus.publish, {"event": "library"})

    app = FastAPI(title="CuePlayer", version=VERSION,
                  description="Cue-triggered video/image playback for LED screens. "
                              "Every command accepts GET or POST with query or JSON params.")
    api = Depends(auth)

    def run(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Blocked as e:
            raise HTTPException(409, str(e))
        except PlayError as e:
            raise HTTPException(404, str(e))

    # ---- info / status -------------------------------------------------------
    @app.get("/api/info", dependencies=[api])
    def info():
        fleet = settings.section("fleet")
        return {"app": "cueplayer", "role": "player", "version": VERSION,
                "name": fleet.get("screen_name") or hostname(),
                "groups": fleet.get("groups") or [],
                "canvas": [settings.section("output")["canvas_width"],
                           settings.section("output")["canvas_height"]],
                "system": system_info(), "disk": lib.disk_usage()}

    @app.get("/api/status", dependencies=[api])
    def status():
        return ctrl.status()

    @app.get("/api/history", dependencies=[api])
    def history():
        return list(reversed(ctrl.history))

    # ---- transport (GET or POST) ---------------------------------------------
    def play_opts(p: Dict[str, Any]) -> Dict[str, Any]:
        return {"loop": as_bool(p.get("loop", False)),
                "duration": as_float(p.get("duration")),
                "after": str(p.get("after") or "default"),
                "transition_ms": transition_params(p)[1],
                "transition_type": transition_params(p)[0],
                "force": as_bool(p.get("force", False))}

    @app.api_route("/api/play", methods=["GET", "POST"], dependencies=[api])
    async def play(p: Dict[str, Any] = Depends(params)):
        ref = p.get("media") or p.get("id") or p.get("name")
        if not ref:
            raise HTTPException(400, "media is required")
        return run(ctrl.play, ref, **play_opts(p))

    @app.api_route("/api/play/{ref}", methods=["GET", "POST"], dependencies=[api])
    async def play_ref(ref: str, p: Dict[str, Any] = Depends(params)):
        return run(ctrl.play, ref, **play_opts(p))

    @app.api_route("/api/cue/{ref}", methods=["GET", "POST"], dependencies=[api])
    async def cue(ref: str, p: Dict[str, Any] = Depends(params)):
        o = play_opts(p)
        overrides = {k: o[k] for k in ("duration", "transition_ms", "transition_type") if o[k] is not None}
        overrides["force"] = o["force"]
        if "loop" in p:
            overrides["loop"] = o["loop"]
        if "after" in p:
            overrides["after"] = o["after"]
        return run(ctrl.play_cue, ref, **overrides)

    @app.get("/api/test", dependencies=[api])
    def test_patterns():
        from .engine import TEST_PATTERNS
        return {k: v[1] for k, v in TEST_PATTERNS.items()}

    @app.api_route("/api/test/{pattern}", methods=["GET", "POST"], dependencies=[api])
    async def test_pattern(pattern: str, p: Dict[str, Any] = Depends(params)):
        """Test pattern on every output until the next command. ?tone=1 adds a 1 kHz tone,
        ?label=0 hides the screen name / canvas size caption."""
        if pattern.lower() in ("off", "stop", "clear"):
            return ctrl.play_default()
        return run(ctrl.test_pattern, pattern, as_bool(p.get("tone", False)),
                   as_bool(p.get("label", True)), as_int(p.get("transition")),
                   as_bool(p.get("force", False)))

    @app.api_route("/api/stop", methods=["GET", "POST"], dependencies=[api])
    async def stop(p: Dict[str, Any] = Depends(params)):
        return ctrl.stop(as_int(p.get("transition")))

    @app.api_route("/api/normal", methods=["GET", "POST"], dependencies=[api])
    @app.api_route("/api/clear", methods=["GET", "POST"], dependencies=[api])
    @app.api_route("/api/default", methods=["GET", "POST"], dependencies=[api])
    async def default(p: Dict[str, Any] = Depends(params)):
        return ctrl.play_default(as_int(p.get("transition")))

    @app.api_route("/api/black", methods=["GET", "POST"], dependencies=[api])
    async def black(p: Dict[str, Any] = Depends(params)):
        return ctrl.black(as_int(p.get("transition")))

    @app.api_route("/api/next", methods=["GET", "POST"], dependencies=[api])
    async def next_(p: Dict[str, Any] = Depends(params)):
        return run(ctrl.next, as_bool(p.get("force", False)))

    # ---- media -------------------------------------------------------------
    @app.get("/api/media", dependencies=[api])
    def media_list(category: Optional[str] = None):
        return lib.list(category)

    @app.post("/api/media", dependencies=[api])
    async def media_upload(file: UploadFile = File(...), category: str = Form("event"),
                           name: str = Form(""), id: str = Form(""), expires: str = Form(""),
                           scale_mode: str = Form("")):
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
                                           name or None, id or None, expires or None,
                                           None, scale_mode or None))
        except ValueError as e:
            raise HTTPException(400, str(e))
        finally:
            tmp.unlink(missing_ok=True)
        item = await asyncio.get_running_loop().run_in_executor(None, tc.evaluate, item["id"]) or item
        bus.publish({"event": "library"})
        return item

    @app.get("/api/media/{item_id}", dependencies=[api])
    def media_get(item_id: str):
        item = lib.get(item_id)
        if not item:
            raise HTTPException(404, "not found")
        return item

    @app.patch("/api/media/{item_id}", dependencies=[api])
    async def media_patch(item_id: str, p: Dict[str, Any] = Depends(params)):
        try:
            item = lib.update(item_id, p)
        except KeyError:
            raise HTTPException(404, "not found")
        if "scale_mode" in p:  # changes the size the playback copy should be
            item = await asyncio.get_running_loop().run_in_executor(None, tc.evaluate, item_id) or item
        bus.publish({"event": "library"})
        return item

    @app.post("/api/media/{item_id}/convert", dependencies=[api])
    async def media_convert(item_id: str):
        """Make a playback copy now, even if the file doesn't strictly need one."""
        if not lib.get(item_id):
            raise HTTPException(404, "not found")
        return await asyncio.get_running_loop().run_in_executor(None, tc.evaluate, item_id, True)

    @app.delete("/api/media/{item_id}/converted", dependencies=[api])
    def media_use_original(item_id: str):
        """Discard the playback copy and always play the uploaded file."""
        item = lib.get(item_id)
        if not item:
            raise HTTPException(404, "not found")
        if (item.get("playback") or {}).get("original_removed"):
            raise HTTPException(409, "the original was not kept (transcode.keep_original is off)")
        tc.use_original(item_id)
        return lib.get(item_id)

    @app.get("/api/network/interfaces", dependencies=[api])
    def network_interfaces():
        """Network interfaces with IPv4 addresses (for choosing the AES67 / PTP interface)."""
        from .aes67 import list_interfaces
        return list_interfaces()

    @app.get("/api/hardware", dependencies=[api])
    def hardware():
        from .hwinfo import detect
        return dict(detect(), converting=tc.current, queued=len(tc.pending))

    @app.delete("/api/media/{item_id}", dependencies=[api])
    def media_delete(item_id: str):
        tc.cancel(item_id)
        if (ctrl.state.get("media") or {}).get("id") == item_id:
            ctrl.stop(0)
        if not lib.delete(item_id):
            raise HTTPException(404, "not found")
        ctrl._prune_default(item_id)
        bus.publish({"event": "library"})
        return {"ok": True}

    @app.get("/api/media/{item_id}/file", dependencies=[api])
    def media_file(item_id: str):
        item = lib.get(item_id)
        if not item:
            raise HTTPException(404, "not found")
        return FileResponse(lib.path_of(item), filename=item["original_filename"])

    @app.get("/api/media/{item_id}/thumb", dependencies=[api])
    def media_thumb(item_id: str):
        p = lib.thumb_of(item_id)
        if not p:
            raise HTTPException(404, "no thumbnail")
        return FileResponse(p, media_type="image/jpeg",
                            headers={"Cache-Control": "max-age=3600"})

    # ---- cues -------------------------------------------------------------
    @app.get("/api/cues", dependencies=[api])
    def cues():
        return lib.list_cues()

    @app.post("/api/cues/scenarios", dependencies=[api])
    def cues_add_scenarios():
        """Add the standard scenarios (capacity, fire alarm, evacuate...) that are missing."""
        added = lib.add_standard_scenarios()
        bus.publish({"event": "cues"})
        return {"added": added, "cues": lib.list_cues()}

    @app.put("/api/cues", dependencies=[api])
    async def cues_replace(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "expected an object of {number: cue}")
        lib.replace_cues(body)
        bus.publish({"event": "cues"})
        return lib.list_cues()

    @app.put("/api/cues/{number}", dependencies=[api])
    async def cue_set(number: str, request: Request):
        try:
            cue = lib.set_cue(number, await request.json())
        except ValueError as e:
            raise HTTPException(400, str(e))
        bus.publish({"event": "cues"})
        return cue

    @app.delete("/api/cues/{number}", dependencies=[api])
    def cue_delete(number: str):
        if not lib.delete_cue(number):
            raise HTTPException(404, "not found")
        bus.publish({"event": "cues"})
        return {"ok": True}

    # ---- settings -----------------------------------------------------------
    @app.get("/api/settings", dependencies=[api])
    def settings_get():
        return settings.get()

    @app.patch("/api/settings", dependencies=[api])
    async def settings_patch(request: Request):
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "expected an object")
        try:
            out = ctrl.apply_settings(body)
        except ValueError as e:
            raise HTTPException(400, str(e))
        bus.publish({"event": "settings"})
        return out

    # ---- AES67 ------------------------------------------------------------
    @app.get("/api/aes67.sdp", dependencies=[api])
    def aes67_sdp():
        """SDP of the AES67 audio stream, for receivers that don't use SAP discovery."""
        if not settings.section("aes67").get("enabled"):
            raise HTTPException(404, "AES67 output is disabled")
        sdp = ctrl.engine.aes67_sdp()
        if not sdp:
            state = (ctrl.engine.status.get("aes67") or {}).get("state", "not running")
            raise HTTPException(503, f"AES67 stream not running: {state}")
        return Response(sdp, media_type="application/sdp",
                        headers={"Content-Disposition": 'inline; filename="cueplayer.sdp"'})

    # ---- preview ----------------------------------------------------------
    @app.get("/api/preview.jpg", dependencies=[api])
    def preview():
        data = ctrl.engine.preview_jpeg()
        if not data:
            return Response(status_code=204)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/preview.mjpg", dependencies=[api])
    async def preview_stream():
        fps = max(1, int(settings.section("preview").get("fps", 5)))

        async def gen():
            last = None
            while True:
                data = ctrl.engine.preview_jpeg()
                if data and data is not last:
                    last = data
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(data)).encode() + b"\r\n\r\n" + data + b"\r\n")
                await asyncio.sleep(1 / fps)

        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")

    # ---- websocket ----------------------------------------------------------
    @app.websocket("/api/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        if not await ws_authorised(websocket, get_token):
            return
        await bus.serve(websocket, {"event": "status", "status": ctrl.status()})

    # ---- GUI ------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "player.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    no_cache_gui(app)

    @app.exception_handler(Exception)
    async def unhandled(_req, exc):  # pragma: no cover
        log.exception("unhandled error")
        return JSONResponse({"detail": str(exc)}, status_code=500)

    return app
