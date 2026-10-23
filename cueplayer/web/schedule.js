/* Schedule tab: playlist editor and week calendar. Uses S, api, el, $, $$ from common.js/player.js. */
"use strict";

const SC = { week: null, playlists: {}, entries: {}, occ: [], plSel: null, plItems: [] };
const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const pad2 = n => String(n).padStart(2, "0");
const isoDate = d => `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
const isoDT = d => `${isoDate(d)}T${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
const parseDT = s => { const [a, b = "00:00"] = s.split("T"); const [y, m, d] = a.split("-").map(Number);
  const [h, mi] = b.split(":").map(Number); return new Date(y, m - 1, d, h || 0, mi || 0); };
const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x; };
const mondayOf = d => { const x = new Date(d.getFullYear(), d.getMonth(), d.getDate()); x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); return x; };
const fmtTime = d => `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
/* The unit's clock (its timezone), not the browser's */
function unitNow() {
  const s = S.status && S.status.schedule && S.status.schedule.now;
  return s ? parseDT(s.slice(0, 16)) : new Date();
}

async function loadSchedule() {
  if (!SC.week) SC.week = mondayOf(unitNow());
  const from = SC.week, to = addDays(SC.week, 7);
  try {
    const [pls, ents, occ, now] = await Promise.all([
      api("GET", "/api/playlists"), api("GET", "/api/schedule"),
      api("GET", `/api/schedule/occurrences?start=${isoDT(from)}&end=${isoDT(to)}`), api("GET", "/api/schedule/now")]);
    Object.assign(SC, { playlists: pls, entries: ents, occ, now });
  } catch (e) { console.error(e); return; }
  renderPlaylists();
  renderCalendar();
  renderUpcoming();
}

/* ---------------- playlists ---------------- */
function mediaName(id) { return (S.media.find(m => m.id === id) || {}).name || "(missing)"; }
function mediaKind(id) { return (S.media.find(m => m.id === id) || {}).kind || ""; }

function renderPlaylists() {
  const list = Object.values(SC.playlists);
  $("#pl-list").replaceChildren(...(list.length ? list.map(p => {
    const secs = p.items.reduce((a, i) => a + (i.duration || (S.media.find(m => m.id === i.media) || {}).duration || 0), 0);
    return el("div", { class: "pl-row" + (SC.plSel === p.id ? " active" : ""), onclick: () => openPlaylist(p.id) },
      el("span", { class: "name" }, p.name),
      el("span", { class: "small muted" }, `${p.items.length} item${p.items.length === 1 ? "" : "s"}` + (secs ? " · " + fmtDur(secs) : "")),
      p.shuffle ? el("span", { class: "chip" }, "shuffle") : null);
  }) : [el("div", { class: "small muted" }, "No playlists yet.")]));
}

function openPlaylist(id) {
  SC.plSel = id;
  const p = id ? SC.playlists[id] : { name: "", items: [], shuffle: false };
  SC.plItems = p.items.map(i => ({ ...i }));
  $("#pl-name").value = p.name;
  $("#pl-shuffle").checked = !!p.shuffle;
  $("#pl-delete").classList.toggle("hidden", !id);
  $("#pl-play").classList.toggle("hidden", !id);
  $("#pl-editor").classList.remove("hidden");
  $("#pl-add").replaceChildren(...mediaOptions(""));
  renderPlItems();
  renderPlaylists();
  $("#pl-name").focus();
}

function renderPlItems() {
  const box = $("#pl-items");
  box.replaceChildren(...SC.plItems.map((it, i) => el("div", { class: "item" },
    el("span", { class: "mono small muted" }, i + 1),
    el("span", { class: "name", title: mediaName(it.media) }, mediaName(it.media)),
    el("span", { class: "badge" }, mediaKind(it.media) || "?"),
    mediaKind(it.media) === "image" ? el("input", { type: "number", min: 1, step: 1, value: it.duration || "", placeholder: "s",
      title: "Seconds on screen", style: { width: "56px" }, oninput: e => { it.duration = Number(e.target.value) || null; } }) : null,
    el("button", { class: "icon", disabled: i === 0, onclick: () => { SC.plItems.splice(i - 1, 0, SC.plItems.splice(i, 1)[0]); renderPlItems(); } }, "↑"),
    el("button", { class: "icon", disabled: i === SC.plItems.length - 1, onclick: () => { SC.plItems.splice(i + 1, 0, SC.plItems.splice(i, 1)[0]); renderPlItems(); } }, "↓"),
    el("button", { class: "icon danger", onclick: () => { SC.plItems.splice(i, 1); renderPlItems(); } }, "×"))));
  if (!SC.plItems.length) box.append(el("div", { class: "small muted" }, "Add media below."));
}

async function savePlaylist() {
  const body = { name: $("#pl-name").value.trim(), shuffle: $("#pl-shuffle").checked, items: SC.plItems };
  const p = SC.plSel ? await api("PUT", "/api/playlists/" + SC.plSel, body) : await api("POST", "/api/playlists", body);
  SC.plSel = p.id;
  toast("Playlist saved", "ok");
  await loadSchedule();
  openPlaylist(p.id);
}

/* ---------------- calendar ---------------- */
function renderCalendar() {
  const cal = $("#cal");
  const now = unitNow();
  const days = [...Array(7)].map((_, i) => addDays(SC.week, i));
  const sameDay = (a, b) => isoDate(a) === isoDate(b);
  $("#cal-label").textContent = `${days[0].toLocaleDateString(undefined, { day: "numeric", month: "short" })} – ` +
    days[6].toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  const sch = SC.now || {};
  $("#cal-now").textContent = `Unit time ${now.toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit", day: "numeric", month: "short" })} (${sch.timezone || "system"})` +
    (sch.active ? ` · On now: ${sch.active.title} until ${sch.active.occ_end.slice(11)}` : " · Nothing scheduled now: Normal playlist");

  const cells = [el("div", { class: "head" }, "")];
  days.forEach((d, i) => cells.push(el("div", { class: "head" + (sameDay(d, now) ? " today" : "") }, `${DAY_NAMES[i]} ${d.getDate()}`)));
  const gutter = el("div", { class: "gutter" }, ...[...Array(24)].map((_, h) =>
    h ? el("span", { style: { top: `calc(var(--hour) * ${h})` } }, `${pad2(h)}:00`) : null));
  cells.push(gutter);
  for (const d of days) {
    const col = el("div", { class: "day" + (sameDay(d, now) ? " today" : "") });
    col.addEventListener("click", e => {
      if (e.target !== col) return;
      const hourPx = col.getBoundingClientRect().height / 24;
      const mins = Math.max(0, Math.min(23 * 60 + 30, Math.floor((e.offsetY / hourPx) * 2) * 30));
      const start = new Date(d.getFullYear(), d.getMonth(), d.getDate(), Math.floor(mins / 60), mins % 60);
      editEntry(null, start, new Date(start.getTime() + 60 * 60000));
    });
    // occurrences overlapping this day, clipped to it
    const d0 = d, d1 = addDays(d, 1);
    for (const o of SC.occ) {
      const s = parseDT(o.occ_start), f = parseDT(o.occ_end);
      if (!(s < d1 && f > d0)) continue;
      const a = Math.max(s, d0), b = Math.min(f, d1);
      const top = (a - d0) / 864e5 * 100, h = Math.max(1.2, (b - a) / 864e5 * 100);
      const plName = o.playlist === "normal" ? "Normal playlist" : o.playlist === "black" ? "Black" : (SC.playlists[o.playlist] || {}).name || "?";
      const on = sch.active && sch.active.id === o.id && sch.active.occ_start === o.occ_start;
      col.append(el("div", { class: "blk" + (on ? " on" : ""), title: `${o.title}\n${fmtTime(s)}–${fmtTime(f)} · ${plName}` +
          (o.repeat !== "none" ? ` · repeats ${o.repeat}` : "") + (o.takeover ? " · takes over" : ""),
        style: { top: top + "%", height: h + "%", background: o.color || "" },
        onclick: () => editEntry(SC.entries[o.id] ? { ...SC.entries[o.id], id: o.id } : null) },
        el("b", {}, o.title), el("span", {}, `${fmtTime(s)}–${fmtTime(f)} · ${plName}`)));
    }
    if (sameDay(d, now)) col.append(el("div", { class: "nowline", style: { top: ((now - d0) / 864e5 * 100) + "%" } }));
    cells.push(col);
  }
  const scroll = cal.scrollTop;
  cal.replaceChildren(...cells);
  cal.scrollTop = scroll || cal.scrollHeight * (7 / 24);   // open at about 7 am
}

