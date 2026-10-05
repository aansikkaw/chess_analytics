"use strict";
// Plateau Breaker front end. Plain JS, no build step.

const $ = (s) => document.querySelector(s);
const GLYPH = { k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟" };
const FILES = "abcdefgh";
const SKILL_SUGGEST = [
  "What should I study this week?",
  "Why do I lose won positions?",
  "Show me my worst mistake",
  "How is my time management?",
  "How are my endgames?",
];

let source = "lichess";
let user = null;
let puzzles = [];
let current = null; // current puzzle
let selected = null; // selected square name
let flipped = false;
let locked = false; // board locked after an attempt
let chatHistory = [];

const store = {
  get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    const msg = typeof d === "string" ? d : Array.isArray(d) ? d.map((e) => e.msg).join("; ") : `Request failed (${res.status})`;
    throw new Error(msg);
  }
  return data;
}

function el(tag, attrs = {}, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "text") n.textContent = v;
    else n.setAttribute(k, v);
  }
  for (const c of children) n.append(c);
  return n;
}

// ---------- health ----------
async function loadHealth() {
  try {
    const h = await api("/api/health");
    const box = $("#health");
    box.replaceChildren(
      el("span", {}, "Engine ", el("b", { class: h.engine ? "ok" : "no", text: h.engine ? "ready" : "missing" })),
      el("span", {}, "Coach ", el("b", { text: h.coach_mode === "agent" ? "AI agent" : "offline mode" })),
    );
    if (!h.engine) showImportError(h.engine_error || "Stockfish isn't available.");
    $("#coachMode").textContent = h.coach_mode === "agent"
      ? "AI coach: answers are built from tool calls on your data and checked with Stockfish."
      : "Offline coach: rule-based answers. Set ANTHROPIC_API_KEY on the server to switch on the AI agent.";
  } catch (e) { showImportError(e.message); }
}

// ---------- import ----------
document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
  source = b.dataset.src;
  document.querySelectorAll(".seg button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
  $("#f-pgn").hidden = source !== "pgn";
  $("#f-max").hidden = source !== "lichess";
  $("#f-user").hidden = source === "demo";
  $("#demoNote").hidden = source !== "demo";
  $("#username").required = source !== "demo";
  $("#userHint").textContent = source === "pgn" ? "(as written in the PGN)" : "(your Lichess handle)";
}));

function showImportError(msg) { const p = $("#importErr"); p.textContent = msg; p.hidden = !msg; }

$("#importForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  showImportError("");
  const body = {
    source,
    username: source === "demo" ? "demo_player" : $("#username").value.trim(),
    max_games: Number($("#maxGames").value) || 40,
    pgn: source === "pgn" ? $("#pgn").value : null,
  };
  $("#importBtn").disabled = true;
  try {
    const { job_id } = await api("/api/import", { method: "POST", body: JSON.stringify(body) });
    await pollJob(job_id);
  } catch (err) {
    showImportError(err.message);
  } finally {
    $("#importBtn").disabled = false;
  }
});

async function pollJob(id) {
  $("#job").hidden = false;
  for (;;) {
    const j = await api(`/api/jobs/${id}`);
    $("#jobStage").textContent = j.stage || "";
    $("#jobCount").textContent = j.total ? `${j.done}/${j.total} games` + (j.skipped ? ` · ${j.skipped} already analysed` : "") : "";
    $("#jobBar").style.width = j.total ? `${(100 * j.done) / j.total}%` : (j.state === "done" ? "100%" : "4%");
    if (j.state === "error") throw new Error(j.error);
    if (j.state === "done") { await openPlayer(j.username); return; }
    await new Promise((r) => setTimeout(r, 900));
  }
}

// ---------- player ----------
async function openPlayer(username) {
  user = username;
  store.set("pb:user", username);
  chatHistory = [];
  $("#chat").replaceChildren();
  $("#app").hidden = false;
  await Promise.all([loadProfile(), loadPuzzles(), loadPlan()]);
  addMsg("a", `Loaded ${username}'s games. Ask me anything about them, or pick a question below.`);
}

