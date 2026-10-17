// Signed-out experience: hero with a live sample report card, the no-signup preview, sign in / sign up,
// forgotten password, password reset and email verification pages. The path from here to a first report card
// is: username -> preview (~15 s) -> free account -> that game's report card, analysed first (about a minute).

import { $, S, api, el, fill, openDialog, post, showErr, sleep, toast, track } from "./core.js";
import { Board, miniBoard, uciArrow, uciMarks } from "./board.js";
import { renderDNA } from "./dna.js";
import { bindCurrency, renderPlans } from "./plans.js";
import { BAD, CLASS, evalGraph } from "./report-graph.js";

let onSignedIn = null;
export function initLanding(signedInCallback) {
  onSignedIn = signedInCallback;
  bindPlatform();
  $("#previewForm").addEventListener("submit", (e) => { e.preventDefault(); runPreview(); });
  document.querySelectorAll("[data-auth]").forEach((b) => b.addEventListener("click", () => openAuth(b.dataset.auth)));
  bindAuthForm();
}

// ---------------- the live sample report card (a demo game; scripts/make_sample_report.py) ----------------
async function renderSample(host) {
  let rep;
  try { rep = await (await fetch("/static/sample-report.json")).json(); } catch { host.closest("figure").hidden = true; return; }
  const moves = rep.moves;
  const res = rep.result.replace("-", "–");
  const boardHost = el("div", { class: "sample-board" });
  const board = new Board(boardHost, { fen: moves[0].fen, orientation: rep.you || "white", coords: false, followCoords: false, draw: false,
    label: "Sample game board" });
  const note = el("p", { class: "sample-note small", "aria-live": "polite" });
  let cur = 0;
  const after = (i) => (moves[i + 1] ? moves[i + 1].fen : rep.final_fen || moves[i].fen);
  function show(n) {
    cur = Math.max(0, Math.min(moves.length, n));
    const m = cur > 0 ? moves[cur - 1] : null;
    if (m && BAD.includes(m.class)) {
      board.set({ fen: m.fen, lastMove: m.ply > 0 ? moves[m.ply - 1].uci : null, arrows: [uciArrow(m.uci, "you"), uciArrow(m.best, "coach")].filter(Boolean) });
    } else board.set({ fen: m ? after(m.ply) : moves[0].fen, lastMove: m ? m.uci : null, arrows: [] });
    graph.mark(cur);
    for (const b of chips) b.setAttribute("aria-pressed", String(Number(b.dataset.ply) + 1 === cur));
    if (!m) { fill(note, "Start position."); return; }
    const [sym, label, tone] = CLASS[m.class];
    fill(note, el("b", { class: "mono", text: `${m.move_number}${m.color === "white" ? "." : "…"}${m.san}` }), sym ? el("b", { class: `mono sym ${tone}`, text: sym }) : null,
      " ", el("span", { class: `cls-word ${tone}`, text: label }),
      BAD.includes(m.class) && m.best_san ? el("span", { class: "muted", text: ` · better was ${m.best_san}` }) : "");
  }
  const graph = evalGraph(rep, { you: rep.you, pro: false, onPick: show });
  graph.node.addEventListener("keydown", (e) => {
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") { show(cur + (e.key === "ArrowRight" ? 1 : -1)); e.preventDefault(); }
  });
  const chips = rep.key_moments.map((k) => {
    const [sym, , tone] = CLASS[k.class];
    return el("button", { type: "button", class: "sample-chip", "data-ply": String(k.ply), "aria-pressed": "false", onclick: () => show(k.ply + 1) },
      el("span", { class: "mono", text: k.label }), el("b", { class: `mono sym ${tone}`, text: sym }));
  });
  const cell = (c) => el("div", { class: `rc-cell${rep.you === c ? " you" : ""}` },
    el("dt", {}, el("span", { class: "rc-name", text: c === rep.you ? "You" : "Opponent" }), el("span", { class: "rc-side", text: c === "white" ? "White" : "Black" })),
    el("dd", { class: "rc-figure" }, el("span", { class: "figure", text: `${rep.sides[c].accuracy}%` }),
      el("span", { class: `grade small g-${rep.sides[c].grade}`, text: rep.sides[c].grade })));
  fill(host,
    el("p", { class: "sample-verdict", text: rep.verdict }),
    el("dl", { class: "rc-scores" },
      el("div", { class: "rc-cell rc-result" }, el("dt", { text: "Result" }), el("dd", { class: "rc-figure" }, el("span", { class: "figure", text: res }))),
      cell("white"), cell("black")),
    graph.node,
    el("div", { class: "sample-row" }, boardHost, el("div", { class: "sample-side" }, note,
      el("p", { class: "tiny muted", text: "Key moments" }), el("div", { class: "sample-chips" }, ...chips))));
  const worst = rep.key_moments.reduce((a, b) => (b.loss > a.loss ? b : a), rep.key_moments[0]);
  show(worst ? worst.ply + 1 : 0);
}