function renderUpcoming() {
  const up = (SC.now && SC.now.upcoming) || [];
  $("#cal-upcoming").replaceChildren(...(up.length ? up.map(o => el("div", { class: "row", style: { gap: "10px" } },
    el("span", { class: "mono" }, o.occ_start.replace("T", " ")), el("b", {}, o.title),
    el("span", { class: "muted" }, "→ " + o.occ_end.slice(11) + (o.repeat !== "none" ? ` · ${o.repeat}` : "")))) :
    [el("div", { class: "muted" }, "Nothing scheduled in the next two weeks.")]));
}

function editEntry(e, start, end) {
  const isNew = !e;
  e = e || { title: "", playlist: Object.keys(SC.playlists)[0] || "normal", start: isoDT(start), end: isoDT(end),
    repeat: "none", days: [], until: "", color: "", takeover: false, enabled: true };
  const s = parseDT(e.start), f = parseDT(e.end);
  const plOpts = [...Object.values(SC.playlists).map(p => [p.id, p.name]), ["normal", "Normal playlist"], ["black", "Black"]];
  const daysBox = el("div", { class: "days-pick" }, ...DAY_NAMES.map((n, i) =>
    el("label", {}, el("input", { type: "checkbox", value: i, checked: (e.days || []).includes(i) }), n)));
  const d = el("dialog", {},
    el("h2", {}, isNew ? "New schedule entry" : "Edit schedule entry"),
    el("form", { method: "dialog", class: "grid" },
      el("label", { class: "field" }, el("span", {}, "Title"), el("input", { name: "title", value: e.title, placeholder: "e.g. Gala evening" })),
      el("label", { class: "field" }, el("span", {}, "Playlist"),
        el("select", { name: "playlist" }, ...plOpts.map(([v, l]) => el("option", { value: v, selected: v === e.playlist }, l)))),
      el("div", { class: "row" },
        el("label", { class: "field" }, el("span", {}, "Date"), el("input", { type: "date", name: "date", value: isoDate(s), required: true })),
        el("label", { class: "field" }, el("span", {}, "From"), el("input", { type: "time", name: "from", value: fmtTime(s), required: true })),
        el("label", { class: "field" }, el("span", {}, "To"), el("input", { type: "time", name: "to", value: fmtTime(f), required: true }))),
      el("div", { class: "small muted" }, "An end time earlier than the start runs past midnight."),
      el("div", { class: "row" },
        el("label", { class: "field" }, el("span", {}, "Repeat"),
          el("select", { name: "repeat" }, ...[["none", "Once"], ["daily", "Every day"], ["weekly", "Weekly on…"]].map(([v, l]) =>
            el("option", { value: v, selected: v === e.repeat }, l)))),
        el("label", { class: "field" }, el("span", {}, "Until (optional)"), el("input", { type: "date", name: "until", value: e.until || "" })),
        el("label", { class: "field" }, el("span", {}, "Colour"), el("input", { type: "color", name: "color", value: e.color || "#2563eb" }))),
      daysBox,
      el("label", { class: "check" }, el("input", { type: "checkbox", name: "takeover", checked: !!e.takeover }),
        "Take over when it starts (interrupts event cues; never a locked scenario)"),
      el("label", { class: "check" }, el("input", { type: "checkbox", name: "enabled", checked: e.enabled !== false }), "Enabled"),
      el("div", { class: "row" },
        el("button", { class: "primary", value: "save" }, "Save"), el("button", { value: "cancel", formnovalidate: true }, "Cancel"),
        isNew ? null : el("button", { class: "danger", value: "delete", formnovalidate: true }, "Delete"))));
  const f2 = d.querySelector("form");
  const syncDays = () => daysBox.classList.toggle("hidden", f2.repeat.value !== "weekly");
  f2.repeat.addEventListener("change", syncDays); syncDays();
  d.addEventListener("close", async () => {
    try {
      if (d.returnValue === "save") {
        const st = parseDT(`${f2.date.value}T${f2.from.value}`);
        let en = parseDT(`${f2.date.value}T${f2.to.value}`);
        if (en <= st) en = addDays(en, 1);
        const body = { title: f2.title.value, playlist: f2.playlist.value, start: isoDT(st), end: isoDT(en),
          repeat: f2.repeat.value, days: $$("input:checked", daysBox).map(c => Number(c.value)), until: f2.until.value || null,
          color: f2.color.value, takeover: f2.takeover.checked, enabled: f2.enabled.checked };
        await api(isNew ? "POST" : "PUT", isNew ? "/api/schedule" : "/api/schedule/" + e.id, body);
        toast("Schedule saved", "ok");
      } else if (d.returnValue === "delete" && confirm(`Delete "${e.title}"?`)) {
        await api("DELETE", "/api/schedule/" + e.id);
      }
    } finally { d.remove(); loadSchedule(); }
  });
  document.body.append(d);
  d.showModal();
}