async function loadProfile() {
  const p = await api(`/api/players/${encodeURIComponent(user)}/profile`);
  const d = p.dna;
  $("#who").textContent = user;
  $("#kpis").replaceChildren(
    kpi(d.base_rating + (d.rating_is_estimated ? "?" : ""), "rating"),
    kpi(`${d.overall_accuracy}%`, "accuracy"),
    kpi(d.games, "games"),
    kpi(p.due_puzzles, "puzzles due"),
  );
  $("#dnaNote").textContent = `Skill ratings shift your ${d.base_rating} up or down by how accurate you are in that kind of position, ` +
    `using ${d.moves} moves from positions that were still in play (within ±${d.contested_cp / 100} pawns).` +
    (d.rating_is_estimated ? " Your PGN had no ratings, so 1500 is a placeholder." : "");

  const ratings = d.skills.map((s) => s.rating).concat(d.base_rating);
  const lo = Math.min(...ratings) - 80, hi = Math.max(...ratings) + 80;
  const pct = (v) => `${(100 * (v - lo)) / (hi - lo)}%`;
  $("#dna").replaceChildren(...d.skills.map((s) => {
    const weak = s.rating <= d.base_rating - 60;
    const track = el("div", { class: "track", role: "img", "aria-label": `${s.label} ${s.rating}` },
      el("div", { class: `fill${weak ? " weak" : ""}`, style: `width:${pct(s.rating)}` }),
      el("div", { class: "ref", style: `left:${pct(d.base_rating)}` }));
    const name = el("span", {}, s.label, el("span", { class: "sub", text: `${s.accuracy}% · ${s.moves} moves` }));
    return el("div", { class: `drow${s.low_confidence ? " low" : ""}` }, name, track, el("span", { class: "val", text: String(s.rating) }));
  }));

  $("#insights").replaceChildren(...(p.insights.length ? p.insights.map((i) =>
    el("div", { class: `ins ${i.severity}` }, el("b", { text: i.stat }), i.text)) : [el("p", { class: "muted", text: "Not enough games for findings yet." })]));

  const head = el("tr", {}, ...["Date", "Opponent", "Colour", "Result", "Accuracy", "Opening"].map((h) => el("th", { text: h })));
  const rows = p.recent_games.map((g) => {
    const cls = g.score === 1 ? "res-w" : g.score === 0 ? "res-l" : "";
    const label = g.score === 1 ? "Won" : g.score === 0 ? "Lost" : "Draw";
    return el("tr", {}, el("td", { class: "num", text: g.played_at }), el("td", { text: g.opponent }),
      el("td", { text: g.color }), el("td", { class: cls, text: label }),
      el("td", { class: "num", text: `${g.accuracy}%` }), el("td", { text: g.opening }));
  });
  $("#recent").replaceChildren(el("thead", {}, head), el("tbody", {}, ...rows));
}

function kpi(v, label) { return el("div", { class: "kpi" }, el("b", { text: String(v) }), el("span", { text: label })); }

// ---------- puzzles ----------
async function loadPuzzles() {
  puzzles = await api(`/api/players/${encodeURIComponent(user)}/puzzles?due=true&limit=50`);
  $("#dueCount").textContent = puzzles.length || "";
  nextPuzzle();
}

function nextPuzzle() {
  current = puzzles.shift() || null;
  selected = null;
  locked = false;
  $("#pzResult").textContent = "";
  $("#pzResult").className = "result";
  $("#nextBtn").hidden = true;
  $("#skipBtn").hidden = !current;
  if (!current) {
    $("#pzTitle").textContent = "No puzzles due";
    $("#pzPrompt").textContent = "You're up to date. Puzzles come back on a spaced-repetition schedule: 1, 3, 7, 14, then 30 days.";
    $("#pzMeta").textContent = "";
    $("#pzThemes").replaceChildren();
    $("#board").replaceChildren();
    $("#toMove").textContent = "";
    return;
  }
  const side = current.fen.split(" ")[1] === "w" ? "White" : "Black";
  flipped = side === "Black";
  $("#pzMeta").textContent = `From your game · move ${current.fen.split(" ")[5]} · box ${current.box}`;
  $("#pzTitle").textContent = `${side} to move`;
  $("#pzPrompt").textContent = `In the game you played ${current.played_san} and lost ${Math.round(current.win_loss)} points of winning chances. Find the better move.`;
  $("#pzThemes").replaceChildren(...current.themes.map((t) => el("span", { class: "chip", text: t.replace(/_/g, " ") })));
  $("#toMove").textContent = `${side} to move`;
  drawBoard(current.fen);
}

function parseFen(fen) {
  const map = {};
  fen.split(" ")[0].split("/").forEach((row, r) => {
    let f = 0;
    for (const ch of row) {
      if (/\d/.test(ch)) f += Number(ch);
      else { map[FILES[f] + (8 - r)] = ch; f++; }
    }
  });
  return map;
}

function drawBoard(fen, highlight = []) {
  const pieces = parseFen(fen);
  const turnWhite = fen.split(" ")[1] === "w";
  const squares = [];
  for (let i = 0; i < 64; i++) {
    const r = flipped ? 7 - Math.floor(i / 8) : Math.floor(i / 8);
    const f = flipped ? 7 - (i % 8) : i % 8;
    const name = FILES[f] + (8 - r);
    const btn = el("button", { class: `sq ${(r + f) % 2 === 0 ? "l" : "d"}`, type: "button", "aria-label": name, "data-sq": name });
    const p = pieces[name];
    if (p) btn.append(el("span", { class: p === p.toUpperCase() ? "w" : "b", text: GLYPH[p.toLowerCase()] }));
    if ((flipped ? r === 0 : r === 7)) btn.append(el("span", { class: "coord", text: FILES[f] }));
    if (name === selected) btn.classList.add("sel");
    if (highlight.includes(name)) btn.classList.add(highlight.indexOf(name) === 0 ? "from" : "to");
    btn.addEventListener("click", () => onSquare(name, pieces, turnWhite));
    squares.push(btn);
  }
  $("#board").replaceChildren(...squares);
}

