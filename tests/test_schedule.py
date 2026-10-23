"""Schedule occurrence maths and validation (no GStreamer needed).

    python tests/test_schedule.py
"""
import datetime as dt
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cueplayer.schedule import Schedule, occurrences, valid_timezone  # noqa: E402

failures = 0


def check(name, cond, detail=""):
    global failures
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    failures += 0 if cond else 1


D = dt.datetime
wk = (D(2026, 10, 19), D(2026, 10, 26))   # Mon 19 Oct - Mon 26 Oct 2026

once = {"start": "2026-10-21T18:00", "end": "2026-10-21T21:30", "repeat": "none"}
check("one-off inside the week", occurrences(once, *wk) == [(D(2026, 10, 21, 18), D(2026, 10, 21, 21, 30))])
check("one-off outside the week", occurrences(once, D(2026, 11, 1), D(2026, 11, 8)) == [])

daily = {"start": "2026-10-20T09:00", "end": "2026-10-20T17:00", "repeat": "daily"}
occ = occurrences(daily, *wk)
check("daily from its start date (6 days)", len(occ) == 6 and occ[0][0] == D(2026, 10, 20, 9), occ)

night = {"start": "2026-10-19T22:00", "end": "2026-10-19T02:00", "repeat": "daily"}
occ = occurrences(night, *wk)
check("daily across midnight lasts 4 h", occ[0] == (D(2026, 10, 19, 22), D(2026, 10, 20, 2)), occ[:1])

weekly = {"start": "2026-10-03T19:00", "end": "2026-10-03T23:00", "repeat": "weekly", "days": [4, 5]}
occ = occurrences(weekly, *wk)
check("weekly on Fri+Sat", [o[0].weekday() for o in occ] == [4, 5], occ)

until = dict(daily, until="2026-10-22")
check("until stops repeats", len(occurrences(until, *wk)) == 3)
check("disabled entry never occurs", occurrences(dict(daily, enabled=False), *wk) == [])

with tempfile.TemporaryDirectory() as d:
    s = Schedule(Path(d))
    pl = s.save_playlist(None, {"name": "Evening", "items": [{"media": "a"}, {"media": "b", "duration": 8}]})
    check("playlist saved with items", s.get_playlist("evening")["items"][1]["duration"] == 8.0)
    try:
        s.save_entry(None, {"playlist": "nope", "start": "2026-10-21T18:00", "end": "2026-10-21T19:00"})
        check("unknown playlist rejected", False)
    except ValueError as e:
        check("unknown playlist rejected", "playlist" in str(e), e)
    try:
        s.save_entry(None, {"playlist": pl["id"], "start": "2026-10-21T18:00", "end": "2026-10-21T17:00"})
        check("end before start rejected", False)
    except ValueError:
        check("end before start rejected", True)
    base = s.save_entry(None, {"title": "Day", "playlist": pl["id"], "start": "2026-10-19T08:00",
                               "end": "2026-10-19T20:00", "repeat": "daily"})
    gala = s.save_entry(None, {"title": "Gala", "playlist": "black", "start": "2026-10-21T18:00",
                               "end": "2026-10-21T19:00"})
    check("daily entry is on mid-morning", s.active(D(2026, 10, 21, 10))["id"] == base["id"])
    check("overlap: the later start wins", s.active(D(2026, 10, 21, 18, 30))["id"] == gala["id"])
    check("nothing on at night", s.active(D(2026, 10, 21, 22)) is None)
    check("upcoming lists the next start", s.upcoming(D(2026, 10, 21, 10), 2)[0]["id"] == gala["id"])
    check("entries survive reload", Schedule(Path(d)).active(D(2026, 10, 21, 18, 30))["title"] == "Gala")
    s.drop_media("a")
    check("deleted media leaves playlists", [i["media"] for i in s.get_playlist(pl["id"])["items"]] == ["b"])

check("timezone check", valid_timezone("Australia/Melbourne") and valid_timezone("") and not valid_timezone("Mars/Base"))

print(f"\n{failures} failure(s)")
sys.exit(1 if failures else 0)
