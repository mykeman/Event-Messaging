"""Plain-text TCP/UDP control protocol (for Q-SYS TcpSocket/UdpSocket, Crestron, telnet...).

One command per line, terminated by CR, LF or CRLF. Case-insensitive keywords.

    PING                         -> PONG
    AUTH <token>                 -> OK AUTH            (only when an API token is set)
    CUE <n|label> [opts]         -> OK ...
    PLAY <media> [opts]          -> OK ...             media = id, name or filename; quote names with spaces
    STOP | DEFAULT               -> OK ...             return to default content
    BLACK [FADE=ms]              -> OK ...
    NEXT                         -> OK ...
    STATUS                       -> STATUS mode=... cue=... media="..." ...

    opts: LOOP | HOLD | DURATION=<s> | AFTER=default|hold|black|cue:<n> | FADE=<ms>

Replies start with OK, ERR or STATUS. TCP clients also receive unsolicited
"EVENT mode=... cue=... media=..." lines whenever playback changes, which is
handy for button feedback in Q-SYS.
"""
from __future__ import annotations

import asyncio
import logging
import shlex
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from .common import check_token

log = logging.getLogger("cueplayer.tcp")

Handler = Callable[[List[str], Dict[str, Any]], Awaitable[str]]


def parse_opts(tokens: List[str]) -> Dict[str, Any]:
    """LOOP HOLD DURATION=10 AFTER=black FADE=0 -> dict"""
    o: Dict[str, Any] = {}
    for t in tokens:
        u = t.upper()
        if u == "LOOP":
            o["loop"] = True
        elif u == "NOLOOP":
            o["loop"] = False
        elif u == "HOLD":
            o["after"] = "hold"
        elif u == "FORCE":
            o["force"] = True
        elif u == "CUT":
            o["transition_type"] = "cut"
        elif "=" in t:
            k, v = t.split("=", 1)
            k = k.lower()
            if k in ("dur", "duration"):
                o["duration"] = float(v)
            elif k in ("fade", "transition_ms"):
                o["transition_ms"] = int(float(v))
            elif k == "transition":
                if v.lower() in ("cut", "fade", "dip", "slide-left", "slide-right", "slide-up", "slide-down"):
                    o["transition_type"] = v.lower()
                else:
                    o["transition_ms"] = int(float(v))
            elif k == "after":
                o["after"] = v.lower() if not v.lower().startswith("cue:") else "cue:" + v[4:]
            elif k == "loop":
                o["loop"] = v.lower() in ("1", "true", "yes", "on")
    return o


def q(v: Any) -> str:
    s = "" if v is None else str(v)
    return '"' + s.replace('"', "'") + '"' if (" " in s or not s) else s


def status_line(st: Dict[str, Any], prefix: str = "STATUS") -> str:
    m = st.get("media") or {}
    parts = [prefix, f"mode={st.get('mode')}", f"cue={q(st.get('cue') or '')}",
             f"media={q(m.get('name') or '')}", f"id={m.get('id') or ''}",
             f"kind={m.get('kind') or ''}", f"lock={1 if st.get('lock') else 0}",
             f"label={q(st.get('label') or '')}"]
    if st.get("elapsed") is not None:
        parts.append(f"elapsed={st['elapsed']:.1f}")
    if st.get("duration"):
        parts.append(f"duration={float(st['duration']):.1f}")
    return " ".join(parts)


def split(line: str) -> List[str]:
    try:
        return shlex.split(line)
    except ValueError:
        return line.split()


