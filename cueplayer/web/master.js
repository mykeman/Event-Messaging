"use strict";

const M = { screens: [], groups: {}, media: [], gcues: {}, cues: {}, settings: null, selected: new Set() };

/* ---------------- targets ---------------- */
function targetOptions() {
  const opts = [["all", "All screens"]];
  for (const g of Object.keys(M.groups)) opts.push(["group:" + g, "Group: " + g]);
  for (const s of M.screens) opts.push(["screen:" + s.id, "Screen: " + s.name]);
  return opts;
}
function renderTargets() {
  const cur = $("#target").value || "all";
  const opts = targetOptions();
  if (M.selected.size) opts.unshift(["selected", `Selected (${M.selected.size})`]);
  $("#target").replaceChildren(...opts.map(([v, l]) => el("option", { value: v, selected: v === cur }, l)));
  $("#target-list").replaceChildren(...[["all"], ...Object.keys(M.groups).map(g => ["group:" + g]),
    ...M.screens.map(s => ["screen:" + s.name])].map(([v]) => el("option", { value: v })));
}
async function sendTarget(action, ref, opts = {}) {
  const t = $("#target").value;
  const targets = t === "selected" ? [...M.selected].map(id => "screen:" + id) : [t];
  const results = await Promise.all(targets.map(tg =>
    api("POST", `/api/target/${encodeURIComponent(tg)}/${action}` + (ref ? "/" + encodeURIComponent(ref) : ""), opts).catch(e => ({ ok: 0, failed: 1, error: e.message }))));
  const ok = results.reduce((a, r) => a + (r.ok || 0), 0), failed = results.reduce((a, r) => a + (r.failed || 0), 0);
  const errs = results.flatMap(r => Object.values(r.results || {}).filter(x => !x.ok).map(x => `${x.name}: ${x.error}`));
  $("#t-result").textContent = `${action}${ref ? " " + ref : ""} → ${ok} ok` + (failed ? `, ${failed} failed: ${errs.join("; ")}` : "");
  toast(`${action} → ${ok} ok${failed ? ", " + failed + " failed" : ""}`, failed ? "err" : "ok");
}

/* ---------------- screens ---------------- */
function screenCard(s) {
  const st = s.status || {};
  const m = st.media;
  const sync = s.sync;
  const syncText = !sync ? "" : sync.state === "running" ? `Syncing ${sync.uploaded}/${sync.total}${sync.current ? " · " + sync.current : ""}`
    : sync.state === "error" ? "Sync failed: " + sync.error : `Synced ${ago(sync.finished)} (${sync.uploaded} up, ${sync.removed} removed)`;
  const card = el("div", { class: "screen" + (M.selected.has(s.id) ? " selected" : ""), dataset: { id: s.id } },
    el("div", { class: "preview-wrap", title: "Click to select", onclick: () => { M.selected.has(s.id) ? M.selected.delete(s.id) : M.selected.add(s.id); renderScreens(); renderTargets(); },
      style: s.info && s.info.canvas ? { aspectRatio: s.info.canvas[0] + "/" + s.info.canvas[1] } : null },
      el("img", { class: "spv", dataset: { id: s.id }, alt: "" }),
      el("div", { class: "placeholder" }, s.online ? "" : "offline")),
    el("div", { class: "info" },
      el("div", { class: "row between" },
        el("span", { class: "name" }, el("span", { class: "dot " + (s.online ? "on" : ""), style: { marginRight: "8px" } }), s.name),
        s.online ? badge(st.mode) : el("span", { class: "badge off" }, "offline")),
      el("div", { class: "small", style: { minHeight: "20px" } }, s.online ? [(st.cue ? "Cue " + st.cue + " · " : ""), m ? m.name : ""].join("") : el("span", { class: "muted" }, s.error || "")),
      el("div", {}, ...(s.groups || []).map(g => el("span", { class: "chip" }, g)), s.sync_cues ? el("span", { class: "chip" }, "cue sync") : null),
      el("div", { class: "small muted" }, s.url, s.info ? " · " + (s.info.canvas || []).join("×") + " · v" + s.info.version : ""),
      syncText ? el("div", { class: "small " + (sync.state === "error" ? "expiry" : "muted") }, syncText) : null,
      el("div", { class: "row", style: { marginTop: "6px" } },
        el("button", { onclick: () => api("POST", `/api/target/screen:${s.id}/default`) }, "Default"),
        el("button", { class: "danger", onclick: () => api("POST", `/api/target/screen:${s.id}/black`) }, "Black"),
        el("button", { onclick: () => api("POST", "/api/sync", { target: "screen:" + s.id }) }, "Sync"),
        el("button", { onclick: () => editScreen(s) }, "Edit"),
        el("a", { class: "btn small", href: s.url, target: "_blank", rel: "noopener" }, "Open ↗"))));
  return card;
}
function renderScreens() {
  const box = $("#screens");
  box.replaceChildren(...(M.screens.length ? M.screens.map(screenCard) :
    [el("div", { class: "card muted" }, "No screens yet. Add one, or set “Master URL” in a player's Settings to have it register itself.")]));
  const online = M.screens.filter(s => s.online).length;
  $("#screen-summary").textContent = `${M.screens.length} screens · ${online} online · ${Object.keys(M.groups).length} groups` +
    (M.selected.size ? ` · ${M.selected.size} selected` : "");
  refreshPreviews();
}
function refreshPreviews() {
  $$("img.spv").forEach(img => {
    const s = M.screens.find(x => x.id === img.dataset.id);
    if (!s || !s.online) return;
    const n = new Image();
    n.onload = () => { img.src = n.src; };
    n.src = withToken(`/api/screens/${s.id}/preview.jpg?t=` + Date.now());
  });
}
setInterval(() => { if ($("#tab-screens").classList.contains("active") && !document.hidden) refreshPreviews(); }, 2000);

