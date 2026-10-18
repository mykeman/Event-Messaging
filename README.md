# CuePlayer

An open digital signage platform for events and venues. It plays cue-triggered video and image content on LED walls and displays, and is controlled from your AV control system, show controller or a browser.

CuePlayer is for places that run a rotation of standard messaging and need to switch to event-specific content on cue: stadiums, arenas, public squares, convention centres, theatres, campuses and corporate foyers. Wayfinding, sponsor loops, safety and emergency messages, and "welcome to tonight's event" screens can all be triggered from a control system button, a schedule or an API call.

- **API triggering** from Q-SYS, Crestron, Extron, AMX, Bitfocus Companion, show-control software or anything else that can send HTTP, TCP or UDP, with live feedback for button LEDs.
- **Media library** split into *standard* generic messaging and *event-specific* files (video or image), with drag-and-drop upload in the web GUI and optional auto-delete dates for event content.
- **Cues**: numbered/labelled presets, optionally a sequence of items, with loop, duration and *then* behaviour (return to default, hold the last frame, black, or chain to another cue).
- **Default (idle) content**: a looping playlist (or black) that plays whenever nothing is triggered.
- **Custom output raster**: compose at the LED processor's exact pixel map, with fit/fill/stretch/1:1 scaling per item, and place the canvas anywhere in the HDMI raster (with optional rescale).
- **NDI output** of the canvas for monitoring and remote displays, plus a live preview in the web GUI.
- **Standalone or fleet**: each screen is a complete, autonomous unit. Optionally add a **master server** VM to manage many screens: groups, one trigger to many screens, global cues and media distribution.
- Runs on Linux: a VM, a small x86 PC, or a Raspberry Pi 4/5.

```
                         ┌───────────────────────── Master VM (optional) ─────────────────────────┐
Control ── TCP/HTTP ───▶ │ screens · groups · global cues · fleet media library · sync · status   │
     │                   └───────────────┬───────────────────────┬───────────────────────────────┘
     │                                   │ HTTP                  │ HTTP
     │ (or direct)       ┌───────────────▼───────┐       ┌───────▼───────────────┐
     └──── TCP/HTTP ───▶ │ Player: East LED      │  ...  │ Player: Atrium        │
                         │ web GUI · cues · media│       │                       │
                         │ GStreamer engine      │       │                       │
                         └──┬─────────────┬──────┘       └───────────────────────┘
                         HDMI (KMS)      NDI
                         → LED processor → monitoring / remote displays
```

If the master is offline, every screen keeps playing and can still be controlled directly. The master is a convenience layer, never a single point of failure.

---

## Install

### Screen endpoint (player)

Raspberry Pi OS Lite (Bookworm, 64-bit), Debian 12+ or Ubuntu 22.04+:

```bash
git clone https://github.com/mykeman/Event-Messaging.git && cd Event-Messaging
sudo ./deploy/install.sh --with-ndi
```

The script installs GStreamer, ffmpeg and the app into `/opt/cueplayer`, with media and settings in `/var/lib/cueplayer`. It then creates a `cueplayer` systemd service and opens the web GUI on `http://<ip>:8080/`. `--with-ndi` runs `deploy/install-ndi.sh`, which:

