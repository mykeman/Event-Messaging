# CuePlayer

An open digital signage platform for events and venues. It plays cue-triggered video and image content on LED walls and displays, and is controlled from your AV control system, show controller or a browser.

CuePlayer is for places that run a rotation of standard messaging and need to switch to event-specific content on cue: stadiums, arenas, public squares, convention centres, theatres, campuses and corporate foyers. Wayfinding, sponsor loops, safety and emergency messages, and "welcome to tonight's event" screens can all be triggered from a control system button, a schedule or an API call.

- **API triggering** from Q-SYS, Crestron, Extron, AMX, Bitfocus Companion, show-control software or anything else that can send HTTP, TCP or UDP, with live feedback for button LEDs.
- **Media library** split into *standard* generic messaging and *event-specific* files (video or image), with drag-and-drop upload in the web GUI and optional auto-delete dates for event content.
- **Standard scenarios**: a ready-made set of situations every venue needs a message for: venue at capacity, entry closed, event delayed, severe weather, lost child, medical emergency, fire alarm, evacuate now and shelter in place (cues 101–109). Assign your content to each. Emergency scenarios are **locked**: they stay on screen until *Normal* is pressed, and ordinary triggers can't replace them.
- **Event cues**: numbered/labelled presets for event-specific content, optionally a sequence of items, with loop, duration and *then* behaviour (return to normal, hold the last frame, black, or chain to another cue).
- **Normal playlist**: the fallback that plays whenever no scenario or event cue is active.
- **Automatic media conversion**: each unit checks its own hardware and converts files it would struggle to play (codec, size, frame rate, 10-bit, interlaced) to an optimised H.264 copy in the background.
- **Custom output raster**: compose at the LED processor's exact pixel map, with fit/fill/stretch/1:1 scaling per item, and place the canvas anywhere in the HDMI raster (with optional rescale).
- **NDI output** of the canvas for monitoring and remote displays, plus a live preview in the web GUI.
- **AES67 audio output** (optional, off by default, not yet bench-tested): the audio mix as a PTP-locked AES67 multicast stream that a Q-SYS Core can receive directly.
- **Standalone or fleet**: each screen is a complete, autonomous unit. Optionally add a **master server** VM to manage many screens: groups, one trigger to many screens, global cues and media distribution.
- Runs on Linux: a VM, a small x86 PC, or a Raspberry Pi 4/5.

