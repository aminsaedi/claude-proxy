/* =====================================================================
   console.js — views, state, and actions
   =====================================================================
   Every view follows the same shape: `mount()` builds its DOM once and keeps
   handles on the parts that change, `update()` writes current values into
   those handles. Nothing rebuilds markup on a live update, which is what keeps
   focus, selection, scroll position and in-flight transitions intact while
   data streams in. The primitives are in console-dom.js.

   Two data channels, deliberately different:
     * /events (SSE) pushes dashboard state, and only when something changed.
     * /requests is polled, but only while the Requests tab is on screen.
   ===================================================================== */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

// ============================ shared state ============================
let state = null;
let es = null;
let lastBeat = 0;
let currentView = "overview";
let auditOverview = null;
let auditModels = null;

const VIEWS = ["overview", "requests", "clients", "upstreams", "settings"];

// Nodes whose text is a live countdown. Registered rather than re-rendered, so
// a ticking clock never counts as "the data changed" — which is what used to
// rebuild whole cards once a minute for no reason.
const countdowns = new Map();
function countdown(node, ts, prefix = "") {
  if (!ts) { countdowns.delete(node); txt(node, "—"); return; }
  countdowns.set(node, { ts, prefix });
  txt(node, prefix + fmtReset(ts));
}

// =====================================================================
// OVERVIEW
// =====================================================================
const Overview = {
  mounted: false,

  mount() {
    if (this.mounted) return;
    this.mounted = true;

    const hero = clear($("#hero"));
    hero.appendChild(el("div.gauge", {
      role: "meter", "aria-label": "Active token 5-hour capacity used",
      "aria-valuemin": "0", "aria-valuemax": "100",
    },
      el("svg", { viewBox: "0 0 140 92", "aria-hidden": "true" },
        el("path.track", { d: "M8 84 A62 62 0 0 1 132 84" }),
        el("path.fill", { "data-ref": "arc", d: "M8 84 A62 62 0 0 1 132 84" })),
      el("div.center",
        el("div.val", el("span", { "data-ref": "pct" }), el("span.pct", { "data-ref": "pctSign" })),
        el("div.cap", { text: "5h capacity used" }))));
    hero.appendChild(el("div.hero-meta",
      el("div.eyebrow", { text: "Active upstream" }),
      el("div.hero-token",
        el("span.name", { "data-ref": "name" }),
        el("span", { "data-ref": "badges" })),
      el("div.hero-sub", { "data-ref": "sub" }),
      el("div.hero-stats",
        stat("7-day used", "u7"), stat("5h resets in", "reset"),
        stat("Upstreams", "count"), stat("Uptime", "uptime"))));
    this.hero = refs(hero);
    this.gauge = hero.querySelector(".gauge");

    this.tiles = clear($("#tiles"));
    this.spend = clear($("#spendChart"));
    this.latency = clear($("#latencyPanel"));
    this.models = clear($("#modelPanel"));
  },

  update() {
    this.mount();
    const h = (state.headers || {})[state.active] || {};
    const hl = healthLabel((state.health || {})[state.active]);
    const u5 = h["anthropic-ratelimit-unified-5h-utilization"];
    const has = u5 !== undefined;
    const p = pct(u5);
    const s = has ? sev(u5) : "neutral";
    const CIRC = 2 * Math.PI * 62 * 0.5;   // semicircle arc length, r=62

    att(this.hero.arc, "stroke", has ? sevMark(s) : "var(--surface-3)");
    att(this.hero.arc, "stroke-dasharray", CIRC);
    att(this.hero.arc, "stroke-dashoffset", has ? CIRC * (1 - Math.min(1, parseFloat(u5) || 0)) : CIRC);
    txt(this.hero.pct, has ? p : "—");
    txt(this.hero.pctSign, has ? "%" : "");
    att(this.gauge, "aria-valuenow", p);
    txt(this.hero.name, state.active || "—");
    html(this.hero.badges, badgeHTML("brand", "live") + badgeHTML(hl.sev, hl.text));
    html(this.hero.sub, heroSub(has, p, s));

    const u7 = h["anthropic-ratelimit-unified-7d-utilization"];
    txt(this.hero.u7, u7 !== undefined ? pct(u7) + "%" : "—");
    sty(this.hero.u7, "color", u7 !== undefined ? sevInk(sev(u7)) : "var(--ink)");
    countdown(this.hero.reset, h["anthropic-ratelimit-unified-5h-reset"]);
    txt(this.hero.count, state.tokens.length);
    txt(this.hero.uptime, state.started_at ? ago(state.started_at).replace(" ago", "") : "—");

    this.renderTiles();
    this.renderSpend();
    this.renderLatency();
    this.renderModels();
  },

  renderTiles() {
    const t = fleetTotals();
    const o = auditOverview || {};
    // Rate over *forwarded* requests: a rejected key never reached upstream, so
    // counting it would both inflate the rate and let a flood of bad-key
    // retries drown out a real upstream problem.
    //
    // `incomplete` counts as a failure: the caller was promised an answer and
    // got half of one. `aborted` deliberately does not — a person pressing Esc
    // in their client is the single most common way a request ends early, and
    // folding that in would make the error rate a measure of how impatient the
    // users are. It gets its own line instead, because when it climbs on its
    // own that is the edge timing requests out, not people changing their mind.
    const failed = (o.errors || 0) + (o.incomplete || 0);
    const errRate = o.forwarded ? failed / o.forwarded : 0;
    const errSev = errRate >= 0.1 ? "crit" : errRate >= 0.02 ? "warn" : "good";
    const bits = [failed && `${fmt(failed)} failed`, o.aborted && `${fmt(o.aborted)} aborted`,
      o.blocked && `${fmt(o.blocked)} blocked`,
      o.rejected && `${fmt(o.rejected)} rejected`].filter(Boolean);
    const tiles = [
      { k: "Spend today", v: usd(t.win["1d"]), sub: state.timezone || "local", accent: true },
      { k: "Spend 7d", v: usd(t.win["7d"]), sub: "7 calendar days", accent: true },
      { k: "Spend all-time", v: usd(t.cost), sub: `${fmt(t.req)} requests` },
      { k: "Requests 24h", v: fmt(o.forwarded || 0), sub: `${fmt(o.tokens || 0)} tokens` },
      {
        k: "Error rate 24h", v: o.forwarded ? (errRate * 100).toFixed(1) + "%" : "—",
        sub: bits.length ? bits.join(" · ") : "no failures",
        ink: o.forwarded ? sevInk(errSev) : null,
      },
      { k: "Active clients", v: String(t.activeClients), sub: `of ${(state.virtual_keys || []).length} keys` },
    ];
    list(this.tiles, tiles, {
      key: x => x.k,
      create: () => el("div.tile", el("div.k"), el("div.v"), el("div.sub")),
      update: (node, x) => {
        cls(node, "accent", !!x.accent);
        txt(node.children[0], x.k);
        txt(node.children[1], x.v);
        sty(node.children[1], "color", x.ink || "");
        txt(node.children[2], x.sub);
      },
    });
  },

  renderSpend() {
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
    const points = Array.from(byDate.values()).sort((a, b) => a.date.localeCompare(b.date));
    txt($("#spendRange"), points.length ? `${points.length} days` : "");
    barChart(this.spend, points, {
      value: p => p.cost_usd, label: p => p.date, format: usd,
      note: state.timezone || "",
      tip: p => [p.date, [["Spend", usd(p.cost_usd)], ["Requests", fmt(p.requests)], ["Tokens", fmt(p.tokens)]]],
    });
  },

  renderLatency() {
    const o = auditOverview;
    const box = this.latency;
    if (!o || !o.forwarded) {
      if (box.__mode !== "empty") {
        clear(box).appendChild(el("div.empty", { text: "No requests forwarded upstream in the last 24h." }));
        box.__mode = "empty";
      }
      return;
    }
    if (box.__mode !== "table") {
      clear(box);
      box.__mode = "table";
      // Percentiles, not averages: latency is long-tailed and the mean
      // describes nobody's experience of it.
      box.appendChild(el("table.tbl",
        el("thead", el("tr", el("th", { text: "Last 24h" }),
          el("th.r", { text: "p50" }), el("th.r", { text: "p95" }))),
        el("tbody",
          el("tr", el("td.name", { text: "Time to first byte" }),
            el("td.r.strong.mono", { "data-ref": "t50" }), el("td.r.strong.mono", { "data-ref": "t95" })),
          el("tr", el("td.name", { text: "Full response" }),
            el("td.r.strong.mono", { "data-ref": "l50" }), el("td.r.strong.mono", { "data-ref": "l95" })))));
      box.appendChild(el("div.subtle", { "data-ref": "note", style: "margin-top:12px" }));
      box.appendChild(el("div.subtle", { "data-ref": "rej", style: "margin-top:8px" }));
      box.__refs = refs(box);
    }
    const r = box.__refs;
    txt(r.t50, ms(o.ttfb_p50));
    txt(r.t95, ms(o.ttfb_p95));
    txt(r.l50, ms(o.latency_p50));
    txt(r.l95, ms(o.latency_p95));
    txt(r.note, `Over the ${fmt(o.forwarded)} request${o.forwarded === 1 ? "" : "s"} actually ` +
      "forwarded upstream. Time to first byte is the part the proxy and upstream control; " +
      "the full response also covers however long the completion took to generate.");
    txt(r.rej, o.rejected
      ? `${fmt(o.rejected)} request${o.rejected > 1 ? "s were" : " was"} rejected on an unknown key ` +
        "and never forwarded — excluded from these figures."
      : "");
  },

  renderModels() {
    const rows = auditModels || [];
    const box = this.models;
    if (!rows.length) {
      if (box.__mode !== "empty") {
        clear(box).appendChild(el("div.empty", { text: "No requests recorded in the last 24h." }));
        box.__mode = "empty";
      }
      return;
    }
    if (box.__mode !== "table") {
      clear(box);
      box.__mode = "table";
      box.appendChild(el("table.tbl",
        el("thead", el("tr", el("th", { text: "Model" }), el("th.r", { text: "Share" }),
          el("th.r", { text: "Requests" }), el("th.r", { text: "Cost" }),
          el("th.r", { text: "Avg TTFB" }), el("th.r", { text: "Avg total" }))),
        el("tbody", { "data-ref": "body" })));
      box.__refs = refs(box);
    }
    const max = Math.max(...rows.map(r => r.cost_usd || 0), 1e-9);
    list(box.__refs.body, rows.map((r, i) => ({ ...r, i })), {
      key: r => r.model || "—",
      create: () => el("tr",
        el("td.name", el("span.swatch", { style: "display:inline-block" }), el("span")),
        el("td.r", { style: "width:120px" }, el("span.track.thin", { style: "display:block" }, el("span.bar"))),
        el("td.r.mono"), el("td.r.strong.mono"), el("td.r.mono"), el("td.r.mono")),
      update: (node, r) => {
        const colour = SERIES[r.i % SERIES.length];
        sty(node.children[0].children[0], "background", colour);
        txt(node.children[0].children[1], " " + (r.model || "—"));
        const bar = node.children[1].firstChild.firstChild;
        sty(bar, "width", Math.round((r.cost_usd || 0) / max * 100) + "%");
        sty(bar, "background", colour);
        txt(node.children[2], fmt(r.requests));
        txt(node.children[3], usd(r.cost_usd));
        txt(node.children[4], ms(r.avg_ttfb_ms));
        txt(node.children[5], ms(r.avg_latency_ms));
      },
    });
  },
};