function editScreen(s) {
  const isNew = !s;
  s = s || { name: "", url: "http://", groups: [], enabled: true, sync_cues: false };
  const d = el("dialog", {},
    el("h2", {}, isNew ? "Add screen" : "Edit " + s.name),
    el("form", { method: "dialog", class: "grid" },
      el("label", { class: "field" }, el("span", {}, "Name"), el("input", { name: "name", value: s.name, required: true })),
      el("label", { class: "field" }, el("span", {}, "Player URL"), el("input", { name: "url", value: s.url, placeholder: "http://10.0.0.21:8080", required: true })),
      el("label", { class: "field" }, el("span", {}, "Player API token" + (s.has_token ? " (set — leave blank to keep)" : "")), el("input", { name: "token", type: "password", autocomplete: "off" })),
      el("label", { class: "field" }, el("span", {}, "Groups (comma separated)"), el("input", { name: "groups", value: (s.groups || []).join(", ") })),
      el("label", { class: "check" }, el("input", { type: "checkbox", name: "enabled", checked: s.enabled !== false }), "Enabled"),
      el("label", { class: "check" }, el("input", { type: "checkbox", name: "sync_cues", checked: !!s.sync_cues }), "Sync shared cue list to this screen (replaces its cues)"),
      el("div", { class: "row" },
        el("button", { class: "primary", value: "save" }, "Save"), el("button", { value: "cancel", formnovalidate: true }, "Cancel"),
        isNew ? null : el("button", { class: "danger", value: "delete", formnovalidate: true }, "Remove screen"))));
  d.addEventListener("close", async () => {
    const f = d.querySelector("form");
    if (d.returnValue === "save") {
      const body = { name: f.name.value, url: f.url.value, groups: f.groups.value, enabled: f.enabled.checked, sync_cues: f.sync_cues.checked };
      if (f.token.value) body.token = f.token.value;
      await api(isNew ? "POST" : "PATCH", isNew ? "/api/screens" : "/api/screens/" + s.id, body);
      toast("Saved", "ok");
    } else if (d.returnValue === "delete" && confirm(`Remove ${s.name} from the master? (The player keeps running.)`)) {
      await api("DELETE", "/api/screens/" + s.id);
    }
    d.remove();
    loadScreens();
  });
  document.body.append(d);
  d.showModal();
}

/* ---------------- global cues ---------------- */
function renderGcueButtons() {
  const entries = Object.entries(M.gcues);
  $("#gcue-buttons").replaceChildren(...(entries.length ? entries.map(([n, c]) => el("button", {
    class: "cue-btn", style: c.color ? { borderColor: c.color } : null,
    onclick: async () => {
      const r = await api("POST", "/api/gcue/" + encodeURIComponent(n));
      const ok = r.actions.reduce((a, x) => a + (x.ok || 0), 0), bad = r.actions.reduce((a, x) => a + (x.failed || 0) + (x.error ? 1 : 0), 0);
      toast(`${c.label || "Cue " + n}: ${ok} ok${bad ? ", " + bad + " failed" : ""}`, bad ? "err" : "ok");
    } },
    el("span", { class: "num" }, "GCUE " + n), el("span", { class: "lbl" }, c.label || "(unnamed)"),
    el("span", { class: "meta" }, c.actions.length + " action" + (c.actions.length === 1 ? "" : "s")))) :
    [el("div", { class: "muted small" }, "No global cues yet.")]));
}