function onSquare(name, pieces, turnWhite) {
  if (!current || locked) return;
  const p = pieces[name];
  const own = p && (p === p.toUpperCase()) === turnWhite;
  if (own) { selected = name; drawBoard(current.fen); return; }
  if (!selected) return;
  let uci = selected + name;
  const mover = pieces[selected];
  if (mover && mover.toLowerCase() === "p" && (name[1] === "8" || name[1] === "1")) uci += "q";
  selected = null;
  submitAttempt(uci);
}

async function submitAttempt(uci) {
  const box = $("#pzResult");
  try {
    const r = await api(`/api/puzzles/${current.id}/attempt`, { method: "POST", body: JSON.stringify({ move: uci }) });
    locked = true;
    box.className = `result ${r.verdict}`;
    const line = r.line && r.line.length ? ` Line: ${r.line.join(" ")}.` : "";
    const when = r.next_review_in_days >= 1 ? `${Math.round(r.next_review_in_days)} days` : "a few minutes";
    box.textContent = r.verdict === "correct"
      ? `Correct: ${r.solution_san}.${line} Back in ${when}.`
      : r.verdict === "also_good"
        ? `${r.your_move} works too. ${r.note} The engine's top choice was ${r.solution_san}.${line} Back in ${when}.`
        : `${r.your_move} isn't it. The move was ${r.solution_san}.${line} You'll see this one again in ${when}.`;
    drawBoard(current.fen, [r.solution.slice(0, 2), r.solution.slice(2, 4)]);
    $("#nextBtn").hidden = false;
    $("#skipBtn").hidden = true;
    $("#dueCount").textContent = puzzles.length || "";
  } catch (err) {
    box.className = "result wrong";
    box.textContent = err.message;
    drawBoard(current.fen);
  }
}

$("#nextBtn").addEventListener("click", nextPuzzle);
$("#skipBtn").addEventListener("click", () => { if (current) puzzles.push(current); nextPuzzle(); });
$("#flipBtn").addEventListener("click", () => { if (current) { flipped = !flipped; drawBoard(current.fen); } });

// ---------- plan ----------
async function loadPlan() {
  const plan = await api(`/api/players/${encodeURIComponent(user)}/plan`);
  $("#planFocus").textContent = plan.focus.length ? `Focus: ${plan.focus.join(" and ")}. Built from your two weakest skills.` : "";
  const done = store.get(`pb:plan:${user}`) || {};
  $("#plan").replaceChildren(...plan.days.map((d) => {
    const items = d.items.map((it, i) => {
      const key = `${d.day}-${i}`;
      const id = `plan-${key}`;
      const cb = el("input", { type: "checkbox", id });
      cb.checked = !!done[key];
      const li = el("li", { class: cb.checked ? "done" : "" }, cb, el("label", { for: id, text: it.task }), el("span", { class: "min", text: `${it.minutes}m` }));
      cb.addEventListener("change", () => { done[key] = cb.checked; li.classList.toggle("done", cb.checked); store.set(`pb:plan:${user}`, done); });
      return li;
    });
    return el("article", { class: "day" }, el("header", {}, el("h3", { text: `Day ${d.day}` }), el("span", { class: "min mono", text: `${d.minutes} min` })),
      el("span", { class: "eyebrow", text: d.focus }), el("ul", {}, ...items));
  }));
}

// ---------- coach ----------
function addMsg(who, text, extraClass = "") {
  const m = el("div", { class: `msg ${who} ${extraClass}`.trim(), text });
  $("#chat").append(m);
  $("#chat").scrollTop = $("#chat").scrollHeight;
  return m;
}

$("#suggest").replaceChildren(...SKILL_SUGGEST.map((q) => {
  const b = el("button", { class: "chip", type: "button", text: q });
  b.addEventListener("click", () => ask(q));
  return b;
}));

async function ask(q) {
  if (!user || !q.trim()) return;
  addMsg("u", q);
  const thinking = addMsg("a", "Looking through your games…", "thinking");
  try {
    const r = await api(`/api/players/${encodeURIComponent(user)}/coach`, {
      method: "POST", body: JSON.stringify({ message: q, history: chatHistory }),
    });
    thinking.remove();
    addMsg("a", r.reply);
    chatHistory.push({ role: "user", content: q }, { role: "assistant", content: r.reply });
    chatHistory = chatHistory.slice(-20);
  } catch (err) {
    thinking.remove();
    addMsg("a", `Sorry, that failed: ${err.message}`);
  }
}

$("#chatForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#chatInput").value;
  $("#chatInput").value = "";
  ask(q);
});

// ---------- tabs ----------
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".tabs button").forEach((x) => x.setAttribute("aria-selected", String(x === b)));
  document.querySelectorAll(".view").forEach((v) => { v.hidden = v.id !== `view-${b.dataset.view}`; });
}));

// ---------- boot ----------
loadHealth();
const last = store.get("pb:user");
if (last) {
  $("#username").value = last === "demo_player" ? "" : last;
  openPlayer(last).catch(() => { $("#app").hidden = true; });
}