function stat(label, ref) {
  return el("div.hero-stat", el("div.k", { text: label }), el("div.v", { "data-ref": ref }));
}

function heroSub(has, p, s) {
  if (!has) return "No rate-limit data yet — send a request or run a probe.";
  const rem = Math.max(0, 100 - p);
  if (s === "crit") return `Only <b style="color:var(--crit-ink)">${rem}%</b> of the 5-hour window left — near or over the limit.`;
  if (s === "warn") return `<b style="color:var(--warn-ink)">${rem}%</b> of the 5-hour window remaining.`;
  return `<b style="color:var(--good-ink)">${rem}%</b> of the 5-hour window remaining — healthy headroom.`;
}

function fleetTotals() {
  let req = 0, cost = 0, activeClients = 0;
  const win = { "1d": 0, "3d": 0, "7d": 0, "30d": 0 };
  for (const vk of state.virtual_keys || []) {
    let kr = 0;
    for (const m of Object.values(vk.usage || {})) {
      req += m.requests || 0;
      cost += m.cost_usd || 0;
      kr += m.requests || 0;
    }
    for (const k in win) win[k] += (vk.windows || {})[k]?.cost_usd || 0;
    if (kr > 0) activeClients++;
  }
  return { req, cost, win, activeClients };
}

// ============================ bar chart ============================
// Single series, so no legend — the section title names the measure. 4px
// rounded tops on the baseline, 2px gaps (see the marks spec).
function barChart(box, points, { value, label, format = usd, note = "", tip }) {
  if (!points.length) {
    if (box.__mode !== "empty") {
      clear(box).appendChild(el("div.empty", { text: "No data yet." }));
      box.__mode = "empty";
    }
    return;
  }
  if (box.__mode !== "chart") {
    clear(box);
    box.__mode = "chart";
    const chart = el("div.chart",
      el("div.chart-head", el("span.t", { "data-ref": "peak" }), el("span.z", { "data-ref": "note" })),
      el("div.bars", { "data-ref": "bars" }),
      el("div.chart-axis", el("span", { "data-ref": "from" }), el("span", { "data-ref": "to" })));
    box.appendChild(chart);
    box.__refs = refs(chart);
    box.__refs.bars.addEventListener("mousemove", e => {
      const b = e.target.closest("[data-i]");
      if (!b || !box.__points) return hideTip();
      const p = box.__points[+b.dataset.i];
      if (p && box.__tip) showTip(e, ...box.__tip(p));
    });
    box.__refs.bars.addEventListener("mouseleave", hideTip);
  }
  const r = box.__refs;
  box.__points = points;
  box.__tip = tip;
  const max = Math.max(...points.map(value), 0);
  txt(r.peak, "peak " + format(max));
  txt(r.note, note);
  txt(r.from, label(points[0]));
  txt(r.to, label(points[points.length - 1]));
  list(r.bars, points.map((p, i) => ({ p, i, last: i === points.length - 1 })), {
    key: x => label(x.p),
    create: () => el("span.b", el("i")),
    update: (node, x) => {
      const v = value(x.p) || 0;
      att(node, "data-i", x.i);
      cls(node, "zero", !v);
      cls(node, "today", x.last);
      sty(node.firstChild, "height", (max > 0 ? Math.max(2, Math.round(v / max * 100)) : 0) + "%");
    },
  });
}

