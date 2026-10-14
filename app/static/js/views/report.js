// Game report card: one game, both players, every move checked by Stockfish, then written up by the coach.
// Route: #/report/<accountId>/<gameId>. Grades and accuracy per player, the evaluation graph, move classes,
// key moments on the board, phase grades, the coach's review, and an annotated PGN to download.

import { api, el, fill, post, svg, toast, track } from "../core.js";
import { Board, boardSettings, uciArrow } from "../board.js";
import { followJob } from "./common.js";

const CLASS = {
  brilliant: ["!!", "Brilliant", "#1baca6"], great: ["!", "Great find", "#5c8bb0"], best: ["★", "Best", "#81b64c"],
  excellent: ["", "Excellent", "#96bc4b"], good: ["", "Good", "#a9c48a"], book: ["", "Book", "#a88865"], forced: ["□", "Forced", "#9a9a9a"],
  inaccuracy: ["?!", "Inaccuracy", "#e8b20c"], mistake: ["?", "Mistake", "#f08a3c"], miss: ["✗", "Missed chance", "#ee6b5e"],
  blunder: ["??", "Blunder", "#e0402b"],
};
const SHOWN = ["brilliant", "great", "best", "excellent", "good", "book", "inaccuracy", "mistake", "miss", "blunder"];
const FEATURE_LINK = { predicted: "#/train/predicted", think: "#/train/think", mistakes: "#/train", plan: "#/train/plan", coach: "#/coach",
  prepare: "#/prepare" };
