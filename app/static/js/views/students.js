// Coach dashboard: the squad at a glance (a skills heatmap built from every student's Rating DNA), each
// student's detail, homework the coach sets and the student ticks off, and invites that give students Pro.

import { $, PLATFORM, S, ago, api, del, el, fill, openDialog, patch, post, showErr, toast, track } from "../core.js";
import { renderDNA } from "../dna.js";
import { openGame } from "./common.js";
import { pollJob, refreshMe, selectAccount } from "../shell.js";

const SUGGEST = {
  openings: "Openings: replay your 5 worst openings from this month and write down the move you'll play next time",
  middlegame: "Middlegame: 3 sessions of 20 minutes on your middlegame puzzles",
  endgames: "Endgames: 20 minutes of your own endgame puzzles, 4 days this week",
  tactics: "Tactics: solve 15 puzzles from your own games, without hints",
  time: "Time: play 3 rapid games and use at least half your clock by move 25",
  converting: "Converting: from a winning position, trade pieces and play it out against the engine 3 times",
  defending: "Defending: play out 3 worse positions from your games and look for the most stubborn move",
};
const SHORT = { openings: "Openings", middlegame: "Middlegame", endgames: "Endgames", tactics: "Tactics", time: "Clock", converting: "Converting", defending: "Defending" };
const fmtOffset = (n) => (n > 0 ? `+${n}` : n < 0 ? `−${Math.abs(n)}` : "±0");
const heat = (offset, low) => {
  const v = Math.max(-150, Math.min(150, offset)) / 150;
  return low ? "heat low" : v <= -0.5 ? "heat n2" : v <= -0.15 ? "heat n1" : v >= 0.5 ? "heat p2" : v >= 0.15 ? "heat p1" : "heat z";
};

export function render(main, sub) {
  if (!S.me || S.me.plan !== "coach") { upsell(main); return; }
  if (sub && /^\d+$/.test(sub)) detail(main, Number(sub));
  else squad(main);
}

function upsell(main) {
  main.replaceChildren(el("div", { class: "page-head" }, el("div", {}, el("h1", { text: "Students" }),
    el("p", { class: "sub", text: "The coach dashboard is part of the Coach plan." }))),
  el("div", { class: "empty stack" }, el("h3", { text: "Coach up to 20 students" }),
    el("p", { text: "See every student's Rating DNA in one heatmap, set homework they tick off, and give them Pro while they're in your squad." }),
    el("a", { class: "btn", href: "#/plans", text: "See the Coach plan" })));
}

