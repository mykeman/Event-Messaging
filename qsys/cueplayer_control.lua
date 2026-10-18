--[[
  CuePlayer control for Q-SYS (Control Script / Text Controller)

  Talks to a CuePlayer screen (port 7700) or to the CuePlayer master (its TCP port)
  over a persistent TCP socket, with auto-reconnect and live feedback.

  Create these controls on the script component (Properties > Controls):
    IPAddress     Text Box                  e.g. 10.10.20.31
    Port          Text Box                  7700 (player) or the master's TCP port
    Token         Text Box (optional)       the unit's API token, blank if none
    Target        Text Box (optional)       master only: "ALL", "GROUP east", "SCREEN atrium"; blank = direct player
    Cue           Button (Momentary) x N    Count = number of cue buttons (e.g. 16)
    CueNumber     Text Box x N              cue number fired by each button (defaults to 1..N)
    CueActive     LED x N                   lights for the cue currently on screen
    GlobalCue     Button (Momentary) x M    master only: fires GCUE <GlobalCueNumber[i]>
    GlobalCueNumber Text Box x M
    Default       Button (Momentary)
    Black         Button (Momentary)
    Next          Button (Momentary)
    Connected     LED
    Mode          Text Box (Indicator)      default / cue / adhoc / hold / black
    NowPlaying    Text Box (Indicator)
    Status        Text Box (Indicator)      last reply or error

  Any other system can use the same protocol, or the HTTP API, e.g.
    HttpClient.Download{ Url = "http://10.10.20.31:8080/api/cue/3", Headers = {["X-API-Key"]="..."},
                         EventHandler = function(t, code, data, err) end }
]]

local sock = TcpSocket.New()
sock.ReadTimeout = 0
sock.WriteTimeout = 5
sock.ReconnectTimeout = 5

local function ctl(name) return Controls[name] end
local function arr(name)
  local c = Controls[name]
  if c == nil then return {} end
  if type(c) == "table" and c[1] ~= nil then return c end
  return { c }
end

local function setText(name, v) if ctl(name) then ctl(name).String = v or "" end end

local function send(line)
  if sock.IsConnected then
    sock:Write(line .. "\r\n")
  else
    setText("Status", "not connected")
  end
end

-- Prefix commands with the master target when configured
local function targeted(cmd)
  local t = ctl("Target") and ctl("Target").String or ""
  if t ~= "" then return t .. " " .. cmd end
  return cmd
end

local function quote(s)
  if s:find("%s") then return '"' .. s .. '"' end
  return s
end

-- Parse key=value / key="quoted value" pairs from a status line
local function parse(line)
  local out = {}
  for k, v in line:gmatch('(%w+)="([^"]*)"') do out[k] = v end
  for k, v in line:gmatch('(%w+)=([^%s"]+)') do if out[k] == nil then out[k] = v end end
  return out
end

local function updateFeedback(kv)
  if kv.mode then setText("Mode", kv.mode) end
  if kv.media then setText("NowPlaying", kv.media) end
  local active = kv.mode == "cue" and kv.cue or ""
  local nums = arr("CueNumber")
  for i, led in ipairs(arr("CueActive")) do
    local n = (nums[i] and nums[i].String ~= "" and nums[i].String) or tostring(i)
    led.Boolean = (active ~= "" and active == n)
  end
end

sock.EventHandler = function(s, evt, err)
  if evt == TcpSocket.Events.Connected then
    if ctl("Connected") then ctl("Connected").Boolean = true end
    setText("Status", "connected")
    local tok = ctl("Token") and ctl("Token").String or ""
    if tok ~= "" then s:Write("AUTH " .. tok .. "\r\n") end
    s:Write("STATUS\r\n")
  elseif evt == TcpSocket.Events.Data then
    local line = s:ReadLine(TcpSocket.EOL.CrLf)
    while line do
      if line:match("^EVENT") or line:match("^STATUS") or line:match("^OK mode=") then
        local kv = parse(line)
        -- Via a master, lines carry screen=<name>; only follow the screen in Target ("SCREEN x")
        local t = ctl("Target") and ctl("Target").String or ""
        local want = t:match("^[Ss][Cc][Rr][Ee][Ee][Nn]%s+(.+)$")
        if kv.screen == nil or (want ~= nil and kv.screen == want) then
          updateFeedback(kv)
        end
      end
      if line:match("^ERR") or line:match("^OK") then setText("Status", line) end
      line = s:ReadLine(TcpSocket.EOL.CrLf)
    end
  elseif evt == TcpSocket.Events.Closed or evt == TcpSocket.Events.Error
      or evt == TcpSocket.Events.Timeout then
    if ctl("Connected") then ctl("Connected").Boolean = false end
    setText("Status", "disconnected" .. (err and (": " .. err) or ""))
  end
end

local function connect()
  if sock.IsConnected then sock:Disconnect() end
  local ip = ctl("IPAddress") and ctl("IPAddress").String or ""
  local port = tonumber(ctl("Port") and ctl("Port").String or "") or 7700
  if ip ~= "" then
    setText("Status", "connecting to " .. ip .. ":" .. port)
    sock:Connect(ip, port)
  end
end

-- Buttons ---------------------------------------------------------------------
local nums = arr("CueNumber")
for i, btn in ipairs(arr("Cue")) do
  if nums[i] and nums[i].String == "" then nums[i].String = tostring(i) end
  btn.EventHandler = function(c)
    if c.Boolean then
      local n = (nums[i] and nums[i].String ~= "" and nums[i].String) or tostring(i)
      send(targeted("CUE " .. quote(n)))
    end
  end
end

local gnums = arr("GlobalCueNumber")
for i, btn in ipairs(arr("GlobalCue")) do
  btn.EventHandler = function(c)
    if c.Boolean then
      local n = (gnums[i] and gnums[i].String ~= "" and gnums[i].String) or tostring(i)
      send("GCUE " .. quote(n))
    end
  end
end

local function simple(name, cmd)
  if ctl(name) then
    ctl(name).EventHandler = function(c) if c.Boolean then send(targeted(cmd)) end end
  end
end
simple("Default", "DEFAULT")
simple("Black", "BLACK")
simple("Next", "NEXT")

for _, name in ipairs({ "IPAddress", "Port", "Token" }) do
  if ctl(name) then ctl(name).EventHandler = connect end
end

-- Keep-alive so a dead link is noticed quickly
local ping = Timer.New()
ping.EventHandler = function() if sock.IsConnected then sock:Write("PING\r\n") end end
ping:Start(15)

connect()
