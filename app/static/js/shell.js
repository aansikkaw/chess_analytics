// Signed-in shell: routing between the four jobs, the account switcher and sync, the account panel, banners.

import { $, $$, PLATFORM, S, account, ago, api, del, el, fill, isCoach, isPro, openDialog, patch, post, showErr, sleep, store, toast, track, upload } from "./core.js";
import * as diagnose from "./views/diagnose.js";
import * as train from "./views/train.js";
import * as prepare from "./views/prepare.js";
import * as coach from "./views/coach.js";
import * as students from "./views/students.js";
import { renderPlans, bindCurrency } from "./plans.js";
import { openSupport } from "./support.js";

const AREAS = {
  diagnose: { title: "Diagnose", view: diagnose },
  train: { title: "Train", view: train },
  prepare: { title: "Prepare", view: prepare },
  coach: { title: "Coach", view: coach },
  students: { title: "Students", view: students },
  plans: { title: "Plans", view: { render: renderPlansPage } },
};
let signOutCb = null;

export async function startApp({ fresh = false } = {}, onSignOut) {
  signOutCb = onSignOut || signOutCb;
  S.me = await api("/api/me");
  if (await joinPendingInvite()) S.me = await api("/api/me");
  $("#landing").hidden = true;
  $("#app").hidden = false;
  const saved = store.get("pb:account");
  const ids = S.me.accounts.map((a) => a.id);
  const own = S.me.accounts.filter((a) => a.role !== "student");
  S.accountId = ids.includes(saved) ? saved : (own.find((a) => a.games) || own[0] || S.me.accounts[0] || {}).id || null;
  renderTop();
  renderBanners();
  bindShellOnce();
  // A username tried in the preview is linked only for a brand-new account, never added to an existing one.
  if (S.pendingLink && fresh) await linkPending();
  S.pendingLink = null;
  if (!location.hash || location.hash === "#") location.hash = "#/diagnose";
  else route();
  const running = S.me.accounts.find((a) => a.running_job);
  if (running) pollJob(running.running_job, running.id);
}

/** A coach's invite link (/join#coach=…) opened before signing in: join their squad now. */
async function joinPendingInvite() {
  let token = null;
  try { token = sessionStorage.getItem("pb:coachInvite"); sessionStorage.removeItem("pb:coachInvite"); } catch { /* storage blocked */ }
  if (!token) return false;
  try {
    const r = await post("/api/coach/join", { token });
    toast(r.message, 7000);
    track("Joined coach");
    return true;
  } catch (e) { toast(e.message, 7000); return false; }
}

async function linkPending() {
  const p = S.pendingLink;
  S.pendingLink = null;
  try {
    const a = await post("/api/accounts", { platform: p.platform, handle: p.handle });
    await refreshMe();
    selectAccount(a.id, { silent: true });
    track("Account linked", { platform: p.platform });
    startSync(a);
  } catch (e) { toast(e.message, 6000); }
}

export async function refreshMe() {
  S.me = await api("/api/me");
  renderTop();
  renderBanners();
}