// ---------------- the squad ----------------
async function squad(main) {
  main.replaceChildren(el("p", { class: "muted", text: "Loading your students…" }));
  let d;
  try { d = await api("/api/coach/students"); }
  catch (e) { main.replaceChildren(el("p", { class: "err", text: e.message })); return; }
  const full = d.students.length >= d.max_students;
  const addBtn = el("button", { class: "btn", type: "button", text: "Add student", disabled: full, onclick: () => addStudent(d) });
  const syncBtn = el("button", { class: "btn quiet", type: "button", text: "Sync everyone", disabled: !d.students.length, onclick: async () => {
    syncBtn.disabled = true;
    try { toast((await post("/api/coach/sync-all")).message); setTimeout(() => squad(main), 1500); } catch (e) { toast(e.message); syncBtn.disabled = false; }
  } });
  const head = el("div", { class: "page-head" }, el("div", {}, el("h1", { text: "Students" }),
    el("p", { class: "sub", text: "Your squad at a glance: where each student loses rating, and what to set them next." })),
  el("div", { class: "row" }, syncBtn, addBtn));
  if (!d.students.length) {
    main.replaceChildren(head, el("div", { class: "empty stack" }, el("h3", { text: "Add your first student" }),
      el("p", { text: `Add a student by their Lichess or Chess.com username. We analyse up to ${d.games_per_student} of their recent games and build their Rating DNA; they don't need an account.` }),
      el("button", { class: "btn", type: "button", text: "Add student", onclick: () => addStudent(d) })));
    return;
  }
  const analysed = d.students.filter((s) => s.games);
  const games = d.students.reduce((n, s) => n + s.games, 0);
  const kpis = el("div", { class: "kpis sheet" },
    el("div", {}, el("b", { class: "figure", text: `${d.students.length}/${d.max_students}` }), el("span", { text: "students" })),
    el("div", {}, el("b", { class: "figure", text: games.toLocaleString() }), el("span", { text: "games analysed" })),
    el("div", {}, el("b", { class: "figure", text: analysed.length ? String(Math.round(analysed.reduce((n, s) => n + s.base_rating, 0) / analysed.length)) : "–" }), el("span", { text: "average rating" })),
    el("div", { class: "squad-weak" }, el("span", { text: "Squad weak spots" }),
      el("div", { class: "chips" }, ...(d.squad.filter((x) => x.avg_offset < 0).slice(0, 3).map((x) =>
        el("span", { class: "tag bad", text: `${x.label} ${fmtOffset(x.avg_offset)}` }))),
      d.squad.some((x) => x.avg_offset < 0) ? null : el("span", { class: "small muted", text: "Nothing stands out yet." }))));

  const skillCols = d.skills;
  const thead = el("thead", {}, el("tr", {}, el("th", { text: "Student", scope: "col" }), el("th", { text: "Rating", scope: "col" }),
    el("th", { text: "Games", scope: "col" }), el("th", { text: "Accuracy", scope: "col" }), el("th", { text: "Form", scope: "col", title: "Score in the last 20 games" }),
    ...skillCols.map((k) => el("th", { class: "skill", scope: "col", title: k.label, text: SHORT[k.key] || k.label })), el("th", { text: "Homework", scope: "col" })));
  const rows = d.students.map((s) => {
    const status = s.running_job ? el("span", { class: "tiny live", text: "Analysing…" })
      : s.last_sync_error ? el("span", { class: "tiny bad", text: s.last_sync_error })
      : el("span", { class: "tiny faint", text: `${s.handle} · ${PLATFORM[s.platform] || s.platform}${s.joined ? " · joined" : ""}` });
    const tr = el("tr", { class: "clickable", tabindex: "0", onclick: () => { location.hash = `#/students/${s.id}`; },
      onkeydown: (e) => { if (e.key === "Enter") location.hash = `#/students/${s.id}`; } },
    el("th", { scope: "row" }, el("div", { class: "who" }, el("b", { text: s.name }), status)),
    el("td", { class: "num", text: s.base_rating ? `${s.base_rating}${s.rating_is_estimated ? "*" : ""}` : "–" }),
    el("td", { class: "num", text: String(s.games) }),
    el("td", { class: "num" }, s.accuracy != null ? `${s.accuracy}%` : "–",
      s.accuracy_trend != null && Math.abs(s.accuracy_trend) >= 0.5 ? el("span", { class: `trend ${s.accuracy_trend > 0 ? "up" : "down"}`, text: ` ${s.accuracy_trend > 0 ? "▲" : "▼"}${Math.abs(s.accuracy_trend)}` }) : null),
    el("td", { class: "num", text: s.form != null ? `${s.form}%` : "–" }),
    ...skillCols.map((k) => {
      const v = s.skills[k.key];
      if (!v) return el("td", { class: "heat none", text: "" });
      return el("td", { class: heat(v.offset, v.low_confidence), title: `${k.label}: ${v.rating} (${fmtOffset(v.offset)} vs overall)${v.low_confidence ? ", few moves so far" : ""}`, text: fmtOffset(v.offset) });
    }),
    el("td", { class: "num", text: s.homework_open || s.homework_done ? `${s.homework_done}/${s.homework_open + s.homework_done}` : "–" }));
    return tr;
  });
  const table = el("div", { class: "table-wrap sheet" }, el("table", { class: "squad" }, thead, el("tbody", {}, ...rows)));
  main.replaceChildren(head, kpis,
    el("div", { class: "row between" }, el("h2", { text: "Skills heatmap" }),
      el("p", { class: "tiny faint", text: "Each skill's rating against the student's overall rating. Red is costing them points; faded cells need more games." })),
    table);
  // Keep analysing rows fresh.
  if (d.students.some((s) => s.running_job)) setTimeout(() => { if (location.hash === "#/students") squad(main); }, 8000);
}

