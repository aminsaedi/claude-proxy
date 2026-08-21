/* =====================================================================
   claude-proxy console
   =====================================================================
   Two data channels, deliberately different:

   * /events (SSE) carries the dashboard state and only pushes when something
     actually changed, so an idle console never re-renders and never loses your
     scroll position, an open panel, or a half-typed form.
   * /requests is polled, but only while the Requests tab is on screen and Live
     is armed. The audit log is far too chatty to belong in the state snapshot,
     and paying for it when nobody is looking would be silly.

   Rendering is keyed-patch throughout: nodes are reused and only rewritten when
   their content changed, which is what keeps focus and selection intact under a
   live feed.
   ===================================================================== */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const enc = encodeURIComponent;

// ============================ formatting ============================
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
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
const tokTotal = m => (m.input_tokens || 0) + (m.output_tokens || 0) + (m.cache_read_input_tokens || 0) + (m.cache_creation_input_tokens || 0);

function fmtReset(ts) {
  if (!ts) return "—";
  const s = Math.max(0, parseInt(ts) - Date.now() / 1000);
  if (s < 60) return "<1m";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), d = Math.floor(h / 24);
  if (d >= 1) return `${d}d ${h % 24}h`;
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}
function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return Math.round(s) + "s";
  if (s < 3600) return Math.round(s / 60) + "m";
  if (s < 86400) return Math.round(s / 3600) + "h";
  return Math.round(s / 86400) + "d";
}
const clock = ts => new Date(ts * 1000).toLocaleTimeString(undefined, { hour12: false });

// ============================ status vocabulary ============================
// Severity is never carried by colour alone: every call site pairs it with an
// icon and a word. Two of the four status steps sit below 3:1 on a light
// surface by design, and this pairing is the mitigation.
function sev(u) { u = parseFloat(u) || 0; return u >= 0.9 ? "crit" : u >= 0.7 ? "warn" : "good"; }
function sevMark(s) { return `var(--${s === "crit" ? "crit" : s === "warn" ? "warn" : s === "serious" ? "serious" : "good"})`; }
function sevInk(s) { return `var(--${s === "crit" ? "crit-ink" : s === "warn" ? "warn-ink" : s === "serious" ? "serious-ink" : "good-ink"})`; }
function healthLabel(h) {
  if (!h || h.last_checked === 0) return { sev: "neutral", text: "unchecked" };
  const st = h.status || (h.healthy ? "healthy" : "unhealthy");
  if (st === "healthy") return { sev: "good", text: "healthy" };
  if (st === "rate_limited") return { sev: "warn", text: "rate-limited" };
  return { sev: "crit", text: `unhealthy · ${h.error_count} err` };
}
const ICON = {
  good: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
  warn: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>',
  serious: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 9v4M12 17h.01"/><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/></svg>',
  crit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
  brand: '<svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="6"/></svg>',
  neutral: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><circle cx="12" cy="12" r="9"/></svg>',
};
const badge = (s, text) => `<span class="badge ${s}">${ICON[s] || ""}${esc(text)}</span>`;

const SVG = {
  eye: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>',
  edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
  trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2m1 0-1 14H7L6 6"/></svg>',
  rotate: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 3v6h-6"/></svg>',
  rename: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.6 13.4 12 22l-9-9V4h9z"/><circle cx="7.5" cy="7.5" r="1.6"/></svg>',
  gauge: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 17a9 9 0 1 1 18 0"/><path d="m12 17 4.5-5"/></svg>',
};

// Fixed categorical slots for the per-model breakdown — assigned in order and
// never cycled, so a model keeps its colour when the list is filtered.
const SERIES = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)"];

// ============================ clipboard ============================
// navigator.clipboard only exists in a secure context, which a plain-http
// tailnet URL is not; and the execCommand fallback needs its temp field inside
// the open <dialog>, since showModal() makes the rest of the document inert.
async function copyText(text, host) {
  try { if (window.isSecureContext && navigator.clipboard) { await navigator.clipboard.writeText(text); return true; } } catch (e) { /* fall through */ }
  const root = host || document.querySelector("dialog[open]") || document.body;
  try {
    const ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", "");
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
  d.className = "dlg"; d.innerHTML = inner;
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
    d.showModal(); d.querySelector("[data-ok]").focus();
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
        const el = form.elements[f.name];
        data[f.name] = f.type === "checkbox" ? el.checked : el.value.trim();
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
    if (!ok) { input.focus(); input.select(); try { input.setSelectionRange(0, value.length); } catch (e) { /* ignore */ } }
    toast(ok ? "ok" : "err", ok ? "Copied to clipboard" : "Couldn't copy — text is selected, press ⌘/Ctrl-C");
  };
  d.querySelector("[data-x]").onclick = () => d.close();
  d.showModal(); input.focus(); input.select();
}

// ============================ toasts ============================
function toast(kind, msg) {
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.setAttribute("role", "status");
  const colour = kind === "ok" ? "var(--good-ink)" : kind === "err" ? "var(--crit-ink)" : "var(--brand-ink)";
  t.innerHTML = `<span style="color:${colour}">${ICON[kind === "ok" ? "good" : kind === "err" ? "crit" : "brand"]}</span><span></span>`;
  t.querySelector("span:last-child").textContent = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => { t.style.transition = "opacity .3s"; t.style.opacity = "0"; setTimeout(() => t.remove(), 300); }, 3600);
}

// ============================ api ============================
async function api(method, path, body) {
  const opt = { method, headers: {}, cache: "no-store" };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let d = {};
  try { d = await r.json(); } catch (e) { /* empty body */ }
  if (!r.ok) throw new Error(d.error || ("HTTP " + r.status));
  return d;
}

// ============================ shared state ============================
let state = null;
let cfgDirty = false;      // never clobber a form the operator is editing
let lastCfgJSON = null;
let lastBeat = 0;
let es = null;
let currentView = "overview";
const CIRC = 2 * Math.PI * 62 * 0.5;   // semicircle arc length, r=62

