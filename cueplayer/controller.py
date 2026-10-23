"""Playback logic: what plays now, what plays next.

Runs on the asyncio event loop. Engine events arrive from the GLib thread and are
marshalled here with call_soon_threadsafe.
"""
from __future__ import annotations

import asyncio
import logging
import random
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

    # ---- lifecycle --------------------------------------------------------------
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.engine.start(self.settings.get())
        # The engine emits engine_started; the default content starts then.
        asyncio.create_task(self._housekeeping())
        # Check every file against this unit's hardware (also converts existing libraries)
        self.loop.run_in_executor(None, self.transcoder.evaluate_all)

    def shutdown(self) -> None:
        self.engine.stop()

    def apply_settings(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        changed = self.settings.update(patch)
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
             label: Optional[str] = None, transition_type: Optional[str] = None) -> Dict[str, Any]:
        item = self.library.resolve(ref)
        if item is None:
            raise PlayError(f"media not found: {ref}")
        self._check_lock(lock, force)
        self._cancel_timer()
        tkind, fade = self._transition(transition_type, transition_ms)
        scale = item.get("scale_mode") or self.settings.section("output")["scale_mode"]
        clip = self.engine.play(str(self.library.playable_path(item)), item["kind"], scale, fade,
                                duration_hint=item.get("duration") or 0,
                                sw_decode=item.get("decode") == "software", transition=tkind)
        if item["kind"] == "image" and not duration:
            # A lone looping/held image stays up until the next command;
            # otherwise images advance after the default image duration.
            if queue or not (loop or after == "hold"):
                duration = float(self.settings.section("playback")["image_duration"])
        self.state = {
            "mode": mode, "cue": cue, "clip": clip, "started": time.time(),
            "media": {k: item.get(k) for k in ("id", "name", "kind", "category", "duration")},
            "duration": duration or (item.get("duration") if item["kind"] == "video" else None),
            "loop": loop, "after": after, "transition_ms": transition_ms,
            "transition_type": transition_type,
            "queue": list(queue or []), "lock": bool(lock), "label": label,
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
        self.engine.black(self._fade(transition_ms))
        self.state = self._blank_state("black")
        self._notify("black")
        return self.status()

    def stop(self, transition_ms: Optional[int] = None) -> Dict[str, Any]:
        """Stop the triggered content and return to the default content."""
        return self.play_default(transition_ms)

    def play_default(self, transition_ms: Optional[int] = None) -> Dict[str, Any]:
        pb = self.settings.section("playback")
        ids = [i for i in pb.get("default_playlist") or [] if self.library.get(i)]
        if pb.get("default_mode") != "playlist" or not ids:
            st = self.black(transition_ms)
            self.state["mode"] = "default"
            self._notify()
            return st
        if self._skip_once in ids and len(ids) > 1:
            ids = [i for i in ids if i != self._skip_once]
        self._skip_once = None
        if pb.get("default_shuffle") and len(ids) > 1:
            choices = [i for i in range(len(ids)) if i != self._default_idx]
            self._default_idx = random.choice(choices)
        else:
            self._default_idx = (self._default_idx + 1) % len(ids)
        # A single default item loops; a playlist advances
        single = len(ids) == 1
        # Returning to normal is how a locked scenario is cleared, so it always wins
        return self.play(ids[self._default_idx], mode="default", loop=single,
                         after="default", transition_ms=transition_ms, force=True)

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
        queue = list(st.get("queue") or [])
        if queue:
            nxt = queue.pop(0)
            if st["loop"]:
                queue.append(m["id"])
            self.play(nxt, loop=st["loop"], after=st["after"], mode=st["mode"], cue=st["cue"],
                      queue=queue, transition_ms=st["transition_ms"],
                      transition_type=st.get("transition_type"),
                      duration=self._cue_duration(st), lock=st.get("lock", False), force=True,
                      label=st.get("label"))
            return
        if st["loop"] and not forced:
            # Gapless-ish restart of the same item
            self.play(m["id"], loop=True, after=st["after"], mode=st["mode"], cue=st["cue"],
                      transition_ms=0, duration=self._cue_duration(st),
                      lock=st.get("lock", False), force=True, label=st.get("label"))
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
                    if playing:
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
