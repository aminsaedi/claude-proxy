/* =====================================================================
   console-dom.js — the rendering primitives the console is built on
   =====================================================================
   The console shows live data. The naive way to do that is to rebuild each
   card's HTML on every frame and assign it to innerHTML, and that is what this
   file exists to replace, because it has three failure modes you can feel:

     * every node is destroyed and recreated, so anything the browser was
       holding — focus, a text selection, a half-typed number — is thrown away
       mid-interaction;
     * CSS transitions restart from scratch, so bars re-animate and the page
       shimmers whenever anything anywhere changes;
     * the "did it change?" test compares rendered strings, and rendered
       strings contain countdowns, so a card is rebuilt every time a clock
       ticks even when nothing meaningful moved.

   So: build the DOM once, then write individual values into it, and only when
   the value actually differs. `txt`/`cls`/`sty`/`att` each remember what they
   last wrote and no-op otherwise; `list` reconciles a keyed collection by
   reusing nodes. An input the operator is currently typing into is never
   written to at all — see `txt`'s guard and `isEditing`.
   ===================================================================== */
"use strict";

// ============================ build ============================

/** el("div.card.wide", {onclick, title}, ...children) → HTMLElement
 *
 *  The tag accepts a compact `tag.class.class#id` form because the alternative
 *  — a className property on every single call — buries the structure being
 *  described under boilerplate.
 */
function el(spec, props, ...children) {
  // The props slot is optional: `el("div", childA, childB)` has to work, or
  // every call that happens not to need attributes silently loses its first
  // child — which is exactly the kind of failure that deletes half a view and
  // leaves no error behind. Anything that isn't a plain object is a child.
  if (props != null && (props instanceof Node || Array.isArray(props) ||
      typeof props !== "object")) {
    children.unshift(props);
    props = null;
  }
  const [head, ...classes] = String(spec).split(".");
  const [tag, id] = head.split("#");
  const node = document.createElementNS(
    tag === "svg" || tag === "path" || tag === "circle"
      ? "http://www.w3.org/2000/svg"
      : "http://www.w3.org/1999/xhtml",
    tag || "div");
  if (id) node.id = id;
  if (classes.length) node.setAttribute("class", classes.join(" "));
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (k === "html") node.innerHTML = v;
      else if (k === "text") node.textContent = v;
      else node.setAttribute(k, v === true ? "" : String(v));
    }
  }
  add(node, children);
  return node;
}

function add(node, children) {
  for (const c of children) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) add(node, c);
    else node.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

/** Empty a node without the innerHTML="" round-trip through the parser. */
function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  if (node.__keyed) node.__keyed.clear();
  return node;
}

// ============================ write ============================
//
// Each setter caches its last written value on the node. The cache is what
// makes these cheap enough to call unconditionally from a render pass: a frame
// where nothing changed touches the DOM zero times.

/** True while the operator is typing into this node (or a node inside it). */
function isEditing(node) {
  const a = document.activeElement;
  return !!a && (a === node || node.contains(a)) &&
    (a.tagName === "INPUT" || a.tagName === "TEXTAREA" || a.tagName === "SELECT");
}

function txt(node, value) {
  if (!node) return;
  const v = value == null ? "" : String(value);
  if (node.__t === v) return;
  node.__t = v;
  node.textContent = v;
}

/** Only for values that are genuinely markup (an icon, a badge). */
function html(node, value) {
  if (!node) return;
  const v = value == null ? "" : String(value);
  if (node.__h === v) return;
  node.__h = v;
  node.innerHTML = v;
}

function cls(node, name, on) {
  if (!node) return;
  const key = "__c" + name;
  const want = !!on;
  if (node[key] === want) return;
  node[key] = want;
  node.classList.toggle(name, want);
}

function sty(node, prop, value) {
  if (!node) return;
  const key = "__s" + prop;
  const v = value == null ? "" : String(value);
  if (node[key] === v) return;
  node[key] = v;
  node.style.setProperty(prop, v);
}

function att(node, name, value) {
  if (!node) return;
  const key = "__a" + name;
  const v = value == null ? null : String(value);
  if (node[key] === v) return;
  node[key] = v;
  if (v === null) node.removeAttribute(name);
  else node.setAttribute(name, v);
}

/** Write into an input, unless the operator is currently editing it. */
function val(node, value) {
  if (!node || isEditing(node)) return;
  const v = value == null ? "" : String(value);
  if (node.value === v) return;
  node.value = v;
}

// ============================ reconcile ============================

/** Keep `container`'s children in step with `items`, reusing nodes by key.
 *
 *  Nodes survive across frames, which is the whole point: a row that hasn't
 *  changed keeps its identity, its event listeners, its scroll position, and
 *  its place in any running transition.
 */