```
                         ┌───────────────────────── Master VM (optional) ─────────────────────────┐
Control ── TCP/HTTP ───▶ │ screens · groups · global cues · fleet media library · sync · status   │
     │                   └───────────────┬───────────────────────┬───────────────────────────────┘
     │                                   │ HTTP                  │ HTTP
     │ (or direct)       ┌───────────────▼───────┐       ┌───────▼───────────────┐
     └──── TCP/HTTP ───▶ │ Player: LED 1         │  ...  │ Player: LED 2         │
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

You can skip NDI and add it later. The player runs without it and reports `NDI: unavailable`. NDI on Linux finds and announces sources through **avahi** (mDNS). The installers set it up; Ubuntu Server and minimal Debian don't include it. If NDI is *on* but viewers can't see the unit, check `systemctl status avahi-daemon`.

#### Dedicated display (appliance mode)

A plain install drives HDMI only while the player is running. Boot messages and the console login prompt still appear on the screen at boot, while the video pipeline restarts after an error, and whenever the service is stopped. For a screen that shows only CuePlayer content, add `--take-hdmi`:

```bash
sudo ./deploy/install.sh --with-ndi --take-hdmi --hdmi-mode 1920x1080@60
sudo reboot
```

This:

- hides the boot text, logo and cursor, moves the local console to tty3 and removes the login prompt from the screen (manage the unit over SSH),
- boots the HDMI port in the given mode,
- disables the splash screen and any desktop, and
- sets the player to direct KMS output at that mode. Your other settings are kept.

`--hdmi-mode` defaults to `1920x1080@60`. `--hdmi-port` defaults to whichever port has a display plugged in at install time, so connect the screen first. To choose a port yourself, use `HDMI-A-1` (the port nearest the USB-C power on a Pi 4 or 5) or `HDMI-A-2`. The flag works on Raspberry Pi OS (it edits `cmdline.txt` and `config.txt`) and on x86 machines that boot with GRUB, and it backs up every boot file it changes to `*.cueplayer.bak`.

If the display or LED processor is powered up after the player, or has no EDID, the Pi may never turn HDMI on. Add `--hdmi-force` to keep the port on regardless. The forced port then always reports *connected*, even when nothing is plugged in, so only use it on the port the screen is really connected to: forcing the wrong port sends the video to the empty socket and leaves the real screen black.

To undo `--take-hdmi`, which restores the original boot files and the login prompt:

```bash
sudo ./deploy/install.sh --release-hdmi && sudo reboot
```

Without `--take-hdmi`, boot to console (`sudo systemctl set-default multi-user.target`) and leave the video sink on `auto`, which picks `kms`. On a machine with a desktop session, `auto` uses the desktop's video sink instead.

### Master server

On any small Linux VM (it needs no GPU or video stack):

```bash
git clone https://github.com/mykeman/Event-Messaging.git && cd Event-Messaging
sudo ./deploy/install.sh --role master
```

Then add players. Any of these works:

- **Found on the network:** players announce themselves over mDNS (Bonjour). They are listed under *Found on the network* on the master's **Screens** tab; click **Add**. This needs the master and players on the same network segment, because mDNS doesn't cross routers.
- **By address:** type a player's IP address or hostname (`10.10.20.31` or `led1.local`) into the box on the Screens tab. There's no need for `http://` or the port. The screen takes the player's own name and groups once it answers.
- **From the player:** in the player's **Settings → Master server**, set *Master URL* to the master's IP address or hostname, plus the *Master token* if the master has one. The player registers itself.

Run with `--no-mdns` (or `CUEPLAYER_MDNS=0`) to turn off announcing and discovery.

### Updating an installed unit

From the cloned repository on the Pi or VM:

```bash
cd Event-Messaging && sudo ./deploy/update.sh
```

This pulls the latest code, updates `/opt/cueplayer` and restarts the service. Media, settings and the service setup are kept, and if the new version fails to start it rolls back to the previous one. Re-run `deploy/install.sh` instead when you need new OS packages or install options such as `--take-hdmi`.

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

**Smooth playback:**

- Match the canvas frame rate to your content and the HDMI mode to the canvas. For 50 fps video, use a 50 fps canvas and `--hdmi-mode 1920x1080@50`. A 50 fps video on a 30 fps canvas or a 60 Hz output will judder.
- The Live tab shows the **decoder** in use and the number of **dropped frames**. If frames are being dropped, raise *Latency budget (ms)* in Settings → Canvas. If the decoder says *software*, check the codec: a Pi 4 decodes H.264 in hardware, a Pi 5 only HEVC (H.265).
- NDI encoding is CPU-heavy. If playback is uneven with NDI on, test with NDI disabled. A Pi 5 or an x86 mini PC is the better choice when you need NDI at full rate.
- Keep the canvas at 1080p or below on a Pi, and pre-scale content to the canvas size where you can.
- Media is composed without per-pixel transparency, so transparent areas of PNGs show as black.

---


**Connecting screens in the field.** A player doesn't need a screen at power-on. With no screen connected it runs without HDMI, and the Live tab shows *waiting for a screen to be connected*; NDI, the preview and control all keep working. The ports are checked every two seconds. When a screen is plugged in, unplugged and replugged, swapped, or moved to the other port, HDMI reconnects to it within a few seconds, and audio follows. All outputs blank for about a second while the video pipeline restarts, and the current item restarts from its beginning. If an LED processor needs a signal before it will report itself connected, use `install.sh --take-hdmi --hdmi-force` on the port it is plugged into, so that port is always driven.

## Q-SYS integration

