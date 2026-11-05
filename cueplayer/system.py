"""System jobs for the web GUI: HDMI boot mode, software update, reboot.

The service runs as an unprivileged user, so the root-only parts go through
deploy/cueplayer-helper (installed to /usr/local/sbin by install.sh / update.sh, with a sudo
rule for exactly that file). On a unit without the helper - or on a dev machine - these
report themselves as unavailable instead of failing.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from .common import VERSION

log = logging.getLogger("cueplayer.system")

HELPER = "/usr/local/sbin/cueplayer-helper"
PREFIX = Path(__file__).resolve().parent.parent          # /opt/cueplayer when installed
REFRESH_RATES = [24, 25, 30, 50, 60]
COMMON_MODES = ["3840x2160", "2560x1440", "1920x1200", "1920x1080", "1680x1050", "1600x900",
                "1440x900", "1366x768", "1280x1024", "1280x800", "1280x720", "1024x768",
                "800x600", "720x576", "720x480"]


class SystemJobError(Exception):
    pass


def helper_available() -> Optional[str]:
    """None if the helper can be used, else why not."""
    if not Path(HELPER).exists():
        return ("the system helper isn't installed on this unit - run "
                "sudo ./deploy/update.sh once from the Event-Messaging clone")
    if not shutil.which("sudo"):
        return "sudo is not installed (apt install sudo, then re-run deploy/update.sh)"
    return None


def run_helper(*args: str, timeout: float = 120) -> Dict[str, Any]:
    why = helper_available()
    if why:
        raise SystemJobError(why)
    try:
        r = subprocess.run(["sudo", "-n", HELPER, *args], capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SystemJobError(f"{args[0]} timed out")
    out = (r.stdout or "").strip().splitlines()
    try:
        data = json.loads(out[-1]) if out else {}
    except ValueError:
        data = {}
    if r.returncode != 0 or not data.get("ok"):
        err = data.get("error") or (r.stderr or r.stdout or "").strip()[-300:]
        if "password is required" in err or "a terminal is required" in err:
            err = "the sudo rule for the helper is missing - re-run sudo ./deploy/update.sh"
        raise SystemJobError(err or f"{args[0]} failed")
    return data


# ---- version ---------------------------------------------------------------------
def build_info() -> Dict[str, Any]:
    info: Dict[str, Any] = {"version": VERSION}
    try:
        info.update(json.loads((Path(__file__).parent / "BUILD.json").read_text()))
    except (OSError, ValueError):
        # Running from a clone (development): ask git directly
        root = Path(__file__).resolve().parent.parent
        if (root / ".git").exists() and shutil.which("git"):
            try:
                r = subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%h%x09%cI%x09%s"],
                                   capture_output=True, text=True, timeout=5)
                parts = r.stdout.strip().split("\t", 2)
                if len(parts) == 3:
                    info.update(commit=parts[0], date=parts[1], subject=parts[2])
            except (OSError, subprocess.SubprocessError):
                pass
    return info


# ---- HDMI modes ------------------------------------------------------------------
def connectors(drm: str = "/sys/class/drm") -> List[Dict[str, Any]]:
    """Display connectors with the resolutions their screen reports (sysfs lists sizes
    only; refresh rates come from the standard list)."""
    out = []
    for path in sorted(Path(drm).glob("card*-*")):
        name = re.sub(r"^card\d+-", "", path.name)
        if not re.match(r"(HDMI|DP|eDP|DSI|VGA|DVI)", name):
            continue
        try:
            status = path.joinpath("status").read_text().strip()
        except OSError:
            continue
        try:
            modes = [m.strip() for m in path.joinpath("modes").read_text().splitlines() if m.strip()]
        except OSError:
            modes = []
        seen: List[str] = []
        for m in modes:
            m = re.sub(r"[^0-9x].*$", "", m)   # 1920x1080i -> 1920x1080
            if m and m not in seen:
                seen.append(m)
        out.append({"name": name, "status": status, "modes": seen})
    return out


def display_modes(port: Optional[str] = None) -> Dict[str, Any]:
    from .engine import boot_video_modes
    conns = connectors()
    screen = next((c for c in conns if c["name"] == port), None) or \
        next((c for c in conns if c["status"] == "connected" and c["modes"]), None)
    reported = screen["modes"] if screen else []
    sizes = reported + [m for m in COMMON_MODES if m not in reported]
    return {"connectors": conns, "port": screen["name"] if screen else port,
            "reported": reported, "sizes": sizes, "refresh_rates": REFRESH_RATES,
            "boot_modes": {k: f"{w}x{h}@{hz:g}" for k, (w, h, hz) in boot_video_modes().items()},
            "helper": helper_available()}


def set_boot_mode(width: int, height: int, refresh: float, port: Optional[str]) -> Dict[str, Any]:
    """Write video=<port>:WxH@Hz to the kernel command line (persists across reboots)."""
    mode = f"{int(width)}x{int(height)}@{int(round(refresh))}" if refresh and width and height else "auto"
    args = ["hdmi-mode", mode]
    if port and re.fullmatch(r"HDMI-A-\d", port):
        args.append(port)
    return run_helper(*args, timeout=120)


# ---- updates ---------------------------------------------------------------------
def update_state() -> Dict[str, Any]:
    st: Dict[str, Any] = {"available": helper_available() is None, "why": helper_available(),
                          "build": build_info()}
    try:
        st["source"] = (PREFIX / "source").read_text().strip()
    except OSError:
        st["source"] = None
        if st["available"]:
            st.update(available=False, why="run sudo ./deploy/update.sh once from the clone to enable updates")
    try:
        st["last"] = json.loads((PREFIX / "update.json").read_text())
    except (OSError, ValueError):
        st["last"] = None
    try:
        st["log"] = (PREFIX / "update.log").read_text()[-6000:]
    except OSError:
        st["log"] = ""
    return st


def check_update() -> Dict[str, Any]:
    return run_helper("update-check", timeout=120)


def start_update() -> Dict[str, Any]:
    return run_helper("update", timeout=30)


def reboot() -> Dict[str, Any]:
    return run_helper("reboot", timeout=15)


def add_routes(app, dependencies, publish=None) -> None:
    """Software update / reboot endpoints, shared by the player and master apps."""
    import asyncio
    from fastapi import HTTPException

    async def job(fn, *a):
        try:
            return await asyncio.get_running_loop().run_in_executor(None, fn, *a)
        except SystemJobError as e:
            raise HTTPException(409, str(e))

    @app.get("/api/system/update", dependencies=dependencies)
    def update_get():
        return update_state()

    @app.post("/api/system/update/check", dependencies=dependencies)
    async def update_check():
        return await job(check_update)

    @app.post("/api/system/update", dependencies=dependencies)
    async def update_start():
        r = await job(start_update)
        log.info("software update started from the web GUI")
        if publish:
            publish({"event": "notice", "message": "Updating - the unit restarts when it's installed"})
        return r

    @app.post("/api/system/reboot", dependencies=dependencies)
    async def reboot_now():
        log.info("reboot requested from the web GUI")
        return await job(reboot)