function list(container, items, { key, create, update }) {
  const kept = container.__keyed || (container.__keyed = new Map());
  const seen = new Set();
  items.forEach((item, i) => {
    const k = key(item);
    seen.add(k);
    let node = kept.get(k);
    if (!node) {
      node = create(item);
      kept.set(k, node);
    }
    update(node, item);
    // Walking left to right, the node for position i belongs at children[i];
    // inserting shifts the rest along, so this converges in one pass.
    if (container.children[i] !== node) {
      container.insertBefore(node, container.children[i] || null);
    }
  });
  for (const [k, node] of kept) {
    if (!seen.has(k)) { node.remove(); kept.delete(k); }
  }
}

/** Collect the [data-ref] descendants of a node into {name: element}.
 *
 *  Views build their DOM once and then need handles on the parts that change.
 *  Doing that with a querySelector per value per frame is wasteful and fragile;
 *  this walks the subtree once at build time.
 */
function refs(root) {
  const out = { root };
  for (const node of root.querySelectorAll("[data-ref]")) out[node.dataset.ref] = node;
  return out;
}

// ============================ formatting ============================

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function fmt(n) {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(Math.round(n));
}

// Money in cents so columns line up, but real-but-tiny spend reads as "<$0.01"
// rather than a $0.00 that looks like nothing happened.
function usd(n) {
  n = Number(n) || 0;
  if (n === 0) return "$0";
  const a = Math.abs(n);
  if (a >= 1000) return "$" + n.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (a >= 0.01) return "$" + n.toFixed(2);
  return "<$0.01";
}

function bytes(n) {
  n = Number(n) || 0;
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i === 0 ? Math.round(n) : n.toFixed(1)) + units[i];
}

function ms(v) {
  if (v == null) return "—";
  return v < 1000 ? Math.round(v) + "ms" : (v / 1000).toFixed(v < 10000 ? 2 : 1) + "s";
}

const pct = u => Math.round((parseFloat(u) || 0) * 100);

const tokTotal = m => (m.input_tokens || 0) + (m.output_tokens || 0) +
  (m.cache_read_input_tokens || 0) + (m.cache_creation_input_tokens || 0);

function fmtReset(ts) {
  if (!ts) return "—";
  const s = Math.max(0, parseInt(ts) - Date.now() / 1000);
  if (s < 60) return "<1m";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), d = Math.floor(h / 24);
  if (d >= 1) return `${d}d ${h % 24}h`;
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 45) return "just now";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}

const clock = ts => new Date(ts * 1000).toLocaleTimeString(undefined, { hour12: false });

// ============================ status vocabulary ============================
//
// Severity is never carried by colour alone: every call site pairs it with an
// icon and a word, because two of the four status steps sit below 3:1 on a
// light surface by design.

function sev(u) { u = parseFloat(u) || 0; return u >= 0.9 ? "crit" : u >= 0.7 ? "warn" : "good"; }
function sevMark(s) { return `var(--${s === "crit" ? "crit" : s === "warn" ? "warn" : s === "serious" ? "serious" : "good"})`; }
function sevInk(s) { return `var(--${s === "crit" ? "crit-ink" : s === "warn" ? "warn-ink" : s === "serious" ? "serious-ink" : "good-ink"})`; }

const ICON = {
  good: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
  warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  serious: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>',
  crit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
  brand: '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="6"/></svg>',
  neutral: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><circle cx="12" cy="12" r="9"/></svg>',
};

const badgeHTML = (s, text) => `<span class="badge ${s}">${ICON[s] || ""}${esc(text)}</span>`;

function healthLabel(h) {
  if (!h || h.last_checked === 0) return { sev: "neutral", text: "unchecked" };
  const st = h.status || (h.healthy ? "healthy" : "unhealthy");
  if (st === "healthy") return { sev: "good", text: "healthy" };
  if (st === "rate_limited") return { sev: "warn", text: "rate-limited" };
  return { sev: "crit", text: `unhealthy · ${h.error_count} err` };
}

const SVG = {
  eye: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>',
  edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
  trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2m1 0-1 14H7L6 6"/></svg>',
  rotate: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/></svg>',
  rename: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.6 13.4 12 22l-9-9V4h9z"/><circle cx="7.5" cy="7.5" r="1.6"/></svg>',
  list: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/></svg>',
};

// Fixed categorical slots for per-model breakdowns — assigned in order and
// never cycled, so a model keeps its colour when the list is filtered.
const SERIES = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)"];

// ============================ clipboard ============================
// navigator.clipboard only exists in a secure context, which a plain-http
// tailnet URL is not; and the execCommand fallback needs its temp field inside
// the open <dialog>, since showModal() makes the rest of the document inert.
async function copyText(text, host) {
  try {
    if (window.isSecureContext && navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) { /* fall through to the legacy path */ }
  const root = host || document.querySelector("dialog[open]") || document.body;
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:0;left:0;width:1px;height:1px;padding:0;border:0;opacity:0";
    root.appendChild(ta);
    ta.focus(); ta.select();
    try { ta.setSelectionRange(0, text.length); } catch (e) { /* older browsers */ }
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  } catch (e) { return false; }
}

// ============================ dialogs ============================