const tipEl = () => $("#tip");
function showTip(ev, title, rows) {
  const node = tipEl();
  node.innerHTML = `<div class="tt">${esc(title)}</div>` +
    rows.map(([k, v]) => `<div class="tr"><span>${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
  node.classList.add("show");
  node.setAttribute("aria-hidden", "false");
  const r = node.getBoundingClientRect();
  node.style.left = Math.min(window.innerWidth - r.width - 10, Math.max(8, ev.clientX + 12)) + "px";
  node.style.top = Math.max(8, ev.clientY - r.height - 12) + "px";
}
function hideTip() {
  tipEl().classList.remove("show");
  tipEl().setAttribute("aria-hidden", "true");
}

// =====================================================================
// CLIENTS — split view: list on the left, detail on the right
// =====================================================================
const PERIODS = [
  ["hour", "per hour", "resets on the hour"],
  ["day", "per day", "resets at local midnight"],
  ["week", "per week", "resets Monday midnight"],
  ["month", "per month", "resets on the 1st"],
];
// Short labels: the control sits in a narrow pane and a native select gives
// its dropdown arrow priority over the text.
const SORTS = [
  ["spend7d", "7d spend"], ["today", "Today"],
  ["requests", "Requests"], ["name", "Name"],
];
const SCOPES = [["1d", "Today"], ["7d", "7 days"], ["30d", "30 days"], ["all", "All-time"]];

const Clients = {
  mounted: false,
  selected: null,
  query: "",
  sort: "spend7d",
  scope: "1d",
  limitsDirty: false,     // never overwrite caps the operator is editing
  saving: false,

  mount() {
    if (this.mounted) return;
    this.mounted = true;
    const root = clear($("#view-clients"));

    const listPane = el("div.cl-list",
      el("div.cl-toolbar",
        el("input.inp.cl-search", {
          type: "search", placeholder: "Search clients…", "aria-label": "Search clients",
          autocomplete: "off", spellcheck: "false",
          oninput: e => { this.query = e.target.value.trim().toLowerCase(); this.renderList(); },
        }),
        el("select.inp.cl-sort", {
          "aria-label": "Sort clients",
          onchange: e => { this.sort = e.target.value; this.renderList(); },
        }, ...SORTS.map(([v, l]) => el("option", { value: v, text: l })))),
      el("div.cl-rows", { "data-ref": "rows", role: "listbox", "aria-label": "Clients" }),
      el("div.cl-foot",
        el("button.btn.sm.primary", { type: "button", onclick: () => addKey() }, "+ Add key")));

    const detail = el("div.cl-detail", { "data-ref": "detail" });
    root.appendChild(el("div.cl-split", listPane, detail));
    this.refs = refs(root);
    this.buildDetail();
  },

  buildDetail() {
    const d = clear(this.refs.detail);

    d.appendChild(el("div.cl-empty", { "data-ref": "empty" },
      el("div.empty", { text: "Select a client to see its usage and limits." })));

    d.appendChild(el("div.cl-body", { "data-ref": "body" },
      // --- header -------------------------------------------------
      el("div.cl-head",
        el("div.cl-title",
          el("h3", { "data-ref": "name" }),
          el("span", { "data-ref": "capBadge" })),
        el("div.cl-sub",
          el("span.mono", { "data-ref": "keyPreview" }),
          el("span", { "data-ref": "traffic" })),
        el("div.cl-actions",
          btn("View requests", SVG.list, () => {
            requestFilters.key = this.selected;
            $("#fKey").value = this.selected;
            go("requests");
            Requests.reload(true);
          }),
          btn("Reveal key", SVG.eye, () => revealKey(this.selected)),
          btn("Rotate", SVG.rotate, () => rotateKey(this.selected)),
          btn("Rename", SVG.rename, () => renameKey(this.selected)),
          btn("Delete", SVG.trash, () => deleteKey(this.selected), "danger"))),

      // --- spend windows ------------------------------------------
      el("div.wins", { "data-ref": "wins" }),

      // --- daily chart --------------------------------------------
      el("div.eyebrow", { text: "Daily spend", style: "margin-top:20px" }),
      el("div", { "data-ref": "chart" }),

      // --- limits editor ------------------------------------------
      el("div.cl-section",
        el("div.cl-section-head",
          el("span.eyebrow", { text: "Spend limits" }),
          el("span.rule"),
          el("span.cl-savestate", { "data-ref": "saveState" })),
        el("div.lim-rows", { "data-ref": "limRows" }),
        el("div.cl-section-foot",
          el("button.btn.primary.sm", {
            "data-ref": "saveBtn", type: "button", disabled: true,
            onclick: () => this.saveLimits(),
          }, "Save limits"),
          el("button.btn.ghost.sm", {
            "data-ref": "revertBtn", type: "button", disabled: true,
            onclick: () => { this.limitsDirty = false; this.renderDetail(); },
          }, "Revert"),
          el("span.grow"),
          el("button.btn.ghost.sm", { type: "button", onclick: () => this.clearLimits() }, "Remove all caps"))),

      // --- per-model ----------------------------------------------
      el("div.cl-section",
        el("div.cl-section-head",
          el("span.eyebrow", { text: "By model" }),
          el("span.rule"),
          el("div.seg", { "data-ref": "scopeSeg", role: "group", "aria-label": "Model breakdown period" },
            ...SCOPES.map(([k, label]) => el("button", {
              type: "button", "data-scope": k,
              onclick: () => { this.scope = k; this.renderDetail(); },
            }, label)))),
        el("div.tbl-scroll", el("table.tbl",
          el("thead", el("tr", el("th", { text: "Model" }), el("th.r", { text: "Cost" }),
            el("th.r", { text: "Req" }), el("th.r", { text: "Input" }), el("th.r", { text: "Output" }),
            el("th.r", { text: "Cache rd" }), el("th.r", { text: "Cache wr" }))),
          el("tbody", { "data-ref": "modelRows" }))),
        el("div.empty", { "data-ref": "modelEmpty", text: "No usage in this period." }))));

    // The four cap rows are built once; only their values change.
    const rowsBox = d.querySelector("[data-ref=limRows]");
    for (const [period, label, hint] of PERIODS) {
      rowsBox.appendChild(el("div.lim-row",
        el("div.lim-label", el("div.lim-period", { text: label }), el("div.lim-hint", { text: hint })),
        el("div.lim-field",
          el("span.lim-currency", { text: "$" }),
          el("input.inp.lim-input", {
            type: "number", min: "0", step: "0.01", inputmode: "decimal", placeholder: "no cap",
            "data-period": period, "aria-label": label + " spend limit",
            // A focused number input eats the wheel and silently edits the
            // value while you are only trying to scroll past it.
            onwheel: e => { if (document.activeElement === e.currentTarget) e.currentTarget.blur(); },
            oninput: () => this.markDirty(),
            onkeydown: e => { if (e.key === "Enter") { e.preventDefault(); this.saveLimits(); } },
          })),
        el("div.lim-usage",
          el("div.track.thin", el("div.bar", { "data-ref": "bar-" + period })),
          el("div.lim-text", { "data-ref": "text-" + period }))));
    }
    this.d = refs(this.refs.detail);
  },

  markDirty() {
    this.limitsDirty = true;
    this.d.saveBtn.disabled = false;
    this.d.revertBtn.disabled = false;
    cls(this.d.saveState, "ok", false);
    cls(this.d.saveState, "dirty", true);
    txt(this.d.saveState, "unsaved changes");
  },

  /** The rows, filtered and sorted. */
  rows() {
    const out = (state.virtual_keys || []).map(vk => {
      const w = vk.windows || {};
      let req = 0, cost = 0;
      for (const m of Object.values(vk.usage || {})) { req += m.requests || 0; cost += m.cost_usd || 0; }
      const caps = vk.limits || [];
      return {
        name: vk.name, preview: vk.preview || "", limits: caps, daily: vk.daily || [],
        windows: w, lifetime: vk.usage || {}, req, cost,
        today: w["1d"]?.cost_usd || 0, week: w["7d"]?.cost_usd || 0,
        worst: caps.length ? caps[0] : null,   // the server sorts tightest-first
      };
    });
    const by = {
      spend7d: (a, b) => b.week - a.week || b.cost - a.cost,
      today: (a, b) => b.today - a.today,
      requests: (a, b) => b.req - a.req,
      name: (a, b) => a.name.localeCompare(b.name),
    }[this.sort];
    return out.sort(by);
  },

  visible() {
    return this.rows().filter(r => !this.query || r.name.toLowerCase().includes(this.query));
  },

  update() {
    this.mount();
    const all = this.rows();
    // Keep the selection if it still exists; otherwise fall to the busiest.
    const names = new Set(all.map(r => r.name));
    if (!this.selected || !names.has(this.selected)) {
      this.selected = all[0]?.name || null;
      this.limitsDirty = false;
    }
    txt($("#clientCount"), String(all.length));
    val(this.refs.root.querySelector(".cl-sort"), this.sort);
    this.renderList();
    this.renderDetail();
  },

  renderList() {
    const rows = this.visible();
    list(this.refs.rows, rows, {
      key: r => r.name,
      create: r => el("button.cl-row", {
        type: "button", role: "option", "data-name": r.name,
        onclick: () => this.select(r.name),
      },
        el("span.cl-dot"),
        el("span.cl-main", el("span.cl-name"), el("span.cl-meta")),
        el("span.cl-spend")),
      update: (node, r) => {
        const s = r.worst ? (r.worst.over ? "crit" : r.worst.ratio >= 0.8 ? "warn" : "good") : null;
        cls(node, "sel", r.name === this.selected);
        att(node, "aria-selected", String(r.name === this.selected));
        sty(node.children[0], "background", s ? sevMark(s) : "var(--surface-3)");
        att(node.children[0], "title", s
          ? `${pct(r.worst.ratio)}% of the ${r.worst.period} cap` : "no spend cap");
        txt(node.children[1].children[0], r.name);
        txt(node.children[1].children[1], r.worst
          ? `${pct(r.worst.ratio)}% of ${usd(r.worst.limit_usd)}/${r.worst.period}`
          : `${fmt(r.req)} requests`);
        txt(node.children[2], usd(this.sort === "today" ? r.today : r.week));
      },
    });
    const none = this.refs.rows.querySelector(".cl-none");
    if (!rows.length && !none) {
      this.refs.rows.appendChild(el("div.empty.cl-none", { text: "No clients match." }));
    } else if (rows.length && none) {
      none.remove();
    }
  },

  select(name) {
    if (name === this.selected) return;
    this.selected = name;
    this.limitsDirty = false;
    if (location.hash !== `#clients/${enc(name)}`) {
      history.replaceState(null, "", `#clients/${enc(name)}`);
    }
    this.renderList();
    this.renderDetail();
  },

  renderDetail() {
    const r = this.rows().find(x => x.name === this.selected);
    cls(this.d.empty, "hide", !!r);
    cls(this.d.body, "hide", !r);
    if (!r) return;

    txt(this.d.name, r.name);
    html(this.d.capBadge, r.worst
      ? badgeHTML(r.worst.over ? "crit" : r.worst.ratio >= 0.8 ? "warn" : "good",
        r.worst.over ? `over ${usd(r.worst.limit_usd)} ${r.worst.period}`
          : `${pct(r.worst.ratio)}% of ${usd(r.worst.limit_usd)}/${r.worst.period}`)
      : badgeHTML("neutral", "no cap"));
    txt(this.d.keyPreview, r.preview || "vk-…");
    txt(this.d.traffic, r.req ? `· ${fmt(r.req)} requests all-time · ${usd(r.cost)}` : "· no traffic yet");

    const wins = [["1d", "Today"], ["3d", "3 days"], ["7d", "7 days"], ["30d", "30 days"]];
    list(this.d.wins, wins.map(([k, label]) => ({ k, label, d: r.windows[k] || {} })), {
      key: x => x.k,
      create: () => el("div.win", el("div.k"), el("div.v"), el("div.sub")),
      update: (node, x) => {
        cls(node, "lead", x.k === "1d");
        txt(node.children[0], x.label);
        txt(node.children[1], usd(x.d.cost_usd));
        txt(node.children[2], `${fmt(x.d.requests)} req · ${fmt(tokTotal(x.d))} tok`);
      },
    });

    barChart(this.d.chart, r.daily || [], {
      value: p => p.cost_usd, label: p => p.date, format: usd, note: state.timezone || "",
      tip: p => [p.date, [["Spend", usd(p.cost_usd)], ["Requests", fmt(p.requests)]]],
    });

    this.renderLimits(r);
    this.renderModels(r);
  },

  renderLimits(r) {
    const byPeriod = {};
    for (const l of r.limits || []) byPeriod[l.period] = l;
    for (const [period] of PERIODS) {
      const l = byPeriod[period];
      const input = this.d.limRows.querySelector(`input[data-period="${period}"]`);
      // Only push server values into the field when the operator isn't editing,
      // so a live frame can never overwrite a number being typed.
      if (!this.limitsDirty) val(input, l ? String(l.limit_usd) : "");
      const s = l ? (l.over ? "crit" : l.ratio >= 0.8 ? "warn" : "good") : null;
      const bar = this.d["bar-" + period];
      sty(bar, "width", l ? Math.min(100, Math.round(l.ratio * 100)) + "%" : "0%");
      sty(bar, "background", s ? sevMark(s) : "var(--surface-3)");
      const text = this.d["text-" + period];
      if (l) {
        countdown(text, l.resets_at, `${usd(l.spent_usd)} of ${usd(l.limit_usd)} · resets `);
        sty(text, "color", s ? sevInk(s) : "var(--muted)");
      } else {
        countdowns.delete(text);
        txt(text, "no cap");
        sty(text, "color", "var(--muted)");
      }
    }
    if (!this.limitsDirty && !this.saving) {
      this.d.saveBtn.disabled = true;
      this.d.revertBtn.disabled = true;
      cls(this.d.saveState, "dirty", false);
      if (!this.d.saveState.__ok) txt(this.d.saveState, "");
    }
  },

  renderModels(r) {
    const source = this.scope === "all" ? r.lifetime : ((r.windows[this.scope] || {}).models || {});
    const models = Object.entries(source)
      .sort((a, b) => (b[1].cost_usd || 0) - (a[1].cost_usd || 0) || tokTotal(b[1]) - tokTotal(a[1]));
    $$("[data-scope]", this.d.scopeSeg).forEach(b =>
      att(b, "aria-pressed", String(b.dataset.scope === this.scope)));
    cls(this.d.modelEmpty, "hide", models.length > 0);
    list(this.d.modelRows, models.map(([name, m], i) => ({ name, m, i })), {
      key: x => x.name,
      create: () => el("tr",
        el("td.name", el("span.swatch", { style: "display:inline-block" }), el("span")),
        el("td.r.strong"), el("td.r"), el("td.r"), el("td.r"), el("td.r"), el("td.r")),
      update: (node, x) => {
        sty(node.children[0].children[0], "background", SERIES[x.i % SERIES.length]);
        txt(node.children[0].children[1], " " + x.name);
        txt(node.children[1], usd(x.m.cost_usd));
        txt(node.children[2], fmt(x.m.requests));
        txt(node.children[3], fmt(x.m.input_tokens));
        txt(node.children[4], fmt(x.m.output_tokens));
        txt(node.children[5], fmt(x.m.cache_read_input_tokens));
        txt(node.children[6], fmt(x.m.cache_creation_input_tokens));
      },
    });
  },

  /** Read the four fields; returns null (and complains) on bad input. */
  readLimits() {
    const limits = {};
    for (const [period, label] of PERIODS) {
      const input = this.d.limRows.querySelector(`input[data-period="${period}"]`);
      const raw = (input.value || "").replace(/^\$/, "").trim();
      if (!raw) continue;
      const v = Number(raw);
      if (!isFinite(v) || v < 0) {
        toast("err", `“${raw}” isn't a valid amount ${label}`);
        input.focus();
        return null;
      }
      if (v > 0) limits[period] = v;
    }
    return limits;
  },

  async saveLimits() {
    if (this.saving) return;
    const name = this.selected;
    const limits = this.readLimits();
    if (limits === null) return;
    this.saving = true;
    this.d.saveBtn.disabled = true;
    cls(this.d.saveState, "dirty", true);
    txt(this.d.saveState, "saving…");
    try {
      const d = await api("PUT", "/virtual-keys/" + enc(name) + "/limits", { limits });
      // Apply the server's own evaluation straight away rather than waiting for
      // the next state push. That is what makes a save look instant, and it is
      // authoritative: `status` is computed from the values just persisted.
      const vk = (state.virtual_keys || []).find(v => v.name === name);
      if (vk) vk.limits = d.status || [];
      this.limitsDirty = false;
      this.saving = false;
      this.renderList();
      this.renderDetail();
      const n = Object.keys(limits).length;
      this.flashSaved(n ? `Saved ${n} cap${n > 1 ? "s" : ""}` : "Caps removed");
      refresh();   // reconcile the rest of the dashboard in the background
    } catch (err) {
      this.saving = false;
      this.d.saveBtn.disabled = false;
      txt(this.d.saveState, "");
      cls(this.d.saveState, "dirty", false);
      toast("err", "Couldn't save limits: " + err.message);
    }
  },

  async clearLimits() {
    if (!await confirmDialog({
      title: `Remove all caps on “${this.selected}”?`,
      message: "This client will be able to spend without a limit.",
      confirmLabel: "Remove caps", danger: true,
    })) return;
    for (const [period] of PERIODS) {
      this.d.limRows.querySelector(`input[data-period="${period}"]`).value = "";
    }
    this.markDirty();
    await this.saveLimits();
  },

  flashSaved(msg) {
    const node = this.d.saveState;
    node.__ok = true;
    cls(node, "dirty", false);
    cls(node, "ok", true);
    txt(node, msg);
    clearTimeout(node.__timer);
    node.__timer = setTimeout(() => {
      node.__ok = false;
      cls(node, "ok", false);
      txt(node, "");
    }, 2600);
  },
};

