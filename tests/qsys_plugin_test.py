"""Runs qsys/CuePlayer.qplug in a minimal simulated Q-SYS runtime (needs `pip install lupa`).

    python tests/qsys_plugin_test.py [cues.json]

Checks the design-time functions (controls and layout for both pages) and the runtime:
button labels from the cue list, the commands buttons send, and feedback LEDs.
"""
import json
import sys
from pathlib import Path

from lupa import LuaRuntime

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = (ROOT / "qsys" / "CuePlayer.qplug").read_text()

SAMPLE_CUES = {
    "1": {"label": "Welcome", "group": "event", "color": ""},
    "2": {"label": "Sponsor loop", "group": "event", "color": "#3d8bfd"},
    "101": {"label": "Venue at capacity", "group": "scenario", "color": "#f59e0b"},
    "107": {"label": "Fire alarm", "group": "scenario", "color": "#ef4444", "lock": True},
    "106": {"label": "Medical emergency", "group": "scenario", "color": "#ef4444", "lock": True},
}

HARNESS = r"""
local M = {}
function M.ctl(name)
  return { Name = name, String = "", Boolean = false, Value = 0, Legend = "", Color = nil }
end
-- Controls.X behaves as one control, Controls.X[i] for arrays
function M.build_controls(defs)
  local C = {}
  for _, d in ipairs(defs) do
    if d.Count and d.Count > 1 then
      local a = {}
      for i = 1, d.Count do a[i] = M.ctl(d.Name .. " " .. i) end
      C[d.Name] = a
    else
      C[d.Name] = M.ctl(d.Name)
    end
  end
  return C
end
function M.press(c)
  c.Boolean = true
  if c.EventHandler then c.EventHandler(c) end
  c.Boolean = false
end
function M.set(c, s)
  c.String = s
  if c.EventHandler then c.EventHandler(c) end
end
M.sent = {}
M.http = {}
M.lines = {}
TcpSocket = { Events = { Connected = 1, Data = 2, Closed = 3, Error = 4, Timeout = 5 }, EOL = { CrLf = 1 } }
function TcpSocket.New()
  local s = { IsConnected = false }
  function s:Connect(ip, port) M.connected_to = ip .. ":" .. port end
  function s:Disconnect() self.IsConnected = false end
  function s:Write(data) table.insert(M.sent, (data:gsub("\r\n$", ""))) end
  function s:ReadLine() return table.remove(M.lines, 1) end
  M.sock = s
  return s
end
HttpClient = { Download = function(t) table.insert(M.http, t) end }
Timer = { New = function() return { Start = function() end } end }
return M
"""


