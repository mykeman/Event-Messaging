"""Entry point:  python -m cueplayer [--role player|master] [--data DIR]"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

import uvicorn

from .common import VERSION, hostname
from .config import Settings
from .library import Library
from .tcp_control import LineServer

log = logging.getLogger("cueplayer")


async def fleet_heartbeat(settings: Settings) -> None:
    """Players with fleet.master_url set announce themselves to the master."""
    import httpx

    async with httpx.AsyncClient() as client:
        last_err = None
        while True:
            fleet, control = settings.section("fleet"), settings.section("control")
            sent = (fleet, control)
            url = (fleet.get("master_url") or "").rstrip("/")
            if url:
                if not url.startswith(("http://", "https://")):
                    url = "http://" + url
                headers = {"Authorization": f"Bearer {fleet['master_token']}"} \
                    if fleet.get("master_token") else {}
                body = {"name": fleet.get("screen_name") or hostname(),
                        "groups": fleet.get("groups") or [],
                        "url": fleet.get("advertise_url") or "",
                        "port": control.get("http_port", 8080),
                        "token": control.get("api_token", ""),
                        "version": VERSION}
                try:
                    r = await client.post(url + "/api/screens/register", json=body,
                                          headers=headers, timeout=5)
                    r.raise_for_status()
                    if last_err:
                        log.info("registered with master %s", url)
                    last_err = None
                except Exception as e:
                    if str(e) != last_err:
                        log.warning("master registration failed (%s): %s", url, e)
                    last_err = str(e)
            # Re-register every 30 s, or at once if fleet/token settings change
            for _ in range(30):
                await asyncio.sleep(1)
                if (settings.section("fleet"), settings.section("control")) != sent:
                    break


async def run(args) -> None:
    data = Path(args.data).expanduser()
    data.mkdir(parents=True, exist_ok=True)
    settings = Settings(data)
    library = Library(data)
    control = settings.section("control")
    http_port = args.port or control.get("http_port", 8080)
    tcp_port = control.get("tcp_port", 7700) if args.tcp_port is None else args.tcp_port
    udp_port = control.get("udp_port", 7701) if args.udp_port is None else args.udp_port
    get_token = lambda: settings.section("control").get("api_token", "")  # noqa: E731

    if args.role == "master":
        from .master import Fleet, create_master_app, master_event_line, master_handler

        fleet = Fleet(data, settings, library)
        app = create_master_app(fleet)
        lines = LineServer(master_handler(fleet), get_token, fleet.subscribe, master_event_line)
        on_stop = lambda: None  # noqa: E731
    else:
        from .api import create_player_app
        from .controller import Controller
        from .tcp_control import player_event_line, player_handler

        ctrl = Controller(settings, library, force_mock=args.mock)
        await ctrl.start()
        app = create_player_app(ctrl)
        lines = LineServer(player_handler(ctrl), get_token, ctrl.subscribe, player_event_line)
        asyncio.create_task(fleet_heartbeat(settings))
        on_stop = ctrl.shutdown

    await lines.start(args.host, tcp_port, udp_port)
    # Don't wait on open GUI websockets at shutdown (systemctl restart would otherwise hang)
    config = uvicorn.Config(app, host=args.host, port=http_port, log_level="warning",
                            access_log=False, timeout_graceful_shutdown=3)
    server = uvicorn.Server(config)
    log.info("CuePlayer %s (%s) - web GUI on http://%s:%s/", VERSION, args.role,
             hostname(), http_port)
    try:
        await server.serve()
    finally:
        on_stop()


def main() -> None:
    p = argparse.ArgumentParser(prog="cueplayer", description="Cue-triggered media player")
    p.add_argument("--role", choices=("player", "master"),
                   default=os.environ.get("CUEPLAYER_ROLE", "player"))
    p.add_argument("--data", default=os.environ.get("CUEPLAYER_DATA", "~/.cueplayer"),
                   help="data directory (media, settings)")
    p.add_argument("--host", default=os.environ.get("CUEPLAYER_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", 0)) or None,
                   help="HTTP port (overrides settings; also $PORT)")
    p.add_argument("--tcp-port", type=int, default=None)
    p.add_argument("--udp-port", type=int, default=None)
    p.add_argument("--mock", action="store_true", help="use the mock engine (no GStreamer)")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