function addStudent(d) {
  const name = el("input", { type: "text", id: "stName", maxlength: "60", autocomplete: "off", required: true });
  const platform = el("select", { id: "stPlat" }, ...(S.config.platforms || ["lichess"]).map((p) => el("option", { value: p, text: PLATFORM[p] })),
    el("option", { value: "pgn", text: "ChessBase / PGN (over-the-board)" }));
  const handle = el("input", { type: "text", id: "stHandle", maxlength: "60", autocomplete: "off", autocapitalize: "off", spellcheck: "false", required: true });
  const err = el("p", { class: "err", role: "alert", hidden: true });
  const btn = el("button", { class: "btn", type: "submit", text: "Add and analyse" });
  const form = el("form", { class: "stack" },
    el("div", { class: "field" }, el("label", { for: "stName", text: "Student's name" }), name),
    el("div", { class: "field" }, el("label", { for: "stPlat", text: "Where they play" }), platform),
    el("div", { class: "field" }, el("label", { for: "stHandle", text: "Their username" }), handle,
      el("span", { class: "tiny faint", text: `We analyse up to ${d.games_per_student} of their recent rated games. Only public games are read.` })),
    btn, err);
  platform.addEventListener("change", () => { btn.textContent = platform.value === "pgn" ? "Add" : "Add and analyse"; });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showErr(err, "");
    btn.disabled = true;
    try {
      const s = await post("/api/coach/students", { name: name.value.trim(), platform: platform.value, handle: handle.value.trim() });
      track("Student added", { platform: s.platform });
      $("#studentDlg").close();
      await refreshMe();
      toast(`${s.name} added. Their games are being analysed.`);
      location.hash = `#/students/${s.id}`;
    } catch (ex) { showErr(err, ex.message); }
    finally { btn.disabled = false; }
  });
  fill($("#studentBody"), form);
  $("#studentTitle").textContent = "Add a student";
  openDialog($("#studentDlg"));
  name.focus();
}

// ---------------- one student ----------------
async function detail(main, id) {
  main.replaceChildren(el("p", { class: "muted", text: "Loading…" }));
  let s;
  try { s = await api(`/api/coach/students/${id}`); }
  catch (e) { main.replaceChildren(el("a", { href: "#/students", text: "← All students" }), el("p", { class: "err", text: e.message })); return; }
  const reload = () => detail(main, id);
  const head = el("div", { class: "page-head" }, el("div", {},
    el("a", { class: "small", href: "#/students", text: "← All students" }),
    el("h1", { text: s.name }),
    el("p", { class: "sub" }, `${s.handle} on ${PLATFORM[s.platform] || s.platform}`,
      s.base_rating ? ` · ${s.base_rating}${s.rating_is_estimated ? " (estimated)" : ""}` : "", ` · ${s.games} games analysed`,
      s.last_synced_at ? ` · ${ago(s.last_synced_at)}` : "")),
  el("div", { class: "row" },
    s.games ? el("button", { class: "btn", type: "button", text: "Open full analysis", onclick: async () => {
      await refreshMe(); // the roster may have grown since the app loaded
      selectAccount(s.account_id, { silent: true });
      location.hash = "#/diagnose";
    } }) : null,
    s.platform !== "pgn" ? el("button", { class: "btn quiet", type: "button", text: s.running_job ? "Analysing…" : "Sync now", disabled: !!s.running_job, onclick: async () => {
      try { const j = await post(`/api/coach/students/${id}/sync`); toast("Syncing. New games appear as they're analysed."); pollJob(j.job_id, s.account_id); setTimeout(reload, 1500); }
      catch (e) { toast(e.message); }
    } }) : null));

  // Left: DNA + insights + games. Right: homework, invite, notes.
  const left = el("div", { class: "stack" });
  if (s.dna) {
    const dnaHost = el("div", { class: "sheet" });
    renderDNA(dnaHost, s.dna, { annotate: true });
    left.append(el("h2", { text: "Rating DNA" }), dnaHost);
    if (s.insights && s.insights.length) {
      left.append(el("h2", { text: "What's costing them points" }),
        el("ol", { class: "findings" }, ...s.insights.map((i) => el("li", { class: i.severity }, el("b", { class: "stat figure", text: i.stat }), el("span", { text: i.text })))));
    }
    if (s.recent_games && s.recent_games.length) {
      left.append(el("h2", { text: "Recent games" }), el("div", { class: "table-wrap sheet" }, el("table", {},
        el("thead", {}, el("tr", {}, ...["Date", "Opponent", "Result", "Accuracy", "Opening"].map((t) => el("th", { scope: "col", text: t })))),
        el("tbody", {}, ...s.recent_games.map((g) => el("tr", { class: "clickable", tabindex: "0", onclick: () => openGame(g.game_id, s.account_id),
          onkeydown: (e) => { if (e.key === "Enter") openGame(g.game_id, s.account_id); } },
        el("td", { text: g.played_at }), el("td", { text: g.opponent }),
        el("td", { class: g.score === 1 ? "res-w" : g.score === 0 ? "res-l" : "", text: g.score === 1 ? "Won" : g.score === 0 ? "Lost" : "Draw" }),
        el("td", { class: "num", text: g.accuracy != null ? `${g.accuracy}%` : "–" }), el("td", { class: "small", text: g.opening })))))));
    }
  } else {
    left.append(el("div", { class: "empty" }, el("h3", { text: s.running_job ? "Analysing their games" : "No games analysed yet" }),
      el("p", { text: s.running_job ? "Their Rating DNA appears after the first 20 games. This page refreshes itself." : s.last_sync_error || "Sync to fetch and analyse their games." })));
    if (s.running_job) setTimeout(() => { if (location.hash === `#/students/${id}`) reload(); }, 8000);
  }

  const right = el("div", { class: "stack" }, homework(s, reload), invitePanel(s, reload), notesPanel(s), dangerPanel(s));
  main.replaceChildren(head, el("div", { class: "student-grid" }, left, right));
}