### Q-SYS plugin (recommended)

`qsys/CuePlayer.qplug` is a Q-SYS Designer plugin. To install it, copy the file to `Documents\QSC\Q-Sys Designer\Plugins` and restart Designer; it appears under *User Plugins → CuePlayer*.

- **Control page:** a large **Normal** button, plus Black and Next, a grid of **Scenario** buttons and a grid of **Event** buttons, each with an active LED. It also shows mode, the locked state, the active cue and what's playing.
- **Automatic labels:** buttons take their labels and colours from the unit's cue list, refreshed every minute. Scenario cues fill the Scenario buttons and event cues fill the Event buttons, in cue-number order, so renaming a cue in the web GUI updates the panel. Turn on *Manual* on the Setup page to type a cue number per button instead.
- **Setup page:** IP address, TCP port (7700), HTTP port (8080), API token, and, when connecting to a master, a *Target* (`ALL`, `GROUP main` or `SCREEN led-2`).
- **Properties:** *Connect To* (Player or Master) and how many scenario and event buttons to show.

Every control has a pin, so the buttons can also be driven from other Q-SYS logic, such as a fire-panel contact closure into *Scenario 7*.

`qsys/cueplayer_control.lua` is a simpler Control Script alternative with the same protocol.

### TCP / UDP line protocol

Players listen on TCP 7700 and UDP 7701. There is one command per line, terminated by CR and/or LF:

```
PING                                → PONG
AUTH <token>                        → OK AUTH          (only if an API token is set)
CUE 3                               → OK mode=cue cue=3 media="Welcome" id=… kind=video
CUE welcome LOOP                    cue by label, with option overrides
PLAY "Gala Opening" HOLD            ad-hoc media by name, id or filename
PLAY gala DURATION=30 AFTER=cue:1
NORMAL | CLEAR | DEFAULT            back to the normal playlist (clears a locked scenario)
CUE 1 FORCE                         override a locked scenario
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
GROUP main CUE 3 LOOP
SCREEN led-2 PLAY "Welcome" HOLD
main DEFAULT                  bare group or screen name also works
STATUS                        one STATUS line per screen
```

### HTTP API

Every command accepts **GET or POST**, with options in the query string or a JSON body. That makes it easy to use from Q-SYS `HttpClient`, a browser or curl. When an API token is set, send `Authorization: Bearer <token>`, `X-API-Key: <token>` or `?token=<token>`. Interactive docs are at `/docs` on every unit.

Player:

```
GET  /api/cue/{n|label}?loop=1&after=hold
GET  /api/play/{media id|name}?duration=10&after=black&transition=0
GET  /api/normal         GET /api/black        GET /api/next        GET /api/status
     (a locked scenario returns 409 to other triggers; add force=1 to override)
POST /api/cues/scenarios                         add any missing standard scenarios
POST /api/media/{id}/convert   DELETE /api/media/{id}/converted   GET /api/hardware
GET  /api/media          POST /api/media (multipart: file, category=event|library, name, expires=YYYY-MM-DD)
PATCH/DELETE /api/media/{id}
GET  /api/cues           PUT /api/cues/{n}      DELETE /api/cues/{n}
GET  /api/settings       PATCH /api/settings
GET  /api/preview.jpg    GET /api/preview.mjpg   WS /api/ws
GET  /api/aes67.sdp      SDP of the AES67 audio stream (404 when off, 503 until PTP is locked)
```

Master:

```
GET  /api/gcue/{n|label}
GET  /api/target/{all|group:<g>|screen:<id|name>}/{cue|play|default|black|stop|next}[/{ref}]
GET  /api/screens        POST /api/screens       PATCH/DELETE /api/screens/{id}
POST /api/media (file, category, targets=all,group:main)    POST /api/sync
GET/PUT/DELETE /api/gcues/{n}       GET/PUT/DELETE /api/cues/{n} (shared player cue list)
```

### AES67 audio into Q-SYS