const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : "");
const num = (m) => `${m.move_number}${m.color === "white" ? "." : "..."}`;
const MATE = 10000;
/** White's evaluation after a move: "+1.2", "−0.4", "M3" (White mates in 3) or "−M2". */
const evalText = (v) => {
  if (Math.abs(v) >= MATE - 500) { const n = MATE - Math.abs(v); return n ? `${v > 0 ? "" : "−"}M${n}` : "mate"; }
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${(Math.abs(v) / 100).toFixed(1)}`;
};

export async function render(main) {
  const [, accountId, gameId] = location.hash.replace(/^#\/?/, "").split("/");
  const head = el("div", { class: "page-head" }, el("div", {}, el("p", { class: "small faint" }, el("a", { href: "#/diagnose", text: "Diagnose" }), " › Game report"),
    el("h1", { text: "Game report" }), el("p", { class: "sub", text: "Every move checked by Stockfish, graded like a test paper, and explained by your coach." })));
  const body = el("div", { class: "stack-lg" });
  main.replaceChildren(head, body);
  if (!accountId || !gameId) { body.replaceChildren(el("p", { class: "err", text: "No game chosen." })); return; }
  load(body, accountId, decodeURIComponent(gameId), head);
}

async function load(body, accountId, gameId, head) {
  const url = `/api/accounts/${accountId}/game/${encodeURIComponent(gameId)}/review`;
  let d;
  try { d = await api(url); } catch (e) { body.replaceChildren(el("p", { class: "err", text: e.message })); return; }
  if (!body.isConnected) return;
  const progress = el("div", { class: "report-progress" });
  if (d.state === "importing") {
    fill(body, el("div", { class: "empty stack" }, el("h3", { text: "Analysing your game" }),
      el("p", { text: "Stockfish is going through the moves. The report card opens as soon as it's done." }), progress));
    followJob(progress, d.job_id, () => load(body, accountId, gameId, head));
    return;
  }
  if (d.state === "none") {
    try {
      const r = await post(url, { ai: true });
      if (r.job_id) { d = { state: "running", job_id: r.job_id }; track("Game review", { ai: r.ai }); }
      else { load(body, accountId, gameId, head); return; }
    } catch (e) { body.replaceChildren(el("p", { class: "err", text: e.message })); return; }
  }
  if (d.state === "running") {
    fill(body, el("div", { class: "empty stack" }, el("h3", { text: "Reviewing the game" }),
      el("p", { text: "Checking both players' moves with Stockfish (top two choices in every position), then your coach writes it up. About half a minute." }),
      progress));
    followJob(progress, d.job_id, () => load(body, accountId, gameId, head));
    return;
  }
  draw(body, d, accountId, gameId, url, head);
}

function draw(body, d, accountId, gameId, url, head) {
  const rep = d.report;
  const you = rep.you;
  const res = rep.result || "*";
  head.querySelector("h1").textContent = `${rep.white || "White"} vs ${rep.black || "Black"}`;
  head.querySelector(".sub").textContent = [res, rep.date, rep.event].filter(Boolean).join(" · ");
  const moves = rep.moves;

  // ---- the board and the move list ----
  const boardHost = el("div", { class: "report-board" });
  const board = new Board(boardHost, { fen: "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1", orientation: you || "white", label: "Game board" });
  const note = el("div", { class: "report-note", "aria-live": "polite" });
  const list = el("ol", { class: "report-moves" });
  const items = [];
  for (let i = 0; i < moves.length; i += 2) {
    const li = el("li", {}, el("span", { class: "n", text: `${moves[i].move_number}.` }));
    for (const m of [moves[i], moves[i + 1]].filter(Boolean)) {
      const [sym, label, color] = CLASS[m.class];
      const b = el("button", { type: "button", class: `mv c-${m.class}`, "aria-label": `${num(m)}${m.san}, ${label}`, onclick: () => go(m.ply + 1) },
        m.san, sym ? el("b", { style: { color }, text: sym }) : null);
      items[m.ply] = b;
      li.append(b);
    }
    list.append(li);
  }
  let cur = 0;
  function go(n, { showBest = true } = {}) {
    cur = Math.max(0, Math.min(moves.length, n));
    items.forEach((b) => b && b.removeAttribute("aria-current"));
    const m = cur > 0 ? moves[cur - 1] : null;
    if (m && items[m.ply]) { items[m.ply].setAttribute("aria-current", "true"); items[m.ply].scrollIntoView({ block: "nearest", inline: "nearest" }); }
    const bad = m && ["inaccuracy", "mistake", "miss", "blunder"].includes(m.class);
    if (bad && showBest) { // show the position before the mistake: what was played (blue) and what was best (red)
      board.set({ fen: m.fen, lastMove: m.ply > 0 ? moves[m.ply - 1].uci : null, arrows: [uciArrow(m.uci, "you"), uciArrow(m.best, "coach")].filter(Boolean) });
    } else {
      const fen = m ? afterFen(m) : moves.length ? moves[0].fen : board.fen;
      board.set({ fen, lastMove: m ? m.uci : null, arrows: [] });
    }
    graph.mark(cur);
    if (!m) { fill(note, el("p", { class: "small muted", text: "Start position. Use the arrows, the graph or the move list." })); return; }
    const [sym, label, color] = CLASS[m.class];
    fill(note, el("p", {}, el("b", { text: `${num(m)}${m.san}${sym}` }), " ", el("span", { class: "cls", style: { background: color }, text: label }),
      m.loss >= 1 ? ` · cost ${Math.round(m.loss)}% of the winning chances` : "",
      el("span", { class: "tiny faint", title: "Stockfish's evaluation after the move, from White's side", text: ` · eval ${evalText(m.eval_white)}` })),
    bad && m.best_san ? el("p", { class: "small" }, "Better was ", el("b", { text: m.best_san }), ". The red arrow shows it; the blue one, the move played.") : null,
    m.class === "great" ? el("p", { class: "small muted", text: "The only good move here, and it was found." }) : null,
    m.class === "brilliant" ? el("p", { class: "small muted", text: "A sound sacrifice: giving material for something better." }) : null,
    m.class === "book" ? el("p", { class: "small muted", text: "Known opening theory." }) : null);
  }
  const fenAfter = new Map();
  const afterFen = (m) => {
    if (fenAfter.has(m.ply)) return fenAfter.get(m.ply);
    const next = moves[m.ply + 1];
    const f = next ? next.fen : null;
    fenAfter.set(m.ply, f);
    return f || finalFen;
  };
  const finalFen = rep.final_fen || null;
  const navBtn = (t, l, f) => el("button", { class: "btn quiet small", type: "button", "aria-label": l, text: t, onclick: f });
  const nav = el("div", { class: "row report-nav" }, navBtn("⏮", "Start", () => go(0)), navBtn("◀", "Back", () => go(cur - 1)),
    navBtn("▶", "Forward", () => go(cur + 1)), navBtn("⏭", "End", () => go(moves.length)));
  const keyNav = (e) => {
    if (!body.isConnected) { document.removeEventListener("keydown", keyNav); return; }
    if (e.target.closest("input, textarea, select")) return;
    if (e.key === "ArrowLeft") { go(cur - 1); e.preventDefault(); }
    if (e.key === "ArrowRight") { go(cur + 1); e.preventDefault(); }
  };
  document.addEventListener("keydown", keyNav);

  const graph = evalGraph(rep, (n) => go(n));

  // ---- the grade cards ----
  const gradeCard = (c) => {
    const s = rep.sides[c];
    const name = rep[c] || cap(c);
    return el("div", { class: `grade-card ${you === c ? "you" : ""}` },
      el("div", { class: "grade-top" }, el("span", { class: `piece ${c}`, "aria-hidden": "true" }),
        el("div", {}, el("b", { text: name }), el("span", { class: "tiny faint", text: you === c ? `${cap(c)} · you` : cap(c) }))),
      el("div", { class: "grade-main" }, el("span", { class: `grade g-${s.grade || "none"}`, text: s.grade || "–" }),
        el("div", {}, el("b", { class: "figure", text: s.accuracy != null ? `${s.accuracy}%` : "–" }), el("span", { class: "tiny faint", text: "accuracy" }))),
      el("p", { class: "tiny faint", text: s.acpl != null ? `Average loss ${s.acpl} centipawns a move · ${s.moves} moves` : "" }),
      el("div", { class: "grade-counts" }, ...["brilliant", "great", "best", "inaccuracy", "mistake", "blunder"].map((k) =>
        el("span", { title: CLASS[k][1] }, el("i", { style: { background: CLASS[k][2] }, text: CLASS[k][0] || "•" }), String(s.counts[k] || 0)))));
  };
  const op = rep.opening || {};
  const lb = op.left_book;
  const hero = el("section", { class: "sheet report-hero" },
    el("div", { class: "stack" },
      el("p", { class: "small faint", text: "How the game was decided" }),
      el("h2", { class: "report-verdict", text: (d.ai && d.ai.headline) || rep.verdict }),
      op.name ? el("p", { class: "small" }, el("span", { class: "tag", text: op.eco }), " ", el("b", { text: op.name }),
        lb != null ? el("span", { class: "muted", text: ` · known theory until move ${Math.floor(lb / 2) + 1}` }) : null) : null,
      rep.turning_point ? el("p", { class: "small muted" }, "Turning point: ",
        el("button", { class: "link", type: "button", text: rep.turning_point.label, onclick: () => go(rep.turning_point.ply + 1) })) : null,
      el("div", { class: "row" },
        el("a", { class: "btn quiet small", href: `${url}.pgn`, download: "", text: "Download annotated PGN" }),
        el("span", { class: "tiny faint", text: `Stockfish depth ${rep.depth}, both players, top two moves in every position.` }))),
    el("div", { class: "grade-pair" }, gradeCard("white"), gradeCard("black")));

  // ---- phases and move classes ----
  const phaseRow = (p) => el("tr", {}, el("th", { text: cap(p) }), ...["white", "black"].map((c) => {
    const ph = rep.sides[c].phases[p];
    return el("td", {}, ph.moves ? el("span", {}, el("span", { class: `grade small g-${ph.grade || "none"}`, text: ph.grade || "–" }),
      ` ${ph.accuracy ?? "–"}%`, el("span", { class: "tiny faint", text: ` · ${ph.moves}` })) : el("span", { class: "faint", text: "–" }));
  }));
  const phases = el("section", { class: "sheet stack" }, el("h3", { text: "Grades by phase" }),
    el("table", { class: "report-table" }, el("thead", {}, el("tr", {}, el("th"), el("th", { text: rep.white || "White" }), el("th", { text: rep.black || "Black" }))),
      el("tbody", {}, ...["opening", "middlegame", "endgame"].map(phaseRow))),
    el("p", { class: "tiny faint", text: "A 90%+ · B 80%+ · C 70%+ · D 60%+ · F below. Accuracy per move uses Lichess's formula." }));
  const classes = el("section", { class: "sheet stack" }, el("h3", { text: "Every move, classified" }),
    el("table", { class: "report-table classes" }, el("thead", {}, el("tr", {}, el("th"), el("th", { text: rep.white || "White" }), el("th", { text: rep.black || "Black" }))),
      el("tbody", {}, ...SHOWN.map((k) => el("tr", {}, el("th", {}, el("i", { class: "cls-dot", style: { background: CLASS[k][2] }, text: CLASS[k][0] }), CLASS[k][1]),
        el("td", { text: String(rep.sides.white.counts[k] || 0) }), el("td", { text: String(rep.sides.black.counts[k] || 0) }))))));

  // ---- key moments ----
  const km = el("section", { class: "sheet stack" }, el("h3", { text: "Key moments" }),
    rep.key_moments.length ? el("ol", { class: "key-list" }, ...rep.key_moments.map((k) => el("li", {},
      el("button", { type: "button", class: "key-item", onclick: () => go(k.ply + 1) },
        el("span", { class: "cls", style: { background: CLASS[k.class][2] }, text: CLASS[k.class][1] }),
        el("b", { text: `${k.label}${CLASS[k.class][0]}` }), el("span", { class: "tiny faint", text: ` ${k.color === you ? "you" : k.color} · −${Math.round(k.loss)}%` }),
        el("span", { class: "small", text: k.explain }),
        k.line && k.line.length ? el("span", { class: "tiny faint", text: `Engine line: ${k.line.join(" ")}` }) : null))))
      : el("p", { class: "small muted", text: "No big swings: a clean game from both sides." }));

  // ---- the coach's review ----
  const coach = coachCard(d, url, () => load(body, accountId, gameId, head), (ply) => go(ply + 1));

  fill(body, hero, el("section", { class: "sheet report-graph" }, el("div", { class: "spread" }, el("h3", { text: "Evaluation" }),
    el("span", { class: "tiny faint", text: "White's winning chances move by move. Click to jump." })), graph.node),
  el("div", { class: "report-main" },
    el("div", { class: "report-left" }, boardHost, el("div", { class: "board-foot" }, nav, boardSettings(board)), note, el("div", { class: "report-movebox" }, list)),
    el("div", { class: "report-right stack-lg" }, coach, km)),
  el("div", { class: "grid-2 even" }, phases, classes));
  const firstKey = rep.key_moments.find((k) => k.color === you) || rep.key_moments[0];
  go(firstKey ? firstKey.ply + 1 : 0);
}

function coachCard(d, url, reload, jump) {
  const ai = d.ai || {};
  const src = ai.source === "ai" ? `Written by your AI coach (${ai.model})` : "Written by the built-in coach";
  const out = el("div");
  const askBtn = d.can_ai && ai.source !== "ai" && d.ai_left > 0
    ? el("button", { class: "btn small", type: "button", text: "Ask the AI coach to write it up", onclick: async (e) => {
      e.currentTarget.disabled = true;
      try {
        const r = await post(url, { ai: true });
        if (r.job_id) followJob(out, r.job_id, reload); else toast(r.message);
      } catch (ex) { toast(ex.message); e.currentTarget.disabled = false; }
    } }) : null;
  const phase = (k) => (ai[k] ? el("div", { class: "coach-phase" }, el("b", { text: cap(k) }), el("p", { text: ai[k] })) : null);
  return el("section", { class: "sheet stack coach-review" },
    el("div", { class: "spread" }, el("h3", { text: "Your coach's review" }), el("span", { class: `tag ${ai.source === "ai" ? "pro" : ""}`, text: ai.source === "ai" ? "AI" : "Built-in" })),
    ai.summary ? el("p", { text: ai.summary }) : null,
    el("div", { class: "coach-phases" }, phase("opening"), phase("middlegame"), phase("endgame")),
    ai.strengths && ai.strengths.length ? el("div", {}, el("p", { class: "small faint", text: "What went well" }),
      el("ul", { class: "good-list small" }, ...ai.strengths.map((t) => el("li", { text: t })))) : null,
    ai.lessons && ai.lessons.length ? el("div", {}, el("p", { class: "small faint", text: "Lessons from this game" }),
      el("ol", { class: "lesson-list" }, ...ai.lessons.map((l) => el("li", {}, el("b", { text: l.title }), el("p", { class: "small", text: l.detail }),
        l.ply != null ? el("button", { class: "link small", type: "button", text: "Show me", onclick: () => jump(l.ply) }) : null)))) : null,
    ai.training && ai.training.length ? el("div", {}, el("p", { class: "small faint", text: "Train next" }),
      el("ul", { class: "train-list small" }, ...ai.training.map((t) => el("li", {}, FEATURE_LINK[t.feature]
        ? el("a", { href: FEATURE_LINK[t.feature], text: t.task }) : el("b", { text: t.task }), t.why ? el("span", { class: "muted", text: ` · ${t.why}` }) : null)))) : null,
    el("p", { class: "tiny faint", text: `${src}. Every move it mentions was checked against the engine's analysis${ai.removed && ai.removed.length ? "; unverifiable sentences were removed" : ""}.` }),
    askBtn ? el("div", { class: "row" }, askBtn, el("span", { class: "tiny faint", text: `${d.ai_left} AI write-up${d.ai_left === 1 ? "" : "s"} left today` })) : null,
    out);
}

