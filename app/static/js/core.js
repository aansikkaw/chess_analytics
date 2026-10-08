// Shared helpers: DOM building, API calls, app state, analytics, error reporting.
// Every piece of server text is inserted with textContent, never as HTML.

export const $ = (s, root = document) => root.querySelector(s);
export const $$ = (s, root = document) => [...root.querySelectorAll(s)];
export const SVGNS = "http://www.w3.org/2000/svg";
export const PLATFORM = { lichess: "Lichess", chesscom: "Chess.com", pgn: "ChessBase / PGN", demo: "Demo" };
export const SKILL_LABEL = { openings: "Openings", middlegame: "Middlegame", endgames: "Endgames", tactics: "Tactics",
  time: "Time management", converting: "Converting", defending: "Defending" };

export const S = {
  config: null, me: null, accountId: null, profile: null, puzzles: [], chat: [], citations: {},
  currency: null, job: null,
};

export function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  setAttrs(n, attrs);
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) n.append(c);
  return n;
}
export function svg(tag, attrs = {}, ...children) {
  const n = document.createElementNS(SVGNS, tag);
  setAttrs(n, attrs);
  for (const c of children.flat()) if (c !== null && c !== undefined && c !== false) n.append(c);
  return n;
}
function setAttrs(n, attrs) {
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") n.setAttribute("class", v);
    else if (k === "text") n.textContent = v;
    else if (k === "style" && typeof v === "object") Object.assign(n.style, v);
    else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v === true ? "" : v);
  }
}

/** replaceChildren that skips null/false (the native one would print "null"). */
export function fill(node, ...kids) {
  node.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false));
  return node;
}

export class ApiError extends Error {
  constructor(msg, status, detail) {
    super(msg);
    this.status = status;
    this.upgrade = !!(detail && detail.upgrade);
    this.verify = !!(detail && detail.verify);
    this.detail = detail;
  }
}

async function handle(res) {
  const data = res.status === 204 ? {} : await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    const msg = typeof d === "string" ? d : d && d.message ? d.message
      : Array.isArray(d) ? d.map((e) => e.msg).join("; ") : `Something went wrong (${res.status}). Please try again.`;
    throw new ApiError(msg, res.status, typeof d === "object" ? d : null);
  }
  return data;
}
export async function api(path, opts = {}) {
  let res;
  try {
    res = await fetch(path, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...opts });
  } catch {
    throw new ApiError("Can't reach the server. Check your connection and try again.", 0, null);
  }
  return handle(res);
}
export const post = (p, body) => api(p, { method: "POST", body: JSON.stringify(body || {}) });
export const patch = (p, body) => api(p, { method: "PATCH", body: JSON.stringify(body || {}) });
export const del = (p, body) => api(p, { method: "DELETE", body: body ? JSON.stringify(body) : undefined });
export async function upload(path, fields) {
  const fd = new FormData();
  for (const [k, v] of Object.entries(fields)) if (v !== undefined && v !== null) fd.append(k, v);
  let res;
  try { res = await fetch(path, { method: "POST", body: fd, credentials: "same-origin" }); }
  catch { throw new ApiError("Can't reach the server. Check your connection and try again.", 0, null); }
  return handle(res);
}

export const store = {
  get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* private mode */ } },
};

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
export const isPro = () => !!S.me && S.me.plan === "pro";
export const hasFeature = (f) => !!S.me && S.me.features.includes(f);
export const account = () => S.me && S.me.accounts.find((a) => a.id === S.accountId);

export function showErr(node, msg) { node.textContent = msg || ""; node.hidden = !msg; }

let toastTimer;
export function toast(msg, ms = 3800) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.hidden = true; }, ms);
}

export function ago(ts) {
  if (!ts) return "never synced";
  const m = Math.round((Date.now() / 1000 - ts) / 60);
  if (m < 2) return "synced just now";
  if (m < 60) return `synced ${m} min ago`;
  if (m < 1440) return `synced ${Math.round(m / 60)} h ago`;
  return `synced ${Math.round(m / 1440)} days ago`;
}
export const signed = (n) => (n > 0 ? `+${n}` : n < 0 ? `−${Math.abs(n)}` : "±0");
export const pawns = (v) => (v == null ? "–" : typeof v === "number" ? (v > 0 ? `+${v.toFixed(1)}` : v.toFixed(1)) : String(v));

// Currency: rupees in India, dollars elsewhere; the visitor can switch.
export function currency() {
  if (S.currency) return S.currency;
  const saved = store.get("pb:cur");
  if (saved) return (S.currency = saved);
  let tz = "";
  try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch { /* old browser */ }
  return (S.currency = /Calcutta|Kolkata/.test(tz) ? "inr" : "usd");
}
export function setCurrency(c) { S.currency = c; store.set("pb:cur", c); }

// ---- dialogs ----
export function openDialog(d) {
  if (!d.open) d.showModal();
  return d;
}
document.addEventListener("click", (e) => {
  const close = e.target.closest("[data-close]");
  if (close) close.closest("dialog").close();
  if (e.target instanceof HTMLDialogElement && e.target.open) {
    // A click on the backdrop closes the dialog.
    const r = e.target.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) e.target.close();
  }
});

// ---- analytics (only if the server turned it on; never emails or usernames) ----
let analytics = { provider: "none" };
export function initAnalytics(cfg) {
  analytics = cfg || { provider: "none" };
  if (analytics.provider === "plausible") {
    window.plausible = window.plausible || function (...a) { (window.plausible.q = window.plausible.q || []).push(a); };
    document.head.append(el("script", { defer: true, "data-domain": analytics.domain, src: analytics.src }));
  } else if (analytics.provider === "posthog") {
    const s = el("script", { async: true, src: `${analytics.host}/static/array.js` });
    s.addEventListener("load", () => {
      if (window.posthog) window.posthog.init(analytics.key, { api_host: analytics.host, person_profiles: "identified_only",
        autocapture: false, capture_pageview: true, disable_session_recording: true, persistence: "memory" });
    });
    document.head.append(s);
  }
}
export function track(event, props) {
  try {
    if (analytics.provider === "plausible" && window.plausible) window.plausible(event, props ? { props } : undefined);
    else if (analytics.provider === "posthog" && window.posthog && window.posthog.capture) window.posthog.capture(event, props);
  } catch { /* analytics must never break the app */ }
}

// ---- browser errors go to our own server (which forwards them to Sentry if configured) ----
let reported = 0;
function report(payload) {
  if (reported++ > 10) return;
  try {
    fetch("/api/client-error", { method: "POST", keepalive: true, headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...payload, page: location.pathname + location.hash }) });
  } catch { /* nothing to do */ }
}
export function initErrorReporting() {
  window.addEventListener("error", (e) => report({ message: String(e.message || "error"), source: e.filename, line: e.lineno, col: e.colno,
    stack: e.error && e.error.stack ? String(e.error.stack) : null }));
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    if (r instanceof ApiError) return; // server already knows about its own errors
    report({ message: `Unhandled: ${r && r.message ? r.message : String(r)}`, stack: r && r.stack ? String(r.stack) : null });
  });
}