class LineServer:
    def __init__(self, handler: Handler, get_token: Callable[[], str],
                 subscribe: Optional[Callable] = None,
                 event_line: Optional[Callable[[Dict[str, Any]], Optional[str]]] = None):
        self.handler = handler
        self.get_token = get_token
        self.subscribe = subscribe
        self.event_line = event_line
        self.servers: List[Any] = []

    async def start(self, host: str, tcp_port: int, udp_port: int) -> None:
        if tcp_port:
            srv = await asyncio.start_server(self._client, host, tcp_port)
            self.servers.append(srv)
            log.info("TCP control on %s:%s", host, tcp_port)
        if udp_port:
            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _Udp(self), local_addr=(host, udp_port))
            self.servers.append(transport)
            log.info("UDP control on %s:%s", host, udp_port)

    async def execute(self, line: str, session: Dict[str, Any]) -> Optional[str]:
        line = line.strip()
        if not line:
            return None
        tokens = split(line)
        cmd = tokens[0].upper()
        if cmd == "PING":
            return "PONG"
        if cmd == "AUTH":
            session["auth"] = check_token(self.get_token(), tokens[1] if len(tokens) > 1 else "")
            return "OK AUTH" if session["auth"] else "ERR AUTH"
        if not session.get("auth") and not check_token(self.get_token(), None):
            return "ERR AUTH required"
        try:
            return await self.handler(tokens, session)
        except Exception as e:  # report every failure to the caller, keep the socket
            return f"ERR {str(e) or type(e).__name__}".replace("\n", " ")

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        log.info("TCP client connected %s", peer)
        session: Dict[str, Any] = {"auth": False, "events": True, "peer": peer}
        lock = asyncio.Lock()

        async def send(text: str) -> None:
            async with lock:
                writer.write((text + "\r\n").encode())
                await writer.drain()

        unsubscribe = None
        if self.subscribe and self.event_line:
            def on_event(msg):
                if not session.get("events"):
                    return
                if not (session.get("auth") or check_token(self.get_token(), None)):
                    return
                line = self.event_line(msg)
                if line:
                    asyncio.ensure_future(send(line))
            unsubscribe = self.subscribe(on_event)
        try:
            buf = b""
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                buf += data.replace(b"\r", b"\n")
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    line = raw.decode(errors="replace")
                    if line.strip().upper() in ("EVENTS OFF", "EVENTS ON"):
                        session["events"] = line.strip().upper().endswith("ON")
                        await send("OK EVENTS")
                        continue
                    reply = await self.execute(line, session)
                    if reply:
                        await send(reply)
                if len(buf) > 65536:
                    buf = b""
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if unsubscribe:
                unsubscribe()
            writer.close()
            log.info("TCP client disconnected %s", peer)


class _Udp(asyncio.DatagramProtocol):
    def __init__(self, server: LineServer):
        self.server = server
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr: Tuple[str, int]) -> None:
        async def handle():
            # UDP is stateless: allow "AUTH <token> <command...>" in one datagram
            for line in data.decode(errors="replace").replace("\r", "\n").split("\n"):
                session = {"auth": False}
                tokens = split(line)
                if len(tokens) > 2 and tokens[0].upper() == "AUTH":
                    await self.server.execute(f"AUTH {tokens[1]}", session)
                    line = " ".join(shlex.quote(t) for t in tokens[2:])
                reply = await self.server.execute(line, session)
                if reply:
                    self.transport.sendto((reply + "\r\n").encode(), addr)
        asyncio.ensure_future(handle())


def player_handler(ctrl) -> Handler:
    async def handle(tokens: List[str], session: Dict[str, Any]) -> str:
        cmd = tokens[0].upper()
        args = tokens[1:]
        if cmd == "CUE":
            if not args:
                return "ERR CUE needs a cue number"
            st = ctrl.play_cue(args[0], **parse_opts(args[1:]))
        elif cmd == "PLAY":
            if not args:
                return "ERR PLAY needs a media name or id"
            st = ctrl.play(args[0], **parse_opts(args[1:]))
        elif cmd in ("STOP", "DEFAULT", "NORMAL", "CLEAR"):
            st = ctrl.play_default(parse_opts(args).get("transition_ms"))
        elif cmd == "BLACK":
            st = ctrl.black(parse_opts(args).get("transition_ms"))
        elif cmd == "TEST":
            name = args[0] if args and "=" not in args[0] and args[0].upper() not in (
                "TONE", "NOLABEL", "FORCE") else "smpte"
            if name.lower() in ("off", "stop", "clear"):
                st = ctrl.play_default()
            else:
                flags = {a.upper() for a in args}
                st = ctrl.test_pattern(name, "TONE" in flags, "NOLABEL" not in flags,
                                       force="FORCE" in flags)
        elif cmd == "NEXT":
            st = ctrl.next(bool(parse_opts(args).get("force")))
        elif cmd == "STATUS":
            return status_line(ctrl.status())
        elif cmd == "HELP":
            return "OK commands: PING AUTH CUE PLAY NORMAL BLACK NEXT TEST STATUS EVENTS ON|OFF (opts: LOOP HOLD FORCE)"
        else:
            return f"ERR unknown command {cmd}"
        return status_line(st, "OK")
    return handle


def player_event_line(msg: Dict[str, Any]) -> Optional[str]:
    if msg.get("event") == "error":
        return f"EVENT error={q(msg.get('error'))}"
    if msg.get("event") in ("play", "black", "hold", "status", "started") and msg.get("status"):
        if msg["event"] == "started":
            return None
        return status_line(msg["status"], "EVENT")
    return None