function homework(s, reload) {
  const list = el("ul", { class: "homework" }, ...s.assignments.map((a) => {
    const cb = el("input", { type: "checkbox", id: `hw-${a.id}` });
    cb.checked = !!a.done_at;
    cb.addEventListener("change", async () => {
      try { await patch(`/api/assignments/${a.id}`, { done: cb.checked }); li.classList.toggle("done", cb.checked); }
      catch (e) { cb.checked = !cb.checked; toast(e.message); }
    });
    const due = a.due_at ? new Date(a.due_at * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" }) : null;
    const li = el("li", { class: a.done_at ? "done" : "" }, cb,
      el("label", { for: `hw-${a.id}` }, el("span", { text: a.title }), a.detail ? el("span", { class: "tiny muted", text: a.detail }) : null),
      el("span", { class: "tiny faint", text: a.done_at ? "done" : due ? `due ${due}` : "" }),
      el("button", { class: "link quiet tiny", type: "button", "aria-label": `Remove ${a.title}`, text: "✕", onclick: async () => {
        try { await del(`/api/assignments/${a.id}`); reload(); } catch (e) { toast(e.message); }
      } }));
    return li;
  }));
  const title = el("input", { type: "text", id: "hwTitle", maxlength: "140", placeholder: "e.g. 15 of your own tactics puzzles, no hints" });
  const skill = el("select", { id: "hwSkill" }, el("option", { value: "", text: "Any skill" }),
    ...Object.entries(s.skills || {}).map(([k, v]) => el("option", { value: k, text: `${v.label} (${fmtOffset(v.offset)})` })));
  const due = el("select", { id: "hwDue" }, ...[["", "No due date"], ["3", "In 3 days"], ["7", "In a week"], ["14", "In 2 weeks"]].map(([v, t]) => el("option", { value: v, text: t })));
  const err = el("p", { class: "err", role: "alert", hidden: true });
  const suggest = s.weakest && s.weakest.length ? el("button", { class: "link small", type: "button", text: "Suggest from their weak spots", onclick: () => {
    const k = s.weakest.find((w) => !s.assignments.some((a) => a.skill === w && !a.done_at)) || s.weakest[0];
    skill.value = k; title.value = SUGGEST[k] || ""; due.value = "7"; title.focus();
  } }) : null;
  const form = el("form", { class: "stack hw-form" },
    el("div", { class: "field" }, el("label", { for: "hwTitle", text: "New homework" }), title),
    el("div", { class: "row" }, el("label", { class: "sr", for: "hwSkill", text: "Skill" }), skill, el("label", { class: "sr", for: "hwDue", text: "Due" }), due),
    el("div", { class: "row between" }, suggest || el("span"), el("button", { class: "btn small", type: "submit", text: "Set homework" })), err);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!title.value.trim()) { showErr(err, "Write what you'd like them to do."); return; }
    try {
      await post(`/api/coach/students/${s.id}/assignments`, { title: title.value.trim(), skill: skill.value || null, due_days: due.value ? Number(due.value) : null });
      track("Homework set");
      reload();
    } catch (ex) { showErr(err, ex.message); }
  });
  return el("section", { class: "sheet stack" }, el("h2", { text: "Homework" }),
    s.assignments.length ? list : el("p", { class: "small muted", text: s.joined ? "Nothing set yet. They'll see homework on their Train page." : "Nothing set yet. Once they join with your invite, they'll see it on their Train page." }),
    form);
}