// ---------------- routing ----------------
export function route() {
  const [areaKey, sub] = (location.hash.replace(/^#\/?/, "") || "diagnose").split("/");
  const area = AREAS[areaKey] ? areaKey : "diagnose";
  for (const a of $$(".rail a[data-area]")) {
    if (a.dataset.area === area) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  const main = $("#view");
  document.title = `${AREAS[area].title} · Plateau Breaker`;
  const acct = account();
  const free = area === "plans" || area === "students"; // pages that don't need a chess account selected
  if (!free && !acct) { renderOnboarding(main); return; }
  if (!free && !acct.games) { renderWaiting(main, acct); return; }
  AREAS[area].view.render(main, sub);
  main.focus({ preventScroll: true });
}
window.addEventListener("hashchange", () => { if (!$("#app").hidden) { window.scrollTo(0, 0); route(); } });

let bound = false;
function bindShellOnce() {
  if (bound) return;
  bound = true;
  $("#acctSwitch").addEventListener("click", openAccounts);
  $("#meBtn").addEventListener("click", openMe);
}

// ---------------- top bar ----------------
export function renderTop() {
  const a = account();
  $("#acctWho").textContent = a ? a.handle : "Link an account";
  $("#acctPlat").textContent = a ? `${PLATFORM[a.platform]}${a.role === "student" ? " · student" : ""}` : "";
  $("#planTag").hidden = !isPro();
  $("#planTag").textContent = isCoach() ? "Coach" : S.me.plan_expires_at ? "Event Pass" : "Pro";
  $("#studentsLink").hidden = !isCoach();
  $("#meBtn").textContent = (S.me.email[0] || "?").toUpperCase();
  if (!S.job) $("#syncState").textContent = a ? (a.last_sync_error ? "Last sync failed" : a.platform === "pgn" || a.platform === "demo" ? `${a.games} games` : ago(a.last_synced_at)) : "";
}

export function setDueCount(n) {
  const b = $("#dueBadge");
  b.textContent = n > 99 ? "99+" : String(n || "");
  b.hidden = !n;
}

function renderBanners() {
  const host = $("#banners");
  const out = [];
  if (!S.me.email_verified) {
    const btn = el("button", { class: "link", type: "button", text: "Send a new link", onclick: async () => {
      btn.disabled = true;
      try { toast((await post("/api/auth/verify/resend")).message); } catch (e) { toast(e.message); } finally { btn.disabled = false; }
    } });
    out.push(el("div", { class: "banner", role: "status" }, el("span", {}, "Confirm your email: we sent a link to ", el("b", { text: S.me.email }), "."), btn));
  }
  if (S.me.plan_expires_at) {
    const days = Math.max(0, Math.ceil((S.me.plan_expires_at * 1000 - Date.now()) / 86_400_000));
    out.push(el("div", { class: "banner" }, `Your Event Pass gives you Pro for ${days} more day${days === 1 ? "" : "s"}.`));
  }
  host.replaceChildren(...out);
}

// ---------------- account switching, linking, syncing ----------------
export function selectAccount(id, { silent = false } = {}) {
  S.accountId = id || null;
  store.set("pb:account", S.accountId);
  S.chat = []; S.chatLog = []; S.citations = {}; S.profile = null; S.puzzles = [];
  renderTop();
  if (!silent) route();
}

function linkForm({ onLinked, compact = false } = {}) {
  const platforms = (S.config && S.config.platforms) || ["lichess"];
  const select = el("select", { id: compact ? "lnkPlat2" : "lnkPlat" },
    ...platforms.map((p) => el("option", { value: p, text: PLATFORM[p] })),
    el("option", { value: "pgn", text: "ChessBase / PGN file (over-the-board games)" }),
    el("option", { value: "demo", text: "Demo games (try it out)" }));
  const handle = el("input", { type: "text", id: compact ? "lnkHandle2" : "lnkHandle", autocomplete: "off", autocapitalize: "off", spellcheck: "false", placeholder: "Your username", maxlength: "60" });
  const hint = el("span", { class: "tiny faint", text: "We only read your public games." });
  const err = el("p", { class: "err", role: "alert", hidden: true });
  const btn = el("button", { class: "btn", type: "submit", text: "Link and analyse" });
  const handleField = el("div", { class: "field" }, el("label", { for: handle.id, text: "Username" }), handle, hint);
  select.addEventListener("change", () => {
    handleField.hidden = select.value === "demo";
    handle.placeholder = select.value === "pgn" ? "Your name as in ChessBase, e.g. Sikka, Aanya" : "Your username";
    hint.textContent = select.value === "pgn" ? "Then upload a .pgn exported from ChessBase." : "We only read your public games.";
    btn.textContent = select.value === "pgn" ? "Link" : "Link and analyse";
  });
  const form = el("form", { class: "stack", novalidate: true },
    el("div", { class: "field" }, el("label", { for: select.id, text: "Where do you play?" }), select), handleField, btn, err);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showErr(err, "");
    btn.disabled = true;
    try {
      const a = await post("/api/accounts", { platform: select.value, handle: handle.value.trim() });
      track("Account linked", { platform: a.platform });
      handle.value = "";
      await refreshMe();
      selectAccount(a.id, { silent: true });
      if (a.platform !== "pgn") startSync(a);
      onLinked && onLinked(a);
      route();
    } catch (ex) { showErr(err, ex.upgrade ? `${ex.message} See Plans.` : ex.message); }
    finally { btn.disabled = false; }
  });
  return form;
}

function renderOnboarding(main) {
  main.replaceChildren(el("div", { class: "page-head" }, el("div", {}, el("h1", { text: "Link your chess account" }),
    el("p", { class: "sub", text: "We'll download your rated games, check every move with Stockfish and build your Rating DNA." }))),
  el("div", { class: "grid-2" }, el("div", { class: "sheet" }, linkForm()),
    el("div", { class: "stack muted small" }, el("h3", { text: "Which games count?" }),
      el("p", { text: "Rated blitz, rapid and classical games by default; you can add bullet. The newest games come first, and your dashboard fills in after the first 20." }),
      el("p", { text: "Over-the-board games: export them from ChessBase as a PGN file and link a ChessBase / PGN account with your name as it appears in the file." }))));
}

function renderWaiting(main, a) {
  const live = a.platform === "lichess" || a.platform === "chesscom";
  const body = a.running_job || S.job
    ? [el("h3", { text: "Analysing your games" }), el("p", { text: "Your dashboard opens as soon as the first games are done. You can leave this page; the work carries on." })]
    : a.platform === "pgn"
      ? [el("h3", { text: `Upload games for ${a.handle}` }), el("p", { text: "Export your games from ChessBase as a .pgn file (or a .zip of several) and upload it." }), uploadForm(a)]
      : [el("h3", { text: `No games analysed for ${a.handle} yet` }), el("p", {}, live ? "Fetch your recent rated games and analyse them." : "Load the demo games."),
        el("button", { class: "btn", type: "button", text: live ? "Analyse my games" : "Load demo games", onclick: () => startSync(a) })];
  main.replaceChildren(el("div", { class: "empty stack" }, ...body));
}

function uploadForm(a) {
  const file = el("input", { type: "file", accept: ".pgn,.zip,application/zip,application/x-chess-pgn,text/plain", id: `up-${a.id}` });
  const err = el("p", { class: "err", role: "alert", hidden: true });
  const btn = el("button", { class: "btn", type: "submit", text: "Upload games" });
  const form = el("form", { class: "stack", style: { maxWidth: "460px", margin: "12px auto 0", textAlign: "left" } },
    el("div", { class: "field" }, el("label", { for: file.id, text: "PGN or ZIP file" }), file,
      el("span", { class: "tiny faint", text: `Your name must match White or Black: "${a.handle}".` })), btn, err);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showErr(err, "");
    if (!file.files[0]) { showErr(err, "Choose a file first."); return; }
    btn.disabled = true;
    try {
      const job = await upload(`/api/accounts/${a.id}/upload`, { file: file.files[0] });
      track("Sync started", { platform: "pgn" });
      pollJob(job.job_id, a.id);
      route();
    } catch (ex) { showErr(err, ex.message); }
    finally { btn.disabled = false; }
  });
  return form;
}