function btn(label, icon, onclick, kind = "") {
  return el("button.btn.sm" + (kind ? "." + kind : ""), { type: "button", onclick },
    el("span.btn-ico", { html: icon }), label);
}

// =====================================================================
// UPSTREAMS
// =====================================================================
const Upstreams = {
  mounted: false,
  mount() {
    if (this.mounted) return;
    this.mounted = true;
    this.box = $("#tokens");
    this.log = $("#log");
  },
  update() {
    this.mount();
    txt($("#tokCount"), String(state.tokens.length));
    list(this.box, state.tokens.map(n => ({ name: n })), {
      key: t => t.name,
      create: t => {
        const node = el("div.tok",
          el("div.tok-top", el("span.tok-name", { text: t.name }), el("span", { "data-ref": "badges" })),
          el("div.tok-preview", { "data-ref": "preview" }),
          el("div", { "data-ref": "meters" }),
          el("div.tok-actions",
            el("button.btn.primary.act-select", { type: "button", onclick: () => selectToken(t.name) }),
            el("button.btn.ghost", { type: "button", onclick: e => probeToken(t.name, e.currentTarget) }, "Test"),
            el("span.grow"),
            el("button.btn.mini", { type: "button", title: "Reveal token", "aria-label": "Reveal token", html: SVG.eye, onclick: () => revealToken(t.name) }),
            el("button.btn.mini", { type: "button", title: "Edit token", "aria-label": "Edit token", html: SVG.edit, onclick: () => editToken(t.name) }),
            el("button.btn.mini.del", { type: "button", title: "Delete token", "aria-label": "Delete token", html: SVG.trash, onclick: () => deleteToken(t.name) })),
          el("details.raw", { "data-ref": "raw" }, el("summary", { text: "Diagnostics" }),
            el("table.rawtable", el("tbody", { "data-ref": "rawBody" }))));
        node.__refs = refs(node);
        return node;
      },
      update: (node, t) => {
        const r = node.__refs;
        const h = (state.headers || {})[t.name] || {};
        const hl = healthLabel((state.health || {})[t.name]);
        const isActive = t.name === state.active;
        cls(node, "is-active", isActive);
        html(r.badges,
          (isActive ? badgeHTML("brand", "active") : "") +
          (t.name === state.default_token ? badgeHTML("neutral", "default") : "") +
          badgeHTML(hl.sev, hl.text));
        txt(r.preview, (state.token_previews || {})[t.name] || "");
        meters(r.meters, h);
        const sel = node.querySelector(".act-select");
        txt(sel, isActive ? "Serving traffic" : "Set active");
        sel.disabled = isActive;
        cls(r.raw, "hide", !Object.keys(h).length);
        list(r.rawBody, Object.entries(h).sort((a, b) => a[0].localeCompare(b[0])).map(([k, v]) => ({ k, v })), {
          key: x => x.k,
          create: () => el("tr", el("td"), el("td")),
          update: (n, x) => { txt(n.children[0], x.k); txt(n.children[1], x.v); },
        });
      },
    });
    this.renderLog();
  },
  renderLog() {
    const logs = (state.rotation_log || []).slice().reverse();
    if (!logs.length) {
      if (this.log.__mode !== "empty") {
        clear(this.log).appendChild(el("div.empty", { text: "No rotations yet." }));
        this.log.__mode = "empty";
      }
      return;
    }
    if (this.log.__mode !== "rows") { clear(this.log); this.log.__mode = "rows"; }
    list(this.log, logs, {
      key: e => `${e.time}-${e.to}`,
      create: () => el("div.logrow", el("div.when"), el("span")),
      update: (node, e) => {
        const sw = e.action === "switched";
        cls(node, "switched", sw);
        cls(node, "notify", !sw);
        txt(node.children[0], new Date(e.time * 1000).toLocaleString());
        html(node.children[1],
          `${sw ? "Switched" : "Would switch"} <b>${esc(e.from)}</b> <span class="arrow">→</span> ` +
          `<b>${esc(e.to)}</b> · ${esc(e.reason === "active_unusable" ? "active dead/rate-limited"
            : `5h at ${Math.round((e.trigger_util_5h || 0) * 100)}%`)}`);
      },
    });
  },
};