function makeDialog(inner) {
  const d = document.createElement("dialog");
  d.className = "dlg";
  d.innerHTML = inner;
  document.body.appendChild(d);
  d.addEventListener("click", e => { if (e.target === d) d.close(); });
  d.addEventListener("close", () => d.remove(), { once: true });
  return d;
}

function confirmDialog({ title, message = "", confirmLabel = "Confirm", danger = false }) {
  return new Promise(resolve => {
    const d = makeDialog(`<div class="dlg-head"><h3>${esc(title)}</h3>${message ? `<p>${message}</p>` : ""}</div>
      <div class="dlg-foot"><button class="btn ghost" data-x type="button">Cancel</button>
      <button class="btn ${danger ? "danger" : "primary"}" data-ok type="button">${esc(confirmLabel)}</button></div>`);
    let ok = false;
    d.querySelector("[data-ok]").onclick = () => { ok = true; d.close(); resolve(true); };
    d.querySelector("[data-x]").onclick = () => d.close();
    d.addEventListener("close", () => { if (!ok) resolve(false); }, { once: true });
    d.showModal();
    d.querySelector("[data-ok]").focus();
  });
}

function formDialog({ title, desc = "", fields, submitLabel = "Save" }) {
  return new Promise(resolve => {
    const body = fields.map(f => {
      if (f.type === "checkbox")
        return `<label class="chk"><input type="checkbox" name="${f.name}" ${f.value ? "checked" : ""}> ${esc(f.label)}</label>`;
      const ctl = f.type === "textarea"
        ? `<textarea name="${f.name}" placeholder="${esc(f.placeholder || "")}" spellcheck="false">${esc(f.value || "")}</textarea>`
        : `<input type="text" name="${f.name}" value="${esc(f.value || "")}" placeholder="${esc(f.placeholder || "")}" autocomplete="off" spellcheck="false">`;
      return `<div class="field"><label>${esc(f.label)}</label>${ctl}${f.hint ? `<div class="fh">${esc(f.hint)}</div>` : ""}</div>`;
    }).join("");
    const d = makeDialog(`<form><div class="dlg-head"><h3>${esc(title)}</h3>${desc ? `<p>${esc(desc)}</p>` : ""}</div>
      <div class="dlg-body">${body}</div>
      <div class="dlg-foot"><button type="button" class="btn ghost" data-x>Cancel</button>
      <button type="submit" class="btn primary">${esc(submitLabel)}</button></div></form>`);
    const form = d.querySelector("form");
    let done = false;
    form.addEventListener("submit", e => {
      e.preventDefault();
      const data = {};
      fields.forEach(f => {
        const node = form.elements[f.name];
        data[f.name] = f.type === "checkbox" ? node.checked : node.value.trim();
      });
      done = true; d.close(); resolve(data);
    });
    d.querySelector("[data-x]").onclick = () => d.close();
    d.addEventListener("close", () => { if (!done) resolve(null); }, { once: true });
    d.showModal();
    const first = form.querySelector("input,textarea");
    if (first) first.focus();
  });
}

function revealDialog({ title, label, value, note = "" }) {
  const d = makeDialog(`<div class="dlg-head"><h3>${esc(title)}</h3>${note ? `<p>${esc(note)}</p>` : ""}</div>
    <div class="dlg-body"><div class="field"><label>${esc(label)}</label>
      <div class="reveal-box"><input type="text" readonly value="${esc(value)}"><button type="button" class="btn" data-copy>Copy</button></div>
    </div></div>
    <div class="dlg-foot"><button type="button" class="btn primary" data-x>Done</button></div>`);
  const input = d.querySelector("input");
  d.querySelector("[data-copy]").onclick = async () => {
    const ok = await copyText(value, d);   // from inside this dialog: top layer, not inert
    if (!ok) {
      input.focus(); input.select();
      try { input.setSelectionRange(0, value.length); } catch (e) { /* ignore */ }
    }
    toast(ok ? "ok" : "err", ok ? "Copied to clipboard" : "Couldn't copy — text is selected, press ⌘/Ctrl-C");
  };
  d.querySelector("[data-x]").onclick = () => d.close();
  d.showModal();
  input.focus(); input.select();
}

// ============================ toasts ============================

function toast(kind, msg) {
  const colour = kind === "ok" ? "var(--good-ink)" : kind === "err" ? "var(--crit-ink)" : "var(--brand-ink)";
  const node = el("div.toast." + kind, { role: "status" },
    el("span", { html: ICON[kind === "ok" ? "good" : kind === "err" ? "crit" : "brand"], style: `color:${colour}` }),
    el("span", { text: msg }));
  document.getElementById("toasts").appendChild(node);
  setTimeout(() => {
    node.style.transition = "opacity .3s";
    node.style.opacity = "0";
    setTimeout(() => node.remove(), 300);
  }, 3600);
}

// ============================ api ============================

async function api(method, path, body) {
  const opt = { method, headers: {}, cache: "no-store" };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  let d = {};
  try { d = await r.json(); } catch (e) { /* empty body */ }
  if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
  return d;
}

const enc = encodeURIComponent;