let sampleDrawn = false;
export function showLanding() {
  $("#app").hidden = true;
  $("#landing").hidden = false;
  if (!sampleDrawn) { // the sample and the plan table only for visitors who see the landing page
    sampleDrawn = true;
    renderSample($("#sampleCard"));
    const rerender = () => renderPlans($("#lpPlans"), { onChoose: () => openAuth("signup"), quiet: true });
    bindCurrency(rerender);
    rerender();
  }
  document.title = "Plateau Breaker: see what is holding your rating back";
}

// ---------------- the instant preview ----------------
const PLATFORM_NAMES = { lichess: "Lichess", chesscom: "Chess.com" };
let platform = "lichess";

function bindPlatform() {
  const host = $("#pvPlatform");
  // Chess.com can be switched off server-side; then the preview is Lichess only and the choice is hidden.
  const on = (S.config.platforms || ["lichess"]).includes("chesscom");
  host.hidden = !on;
  if (!on) return;
  for (const b of host.querySelectorAll("[data-platform]")) {
    b.onclick = () => {
      platform = b.dataset.platform;
      for (const o of host.querySelectorAll("[data-platform]")) o.setAttribute("aria-pressed", String(o === b));
      $("#pvLabel").textContent = `Your ${PLATFORM_NAMES[platform]} username`;
      $("#pvUser").focus();
    };
  }
}

async function runPreview() {
  const name = $("#pvUser").value.trim();
  const err = $("#pvErr");
  showErr(err, "");
  if (!name) { showErr(err, `Type your ${PLATFORM_NAMES[platform]} username first.`); $("#pvUser").focus(); return; }
  const btn = $("#pvBtn");
  btn.disabled = true;
  const out = $("#previewOut");
  out.hidden = false;
  const stage = el("p", { class: "muted", text: "Finding your games…" });
  const bar = el("i");
  out.replaceChildren(el("div", { class: "progress" }, el("h2", { text: `Diagnosing ${name}` }), stage, el("div", { class: "bar" }, bar)));
  out.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start" });
  track("Preview started");
  try {
    const { job_id } = await post("/api/preview", { username: name, platform });
    let j;
    for (let i = 0; i < 240; i++) {
      j = await api(`/api/preview/${job_id}`);
      if (j.state === "done" || j.state === "error") break;
      stage.textContent = j.state === "queued" && j.position ? `Waiting for a free engine (${j.position} ahead of you)…`
        : j.total ? `${j.stage}: ${j.done} of ${j.total} games` : `${j.stage}…`;
      bar.style.width = j.total ? `${Math.max(4, (100 * j.done) / j.total)}%` : "6%";
      await sleep(1000);
    }
    if (j.state !== "done") throw new Error(j.error || "That took too long. Please try again in a minute.");
    renderPreview(out, j);
    track("Preview done");
  } catch (e) {
    out.replaceChildren(el("div", { class: "notice", role: "alert" }, e.message));
  } finally {
    btn.disabled = false;
  }
}