// ============================ chart tooltip ============================
// An HTML chart is interactive by default (interaction.md): every bar gets a
// hover readout, with a hit target the full column height rather than the mark.
const tip = () => $("#tip");
function showTip(ev, title, rows) {
  const el = tip();
  el.innerHTML = `<div class="tt">${esc(title)}</div>` +
    rows.map(([k, v]) => `<div class="tr"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
  el.classList.add("show");
  el.setAttribute("aria-hidden", "false");
  const r = el.getBoundingClientRect();
  const x = Math.min(window.innerWidth - r.width - 10, Math.max(8, ev.clientX + 12));
  const y = Math.max(8, ev.clientY - r.height - 12);
  el.style.left = x + "px";
  el.style.top = y + "px";
}
function hideTip() { tip().classList.remove("show"); tip().setAttribute("aria-hidden", "true"); }

/* Bar chart of one measure over time. One series, so no legend — the section
   title names the measure. 4px rounded tops on the baseline, 2px gaps. */
function barChart(points, { valueKey = "value", labelKey = "label", format = usd, sub = () => "" }) {
  if (!points.length) return `<div class="empty">No data yet.</div>`;
  const max = Math.max(...points.map(p => p[valueKey] || 0), 0);
  const bars = points.map((p, i) => {
    const v = p[valueKey] || 0;
    const h = max > 0 ? Math.max(2, Math.round(v / max * 100)) : 0;
    const last = i === points.length - 1;
    return `<span class="b ${v ? "" : "zero"} ${last ? "today" : ""}" data-i="${i}"><i style="height:${h}%"></i></span>`;
  }).join("");
  return `<div class="chart">
    <div class="chart-head"><span class="t">peak ${esc(format(max))}</span><span class="z">${esc(sub())}</span></div>
    <div class="bars" data-chart>${bars}</div>
    <div class="chart-axis"><span>${esc(points[0][labelKey])}</span><span>${esc(points[points.length - 1][labelKey])}</span></div>
  </div>`;
}
function bindChart(root, points, render) {
  const bars = root.querySelector("[data-chart]");
  if (!bars) return;
  bars.addEventListener("mousemove", e => {
    const b = e.target.closest("[data-i]");
    if (!b) return hideTip();
    const p = points[+b.dataset.i];
    if (p) showTip(e, ...render(p));
  });
  bars.addEventListener("mouseleave", hideTip);
}

// =====================================================================
// OVERVIEW — hero gauge
// =====================================================================
function renderHero() {
  const hero = $("#hero");
  const active = state.active;
  const h = (state.headers || {})[active] || {};
  const hh = (state.health || {})[active];
  const u5 = h["anthropic-ratelimit-unified-5h-utilization"];
  const has = u5 !== undefined;
  const p = pct(u5);
  const s = has ? sev(u5) : "neutral";
  const hl = healthLabel(hh);
  const frac = Math.min(1, parseFloat(u5) || 0);   // clamp the sweep, show the true %
  const off = CIRC * (1 - frac);
  const u7 = h["anthropic-ratelimit-unified-7d-utilization"];

  hero.classList.remove("stale");
  hero.innerHTML = `
    <div class="gauge" role="meter" aria-label="Active token 5-hour capacity used"
         aria-valuenow="${p}" aria-valuemin="0" aria-valuemax="100">
      <svg viewBox="0 0 140 92" aria-hidden="true">
        <path class="track" d="M8 84 A62 62 0 0 1 132 84"/>
        <path class="fill" d="M8 84 A62 62 0 0 1 132 84"
              stroke="${has ? sevMark(s) : "var(--surface-3)"}"
              stroke-dasharray="${CIRC}" stroke-dashoffset="${has ? off : CIRC}"/>
      </svg>
      <div class="center">
        <div class="val">${has ? p : "—"}<span class="pct">${has ? "%" : ""}</span></div>
        <div class="cap">5h capacity used</div>
      </div>
    </div>
    <div class="hero-meta">
      <div class="eyebrow">Active upstream</div>
      <div class="hero-token">
        <span class="name">${esc(active || "—")}</span>
        ${badge("brand", "live")}
        ${badge(hl.sev, hl.text)}
      </div>
      <div class="hero-sub">${heroSub(has, p, s)}</div>
      <div class="hero-stats">
        <div class="hero-stat"><div class="k">7-day used</div>
          <div class="v" style="color:${u7 !== undefined ? sevInk(sev(u7)) : "var(--ink)"}">${u7 !== undefined ? pct(u7) + "%" : "—"}</div></div>
        <div class="hero-stat"><div class="k">5h resets in</div>
          <div class="v" data-reset="${h["anthropic-ratelimit-unified-5h-reset"] || ""}">${fmtReset(h["anthropic-ratelimit-unified-5h-reset"])}</div></div>
        <div class="hero-stat"><div class="k">Upstreams</div><div class="v">${state.tokens.length}</div></div>
        <div class="hero-stat"><div class="k">Uptime</div><div class="v">${state.started_at ? ago(state.started_at) : "—"}</div></div>
      </div>
    </div>`;
}
function heroSub(has, p, s) {
  if (!has) return "No rate-limit data yet — send a request or run a probe.";
  const rem = Math.max(0, 100 - p);
  if (s === "crit") return `Only <b style="color:var(--crit-ink)">${rem}%</b> of the 5-hour window left — near or over the limit.`;
  if (s === "warn") return `<b style="color:var(--warn-ink)">${rem}%</b> of the 5-hour window remaining.`;
  return `<b style="color:var(--good-ink)">${rem}%</b> of the 5-hour window remaining — healthy headroom.`;
}

// =====================================================================
// OVERVIEW — KPI tiles
// =====================================================================
function sumUsage() {
  let req = 0, cr = 0, cost = 0, activeClients = 0;
  const win = { "1d": 0, "3d": 0, "7d": 0, "30d": 0 };
  for (const vk of state.virtual_keys || []) {
    let kr = 0;
    for (const m of Object.values(vk.usage || {})) {
      req += m.requests || 0;
      cr += m.cache_read_input_tokens || 0;
      cost += m.cost_usd || 0;
      kr += m.requests || 0;
    }
    for (const k in win) win[k] += (vk.windows || {})[k]?.cost_usd || 0;
    if (kr > 0) activeClients++;
  }
  return { req, cr, cost, win, activeClients };
}
function renderTiles() {
  const t = sumUsage();
  const tz = state.timezone || "local";
  const o = auditOverview || {};
  // Rate over *forwarded* requests: a rejected key never reached upstream, so
  // counting it as a failure of the upstream would be misleading in both
  // directions — it inflates the rate, and a flood of them would drown out a
  // real upstream problem.
  const bad = (o.errors || 0) + (o.blocked || 0);
  const errRate = o.forwarded ? (o.errors || 0) / o.forwarded : 0;
  const errSev = errRate >= 0.1 ? "crit" : errRate >= 0.02 ? "warn" : "good";
  const rejected = o.rejected || 0;
  const tiles = [
    { k: "Spend today", v: usd(t.win["1d"]), sub: tz, cls: "accent" },
    { k: "Spend 7d", v: usd(t.win["7d"]), sub: "7 calendar days", cls: "accent" },
    { k: "Spend all-time", v: usd(t.cost), sub: `${fmt(t.req)} requests` },
    { k: "Requests 24h", v: fmt(o.forwarded || 0), sub: `${fmt(o.tokens || 0)} tokens` },
    {
      k: "Error rate 24h",
      v: o.forwarded ? (errRate * 100).toFixed(1) + "%" : "—",
      // Kept short: this line is one tile wide and ellipsises if it runs long.
      sub: bad || rejected
        ? [o.errors && `${fmt(o.errors)} failed`, o.blocked && `${fmt(o.blocked)} blocked`,
           rejected && `${fmt(rejected)} rejected`].filter(Boolean).join(" · ")
        : "no failures",
      ink: o.forwarded ? sevInk(errSev) : null,
    },
    { k: "Active clients", v: String(t.activeClients), sub: `of ${(state.virtual_keys || []).length} keys` },
  ];
  $("#tiles").innerHTML = tiles.map(x => `<div class="tile ${x.cls || ""}">
    <div class="k">${esc(x.k)}</div>
    <div class="v"${x.ink ? ` style="color:${x.ink}"` : ""}>${esc(x.v)}</div>
    <div class="sub">${esc(x.sub)}</div></div>`).join("");
}

// =====================================================================
// OVERVIEW — spend chart, latency, model table
// =====================================================================
function renderSpendChart() {
  // One series across all clients: the fleet's daily spend.
  const byDate = new Map();
  for (const vk of state.virtual_keys || []) {
    for (const d of vk.daily || []) {
      const cur = byDate.get(d.date) || { date: d.date, cost_usd: 0, requests: 0, tokens: 0 };
      cur.cost_usd += d.cost_usd || 0;
      cur.requests += d.requests || 0;
      cur.tokens += tokTotal(d);
      byDate.set(d.date, cur);
    }
  }
  const points = Array.from(byDate.values()).sort((a, b) => a.date.localeCompare(b.date))
    .map(d => ({ ...d, value: d.cost_usd, label: d.date }));
  const box = $("#spendChart");
  $("#spendRange").textContent = points.length ? `${points.length} days` : "";
  box.innerHTML = barChart(points, { sub: () => state.timezone || "" });
  bindChart(box, points, p => [p.date, [
    ["Spend", usd(p.cost_usd)], ["Requests", fmt(p.requests)], ["Tokens", fmt(p.tokens)],
  ]]);
}

function renderLatency() {
  const o = auditOverview;
  const box = $("#latencyPanel");
  if (!o || !o.forwarded) {
    box.innerHTML = `<div class="empty">No requests forwarded upstream in the last 24h.</div>`;
    return;
  }
  // Percentiles, not averages: latency is long-tailed, and the mean describes
  // nobody's experience of it.
  const rows = [
    ["Time to first byte", o.ttfb_p50, o.ttfb_p95],
    ["Full response", o.latency_p50, o.latency_p95],
  ];
  const rejectedNote = o.rejected
    ? `<div class="subtle" style="margin-top:8px">${fmt(o.rejected)} request${o.rejected > 1 ? "s were" : " was"}
       rejected on an unknown key and never forwarded — excluded from these figures.</div>`
    : "";
  box.innerHTML = `<table class="tbl">
    <thead><tr><th>Last 24h</th><th class="r">p50</th><th class="r">p95</th></tr></thead>
    <tbody>${rows.map(([label, p50, p95]) => `<tr>
      <td class="name">${esc(label)}</td>
      <td class="strong mono">${esc(ms(p50))}</td>
      <td class="strong mono">${esc(ms(p95))}</td></tr>`).join("")}</tbody>
  </table>
  <div class="subtle" style="margin-top:12px">
    Over the ${esc(fmt(o.forwarded || 0))} request${o.forwarded === 1 ? "" : "s"} actually forwarded upstream.
    Time to first byte is the part the proxy and upstream control; the full
    response also covers however long the completion took to generate.
  </div>${rejectedNote}`;
}

function renderModels() {
  const rows = auditModels || [];
  const box = $("#modelPanel");
  if (!rows.length) {
    box.innerHTML = `<div class="empty">No requests recorded in the last 24h.</div>`;
    return;
  }
  const max = Math.max(...rows.map(r => r.cost_usd || 0), 1e-9);
  box.innerHTML = `<table class="tbl">
    <thead><tr><th>Model</th><th class="r">Share</th><th class="r">Requests</th>
      <th class="r">Cost</th><th class="r">Avg TTFB</th><th class="r">Avg total</th></tr></thead>
    <tbody>${rows.map((r, i) => {
      const share = Math.round((r.cost_usd || 0) / max * 100);
      const colour = SERIES[i % SERIES.length];
      return `<tr>
        <td class="name"><span class="swatch" style="display:inline-block;background:${colour}"></span> ${esc(r.model || "—")}</td>
        <td class="r" style="width:120px">
          <span class="track thin" style="display:block"><span class="bar" style="width:${share}%;background:${colour}"></span></span>
        </td>
        <td class="r mono">${esc(fmt(r.requests))}</td>
        <td class="r strong mono">${esc(usd(r.cost_usd))}</td>
        <td class="r mono">${esc(ms(r.avg_ttfb_ms))}</td>
        <td class="r mono">${esc(ms(r.avg_latency_ms))}</td>
      </tr>`;
    }).join("")}</tbody>
  </table>`;
}

// =====================================================================
// UPSTREAMS — token cards
// =====================================================================
const tokRefs = new Map();
function meterHTML(h, period, big) {
  const u = h[`anthropic-ratelimit-unified-${period}-utilization`];
  if (u === undefined) return "";
  const p = pct(u), s = sev(u);
  const reset = h[`anthropic-ratelimit-unified-${period}-reset`] || "";
  return `<div class="meter">
    <div class="meter-row">
      <span class="period">${period}</span>
      <span class="pct" style="color:${sevInk(s)}">${p}%</span>
      <span class="reset" data-reset="${esc(reset)}">resets ${fmtReset(reset)}</span>
    </div>
    <div class="track ${big ? "" : "thin"}"><div class="bar" style="width:${Math.min(100, p)}%;background:${sevMark(s)}"></div></div>
  </div>`;
}
const META_KEYS = ["anthropic-ratelimit-unified-status", "anthropic-ratelimit-unified-overage-status", "anthropic-ratelimit-unified-fallback"];
function tokenCardHTML(name) {
  const h = (state.headers || {})[name] || {};
  const hl = healthLabel((state.health || {})[name]);
  const isActive = name === state.active;
  const isDefault = name === state.default_token;
  const hasData = Object.keys(h).length > 0;
  const preview = (state.token_previews || {})[name] || "";
  const rawRows = Object.entries(h).sort((a, b) => a[0].localeCompare(b[0]))
    .map(([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("");
  const meta = META_KEYS.filter(k => h[k] !== undefined)
    .map(k => `<span class="chip">${esc(k.replace("anthropic-ratelimit-unified-", ""))}: ${esc(h[k])}</span>`).join(" ");
  return `
    <div class="tok-top">
      <span class="tok-name">${esc(name)}</span>
      ${isActive ? badge("brand", "active") : ""}
      ${isDefault ? badge("neutral", "default") : ""}
      ${badge(hl.sev, hl.text)}
    </div>
    ${preview ? `<div class="tok-preview" title="Masked — use Reveal for the full token">${esc(preview)}</div>` : ""}
    ${hasData ? meterHTML(h, "5h", true) + meterHTML(h, "7d", false)
      : `<div class="subtle" style="padding:6px 0">No rate-limit data yet.</div>`}
    ${meta ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:4px">${meta}</div>` : ""}
    <div class="tok-actions">
      <button class="btn primary act-select" ${isActive ? "disabled" : ""}>${isActive ? "Serving traffic" : "Set active"}</button>
      <button class="btn ghost act-probe">Test</button>
      <span class="grow"></span>
      <button class="btn mini act-reveal" title="Reveal token" aria-label="Reveal token">${SVG.eye}</button>
      <button class="btn mini act-edit" title="Edit / rotate token" aria-label="Edit token">${SVG.edit}</button>
      <button class="btn mini del act-delete" title="Delete token" aria-label="Delete token">${SVG.trash}</button>
    </div>
    ${hasData ? `<details class="raw"><summary>Diagnostics</summary><table class="rawtable">${rawRows}</table></details>` : ""}`;
}
function renderTokens() {
  const box = $("#tokens");
  $("#tokCount").textContent = String(state.tokens.length);
  const seen = new Set();
  state.tokens.forEach(name => {
    seen.add(name);
    let card = tokRefs.get(name);
    if (!card) {
      card = document.createElement("div");
      card.className = "tok";
      tokRefs.set(name, card);
      box.appendChild(card);
    }
    card.classList.toggle("is-active", name === state.active);
    const html = tokenCardHTML(name);
    if (card._html === html) return;   // unchanged: keep focus and open panels
    const wasOpen = card.querySelector("details.raw")?.open;
    card.innerHTML = html; card._html = html;
    if (wasOpen) { const d = card.querySelector("details.raw"); if (d) d.open = true; }
    card.querySelector(".act-select").onclick = () => selectToken(name);
    card.querySelector(".act-probe").onclick = e => probeToken(name, e.currentTarget);
    card.querySelector(".act-reveal").onclick = () => revealToken(name);
    card.querySelector(".act-edit").onclick = () => editToken(name);
    card.querySelector(".act-delete").onclick = () => deleteToken(name);
  });
  for (const [name, card] of tokRefs) {
    if (!seen.has(name)) { card.remove(); tokRefs.delete(name); }
  }
}

// =====================================================================
// CLIENTS
// =====================================================================
const clientOpen = new Set();
const clientRefs = new Map();
const clientScope = new Map();
const SCOPES = [["1d", "Today"], ["7d", "7 days"], ["30d", "30 days"], ["all", "All-time"]];
const PERIOD_LABEL = { hour: "per hour", day: "per day", week: "per week", month: "per month" };

function limitChip(limits) {
  if (!limits || !limits.length) return "";
  const w = limits[0];   // the server sorts tightest-first
  const s = w.over ? "crit" : w.ratio >= 0.8 ? "warn" : "good";
  const text = w.over
    ? `over ${usd(w.limit_usd)} ${PERIOD_LABEL[w.period]}`
    : `${usd(w.spent_usd)} / ${usd(w.limit_usd)} ${PERIOD_LABEL[w.period]}`;
  return badge(s, text);
}
function winsHTML(w) {
  return `<div class="wins">` + [["1d", "Today"], ["3d", "3 days"], ["7d", "7 days"], ["30d", "30 days"]].map(([k, label], i) => {
    const d = w[k] || {};
    return `<div class="win ${i === 0 ? "lead" : ""}">
      <div class="k">${label}</div>
      <div class="v">${usd(d.cost_usd)}</div>
      <div class="sub">${fmt(d.requests)} req · ${fmt(tokTotal(d))} tok</div>
    </div>`;
  }).join("") + `</div>`;
}
function capsHTML(limits) {
  const body = (limits && limits.length) ? limits.map(l => {
    const s = l.over ? "crit" : l.ratio >= 0.8 ? "warn" : "good";
    return `<div class="cap">
      <div class="cap-row">
        <span class="period">${esc(PERIOD_LABEL[l.period] || l.period)}</span>
        <span class="amt" style="color:${sevInk(s)}">${usd(l.spent_usd)}</span>
        <span style="color:var(--muted)">of ${usd(l.limit_usd)}</span>
        <span class="reset" data-reset="${l.resets_at || ""}">resets ${fmtReset(l.resets_at)}</span>
      </div>
      <div class="track thin"><div class="bar" style="width:${Math.min(100, Math.round(l.ratio * 100))}%;background:${sevMark(s)}"></div></div>
    </div>`;
  }).join("") : `<div class="subtle" style="font-style:italic">No spend limits — this client can spend without a cap.</div>`;
  return `<div class="caps"><div class="eyebrow" style="margin-bottom:8px">Spend limits</div>${body}</div>`;
}
function clientInnerHTML(t, max) {
  const scope = clientScope.get(t.name) || "1d";
  const models = Object.entries(scope === "all" ? t.lifetime : ((t.windows[scope] || {}).models || {}))
    .sort((a, b) => (b[1].cost_usd || 0) - (a[1].cost_usd || 0) || tokTotal(b[1]) - tokTotal(a[1]));
  const rows = models.map(([mn, m], i) => `<tr>
    <td class="name"><span class="swatch" style="display:inline-block;background:${SERIES[i % SERIES.length]}"></span> ${esc(mn)}</td>
    <td class="strong">${usd(m.cost_usd)}</td><td>${fmt(m.requests)}</td><td>${fmt(m.input_tokens)}</td>
    <td>${fmt(m.output_tokens)}</td><td>${fmt(m.cache_read_input_tokens)}</td><td>${fmt(m.cache_creation_input_tokens)}</td>
  </tr>`).join("");
  const share = Math.round(t.rank / max * 100);
  const daily = (t.daily || []).map(d => ({ ...d, value: d.cost_usd, label: d.date }));
  return `<button class="client-head" aria-expanded="${clientOpen.has(t.name)}" style="--sh:${share}%"
      title="${share}% of the busiest client's recent spend">
      <span class="client-id">
        <svg class="caret" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>
        <span class="name">${esc(t.name)}</span>
        ${limitChip(t.limits)}
      </span>
      <span class="client-nums">
        <span class="cn"><div class="v">${usd(t.today)}</div><div class="k">today</div></span>
        <span class="cn"><div class="v">${usd(t.week)}</div><div class="k">7d</div></span>
        <span class="cn"><div class="v">${fmt(t.req)}</div><div class="k">req</div></span>
      </span>
    </button>
    <div class="client-body">
      ${winsHTML(t.windows)}
      ${daily.length ? `<div class="eyebrow" style="margin-top:16px">Daily spend</div>${barChart(daily, { sub: () => state.timezone || "" })}` : ""}
      ${capsHTML(t.limits)}
      <div class="client-actions">
        <button class="btn ck-limits" type="button">${SVG.gauge} Set limits</button>
        <button class="btn ck-requests" type="button">${SVG.eye} View requests</button>
        <button class="btn ck-reveal" type="button">${SVG.eye} Reveal key</button>
        <button class="btn ck-rotate" type="button">${SVG.rotate} Rotate</button>
        <button class="btn ck-rename" type="button">${SVG.rename} Rename</button>
        <button class="btn danger ck-delete" type="button">${SVG.trash} Delete</button>
        <span class="ckey" title="Masked — use Reveal for the full key">${esc(t.preview || "vk-…")}</span>
      </div>
      <div class="seg" role="group" aria-label="Model breakdown period" style="margin:14px 0 8px">
        ${SCOPES.map(([k, label]) => `<button type="button" data-scope="${k}" aria-pressed="${scope === k}">${label}</button>`).join("")}
      </div>
      ${models.length ? `<div class="tbl-scroll"><table class="tbl"><thead><tr><th>Model</th><th class="r">Cost</th><th class="r">Req</th><th class="r">Input</th><th class="r">Output</th><th class="r">Cache rd</th><th class="r">Cache wr</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<div class="empty">No usage in this period.</div>`}
    </div>`;
}
function bindClient(el, nm, daily) {
  el.querySelector(".client-head").onclick = () => {
    if (clientOpen.has(nm)) clientOpen.delete(nm); else clientOpen.add(nm);
    el.classList.toggle("open");
    el.querySelector(".client-head").setAttribute("aria-expanded", clientOpen.has(nm));
  };
  $$("[data-scope]", el).forEach(b => b.addEventListener("click", () => {
    clientScope.set(nm, b.dataset.scope);
    renderClients();
  }));
  bindChart(el, daily, p => [p.date, [["Spend", usd(p.cost_usd)], ["Requests", fmt(p.requests)]]]);
  el.querySelector(".ck-limits")?.addEventListener("click", () => editLimits(nm));
  el.querySelector(".ck-requests")?.addEventListener("click", () => { filters.key = nm; $("#fKey").value = nm; go("requests"); loadRequests(true); });
  el.querySelector(".ck-reveal")?.addEventListener("click", () => revealKey(nm));
  el.querySelector(".ck-rotate")?.addEventListener("click", () => rotateKey(nm));
  el.querySelector(".ck-rename")?.addEventListener("click", () => renameKey(nm));
  el.querySelector(".ck-delete")?.addEventListener("click", () => deleteKey(nm));
}
function renderClients() {
  const box = $("#clients");
  const vks = state.virtual_keys || [];
  $("#clientCount").textContent = String(vks.length);
  const totals = vks.map(vk => {
    const windows = vk.windows || {};
    const lifetime = vk.usage || {};
    let req = 0, cost = 0;
    for (const m of Object.values(lifetime)) { req += m.requests || 0; cost += m.cost_usd || 0; }
    const today = windows["1d"]?.cost_usd || 0;
    const week = windows["7d"]?.cost_usd || 0;
    return {
      name: vk.name, preview: vk.preview || "", limits: vk.limits || [], daily: vk.daily || [],
      windows, lifetime, req, cost, today, week,
      rank: week || cost,   // rank by recent spend — the number an operator acts on
    };
  }).sort((a, b) => b.rank - a.rank || b.cost - a.cost);
  if (!totals.length) {
    box.innerHTML = `<div class="card"><div class="empty">No clients configured.</div></div>`;
    clientRefs.clear();
    return;
  }
  const max = Math.max(1e-9, ...totals.map(t => t.rank));
  const seen = new Set();
  totals.forEach(t => {
    seen.add(t.name);
    let el = clientRefs.get(t.name);
    if (!el) {
      el = document.createElement("div");
      el.className = "client";
      el.dataset.name = t.name;
      clientRefs.set(t.name, el);
    }
    const html = clientInnerHTML(t, max);
    if (el._html !== html) {
      el.innerHTML = html; el._html = html;
      bindClient(el, t.name, (t.daily || []).map(d => ({ ...d, value: d.cost_usd, label: d.date })));
    }
    el.classList.toggle("open", clientOpen.has(t.name));
    box.appendChild(el);   // reorder into sorted position; a no-op if already there
  });
  for (const [name, el] of clientRefs) {
    if (!seen.has(name)) { el.remove(); clientRefs.delete(name); }
  }
  const live = new Set(clientRefs.values());
  Array.from(box.children).forEach(c => { if (!live.has(c)) c.remove(); });
}

// =====================================================================
// REQUESTS — the audit log
// =====================================================================
const filters = { q: "", key: "", outcome: "", hours: "24" };
let rows = [];              // newest first
let liveTail = true;
let maxId = 0, minId = 0;
let tailTimer = null;
let selectedId = null;
let auditOverview = null;
let auditModels = null;

function outcomeBadge(r) {
  if (r.outcome === "blocked") return badge("warn", "over budget");
  if (r.outcome === "rejected") return badge("crit", "rejected");
  if (r.outcome === "error" || r.status >= 400) return badge("crit", String(r.status || "error"));
  if (r.streamed) return badge("good", "stream");
  return badge("good", "ok");
}
function reqRowHTML(r) {
  const tokens = (r.input_tokens || 0) + (r.output_tokens || 0) + (r.cache_read_input_tokens || 0) + (r.cache_creation_input_tokens || 0);
  const summary = r.summary || (r.outcome === "rejected" ? "(unauthenticated request)" : r.path || "");
  return `<td class="when" title="${esc(new Date(r.ts * 1000).toLocaleString())}">${esc(clock(r.ts))}</td>
    <td>${outcomeBadge(r)}</td>
    <td class="sum" title="${esc(summary)}">${esc(summary)}</td>
    <td>${esc(r.key_name || "—")}</td>
    <td class="num" title="${esc(r.model || "")}">${esc((r.model || "—").replace(/^claude-/, ""))}</td>
    <td class="num r">${esc(fmt(tokens))}</td>
    <td class="num r">${esc(usd(r.cost_usd))}</td>
    <td class="num r" title="TTFB ${esc(ms(r.ttfb_ms))}">${esc(ms(r.latency_ms))}</td>`;
}
function renderRequests(newIds) {
  const body = $("#reqBody");
  const empty = $("#reqEmpty");
  $("#reqCount").textContent = rows.length ? `${rows.length} shown` : "";
  $("#navReqCount").textContent = auditOverview?.requests ? fmt(auditOverview.requests) : "";
  if (!rows.length) {
    body.innerHTML = "";
    empty.hidden = false;
    empty.textContent = state?.audit?.mode === "off"
      ? "Request auditing is turned off — enable it under Settings."
      : "No requests match these filters.";
    return;
  }
  empty.hidden = true;
  // Keyed patch by row id, so a live tail prepends without redrawing (and
  // un-selecting) everything already on screen.
  const existing = new Map(Array.from(body.children).map(tr => [+tr.dataset.id, tr]));
  const frag = document.createDocumentFragment();
  for (const r of rows) {
    let tr = existing.get(r.id);
    if (!tr) {
      tr = document.createElement("tr");
      tr.dataset.id = r.id;
      tr.innerHTML = reqRowHTML(r);
      tr.onclick = () => openRequest(r.id);
      if (newIds && newIds.has(r.id)) tr.classList.add("newrow");
    } else {
      existing.delete(r.id);
    }
    tr.setAttribute("aria-selected", String(r.id === selectedId));
    frag.appendChild(tr);
  }
  for (const tr of existing.values()) tr.remove();
  body.appendChild(frag);
  $("#moreBtn").hidden = rows.length < 25;
}

function queryString(extra) {
  const p = new URLSearchParams();
  if (filters.q) p.set("q", filters.q);
  if (filters.key) p.set("key", filters.key);
  if (filters.outcome) p.set("outcome", filters.outcome);
  if (filters.hours) p.set("since", String(Date.now() / 1000 - Number(filters.hours) * 3600));
  p.set("limit", "100");
  for (const k in extra || {}) p.set(k, extra[k]);
  return p.toString();
}
async function loadRequests(reset) {
  if (reset) { rows = []; maxId = 0; minId = 0; }
  try {
    const d = await api("GET", "/requests?" + queryString(minId ? { before_id: minId } : {}));
    const fresh = d.requests || [];
    rows = reset ? fresh : rows.concat(fresh);
    if (rows.length) {
      maxId = Math.max(maxId, ...rows.map(r => r.id));
      minId = Math.min(...rows.map(r => r.id));
    }
    renderRequests();
  } catch (err) {
    toast("err", "Couldn't load requests: " + err.message);
  }
}
async function tailRequests() {
  if (!liveTail || currentView !== "requests" || document.hidden) return;
  try {
    const d = await api("GET", "/requests?" + queryString({ after_id: String(maxId), limit: "50" }));
    const fresh = d.requests || [];
    if (!fresh.length) return;
    const ids = new Set(fresh.map(r => r.id));
    rows = fresh.concat(rows.filter(r => !ids.has(r.id)));
    maxId = Math.max(maxId, ...fresh.map(r => r.id));
    if (!minId) minId = Math.min(...rows.map(r => r.id));
    renderRequests(ids);
  } catch (err) { /* a failed tail poll just retries on the next tick */ }
}
function startTail() {
  stopTail();
  tailTimer = setInterval(tailRequests, 2500);
}
function stopTail() { if (tailTimer) { clearInterval(tailTimer); tailTimer = null; } }

// ---- request detail drawer ----
function turn(role, text, cls) {
  if (!text) return "";
  return `<div class="turn ${cls || role}">
    <div class="turn-head">${esc(role)}<span class="len">${esc(fmt(text.length))} chars</span></div>
    <div class="turn-body">${esc(text)}</div></div>`;
}
function blockText(content) {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return content == null ? "" : JSON.stringify(content, null, 2);
  return content.map(b => {
    if (typeof b === "string") return b;
    if (!b || typeof b !== "object") return "";
    if (typeof b.text === "string") return b.text;
    if (b.type === "thinking") return "[thinking]\n" + (b.thinking || "");
    if (b.type === "tool_use") return `[tool_use: ${b.name}]\n` + JSON.stringify(b.input, null, 2);
    if (b.type === "tool_result") return `[tool_result${b.is_error ? " (error)" : ""}]\n` + blockText(b.content);
    if (b.type === "image") return "[image]";
    if (b.type === "document") return "[document]";
    return JSON.stringify(b);
  }).filter(Boolean).join("\n\n");
}
function renderDetail(d) {
  const req = d.request;
  const meta = [
    ["Time", new Date(d.ts * 1000).toLocaleString()],
    ["Client", d.key_name || "—"],
    ["Model", d.model || "—"],
    ["Status", String(d.status || "—")],
    ["Outcome", d.outcome || "—"],
    ["Upstream", d.token_name || "—"],
    ["Attempts", String(d.attempts || 1)],
    ["TTFB", ms(d.ttfb_ms)],
    ["Total", ms(d.latency_ms)],
    ["Input", fmt(d.input_tokens)],
    ["Output", fmt(d.output_tokens)],
    ["Cache read", fmt(d.cache_read_input_tokens)],
    ["Cache write", fmt(d.cache_creation_input_tokens)],
    ["Cost", usd(d.cost_usd)],
    ["Client IP", d.client_ip || "—"],
    ["Request ID", d.request_id || "—"],
  ];
  let body = `<div class="kv">${meta.map(([k, v]) =>
    `<div><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join("")}</div>`;

  if (d.error) body += `<div class="turn"><div class="turn-head" style="color:var(--crit-ink)">Error</div><div class="turn-body">${esc(d.error)}</div></div>`;

  if (req && typeof req === "object") {
    const params = ["max_tokens", "temperature", "top_p", "top_k", "stream", "stop_sequences"]
      .filter(k => req[k] !== undefined)
      .map(k => `<span class="chip">${esc(k)}: ${esc(JSON.stringify(req[k]))}</span>`).join(" ");
    if (params) body += `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px">${params}</div>`;
    if (req.system) body += turn("system", blockText(req.system), "system");
    if (Array.isArray(req.tools) && req.tools.length) {
      body += turn(`tools (${req.tools.length})`,
        req.tools.map(t => `${t.name}: ${t.description || ""}`).join("\n\n"), "tools");
    }
    for (const m of (req.messages || [])) {
      body += turn(m.role, blockText(m.content), m.role === "user" ? "user" : "assistant");
    }
  } else if (req) {
    body += turn("request", String(req), "user");
  } else {
    body += `<div class="subtle">No request body stored${state?.audit?.mode === "meta" ? " — auditing is in metadata-only mode." : "."}</div>`;
  }

  if (d.response) body += turn("response", d.response, "assistant");
  if (d.truncated) body += `<div class="subtle" style="margin-top:8px">⚠ Bodies were truncated to the configured per-request cap.</div>`;
  return body;
}
async function openRequest(id) {
  selectedId = id;
  renderRequests();
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#drawerScrim").classList.add("open");
  $("#drawerBody").innerHTML = `<div class="skl" style="height:120px"></div>`;
  try {
    const d = await api("GET", "/requests/" + id);
    $("#drawerTitle").textContent = `${d.model || d.path || "Request"} · ${clock(d.ts)}`;
    $("#drawerBody").innerHTML = renderDetail(d);
    $("#drawerCopy").onclick = async () => {
      const ok = await copyText(JSON.stringify(d, null, 2), document.body);
      toast(ok ? "ok" : "err", ok ? "Request JSON copied" : "Couldn't copy");
    };
  } catch (err) {
    $("#drawerBody").innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}
function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#drawerScrim").classList.remove("open");
  selectedId = null;
  renderRequests();
}

async function loadAuditSummary() {
  try {
    const [o, m] = await Promise.all([
      api("GET", "/audit/overview?hours=24"),
      api("GET", "/audit/models?hours=24"),
    ]);
    auditOverview = o;
    auditModels = m.models || [];
    if (state) { renderTiles(); renderLatency(); renderModels(); }
  } catch (err) { /* the overview degrades to "no data" on its own */ }
}

// =====================================================================
// SETTINGS
// =====================================================================
const CFG = [
  { g: "Auto-rotation", rows: [
    ["auto_rotation.enabled", "bool", "Enabled", "Switch tokens on high 5h utilization or when the active token is dead/rate-limited"],
    ["auto_rotation.threshold_5h", "num", "5h threshold", "Rotate when the active token's 5h use ≥ this (0–1)"],
    ["auto_rotation.target_max_util_5h", "num", "Target max util", "On high-util rotation, only switch to tokens below this"],
    ["auto_rotation.check_interval_seconds", "num", "Check interval", "Seconds between evaluations"],
    ["auto_rotation.probe_before_switch", "bool", "Probe before switch", "Health-check a candidate before switching to it"],
    ["auto_rotation.cooldown_seconds", "num", "Cooldown", "Min seconds between util-based rotations"],
    ["auto_rotation.notify_only", "bool", "Notify only", "Log rotations without actually switching"],
  ] },
  { g: "Request auditing", rows: [
    ["audit.mode", "select:off,meta,full", "Capture mode", "off = nothing · meta = one row per request, no prompt text · full = prompts and completions too"],
    ["audit.retention_days", "num", "Keep for (days)", "Records older than this are deleted on the next sweep"],
    ["audit.max_gb", "num", "Storage cap (GB)", "If the audit DB exceeds this, the oldest records are dropped until it fits"],
    ["audit.max_body_kb", "num", "Per-body cap (KB)", "Prompts and responses are truncated past this, so one huge context can't eat the budget"],
  ] },
  { g: "Probes & timeouts", rows: [
    ["health_probe_interval_seconds", "num", "Health probe interval", "Re-probe unhealthy tokens (seconds)"],
    ["active_probe_interval_seconds", "num", "Active probe interval", "Validate the active token (seconds)"],
    ["upstream_timeout_seconds", "num", "Upstream timeout", "HTTP timeout for API calls (restart to apply)"],
  ] },
  { g: "Cost & limits", rows: [
    ["timezone", "text", "Timezone", "IANA name. Sets the day/week/month boundaries for spend windows and limits"],
    ["pricing.online", "bool", "Fetch prices online", "Pull per-token rates from the public price list; off = use the cached copy"],
    ["pricing.refresh_hours", "num", "Price refresh", "Hours between price-list fetches"],
    ["usage_retention_days", "num", "Usage history", "Days of hourly usage kept on disk (min 40)"],
  ] },
];
const cget = (o, p) => p.split(".").reduce((a, k) => (a && a[k] !== undefined ? a[k] : undefined), o);

function buildForm() {
  const cfg = state.config || {};
  lastCfgJSON = JSON.stringify(cfg);
  const f = $("#cfgForm");
  f.innerHTML = CFG.map(grp => `<div class="cfg-group"><div class="glabel">${esc(grp.g)}</div>${grp.rows.map(([path, type, label, hint]) => {
    const v = cget(cfg, path);
    let ctl;
    if (type === "bool") {
      ctl = `<label class="switch"><input type="checkbox" data-path="${path}" data-type="bool" ${v ? "checked" : ""} aria-label="${esc(label)}"><span class="sl"></span></label>`;
    } else if (type.startsWith("select:")) {
      const opts = type.slice(7).split(",");
      ctl = `<select class="inp" data-path="${path}" data-type="text" aria-label="${esc(label)}">${
        opts.map(o => `<option value="${esc(o)}" ${o === v ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
    } else if (type === "text") {
      ctl = `<input class="inp wide" type="text" spellcheck="false" data-path="${path}" data-type="text" value="${esc(v)}" aria-label="${esc(label)}">`;
    } else {
      ctl = `<input class="inp" type="number" step="any" min="0" data-path="${path}" data-type="num" value="${esc(v)}" aria-label="${esc(label)}">`;
    }
    return `<div class="cfg-row"><span class="lab"><div class="t">${esc(label)}</div><div class="h">${esc(hint)}</div></span>${ctl}</div>`;
  }).join("")}</div>`).join("") +
    `<div class="cfg-foot"><button type="submit" class="btn primary" id="saveCfg">Save settings</button>
     <button type="button" class="btn ghost" id="revertCfg">Revert</button></div>`;
  $$("[data-path]", f).forEach(el => el.addEventListener("input", () => { cfgDirty = true; }));
  f.onsubmit = saveConfig;
  $("#revertCfg").onclick = () => { cfgDirty = false; buildForm(); toast("info", "Reverted to saved settings"); };
}
async function saveConfig(e) {
  e.preventDefault();
  const f = $("#cfgForm"), btn = $("#saveCfg");
  const payload = {};
  $$("[data-path]", f).forEach(el => {
    const t = el.dataset.type;
    const val = t === "bool" ? el.checked : t === "text" ? el.value.trim() : parseFloat(el.value);
    const [a, b] = el.dataset.path.split(".");
    if (b) { (payload[a] = payload[a] || {})[b] = val; } else { payload[a] = val; }
  });
  btn.disabled = true; btn.textContent = "Saving…";
  try {
    const d = await api("POST", "/config", payload);
    cfgDirty = false;
    state.config = d.config;
    buildForm();
    toast("ok", "Settings saved");
  } catch (err) {
    toast("err", "Couldn't save: " + err.message);
  } finally {
    btn.disabled = false; btn.textContent = "Save settings";
  }
}

function renderAuditPanel() {
  const a = state.audit || {};
  const used = a.bytes || 0, cap = a.max_bytes || 1;
  const ratio = Math.min(1, used / cap);
  const s = ratio >= 0.9 ? "crit" : ratio >= 0.7 ? "warn" : "good";
  const modeBadge = a.mode === "off" ? badge("neutral", "off")
    : a.mode === "meta" ? badge("warn", "metadata only") : badge("good", "full capture");
  const span = (a.oldest && a.newest)
    ? `${ago(a.oldest)} of history` : "no records yet";
  $("#auditPanel").innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap">
      ${modeBadge}
      <span class="chip">${esc(fmt(a.rows || 0))} requests</span>
      <span class="chip">${esc(span)}</span>
    </div>
    <div class="meter">
      <div class="meter-row">
        <span class="period">storage</span>
        <span class="pct" style="color:${sevInk(s)}">${esc(bytes(used))}</span>
        <span class="reset">of ${esc(bytes(cap))} cap · ${esc(String(a.retention_days || 7))}d retention</span>
      </div>
      <div class="track"><div class="bar" style="width:${Math.round(ratio * 100)}%;background:${sevMark(s)}"></div></div>
    </div>
    <div class="subtle" style="margin:10px 0 14px">
      Whichever limit bites first wins: records past ${esc(String(a.retention_days || 7))} days are dropped, and if the
      file still exceeds the cap the oldest go too. Writes happen on a background
      thread, so capture never adds latency to a request.
      ${a.dropped ? `<br><b style="color:var(--warn-ink)">${esc(fmt(a.dropped))} record(s) dropped</b> — the writer fell behind a burst.` : ""}
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">
      <button class="btn sm" id="sweepAudit">Run retention now</button>
      <button class="btn sm danger" id="purgeAudit">Purge all records</button>
    </div>`;
  $("#sweepAudit").onclick = async ev => {
    const b = ev.currentTarget; b.disabled = true; b.textContent = "Sweeping…";
    try { const d = await api("POST", "/audit/sweep"); toast("ok", `Removed ${d.removed_age + d.removed_size} record(s)`); await refresh(); }
    catch (err) { toast("err", "Sweep failed: " + err.message); }
    finally { b.disabled = false; b.textContent = "Run retention now"; }
  };
  $("#purgeAudit").onclick = async () => {
    if (!await confirmDialog({
      title: "Purge the audit log?",
      message: "Every recorded request, prompt, and completion is deleted. Usage and cost history are <b>not</b> affected. This can't be undone.",
      confirmLabel: "Purge everything", danger: true,
    })) return;
    try { const d = await api("POST", "/audit/purge"); toast("ok", `Purged ${fmt(d.removed)} records`); rows = []; maxId = minId = 0; renderRequests(); await refresh(); }
    catch (err) { toast("err", "Purge failed: " + err.message); }
  };
}

const PRICE_SRC = {
  online: { sev: "good", text: "live price list" },
  cache: { sev: "warn", text: "cached price list" },
  fallback: { sev: "crit", text: "built-in fallback rates" },
};
function renderPricing() {
  const p = state.pricing || {};
  const s = PRICE_SRC[p.source] || { sev: "neutral", text: p.source || "unknown" };
  const when = p.fetched_at ? new Date(p.fetched_at * 1000).toLocaleString() : "never";
  const unpriced = p.unpriced_models || [];
  $("#pricingPanel").innerHTML = `
    <div class="cfg-row"><span class="lab">
      <div class="t">${badge(s.sev, s.text)} <span class="chip">${esc(String(p.models || 0))} models</span></div>
      <div class="h">Fetched ${esc(when)}${p.last_error ? ` · last attempt failed: ${esc(p.last_error)}` : ""}</div>
    </span><button type="button" class="btn sm" id="refreshPrices">Refresh now</button></div>
    ${unpriced.length ? `<div class="cfg-row"><span class="lab">
      <div class="t" style="color:var(--warn-ink)">${unpriced.length} model${unpriced.length > 1 ? "s" : ""} with no published price</div>
      <div class="h mono">${esc(unpriced.join(", "))} — counted as $0.00</div></span></div>` : ""}
    <div class="cfg-row"><span class="lab"><div class="h">Rates come from the LiteLLM community price list and are applied when a
      request is recorded, so past spend never changes when prices do.</div></span></div>`;
  $("#refreshPrices").onclick = async ev => {
    const b = ev.currentTarget; b.disabled = true; b.textContent = "Fetching…";
    try { const d = await api("POST", "/pricing/refresh"); toast("ok", `Loaded ${d.models} model prices`); await refresh(); }
    catch (err) { toast("err", "Price refresh failed: " + err.message); }
    finally { b.disabled = false; b.textContent = "Refresh now"; }
  };
}

function renderLog() {
  const logs = state.rotation_log || [];
  const box = $("#log");
  if (!logs.length) { box.innerHTML = `<div class="empty">No rotations yet.</div>`; return; }
  box.innerHTML = logs.slice().reverse().map(e => {
    const when = new Date(e.time * 1000).toLocaleString();
    const sw = e.action === "switched";
    const why = e.reason === "active_unusable" ? "active dead/rate-limited" : `5h at ${Math.round((e.trigger_util_5h || 0) * 100)}%`;
    return `<div class="logrow ${sw ? "switched" : "notify"}"><div class="when">${esc(when)}</div>${sw ? "Switched" : "Would switch"} <b>${esc(e.from)}</b> <span class="arrow">→</span> <b>${esc(e.to)}</b> · ${esc(why)}</div>`;
  }).join("");
}

// =====================================================================
// Actions — tokens
// =====================================================================
async function selectToken(name) {
  try {
    await api("POST", "/select", { name });
    state.active = name; renderHero(); renderTokens();
    toast("ok", `Now serving via “${name}”`);
    refresh();
  } catch (err) { toast("err", "Switch failed: " + err.message); }
}
async function probeToken(name, btn) {
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = "Testing…";
  try {
    const d = await api("POST", "/probe", { name });
    toast(d.healthy ? "ok" : "err", d.healthy ? `“${name}” is healthy` : `“${name}” — ${d.status}`);
    await refresh();
  } catch (err) { toast("err", "Probe failed: " + err.message); }
  finally { btn.disabled = false; btn.textContent = old; }
}
async function addToken() {
  const data = await formDialog({
    title: "Add upstream token",
    desc: "An Anthropic OAuth token (sk-ant-oat-…) the proxy forwards requests with. Loaded live — no restart needed.",
    submitLabel: "Add token",
    fields: [
      { name: "name", label: "Name", placeholder: "e.g. personal", hint: "Shown in the console; must be unique." },
      { name: "token", label: "OAuth token", type: "textarea", placeholder: "sk-ant-oat-…" },
      { name: "default", label: "Use as default on restart", type: "checkbox", value: false },
    ],
  });
  if (!data) return;
  if (!data.name || !data.token) { toast("err", "Name and token are both required"); return; }
  try {
    await api("POST", "/tokens", { name: data.name, token: data.token, default: data.default });
    toast("ok", `Added token “${data.name}”`);
    await refresh();
  } catch (err) { toast("err", "Couldn't add token: " + err.message); }
}
async function editToken(name) {
  const data = await formDialog({
    title: `Edit “${name}”`,
    desc: "Leave the token blank to keep the current one. A new token replaces the stored OAuth secret and re-checks health.",
    submitLabel: "Save changes",
    fields: [
      { name: "token", label: "New OAuth token", type: "textarea", placeholder: "blank = unchanged" },
      { name: "default", label: "Make this the default on restart", type: "checkbox", value: state.default_token === name },
    ],
  });
  if (!data) return;
  const body = {};
  if (data.token) body.token = data.token;
  if (data.default) body.default = true;
  if (!Object.keys(body).length) { toast("info", "Nothing changed"); return; }
  try {
    await api("PATCH", "/tokens/" + enc(name), body);
    toast("ok", `Updated “${name}”`);
    await refresh();
  } catch (err) { toast("err", "Update failed: " + err.message); }
}
async function deleteToken(name) {
  if (!await confirmDialog({
    title: `Delete token “${name}”?`,
    message: "The proxy stops using it immediately. This can't be undone.",
    confirmLabel: "Delete token", danger: true,
  })) return;
  try {
    await api("DELETE", "/tokens/" + enc(name));
    toast("ok", `Deleted “${name}”`);
    await refresh();
  } catch (err) { toast("err", "Delete failed: " + err.message); }
}
async function revealToken(name) {
  try {
    const d = await api("POST", "/tokens/" + enc(name) + "/reveal");
    revealDialog({ title: `Token · ${name}`, label: "OAuth token", value: d.token, note: "Anyone on the tailnet can read this." });
  } catch (err) { toast("err", "Couldn't reveal: " + err.message); }
}

// =====================================================================
// Actions — virtual keys
// =====================================================================
async function addKey() {
  const data = await formDialog({
    title: "Add virtual key",
    desc: "A downstream key (vk-…) you hand to a client. Leave the value blank to generate a strong one.",
    submitLabel: "Create key",
    fields: [
      { name: "name", label: "Client name", placeholder: "e.g. laptop, home-assistant" },
      { name: "key", label: "Key value", placeholder: "blank = auto-generate", hint: "Must be unique. Starts with vk- when generated." },
    ],
  });
  if (!data) return;
  if (!data.name) { toast("err", "Client name is required"); return; }
  try {
    const d = await api("POST", "/virtual-keys", { name: data.name, key: data.key || undefined });
    await refresh();
    revealDialog({ title: `Key created · ${d.name}`, label: "Virtual key", value: d.key, note: "Copy it now and give it to the client — it's shown in full only here." });
  } catch (err) { toast("err", "Couldn't create key: " + err.message); }
}
async function rotateKey(name) {
  if (!await confirmDialog({
    title: `Rotate “${name}”?`,
    message: "Generates a new key value. The old key stops working <b>immediately</b>, so the client must be updated. Usage history is kept.",
    confirmLabel: "Rotate key", danger: true,
  })) return;
  try {
    const d = await api("POST", "/virtual-keys/" + enc(name) + "/rotate");
    await refresh();
    revealDialog({ title: `Rotated · ${d.name}`, label: "New virtual key", value: d.key, note: "Update the client with this new value now." });
  } catch (err) { toast("err", "Rotate failed: " + err.message); }
}
async function renameKey(name) {
  const data = await formDialog({ title: `Rename “${name}”`, submitLabel: "Rename", fields: [{ name: "name", label: "New client name", value: name }] });
  if (!data || !data.name || data.name === name) return;
  try {
    await api("PATCH", "/virtual-keys/" + enc(name), { name: data.name });
    toast("ok", `Renamed to “${data.name}”`);
    await refresh();
  } catch (err) { toast("err", "Rename failed: " + err.message); }
}
async function deleteKey(name) {
  if (!await confirmDialog({
    title: `Delete key “${name}”?`,
    message: "The client using it loses access immediately. This can't be undone.",
    confirmLabel: "Delete key", danger: true,
  })) return;
  try {
    await api("DELETE", "/virtual-keys/" + enc(name));
    toast("ok", `Deleted “${name}”`);
    await refresh();
  } catch (err) { toast("err", "Delete failed: " + err.message); }
}
async function revealKey(name) {
  try {
    const d = await api("POST", "/virtual-keys/" + enc(name) + "/reveal");
    revealDialog({ title: `Key · ${name}`, label: "Virtual key", value: d.key, note: "Anyone on the tailnet can read this." });
  } catch (err) { toast("err", "Couldn't reveal: " + err.message); }
}

// All four periods are edited together and sent as one replace-all payload, so
// clearing a box is how you remove a cap.
const LIMIT_PERIODS = [
  ["hour", "$ per hour", "resets on the hour"],
  ["day", "$ per day", "resets at local midnight"],
  ["week", "$ per week", "resets Monday at local midnight"],
  ["month", "$ per month", "resets on the 1st"],
];
async function editLimits(name) {
  const vk = (state.virtual_keys || []).find(v => v.name === name) || {};
  const cur = {};
  for (const l of (vk.limits || [])) cur[l.period] = l.limit_usd;
  const tz = state.timezone || "local time";
  const data = await formDialog({
    title: `Spend limits · ${name}`,
    desc: `Requests are rejected with HTTP 429 while any cap is exceeded. Windows are calendar-aligned in ${tz}. Leave a box blank for no cap.`,
    submitLabel: "Save limits",
    fields: LIMIT_PERIODS.map(([p, label, hint]) => ({
      name: p, label, hint,
      value: cur[p] !== undefined ? String(cur[p]) : "",
      placeholder: "no cap",
    })),
  });
  if (!data) return;
  const limits = {};
  for (const [p] of LIMIT_PERIODS) {
    const raw = (data[p] || "").replace(/^\$/, "").trim();
    if (!raw) continue;
    const v = Number(raw);
    if (!isFinite(v) || v < 0) { toast("err", `“${raw}” isn't a valid amount for ${p}`); return; }
    if (v > 0) limits[p] = v;
  }
  try {
    await api("PUT", "/virtual-keys/" + enc(name) + "/limits", { limits });
    const n = Object.keys(limits).length;
    toast("ok", n ? `Saved ${n} limit${n > 1 ? "s" : ""} for “${name}”` : `Removed all limits for “${name}”`);
    await refresh();
  } catch (err) { toast("err", "Couldn't save limits: " + err.message); }
}

// =====================================================================
// System status + render orchestration
// =====================================================================
function renderSystem() {
  const hh = (state.health || {})[state.active];
  const activeSev = healthLabel(hh).sev;
  const h = (state.headers || {})[state.active] || {};
  const u5 = h["anthropic-ratelimit-unified-5h-utilization"];
  const blocked = (state.virtual_keys || []).filter(vk => (vk.limits || []).some(l => l.over)).length;
  let overall = "good", label = "All systems nominal";
  if (activeSev === "crit") { overall = "crit"; label = "Active token unavailable"; }
  else if (u5 !== undefined && sev(u5) === "crit") { overall = "crit"; label = "Capacity exhausted"; }
  else if (activeSev === "warn" || (u5 !== undefined && sev(u5) === "warn")) { overall = "warn"; label = "Degraded — high utilization"; }
  else if (blocked) { overall = "warn"; label = `${blocked} client${blocked > 1 ? "s" : ""} over budget`; }
  $("#sysdot").className = "sysdot " + overall;
  $("#syslabel").textContent = label;
}
function syncKeyFilter() {
  const sel = $("#fKey");
  const names = (state.virtual_keys || []).map(v => v.name);
  const sig = names.join("|");
  if (sel._sig === sig) return;
  sel._sig = sig;
  const cur = sel.value;
  sel.innerHTML = `<option value="">All clients</option>` +
    names.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
  sel.value = cur;
}
function renderAll() {
  renderSystem();
  renderHero();
  renderTiles();
  renderSpendChart();
  renderLatency();
  renderModels();
  renderTokens();
  renderClients();
  renderAuditPanel();
  renderPricing();
  renderLog();
  syncKeyFilter();
  const a = state.audit || {};
  $("#auditChip").textContent = a.mode === "off" ? "auditing off"
    : `${a.mode} · ${fmt(a.rows || 0)} kept · ${bytes(a.bytes || 0)}`;
  const cfgJSON = JSON.stringify(state.config || {});
  if (!cfgDirty && cfgJSON !== lastCfgJSON) buildForm();   // never clobber an edit in progress
  $("#authnote").textContent =
    (state.auth_enabled ? "Admin auth: on" : "Tailnet-only · no admin auth") + ` · v${state.version || "?"}`;
}
function clearStale() {
  $("#banner").classList.remove("show");
  $$(".stale").forEach(e => e.classList.remove("stale"));
}
function applyState(data) {
  state = data;
  lastBeat = Date.now();
  clearStale();
  renderAll();
}
function connError(msg) {
  $("#bannerText").textContent = "Live connection lost — reconnecting… (" + msg + ")";
  $("#banner").classList.add("show");
  ["hero", "tokens", "clients", "tiles"].forEach(id => { if (state) $("#" + id)?.classList.add("stale"); });
}
async function refresh() {
  try {
    const r = await fetch("/state", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    applyState(await r.json());
  } catch (err) { connError(err.message); }
}
function connectSSE() {
  try { if (es) es.close(); } catch (e) { /* already closed */ }
  es = new EventSource("/events");
  es.addEventListener("state", e => {
    let data;
    try { data = JSON.parse(e.data); } catch (err) { return; }
    applyState(data);
  });
  es.addEventListener("ping", () => { lastBeat = Date.now(); clearStale(); });
  es.onopen = () => { lastBeat = Date.now(); clearStale(); };
  es.onerror = () => connError("stream closed");   // EventSource retries on its own
}
function tick() {
  if (lastBeat) {
    const s = Math.round((Date.now() - lastBeat) / 1000);
    $("#synctext").textContent = s < 5 ? "live" : `${s}s ago`;
  }
  $$("[data-reset]").forEach(el => {
    const ts = el.getAttribute("data-reset");
    if (ts) el.textContent = el.classList.contains("reset") ? "resets " + fmtReset(ts) : fmtReset(ts);
  });
}

// =====================================================================
// Router
// =====================================================================
function go(view) {
  currentView = view;
  $$("#nav button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.view === view)));
  $$(".view").forEach(v => v.classList.toggle("active", v.id === "view-" + view));
  history.replaceState(null, "", "#" + view);
  if (view === "requests") {
    if (!rows.length) loadRequests(true);
    if (liveTail) startTail();
  } else {
    stopTail();
  }
  if (view === "overview") loadAuditSummary();
}

// =====================================================================
// Theme
// =====================================================================
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("cp-theme", t); } catch (e) { /* private mode */ }
  $("#themeIcon").innerHTML = t === "dark"
    ? '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>'
    : '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>';
}
let storedTheme = null;
try { storedTheme = localStorage.getItem("cp-theme"); } catch (e) { /* private mode */ }
applyTheme(storedTheme || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));

// =====================================================================
// Wiring
// =====================================================================
$("#themeBtn").onclick = () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
$("#refreshBtn").onclick = () => {
  const b = $("#refreshBtn");
  b.style.transform = "rotate(360deg)";
  setTimeout(() => { b.style.transform = ""; }, 400);
  refresh();
  if (currentView === "requests") loadRequests(true);
  loadAuditSummary();
  if (!es || es.readyState === 2) connectSSE();
};
$("#addTokenBtn").onclick = addToken;
$("#addKeyBtn").onclick = addKey;
$$("#nav button").forEach(b => { b.onclick = () => go(b.dataset.view); });

// request filters — the search box debounces so typing doesn't fire a query per keystroke
let searchTimer = null;
$("#fSearch").addEventListener("input", e => {
  filters.q = e.target.value.trim();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadRequests(true), 280);
});
$("#fKey").addEventListener("change", e => { filters.key = e.target.value; loadRequests(true); });
$("#fOutcome").addEventListener("change", e => { filters.outcome = e.target.value; loadRequests(true); });
$("#fRange").addEventListener("change", e => { filters.hours = e.target.value; loadRequests(true); });
$("#fClear").onclick = () => {
  filters.q = ""; filters.key = ""; filters.outcome = ""; filters.hours = "24";
  $("#fSearch").value = ""; $("#fKey").value = ""; $("#fOutcome").value = ""; $("#fRange").value = "24";
  loadRequests(true);
};
$("#moreBtn").onclick = () => loadRequests(false);
$("#liveBtn").onclick = () => {
  liveTail = !liveTail;
  $("#liveBtn").setAttribute("aria-pressed", String(liveTail));
  $("#liveDot").style.visibility = liveTail ? "visible" : "hidden";
  if (liveTail && currentView === "requests") { startTail(); tailRequests(); } else { stopTail(); }
};
$("#drawerClose").onclick = closeDrawer;
$("#drawerScrim").onclick = closeDrawer;
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && $("#drawer").classList.contains("open")) closeDrawer();
});
// Pause the tail while the tab is hidden — a backgrounded console should cost nothing.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopTail();
  else if (liveTail && currentView === "requests") { startTail(); tailRequests(); }
});

// ============================ boot ============================
refresh();                      // instant first paint via GET /state
connectSSE();                   // then switch to the live push feed
loadAuditSummary();
setInterval(tick, 1000);
setInterval(loadAuditSummary, 60000);
go((location.hash || "#overview").slice(1) in { overview: 1, requests: 1, clients: 1, upstreams: 1, settings: 1 }
  ? location.hash.slice(1) : "overview");