function meters(box, h) {
  const rows = [["5h", true], ["7d", false]]
    .filter(([p]) => h[`anthropic-ratelimit-unified-${p}-utilization`] !== undefined)
    .map(([p, big]) => ({
      p, big,
      u: h[`anthropic-ratelimit-unified-${p}-utilization`],
      reset: h[`anthropic-ratelimit-unified-${p}-reset`],
    }));
  if (!rows.length) {
    if (box.__mode !== "empty") {
      clear(box).appendChild(el("div.subtle", { text: "No rate-limit data yet.", style: "padding:6px 0" }));
      box.__mode = "empty";
    }
    return;
  }
  if (box.__mode !== "rows") { clear(box); box.__mode = "rows"; }
  list(box, rows, {
    key: r => r.p,
    create: () => el("div.meter",
      el("div.meter-row", el("span.period"), el("span.pct"), el("span.reset")),
      el("div.track", el("div.bar"))),
    update: (node, r) => {
      const s = sev(r.u);
      txt(node.children[0].children[0], r.p);
      txt(node.children[0].children[1], pct(r.u) + "%");
      sty(node.children[0].children[1], "color", sevInk(s));
      countdown(node.children[0].children[2], r.reset, "resets ");
      cls(node.children[1], "thin", !r.big);
      sty(node.children[1].firstChild, "width", Math.min(100, pct(r.u)) + "%");
      sty(node.children[1].firstChild, "background", sevMark(s));
    },
  });
}

// =====================================================================
// REQUESTS — the audit log
// =====================================================================
const requestFilters = { q: "", key: "", outcome: "", hours: "24" };

