"""Media library and cue list, persisted as JSON alongside the media files."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

log = logging.getLogger("cueplayer.library")

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".ts", ".mxf"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
CATEGORIES = ("library", "event")
AFTER_OPTIONS = ("default", "hold", "black")  # or "cue:<n>"


def kind_for(filename: str) -> Optional[str]:
    ext = Path(filename).suffix.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    return None


def _atomic_write(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe(path: Path) -> Dict[str, Any]:
    """Width/height/duration via ffprobe (best effort)."""
    if not shutil.which("ffprobe"):
        return {}
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
             "-show_streams", "-select_streams", "v:0", str(path)],
            capture_output=True, timeout=30, check=True).stdout
        info = json.loads(out)
        st = (info.get("streams") or [{}])[0]
        dur = float(info.get("format", {}).get("duration") or st.get("duration") or 0)
        return {"width": int(st.get("width") or 0), "height": int(st.get("height") or 0),
                "duration": round(dur, 3), "codec": st.get("codec_name")}
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        log.warning("ffprobe failed for %s: %s", path, e)
        return {}


def make_thumbnail(src: Path, dest: Path, kind: str, duration: float) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    seek = ["-ss", str(min(1.0, duration / 2))] if kind == "video" and duration else []
    try:
        subprocess.run(["ffmpeg", "-y", "-v", "error", *seek, "-i", str(src), "-frames:v", "1",
                        "-vf", "scale=320:-2", str(dest)],
                       capture_output=True, timeout=60, check=True)
        return dest.exists()
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("thumbnail failed for %s: %s", src, e)
        return False


class Library:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir)
        self.media_dir = self.dir / "media"
        self.thumb_dir = self.dir / "thumbs"
        self.incoming_dir = self.dir / "incoming"
        for d in (self.media_dir, self.thumb_dir, self.incoming_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.index_path = self.dir / "media.json"
        self.cues_path = self.dir / "cues.json"
        self._lock = threading.RLock()
        self.items: Dict[str, Dict[str, Any]] = self._load(self.index_path, {})
        self.cues: Dict[str, Dict[str, Any]] = self._load(self.cues_path, {})
        self._scan_orphans()

    # ---- persistence ----------------------------------------------------------
    @staticmethod
    def _load(path: Path, default):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return default

    def _save(self) -> None:
        _atomic_write(self.index_path, self.items)

    def _save_cues(self) -> None:
        _atomic_write(self.cues_path, self.cues)

    def _scan_orphans(self) -> None:
        """Drop index entries whose file is gone (e.g. manual deletion)."""
        with self._lock:
            gone = [k for k, v in self.items.items() if not (self.media_dir / v["filename"]).exists()]
            for k in gone:
                del self.items[k]
            if gone:
                self._save()

    # ---- media ----------------------------------------------------------------
    def path_of(self, item: Dict[str, Any]) -> Path:
        return self.media_dir / item["filename"]

    def thumb_of(self, item_id: str) -> Optional[Path]:
        p = self.thumb_dir / f"{item_id}.jpg"
        return p if p.exists() else None

    def list(self, category: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = [dict(v, has_thumb=self.thumb_of(v["id"]) is not None) for v in self.items.values()]
        if category:
            items = [i for i in items if i["category"] == category]
        return sorted(items, key=lambda i: (i["category"], i["name"].lower()))

    def get(self, item_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            v = self.items.get(item_id)
            return dict(v) if v else None

    def resolve(self, ref: Union[str, int]) -> Optional[Dict[str, Any]]:
        """Find media by id, name, or original filename (case-insensitive)."""
        ref = str(ref).strip()
        if not ref:
            return None
        with self._lock:
            if ref in self.items:
                return dict(self.items[ref])
            low = ref.lower()
            for v in self.items.values():
                if v["name"].lower() == low or v.get("original_filename", "").lower() == low \
                        or Path(v.get("original_filename", "")).stem.lower() == low:
                    return dict(v)
        return None

    def add_file(self, tmp_path: Path, original_filename: str, category: str = "event",
                 name: Optional[str] = None, item_id: Optional[str] = None,
                 expires: Optional[str] = None, tags: Optional[List[str]] = None,
                 scale_mode: Optional[str] = None) -> Dict[str, Any]:
        """Move an uploaded temp file into the library. Returns the item."""
        kind = kind_for(original_filename)
        if kind is None:
            tmp_path.unlink(missing_ok=True)
            raise ValueError(f"unsupported file type: {original_filename}")
        if category not in CATEGORIES:
            category = "event"
        checksum = sha256_file(tmp_path)
        with self._lock:
            # Same content already present under the same id (e.g. master re-sync)
            if item_id and item_id in self.items and self.items[item_id].get("checksum") == checksum:
                tmp_path.unlink(missing_ok=True)
                return self.update(item_id, {"name": name, "category": category,
                                             "expires": expires, "tags": tags,
                                             "scale_mode": scale_mode})
        item_id = item_id if item_id and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item_id) \
            else uuid.uuid4().hex[:12]
        ext = Path(original_filename).suffix.lower()
        filename = f"{item_id}{ext}"
        dest = self.media_dir / filename
        shutil.move(str(tmp_path), dest)
        meta = probe(dest)
        item = {
            "id": item_id,
            "name": (name or Path(original_filename).stem).strip(),
            "original_filename": Path(original_filename).name,
            "filename": filename,
            "kind": kind,
            "category": category,
            "size": dest.stat().st_size,
            "width": meta.get("width", 0),
            "height": meta.get("height", 0),
            "duration": meta.get("duration", 0) if kind == "video" else 0,
            "codec": meta.get("codec"),
            "checksum": checksum,
            "created": time.time(),
            "expires": expires or None,
            "tags": tags or [],
            "scale_mode": scale_mode or None,
        }
        make_thumbnail(dest, self.thumb_dir / f"{item_id}.jpg", kind, item["duration"])
        with self._lock:
            old = self.items.get(item_id)
            if old and old["filename"] != filename:
                (self.media_dir / old["filename"]).unlink(missing_ok=True)
            self.items[item_id] = item
            self._save()
        log.info("added %s (%s, %s)", item["name"], kind, category)
        return dict(item)

    EDITABLE = ("name", "category", "expires", "tags", "scale_mode")

    def update(self, item_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            item = self.items.get(item_id)
            if item is None:
                raise KeyError(item_id)
            for k in self.EDITABLE:
                if k in patch and patch[k] is not None:
                    v = patch[k]
                    if k == "category" and v not in CATEGORIES:
                        continue
                    if k == "scale_mode" and v not in ("", "fit", "fill", "stretch", "none"):
                        continue
                    item[k] = v or None if k in ("expires", "scale_mode") else v
            self._save()
            return dict(item)

    def delete(self, item_id: str) -> bool:
        with self._lock:
            item = self.items.pop(item_id, None)
            if item is None:
                return False
            self._save()
            # Remove from cues
            changed = False
            for c in self.cues.values():
                media = c.get("media")
                if isinstance(media, list) and item_id in media:
                    c["media"] = [m for m in media if m != item_id]
                    changed = True
                elif media == item_id:
                    c["media"] = None
                    changed = True
            if changed:
                self._save_cues()
        (self.media_dir / item["filename"]).unlink(missing_ok=True)
        (self.thumb_dir / f"{item_id}.jpg").unlink(missing_ok=True)
        log.info("deleted %s", item["name"])
        return True

    def expired(self, now: Optional[dt.date] = None) -> List[str]:
        now = now or dt.date.today()
        out = []
        with self._lock:
            for v in self.items.values():
                exp = v.get("expires")
                if not exp:
                    continue
                try:
                    if dt.date.fromisoformat(str(exp)[:10]) < now:
                        out.append(v["id"])
                except ValueError:
                    pass
        return out

    def disk_usage(self) -> Dict[str, int]:
        du = shutil.disk_usage(self.media_dir)
        used = sum(v.get("size", 0) for v in self.items.values())
        return {"library_bytes": used, "free_bytes": du.free, "total_bytes": du.total}

    # ---- cues -----------------------------------------------------------------
    @staticmethod
    def _clean_cue(data: Dict[str, Any]) -> Dict[str, Any]:
        media = data.get("media")
        if isinstance(media, list):
            media = [str(m) for m in media if m]
        elif media:
            media = str(media)
        after = str(data.get("after") or "default")
        if after not in AFTER_OPTIONS and not re.fullmatch(r"cue:\S+", after):
            after = "default"
        dur = data.get("duration")
        tr = data.get("transition_ms")
        return {
            "label": str(data.get("label") or "").strip(),
            "media": media or None,
            "loop": bool(data.get("loop", False)),
            "duration": float(dur) if dur not in (None, "", 0) else None,
            "after": after,
            "transition_ms": int(tr) if tr not in (None, "") else None,
            "color": str(data.get("color") or ""),
        }

    def list_cues(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in sorted(self.cues.items(), key=lambda kv: _cue_sort(kv[0]))}

    def get_cue(self, ref: Union[str, int]) -> Optional[Dict[str, Any]]:
        ref = str(ref).strip()
        with self._lock:
            if ref in self.cues:
                return dict(self.cues[ref], number=ref)
            for k, v in self.cues.items():
                if v.get("label", "").lower() == ref.lower():
                    return dict(v, number=k)
        return None

    def set_cue(self, number: str, data: Dict[str, Any]) -> Dict[str, Any]:
        number = str(number).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", number):
            raise ValueError("cue number must be 1-32 chars of A-Z, 0-9, _ or -")
        with self._lock:
            self.cues[number] = self._clean_cue(data)
            self._save_cues()
            return dict(self.cues[number], number=number)

    def delete_cue(self, number: str) -> bool:
        with self._lock:
            if self.cues.pop(str(number), None) is None:
                return False
            self._save_cues()
            return True

    def replace_cues(self, cues: Dict[str, Dict[str, Any]]) -> None:
        with self._lock:
            self.cues = {str(k): self._clean_cue(v) for k, v in cues.items()}
            self._save_cues()


def _cue_sort(k: str):
    return (0, int(k), "") if k.isdigit() else (1, 0, k)