export async function startSync(a, opts = {}) {
  try {
    let job;
    if (a.platform === "demo") job = await post(`/api/accounts/${a.id}/pgn`, { pgn: "demo" });
    else job = await post(`/api/accounts/${a.id}/sync`, opts);
    track("Sync started", { platform: a.platform });
    pollJob(job.job_id, a.id);
    route();
  } catch (e) {
    if (e.status === 409 && a.running_job) pollJob(a.running_job, a.id);
    else toast(e.verify ? e.message : e.message, 6000);
  }
}

export async function pollJob(jobId, accountId) {
  if (S.job === jobId) return;
  S.job = jobId;
  const bar = $("#syncBar"), fill = bar.querySelector("i"), state = $("#syncState");
  bar.hidden = false;
  let shownFirst = false, refreshedAt = 0;
  for (;;) {
    let j;
    try { j = await api(`/api/jobs/${jobId}`); } catch (e) { state.textContent = e.message; break; }
    const pct = j.total ? (100 * j.done) / j.total : j.state === "done" ? 100 : 4;
    fill.style.width = `${Math.max(3, pct)}%`;
    state.textContent = j.state === "queued" && j.position ? `Waiting for the engine (${j.position} ahead)` : j.total ? `${j.stage}: ${j.done} of ${j.total}` : j.stage;
    if (shownFirst && j.state !== "done" && j.state !== "error" && j.done - refreshedAt >= 100) { // a big import: show the growing picture
      refreshedAt = j.done;
      await refreshMe().catch(() => {});
      if (S.accountId === accountId && (location.hash.split("/")[1] || "diagnose") === "diagnose") { S.profile = null; route(); }
    }
    if (j.first_results && !shownFirst && j.state !== "done") {
      shownFirst = true;
      refreshedAt = j.done || 0;
      await refreshMe();
      if (S.accountId === accountId) { S.profile = null; route(); toast("Your first results are in. The rest are still being analysed."); }
    }
    if (j.state === "error") { toast(j.error, 8000); break; }
    if (j.state === "done") {
      toast(j.new_games ? `Done: ${j.new_games} new games and ${j.puzzles_added || 0} new puzzles.` : j.drills != null ? `Prep Check done: ${j.drills} drills added.` : "You're up to date: no new games.");
      break;
    }
    await sleep(1200);
  }
  S.job = null;
  bar.hidden = true;
  fill.style.width = "0";
  await refreshMe().catch(() => {});
  if (S.accountId === accountId) { S.profile = null; route(); }
}