const Requests = {
  rows: [],
  live: true,
  maxId: 0,
  minId: 0,
  timer: null,
  selectedId: null,

  update() {
    const a = state.audit || {};
    txt($("#auditChip"), a.mode === "off" ? "auditing off"
      : `${a.mode} · ${fmt(a.rows || 0)} kept · ${bytes(a.bytes || 0)}`);
    txt($("#navReqCount"), auditOverview?.forwarded ? fmt(auditOverview.forwarded) : "");
    this.syncKeyFilter();
  },

  syncKeyFilter() {
    const sel = $("#fKey");
    const names = (state.virtual_keys || []).map(v => v.name);
    const sig = names.join("|");
    if (sel.__sig === sig) return;
    sel.__sig = sig;
    const cur = sel.value;
    clear(sel);
    sel.appendChild(el("option", { value: "", text: "All clients" }));
    names.forEach(n => sel.appendChild(el("option", { value: n, text: n })));
    sel.value = cur;
  },

  query(extra) {
    const p = new URLSearchParams();
    if (requestFilters.q) p.set("q", requestFilters.q);
    if (requestFilters.key) p.set("key", requestFilters.key);
    // "failed" is not an outcome, it is every outcome that is not `ok`;
    // the server owns that list so the console cannot fall behind it.
    if (requestFilters.outcome === "failed") p.set("failed", "1");
    else if (requestFilters.outcome) p.set("outcome", requestFilters.outcome);
    if (requestFilters.hours) p.set("since", String(Date.now() / 1000 - Number(requestFilters.hours) * 3600));
    p.set("limit", "100");
    for (const k in extra || {}) p.set(k, extra[k]);
    return p.toString();
  },

  async reload(reset) {
    if (reset) { this.rows = []; this.maxId = 0; this.minId = 0; }
    try {
      const d = await api("GET", "/requests?" + this.query(this.minId ? { before_id: this.minId } : {}));
      const fresh = d.requests || [];
      this.rows = reset ? fresh : this.rows.concat(fresh);
      if (this.rows.length) {
        this.maxId = Math.max(this.maxId, ...this.rows.map(r => r.id));
        this.minId = Math.min(...this.rows.map(r => r.id));
      }
      this.render();
    } catch (err) { toast("err", "Couldn't load requests: " + err.message); }
  },

  async tail() {
    if (!this.live || currentView !== "requests" || document.hidden) return;
    try {
      const d = await api("GET", "/requests?" + this.query({ after_id: String(this.maxId), limit: "50" }));
      const fresh = d.requests || [];
      if (!fresh.length) return;
      const ids = new Set(fresh.map(r => r.id));
      this.rows = fresh.concat(this.rows.filter(r => !ids.has(r.id)));
      this.maxId = Math.max(this.maxId, ...fresh.map(r => r.id));
      if (!this.minId) this.minId = Math.min(...this.rows.map(r => r.id));
      this.render(ids);
    } catch (err) { /* a failed tail poll just retries next tick */ }
  },

  start() { this.stop(); this.timer = setInterval(() => this.tail(), 2500); },
  stop() { if (this.timer) { clearInterval(this.timer); this.timer = null; } },

  render(newIds) {
    const body = $("#reqBody");
    const empty = $("#reqEmpty");
    txt($("#reqCount"), this.rows.length ? `${this.rows.length} shown` : "");
    if (!this.rows.length) {
      clear(body);
      empty.hidden = false;
      txt(empty, state?.audit?.mode === "off"
        ? "Request auditing is turned off — enable it under Settings."
        : "No requests match these filters.");
      return;
    }
    empty.hidden = true;
    list(body, this.rows, {
      key: r => r.id,
      create: r => {
        const node = el("tr", {
          tabindex: "0",
          onclick: () => this.open(r.id),
          onkeydown: e => {
            if (e.key === "Enter" || e.key === " ") { e.preventDefault(); this.open(r.id); }
          },
        },
          el("td.when"), el("td"), el("td.sum"), el("td"),
          el("td.num"), el("td.num.r"), el("td.num.r"), el("td.num.r"));
        if (newIds && newIds.has(r.id)) {
          node.classList.add("newrow");
          setTimeout(() => node.classList.remove("newrow"), 1200);
        }
        return node;
      },
      update: (node, r) => {
        att(node, "aria-selected", String(r.id === this.selectedId));
        const summary = r.summary || (r.outcome === "rejected" ? "(unauthenticated request)" : r.path || "");
        txt(node.children[0], clock(r.ts));
        att(node.children[0], "title", new Date(r.ts * 1000).toLocaleString());
        html(node.children[1], outcomeBadge(r));
        txt(node.children[2], summary);
        att(node.children[2], "title", summary);
        txt(node.children[3], r.key_name || "—");
        txt(node.children[4], (r.model || "—").replace(/^claude-/, ""));
        att(node.children[4], "title", r.model || "");
        txt(node.children[5], fmt(tokTotal(r)));
        txt(node.children[6], usd(r.cost_usd));
        txt(node.children[7], ms(r.latency_ms));
        att(node.children[7], "title", "TTFB " + ms(r.ttfb_ms));
      },
    });
    $("#moreBtn").hidden = this.rows.length < 25;
  },

  async open(id) {
    this.selectedId = id;
    this.render();
    // Remember what opened it, then make everything behind the drawer inert so
    // Tab can't walk out of it into a page the user can't even see.
    this.returnFocus = document.activeElement;
    $("#drawer").classList.add("open");
    $("#drawer").setAttribute("aria-hidden", "false");
    $("#drawerScrim").classList.add("open");
    pageInert(true);
    $("#drawerClose").focus();
    clear($("#drawerBody")).appendChild(el("div.skl", { style: "height:120px" }));
    try {
      const d = await api("GET", "/requests/" + id);
      if (this.selectedId !== id) return;      // a newer row won the race
      txt($("#drawerTitle"), `${d.model || d.path || "Request"} · ${clock(d.ts)}`);
      $("#drawerBody").innerHTML = detailHTML(d);
      clampTurns($("#drawerBody"));
      $("#drawerCopy").onclick = async () => {
        const ok = await copyText(JSON.stringify(d, null, 2), document.body);
        toast(ok ? "ok" : "err", ok ? "Request JSON copied" : "Couldn't copy");
      };
    } catch (err) {
      clear($("#drawerBody")).appendChild(el("div.empty", { text: err.message }));
    }
  },

  close() {
    $("#drawer").classList.remove("open");
    $("#drawer").setAttribute("aria-hidden", "true");
    $("#drawerScrim").classList.remove("open");
    pageInert(false);
    this.selectedId = null;
    this.render();
    if (this.returnFocus && this.returnFocus.isConnected) this.returnFocus.focus();
    this.returnFocus = null;
  },
};

/** Seal (or unseal) the page behind an overlay. */
function pageInert(on) {
  for (const node of [$("#scroll"), $(".topbar")]) if (node) node.inert = on;
}

/** Clamp overlong prompt turns behind a toggle.
 *
 * They used to be `max-height` + `overflow-y: auto`, i.e. a scrollbox nested in
 * the drawer's own scrollbox: reading one turn meant scrolling two things, and
 * the wheel picked whichever the pointer happened to be over. */
function clampTurns(root) {
  for (const body of $$(".turn-body", root)) {
    body.classList.add("clamped");
    if (body.scrollHeight <= body.clientHeight + 4) { body.classList.remove("clamped"); continue; }
    const more = el("button.turn-more", {
      type: "button", "aria-expanded": "false",
      text: `Show all — ${fmt(body.textContent.length)} characters`,
      onclick: () => {
        const open = body.classList.toggle("clamped");
        att(more, "aria-expanded", String(!open));
        txt(more, open ? `Show all — ${fmt(body.textContent.length)} characters` : "Show less");
        if (open) body.parentNode.scrollIntoView({ block: "nearest" });
      },
    });
    body.after(more);
  }
}

function outcomeBadge(r) {
  if (r.outcome === "blocked") return badgeHTML("warn", "over budget");
  if (r.outcome === "aborted") return badgeHTML("warn", "aborted");
  if (r.outcome === "incomplete") return badgeHTML("warn", "incomplete");
  if (r.outcome === "rejected") return badgeHTML("crit", "rejected");
  if (r.outcome === "error" || r.status >= 400) return badgeHTML("crit", String(r.status || "error"));
  return badgeHTML("good", r.streamed ? "stream" : "ok");
}

