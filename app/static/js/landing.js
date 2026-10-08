// Signed-out experience: hero with a sample Rating DNA, the no-signup preview, sign in / sign up,
// forgotten password, password reset and email verification pages.

import { $, S, api, el, openDialog, post, showErr, sleep, toast, track } from "./core.js";
import { Board, miniBoard, uciArrow, uciMarks } from "./board.js";
import { renderDNA } from "./dna.js";
import { bindCurrency, renderPlans } from "./plans.js";

const SAMPLE = {
  base_rating: 1850, rating_is_estimated: false, games: 214, overall_accuracy: 86.9,
  skills: [
    { key: "openings", label: "Openings", rating: 1892, accuracy: 91.2, moves: 1430, low_confidence: false },
    { key: "middlegame", label: "Middlegame play", rating: 1822, accuracy: 84.6, moves: 4120, low_confidence: false },
    { key: "endgames", label: "Endgames", rating: 1869, accuracy: 88.4, moves: 1710, low_confidence: false },
    { key: "tactics", label: "Tactics", rating: 1834, accuracy: 85.6, moves: 640, low_confidence: false },
    { key: "time", label: "Time management", rating: 1870, accuracy: 88.5, moves: 410, low_confidence: false },
    { key: "converting", label: "Converting wins", rating: 1825, accuracy: 84.8, moves: 520, low_confidence: false },
    { key: "defending", label: "Defending", rating: 1861, accuracy: 87.6, moves: 380, low_confidence: false },
  ],
};

let onSignedIn = null;
export function initLanding(signedInCallback) {
  onSignedIn = signedInCallback;
  renderDNA($("#heroDna"), SAMPLE, { annotate: true, animate: true });
  new Board($("#pitchBoard"), { fen: "rnbqkb1r/1p2pppp/p2p1n2/8/3NP3/2N5/PPP2PPP/R1BQKB1R w KQkq - 0 6", coords: true,
    arrows: [{ from: "f1", to: "e2", kind: "you" }, { from: "c1", to: "e3", kind: "coach" }],
    label: "Najdorf position: you played Be2, your preparation says Be3" });
  const rerender = () => renderPlans($("#lpPlans"), { onChoose: () => openAuth("signup") });
  bindCurrency(rerender);
  rerender();
  $("#previewForm").addEventListener("submit", (e) => { e.preventDefault(); runPreview(); });
  document.querySelectorAll("[data-auth]").forEach((b) => b.addEventListener("click", () => openAuth(b.dataset.auth)));
  bindAuthForm();
}

export function showLanding() {
  $("#app").hidden = true;
  $("#landing").hidden = false;
  document.title = "Plateau Breaker: find what's costing you rating";
}

// ---------------- the instant preview ----------------
async function runPreview() {
  const name = $("#pvUser").value.trim();
  const err = $("#pvErr");
  showErr(err, "");
  if (!name) { showErr(err, "Type your Lichess username first."); $("#pvUser").focus(); return; }
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
    const { job_id } = await post("/api/preview", { username: name, platform: "lichess" });
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
  S.pendingLink = { platform: p.platform, handle: p.handle };
  const dnaHost = el("div");
  renderDNA(dnaHost, p.dna, { annotate: true, animate: true });
  const findings = el("ol", { class: "findings" }, ...(p.insights.length ? p.insights.map((i) =>
    el("li", { class: i.severity }, el("b", { class: "stat figure", text: i.stat }), el("span", { text: i.text })))
    : [el("li", {}, el("span"), el("span", { class: "muted", text: "Play a few more rated games for findings." }))]));
  let momentBlock = null;
  if (p.moment) {
    const m = p.moment;
    momentBlock = el("div", { class: "stack" }, el("h3", { text: "Your costliest moment" }),
      el("div", { class: "row", style: { alignItems: "flex-start", gap: "16px" } },
        miniBoard(m.fen, { arrows: [uciArrow(m.played_uci, "you"), uciArrow(m.best_uci, "coach")].filter(Boolean),
          marks: uciMarks(m.played_uci, "you"), large: true, label: `You played ${m.you_played}; the engine preferred ${m.engine_best}` }),
        el("div", { class: "stack small" },
          el("p", {}, `Move ${m.move} against ${m.opponent}: you lost `, el("b", { text: `${m.winning_chances_lost}%` }), " of your winning chances."),
          el("p", {}, "Blue is what you played. The engine's move, in red: ", el("b", { text: m.engine_best }), m.engine_line && m.engine_line.length > 1 ? ` (${m.engine_line.join(" ")})` : ""))));
  }
  out.replaceChildren(
    el("div", { class: "section-head" }, el("div", {}, el("h2", { text: `${p.handle}: rated ${p.dna.base_rating}` }),
      el("p", { class: "muted small", text: `From your last ${p.games} rated games, at a quick engine depth. A full account analyses up to 500 of your games more deeply.` }))),
    el("div", { class: "preview-grid" }, el("div", { class: "sheet" }, dnaHost),
      el("div", { class: "sheet stack-lg" }, el("div", { class: "stack" }, el("h3", { text: "What's costing you" }), findings), momentBlock)),
    el("div", { class: "preview-cta" },
      el("p", {}, el("b", { text: "Get the full picture: " }), "all your games, puzzles from your own mistakes, a weekly plan and the AI coach."),
      el("button", { class: "btn", type: "button", text: "Create a free account", onclick: () => openAuth("signup") })));
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
  const lead = $("#authLead");
  lead.hidden = !(m === "forgot" || (m === "signup" && S.pendingLink));
  lead.textContent = m === "forgot" ? "Enter your email and we'll send a link to choose a new password."
    : S.pendingLink ? `We'll link ${S.pendingLink.handle} and analyse your games as soon as you're in.` : "";
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
      await post(`/api/auth/${mode}`, { email, password });
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
