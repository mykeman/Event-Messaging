/* Shared helpers for the player and master GUIs. No external dependencies. */
"use strict";

const TOKEN_KEY = "cueplayer.token";

function getToken() { try { return localStorage.getItem(TOKEN_KEY) || ""; } catch { return ""; } }
function setToken(t) { try { localStorage.setItem(TOKEN_KEY, t); } catch {} }

function withToken(url) {
  const t = getToken();
  if (!t) return url;
  return url + (url.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(t);
}

let authPromptOpen = false;
function promptToken() {
  if (authPromptOpen) return;
  authPromptOpen = true;
  const d = el("dialog", {},
    el("h2", {}, "API token required"),
    el("p", { class: "muted" }, "This unit is protected by an API token. Enter it to continue."),
    el("form", { method: "dialog", class: "row" },
      el("input", { id: "tokinput", type: "password", placeholder: "token", style: "flex:1" }),
      el("button", { class: "primary" }, "Save")));
  d.addEventListener("close", () => {
    setToken(d.querySelector("#tokinput").value.trim());
    location.reload();
  });
  document.body.append(d);
  d.showModal();
}

async function api(method, path, body, opts = {}) {
  const headers = {};
  const t = getToken();
  if (t) headers["Authorization"] = "Bearer " + t;
  let payload;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  const r = await fetch(path, { method, headers, body: payload });
  if (r.status === 401) { promptToken(); throw new Error("unauthorised"); }
  if (r.status === 204) return null;
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) {
    const msg = (data && data.detail) ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : r.statusText;
    if (!opts.quiet) toast(msg, "err");
    throw new Error(msg);
  }
  return data;
}

/* Upload with progress (fetch has no upload progress) */
function upload(path, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const x = new XMLHttpRequest();
    x.open("POST", path);
    const t = getToken();
    if (t) x.setRequestHeader("Authorization", "Bearer " + t);
    x.upload.onprogress = e => e.lengthComputable && onProgress && onProgress(e.loaded / e.total);
    x.onload = () => {
      let data = null;
      try { data = JSON.parse(x.responseText); } catch {}
      if (x.status === 401) { promptToken(); return reject(new Error("unauthorised")); }
      x.status < 300 ? resolve(data) : reject(new Error((data && data.detail) || x.statusText));
    };
    x.onerror = () => reject(new Error("network error"));
    x.send(formData);
  });
}

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (k === "style" && typeof v === "object") Object.assign(e.style, v);
    else if (k === "dataset") Object.assign(e.dataset, v);
    else if (k in e && typeof v !== "string") e[k] = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    e.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return e;
}
const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];

function toast(msg, kind = "") {
  let box = $("#toasts");
  if (!box) { box = el("div", { id: "toasts" }); document.body.append(box); }
  const t = el("div", { class: "toast " + kind }, msg);
  box.append(t);
  setTimeout(() => t.remove(), kind === "err" ? 6000 : 3000);
}

function fmtDur(s) {
  if (s === null || s === undefined || isNaN(s)) return "";
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(sec).padStart(2, "0");
}
function fmtBytes(b) {
  if (!b) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(b) / Math.log(1024)));
  return (b / Math.pow(1024, i)).toFixed(i ? 1 : 0) + " " + u[i];
}
function ago(t) {
  if (!t) return "never";
  const s = Date.now() / 1000 - t;
  if (s < 5) return "just now";
  if (s < 60) return Math.round(s) + "s ago";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  return Math.round(s / 3600) + "h ago";
}

/* Hash-based tabs */
function initTabs(onChange) {
  const show = () => {
    const name = (location.hash || "").slice(1) || $$("nav a")[0].getAttribute("href").slice(1);
    $$("nav a").forEach(a => a.classList.toggle("active", a.getAttribute("href") === "#" + name));
    $$("section.tab").forEach(s => s.classList.toggle("active", s.id === "tab-" + name));
    onChange && onChange(name);
  };
  window.addEventListener("hashchange", show);
  show();
}

/* Websocket with auto-reconnect */
function connectWS(onMsg, onState) {
  let ws, retry = 1000;
  const open = () => {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(withToken(proto + "//" + location.host + "/api/ws"));
    ws.onopen = () => { retry = 1000; onState && onState(true); };
    ws.onmessage = e => { try { onMsg(JSON.parse(e.data)); } catch (err) { console.error(err); } };
    ws.onclose = e => {
      onState && onState(false);
      if (e.code === 4401) { promptToken(); return; }
      setTimeout(open, retry); retry = Math.min(retry * 2, 10000);
    };
  };
  open();
}

function confirmDo(msg, fn) { if (confirm(msg)) fn(); }

/* Drag & drop + file picker */
function makeDropZone(zone, onFiles) {
  const input = el("input", { type: "file", multiple: true, accept: "video/*,image/*", class: "hidden" });
  zone.append(input);
  zone.addEventListener("click", e => { if (e.target === zone || zone.contains(e.target) && e.target.tagName !== "INPUT") input.click(); });
  input.addEventListener("change", () => { onFiles([...input.files]); input.value = ""; });
  zone.addEventListener("dragover", e => { e.preventDefault(); zone.classList.add("over"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("over"));
  zone.addEventListener("drop", e => { e.preventDefault(); zone.classList.remove("over"); onFiles([...e.dataTransfer.files]); });
}

function badge(mode) { return el("span", { class: "badge " + (mode || "") }, mode || "—"); }

function inputVal(e) {
  if (e.type === "checkbox") return e.checked;
  if (e.type === "number") return e.value === "" ? 0 : Number(e.value);
  return e.value;
}
