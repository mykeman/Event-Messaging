"""Bits shared by the player and master web apps."""
from __future__ import annotations

import asyncio
import hmac
import json
import socket
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import HTTPException, Request, WebSocket

VERSION = "1.0.0"
WEB_DIR = Path(__file__).parent / "web"


def check_token(expected: str, provided: Optional[str]) -> bool:
    if not expected:
        return True
    return bool(provided) and hmac.compare_digest(expected, provided)


def token_from(req: Any) -> Optional[str]:
    """Extract a token from Authorization, X-API-Key or ?token= (works for Request and WebSocket)."""
    auth = req.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return req.headers.get("x-api-key") or req.query_params.get("token")


def make_auth(get_token: Callable[[], str]):
    async def auth(request: Request) -> None:
        if not check_token(get_token(), token_from(request)):
            raise HTTPException(status_code=401, detail="invalid or missing API token")
    return auth


async def ws_authorised(ws: WebSocket, get_token: Callable[[], str]) -> bool:
    if check_token(get_token(), token_from(ws)):
        return True
    await ws.close(code=4401)
    return False


async def params(request: Request) -> Dict[str, Any]:
    """Merge query string and JSON/form body so every command works as GET or POST.

    Q-SYS HttpClient, browsers and curl can all hit the same endpoints.
    """
    out: Dict[str, Any] = dict(request.query_params)
    out.pop("token", None)
    if request.method in ("POST", "PUT", "PATCH"):
        ctype = request.headers.get("content-type", "")
        try:
            if "json" in ctype:
                body = await request.json()
                if isinstance(body, dict):
                    out.update(body)
            elif "form" in ctype:
                form = await request.form()
                out.update({k: v for k, v in form.items() if isinstance(v, str)})
        except (ValueError, json.JSONDecodeError):
            raise HTTPException(400, "invalid request body")
    return out


def as_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "loop")


def as_float(v: Any) -> Optional[float]:
    if v in (None, "", "null"):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, f"not a number: {v}")


def as_int(v: Any) -> Optional[int]:
    f = as_float(v)
    return None if f is None else int(f)


def hostname() -> str:
    return socket.gethostname().split(".")[0]


class Broadcaster:
    """Fan-out of JSON messages to websocket clients."""

    def __init__(self) -> None:
        self.queues: "set[asyncio.Queue]" = set()

    def publish(self, msg: Dict[str, Any]) -> None:
        for q in list(self.queues):
            if q.qsize() > 50:
                continue  # slow client; drop
            q.put_nowait(msg)

    async def serve(self, ws: WebSocket, initial: Dict[str, Any]) -> None:
        q: asyncio.Queue = asyncio.Queue()
        self.queues.add(q)
        try:
            await ws.send_json(initial)
            while True:
                msg = await q.get()
                await ws.send_json(msg)
        except Exception:
            pass
        finally:
            self.queues.discard(q)
