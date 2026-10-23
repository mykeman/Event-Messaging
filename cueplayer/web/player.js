"use strict";

const S = { status: null, media: [], cues: {}, settings: null, info: null, defList: [] };

const SETTINGS_FORM = [
  { section: "output", title: "Canvas (content raster)", note: "The pixel size everything is composed at. For an LED wall, use the processor's pixel map (e.g. 1152×576). NDI carries this.",
    presets: [["1920×1080", 1920, 1080], ["1280×720", 1280, 720], ["3840×2160", 3840, 2160], ["1080×1920", 1080, 1920]],
    fields: [["canvas_width", "Width", "number"], ["canvas_height", "Height", "number"],
      ["framerate", "Frame rate", "select", [24, 25, 30, 50, 60]], ["background", "Background", "color"],
      ["scale_mode", "Scale media", "select", ["fit", "fill", "stretch", "none"]],
      ["hw_decode", "Hardware decode", "check"], ["latency_ms", "Latency budget (ms)", "number"]] },
  { section: "display", title: "Display output (HDMI)", note: "Display mode and where the canvas is placed. Out width/height 0 = canvas size (1:1). Display mode: auto lets the player set the HDMI mode (usually 60 Hz); keep reuses the mode from install.sh --take-hdmi --hdmi-mode, e.g. 50 Hz.",
    fields: [["enabled", "Enabled", "check"], ["sink", "Video sink", "select", ["auto", "kms", "gl", "x", "wayland", "fb", "none"]],
      ["width", "Display width", "number"], ["height", "Display height", "number"],
      ["x", "Offset X", "number"], ["y", "Offset Y", "number"],
      ["out_width", "Out width", "number"], ["out_height", "Out height", "number"],
      ["connector_id", "DRM connector (0 = auto)", "number"],
      ["modeset", "Display mode (kms)", "select", ["auto", "keep"]]] },
  { section: "ndi", title: "NDI output", fields: [["enabled", "Enabled", "check"], ["name", "Source name", "text", null, "wide"]] },
  { section: "transcode", title: "Media conversion", note: "Files this unit would struggle to play (codec, size, frame rate, 10-bit, interlaced) get an optimised H.264 copy in the background. The original plays until it's ready.",
    fields: [["mode", "Mode", "select", ["auto", "always", "off"]], ["keep_original", "Keep originals", "check"],
      ["crf", "Quality (CRF)", "number"], ["preset", "Speed preset", "select", ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"]],
      ["threads", "Threads (0 = auto)", "number"]] },
  { section: "transition", title: "Transitions", note: "Default between items: cut, crossfade (fade), dip to black, or slide (the new item pushes the old one off). Cues can override it.",
    fields: [["type", "Type", "select", ["cut", "fade", "dip", "slide-left", "slide-right", "slide-up", "slide-down"]], ["duration_ms", "Duration (ms)", "number"]] },
  { section: "audio", title: "Audio", note: "Video soundtracks to HDMI and into NDI. With sink auto and ALSA device blank, sound goes out of the same HDMI port as the video. Set a device (e.g. hw:CARD=Headphones) to send it elsewhere.",
    fields: [["enabled", "Enabled", "check"], ["sink", "Sink", "select", ["auto", "alsa", "pulse", "none"]], ["device", "ALSA device", "text"]] },
  { section: "aes67", title: "AES67 audio output", note: "Sends the audio mix as an AES67 multicast stream, locked to PTP, for Q-SYS and other AES67 receivers. Needs Audio enabled. Blank multicast = 239.69.x.y from this unit's address.",
    fields: [["enabled", "Enabled", "check"], ["sap", "Announce (SAP)", "check"], ["multicast", "Multicast address", "text"], ["port", "Port", "number"],
      ["channels", "Channels", "select", [2, 1]], ["bit_depth", "Bit depth", "select", [24, 16]],
      ["ptime_ms", "Packet time (ms)", "select", [1, 0.125, 0.25, 4]], ["ptp_domain", "PTP domain", "number"],
      ["ttl", "TTL", "number"], ["dscp", "Media DSCP", "number"], ["interface", "Interface (blank = default)", "text"],
      ["session_name", "Session name", "text", null, "wide"]] },
  { section: "preview", title: "Web preview", fields: [["enabled", "Enabled", "check"], ["fps", "FPS", "number"], ["width", "Width", "number"]] },
  { section: "control", title: "Control", note: "Port changes take effect after a service restart.",
    fields: [["http_port", "HTTP port", "number"], ["tcp_port", "TCP port", "number"], ["udp_port", "UDP port", "number"],
      ["api_token", "API token (blank = open)", "password", null, "wide"]] },
  { section: "fleet", title: "Master server (optional)", note: "Leave Master URL blank for a standalone unit. When set, this screen registers itself with the master.",
    fields: [["master_url", "Master URL", "text", null, "wide"], ["master_token", "Master token", "password"],
      ["screen_name", "Screen name", "text"], ["groups", "Groups (comma separated)", "list", null, "wide"],
      ["advertise_url", "Advertise URL (optional)", "text", null, "wide"]] },
];

/* ---------------- live ---------------- */
function renderStatus() {
  const st = S.status;
  if (!st) return;
  const m = st.media;
  $("#now-badge").replaceChildren(badge(st.mode));
  $("#now-cue").textContent = st.cue ? "Cue " + st.cue + (cueLabel(st.cue) ? " · " + cueLabel(st.cue) : "") : "";
  $("#now-title").textContent = m ? m.name : (st.mode === "black" ? "Black" : "—");
  const eng = st.engine || {};
  $("#st-ndi").textContent = eng.ndi || "—";
  $("#st-display").textContent = (eng.display || "—") + (eng.display_format ? " · " + eng.display_format : "");
  $("#st-audio").textContent = eng.audio || "—";
  $("#st-display-warn").textContent = eng.display_warning ? "⚠ " + eng.display_warning : "";
  $("#st-display-warn").classList.toggle("hidden", !eng.display_warning);
  $("#st-decoder").textContent = eng.video_decoder ? eng.video_decoder + (/^(avdec|vp8dec|vp9dec|dav1d|openh264)/.test(eng.video_decoder) ? " (software)" : " (hardware)") : "—";
  const dropped = eng.frames_dropped || 0;
  const f = eng.fps;
  $("#st-frames").textContent = (f ? `clip ${f.clip} · mixed ${f.composited} · HDMI ${f.display} fps` +
    (f.display_drops ? ` (${f.display_drops}/s not shown)` : "") : "—") +
    (dropped ? ` · ${dropped} dropped` : "");
  $("#st-frames").title = "Frames per second at each stage: the playing clip, the compositor, and the HDMI output. A stage at 0 while the one before is running shows where playback stalls.";
  $("#st-frames").style.color = dropped ? "var(--warn)" : "";
  if ($("#aes67-status")) $("#aes67-status").textContent = aes67Status();
  $("#canvas-info").textContent = (eng.canvas || "") + (eng.backend ? " · " + eng.backend : "");
  const errs = (eng.errors || []).slice(-3).reverse();
  $("#st-errors").replaceChildren(...errs.map(e => el("div", {}, new Date(e.t * 1000).toLocaleTimeString() + " " + e.msg)));
  $$("#live-cues .cue-btn, #live-scenarios .cue-btn").forEach(b => b.classList.toggle("active", ["cue", "hold"].includes(st.mode) && b.dataset.cue === st.cue));
  $("#lock-banner").classList.toggle("hidden", !st.lock);
  $("#lock-text").textContent = st.lock ? "🔒 " + (st.label || "Scenario") + " is active" : "";
  if (st.mode === "default") $("#now-badge").replaceChildren(el("span", { class: "badge default" }, "normal"));
  $$(".media[data-id]").forEach(c => c.classList.toggle("playing", !!m && c.dataset.id === m.id));
  tickProgress();
}
function tickProgress() {
  const st = S.status;
  if (!st) return;
  const elapsed = st.started ? Date.now() / 1000 - st.started : 0;
  const dur = st.duration;
  $("#now-time").textContent = st.started ? fmtDur(elapsed) + (dur ? " / " + fmtDur(dur) : "") + (st.loop ? " ⟳" : "") : "";
  $("#now-progress").style.width = dur ? Math.min(100, (elapsed % dur) / dur * 100) + "%" : "0";
}
setInterval(tickProgress, 500);

function cueLabel(n) { return (S.cues[n] || {}).label || ""; }

function renderLiveCues() {
  const all = Object.entries(S.cues);
  const fill = (box, entries, empty) => {
    if (!entries.length) { box.replaceChildren(el("div", { class: "muted small" }, empty)); return; }
    box.replaceChildren(...entries.map(cueButton));
  };
  fill($("#live-scenarios"), all.filter(([, c]) => c.group === "scenario"), "No scenarios — add the standard set on the Cues tab.");
  fill($("#live-cues"), all.filter(([, c]) => c.group !== "scenario"), "No event cues yet — add some on the Cues tab.");
  renderStatus();
}

function cueButton([n, c]) {
    const items = Array.isArray(c.media) ? c.media : (c.media ? [c.media] : []);
    const names = items.map(id => (S.media.find(m => m.id === id) || {}).name || "missing").join(" → ");
    return el("button", { class: "cue-btn", dataset: { cue: n }, style: c.color ? { borderColor: c.color } : null,
      onclick: () => api("POST", "/api/cue/" + encodeURIComponent(n)) },
      el("span", { class: "num" }, "CUE " + n),
      el("span", { class: "lbl" }, c.label || names || "(empty)"),
      el("span", { class: "meta" }, [c.lock ? "🔒 locked" : "", !items.length ? "no media" : "", c.loop ? "loop" : "",
        c.after !== "default" && !c.loop ? "→ " + c.after : ""].filter(Boolean).join(" · ")));
}

function thumbUrl(m) { return m.has_thumb ? withToken("/api/media/" + m.id + "/thumb") : ""; }

function renderQuick() {
  const f = $("#quick-filter").value.toLowerCase();
  const list = S.media.filter(m => !f || m.name.toLowerCase().includes(f));
  $("#quick-media").replaceChildren(...list.map(m => el("div", { class: "media", dataset: { id: m.id } },
    el("div", { class: "thumb", style: { backgroundImage: m.has_thumb ? `url("${thumbUrl(m)}")` : "" }, title: "Play " + m.name,
      onclick: () => api("POST", "/api/play/" + m.id, { loop: $("#quick-loop").checked, after: $("#quick-hold").checked ? "hold" : "default" }) },
      el("span", { class: "kind badge" }, m.kind),
      m.duration ? el("span", { class: "dur" }, fmtDur(m.duration)) : null),
    el("div", { class: "body" }, el("div", { class: "name small", title: m.name }, m.name)))));
  renderStatus();
}

/* preview polling (jpeg snapshots, at preview fps) */
let previewTimer = null;
function startPreview() {
  clearInterval(previewTimer);
  const img = $("#preview");
  const fps = (S.settings && S.settings.preview && S.settings.preview.fps) || 5;
  const ar = S.settings ? S.settings.output.canvas_width / S.settings.output.canvas_height : 16 / 9;
  $("#preview-wrap").style.aspectRatio = String(ar);
  let busy = false;
  const load = () => {
    if (busy || document.hidden || !$("#tab-live").classList.contains("active")) return;
    busy = true;
    const next = new Image();
    next.onload = () => { img.src = next.src; $("#preview-ph").classList.add("hidden"); busy = false; };
    next.onerror = () => { $("#preview-ph").classList.remove("hidden");
      $("#preview-ph").textContent = S.status && S.status.engine && S.status.engine.backend === "mock" ? "Preview unavailable (mock engine)" : "No preview"; busy = false; };
    next.src = withToken("/api/preview.jpg?t=" + Date.now());
  };
  previewTimer = setInterval(load, 1000 / Math.min(fps, 10));
  load();
}

/* ---------------- media library ---------------- */
function mediaCard(m) {
  const exp = m.expires ? el("span", { class: "small expiry" }, "expires " + m.expires) : null;
  const meta = [m.kind, m.width ? m.width + "×" + m.height : "", fmtBytes(m.size)].filter(Boolean).join(" · ");
  const inDefault = S.defList.includes(m.id);
  return el("div", { class: "media", dataset: { id: m.id } },
    el("div", { class: "thumb", style: { backgroundImage: m.has_thumb ? `url("${thumbUrl(m)}")` : "" },
      onclick: () => api("POST", "/api/play/" + m.id), title: "Play now" },
      el("span", { class: "kind badge" }, m.kind),
      m.duration ? el("span", { class: "dur" }, fmtDur(m.duration)) : null),
    el("div", { class: "body" },
      el("div", { class: "name", title: m.original_filename }, m.name),
      el("div", { class: "small muted" }, meta),
      (m.tags || []).includes("master") ? el("div", {}, el("span", { class: "chip" }, "managed by master")) : null,
      playbackLine(m),
      m.decode === "software" ? el("div", { class: "small muted", title: "The hardware decoder couldn't play this file, so it is decoded in software. Edit → Try hardware decoding to switch back." }, "Decoded in software") : null,
      exp),
    el("div", { class: "actions" },
      el("button", { onclick: () => api("POST", "/api/play/" + m.id) }, "Play"),
      el("button", { onclick: () => editMedia(m) }, "Edit"),
      el("button", { title: inDefault ? "Remove from default playlist" : "Add to default playlist",
        onclick: async () => {
          const list = inDefault ? S.defList.filter(i => i !== m.id) : [...S.defList, m.id];
          await api("PATCH", "/api/settings", { playback: { default_playlist: list } });
          toast(inDefault ? "Removed from default" : "Added to default playlist", "ok"); await loadSettings(); renderLibrary();
        } }, inDefault ? "− Default" : "+ Default"),
      el("button", { class: "danger", onclick: () => confirmDo(`Delete "${m.name}"?`, async () => {
        await api("DELETE", "/api/media/" + m.id); toast("Deleted", "ok"); }) }, "Delete")));
}

/* Conversion status for a media item */
function playbackLine(m) {
  const pb = m.playback || {};
  const why = (pb.reasons || []).join("; ");
  const t = pb.target;
  const tdesc = t ? (t.kind === "video" ? `H.264 ${t.width}×${t.height}@${t.fps}` : `${t.format.toUpperCase()} ${t.width}×${t.height}`) : "";
  switch (pb.status) {
    case "queued": return el("div", { class: "small", style: { color: "var(--warn)" }, title: why }, pb.file ? "⏳ Queued for re-conversion (playing previous copy)" : "⏳ Queued for conversion (playing original)");
    case "converting": return el("div", { class: "small", title: why },
      el("div", { style: { color: "var(--warn)" } }, `Converting to ${tdesc} · ${Math.round(pb.progress || 0)}%`),
      el("div", { class: "progress" }, el("div", { style: { width: (pb.progress || 0) + "%" } })));
    case "ready": return el("div", { class: "small", style: { color: "var(--ok)" }, title: why }, `✓ Optimised: ${tdesc}`);
    case "failed": return el("div", { class: "small expiry", title: pb.error || "" }, "Conversion failed — playing original");
    default:
      if (pb.skip && why) return el("div", { class: "small muted", title: why }, "Playing original (conversion turned off for this file)");
      if (pb.disabled && why) return el("div", { class: "small", style: { color: "var(--warn)" }, title: why }, "May not play smoothly (conversion is off)");
      return null;
  }
}

function renderLibrary() {
  const f = ($("#lib-filter").value || "").toLowerCase();
  const match = m => !f || m.name.toLowerCase().includes(f) || (m.original_filename || "").toLowerCase().includes(f);
  for (const cat of ["event", "library"]) {
    const list = S.media.filter(m => m.category === cat && match(m));
    $("#lib-" + cat).replaceChildren(...(list.length ? list.map(mediaCard) : [el("div", { class: "muted small" }, "Nothing here yet.")]));
  }
  renderStatus();
}

function editMedia(m) {
  const d = el("dialog", {},
    el("h2", {}, "Edit media"),
    el("form", { method: "dialog", class: "grid" },
      el("label", { class: "field" }, el("span", {}, "Name"), el("input", { name: "name", value: m.name })),
      el("div", { class: "row" },
        el("label", { class: "field" }, el("span", {}, "Category"),
          el("select", { name: "category" }, ...[["event", "Event-specific"], ["library", "Standard library"]].map(([v, l]) =>
            el("option", { value: v, selected: m.category === v }, l)))),
        el("label", { class: "field" }, el("span", {}, "Scaling"),
          el("select", { name: "scale_mode" }, ...["", "fit", "fill", "stretch", "none"].map(v =>
            el("option", { value: v, selected: (m.scale_mode || "") === v }, v || "Use default")))),
        el("label", { class: "field" }, el("span", {}, "Auto-delete after"), el("input", { type: "date", name: "expires", value: m.expires || "" }))),
      el("div", { class: "small muted" }, `ID ${m.id} · ${m.original_filename}` + (m.codec ? " · " + m.codec : "") +
        (m.probe && m.probe.fps ? ` · ${m.probe.fps} fps` : "") + (m.probe && m.probe.pix_fmt ? " · " + m.probe.pix_fmt : "")),
      (m.playback && (m.playback.reasons || []).length) ? el("div", { class: "small" },
        el("b", {}, "Conversion: "), m.playback.reasons.join("; "), m.playback.error ? el("div", { class: "expiry" }, m.playback.error) : null) : null,
      el("div", { class: "row" },
        el("button", { type: "button", onclick: async () => { await api("POST", `/api/media/${m.id}/convert`); toast("Conversion queued", "ok"); } },
          (m.playback || {}).status === "ready" ? "Re-convert" : "Convert now"),
        m.decode === "software" ? el("button", { type: "button", onclick: async () => { await api("PATCH", `/api/media/${m.id}`, { decode: "auto" }); toast("Hardware decoding will be tried again", "ok"); } }, "Try hardware decoding") : null,
        (m.playback || {}).status === "ready" || ["queued", "converting"].includes((m.playback || {}).status) ?
          el("button", { type: "button", onclick: async () => { await api("DELETE", `/api/media/${m.id}/converted`); toast("Playing the original", "ok"); } }, "Use original") : null),
      el("div", { class: "row" },
        el("button", { class: "primary", value: "save" }, "Save"),
        el("button", { value: "cancel" }, "Cancel"),
        el("a", { href: withToken("/api/media/" + m.id + "/file"), class: "small" }, "Download original"))));
  d.addEventListener("close", async () => {
    if (d.returnValue === "save") {
      const fd = Object.fromEntries(new FormData(d.querySelector("form")));
      await api("PATCH", "/api/media/" + m.id, fd);
      toast("Saved", "ok");
    }
    d.remove();
  });
  document.body.append(d);
  d.showModal();
}

async function uploadFiles(files) {
  // Several files upload at once; each gets its own entry in the activity overlay
  await Promise.all(files.map(async f => {
    const fd = new FormData();
    fd.append("file", f);
    fd.append("category", $("#up-category").value);
    fd.append("expires", $("#up-expires").value);
    fd.append("scale_mode", $("#up-scale").value);
    try { await uploadWithProgress("/api/media", fd, f); }
    catch (e) { toast(f.name + ": " + e.message, "err"); }
  }));
}

/* Conversions in the activity overlay, from each item's playback status */
const convSeen = new Map();  // id -> last status shown
function syncConversionJobs() {
  const live = new Set();
  for (const m of S.media) {
    const pb = m.playback || {};
    const key = "conv:" + m.id;
    const t = pb.target;
    const what = t ? (t.kind === "video" ? `H.264 ${t.width}×${t.height}@${t.fps}` : `${(t.format || "").toUpperCase()} ${t.width}×${t.height}`) : "";
    if (pb.status === "queued") {
      live.add(m.id);
      jobs.set(key, { title: m.name, detail: "Waiting to convert" + (what ? " to " + what : "") + (pb.file ? " · previous copy plays meanwhile" : " · original plays meanwhile"), pct: null });
    } else if (pb.status === "converting") {
      live.add(m.id);
      const extra = [pb.speed ? pb.speed + "× speed" : "", pb.eta != null ? fmtDur(pb.eta) + " left" : ""].filter(Boolean).join(" · ");
      jobs.set(key, { title: m.name, detail: `Converting to ${what}` + (extra ? " · " + extra : ""), pct: m.kind === "video" ? (pb.progress || 0) : null });
    } else if (convSeen.has(m.id) && jobs.has(key)) {
      if (pb.status === "ready") jobs.done(key, "Optimised: " + what);
      else if (pb.status === "failed") jobs.fail(key, "Conversion failed: " + (pb.error || "unknown error"));
      else jobs.done(key, "Playing the original");
    }
  }
  for (const id of [...convSeen.keys()]) if (!live.has(id)) convSeen.delete(id);
  for (const id of live) convSeen.set(id, true);
  // items deleted while converting
  for (const k of jobs.keys()) if (k.startsWith("conv:") && !S.media.find(m => "conv:" + m.id === k)) jobs.done(k, "Removed");
}

/* ---------------- cues ---------------- */
function mediaOptions(selected) {
  return [el("option", { value: "" }, "— choose —"),
    ...["event", "library"].map(cat => el("optgroup", { label: cat === "event" ? "Event-specific" : "Standard library" },
      ...S.media.filter(m => m.category === cat).map(m => el("option", { value: m.id, selected: m.id === selected }, m.name))))];
}

function cueRow(num, c, isNew) {
  const items = Array.isArray(c.media) ? c.media : (c.media ? [c.media] : [""]);
  const mediaBox = el("div", { class: "grid", style: { gap: "4px" } });
  const addSel = (val) => {
    const s = el("select", { class: "cue-media" }, ...mediaOptions(val));
    mediaBox.append(el("div", { class: "row", style: { flexWrap: "nowrap" } }, s,
      el("button", { class: "icon", title: "Remove", type: "button", onclick: e => { if (mediaBox.children.length > 1) e.target.parentElement.remove(); } }, "×")));
  };
  items.forEach(addSel);
  const afterSel = el("select", { class: "cue-after" },
    ...[["default", "Default"], ["hold", "Hold"], ["black", "Black"]].map(([v, l]) => el("option", { value: v, selected: c.after === v }, l)),
    ...Object.keys(S.cues).filter(n => n !== num).map(n => el("option", { value: "cue:" + n, selected: c.after === "cue:" + n }, "Cue " + n)));
  const tr = el("tr", {},
    el("td", {}, el("input", { class: "cue-num", value: num, disabled: !isNew })),
    el("td", {}, el("input", { class: "cue-label", value: c.label || "", placeholder: "Label" })),
    el("td", {}, mediaBox, el("button", { class: "small icon", type: "button", style: { marginTop: "4px" }, onclick: () => addSel("") }, "+ sequence")),
    el("td", {}, el("select", { class: "cue-group" }, el("option", { value: "event", selected: c.group !== "scenario" }, "Event"),
      el("option", { value: "scenario", selected: c.group === "scenario" }, "Scenario"))),
    el("td", {}, el("input", { type: "checkbox", class: "cue-lock", checked: !!c.lock })),
    el("td", {}, el("input", { type: "checkbox", class: "cue-loop", checked: !!c.loop })),
    el("td", {}, el("input", { type: "number", class: "cue-dur", min: 0, step: "0.1", value: c.duration || "", placeholder: "auto" })),
    el("td", {}, el("div", { class: "row", style: { flexWrap: "nowrap", gap: "4px" } },
      el("select", { class: "cue-tr", title: "Transition into this cue" },
        el("option", { value: "", selected: !c.transition }, "Default"),
        ...[["cut", "Cut"], ["fade", "Fade"], ["dip", "Dip to black"], ["slide-left", "Slide ←"], ["slide-right", "Slide →"],
            ["slide-up", "Slide ↑"], ["slide-down", "Slide ↓"]].map(([v, l]) => el("option", { value: v, selected: c.transition === v }, l))),
      el("input", { type: "number", class: "cue-trms", min: 0, step: 50, value: c.transition_ms ?? "", placeholder: "ms", title: "Duration (ms); blank = default", style: { width: "64px" } }))),
    el("td", {}, afterSel),
    el("td", {}, el("input", { type: "color", class: "cue-color", value: c.color || "#2a2f3a" })),
    el("td", {}, el("div", { class: "row", style: { flexWrap: "nowrap" } },
      el("button", { class: "primary", onclick: () => saveCue(tr) }, "Save"),
      el("button", { title: "Trigger", onclick: () => api("POST", "/api/cue/" + encodeURIComponent(num)) , disabled: isNew }, "▶"),
      el("button", { class: "danger icon", title: "Delete", onclick: () => isNew ? tr.remove() :
        confirmDo("Delete cue " + num + "?", () => api("DELETE", "/api/cues/" + encodeURIComponent(num))) }, "×"))));
  return tr;
}

async function saveCue(tr) {
  const num = $(".cue-num", tr).value.trim();
  if (!num) return toast("Cue number required", "err");
  const media = $$(".cue-media", tr).map(s => s.value).filter(Boolean);
  const color = $(".cue-color", tr).value;
  await api("PUT", "/api/cues/" + encodeURIComponent(num), {
    label: $(".cue-label", tr).value, media: media.length > 1 ? media : (media[0] || null),
    loop: $(".cue-loop", tr).checked, duration: $(".cue-dur", tr).value || null,
    after: $(".cue-after", tr).value, color: color === "#2a2f3a" ? "" : color,
    group: $(".cue-group", tr).value, lock: $(".cue-lock", tr).checked,
    transition: $(".cue-tr", tr).value || null, transition_ms: $(".cue-trms", tr).value === "" ? null : Number($(".cue-trms", tr).value) });
  toast("Cue " + num + " saved", "ok");
}

function renderCues() {
  $("#cue-rows").replaceChildren(...Object.entries(S.cues).map(([n, c]) => cueRow(n, c, false)));
}

/* ---------------- default playlist ---------------- */
function renderDefault() {
  const pb = S.settings.playback;
  $("#def-mode").value = pb.default_mode;
  $("#def-imgdur").value = pb.image_duration;
  $("#def-shuffle").checked = !!pb.default_shuffle;
  const list = $("#def-list");
  list.replaceChildren(...S.defList.map((id, i) => {
    const m = S.media.find(x => x.id === id);
    return el("div", { class: "item" },
      el("span", { class: "mono small muted" }, i + 1),
      el("span", { class: "name", title: m ? m.name : "" }, m ? m.name : "(missing " + id + ")"),
      m ? el("span", { class: "badge" }, m.kind) : null,
      el("button", { class: "icon", disabled: i === 0, onclick: () => { S.defList.splice(i - 1, 0, S.defList.splice(i, 1)[0]); renderDefault(); } }, "↑"),
      el("button", { class: "icon", disabled: i === S.defList.length - 1, onclick: () => { S.defList.splice(i + 1, 0, S.defList.splice(i, 1)[0]); renderDefault(); } }, "↓"),
      el("button", { class: "icon danger", onclick: () => { S.defList.splice(i, 1); renderDefault(); } }, "×"));
  }));
  if (!S.defList.length) list.append(el("div", { class: "muted small" }, "Empty — screen shows black when idle."));
  $("#def-add").replaceChildren(...mediaOptions(""));
}

/* ---------------- settings ---------------- */
function renderSettingsForms() {
  const box = $("#settings-forms");
  box.replaceChildren(...SETTINGS_FORM.map(sec => {
    const vals = S.settings[sec.section] || {};
    const grid = el("div", { class: "form-grid" }, ...sec.fields.map(([key, label, type, opts, cls]) => {
      let input;
      const v = vals[key];
      if (type === "select") input = el("select", {}, ...opts.map(o => el("option", { value: o, selected: String(o) === String(v) }, o)));
      else if (type === "check") return el("label", { class: "check", style: { marginTop: "18px" } },
        el("input", { type: "checkbox", checked: !!v, dataset: { s: sec.section, k: key, t: type } }), label);
      else if (type === "list") input = el("input", { value: (v || []).join(", ") });
      else input = el("input", { type: type === "password" ? "password" : type, value: v ?? "", autocomplete: "off" });
      Object.assign(input.dataset, { s: sec.section, k: key, t: type });
      return el("label", { class: "field " + (cls || "") }, el("span", {}, label), input);
    }));
    const presets = sec.presets ? el("div", { class: "row", style: { marginBottom: "10px" } }, ...sec.presets.map(([l, w, h]) =>
      el("button", { class: "small", onclick: () => {
        $(`[data-s="output"][data-k="canvas_width"]`).value = w; $(`[data-s="output"][data-k="canvas_height"]`).value = h; updateDiagram();
      } }, l))) : null;
    const status = sec.section === "ndi" ? el("div", { class: "small muted", style: { marginTop: "8px" } }, "Status: " + ((S.status && S.status.engine.ndi) || "—"))
      : sec.section === "aes67" ? el("div", { id: "aes67-status", class: "small muted", style: { marginTop: "8px" } }, aes67Status()) : null;
    return el("div", { class: "card" }, el("h2", {}, sec.title),
      sec.note ? el("p", { class: "small muted", style: { marginTop: 0 } }, sec.note) : null, presets, grid, status);
  }));
  $$("#settings-forms input, #settings-forms select").forEach(i => i.addEventListener("input", updateDiagram));
  updateDiagram();
}

function aes67Status() {
  const a = (S.status && S.status.engine && S.status.engine.aes67) || {};
  if (!a.state || a.state === "disabled") return "Status: disabled";
  const parts = ["Status: " + a.state, "PTP " + (a.synced ? "locked" : "not locked") + " (domain " + a.domain + ")"];
  if (a.grandmaster) parts.push("grandmaster " + a.grandmaster);
  if (a.latency_ms) parts.push("send latency " + a.latency_ms + " ms");
  return parts.join(" · ") + (a.synced ? " · SDP: /api/aes67.sdp" : "");
}

function formValue(s, k) {
  const e = $(`#settings-forms [data-s="${s}"][data-k="${k}"]`);
  return e ? inputVal(e) : undefined;
}

function updateDiagram() {
  const cw = +formValue("output", "canvas_width") || 1, ch = +formValue("output", "canvas_height") || 1;
  const dw = +formValue("display", "width") || 1, dh = +formValue("display", "height") || 1;
  const ow = +formValue("display", "out_width") || cw, oh = +formValue("display", "out_height") || ch;
  const x = +formValue("display", "x") || 0, y = +formValue("display", "y") || 0;
  const dg = $("#diagram");
  dg.style.aspectRatio = dw + "/" + dh;
  Object.assign($("#diagram-canvas").style, { left: (x / dw * 100) + "%", top: (y / dh * 100) + "%",
    width: (ow / dw * 100) + "%", height: (oh / dh * 100) + "%" });
  $("#diagram-canvas").textContent = `${cw}×${ch}`;
  $("#diagram-text").textContent = `Display ${dw}×${dh}. Canvas ${cw}×${ch} shown at ${x},${y}` +
    ((ow !== cw || oh !== ch) ? `, scaled to ${ow}×${oh}` : " (1:1)") + ".";
}

async function saveSettings() {
  const patch = {};
  $$("#settings-forms [data-s]").forEach(e => {
    const { s, k, t } = e.dataset;
    let v = inputVal(e);
    if (t === "list") v = String(v).split(",").map(x => x.trim()).filter(Boolean);
    if (t === "select" && /^\d+(\.\d+)?$/.test(v)) v = Number(v);
    (patch[s] = patch[s] || {})[k] = v;
  });
  const oldToken = S.settings.control.api_token;
  S.settings = await api("PATCH", "/api/settings", patch);
  if (patch.control && patch.control.api_token !== oldToken) setToken(patch.control.api_token);
  toast("Settings saved", "ok");
  startPreview();
}

async function loadInfo() {
  S.info = await api("GET", "/api/info");
  try { S.hw = await api("GET", "/api/hardware", undefined, { quiet: true }); } catch { S.hw = null; }
  if (S.hw) {
    const out = S.settings.output, tc = S.settings.transcode || {};
    $("#hw-line").textContent = `This unit: ${S.hw.summary}. ` + (tc.mode === "off" ? "Conversion is off." :
      `Files are optimised for ${out.canvas_width}×${out.canvas_height}@${out.framerate} H.264` + (S.hw.converting ? " · converting now" : "") + (S.hw.queued ? ` · ${S.hw.queued} queued` : "") + ".");
  }
  $("#unit-name").textContent = S.info.name;
  document.title = "CuePlayer · " + S.info.name;
  const sys = S.info.system || {}, disk = S.info.disk || {};
  const rows = [["Name", S.info.name], ["Version", S.info.version], ["Host", sys.hostname],
    ["CPU temp", sys.cpu_temp_c ? sys.cpu_temp_c + " °C" : "—"], ["Load", (sys.load || []).map(n => n.toFixed(2)).join(" ")],
    ["Uptime", sys.uptime_s ? fmtDur(sys.uptime_s) : "—"], ["Library", fmtBytes(disk.library_bytes)], ["Free disk", fmtBytes(disk.free_bytes)],
    ["Engine", S.status ? S.status.engine.backend + " " + (S.status.engine.gst_version || "") : "—"],
    ["Hardware", S.hw ? S.hw.model : "—"], ["HW decode", S.hw ? Object.keys(S.hw.hw_decoders).join(", ").toUpperCase() || "none" : "—"]];
  $("#sysinfo").replaceChildren(...rows.flatMap(([k, v]) => [el("dt", {}, k), el("dd", {}, v ?? "—")]));
  $("#disk").textContent = `Library ${fmtBytes(disk.library_bytes)} · ${fmtBytes(disk.free_bytes)} free`;
}

/* ---------------- API docs ---------------- */
function renderApiDocs() {
  const base = location.origin;
  const c = S.settings.control;
  $("#api-http").textContent = [
    `GET ${base}/api/cue/3`, `GET ${base}/api/cue/welcome?loop=1`,
    `GET ${base}/api/play/<media id or name>?after=hold`, `GET ${base}/api/default`,
    `GET ${base}/api/black?transition=1000`, `GET ${base}/api/next`, `GET ${base}/api/status`,
    `POST ${base}/api/media   (multipart: file, category=event|library)`,
    `GET ${base}/api/preview.mjpg   (live MJPEG preview)`].join("\n");
  $("#api-tcp").textContent = [
    `# TCP ${location.hostname}:${c.tcp_port}   UDP ${location.hostname}:${c.udp_port}`,
    `AUTH <token>          (only if a token is set)`,
    `CUE 3`, `CUE 3 LOOP`, `PLAY "Welcome Slide" HOLD`, `PLAY gala DURATION=30 AFTER=cue:1`,
    `STOP | DEFAULT | BLACK FADE=1000 | NEXT`, `STATUS`, `EVENTS OFF`,
    ``, `> OK mode=cue cue=3 media="Welcome" id=… kind=video`,
    `> EVENT mode=default cue="" media="Loop A" …`].join("\n");
}

/* ---------------- loading ---------------- */
async function loadMedia() { S.media = await api("GET", "/api/media"); renderLibrary(); renderQuick(); renderLiveCues(); if (S.settings) renderDefault(); syncConversionJobs(); }
async function loadCues() { S.cues = await api("GET", "/api/cues"); renderLiveCues(); renderCues(); }
async function loadSettings() { S.settings = await api("GET", "/api/settings"); S.defList = [...(S.settings.playback.default_playlist || [])]; }

async function init() {
  try {
    S.status = await api("GET", "/api/status");
    await loadSettings();
    await Promise.all([loadMedia(), loadCues(), loadInfo()]);
  } catch (e) { console.error(e); return; }
  renderDefault();
  renderSettingsForms();
  renderApiDocs();
  renderStatus();
  startPreview();

  $("#btn-default").onclick = () => api("POST", "/api/default");
  $("#btn-black").onclick = () => api("POST", "/api/black");
  $("#tp-pattern").replaceChildren(...TEST_PATTERNS.map(([v, l]) => el("option", { value: v }, l)));
  $("#tp-show").onclick = () => api("POST", "/api/test/" + $("#tp-pattern").value,
    { tone: $("#tp-tone").checked, label: $("#tp-label").checked });
  $("#btn-next").onclick = () => api("POST", "/api/next");
  $("#quick-filter").oninput = renderQuick;
  $("#lib-filter").oninput = renderLibrary;
  makeDropZone($("#drop"), uploadFiles);
  $("#cue-add").onclick = () => {
    const nums = Object.keys(S.cues).filter(n => /^\d+$/.test(n)).map(Number);
    const next = String(nums.length ? Math.max(...nums) + 1 : 1);
    $("#cue-rows").append(cueRow(next, { loop: false, after: "default" }, true));
  };
  $("#lock-clear").onclick = () => api("POST", "/api/normal");
  $("#cue-scenarios").onclick = async () => {
    const r = await api("POST", "/api/cues/scenarios");
    toast(r.added.length ? `Added ${r.added.length} scenarios (101–109) — assign media to each` : "All standard scenarios are already present", "ok");
  };
  $("#def-add-btn").onclick = () => { const v = $("#def-add").value; if (v) { S.defList.push(v); renderDefault(); } };
  $("#def-save").onclick = async () => {
    await api("PATCH", "/api/settings", { playback: { default_playlist: S.defList, default_mode: $("#def-mode").value,
      default_shuffle: $("#def-shuffle").checked, image_duration: Number($("#def-imgdur").value) || 10 } });
    toast("Default content saved", "ok"); await loadSettings(); renderLibrary();
  };
  $("#settings-save").onclick = saveSettings;

  connectWS(async msg => {
    if (msg.status) { S.status = msg.status; renderStatus(); }
    if (msg.event === "library") await loadMedia();
    if (msg.event === "cues") await loadCues();
    if (msg.event === "settings") { await loadSettings(); renderDefault(); }
    if (msg.event === "error") toast(msg.error, "err");
    if (msg.event === "notice") toast(msg.message, "ok");
  }, on => {
    $("#conn-dot").classList.toggle("on", on);
    $("#conn-text").textContent = on ? "live" : "reconnecting";
  });
  setInterval(loadInfo, 30000);
  // Engine stats (decoder, dropped frames) change between playback events
  setInterval(async () => {
    if (document.hidden || !$("#tab-live").classList.contains("active")) return;
    try { S.status = await api("GET", "/api/status", undefined, { quiet: true }); renderStatus(); } catch {}
  }, 1000);
}

initTabs(name => { if (name === "live") startPreview(); });
init();