def main():
    cues = dict(SAMPLE_CUES)
    if len(sys.argv) > 1:  # a real cue list from a unit, plus sample event cues 1 and 2
        cues = dict(json.loads(Path(sys.argv[1]).read_text()), **{k: SAMPLE_CUES[k] for k in ("1", "2")})
    lua = LuaRuntime(unpack_returned_tuples=True)
    M = lua.execute(HARNESS)
    to_lua = lambda v: lua.table_from({k: to_lua(x) for k, x in v.items()}) if isinstance(v, dict) else v  # noqa: E731
    lua.execute("package.preload['rapidjson'] = function() return { decode = _G.__decode } end")
    lua.globals().__decode = lambda s: to_lua(json.loads(s))

    failures = 0

    def check(name, cond, detail=""):
        nonlocal failures
        print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
        failures += 0 if cond else 1

    # ---- design time --------------------------------------------------------
    lua.execute(PLUGIN)  # Controls is nil: only defines the functions
    g = lua.globals()
    props = lua.table_from({p["Name"]: lua.table_from({"Value": p["Value"]}) for p in g.GetProperties().values()})
    defs = list(g.GetControls(props).values())
    names = set()
    for d in defs:
        n = d["Count"]
        names |= {d["Name"]} if n == 1 else {f"{d['Name']} {i}" for i in range(1, n + 1)}
    laid = set()
    for page in (1, 2):
        props["page_index"] = lua.table_from({"Value": page})
        layout, graphics = g.GetControlLayout(props)
        laid |= set(layout.keys())
        check(f"page {page} has graphics", len(list(graphics.values())) > 0)
    check("every control is on a page", names <= laid, sorted(names - laid))
    check("no layout entry without a control", laid <= names, sorted(laid - names))
    check("9 scenario + 12 event buttons by default",
          {"Scenario 9", "Event 12"} <= names and "Scenario 10" not in names)

    # ---- runtime ------------------------------------------------------------
    g.Properties = lua.table_from({"Connect To": lua.table_from({"Value": "Player"}),
                                   "Debug Print": lua.table_from({"Value": "None"})})
    C = M.build_controls(g.GetControls(props))
    g.Controls = C
    lua.execute(PLUGIN)  # now runs the runtime section
    check("defaults ports", C.TcpPort.String == "7700" and C.HttpPort.String == "8080")
    M.set(C.IPAddress, "10.0.0.21")
    check("connects to IP:TCP port", M.connected_to == "10.0.0.21:7700", M.connected_to)

    # socket connects -> STATUS (no token)
    M.sock.IsConnected = True
    M.sock.EventHandler(M.sock, 1, None)
    check("asks for STATUS on connect", "STATUS" in list(M.sent.values()), list(M.sent.values()))
    check("connected LED", C.Connected.Boolean)

    # cue list arrives over HTTP
    req = list(M.http.values())[-1]
    check("fetches /api/cues", req.Url == "http://10.0.0.21:8080/api/cues", req.Url)
    req.EventHandler(req, 200, json.dumps(cues), None)
    sc = [c for c in sorted(cues, key=lambda k: (0, int(k)) if k.isdigit() else (1, k))
          if cues[c].get("group") == "scenario"]
    ev = [c for c in sorted(cues, key=lambda k: (0, int(k)) if k.isdigit() else (1, k))
          if cues[c].get("group") != "scenario"]
    check("scenario buttons labelled in cue order",
          [C.Scenario[i + 1].Legend for i in range(len(sc))] == [cues[n]["label"] or f"Cue {n}" for n in sc],
          [C.Scenario[i + 1].Legend for i in range(len(sc))])
    check("scenario buttons coloured", C.Scenario[1].Color == (cues[sc[0]].get("color") or None), C.Scenario[1].Color)
    check("event buttons mapped", [C.EventCue[i + 1].String for i in range(len(ev))] == ev)
    check("unused buttons blank", C.Event[len(ev) + 1].Legend == "—", C.Event[len(ev) + 1].Legend)

    fire = next(i for i, n in enumerate(sc) if cues[n]["label"] == "Fire alarm") + 1
    M.press(C.Scenario[fire])
    check("scenario button sends CUE <n>", list(M.sent.values())[-1] == f"CUE {sc[fire - 1]}", list(M.sent.values())[-1])
    M.press(C.Event[1])
    check("event button sends CUE <n>", list(M.sent.values())[-1] == f"CUE {ev[0]}")
    M.press(C.Normal)
    check("Normal sends NORMAL", list(M.sent.values())[-1] == "NORMAL")

    # feedback
    M.lines[1] = f'EVENT mode=cue cue={sc[fire - 1]} media="Fire" id=x kind=video lock=1 label="Fire alarm"'
    M.sock.EventHandler(M.sock, 2, None)
    check("active scenario LED lit", C.ScenarioActive[fire].Boolean and not C.ScenarioActive[1 if fire != 1 else 2].Boolean)
    check("locked LED + labels", C.Locked.Boolean and "Fire alarm" in C.ActiveCue.String and C.Mode.String == "cue")
    M.lines[1] = 'EVENT mode=default cue="" media="Loop" id=y kind=video lock=0 label=""'
    M.sock.EventHandler(M.sock, 2, None)
    check("normal clears LEDs", C.IsNormal.Boolean and not C.Locked.Boolean and not C.ScenarioActive[fire].Boolean
          and C.Mode.String == "normal")
    M.lines[1] = "ERR blocked: 'Fire alarm' is active - send NORMAL to clear it (or add force=1)"
    M.sock.EventHandler(M.sock, 2, None)
    check("errors shown in Status", "blocked" in C.Status.String and C.Status.Value == 1)

    # manual mapping
    C.ManualMap.Boolean = True
    M.set(C.EventCue[1], "2")
    req = list(M.http.values())[-1]
    req.EventHandler(req, 200, json.dumps(cues), None)
    check("manual cue number keeps its button", C.EventCue[1].String == "2" and C.Event[1].Legend == cues["2"]["label"],
          (C.EventCue[1].String, C.Event[1].Legend))

    # via master
    g.Properties["Connect To"].Value = "Master"
    C2 = M.build_controls(g.GetControls(props))
    g.Controls = C2
    lua.execute(PLUGIN)
    check("master target defaults to ALL", C2.Target.String == "ALL")
    M.sock.IsConnected = True
    M.set(C2.Target, "GROUP east")
    M.press(C2.Normal)
    check("master commands prefixed with target", list(M.sent.values())[-1] == "GROUP east NORMAL")
    M.set(C2.Target, "SCREEN atrium")
    M.lines[1] = 'EVENT screen=lobby online=1 mode=cue cue=1 media="x" lock=0'
    M.lines[2] = 'EVENT screen=atrium online=1 mode=hold cue=2 media="Sponsor" lock=0'
    M.sock.EventHandler(M.sock, 2, None)
    check("master feedback follows the targeted screen", C2.NowPlaying.String == "Sponsor", C2.NowPlaying.String)

    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