function actionRow(a = {}) {
  const tg = el("select", { class: "a-target" }, ...targetOptions().map(([v, l]) => el("option", { value: v, selected: v === (a.target || "all") }, l)));
  const act = el("select", { class: "a-action" }, ...["cue", "play", "default", "black", "stop", "next"].map(v => el("option", { value: v, selected: v === (a.action || "cue") }, v)));
  const ref = el("input", { class: "a-ref", value: a.ref || "", placeholder: "cue # / media", list: "media-list-dl" });
  const after = el("select", { class: "a-after" }, ...[["", "(cue default)"], ["default", "then default"], ["hold", "then hold"], ["black", "then black"]].map(([v, l]) =>
    el("option", { value: v, selected: v === (a.after || "") }, l)));
  const loop = el("input", { type: "checkbox", class: "a-loop", checked: !!a.loop });
  const sync = () => { const needs = ["cue", "play"].includes(act.value); ref.disabled = !needs; after.disabled = !needs; loop.disabled = !needs; };
  act.addEventListener("change", sync); sync();
  return el("tr", {}, el("td", {}, tg), el("td", {}, act), el("td", {}, ref), el("td", {}, after),
    el("td", {}, el("label", { class: "check small" }, loop, "loop")),
    el("td", {}, el("button", { class: "icon danger", onclick: e => e.target.closest("tr").remove() }, "×")));
}

function gcueCard(num, c, isNew) {
  const rows = el("tbody", {}, ...(c.actions.length ? c.actions : [{}]).map(actionRow));
  const card = el("div", { class: "card", style: { background: "var(--panel-2)" } },
    el("div", { class: "row" },
      el("label", { class: "field" }, el("span", {}, "#"), el("input", { class: "g-num", value: num, disabled: !isNew, style: { width: "80px" } })),
      el("label", { class: "field", style: { flex: 1 } }, el("span", {}, "Label"), el("input", { class: "g-label", value: c.label || "" })),
      el("label", { class: "field" }, el("span", {}, "Colour"), el("input", { type: "color", class: "g-color", value: c.color || "#2a2f3a" }))),
    el("div", { class: "table-wrap", style: { marginTop: "8px" } }, el("table", { style: { minWidth: "640px" } },
      el("thead", {}, el("tr", {}, ...["Target", "Action", "Cue / media", "After", "", ""].map(h => el("th", {}, h)))), rows)),
    el("div", { class: "row", style: { marginTop: "8px" } },
      el("button", { onclick: () => rows.append(actionRow()) }, "+ Action"),
      el("span", { style: { flex: 1 } }),
      el("button", { class: "primary", onclick: async () => {
        const n = $(".g-num", card).value.trim();
        if (!n) return toast("Number required", "err");
        const actions = $$("tr", rows).map(r => ({ target: $(".a-target", r).value, action: $(".a-action", r).value,
          ref: $(".a-ref", r).value || null, after: $(".a-after", r).value || null, loop: $(".a-loop", r).checked || null }));
        const color = $(".g-color", card).value;
        await api("PUT", "/api/gcues/" + encodeURIComponent(n), { label: $(".g-label", card).value, color: color === "#2a2f3a" ? "" : color, actions });
        toast("Global cue " + n + " saved", "ok");
      } }, "Save"),
      el("button", { class: "danger", onclick: () => isNew ? card.remove() : confirmDo("Delete global cue " + num + "?", () => api("DELETE", "/api/gcues/" + encodeURIComponent(num))) }, "Delete")));
  return card;
}
function renderGcueEditor() {
  $("#gcue-editor").replaceChildren(...Object.entries(M.gcues).map(([n, c]) => gcueCard(n, c, false)));
}