/* ---------------- wiring ---------------- */
$("#pl-new").onclick = () => openPlaylist(null);
$("#pl-add-btn").onclick = () => { const v = $("#pl-add").value; if (v) { SC.plItems.push({ media: v, duration: null }); renderPlItems(); } };
$("#pl-save").onclick = savePlaylist;
$("#pl-play").onclick = () => SC.plSel && api("POST", `/api/playlist/${SC.plSel}/play`).then(() => toast("Playing " + $("#pl-name").value, "ok"));
$("#pl-delete").onclick = () => SC.plSel && confirmDo(`Delete playlist "${$("#pl-name").value}"?`, async () => {
  await api("DELETE", "/api/playlists/" + SC.plSel);
  SC.plSel = null; $("#pl-editor").classList.add("hidden"); loadSchedule();
});
$("#cal-prev").onclick = () => { SC.week = addDays(SC.week, -7); loadSchedule(); };
$("#cal-next").onclick = () => { SC.week = addDays(SC.week, 7); loadSchedule(); };
$("#cal-today").onclick = () => { SC.week = mondayOf(unitNow()); loadSchedule(); };
$("#cal-new").onclick = () => { const s = unitNow(); s.setMinutes(0, 0, 0); s.setHours(s.getHours() + 1); editEntry(null, s, new Date(s.getTime() + 3600e3)); };
setInterval(() => { if ($("#tab-schedule").classList.contains("active") && !document.hidden) loadSchedule(); }, 60000);