function renderPreview(out, p) {
  const m = p.moment;
  S.pendingLink = { platform: p.platform, handle: p.handle, game_id: m && m.game_id, opponent: m && m.opponent };
  // One primary action at a time: the hero's button steps back once there's a report card to go and get.
  const heroBtn = $("#pvBtn");
  heroBtn.className = "btn quiet";
  heroBtn.textContent = "Try another username";
  const dnaHost = el("div");
  renderDNA(dnaHost, p.dna, { annotate: true, animate: true });
  const findings = el("ol", { class: "findings" }, ...(p.insights.length ? p.insights.map((i) =>
    el("li", { class: i.severity }, el("b", { class: "stat figure", text: i.stat }), el("span", { text: i.text })))
    : [el("li", {}, el("span"), el("span", { class: "muted", text: "Play a few more rated games for findings." }))]));
  let momentBlock = null;
  if (m) {
    momentBlock = el("div", { class: "stack" }, el("h3", { text: "Your costliest moment" }),
      el("div", { class: "row", style: { alignItems: "flex-start", gap: "16px" } },
        miniBoard(m.fen, { arrows: [uciArrow(m.played_uci, "you"), uciArrow(m.best_uci, "coach")].filter(Boolean),
          marks: uciMarks(m.played_uci, "you"), large: true, label: `You played ${m.you_played}; the engine preferred ${m.engine_best}` }),
        el("div", { class: "stack small" },
          el("p", {}, `Move ${m.move} against ${m.opponent}: you lost `, el("b", { text: `${m.winning_chances_lost}%` }), " of your winning chances."),
          el("p", {}, "Blue is what you played. The engine's move, in red: ", el("b", { text: m.engine_best }), m.engine_line && m.engine_line.length > 1 ? ` (${m.engine_line.join(" ")})` : ""))));
  }
  const cta = el("div", { class: "preview-cta" },
    el("p", {}, el("b", { text: m ? `Next: the full report card for your game against ${m.opponent}. ` : "Next: your full report cards. " }),
      "Every move checked, the moments that decided it, and what to take into your next game. Free; about a minute after you sign up."),
    el("button", { class: "btn", type: "button", text: m ? "Get the full report card for this game" : "Create a free account", onclick: () => openAuth("signup") }));
  out.replaceChildren(
    el("div", { class: "section-head" }, el("div", {}, el("h2", { text: `${p.handle}: rated ${p.dna.base_rating}` }),
      el("p", { class: "muted small", text: `A first look at your last ${p.games} rated games, at a quick engine depth.` }))),
    cta,
    el("div", { class: "preview-grid" }, el("div", { class: "sheet" }, dnaHost),
      el("div", { class: "sheet stack-lg" }, el("div", { class: "stack" }, el("h3", { text: "What's costing you" }), findings), momentBlock)));
}

// ---------------- sign in / sign up / forgot / reset ----------------
let mode = "login";
let resetToken = null;
const COPY = {
  login: { title: "Sign in", btn: "Sign in", swap: "Create a free account", pw: true, ac: "current-password" },
  signup: { title: "Create your free account", btn: "Create account", swap: "I already have an account", pw: true, ac: "new-password" },
  forgot: { title: "Reset your password", btn: "Send reset link", swap: "Back to sign in", pw: false },
  reset: { title: "Choose a new password", btn: "Save new password", swap: "Back to sign in", pw: true, ac: "new-password", noEmail: true },
};

export function openAuth(m = "login") {
  setMode(m);
  // The dialog focuses its [autofocus] field when it opens: no timers that could steal focus mid-typing.
  $("#authEmail").toggleAttribute("autofocus", !COPY[m].noEmail);
  $("#authPw").toggleAttribute("autofocus", !!COPY[m].noEmail);
  openDialog($("#authDlg"));
}