// About 2 seconds of engine time per game; the dashboard fills in after the first 20.
function timeEstimate(n) {
  const min = Math.max(1, Math.round((n * 2) / 60));
  return `About ${min} minute${min === 1 ? "" : "s"} for ${n} new games. Your dashboard updates as they're analysed.`;
}

function openAccounts() {
  const body = $("#accountBody");
  const render = () => {
    const item = (a) => {
      const meta = a.last_sync_error ? el("span", { class: "meta bad", text: a.last_sync_error })
        : el("span", { class: "meta", text: `${a.games} games, ${a.platform === "pgn" || a.platform === "demo" ? "imported" : ago(a.last_synced_at)}${a.auto_sync ? ", auto-sync on" : ""}` });
      return el("button", { class: "acct-item", type: "button", "aria-pressed": String(a.id === S.accountId),
        onclick: () => { selectAccount(a.id); render(); } },
      el("span", {}, el("b", { text: a.handle }), " ", el("span", { class: "faint small", text: PLATFORM[a.platform] })),
      a.id === S.accountId ? el("span", { class: "tag ok", text: "Selected" }) : el("span"), meta);
    };
    const mine = S.me.accounts.filter((x) => x.role !== "student");
    const roster = S.me.accounts.filter((x) => x.role === "student");
    const list = el("div", { class: "acct-list" }, ...mine.map(item));
    const a = account();
    fill(body,
      mine.length ? list : el("p", { class: "muted", text: "No accounts linked yet." }),
      roster.length ? el("div", { class: "stack" }, el("h3", { text: "Your students" }),
        el("p", { class: "small muted" }, "Pick one to open their full analysis. ", el("a", { href: "#/students", onclick: () => $("#accountDlg").close(), text: "Manage students" })),
        el("div", { class: "acct-list" }, ...roster.map(item))) : null,
      a ? syncPanel(a, render) : null,
      el("div", { class: "stack" }, el("h3", { text: "Link another account" }),
        el("p", { class: "small muted", text: `Your plan allows ${S.me.limits.max_accounts} linked accounts.` }),
        linkForm({ compact: true, onLinked: () => $("#accountDlg").close() })));
  };
  render();
  openDialog($("#accountDlg"));
}