/* ---------------- media ---------------- */
function renderMediaList() {
  let dl = $("#media-list-dl");
  if (!dl) { dl = el("datalist", { id: "media-list-dl" }); document.body.append(dl); }
  dl.replaceChildren(...M.media.map(m => el("option", { value: m.id }, m.name)));
  $("#t-media").replaceChildren(el("option", { value: "" }, "— media —"), ...M.media.map(m => el("option", { value: m.id }, m.name)));
  const f = ($("#lib-filter").value || "").toLowerCase();
  const list = M.media.filter(m => !f || m.name.toLowerCase().includes(f));
  $("#library").replaceChildren(...(list.length ? list.map(m => {
    const tinput = el("input", { value: (m.targets || ["all"]).join(", "), list: "target-list", style: { width: "100%" } });
    return el("div", { class: "media" },
      el("div", { class: "thumb", style: { backgroundImage: m.has_thumb ? `url("${withToken("/api/media/" + m.id + "/thumb")}")` : "" } },
        el("span", { class: "kind badge" }, m.kind), m.duration ? el("span", { class: "dur" }, fmtDur(m.duration)) : null),
      el("div", { class: "body" },
        el("div", { class: "name", title: m.original_filename }, m.name),
        el("div", { class: "small muted" }, [m.category === "event" ? "event" : "library", m.width ? m.width + "×" + m.height : "", fmtBytes(m.size)].filter(Boolean).join(" · ")),
        m.expires ? el("div", { class: "small expiry" }, "expires " + m.expires) : null,
        el("label", { class: "field" }, el("span", {}, "Targets"), tinput)),
      el("div", { class: "actions" },
        el("button", { onclick: async () => { await api("PATCH", "/api/media/" + m.id, { targets: tinput.value }); await api("POST", "/api/sync"); toast("Targets saved, syncing", "ok"); } }, "Save & sync"),
        el("button", { onclick: () => sendTarget("play", m.id, { after: "hold" }) , title: "Play on the target selected on the Screens tab" }, "Play"),
        el("button", { class: "danger", onclick: () => confirmDo(`Delete "${m.name}" from the master and all screens?`, () => api("DELETE", "/api/media/" + m.id)) }, "Delete")));
  }) : [el("div", { class: "muted small" }, "No fleet media yet.")]));
}

async function uploadFiles(files) {
  for (const f of files) {
    const bar = el("div", { class: "progress" }, el("div"));
    const pct = el("span", { class: "mono" }, "0%");
    const row = el("div", { class: "upload-row" }, el("span", {}, f.name), bar, pct);
    $("#uploads").append(row);
    const fd = new FormData();
    fd.append("file", f);
    fd.append("category", $("#up-category").value);
    fd.append("targets", $("#up-targets").value || "all");
    fd.append("expires", $("#up-expires").value);
    try {
      await upload("/api/media", fd, p => { bar.firstChild.style.width = (p * 100) + "%"; pct.textContent = Math.round(p * 100) + "%"; });
      pct.textContent = "✓"; setTimeout(() => row.remove(), 2500);
    } catch (e) { pct.textContent = "✗"; toast(f.name + ": " + e.message, "err"); }
  }
}

/* ---------------- shared cues ---------------- */
function sharedCueRow(num, c, isNew) {
  const items = Array.isArray(c.media) ? c.media : (c.media ? [c.media] : [""]);
  const sel = el("select", { class: "c-media", multiple: true, size: Math.min(4, Math.max(2, M.media.length)) },
    ...M.media.map(m => el("option", { value: m.id, selected: items.includes(m.id) }, m.name)));
  const tr = el("tr", {},
    el("td", {}, el("input", { class: "c-num", value: num, disabled: !isNew })),
    el("td", {}, el("input", { class: "c-label", value: c.label || "" })),
    el("td", {}, sel),
    el("td", {}, el("input", { type: "checkbox", class: "c-loop", checked: !!c.loop })),
    el("td", {}, el("input", { type: "number", class: "c-dur", value: c.duration || "", placeholder: "auto", style: { width: "80px" } })),
    el("td", {}, el("select", { class: "c-after" }, ...["default", "hold", "black"].map(v => el("option", { value: v, selected: c.after === v }, v)))),
    el("td", {}, el("div", { class: "row", style: { flexWrap: "nowrap" } },
      el("button", { class: "primary", onclick: async () => {
        const n = $(".c-num", tr).value.trim();
        const media = [...sel.selectedOptions].map(o => o.value);
        await api("PUT", "/api/cues/" + encodeURIComponent(n), { label: $(".c-label", tr).value, media: media.length > 1 ? media : media[0] || null,
          loop: $(".c-loop", tr).checked, duration: $(".c-dur", tr).value || null, after: $(".c-after", tr).value });
        toast("Saved — sync screens to push", "ok"); loadCues();
      } }, "Save"),
      el("button", { class: "danger icon", onclick: () => isNew ? tr.remove() : confirmDo("Delete cue " + num + "?", async () => { await api("DELETE", "/api/cues/" + encodeURIComponent(num)); loadCues(); }) }, "×"))));
  return tr;
}

