"""Playback logic: what plays now, what plays next.

Runs on the asyncio event loop. Engine events arrive from the GLib thread and are
marshalled here with call_soon_threadsafe.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional

from .config import PIPELINE_SECTIONS, Settings
from .engine import BaseEngine, create_engine
from .library import Library

log = logging.getLogger("cueplayer.controller")


class PlayError(Exception):
    pass


class Blocked(PlayError):
    """A locked scenario (e.g. fire alarm) is on screen and the trigger may not replace it."""


def parse_time(v: Any) -> float:
    """Seconds from 90, 90.5, "1:30" or "1:02:03"."""
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return max(0.0, float(v))
    s = str(v).strip()
    if re.fullmatch(r"\d+(:\d{1,2}){1,2}(\.\d+)?", s):
        total = 0.0
        for part in s.split(":"):
            total = total * 60 + float(part)
        return total
    try:
        return max(0.0, float(s))
    except ValueError:
        raise PlayError(f"bad time '{v}' (use seconds or h:mm:ss)")


FEATURE_ACTIVE = ("playing", "interrupted")


class Controller:
    def __init__(self, settings: Settings, library: Library, force_mock: bool = False):
        self.settings = settings
        self.library = library
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.engine: BaseEngine = create_engine(self._engine_event_threadsafe, force_mock)
        from .transcode import Transcoder
        self.transcoder = Transcoder(library, settings.get)
        self.listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._timer: Optional[asyncio.TimerHandle] = None
        self._default_idx = -1
        self.state: Dict[str, Any] = self._blank_state("starting")
        self.history: List[Dict[str, Any]] = []
        self._failures: Dict[str, int] = {}   # media id -> consecutive playback failures
        self._skip_once: Optional[str] = None
        from .schedule import Schedule
        self.schedule = Schedule(library.dir)
        self._sched_key: Optional[tuple] = None
        self._sched_active: Optional[Dict[str, Any]] = None
        # Feature screening: the film, its state and position (persisted, so a restart
        # carries on where it was)
        self.feature: Optional[Dict[str, Any]] = None
        self._feature_path = library.dir / "feature.json"
        self._feature_triggered: List[str] = []   # schedule occurrences already started
        self._feature_load()

    # ---- lifecycle --------------------------------------------------------------
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.engine.start(self.settings.get())
        # The engine emits engine_started; the default content starts then.
        asyncio.create_task(self._housekeeping())
        asyncio.create_task(self._schedule_loop())
        asyncio.create_task(self._feature_loop())
        # Check every file against this unit's hardware (also converts existing libraries)
        self.loop.run_in_executor(None, self.transcoder.evaluate_all)

    def shutdown(self) -> None:
        self.engine.stop()

    def apply_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        changed = self.settings.update(patch)
        if "schedule" in changed:  # timezone changed: re-evaluate what's on now
            self._sched_key = ("recheck",)
            self.check_schedule()
        if changed & {"output", "transcode"}:
            self.loop.run_in_executor(None, self.transcoder.evaluate_all)
        if changed & set(PIPELINE_SECTIONS):
            log.info("rebuilding pipeline (changed: %s)", ", ".join(sorted(changed)))
            self.engine.rebuild(self.settings.get())
        elif "playback" in changed and self.state["mode"] in ("default", "black", "starting"):
            self.play_default()
        self._notify()
        return self.settings.get()

    # ---- state ---------------------------------------------------------------------
    @staticmethod
    def _blank_state(mode: str) -> Dict[str, Any]:
        return {"mode": mode, "media": None, "cue": None, "clip": None, "started": None,
                "duration": None, "loop": False, "after": None, "transition_ms": None,
                "queue": [], "lock": False, "label": None}

    def status(self) -> Dict[str, Any]:
        s = dict(self.state)
        s.pop("queue", None)
        s["queue_length"] = len(self.state.get("queue") or [])
        if s["started"]:
            s["elapsed"] = round(time.time() - s["started"], 2)
        s["engine"] = self.engine.status
        s["feature"] = self.feature_status()
        try:
            s["schedule"] = self.schedule_status()
        except Exception:
            pass
        return s

    def subscribe(self, fn: Callable[[Dict[str, Any]], None]) -> Callable[[], None]:
        self.listeners.append(fn)
        return lambda: self.listeners.remove(fn) if fn in self.listeners else None

    def _notify(self, event: str = "status") -> None:
        msg = {"event": event, "status": self.status()}
        for fn in list(self.listeners):
            try:
                fn(msg)
            except Exception:
                log.exception("listener failed")

    # ---- commands --------------------------------------------------------------
    def _fade(self, override: Optional[int]) -> int:
        """Fade time for going to black (any non-cut transition fades)."""
        if override is not None:
            return max(0, int(override))
        tr = self.settings.section("transition")
        return int(tr.get("duration_ms", 0)) if tr.get("type") != "cut" else 0

    def _transition(self, kind: Optional[str], ms: Optional[int]):
        """(type, ms) for the next change: explicit values win over the Settings default."""
        from .engine import TRANSITIONS
        tr = self.settings.section("transition")
        kind = kind if kind in TRANSITIONS else (tr.get("type") if tr.get("type") in TRANSITIONS else "fade")
        ms = max(0, int(ms)) if ms is not None else int(tr.get("duration_ms", 0))
        return kind, (0 if kind == "cut" else ms)

    def _check_lock(self, lock: bool, force: bool) -> None:
        """Locked scenarios stay up until cleared with Normal/Black, or replaced by another
        locked scenario. force=True overrides (operators, internal sequencing)."""
        if self.state.get("lock") and not lock and not force:
            raise Blocked(f"blocked: '{self.state.get('label') or 'locked scenario'}' is active - "
                          "send NORMAL to clear it (or add force=1)")

    def play(self, ref: Any, loop: bool = False, duration: Optional[float] = None,
             after: str = "default", transition_ms: Optional[int] = None,
             mode: str = "adhoc", cue: Optional[str] = None,
             queue: Optional[List[str]] = None, lock: bool = False, force: bool = False,
             label: Optional[str] = None, transition_type: Optional[str] = None,
             item_duration: Optional[float] = None, source: Optional[str] = None,
             preroll_ms: Optional[int] = None, start: float = 0) -> Dict[str, Any]:
        item = self.library.resolve(ref)
        if item is None:
            raise PlayError(f"media not found: {ref}")
        self._check_lock(lock, force)
        self._cancel_timer()
        if mode != "feature":
            self._feature_interrupt(label or (f"cue {cue}" if cue else None) or source or item["name"])
        start = float(start or 0) if item["kind"] == "video" else 0.0
        tkind, fade = self._transition(transition_type, transition_ms)
        scale = item.get("scale_mode") or self.settings.section("output")["scale_mode"]
        clip = self.engine.play(str(self.library.playable_path(item)), item["kind"], scale, fade,
                                duration_hint=item.get("duration") or 0,
                                sw_decode=item.get("decode") == "software", transition=tkind,
                                preroll_ms=preroll_ms, start_s=start)
        if item["kind"] == "image" and not duration:
            # A lone looping/held image stays up until the next command;
            # otherwise images advance after the default image duration.
            if queue or not (loop or after == "hold"):
                duration = float(self.settings.section("playback")["image_duration"])
        self.state = {
            # started is backdated by the start offset, so elapsed = position in the file
            "mode": mode, "cue": cue, "clip": clip, "started": time.time() - start,
            "media": {k: item.get(k) for k in ("id", "name", "kind", "category", "duration")},
            "duration": duration or (item.get("duration") if item["kind"] == "video" else None),
            "loop": loop, "after": after, "transition_ms": transition_ms,
            "transition_type": transition_type,
            "queue": list(queue or []), "lock": bool(lock), "label": label,
            "item_duration": item_duration, "source": source,
        }
        if duration:
            self._timer = self.loop.call_later(duration, self._on_finished, clip)
        self._log_history()
        self._notify("play")
        return self.status()

    def play_cue(self, ref: Any, **overrides: Any) -> Dict[str, Any]:
        cue = self.library.get_cue(ref)
        if cue is None:
            raise PlayError(f"cue not found: {ref}")
        media = cue.get("media")
        items = media if isinstance(media, list) else ([media] if media else [])
        items = [m for m in items if self.library.get(m)]
        if not items:
            raise PlayError(f"cue {cue['number']} has no media")
        opts = {"loop": cue["loop"], "duration": cue["duration"], "after": cue["after"],
                "transition_ms": cue["transition_ms"], "lock": cue.get("lock", False),
                "transition_type": cue.get("transition"),
                "label": cue.get("label") or None}
        opts.update({k: v for k, v in overrides.items() if v is not None})
        return self.play(items[0], mode="cue", cue=cue["number"], queue=items[1:], **opts)

    def test_pattern(self, pattern: str = "smpte", tone: bool = False, label: bool = True,
                     transition_ms: Optional[int] = None, force: bool = False) -> Dict[str, Any]:
        """Show a test pattern on every output until the next command (e.g. NORMAL)."""
        from .common import hostname
        from .engine import TEST_PATTERNS
        pattern = (pattern or "smpte").lower()
        if pattern not in TEST_PATTERNS:
            raise PlayError(f"unknown test pattern '{pattern}' - one of: {', '.join(TEST_PATTERNS)}")
        self._check_lock(False, force)
        self._cancel_timer()
        self._feature_interrupt(f"test pattern {pattern}")
        out = self.settings.section("output")
        name = self.settings.section("fleet").get("screen_name") or hostname()
        title = TEST_PATTERNS[pattern][1]
        text = (f"{name}  ·  {out['canvas_width']}×{out['canvas_height']} @ {out['framerate']} fps"
                f"  ·  {title}") if label else None
        tkind, fade = self._transition(None, transition_ms)
        clip = self.engine.play_pattern(pattern, fade, text, tone, transition=tkind)
        self.state = dict(self._blank_state("test"), clip=clip, started=time.time(), after="hold",
                          media={"id": f"pattern:{pattern}", "name": f"Test pattern: {title}",
                                 "kind": "pattern", "category": None, "duration": None},
                          pattern={"name": pattern, "tone": tone, "label": label})
        self._log_history()
        self._notify("play")
        return self.status()

    def black(self, transition_ms: Optional[int] = None) -> Dict[str, Any]:
        self._cancel_timer()
        self._feature_interrupt("black")
        self.engine.black(self._fade(transition_ms))
        self.state = self._blank_state("black")
        self._notify("black")
        return self.status()

    def stop(self, transition_ms: Optional[int] = None) -> Dict[str, Any]:
        """Stop the triggered content and return to the default content."""
        return self.play_default(transition_ms)

    def _normal_items(self):
        """What the normal (fallback) layer plays now: the scheduled playlist if an entry is
        on, else the Normal playlist. -> (ids, durations, shuffle, source label, black?)"""
        pb = self.settings.section("playback")
        act = self._sched_active
        if act and act.get("playlist") == "black":
            return [], {}, False, f"schedule: {act['title']}", True
        if act and act.get("playlist") not in (None, "", "normal", "feature"):
            pl = self.schedule.get_playlist(act["playlist"])
            items = [i for i in (pl or {}).get("items", []) if self.library.get(i["media"])]
            if items:
                return ([i["media"] for i in items], {i["media"]: i.get("duration") for i in items},
                        bool(pl.get("shuffle")), f"schedule: {act['title']}", False)
        ids = [i for i in pb.get("default_playlist") or [] if self.library.get(i)]
        label = f"schedule: {act['title']}" if act else "normal playlist"
        return ids, {}, bool(pb.get("default_shuffle")), label, pb.get("default_mode") != "playlist"

    def play_default(self, transition_ms: Optional[int] = None) -> Dict[str, Any]:
        # While a feature is on, it *is* the normal content: Normal leaves a playing feature
        # alone and carries on with an interrupted one (e.g. after an emergency message).
        f = self.feature
        if f and f["state"] == "playing" and self.state.get("mode") == "feature":
            return self.status()
        if f and f["state"] == "interrupted":
            return self._feature_resume(transition_ms)
        ids, durations, shuffle, source, black = self._normal_items()
        if black or not ids:
            st = self.black(transition_ms)
            self.state["mode"] = "default"
            self.state["source"] = source
            self._notify()
            return st
        if self._skip_once in ids and len(ids) > 1:
            ids = [i for i in ids if i != self._skip_once]
        self._skip_once = None
        if shuffle and len(ids) > 1:
            choices = [i for i in range(len(ids)) if i != self._default_idx]
            self._default_idx = random.choice(choices)
        else:
            self._default_idx = (self._default_idx + 1) % len(ids)
        # A single default item loops; a playlist advances
        single = len(ids) == 1
        # Returning to normal is how a locked scenario is cleared, so it always wins
        mid = ids[self._default_idx]
        return self.play(mid, mode="default", loop=single, after="default",
                         transition_ms=transition_ms, force=True,
                         duration=None if single else durations.get(mid), source=source)

    def play_playlist(self, ref: Any, loop: bool = True, transition_ms: Optional[int] = None,
                      transition_type: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
        """Play a playlist now (ad hoc), looping by default, until the next command."""
        pl = self.schedule.get_playlist(ref)
        if pl is None:
            raise PlayError(f"playlist not found: {ref}")
        items = [i for i in pl["items"] if self.library.get(i["media"])]
        if pl.get("shuffle"):
            random.shuffle(items)
        if not items:
            raise PlayError(f"playlist '{pl['name']}' has no media")
        first, rest = items[0], [dict(i) for i in items[1:]]
        return self.play(first["media"], loop=loop, mode="adhoc", queue=rest,
                         duration=first.get("duration"), item_duration=first.get("duration"),
                         transition_ms=transition_ms, transition_type=transition_type,
                         force=force, source=f"playlist: {pl['name']}")

    # ---- schedule -------------------------------------------------------------------
    def now(self):
        from .schedule import _now
        return _now(self.settings.section("schedule").get("timezone", ""))

    def schedule_status(self) -> Dict[str, Any]:
        now = self.now()
        act = self._sched_active
        nxt = self.schedule.upcoming(now, 1)
        return {"now": now.isoformat(timespec="seconds"),
                "timezone": self.settings.section("schedule").get("timezone") or "system",
                "active": {k: act.get(k) for k in ("id", "title", "playlist", "occ_start", "occ_end")} if act else None,
                "next": {k: nxt[0].get(k) for k in ("id", "title", "occ_start", "occ_end")} if nxt else None}

    def check_schedule(self) -> None:
        """Apply the schedule entry that's on now, if it changed."""
        act = self.schedule.active(self.now())
        key = (act["id"], act["occ_start"]) if act else None
        if key == self._sched_key:
            return
        prev = self._sched_active
        self._sched_key, self._sched_active = key, act
        self._default_idx = -1
        if act and act.get("playlist") == "feature":
            self._schedule_feature(act)
            return
        log.info("schedule: %s", f"'{act['title']}' until {act['occ_end']}" if act else
                 f"'{prev['title']}' ended" if prev else "nothing scheduled")
        mode = self.state.get("mode")
        if mode in ("default", "starting"):
            self.play_default()
        elif act and act.get("takeover") and not self.state.get("lock") and \
                mode in ("cue", "adhoc", "hold", "test"):
            self.play_default()  # the entry interrupts event content (never a locked scenario)
        else:
            self._notify("schedule")

    async def _schedule_loop(self) -> None:
        while True:
            try:
                self.check_schedule()
            except Exception:
                log.exception("schedule check failed")
            await asyncio.sleep(5)

    def next(self, force: bool = False) -> Dict[str, Any]:
        self._check_lock(False, force)
        self._on_finished(self.state.get("clip"), forced=True)
        return self.status()

    # ---- engine events --------------------------------------------------------------
    def _engine_event_threadsafe(self, ev: Dict[str, Any]) -> None:
        if self.loop is None:
            return
        self.loop.call_soon_threadsafe(self._engine_event, ev)

    def _engine_event(self, ev: Dict[str, Any]) -> None:
        t = ev.get("type")
        if t == "engine_started":
            # Pipeline (re)built: resume what should be on screen
            if self.state["mode"] == "black":
                return
            if self.state["mode"] in ("starting", "default") or not self.state.get("media"):
                self._default_idx = max(-1, self._default_idx - 1)  # replay same default item
                self.play_default(0)
            else:
                m = self.state["media"]
                remaining = None
                if self.state.get("duration") and self.state.get("started"):
                    remaining = max(1.0, self.state["duration"] - (time.time() - self.state["started"]))
                self._replay_current(transition_ms=0,
                                     duration=remaining if m["kind"] == "image" else None)
        elif t == "clip_ended":
            if ev.get("clip") == self.state.get("clip"):
                self._on_finished(ev["clip"])
        elif t == "clip_error":
            log.warning("clip %s failed: %s", ev.get("clip"), ev.get("error"))
            if ev.get("clip") == self.state.get("clip"):
                self._clip_failed(ev)
        elif t == "notice":
            for fn in list(self.listeners):
                try:
                    fn({"event": "notice", "message": ev.get("message", ""), "status": self.status()})
                except Exception:
                    pass
        elif t == "engine_error":
            self._notify_error(ev.get("error", "engine error"))
        elif t == "clip_started":
            if ev.get("clip") == self.state.get("clip") and self.state.get("media"):
                self._failures.pop(self.state["media"]["id"], None)
            self._notify("started")

    def _replay_current(self, transition_ms: int = 0, duration: Optional[float] = None) -> None:
        st = self.state
        if not st.get("media"):
            return
        if st.get("mode") == "feature" and self.feature and self.feature["state"] == "playing":
            self._feature_start_clip(self._feature_pos(), transition_ms, rewind=False)
            return
        if st.get("pattern"):
            p = st["pattern"]
            self.test_pattern(p["name"], p.get("tone", False), p.get("label", True), transition_ms, True)
            return
        try:
            self.play(st["media"]["id"], loop=st["loop"], after=st["after"], duration=duration,
                      mode=st["mode"], cue=st["cue"], queue=st.get("queue"),
                      transition_ms=transition_ms, lock=st.get("lock", False), force=True,
                      label=st.get("label"))
        except PlayError as e:
            log.warning("could not resume %s: %s", st["media"].get("name"), e)
            self.play_default()

    def _clip_failed(self, ev: Dict[str, Any]) -> None:
        m = self.state.get("media") or {}
        item = self.library.get(m.get("id", "")) or {}
        name = m.get("name") or "file"
        # Hardware decoders reject some streams: fall back to software for this file
        if ev.get("hw_decoder") and item and item.get("decode") != "software":
            self.library.set_decode(item["id"], "software")
            self._notify_error(f"The hardware decoder couldn't play {name} - "
                               f"retrying with software decoding")
            self._replay_current()
            return
        fails = self._failures.get(m.get("id"), 0) + 1
        self._failures[m.get("id")] = fails
        if self.state["mode"] == "feature" and self.feature and fails <= 5:
            # A film is not skipped for a hiccup: pick it up again where it was
            self._notify_error(f"{name} failed to play ({ev.get('error')}) - retrying")
            self.loop.call_later(min(30, 2 * fails), self._replay_current)
            return
        if self.state.get("lock"):
            # Never drop an emergency message because a file hiccuped: keep retrying
            self._notify_error(f"{name} failed to play ({ev.get('error')}) - retrying")
            self.loop.call_later(min(30, 2 * fails), self._replay_current)
            return
        if self.state["mode"] == "default" and fails < 2:
            self._notify_error(f"{name} failed to play ({ev.get('error')}) - retrying")
            self.loop.call_later(2, self._retry_default)
            return
        self._notify_error(f"{name} can't be played ({ev.get('error')}) - skipped. "
                           f"Try Convert now or re-export the file.")
        pb = self.settings.section("playback")
        others = [i for i in pb.get("default_playlist") or [] if i != m.get("id") and self.library.get(i)]
        if self.state["mode"] == "default" and not others:
            self.black(0)  # nothing else to fall back to: don't loop on a broken file
            self.state["mode"] = "default"
            self._notify()
        else:
            self._skip_once = m.get("id")
            self.play_default()

    def _retry_default(self) -> None:
        if self.state["mode"] == "default":
            self.play_default()

    def _notify_error(self, msg: str) -> None:
        for fn in list(self.listeners):
            try:
                fn({"event": "error", "error": msg, "status": self.status()})
            except Exception:
                pass

    def _cancel_timer(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def _on_finished(self, clip: Any, forced: bool = False) -> None:
        """Current item finished (video EOS, image/duration timer, or 'next')."""
        if clip is None or clip != self.state.get("clip"):
            if not forced:
                return
        self._cancel_timer()
        st = self.state
        m = st.get("media")
        if m is None:
            if forced:
                self.play_default()
            return
        if st["mode"] == "feature":
            self._feature_finished(forced)
            after = st.get("after") or "default"
            if after == "hold":
                self.state["mode"] = "hold"
                self._notify("hold")
            elif after == "black":
                self.black()
            else:
                self.play_default()
            return
        queue = list(st.get("queue") or [])
        if queue:
            nxt = queue.pop(0)
            if st["loop"]:
                queue.append({"media": m["id"], "duration": st.get("item_duration")}
                             if st.get("item_duration") else m["id"])
            nid, ndur = (nxt.get("media"), nxt.get("duration")) if isinstance(nxt, dict) else (nxt, None)
            self.play(nid, loop=st["loop"], after=st["after"], mode=st["mode"], cue=st["cue"],
                      queue=queue, transition_ms=st["transition_ms"],
                      transition_type=st.get("transition_type"),
                      duration=ndur or self._cue_duration(st), lock=st.get("lock", False), force=True,
                      label=st.get("label"), item_duration=ndur, source=st.get("source"))
            return
        if st["loop"] and not forced:
            # Restart of the same item: short pre-roll, so the loop point doesn't hold the
            # last frame for the full pre-roll time
            self.play(m["id"], loop=True, after=st["after"], mode=st["mode"], cue=st["cue"],
                      transition_ms=0, duration=self._cue_duration(st),
                      lock=st.get("lock", False), force=True, label=st.get("label"),
                      source=st.get("source"), preroll_ms=80)
            return
        if st["mode"] == "default":
            self.play_default()
            return
        after = st.get("after") or "default"
        if after == "hold":
            self.state["mode"] = "hold"
            self._notify("hold")
        elif after == "black":
            self.black()
        elif after.startswith("cue:"):
            try:
                self.play_cue(after[4:], force=True)
            except PlayError as e:
                log.warning("after-cue failed: %s", e)
                self.play_default()
        else:
            self.play_default()

    def _cue_duration(self, st: Dict[str, Any]) -> Optional[float]:
        if st.get("cue"):
            cue = self.library.get_cue(st["cue"])
            if cue:
                return cue.get("duration")
        return None

    def _log_history(self) -> None:
        m = self.state.get("media") or {}
        self.history.append({"t": time.time(), "mode": self.state["mode"], "cue": self.state["cue"],
                             "media": m.get("name")})
        del self.history[:-100]

    # ---- housekeeping ---------------------------------------------------------------
    async def _housekeeping(self) -> None:
        while True:
            await asyncio.sleep(300)
            try:
                for item_id in self.library.expired():
                    playing = (self.state.get("media") or {}).get("id") == item_id
                    if playing or (self.feature and self.feature["state"] in FEATURE_ACTIVE
                                   and self.feature["media"]["id"] == item_id):
                        continue
                    log.info("removing expired media %s", item_id)
                    self.library.delete(item_id)
                    self._prune_default(item_id)
            except Exception:
                log.exception("housekeeping failed")

    def _prune_default(self, item_id: str) -> None:
        pb = self.settings.section("playback")
        if item_id in (pb.get("default_playlist") or []):
            self.settings.update({"playback": {
                "default_playlist": [i for i in pb["default_playlist"] if i != item_id]}})

    # ---- feature screening --------------------------------------------------------------
    def feature_status(self) -> Optional[Dict[str, Any]]:
        f = self.feature
        if not f:
            return None
        pos = self._feature_pos()
        dur = float(f.get("duration") or 0)
        out = {k: f.get(k) for k in ("media", "state", "source", "interrupted_by", "started_at",
                                    "finished_at", "after", "event")}
        out.update(position=round(pos, 1), duration=round(dur, 1) if dur else None,
                   remaining=round(max(0.0, dur - pos), 1) if dur else None,
                   progress=round(min(1.0, pos / dur), 4) if dur else None)
        return out

    def _feature_pos(self) -> float:
        f = self.feature
        if not f:
            return 0.0
        pos = float(f.get("position") or 0)
        if f["state"] == "playing" and self.state.get("mode") == "feature":
            p = self.engine.clip_position(self.state.get("clip"))
            if p is None and self.state.get("started"):
                p = time.time() - self.state["started"]
            if p is not None:
                pos = p
        dur = float(f.get("duration") or 0)
        return max(0.0, min(pos, dur) if dur else pos)

    def play_feature(self, ref: Any, start: Any = 0, source: str = "api", force: bool = False,
                     transition_ms: Optional[int] = None) -> Dict[str, Any]:
        """Start a feature (film). A locked scenario on screen isn't replaced: the feature
        then waits (interrupted at its start) and begins when the scenario is cleared."""
        item = self.library.resolve(ref)
        if item is None:
            raise PlayError(f"media not found: {ref}")
        if item["kind"] != "video":
            raise PlayError(f"{item['name']} is not a video - a feature must be a video file")
        start = parse_time(start)
        dur = float(item.get("duration") or 0)
        if dur and start >= dur:
            raise PlayError(f"start {start:g}s is past the end of {item['name']} ({dur:g}s)")
        old = self.feature
        if old and old["state"] in FEATURE_ACTIVE and old["media"]["id"] != item["id"]:
            self._feature_event("stopped")
        self.feature = {"media": {k: item.get(k) for k in ("id", "name", "duration")},
                        "state": "interrupted", "position": start, "duration": dur or None,
                        "source": source, "started_at": time.time(), "finished_at": None,
                        "after": self.settings.section("feature").get("after", "default"),
                        "interrupted_by": None}
        if self.state.get("lock") and not force:
            self.feature["interrupted_by"] = self.state.get("label") or "locked scenario"
            self._feature_event("waiting")
            self._notify()
            return self.status()
        self._feature_start_clip(start, transition_ms, rewind=False)
        self._feature_event("started")
        return self.status()

    def _feature_start_clip(self, pos: float, transition_ms: Optional[int] = None,
                            rewind: bool = True) -> None:
        f = self.feature
        if rewind:
            pos = max(0.0, pos - float(self.settings.section("feature").get("rewind_s") or 0))
        f.update(state="playing", position=pos, interrupted_by=None)
        try:
            self.play(f["media"]["id"], mode="feature", after=f.get("after") or "default",
                      transition_ms=transition_ms, force=True, label=f["media"]["name"],
                      source="feature", start=pos)
        except PlayError as e:
            log.warning("feature %s can't be played: %s", f["media"].get("name"), e)
            f["state"] = "stopped"
            self._feature_event("stopped")
            self.play_default()

    def _feature_resume(self, transition_ms: Optional[int] = None) -> Dict[str, Any]:
        f = self.feature
        log.info("feature: resuming %s at %.1fs", f["media"]["name"], f.get("position") or 0)
        self._feature_start_clip(float(f.get("position") or 0), transition_ms)
        self._feature_event("resumed")
        return self.status()

    def feature_resume(self, force: bool = False) -> Dict[str, Any]:
        f = self.feature
        if not f or f["state"] != "interrupted":
            raise PlayError("no interrupted feature to resume")
        self._check_lock(False, force)
        return self._feature_resume()

    def _feature_interrupt(self, by: Optional[str]) -> None:
        """Something else is taking the screen: remember where the feature was."""
        f = self.feature
        if not f or f["state"] != "playing" or self.state.get("mode") != "feature":
            return
        f["position"] = self._feature_pos()
        f.update(state="interrupted", interrupted_by=by)
        log.info("feature: %s interrupted at %.1fs by %s", f["media"]["name"], f["position"], by)
        self._feature_event("interrupted")

    def _feature_finished(self, forced: bool = False) -> None:
        f = self.feature
        if not f:
            return
        f.update(state="finished", finished_at=time.time(),
                 position=f.get("duration") or self._feature_pos())
        log.info("feature: %s %s", f["media"]["name"], "skipped to the end" if forced else "finished")
        self._feature_event("finished")

    def feature_stop(self, then: Optional[str] = None,
                     transition_ms: Optional[int] = None) -> Dict[str, Any]:
        """End the feature. If it's on screen, go to `then` (default | black | hold;
        default: Normal playlist)."""
        f = self.feature
        if not f or f["state"] not in FEATURE_ACTIVE:
            raise PlayError("no feature is playing")
        on_screen = f["state"] == "playing" and self.state.get("mode") == "feature"
        f["position"] = self._feature_pos()
        f["state"] = "stopped"
        self._feature_event("stopped")
        if on_screen:
            if then == "black":
                return self.black(transition_ms)
            if then == "hold":
                self._cancel_timer()
                self.state["mode"] = "hold"
                self._notify("hold")
                return self.status()
            return self.play_default(transition_ms)
        self._notify()
        return self.status()

    def feature_seek(self, pos: Any) -> Dict[str, Any]:
        f = self.feature
        if not f or f["state"] not in FEATURE_ACTIVE:
            raise PlayError("no feature is playing")
        pos = parse_time(pos)
        dur = float(f.get("duration") or 0)
        if dur and pos >= dur:
            raise PlayError(f"{pos:g}s is past the end ({dur:g}s)")
        if f["state"] == "playing" and self.state.get("mode") == "feature":
            self._feature_start_clip(pos, 0, rewind=False)
        else:
            f["position"] = pos
        self._feature_event("seek")
        return self.status()

    def media_removed(self, item_id: str) -> None:
        f = self.feature
        if f and f["state"] in FEATURE_ACTIVE and f["media"]["id"] == item_id:
            f["state"] = "stopped"
            self._feature_event("stopped")

    def _schedule_feature(self, act: Dict[str, Any]) -> None:
        """A schedule entry that starts a feature: once per occurrence (also across restarts)."""
        key = f"{act['id']}@{act['occ_start']}"
        if key in self._feature_triggered:
            self._notify("schedule")
            return
        self._feature_triggered = (self._feature_triggered + [key])[-50:]
        log.info("schedule: starting feature '%s' (%s)", act.get("title"), act.get("media"))
        try:
            self.play_feature(act.get("media"), source=f"schedule: {act.get('title')}")
        except PlayError as e:
            log.warning("scheduled feature failed: %s", e)
            self._notify_error(f"Scheduled feature '{act.get('title')}' couldn't start: {e}")
        self._feature_save()

    def _feature_event(self, name: str) -> None:
        if self.feature:
            self.feature["event"] = name
        self._feature_save()
        fs = self.feature_status()
        msg = {"event": "feature", "feature_event": name, "feature": fs, "status": self.status()}
        for fn in list(self.listeners):
            try:
                fn(msg)
            except Exception:
                log.exception("listener failed")
        cfg = self.settings.section("feature")
        if cfg.get("notify_url") and self.loop is not None:
            self.loop.create_task(self._webhook(cfg, name, fs))

    async def _webhook(self, cfg: Dict[str, Any], name: str, fs: Optional[Dict[str, Any]]) -> None:
        import httpx
        from .common import hostname
        url = cfg["notify_url"].replace("{event}", name)
        body = {"event": name, "feature": fs,
                "screen": self.settings.section("fleet").get("screen_name") or hostname()}
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                if cfg.get("notify_method") == "GET":
                    r = await c.get(url)
                else:
                    r = await c.post(url, json=body)
                r.raise_for_status()
        except Exception as e:
            log.warning("feature webhook %s failed: %s", url, e)

    async def _feature_loop(self) -> None:
        """Position updates for TCP clients / the GUI, and a saved resume point."""
        last_save = 0.0
        while True:
            interval = float(self.settings.section("feature").get("position_interval_s") or 1)
            await asyncio.sleep(max(0.2, interval))
            f = self.feature
            if not f or f["state"] != "playing" or self.state.get("mode") != "feature":
                continue
            try:
                f["position"] = self._feature_pos()
                msg = {"event": "feature_position", "feature": self.feature_status(),
                       "status": self.status()}
                for fn in list(self.listeners):
                    try:
                        fn(msg)
                    except Exception:
                        log.exception("listener failed")
                if time.time() - last_save > 5:
                    last_save = time.time()
                    self._feature_save()
            except Exception:
                log.exception("feature position update failed")

    def _feature_save(self) -> None:
        f = dict(self.feature) if self.feature else None
        if f and f["state"] == "playing":
            f["position"] = self._feature_pos()
        try:
            tmp = self._feature_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"feature": f, "triggered": self._feature_triggered}))
            os.replace(tmp, self._feature_path)
        except OSError as e:
            log.warning("could not save feature state: %s", e)

    def _feature_load(self) -> None:
        try:
            data = json.loads(self._feature_path.read_text())
        except (OSError, ValueError):
            return
        self._feature_triggered = list(data.get("triggered") or [])[-50:]
        f = data.get("feature")
        if not f or not self.library.get((f.get("media") or {}).get("id", "")):
            return
        if f.get("state") in FEATURE_ACTIVE:
            if self.settings.section("feature").get("resume_after_restart", True):
                # Picked up by play_default once the pipeline is running
                f.update(state="interrupted", interrupted_by="restart")
                log.info("feature: will resume %s at %.1fs", f["media"].get("name"), f.get("position") or 0)
            else:
                f["state"] = "stopped"
        self.feature = f