function syncPanel(a, rerender) {
  const live = a.platform === "lichess" || a.platform === "chesscom";
  const panel = el("div", { class: "stack" }, el("h3", { text: `Sync ${a.handle}` }));
  if (live) {
    const tcs = ["bullet", "blitz", "rapid", "classical"].map((t) => {
      const cb = el("input", { type: "checkbox", value: t });
      cb.checked = a.time_classes.includes(t);
      return el("label", { class: "check" }, cb, t[0].toUpperCase() + t.slice(1));
    });
    const capN = S.me.limits.max_games_per_sync;
    let count = Math.min(Number(store.get("pb:syncCount")) || 200, capN);
    const picks = [50, 100, 200, 300, 500].map((n) => {
      const b = el("button", { type: "button", class: "seg-btn", "aria-pressed": String(n === count), text: String(n),
        onclick: () => {
          if (n > capN) { toast(`Up to ${capN} games per sync on your plan. Pro analyses up to 500.`); return; }
          count = n; store.set("pb:syncCount", n);
          for (const x of picks) x.setAttribute("aria-pressed", String(Number(x.textContent) === n));
          estimate.textContent = timeEstimate(n);
        } });
      if (n > capN) b.classList.add("locked");
      return b;
    });
    const estimate = el("span", { class: "tiny faint", text: timeEstimate(count) });
    const auto = el("input", { type: "checkbox" });
    auto.checked = a.auto_sync;
    auto.addEventListener("change", async () => {
      try { await patch(`/api/accounts/${a.id}`, { auto_sync: auto.checked }); await refreshMe(); }
      catch (e) { auto.checked = false; toast(e.upgrade ? "Auto-sync is part of Pro." : e.message); }
    });
    panel.append(
      el("fieldset", {}, el("legend", { text: "Time controls" }), el("div", { class: "row" }, ...tcs)),
      el("fieldset", {}, el("legend", { text: "How many recent games to analyse" }),
        el("div", { class: "seg", role: "group", "aria-label": "Number of games" }, ...picks), estimate,
        el("p", { class: "tiny faint", text: "More games give a steadier Rating DNA. Only games you haven't analysed yet are added." })),
      el("div", { class: "row" }, el("button", { class: "btn", type: "button", text: "Sync now", onclick: () => {
        const classes = tcs.map((l) => l.querySelector("input")).filter((c) => c.checked).map((c) => c.value);
        if (!classes.length) { toast("Pick at least one time control."); return; }
        startSync(a, { time_classes: classes, max_games: count });
        $("#accountDlg").close();
      } })),
      el("label", { class: "check" }, auto, "Sync new games automatically", isPro() ? null : el("span", { class: "tag pro", text: "Pro" })));
  } else if (a.platform === "pgn") {
    panel.append(uploadForm(a));
  } else {
    panel.append(el("button", { class: "btn quiet", type: "button", text: "Reload demo games", onclick: () => { startSync(a); $("#accountDlg").close(); } }));
  }
  const rm = el("button", { class: "link quiet small", type: "button", text: `Remove ${a.handle} and its analysis`, onclick: async () => {
    if (rm.dataset.confirm !== "1") { rm.dataset.confirm = "1"; rm.textContent = "Click again to remove it for good"; return; }
    await del(`/api/accounts/${a.id}`);
    await refreshMe();
    selectAccount((S.me.accounts[0] || {}).id || null);
    rerender();
  } });
  panel.append(rm);
  return panel;
}

// ---------------- my account ----------------
function openMe() {
  const body = $("#meBody");
  const digest = el("input", { type: "checkbox" });
  digest.checked = S.me.digest;
  digest.addEventListener("change", async () => {
    try { await patch("/api/me", { digest: digest.checked }); S.me.digest = digest.checked; toast(digest.checked ? "Weekly email on." : "Weekly email off."); }
    catch (e) { digest.checked = !digest.checked; toast(e.message); }
  });
  const pw = el("input", { type: "password", id: "delPw", autocomplete: "current-password" });
  const delErr = el("p", { class: "err", role: "alert", hidden: true });
  const delForm = el("form", { class: "stack" },
    el("div", { class: "field" }, el("label", { for: "delPw", text: "Type your password to confirm" }), pw),
    el("button", { class: "btn danger", type: "submit", text: "Delete my account and data" }), delErr);
  delForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await del("/api/me", { password: pw.value }); $("#meDlg").close(); toast("Your account and all its data are deleted."); signOutCb && signOutCb(); }
    catch (ex) { showErr(delErr, ex.message); }
  });
  const expires = S.me.plan_expires_at ? ` until ${new Date(S.me.plan_expires_at * 1000).toLocaleDateString()}` : "";
  fill(body,
    el("div", { class: "stack" }, el("p", {}, el("b", { text: S.me.email })),
      el("p", { class: "small" }, S.me.email_verified ? el("span", { class: "ok-text", text: "Email confirmed" }) : el("span", { class: "muted", text: "Email not confirmed yet" })),
      el("p", { class: "small" }, `Plan: ${S.me.plan_name}${expires}. `, el("a", { href: "#/plans", onclick: () => $("#meDlg").close(), text: "See plans" })),
      el("p", { class: "small muted", text: `Today: ${S.me.usage_today.coach} of ${S.me.limits.coach_messages_per_day} coach questions` +
        (S.me.limits.scouts_per_day ? `, ${S.me.usage_today.scout} of ${S.me.limits.scouts_per_day} scouting reports.` : ".") })),
    S.me.coaches && S.me.coaches.length ? el("div", { class: "stack" }, el("h3", { text: "Your coach" }),
      ...S.me.coaches.map((c) => el("p", { class: "small" }, `You're in ${c.coach_email}'s squad${S.me.sponsored_by ? ", which gives you Pro" : ""}. `,
        el("button", { class: "link quiet small", type: "button", text: "Leave the squad", onclick: async (e) => {
          const btn = e.currentTarget;
          if (btn.dataset.confirm !== "1") { btn.dataset.confirm = "1"; btn.textContent = "Click again to leave"; return; }
          try { await del(`/api/me/coaching/${c.student_id}`); await refreshMe(); $("#meDlg").close(); toast("You've left the squad."); }
          catch (ex) { toast(ex.message); }
        } })))) : null,
    el("div", { class: "stack" }, el("h3", { text: "Email" }), el("label", { class: "check" }, digest, "Send me a weekly summary of my games")),
    S.me.is_admin ? emailSetupPanel() : null,
    el("div", { class: "stack" }, el("h3", { text: "Help" }),
      el("div", { class: "row" }, el("button", { class: "btn quiet small", type: "button", text: "Contact support", onclick: () => { $("#meDlg").close(); openSupport(); } }),
        el("a", { class: "btn quiet small", href: "/status", text: "Service status" }))),
    el("div", { class: "row" }, el("button", { class: "btn quiet", type: "button", text: "Sign out", onclick: async () => {
      await post("/api/auth/logout").catch(() => {}); $("#meDlg").close(); signOutCb && signOutCb();
    } })),
    el("details", { class: "stack" }, el("summary", { class: "small muted", text: "Delete account" }),
      el("p", { class: "small muted", text: "This deletes your account, linked accounts, analysed games, puzzles, repertoires and knowledge bundles. It can't be undone." }), delForm));
  openDialog($("#meDlg"));
}