1. downloads the NDI SDK for Linux (you'll be asked to accept the NDI licence), then
2. builds the GStreamer `ndisink` plugin from gst-plugins-rs. The build takes about 5 minutes on x86 and 20–40 minutes on a Pi.

You can skip NDI and add it later. The player runs without it and reports `NDI: unavailable`.

**For direct-to-HDMI output with no desktop**, boot to console (`sudo systemctl set-default multi-user.target`) and leave the video sink on `auto` (it picks `kms`). On a machine with a desktop session, `auto` uses the desktop's video sink instead.

### Master server

On any small Linux VM (it needs no GPU or video stack):

```bash
git clone https://github.com/mykeman/Event-Messaging.git && cd Event-Messaging
sudo ./deploy/install.sh --role master
```

Then on each player, open **Settings → Master server** and set the *Master URL* (e.g. `http://10.10.20.5:8080`), plus the *Master token* if the master has one. The player registers itself and appears on the master's **Screens** tab. You can also add screens by hand on the master.

### Development / trying it out (no GStreamer)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m cueplayer --mock --data ./data            # player GUI on :8080
.venv/bin/python -m cueplayer --role master --data ./mdata --port 8090 --tcp-port 7800
.venv/bin/python tests/smoke_test.py                           # end-to-end test
```

`--mock` swaps the GStreamer engine for a simulated one, so you can test the GUI and API on a laptop.

---

## LED / output setup (Settings tab)

| Setting | Meaning |
|---|---|
| **Canvas** width × height @ fps | The raster everything is composed at. Use the LED processor's pixel map (e.g. `1152×576`). NDI carries exactly this. |
| **Scale media** | `fit` (letterbox), `fill` (crop), `stretch`, or `none` (1:1, centred). Can be overridden per item. |
| **Display** width × height | The HDMI mode to drive (e.g. `1920×1080`). |
| **Offset X/Y** | Where the canvas sits in the HDMI raster, matching the region your LED processor captures. |
| **Out width/height** | Optionally rescale the canvas on HDMI (0 = 1:1). |
| **Video sink** | `auto`, `kms` (direct DRM, no desktop), `gl`, `x`, `wayland`, `fb`, or `none` (NDI only, e.g. a VM). |

The Settings tab shows a live diagram of the canvas inside the display raster. Saving output settings restarts the video pipeline, which blanks the screen for about a second.

**Raspberry Pi notes:** use 25 or 30 fps and keep the canvas at 1080p or below. Encode media as H.264 1080p (or HEVC on a Pi 5, which has no H.264 hardware decoder). Pre-scaling content to the canvas size saves CPU. NDI encoding is CPU-heavy, so a Pi 5 or an x86 mini PC is the better choice when you need NDI at full rate.

---

## Q-SYS integration

`qsys/cueplayer_control.lua` is a ready-made Control Script. It keeps a persistent TCP connection, auto-reconnects, and drives cue buttons, cue-active LEDs, Default/Black/Next, and now-playing feedback. The controls it expects are listed at the top of the file. Point it at a player (port 7700) for a single screen, or at the master to drive groups and global cues.

### TCP / UDP line protocol

Players listen on TCP 7700 and UDP 7701. There is one command per line, terminated by CR and/or LF:

```
PING                                → PONG
AUTH <token>                        → OK AUTH          (only if an API token is set)
CUE 3                               → OK mode=cue cue=3 media="Welcome" id=… kind=video
CUE welcome LOOP                    cue by label, with option overrides
PLAY "Gala Opening" HOLD            ad-hoc media by name, id or filename
PLAY gala DURATION=30 AFTER=cue:1
DEFAULT | STOP                      back to default content
BLACK FADE=1000
NEXT
STATUS                              → STATUS mode=… cue=… media="…" elapsed=… duration=…
EVENTS OFF | EVENTS ON              unsolicited EVENT lines (on by default)
```

Options: `LOOP`, `HOLD`, `DURATION=<s>`, `AFTER=default|hold|black|cue:<n>`, `FADE=<ms>`.
Over UDP, a single datagram can carry `AUTH <token> CUE 3`.

TCP clients receive `EVENT mode=… cue=… media="…"` whenever playback changes.

**Master** (its TCP port):

```
GCUE 10                       global cue (multi-screen actions)
CUE 3                         global cue 3 if defined, else ALL CUE 3
ALL BLACK
GROUP east CUE 3 LOOP
SCREEN atrium PLAY "Welcome" HOLD
east DEFAULT                  bare group or screen name also works
STATUS                        one STATUS line per screen
```

### HTTP API

Every command accepts **GET or POST**, with options in the query string or a JSON body. That makes it easy to use from Q-SYS `HttpClient`, a browser or curl. When an API token is set, send `Authorization: Bearer <token>`, `X-API-Key: <token>` or `?token=<token>`. Interactive docs are at `/docs` on every unit.

Player:

```
GET  /api/cue/{n|label}?loop=1&after=hold
GET  /api/play/{media id|name}?duration=10&after=black&transition=0
GET  /api/default        GET /api/black        GET /api/next        GET /api/status
GET  /api/media          POST /api/media (multipart: file, category=event|library, name, expires=YYYY-MM-DD)
PATCH/DELETE /api/media/{id}
GET  /api/cues           PUT /api/cues/{n}      DELETE /api/cues/{n}
GET  /api/settings       PATCH /api/settings
GET  /api/preview.jpg    GET /api/preview.mjpg   WS /api/ws
```

Master:

```
GET  /api/gcue/{n|label}
GET  /api/target/{all|group:<g>|screen:<id|name>}/{cue|play|default|black|stop|next}[/{ref}]
GET  /api/screens        POST /api/screens       PATCH/DELETE /api/screens/{id}
POST /api/media (file, category, targets=all,group:east)    POST /api/sync
GET/PUT/DELETE /api/gcues/{n}       GET/PUT/DELETE /api/cues/{n} (shared player cue list)
```

---

## How the master distributes media

- Media uploaded on the master has **targets** (`all`, `group:east`, `screen:atrium`, comma separated). The master pushes each file to the matching screens using the same media id, checks it by SHA-256 so unchanged files are never re-sent, and tags it as *managed by master*.
- Changing targets or deleting media on the master removes it from screens that no longer need it. **Media uploaded directly on a screen is never touched**, so operators can still drop in last-minute event files locally.
- The **shared cue list** is pushed to screens that have *Sync cue list* enabled, so cue 3 means the same thing everywhere. **Global cues** live only on the master and can do different things on different screens from one trigger.

## How playback works

A single always-running GStreamer pipeline renders the canvas. A live background source feeds a `compositor`, then a `tee` splits the output to HDMI (via `videobox` placement), `ndisink`, the JPEG web preview, and a clock sink. Each clip is a bin that gets added to the running pipeline. Its timestamp offset is set when its first frame arrives, and until then the previous clip repeats its last frame, so cuts and crossfades have no black gaps. If the pipeline fails (for example, the HDMI output is unplugged), it rebuilds itself after 5 seconds and resumes what was on screen.

## Data layout

```
/var/lib/cueplayer/
  settings.json   media.json   cues.json        # player
  screens.json    global_cues.json              # master
  media/          thumbs/      incoming/
```

Back up this directory to back up a unit.

## Status / known limitations

- The API, cue logic, TCP/UDP protocol, master fleet sync and both GUIs are covered by `tests/smoke_test.py`, which uses the mock engine.
- **The GStreamer engine (`cueplayer/engine.py`) has not yet been run on target hardware.** Before deployment, bench-test it on the actual Pi or VM with real media: cut/fade between video and image, looping, HDMI placement on the LED processor, NDI in Studio Monitor, and long soak runs.
- NDI uses the NDI SDK runtime, which has its own licence terms.
- Port changes take effect after `sudo systemctl restart cueplayer`.