> **Untested on real AES67 hardware.** The AES67 output is written against GStreamer's RTP and PTP elements and its SDP/SAP output is unit-tested, but it has not yet been run against a Q-SYS Core. Bench-test it (PTP lock, discovery, clean audio over a long run, lip-sync with the LED wall) before using it on a show.

A player can send its audio mix (video soundtracks) straight to Q-SYS as an AES67 stream: 48 kHz, L24 (or L16), 1 or 2 channels, 1 ms packets, multicast, locked to the Core's PTP clock. No Dante or USB audio interface is needed. If you'd rather not use it, a Dante AVIO USB adapter used as an ALSA device (*Audio → Sink: alsa*) also works.

**On the player** (Settings tab):

1. Turn on **Audio**, then **AES67 audio output**. Set **PTP domain** to the Core's domain (Q-SYS default: 0).
2. Leave **Multicast address** blank for `239.69.<c>.<d>` (from the unit's `a.b.c.d`), or set an address from your multicast plan. Each player needs its own address.
3. Save. The card shows *waiting for PTP sync*, then *on* with the PTP **grandmaster** ID. Until PTP has locked the player runs without AES67; when it locks, the pipeline restarts once (a short blank on the screen).
4. The stream is announced by **SAP** every 30 s. The SDP is also at `http://<player>:8080/api/aes67.sdp`.

`sudo deploy/install.sh` gives GStreamer's `gst-ptp-helper` the `cap_net_bind_service,cap_net_admin` capabilities it needs to use PTP ports 319/320. An OS update to GStreamer can reset this, so re-run `install.sh` afterwards (if the card says *PTP init failed*, this is why).

**In Q-SYS Designer:**

1. Core properties → Network → **AES67**: enable AES67 on the LAN the players are on, and set the **PTP domain** to match. Make sure the Core (or your designated grandmaster) is the PTP leader; the player is PTPv2 follower only.
2. Add an **AES67 Receiver** to the design. With SAP discovery on, pick the stream (named *"<screen name> audio"* by default). Otherwise paste the SDP from `/api/aes67.sdp` or enter the multicast address, port 5004, 48 kHz, L24 and channel count by hand.
3. Set the receiver latency to at least 2 ms (more on a busy network). The player stamps each packet with the PTP time it is sent, so the receiver latency only has to cover the network.

**Network:**

- Turn on **IGMP snooping** (with a querier) on the audio VLAN, so multicast only reaches the ports that join it.
- Use **QoS**: DSCP **46 (EF)** for PTP and **34 (AF41)** for media, with strict-priority queueing for PTP. The player marks media with *Media DSCP* (default 34). GStreamer's PTP helper may not mark its PTP packets, so classify PTP (UDP 319/320) by port on the switch if your network only trusts markings.
- Keep PTP and media on the same interface. On a unit with more than one NIC, set **Interface** (e.g. `eth0`).

**Timing:** the player runs its whole pipeline on the PTP clock. The audio leaves at the same moment the frame goes to HDMI (after the *Latency budget*, plus about 20 ms and 2 frames), so it stays in step with the LED wall. Add delay in Q-SYS only if the LED processor delays the picture.

---


#### Network interface and ST 2110-30

- **Interface:** *Settings → AES67 / ST 2110-30 → Network interface* lists the unit's ports with their addresses (on a Giada F106D, its two Ethernet ports). The audio, its PTP timing and its SAP announcements all use the chosen port, so you can keep media on a dedicated AV network. GStreamer can only start PTP once per run, so after changing the interface on a running stream, restart the service (`sudo systemctl restart cueplayer`). The status line says so if that's needed.
- **Profile `st2110-30`:** for SMPTE ST 2110 systems. It defaults the PTP domain to 127 (the ST 2059-2 default; still editable), adds `a=fmtp:<pt> channel-order=SMPTE2110.(ST)` (or `(M)` for mono) to the SDP, and drops the AES67/RAVENNA-only `a=clock-domain` line. Use a 1 ms packet time for ST 2110-30 Level A, or 0.125 ms for Level C receivers.
- **RTP payload type:** 96–127, for receivers that expect a specific value.

The media DSCP (default 34, AF41) and TTL are configurable for both profiles.

## Upload and conversion progress

While anything is uploading, converting or being sent to screens, an **Activity** panel appears in the bottom-left corner of the web GUI on every tab. Each upload shows its progress, then *Processing on the unit…* while the player checks the file. Each conversion shows its target format, a progress bar, its speed (for example 1.4× real time) and the time left. On the master, each screen being sent media shows the file count. The panel can be collapsed, and it clears itself when the work is done.

## Media conversion

Every player checks what it is running on (Raspberry Pi 4 or 5, x86 with Intel/AMD/NVIDIA decoding, and so on) and which codecs it can decode in hardware. It then checks each file against that and the canvas. A file gets an optimised **H.264 playback copy** (8-bit 4:2:0 MP4, at the canvas size and frame rate) when it:

- uses a codec this unit can't decode efficiently (ProRes, DNxHD, MPEG-2, VP9/AV1 without hardware support, HEVC on machines without an HEVC decoder),
- is 10-bit or 4:2:2/4:4:4,
- is interlaced (it gets deinterlaced),
- is larger than it will be shown on the canvas, or above a Pi 4's 1080p H.264 decoder limit,
- has a higher frame rate than the canvas (e.g. 59.94 fps on a 50 fps canvas), or
- has an extreme bitrate.

Large photos, and image formats other than PNG/JPEG, are scaled to the canvas as PNG/JPEG.

Conversion runs in the background at the lowest CPU and disk priority, leaving two cores free for playback. **The original keeps playing until the copy is ready**, so last-minute event files can be used straight away. The Media tab shows each file's status (queued, converting with a progress bar, optimised, or failed) and why it was converted. *Edit* has **Convert now** and **Use original** for individual files. Changing the canvas re-checks the whole library, and existing libraries are checked when you update.

Settings → *Media conversion* has the mode (`auto`, `always` or `off`), whether to keep originals, x264 quality (CRF) and speed preset, and thread count. On a Pi, converting a long 1080p clip can take a while. Converting high-bitrate masters before upload, or on a faster machine, is still the quickest route.

## How the master distributes media

- Media uploaded on the master has **targets** (`all`, `group:main`, `screen:led-2`, comma separated). The master pushes each file to the matching screens using the same media id, checks it by SHA-256 so unchanged files are never re-sent, and tags it as *managed by master*.
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

## Playlists and schedule

The **Schedule** tab has a playlist editor and a week calendar.

- **Playlists** are named, ordered lists of videos and images. Each image can have its own on-screen time; blank uses the Settings image duration. A playlist can shuffle. **▶ Play now**, `GET /api/playlist/<name>/play` and TCP `PLAYLIST <name>` all play a playlist straight away, looping.
- **Schedule entries** play a playlist (or the Normal playlist, or black) from a start to an end time. An entry runs once, every day, or weekly on chosen days, with an optional *until* date. An end time earlier than the start runs past midnight. Click an empty slot in the calendar to add an entry, or click an entry to edit it. Where entries overlap, the one that started most recently wins.
- **How it plays:** while an entry is on, its playlist **replaces the Normal playlist** as the fallback content. Cues and scenarios still interrupt it, and it resumes when they finish. A locked scenario always wins. An entry marked **take over** also cuts off event cues when it starts.
- **Time:** schedules use the unit's clock. Set **Settings → Schedule → Timezone** (e.g. `Australia/Melbourne`), since server installs often run on UTC. The calendar shows the unit's current time and what's on now.
- **API:** `GET/POST/PUT/DELETE /api/playlists`, `/api/schedule`, `GET /api/schedule/now` (active, next and upcoming), and `GET /api/schedule/occurrences?start=…&end=…`.

Schedules are per screen.

## Transitions

**Settings → Transitions** sets the default change between items:

- **cut**
- **fade**: crossfade
- **dip**: dip to black, where the old item fades out and then the new one fades in
- **slide-left / right / up / down**: the new item pushes the old one off the canvas

Each has a duration in milliseconds. Any cue can override the type and duration in its *Transition* column. A command can override them too: `GET /api/cue/3?transition=dip&transition_ms=800`, or over TCP, `CUE 3 TRANSITION=slide-left FADE=600` or `CUE 3 CUT`. Audio crossfades with every type except cut.

A new item only appears once its first real picture is decoded. Some hardware decoders hand out a few empty frames at the start of a stream, which show as solid green. The player discards those, and any frame the decoder flags as damaged, so they never reach the screen.

## Test patterns

The Live tab has a **Test pattern** picker. The patterns are:

- SMPTE bars: standard, HD (RP 219), 75% and 100%
- checkerboard (8 px and 1 px)
- gradient
- solid white, red, green, blue and black
- a moving ball for checking motion and frame cadence

Each pattern is generated at the exact canvas size, so every pixel of the LED map is used, and goes to HDMI, NDI and the preview. The optional caption along the bottom shows the screen's name and canvas size, so during LED mapping you can tell which screen is which. *Tone* adds a 1 kHz line-up tone at −20 dBFS (it needs Audio on). The pattern stays up until the next command, such as **Normal**. A locked scenario blocks it unless forced.

```
GET /api/test                        list of patterns
GET /api/test/smpte?tone=1&label=0   show one          GET /api/test/off   back to normal
TCP:  TEST smpte-hd TONE   |   TEST checkers NOLABEL   |   TEST OFF
Master:  ALL TEST smpte   |   GROUP main TEST ball   |   GET /api/target/all/test/smpte
```

On the master, the Screens tab toolbar has the same picker, sending to the selected target. Global cues can include a *test* action.

## Audio

Turn on **Settings → Audio**; it is off by default. With *Sink* on `auto` and *ALSA device* left blank, a Raspberry Pi sends sound out of the **same HDMI port as the video** (`hdmi:CARD=vc4hdmi0` or `vc4hdmi1`). On Intel/AMD PCs, where all HDMI outputs share one sound card, the player uses the HDMI output whose screen reports audio support (its ELD), for example `plughw:CARD=PCH,DEV=3`. Without this, ALSA's default is the first HDMI port or the headphone jack. To use a different output, set *ALSA device*, for example `hw:CARD=Headphones` for the headphone jack, or a USB or Dante AVIO adapter. List the devices with `aplay -L`. The Live tab's *Audio* status shows which device is in use.

## Status / known limitations

- Tests:
  - `tests/smoke_test.py` covers the API, cue and scenario-lock logic, the TCP/UDP protocol, master fleet sync and the GUI pages, using the mock engine.
  - `tests/test_transcode.py` covers the convert/don't-convert decisions and the ffmpeg command.
  - `tests/test_aes67.py` covers the AES67 SDP and SAP packets and the AES67 settings validation.
  - `tests/qsys_plugin_test.py` runs the Q-SYS plugin in a simulated Q-SYS runtime (needs `pip install lupa`).
  - The plugin has not yet been loaded in Q-SYS Designer, and conversion has not yet been run with a real ffmpeg on a Pi.
- **The GStreamer engine (`cueplayer/engine.py`) has not yet been run on target hardware.** Before deployment, bench-test it on the actual Pi or VM with real media: cut/fade between video and image, looping, HDMI placement on the LED processor, NDI in Studio Monitor, and long soak runs.
- **AES67 output has not yet been tested with real AES67 hardware or a Q-SYS Core.** Only the SDP/SAP generation and settings validation are tested (`tests/test_aes67.py`). Bench-test PTP lock, Q-SYS discovery, audio quality and lip-sync before relying on it. It supports 1–2 channels and assumes the PTP helper has the capabilities set by `install.sh`.
- NDI uses the NDI SDK runtime, which has its own licence terms.
- Port changes take effect after `sudo systemctl restart cueplayer`.