function invitePanel(s) {
  const box = el("section", { class: "sheet stack" }, el("h2", { text: "Their own login" }));
  if (s.joined) {
    box.append(el("p", { class: "small" }, el("span", { class: "ok-text", text: "Joined" }), ` as ${s.student_email}. They have Pro while they're in your squad, and see your homework.`));
    return box;
  }
  const email = el("input", { type: "email", id: "invEmail", placeholder: "Their email (optional)", autocomplete: "off" });
  const out = el("div", { class: "stack" });
  const btn = el("button", { class: "btn quiet small", type: "submit", text: "Create invite link" });
  const form = el("form", { class: "row" }, el("label", { class: "sr", for: "invEmail", text: "Student's email" }), email, btn);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    btn.disabled = true;
    try {
      const r = await post(`/api/coach/students/${s.id}/invite`, { email: email.value.trim() || null });
      const linkInput = el("input", { type: "text", readonly: true, value: r.link, "aria-label": "Invite link" });
      fill(out, el("div", { class: "row" }, linkInput, el("button", { class: "btn small", type: "button", text: "Copy", onclick: async () => {
        try { await navigator.clipboard.writeText(r.link); toast("Link copied."); } catch { linkInput.select(); }
      } })), el("p", { class: "tiny faint", text: `${r.sent_to ? `Emailed to ${r.sent_to}. ` : ""}The link works once, for ${r.expires_in_days} days.` }));
      track("Student invited");
    } catch (ex) { toast(ex.message); }
    finally { btn.disabled = false; }
  });
  box.append(...[el("p", { class: "small muted", text: "Invite them to sign in with their own account: they get Pro free while in your squad, see the homework you set, and train on their own mistakes." }),
    s.invite_pending ? el("p", { class: "tiny faint", text: "An invite is waiting. Creating a new link replaces it." }) : null, form, out].filter(Boolean));
  return box;
}

function notesPanel(s) {
  const area = el("textarea", { id: `note-${s.id}`, rows: "4", maxlength: "4000", placeholder: "Private notes: what you worked on, what to look at next lesson…" });
  area.value = s.note || "";
  const saved = el("span", { class: "tiny faint", "aria-live": "polite" });
  area.addEventListener("blur", async () => {
    if (area.value === (s.note || "")) return;
    try { await patch(`/api/coach/students/${s.id}`, { note: area.value }); s.note = area.value; saved.textContent = "Saved"; }
    catch (e) { saved.textContent = e.message; }
  });
  return el("section", { class: "sheet stack" }, el("h2", { text: "Notes" }), el("label", { class: "sr", for: area.id, text: "Private notes" }), area, saved);
}

function dangerPanel(s) {
  const rm = el("button", { class: "link quiet small", type: "button", text: `Remove ${s.name} from your squad`, onclick: async () => {
    if (rm.dataset.confirm !== "1") { rm.dataset.confirm = "1"; rm.textContent = "Click again: this deletes their analysis and homework"; return; }
    try { await del(`/api/coach/students/${s.id}`); await refreshMe(); toast(`${s.name} removed.`); location.hash = "#/students"; }
    catch (e) { toast(e.message); }
  } });
  return el("div", {}, rm);
}
