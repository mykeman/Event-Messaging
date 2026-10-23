"""Playlists and the schedule (calendar of playlists), persisted as JSON.

A schedule entry says "play this playlist from start to end", once or repeating daily or
on chosen weekdays. While an entry is on, its playlist replaces the Normal playlist as the
fallback content; cues and scenarios still interrupt it as usual. Times are wall-clock
times in the unit's configured timezone.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPEATS = ("none", "daily", "weekly")
SPECIAL_PLAYLISTS = ("normal", "black")   # an entry can also show the Normal playlist / black


def _now(tz: str = "") -> dt.datetime:
    """Current local wall-clock time (naive) in tz, or the system timezone."""
    if tz:
        try:
            from zoneinfo import ZoneInfo
            return dt.datetime.now(ZoneInfo(tz)).replace(tzinfo=None, microsecond=0)
        except Exception:
            pass
    return dt.datetime.now().replace(microsecond=0)


def valid_timezone(tz: str) -> bool:
    if not tz:
        return True
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
        return True
    except Exception:
        return False


def _parse_dt(v: Any) -> dt.datetime:
    s = str(v or "").strip().replace(" ", "T")
    return dt.datetime.fromisoformat(s[:16]) if len(s) >= 16 else dt.datetime.fromisoformat(s + "T00:00")


def occurrences(entry: Dict[str, Any], start: dt.datetime, end: dt.datetime) -> List[Tuple[dt.datetime, dt.datetime]]:
    """(start, end) of every occurrence of entry overlapping [start, end)."""
    if not entry.get("enabled", True):
        return []
    try:
        s0, e0 = _parse_dt(entry["start"]), _parse_dt(entry["end"])
    except (KeyError, ValueError):
        return []
    repeat = entry.get("repeat", "none")
    if repeat == "none":
        return [(s0, e0)] if s0 < end and e0 > start and e0 > s0 else []
    length = e0 - s0
    if length <= dt.timedelta(0):          # e.g. 22:00 - 02:00 on a repeating entry
        length += dt.timedelta(days=1)
    if length > dt.timedelta(days=1):
        length = dt.timedelta(days=1)
    until = None
    if entry.get("until"):
        try:
            until = dt.date.fromisoformat(str(entry["until"])[:10])
        except ValueError:
            pass
    days = entry.get("days") or [s0.weekday()]
    out = []
    d = max(s0.date(), (start - length).date())
    while d <= end.date():
        if until and d > until:
            break
        if repeat == "daily" or d.weekday() in days:
            s = dt.datetime.combine(d, s0.time())
            e = s + length
            if s < end and e > start and s >= s0:
                out.append((s, e))
        d += dt.timedelta(days=1)
    return out


class Schedule:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.pl_path = self.dir / "playlists.json"
        self.sc_path = self.dir / "schedule.json"
        self._lock = threading.RLock()
        self.playlists: Dict[str, Dict[str, Any]] = self._load(self.pl_path)
        self.entries: Dict[str, Dict[str, Any]] = self._load(self.sc_path)

    @staticmethod
    def _load(p: Path) -> Dict[str, Any]:
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, path: Path, data: Any) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, path)

    # ---- playlists ---------------------------------------------------------------
    @staticmethod
    def clean_playlist(d: Dict[str, Any]) -> Dict[str, Any]:
        items = []
        for it in d.get("items") or []:
            if isinstance(it, str):
                it = {"media": it}
            mid = str(it.get("media") or "").strip()
            if not mid:
                continue
            dur = it.get("duration")
            try:
                dur = float(dur) if dur not in (None, "", 0, "0") else None
            except (TypeError, ValueError):
                dur = None
            items.append({"media": mid, "duration": dur})
        name = str(d.get("name") or "").strip()[:80]
        if not name:
            raise ValueError("playlist name is required")
        return {"name": name, "items": items, "shuffle": bool(d.get("shuffle", False)),
                "color": str(d.get("color") or "")[:16]}

    def list_playlists(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v, id=k) for k, v in sorted(self.playlists.items(),
                                                        key=lambda kv: kv[1]["name"].lower())}

    def get_playlist(self, ref: str) -> Optional[Dict[str, Any]]:
        ref = str(ref or "").strip()
        with self._lock:
            if ref in self.playlists:
                return dict(self.playlists[ref], id=ref)
            for k, v in self.playlists.items():
                if v["name"].lower() == ref.lower():
                    return dict(v, id=k)
        return None

    def save_playlist(self, pid: Optional[str], d: Dict[str, Any]) -> Dict[str, Any]:
        p = self.clean_playlist(d)
        with self._lock:
            pid = pid if pid and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", pid) else uuid.uuid4().hex[:10]
            self.playlists[pid] = p
            self._save(self.pl_path, self.playlists)
            return dict(p, id=pid)

    def delete_playlist(self, pid: str) -> bool:
        with self._lock:
            if self.playlists.pop(pid, None) is None:
                return False
            self._save(self.pl_path, self.playlists)
            return True

    def drop_media(self, media_id: str) -> None:
        """Remove a deleted media item from every playlist."""
        with self._lock:
            changed = False
            for p in self.playlists.values():
                kept = [i for i in p["items"] if i["media"] != media_id]
                if len(kept) != len(p["items"]):
                    p["items"], changed = kept, True
            if changed:
                self._save(self.pl_path, self.playlists)

    # ---- schedule entries ----------------------------------------------------------
    def clean_entry(self, d: Dict[str, Any]) -> Dict[str, Any]:
        errors = []
        title = str(d.get("title") or "").strip()[:80]
        playlist = str(d.get("playlist") or "").strip()
        if playlist not in SPECIAL_PLAYLISTS and playlist not in self.playlists:
            errors.append("choose a playlist")
        try:
            start, end = _parse_dt(d.get("start")), _parse_dt(d.get("end"))
        except ValueError:
            start = end = None
            errors.append("start and end must be date-times like 2026-10-24T18:00")
        repeat = d.get("repeat") or "none"
        if repeat not in REPEATS:
            errors.append(f"repeat must be one of {', '.join(REPEATS)}")
        if start and end and repeat == "none" and end <= start:
            errors.append("end must be after start")
        days = sorted({int(x) for x in (d.get("days") or []) if str(x).isdigit() and 0 <= int(x) <= 6})
        if repeat == "weekly" and not days and start:
            days = [start.weekday()]
        until = str(d.get("until") or "")[:10] or None
        if until:
            try:
                dt.date.fromisoformat(until)
            except ValueError:
                errors.append("until must be a date like 2026-12-31")
        if errors:
            raise ValueError("; ".join(errors))
        return {"title": title or (self.playlists.get(playlist, {}).get("name") or playlist.title()),
                "playlist": playlist, "start": start.isoformat(timespec="minutes"),
                "end": end.isoformat(timespec="minutes"), "repeat": repeat, "days": days,
                "until": until, "color": str(d.get("color") or "")[:16],
                "takeover": bool(d.get("takeover", False)), "enabled": bool(d.get("enabled", True))}

    def list_entries(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v, id=k) for k, v in self.entries.items()}

    def save_entry(self, eid: Optional[str], d: Dict[str, Any]) -> Dict[str, Any]:
        e = self.clean_entry(d)
        with self._lock:
            eid = eid if eid and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", eid) else uuid.uuid4().hex[:10]
            self.entries[eid] = e
            self._save(self.sc_path, self.entries)
            return dict(e, id=eid)

    def delete_entry(self, eid: str) -> bool:
        with self._lock:
            if self.entries.pop(eid, None) is None:
                return False
            self._save(self.sc_path, self.entries)
            return True

    def occurrences(self, start: dt.datetime, end: dt.datetime) -> List[Dict[str, Any]]:
        out = []
        with self._lock:
            for eid, e in self.entries.items():
                for s, f in occurrences(e, start, end):
                    out.append(dict(e, id=eid, occ_start=s.isoformat(timespec="minutes"),
                                    occ_end=f.isoformat(timespec="minutes")))
        return sorted(out, key=lambda o: o["occ_start"])

    def active(self, now: dt.datetime) -> Optional[Dict[str, Any]]:
        """The entry on at `now`; where entries overlap, the one that started last wins."""
        on = [o for o in self.occurrences(now - dt.timedelta(days=2), now + dt.timedelta(seconds=1))
              if _parse_dt(o["occ_start"]) <= now < _parse_dt(o["occ_end"])]
        return max(on, key=lambda o: o["occ_start"]) if on else None

    def upcoming(self, now: dt.datetime, limit: int = 10) -> List[Dict[str, Any]]:
        return [o for o in self.occurrences(now, now + dt.timedelta(days=14))
                if _parse_dt(o["occ_start"]) > now][:limit]