function turn(role, text, kind) {
  if (!text) return "";
  return `<div class="turn ${kind || role}">
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

function detailHTML(d) {
  const req = d.request;
  const meta = [
    ["Time", new Date(d.ts * 1000).toLocaleString()], ["Client", d.key_name || "—"],
    ["Model", d.model || "—"], ["Status", String(d.status || "—")],
    ["Outcome", d.outcome || "—"], ["Upstream", d.token_name || "—"],
    ["Attempts", String(d.attempts || 1)], ["TTFB", ms(d.ttfb_ms)],
    ["Total", ms(d.latency_ms)], ["Input", fmt(d.input_tokens)],
    ["Output", fmt(d.output_tokens)], ["Cache read", fmt(d.cache_read_input_tokens)],
    ["Cache write", fmt(d.cache_creation_input_tokens)], ["Cost", usd(d.cost_usd)],
    ["Client IP", d.client_ip || "—"], ["Request ID", d.request_id || "—"],
  ];
  let body = `<div class="kv">${meta.map(([k, v]) =>
    `<div><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div></div>`).join("")}</div>`;
  if (d.error) {
    body += `<div class="turn"><div class="turn-head" style="color:var(--crit-ink)">Error</div>
      <div class="turn-body">${esc(d.error)}</div></div>`;
  }
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
  { g: "Auth throttle", rows: [
    ["auth_guard.max_failures", "num", "Invalid keys before block", "Failed attempts from one address before it gets 429s. 0 disables the throttle. Only ever applies to requests whose key already failed, so a valid key is never delayed"],
    ["auth_guard.window_seconds", "num", "Counting window", "Seconds the failure count accumulates over before resetting"],
    ["auth_guard.block_seconds", "num", "Block for", "Seconds an address is refused after tripping the threshold"],
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

const Settings = {
  built: false,
  dirty: false,

  update() {
    if (!this.built) this.build();
    else if (!this.dirty) this.fill();
    this.renderAudit();
    this.renderPricing();
  },

  build() {
    this.built = true;
    const f = clear($("#cfgForm"));
    for (const grp of CFG) {
      const g = el("div.cfg-group", el("div.glabel", { text: grp.g }));
      for (const [path, type, label, hint] of grp.rows) {
        let ctl;
        if (type === "bool") {
          ctl = el("label.switch",
            el("input", { type: "checkbox", "data-path": path, "data-type": "bool", "aria-label": label, onchange: () => this.markDirty() }),
            el("span.sl"));
        } else if (type.startsWith("select:")) {
          ctl = el("select.inp", { "data-path": path, "data-type": "text", "aria-label": label, onchange: () => this.markDirty() },
            ...type.slice(7).split(",").map(o => el("option", { value: o, text: o })));
        } else if (type === "text") {
          ctl = el("input.inp.wide", { type: "text", spellcheck: "false", "data-path": path, "data-type": "text", "aria-label": label, oninput: () => this.markDirty() });
        } else {
          ctl = el("input.inp", { type: "number", step: "any", min: "0", "data-path": path, "data-type": "num", "aria-label": label, oninput: () => this.markDirty() });
        }
        g.appendChild(el("div.cfg-row",
          el("span.lab", el("div.t", { text: label }), el("div.h", { text: hint })), ctl));
      }
      f.appendChild(g);
    }
    f.appendChild(el("div.cfg-foot",
      el("button.btn.primary#saveCfg", { type: "submit" }, "Save settings"),
      el("button.btn.ghost", {
        type: "button",
        onclick: () => { this.dirty = false; this.fill(); toast("info", "Reverted to saved settings"); },
      }, "Revert")));
    f.onsubmit = e => this.save(e);
    this.fill();
  },

  markDirty() { this.dirty = true; },

  fill() {
    const cfg = state.config || {};
    for (const node of $$("[data-path]", $("#cfgForm"))) {
      const v = cget(cfg, node.dataset.path);
      if (node.type === "checkbox") { if (!isEditing(node)) node.checked = !!v; }
      else val(node, v);
    }
  },

  async save(e) {
    e.preventDefault();
    const b = $("#saveCfg");
    const payload = {};
    for (const node of $$("[data-path]", $("#cfgForm"))) {
      const t = node.dataset.type;
      const v = t === "bool" ? node.checked : t === "text" ? node.value.trim() : parseFloat(node.value);
      const [a, sub] = node.dataset.path.split(".");
      if (sub) { (payload[a] = payload[a] || {})[sub] = v; } else { payload[a] = v; }
    }
    b.disabled = true;
    txt(b, "Saving…");
    try {
      const d = await api("POST", "/config", payload);
      this.dirty = false;
      state.config = d.config;
      this.fill();
      toast("ok", "Settings saved");
    } catch (err) {
      toast("err", "Couldn't save: " + err.message);
    } finally {
      b.disabled = false;
      txt(b, "Save settings");
    }
  },

  renderAudit() {
    const a = state.audit || {};
    const used = a.bytes || 0, cap = a.max_bytes || 1;
    const ratio = Math.min(1, used / cap);
    const s = ratio >= 0.9 ? "crit" : ratio >= 0.7 ? "warn" : "good";
    const box = $("#auditPanel");
    if (!box.__built) {
      box.__built = true;
      clear(box).appendChild(el("div", { style: "display:flex;align-items:center;gap:10px;margin-bottom:14px;flex-wrap:wrap" },
        el("span", { "data-ref": "mode" }), el("span.chip", { "data-ref": "rows" }), el("span.chip", { "data-ref": "span" })));
      box.appendChild(el("div.meter",
        el("div.meter-row", el("span.period", { text: "storage" }),
          el("span.pct", { "data-ref": "used" }), el("span.reset", { "data-ref": "cap" })),
        el("div.track", el("div.bar", { "data-ref": "bar" }))));
      box.appendChild(el("div.subtle", { "data-ref": "note", style: "margin:10px 0 14px" }));
      box.appendChild(el("div", { style: "display:flex;gap:8px;flex-wrap:wrap" },
        el("button.btn.sm", { type: "button", onclick: e => this.sweep(e.currentTarget) }, "Run retention now"),
        el("button.btn.sm.danger", { type: "button", onclick: () => this.purge() }, "Purge all records")));
      box.__refs = refs(box);
    }
    const r = box.__refs;
    html(r.mode, a.mode === "off" ? badgeHTML("neutral", "off")
      : a.mode === "meta" ? badgeHTML("warn", "metadata only") : badgeHTML("good", "full capture"));
    txt(r.rows, `${fmt(a.rows || 0)} requests`);
    txt(r.span, a.oldest && a.newest ? `${ago(a.oldest).replace(" ago", "")} of history` : "no records yet");
    txt(r.used, bytes(used));
    sty(r.used, "color", sevInk(s));
    txt(r.cap, `of ${bytes(cap)} cap · ${a.retention_days || 7}d retention`);
    sty(r.bar, "width", Math.round(ratio * 100) + "%");
    sty(r.bar, "background", sevMark(s));
    html(r.note, `Whichever limit bites first wins: records past ${a.retention_days || 7} days are dropped,
      and if the file still exceeds the cap the oldest go too. Writes happen on a background thread,
      so capture never adds latency to a request.` +
      (a.dropped ? `<br><b style="color:var(--warn-ink)">${fmt(a.dropped)} record(s) dropped</b> — the writer fell behind a burst.` : ""));
  },

  async sweep(b) {
    b.disabled = true; txt(b, "Sweeping…");
    try {
      const d = await api("POST", "/audit/sweep");
      toast("ok", `Removed ${d.removed_age + d.removed_size} record(s)`);
      await refresh();
    } catch (err) { toast("err", "Sweep failed: " + err.message); }
    finally { b.disabled = false; txt(b, "Run retention now"); }
  },

  async purge() {
    if (!await confirmDialog({
      title: "Purge the audit log?",
      message: "Every recorded request, prompt, and completion is deleted. Usage and cost history are <b>not</b> affected. This can't be undone.",
      confirmLabel: "Purge everything", danger: true,
    })) return;
    try {
      const d = await api("POST", "/audit/purge");
      toast("ok", `Purged ${fmt(d.removed)} records`);
      Requests.rows = []; Requests.maxId = Requests.minId = 0; Requests.render();
      await refresh();
    } catch (err) { toast("err", "Purge failed: " + err.message); }
  },

  renderPricing() {
    const p = state.pricing || {};
    const SRC = {
      online: { sev: "good", text: "live price list" },
      cache: { sev: "warn", text: "cached price list" },
      fallback: { sev: "crit", text: "built-in fallback rates" },
    };
    const s = SRC[p.source] || { sev: "neutral", text: p.source || "unknown" };
    const box = $("#pricingPanel");
    if (!box.__built) {
      box.__built = true;
      clear(box).appendChild(el("div.cfg-row",
        el("span.lab", el("div.t", { "data-ref": "src" }), el("div.h", { "data-ref": "when" })),
        el("button.btn.sm", { type: "button", onclick: e => this.refreshPrices(e.currentTarget) }, "Refresh now")));
      box.appendChild(el("div.cfg-row", { "data-ref": "unpricedRow" },
        el("span.lab", el("div.t", { "data-ref": "unpriced", style: "color:var(--warn-ink)" }),
          el("div.h.mono", { "data-ref": "unpricedList" }))));
      box.appendChild(el("div.cfg-row", el("span.lab", el("div.h", {
        text: "Rates come from the LiteLLM community price list and are applied when a request is recorded, so past spend never changes when prices do.",
      }))));
      box.__refs = refs(box);
    }
    const r = box.__refs;
    html(r.src, badgeHTML(s.sev, s.text) + ` <span class="chip">${fmt(p.models || 0)} models</span>`);
    txt(r.when, `Fetched ${p.fetched_at ? new Date(p.fetched_at * 1000).toLocaleString() : "never"}` +
      (p.last_error ? ` · last attempt failed: ${p.last_error}` : ""));
    const un = p.unpriced_models || [];
    cls(r.unpricedRow, "hide", !un.length);
    txt(r.unpriced, `${un.length} model${un.length > 1 ? "s" : ""} with no published price`);
    txt(r.unpricedList, un.join(", ") + " — counted as $0.00");
  },

  async refreshPrices(b) {
    b.disabled = true; txt(b, "Fetching…");
    try {
      const d = await api("POST", "/pricing/refresh");
      toast("ok", `Loaded ${d.models} model prices`);
      await refresh();
    } catch (err) { toast("err", "Price refresh failed: " + err.message); }
    finally { b.disabled = false; txt(b, "Refresh now"); }
  },
};

// =====================================================================
// Actions
// =====================================================================
async function selectToken(name) {
  try {
    await api("POST", "/select", { name });
    state.active = name;
    Overview.update(); Upstreams.update();
    toast("ok", `Now serving via “${name}”`);
    refresh();
  } catch (err) { toast("err", "Switch failed: " + err.message); }
}

async function probeToken(name, b) {
  const old = b.textContent;
  b.disabled = true; txt(b, "Testing…");
  try {
    const d = await api("POST", "/probe", { name });
    toast(d.healthy ? "ok" : "err", d.healthy ? `“${name}” is healthy` : `“${name}” — ${d.status}`);
    await refresh();
  } catch (err) { toast("err", "Probe failed: " + err.message); }
  finally { b.disabled = false; txt(b, old); }
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
    Clients.select(d.name);
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
  const data = await formDialog({
    title: `Rename “${name}”`, submitLabel: "Rename",
    fields: [{ name: "name", label: "New client name", value: name }],
  });
  if (!data || !data.name || data.name === name) return;
  try {
    await api("PATCH", "/virtual-keys/" + enc(name), { name: data.name });
    Clients.selected = data.name;
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
    Clients.selected = null;
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

// =====================================================================
// System status + orchestration
// =====================================================================
function renderSystem() {
  const hl = healthLabel((state.health || {})[state.active]);
  const h = (state.headers || {})[state.active] || {};
  const u5 = h["anthropic-ratelimit-unified-5h-utilization"];
  const blocked = (state.virtual_keys || []).filter(vk => (vk.limits || []).some(l => l.over)).length;
  let overall = "good", label = "All systems nominal";
  if (hl.sev === "crit") { overall = "crit"; label = "Active token unavailable"; }
  else if (u5 !== undefined && sev(u5) === "crit") { overall = "crit"; label = "Capacity exhausted"; }
  else if (hl.sev === "warn" || (u5 !== undefined && sev(u5) === "warn")) { overall = "warn"; label = "Degraded — high utilization"; }
  else if (blocked) { overall = "warn"; label = `${blocked} client${blocked > 1 ? "s" : ""} over budget`; }
  const dot = $("#sysdot");
  ["good", "warn", "crit"].forEach(c => cls(dot, c, c === overall));
  txt($("#syslabel"), label);
}

function renderAll() {
  renderSystem();
  Overview.update();
  Clients.update();
  Upstreams.update();
  Requests.update();
  Settings.update();
  txt($("#authnote"),
    (state.auth_enabled ? "Admin auth: on" : "Tailnet-only · no admin auth") + ` · v${state.version || "?"}`);
}

function clearStale() {
  $("#banner").classList.remove("show");
}

function applyState(data) {
  state = data;
  lastBeat = Date.now();
  clearStale();
  renderAll();
}

function connError(msg) {
  txt($("#bannerText"), "Live connection lost — reconnecting… (" + msg + ")");
  $("#banner").classList.add("show");
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
  es.addEventListener("bye", () => { /* pod draining; EventSource reconnects to the new one */ });
  es.onopen = () => { lastBeat = Date.now(); clearStale(); };
  es.onerror = () => connError("stream closed");
}

function tick() {
  if (lastBeat) {
    const s = Math.round((Date.now() - lastBeat) / 1000);
    txt($("#synctext"), s < 5 ? "live" : `${s}s ago`);
  }
  for (const [node, { ts, prefix }] of countdowns) {
    if (!node.isConnected) { countdowns.delete(node); continue; }
    txt(node, prefix + fmtReset(ts));
  }
}

async function loadAuditSummary() {
  try {
    const [o, m] = await Promise.all([
      api("GET", "/audit/overview?hours=24"),
      api("GET", "/audit/models?hours=24"),
    ]);
    auditOverview = o;
    auditModels = m.models || [];
    if (state) { Overview.update(); Requests.update(); }
  } catch (err) { /* the overview degrades to "no data" on its own */ }
}

// =====================================================================
// Router
// =====================================================================
const scrollTops = new Map();

function go(view, sub) {
  if (!VIEWS.includes(view)) view = "overview";
  const changed = view !== currentView;
  if (changed) scrollTops.set(currentView, $("#scroll").scrollTop);
  currentView = view;
  $$("#nav button").forEach(b => {
    const on = b.dataset.view === view;
    att(b, "aria-selected", String(on));
    att(b, "tabindex", on ? "0" : "-1");
  });
  $$(".view").forEach(v => cls(v, "active", v.id === "view-" + view));
  // Each section keeps its own place, so coming back to a request log you had
  // scrolled halfway down doesn't dump you at the top.
  if (changed) $("#scroll").scrollTo({ top: scrollTops.get(view) || 0, behavior: "instant" });
  const target = view === "clients" && (sub || Clients.selected)
    ? `#clients/${enc(sub || Clients.selected)}` : "#" + view;
  if (location.hash !== target) history.replaceState(null, "", target);
  if (view === "requests") {
    if (!Requests.rows.length) Requests.reload(true);
    if (Requests.live) Requests.start();
  } else {
    Requests.stop();
  }
  if (view === "overview") loadAuditSummary();
}