/* ---------------- settings / docs ---------------- */
function renderSettings() {
  const c = M.settings.control;
  $("#m-token").value = c.api_token || "";
  $("#m-tcp").value = c.tcp_port; $("#m-udp").value = c.udp_port;
  const b = location.origin;
  $("#api-http").textContent = [
    `GET ${b}/api/gcue/10                       global cue`,
    `GET ${b}/api/target/all/cue/3`,
    `GET ${b}/api/target/group:east/cue/3?loop=1`,
    `GET ${b}/api/target/screen:<name>/play/<media>?after=hold`,
    `GET ${b}/api/target/all/default`,
    `GET ${b}/api/target/all/black`,
    `GET ${b}/api/screens                       fleet status`,
    `POST ${b}/api/media  (file, category, targets=all)`,
    `POST ${b}/api/sync   (target=all)`].join("\n");
  $("#api-tcp").textContent = [
    `# TCP ${location.hostname}:${c.tcp_port}`,
    `GCUE 10`, `CUE 3                (global cue 3 if defined, else ALL CUE 3)`,
    `ALL BLACK`, `GROUP east CUE 3 LOOP`, `SCREEN atrium PLAY "Welcome" HOLD`,
    `east DEFAULT         (bare group or screen name)`, `STATUS               (one line per screen)`,
    ``, `> OK target=group:east action=cue ok=3 failed=0`,
    `> EVENT screen=atrium online=1 mode=cue cue=3 media="Welcome" …`].join("\n");
}

/* ---------------- loading ---------------- */
async function loadScreens() {
  [M.screens, M.groups] = await Promise.all([api("GET", "/api/screens"), api("GET", "/api/groups")]);
  for (const id of [...M.selected]) if (!M.screens.find(s => s.id === id)) M.selected.delete(id);
  renderScreens(); renderTargets();
}
async function loadMedia() { M.media = await api("GET", "/api/media"); renderMediaList(); }
async function loadGcues() { M.gcues = await api("GET", "/api/gcues"); renderGcueButtons(); renderGcueEditor(); }
async function loadCues() { M.cues = await api("GET", "/api/cues"); $("#cue-rows").replaceChildren(...Object.entries(M.cues).map(([n, c]) => sharedCueRow(n, c, false))); }

async function init() {
  try {
    M.settings = await api("GET", "/api/settings");
    await Promise.all([loadScreens(), loadMedia()]);
    await Promise.all([loadGcues(), loadCues()]);
  } catch (e) { console.error(e); return; }
  renderSettings();
  const info = await api("GET", "/api/info");
  $("#disk").textContent = `Master library ${fmtBytes(info.disk.library_bytes)} · ${fmtBytes(info.disk.free_bytes)} free`;

  $("#t-cue-go").onclick = () => $("#t-cue").value.trim() && sendTarget("cue", $("#t-cue").value.trim());
  $("#t-cue").onkeydown = e => { if (e.key === "Enter") $("#t-cue-go").click(); };
  $("#t-play-go").onclick = () => $("#t-media").value && sendTarget("play", $("#t-media").value,
    { loop: $("#t-loop").checked, after: $("#t-hold").checked ? "hold" : "default" });
  $("#t-default").onclick = () => sendTarget("default");
  $("#t-black").onclick = () => sendTarget("black");
  $("#screen-add").onclick = () => editScreen(null);
  $("#sync-all").onclick = async () => { await api("POST", "/api/sync"); toast("Sync started", "ok"); };
  $("#gcue-add").onclick = () => {
    const nums = Object.keys(M.gcues).filter(n => /^\d+$/.test(n)).map(Number);
    $("#gcue-editor").prepend(gcueCard(String(nums.length ? Math.max(...nums) + 1 : 1), { actions: [] }, true));
  };
  $("#cue-add").onclick = () => {
    const nums = Object.keys(M.cues).filter(n => /^\d+$/.test(n)).map(Number);
    $("#cue-rows").append(sharedCueRow(String(nums.length ? Math.max(...nums) + 1 : 1), { after: "default" }, true));
  };
  $("#lib-filter").oninput = renderMediaList;
  makeDropZone($("#drop"), uploadFiles);
  $("#m-save").onclick = async () => {
    const tok = $("#m-token").value;
    M.settings = await api("PATCH", "/api/settings", { control: { api_token: tok, tcp_port: +$("#m-tcp").value, udp_port: +$("#m-udp").value } });
    setToken(tok); toast("Saved", "ok");
  };

  let pending = null;
  connectWS(msg => {
    if (["screens", "screen"].includes(msg.event)) {
      clearTimeout(pending); pending = setTimeout(loadScreens, 150);   // coalesce bursts
    }
    if (msg.event === "library") loadMedia();
    if (msg.event === "gcues") loadGcues();
  }, on => { $("#conn-dot").classList.toggle("on", on); $("#conn-text").textContent = on ? "live" : "reconnecting"; });
}

initTabs();
init();