// Owner only: how this server sends email, what's wrong with the settings, and a test button.
function emailSetupPanel() {
  const box = el("div", { class: "stack owner-panel" }, el("h3", { text: "Email sending (owner only)" }), el("p", { class: "small muted", text: "Checking…" }));
  api("/api/admin/email").then((info) => renderEmailSetup(box, info)).catch((e) => fill(box, box.firstChild, el("p", { class: "err", text: e.message })));
  return box;
}

function renderEmailSetup(box, info, result = null) {
  const warnings = [...(info.warnings || [])];
  if (info.configured && info.public_url !== location.origin && !/localhost|127\.0\.0\.1/.test(info.public_url)) {
    warnings.push(`You're on ${location.origin}, but links in emails open ${info.public_url}. If people use this address, set PUBLIC_URL to it.`);
  }
  const last = info.last;
  const btn = el("button", { class: "btn quiet small", type: "button", text: info.configured ? "Send me a test email" : "Save a test email to outbox/" });
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "Sending…";
    try {
      const r = await post("/api/admin/email/test");
      renderEmailSetup(box, r, r);
    } catch (e) {
      renderEmailSetup(box, info, { ok: false, error: e.message });
    }
  });
  fill(box, el("h3", { text: "Email sending (owner only)" }),
    info.configured
      ? el("p", { class: "small" }, "Sending through ", el("b", { text: info.via }), " as ", el("b", { text: info.from }), ".")
      : el("p", { class: "small" }, "Not set up: emails are saved in the ", el("code", { text: "outbox/" }),
        " folder instead. Add the email secrets from the README (“Turn on real email”), then restart the app."),
    el("p", { class: "small muted" }, "Links in emails open ", el("code", { text: info.public_url })),
    last && !result ? el("p", { class: `small ${last.ok ? "ok-text" : "err"}`, text: last.ok ? `Last email went out ${new Date(last.at * 1000).toLocaleString()}.`
      : `Last email failed (${new Date(last.at * 1000).toLocaleString()}): ${last.error}` }) : null,
    warnings.length ? el("ul", { class: "notes small" }, ...warnings.map((w) => el("li", { text: w }))) : null,
    result ? el("p", { class: `small ${result.ok ? "ok-text" : "err"}`, role: result.ok ? "status" : "alert", text: result.ok ? result.message : result.error }) : null,
    el("div", { class: "row" }, btn));
}

// ---------------- plans page ----------------
function renderPlansPage(main) {
  const host = el("div", { class: "plans" });
  main.replaceChildren(el("div", { class: "page-head" }, el("div", {}, el("h1", { text: "Plans" }),
    el("p", { class: "sub", text: "Payments aren't switched on yet: choosing a plan sends us a request and we turn it on by hand, usually within a day." })),
  el("div", { class: "seg", role: "group", "aria-label": "Currency" },
    el("button", { type: "button", "data-cur": "inr", text: "₹ INR" }), el("button", { type: "button", "data-cur": "usd", text: "$ USD" }))), host);
  const rerender = () => renderPlans(host);
  bindCurrency(rerender);
  rerender();
}