function routeFromHash() {
  const [view, sub] = location.hash.replace(/^#/, "").split("/");
  if (view === "clients" && sub) {
    const name = decodeURIComponent(sub);
    if (Clients.selected !== name) { Clients.selected = name; Clients.limitsDirty = false; }
    if (state) Clients.update();
  }
  go(view || "overview");
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
  if (currentView === "requests") Requests.reload(true);
  loadAuditSummary();
  if (!es || es.readyState === 2) connectSSE();
};
$("#addTokenBtn").onclick = addToken;
// Focus the scroll region directly rather than letting the browser write
// "#scroll" into the hash, which the router would read as an unknown view.
$(".skip").onclick = e => { e.preventDefault(); $("#scroll").focus(); };
$$("#nav button").forEach(b => { b.onclick = () => go(b.dataset.view); });
$("#nav").addEventListener("keydown", e => {
  const step = { ArrowRight: 1, ArrowLeft: -1, Home: "first", End: "last" }[e.key];
  if (step === undefined) return;
  e.preventDefault();
  const i = VIEWS.indexOf(currentView);
  const next = step === "first" ? 0
    : step === "last" ? VIEWS.length - 1
    : (i + step + VIEWS.length) % VIEWS.length;
  go(VIEWS[next]);
  $(`#nav button[data-view="${VIEWS[next]}"]`).focus();
});

let searchTimer = null;
$("#fSearch").addEventListener("input", e => {
  requestFilters.q = e.target.value.trim();
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => Requests.reload(true), 280);
});
$("#fKey").addEventListener("change", e => { requestFilters.key = e.target.value; Requests.reload(true); });
$("#fOutcome").addEventListener("change", e => { requestFilters.outcome = e.target.value; Requests.reload(true); });
$("#fRange").addEventListener("change", e => { requestFilters.hours = e.target.value; Requests.reload(true); });
$("#fClear").onclick = () => {
  Object.assign(requestFilters, { q: "", key: "", outcome: "", hours: "24" });
  $("#fSearch").value = ""; $("#fKey").value = ""; $("#fOutcome").value = ""; $("#fRange").value = "24";
  Requests.reload(true);
};
$("#moreBtn").onclick = () => Requests.reload(false);
$("#liveBtn").onclick = () => {
  Requests.live = !Requests.live;
  att($("#liveBtn"), "aria-pressed", String(Requests.live));
  sty($("#liveDot"), "visibility", Requests.live ? "visible" : "hidden");
  if (Requests.live && currentView === "requests") { Requests.start(); Requests.tail(); } else { Requests.stop(); }
};
$("#drawerClose").onclick = () => Requests.close();
$("#drawerScrim").onclick = () => Requests.close();
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && $("#drawer").classList.contains("open")) Requests.close();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) Requests.stop();
  else if (Requests.live && currentView === "requests") { Requests.start(); Requests.tail(); }
});
window.addEventListener("hashchange", routeFromHash);

// ============================ boot ============================
refresh().then(routeFromHash);
connectSSE();
loadAuditSummary();
setInterval(tick, 1000);
setInterval(loadAuditSummary, 60000);