/** White's winning chances (0-100) after every ply, with the mistakes marked. Click anywhere to jump there. */
function evalGraph(rep, onPick) {
  const s = rep.series;
  const n = Math.max(1, s.length - 1);
  const W = 800, H = 150;
  const x = (i) => (i / n) * W, y = (v) => H - (v / 100) * H;
  const pts = s.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = svg("polygon", { class: "eg-white", points: `0,${H} ${pts} ${W},${H}` });
  const mid = svg("line", { x1: 0, x2: W, y1: H / 2, y2: H / 2, class: "eg-mid" });
  const cursor = svg("line", { y1: 0, y2: H, class: "eg-cursor" });
  const dots = rep.moves.filter((m) => ["inaccuracy", "mistake", "miss", "blunder", "brilliant", "great"].includes(m.class)).map((m) =>
    svg("circle", { cx: x(m.ply + 1), cy: y(s[m.ply + 1]), r: m.class === "blunder" ? 5 : 4, fill: CLASS[m.class][2], class: "eg-dot" },
      svg("title", {}, `${num(m)}${m.san}${CLASS[m.class][0]}: ${CLASS[m.class][1]}`)));
  const hit = svg("rect", { x: 0, y: 0, width: W, height: H, fill: "transparent", class: "eg-hit" });
  const node = svg("svg", { class: "eval-graph", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", role: "img",
    "aria-label": "Evaluation graph: White's winning chances through the game" }, svg("rect", { x: 0, y: 0, width: W, height: H, class: "eg-black" }),
  area, mid, ...dots, cursor, hit);
  const pick = (e) => {
    const r = node.getBoundingClientRect();
    onPick(Math.round(((e.clientX - r.left) / r.width) * n));
  };
  hit.addEventListener("click", pick);
  return { node, mark: (i) => { cursor.setAttribute("x1", x(i)); cursor.setAttribute("x2", x(i)); } };
}