function setMode(m) {
  mode = m;
  const c = COPY[m];
  $("#authTitle").textContent = c.title;
  $("#authBtn").textContent = c.btn;
  $("#authSwap").textContent = c.swap;
  $("#authPwField").hidden = !c.pw;
  $("#authEmail").closest(".field").hidden = !!c.noEmail;
  $("#authPw").autocomplete = c.ac || "off";
  $("#pwHint").hidden = !(m === "signup" || m === "reset");
  $("#authForgot").hidden = m !== "login";
  $("#authTermsField").hidden = !(m === "signup" && S.config.require_terms !== false);
  const lead = $("#authLead");
  lead.hidden = !(m === "forgot" || (m === "signup" && S.pendingLink));
  lead.textContent = m === "forgot" ? "Enter your email and we'll send a link to choose a new password."
    : S.pendingLink ? (S.pendingLink.game_id
      ? `We'll link ${S.pendingLink.handle} and open the report card for your game against ${S.pendingLink.opponent} as soon as you're in.`
      : `We'll link ${S.pendingLink.handle} and analyse your games as soon as you're in.`) : "";
  showErr($("#authErr"), "");
  $("#authOk").hidden = true;
}

function bindAuthForm() {
  $("#authSwap").addEventListener("click", () => setMode(mode === "login" ? "signup" : "login"));
  $("#authForgot").addEventListener("click", () => setMode("forgot"));
  $("#authForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("#authBtn");
    showErr($("#authErr"), "");
    btn.disabled = true;
    try {
      const email = $("#authEmail").value.trim(), password = $("#authPw").value;
      if (mode === "forgot") {
        const r = await post("/api/auth/forgot", { email });
        const ok = $("#authOk"); ok.textContent = r.message; ok.hidden = false;
        return;
      }
      if (mode === "reset") {
        await post("/api/auth/reset", { token: resetToken, password });
        resetToken = null;
        $("#authDlg").close();
        toast("Password changed. You're signed in.");
        await onSignedIn();
        return;
      }
      if (mode === "signup" && S.config.require_terms !== false && !$("#authTerms").checked) {
        throw new Error("Please confirm you're 18 or over and agree to the Terms and Privacy Policy.");
      }
      await post(`/api/auth/${mode}`, mode === "signup" ? { email, password, accept_terms: $("#authTerms").checked } : { email, password });
      track(mode === "signup" ? "Signup" : "Login");
      $("#authDlg").close();
      $("#authPw").value = "";
      await onSignedIn({ fresh: mode === "signup" });
    } catch (err) {
      showErr($("#authErr"), err.message);
    } finally {
      btn.disabled = false;
    }
  });
}

/**
 * Email links: /verify#token=… and /reset#token=…. The token sits in the #fragment, which browsers never send
 * to the server, and it's taken out of the address bar at once (before analytics loads), so it can't leak
 * into logs, analytics or the Referer header.
 */
let pendingEmailLink = null;
export function captureEmailLink() {
  if (location.pathname === "/join") { // a coach's invite: keep it for after sign-in, out of the address bar
    const t = new URLSearchParams(location.hash.replace(/^#/, "")).get("coach");
    try { if (t) sessionStorage.setItem("pb:coachInvite", t); } catch { /* storage blocked */ }
    history.replaceState(null, "", "/");
    return;
  }
  if (location.pathname !== "/reset" && location.pathname !== "/verify") return;
  const fromHash = new URLSearchParams(location.hash.replace(/^#/, "")).get("token");
  const fromQuery = new URLSearchParams(location.search).get("token"); // links from older emails
  pendingEmailLink = { kind: location.pathname.slice(1), token: fromHash || fromQuery };
  history.replaceState(null, "", "/");
}

/** Act on a captured email link. Returns true if a reset dialog was opened. */
export async function handleEmailLinks() {
  const link = pendingEmailLink;
  pendingEmailLink = null;
  if (!link) return false;
  const token = link.token;
  if (link.kind === "reset") {
    resetToken = token;
    if (!token) { toast("That reset link is incomplete. Ask for a new one."); return false; }
    openAuth("reset");
    return true;
  }
  if (link.kind === "verify") {
    if (!token) return false;
    try {
      const r = await post("/api/auth/verify", { token });
      toast(r.message);
      track("Email verified");
    } catch (e) { toast(e.message, 6000); }
  }
  return false;
}

/** True when a coach's invite is waiting for the visitor to sign in. */
export function hasCoachInvite() {
  try { return !!sessionStorage.getItem("pb:coachInvite"); } catch { return false; }
}
