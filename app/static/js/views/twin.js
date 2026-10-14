// Opponent Twin: practise against a bot built from one opponent's games.
// The board is on the left; the Twin's reasoning (book or model, and what else it considered) is on the right;
// below are the Twin's profile and, after a game, a review. The game is kept in this browser so you can come back to it.

import { api, el, fill, post, store, svg, toast, track } from "../core.js";
import { Board, boardSettings, sideToMove } from "../board.js";
import { openPosition } from "./common.js";

const START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const TC_OPTIONS = [["", "All their games"], ["blitz", "Blitz only"], ["rapid,classical", "Rapid and classical"], ["bullet", "Bullet only"]];
const PHASES = [["opening", "Opening"], ["middlegame", "Middlegame"], ["endgame", "Endgame"]];

const games = {}; // player id -> the game in progress (also saved in this browser)
const saveKey = (pid) => `pb:twin:${pid}`;
const tcPref = {};

function newGame(pid, color, mode, tc, mainLine) {
  return { pid, color, mode, tc, mainLine, moves: [], sans: [], fens: [START], last: null, over: null, review: null, started: Date.now() };
}
function persist(g) { store.set(saveKey(g.pid), g); }
function restore(pid) {
  if (games[pid]) return games[pid];
  const g = store.get(saveKey(pid));
  if (g && Array.isArray(g.moves) && Array.isArray(g.fens)) games[pid] = g;
  return games[pid] || null;
}

export async function render(content, player) {
  const tc = tcPref[player.id] ?? (restore(player.id) || {}).tc ?? "";
  content.replaceChildren(el("p", { class: "muted", text: "Loading the Twin…" }));
  let st;
  try { st = await api(`/api/twin/${player.id}${tc ? `?tc=${encodeURIComponent(tc)}` : ""}`); }
  catch (e) { content.replaceChildren(el("p", { class: "err", text: e.message })); return; }
  const tcSel = el("select", { id: "twinTc", "aria-label": "Which games the Twin learns from" },
    ...TC_OPTIONS.map(([v, t]) => el("option", { value: v, text: t, selected: v === tc })));
  tcSel.addEventListener("change", () => { tcPref[player.id] = tcSel.value; render(content, player); });
  const head = el("section", { class: "sheet stack twin-head" },
    el("div", { class: "row between" },
      el("div", {}, el("p", { class: "small faint", text: "Opponent Twin · practice bot" }), el("h2", { text: `${player.handle}'s Twin` })),
      el("div", { class: "field narrow" }, el("label", { for: "twinTc", text: "Learn from" }), tcSel)),
    el("p", { class: "small muted", text: "It plays their openings from their own games, and out of book it makes the kind of moves they make: their accuracy in each phase, their blunders, their style. Practice only: it's a model built from public games, not the real player." }));
  if (!st.twin) { fill(content, head, buildCard(content, player, st, tc)); return; }
  const g = restore(player.id);
  const gameBox = el("div");
  fill(content, head, gameBox, profileCards(st, content, player, tc));
  if (g && g.tc === tc && !g.over) playGame(gameBox, player, g, st);
  else if (g && g.tc === tc && g.over) playGame(gameBox, player, g, st);
  else setupCard(gameBox, player, st, tc);
}

// ---------------- building ----------------
function buildCard(content, player, st, tc) {
  const enough = st.games >= st.min_games;
  const out = el("div", { class: "stack" });
  const btn = el("button", { class: "btn", type: "button", text: "Build the Twin", disabled: !enough || !st.engine || !st.builds_left });
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const r = await post(`/api/twin/${player.id}/build`, { time_classes: tc ? tc.split(",") : null });
      track("Twin build");
      if (r.job_id) watchJob(out, r.job_id, () => render(content, player));
      else { toast(r.message); render(content, player); }
    } catch (e) { fill(out, el("p", { class: "err", text: e.message })); btn.disabled = false; }
  });
  if (st.job_id) setTimeout(() => watchJob(out, st.job_id, () => render(content, player)));
  const why = !st.engine ? "Stockfish isn't running on this server, so Twins can't be built here."
    : !enough ? `The Twin needs at least ${st.min_games} games with these time controls; there are ${st.games}. Update the player or download more games.`
      : !st.builds_left ? "You've used today's Twin builds. Try again tomorrow."
        : `First it studies ${(st.study && st.study.next_batch) || st.games} of their ${st.games} games with the engine (about ${Math.max(1, Math.round((((st.study && st.study.next_batch) || st.games) * 1.1) / 60))} min), newest first plus a spread across their history. Afterwards "Keep learning" studies more, and their book always uses every game.`;
  return el("section", { class: "sheet stack twin-build" },
    el("h3", { text: "Build their Twin" }),
    el("ol", { class: "twin-steps small" },
      el("li", { text: "Replays their games and, at each of their moves, scores every sensible alternative with Stockfish." }),
      el("li", { text: "Fits a personal decision model: how much they give away in each phase, and what they like when moves are close." }),
      el("li", { text: "Tests itself on games it didn't learn from, so you can see how faithful it is." })),
    el("p", { class: "small muted", text: why }), el("div", { class: "row" }, btn), out);
}

export async function watchJob(out, jobId, done) {
  const bar = el("div", { class: "syncbar twin-bar" }, el("i"));
  const label = el("p", { class: "small muted", text: "Starting…" });
  fill(out, label, bar);
  for (;;) {
    let j;
    try { j = await api(`/api/jobs/${jobId}`); } catch (e) { fill(out, el("p", { class: "err", text: e.message })); return; }
    if (j.state === "done") { toast("The Twin is ready."); done(); return; }
    if (j.state === "error") { fill(out, el("p", { class: "err", text: j.error || "The build failed." })); return; }
    label.textContent = j.state === "queued" ? `${j.stage && j.done ? `${j.stage}. ` : ""}Waiting for a free worker${j.position ? ` (${j.position} ahead)` : ""}…`
      : `${j.stage || "Working"}${j.total && !/\d/.test(j.stage || "") ? `: ${j.done} of ${j.total}` : ""}…`;
    bar.firstChild.style.width = j.total ? `${Math.round((100 * j.done) / j.total)}%` : "8%";
    await new Promise((r) => setTimeout(r, 1500));
  }
}

// ---------------- the Twin's profile ----------------
function profileCards(st, content, player, tc) {
  const t = st.twin;
  const p = t.profile;
  const f = p.faithfulness || {};
  const pct = (v) => (v == null ? "–" : `${v}%`);
  const bar = (label, v) => el("div", { class: "twin-acc" }, el("span", { text: label }),
    el("div", { class: "twin-acc-track" }, el("i", { style: { width: `${Math.max(0, Math.min(100, v || 0))}%` } })),
    el("b", { text: v == null ? "–" : `${v}%` }));
  const open = (key, label) => (p.openings && p.openings[key] ? el("p", { class: "small" }, el("b", { text: `${label}: ` }),
    p.openings[key].map((o) => `${o.san} ${o.pct}%`).join(", ")) : null);
  const tt = p.time_trouble || {};
  const sd = st.study || {};
  const note = el("div", { class: "stack" });
  const learnText = sd.next_batch > 0 ? `Keep learning: study ${sd.next_batch} more of their games`
    : t.stale ? "Refit with their new games" : null;
  const learn = learnText ? el("button", { class: "btn small", type: "button", text: learnText, onclick: async () => {
    try {
      const r = await post(`/api/twin/${player.id}/build`, { time_classes: tc ? tc.split(",") : null });
      if (r.job_id) { learn.disabled = true; toast(r.message); watchJob(note, r.job_id, () => render(content, player)); } else toast(r.message);
    } catch (e) { toast(e.message); }
  } }) : null;
  const studied = sd.available ? Math.round((100 * sd.studied) / sd.available) : 0;
  const learnNote = sd.next_batch > 0 ? null : sd.studied >= sd.available
    ? "It has studied every game in the library for these time controls."
    : `It has studied ${sd.studied} games, your plan's limit.`;
  return el("div", { class: "twin-profile" },
    el("section", { class: "sheet stack" }, el("h3", { text: "How faithful is it?" }),
      el("div", { class: "twin-big" }, el("b", { text: pct(f.twin_top3) }),
        el("span", { class: "small", text: `of their real moves were among the Twin's top 3 choices, in games it never saw (Stockfish's top 3: ${pct(f.engine_top3)}).` })),
      el("p", { class: "small muted", text: `First choice exactly right: Twin ${pct(f.twin_top1)}, Stockfish ${pct(f.engine_top1)}.${f.book_top1 != null ? ` Their opening moves predicted by their own book: ${f.book_top1}%.` : ""}` }),
      learningCurve(p.learning_curve || []),
      el("div", { class: "twin-study" },
        el("div", { class: "row between" }, el("b", { class: "small", text: `Studied ${sd.studied ?? t.games} of ${sd.available ?? "–"} games` }),
          el("span", { class: "tiny faint", text: `${studied}%` })),
        el("div", { class: "twin-acc-track" }, el("i", { style: { width: `${studied}%` } })),
        el("p", { class: "tiny faint", text: `${t.decisions.toLocaleString()} of their decisions, from ${p.oldest_game || "–"} to ${t.newest_game || "–"}. Recent games count more (a game two years older counts half). Their opening book uses every game in the library.` })),
      learn ? el("div", { class: "row" }, learn) : el("p", { class: "tiny faint", text: learnNote }),
      sd.available < 300 && player.platform !== "pgn" ? el("p", { class: "tiny" },
        "More games make a more faithful Twin. On their player page, ", el("b", { text: "Download full history" }), " gives it their whole career to learn from.") : null,
      note),
    el("section", { class: "sheet stack" }, el("h3", { text: "How they play" }),
      ...PHASES.map(([k, label]) => bar(label, p.accuracy && p.accuracy[k])),
      el("p", { class: "small muted", text: `${p.blunders_per_100 ?? "–"} blunders and ${p.mistakes_per_100 ?? "–"} mistakes per 100 moves.${tt.moves >= 15 && tt.accuracy != null ? ` In time trouble: ${tt.accuracy}% accuracy (${tt.calm_accuracy}% otherwise).` : ""}` }),
      p.style && p.style.length ? el("div", { class: "chips" }, ...p.style.map((s) => el("span", { class: "chip static", text: s.text }))) : null,
      p.castling && p.castling.usual_move ? el("p", { class: "small muted", text: `Castles around move ${p.castling.usual_move} (short ${p.castling.short}, long ${p.castling.long} of ${p.castling.games} games).` }) : null),
    el("section", { class: "sheet stack" }, el("h3", { text: "Their openings" }),
      open("white", "As White"), open("black_vs_e4", "Against 1.e4"), open("black_vs_d4", "Against 1.d4"),
      el("p", { class: "tiny faint", text: "The Twin plays these from their own games, weighting recent games more." })));
}

// Faithfulness as more of their games are used (each point: fitted on that many games, tested on the same held-out games).
function learningCurve(points) {
  if (points.length < 2) return null;
  const W = 260, H = 96, P = 22;
  const xs = points.map((p) => Math.log(p.games)), ys = points.map((p) => p.twin_top3 ?? 0);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = Math.max(0, Math.min(...ys) - 8), y1 = Math.min(100, Math.max(...ys) + 4);
  const X = (v) => P + ((v - x0) / (x1 - x0 || 1)) * (W - 2 * P);
  const Y = (v) => H - P + 6 - ((v - y0) / (y1 - y0 || 1)) * (H - P - 10);
  const line = points.map((p, i) => `${i ? "L" : "M"}${X(xs[i]).toFixed(1)},${Y(ys[i]).toFixed(1)}`).join(" ");
  const chart = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "twin-curve", role: "img",
    "aria-label": `Faithfulness rises from ${ys[0]}% with ${points[0].games} games to ${ys[ys.length - 1]}% with ${points[points.length - 1].games} games` },
    svg("line", { x1: P, x2: W - P, y1: H - P + 6, y2: H - P + 6, class: "axis" }),
    svg("path", { d: line, class: "curve" }),
    ...points.map((p, i) => svg("circle", { cx: X(xs[i]), cy: Y(ys[i]), r: 3.2, class: "dot" })),
    ...points.map((p, i) => svg("text", { x: X(xs[i]), y: H - 3, "text-anchor": "middle", class: "lbl" }, String(p.games))),
    svg("text", { x: X(xs[0]), y: Y(ys[0]) - 7, "text-anchor": "start", class: "val" }, `${ys[0]}%`),
    svg("text", { x: X(xs[xs.length - 1]), y: Y(ys[ys.length - 1]) - 7, "text-anchor": "end", class: "val" }, `${ys[ys.length - 1]}%`));
  return el("figure", { class: "twin-curve-wrap" }, chart,
    el("figcaption", { class: "tiny faint", text: "Learning curve: how often their real move is in the Twin's top 3, by how many of their games it learned from. A flat line means it already knows them well." }));
}

// ---------------- setting up a game ----------------
function setupCard(box, player, st, tc) {
  let color = "white";
  let mode = "faithful";
  const mainLine = el("input", { type: "checkbox", id: "twinMain" });
  const seg = (name, opts, get, set) => el("div", { class: "seg", role: "radiogroup", "aria-label": name },
    ...opts.map(([v, t]) => {
      const b = el("button", { type: "button", role: "radio", "aria-checked": String(get() === v), text: t });
      b.addEventListener("click", () => { set(v); b.parentElement.querySelectorAll("button").forEach((x) => x.setAttribute("aria-checked", String(x === b))); });
      return b;
    }));
  const start = el("button", { class: "btn", type: "button", text: "Start the game", onclick: () => {
    const c = color === "random" ? (Math.random() < 0.5 ? "white" : "black") : color;
    const g = newGame(player.id, c, mode, tc, mainLine.checked);
    games[player.id] = g; persist(g);
    track("Twin game", { mode });
    playGame(box, player, g, st);
  } });
  fill(box, el("section", { class: "sheet stack twin-setup" }, el("h3", { text: `Play ${player.handle}'s Twin` }),
    el("div", { class: "form-row" },
      el("div", { class: "field" }, el("span", { class: "label", text: "You play" }),
        seg("You play", [["white", "White"], ["black", "Black"], ["random", "Random"]], () => color, (v) => { color = v; })),
      el("div", { class: "field" }, el("span", { class: "label", text: "The Twin plays" }),
        seg("Strength", [["faithful", "Exactly like them"], ["good_day", "On a good day"]], () => mode, (v) => { mode = v; }))),
    el("p", { class: "tiny faint", text: "\"On a good day\" keeps their openings and style but makes fewer mistakes: prepare for their best." }),
    el("label", { class: "check", for: "twinMain" }, mainLine,
      "Rehearse their main lines: while in their book, always play the move they've played most"),
    el("div", { class: "row" }, start)));
}

// ---------------- the game ----------------
function playGame(box, player, g, st) {
  const boardHost = el("div", { class: "twin-board" });
  const board = new Board(boardHost, { fen: g.fens[g.fens.length - 1], orientation: g.color, interactive: true, movable: g.color,
    lastMove: g.moves.length ? g.moves[g.moves.length - 1] : null, label: `Practice game against ${player.handle}'s Twin`,
    onMove: (_f, _t, info) => onYourMove(info) });
  const status = el("div", { class: "twin-status", "aria-live": "polite" });
  const why = el("div", { class: "twin-why" });
  const sheet = el("ol", { class: "twin-moves" });
  const reviewBox = el("div", { class: "stack-lg" });
  let thinking = false;
  const yourTurn = () => !g.over && sideToMove(g.fens[g.fens.length - 1]) === g.color;

  function drawMoves() {
    const items = [];
    for (let i = 0; i < g.sans.length; i += 2) {
      items.push(el("li", {}, el("span", { class: "mv", text: g.sans[i] }), g.sans[i + 1] ? el("span", { class: "mv", text: g.sans[i + 1] }) : null));
    }
    sheet.replaceChildren(...items);
    sheet.scrollTop = sheet.scrollHeight;
  }
  function drawStatus() {
    if (g.over) {
      const won = (g.over.result === "1-0" && g.color === "white") || (g.over.result === "0-1" && g.color === "black");
      const draw = g.over.result === "1/2-1/2";
      fill(status, el("strong", { text: draw ? "Draw" : won ? "You won" : `${player.handle}'s Twin won` }),
        el("span", { text: `${g.over.result}${g.over.reason ? `, by ${g.over.reason}` : ""}` }));
      status.className = `twin-status ${draw ? "" : won ? "good" : "bad"}`;
    } else {
      fill(status, el("strong", { text: thinking ? "The Twin is thinking…" : yourTurn() ? "Your move" : "Twin to move" }),
        el("span", { text: `You're ${g.color}. ${g.mode === "good_day" ? "Twin on a good day." : "Twin plays exactly like them."}${g.mainLine ? " Rehearsing their main lines." : ""}` }));
      status.className = "twin-status";
    }
    board.set({ locked: thinking || !yourTurn() });
    resignBtn.hidden = !!g.over;
    reviewBtn.hidden = !g.over || !g.moves.length;
  }
  function drawWhy() {
    const l = g.last;
    if (!l) { fill(why, el("p", { class: "small muted", text: g.color === "white" ? "You have the first move." : "The Twin opens…" })); return; }
    fill(why, el("p", { class: "small" }, el("span", { class: `tag ${l.source === "book" ? "ok" : ""}`, text: l.source === "book" ? "Their book" : l.source === "forced" ? "Forced" : "Their model" }),
      " ", l.why),
    l.alternatives && l.alternatives.length > 1 ? el("div", { class: "twin-alts" }, el("span", { class: "tiny faint", text: l.source === "book" ? "What they've played here:" : "What the Twin considered:" }),
      ...l.alternatives.map((a) => el("div", { class: "twin-alt" }, el("b", { text: a.san }),
        el("div", { class: "twin-acc-track" }, el("i", { style: { width: `${a.pct}%` } })), el("span", { class: "tiny", text: `${a.pct}%${a.games ? ` · ${a.games} game${a.games === 1 ? "" : "s"}` : ""}` })))) : null);
  }
  function push(uci, san, fen) {
    g.moves.push(uci); g.sans.push(san); g.fens.push(fen);
  }
  async function twinTurn() {
    if (g.over || yourTurn()) return;
    thinking = true; drawStatus();
    let r;
    // A short, human-feeling pause: the server answers in a fraction of a second.
    const pause = new Promise((res) => setTimeout(res, 450 + Math.random() * 700));
    try {
      [r] = await Promise.all([post(`/api/twin/${player.id}/move`, { moves: g.moves, time_classes: g.tc ? g.tc.split(",") : null, mode: g.mode, main_line: !!g.mainLine }), pause]);
    }
    catch (e) { thinking = false; drawStatus(); fill(why, el("p", { class: "err", text: e.message }), el("button", { class: "btn quiet small", type: "button", text: "Try again", onclick: twinTurn })); return; }
    thinking = false;
    if (r.uci) {
      push(r.uci, r.san, r.fen);
      g.last = { source: r.source, why: r.why, alternatives: r.alternatives };
      board.set({ fen: r.fen, lastMove: r.uci, sound: true });
    }
    if (r.over) g.over = r.over;
    persist(g); drawMoves(); drawWhy(); drawStatus();
  }
  async function onYourMove(info) {
    push(info.uci, info.san, info.fen);
    persist(g); drawMoves();
    // The server checks for the end of the game (stalemate, repetition…) when it's asked for the Twin's move.
    await twinTurn();
  }
  const takeBack = () => {
    if (thinking || !g.moves.length) return;
    g.over = null; g.review = null; reviewBox.replaceChildren();
    do { g.moves.pop(); g.sans.pop(); g.fens.pop(); } while (g.moves.length && !yourTurn());
    if (!yourTurn() && !g.moves.length) { persist(g); board.set({ fen: START, lastMove: null }); drawMoves(); twinTurn(); return; }
    g.last = null; persist(g);
    board.set({ fen: g.fens[g.fens.length - 1], lastMove: g.moves[g.moves.length - 1] || null });
    drawMoves(); drawWhy(); drawStatus();
  };
  const resignBtn = el("button", { class: "btn quiet small", type: "button", text: "Resign", onclick: (e) => {
    const b = e.currentTarget;
    if (b.dataset.confirm !== "1") { b.dataset.confirm = "1"; b.textContent = "Click again to resign"; return; }
    g.over = { result: g.color === "white" ? "0-1" : "1-0", reason: "resignation" };
    persist(g); drawStatus();
  } });
  const reviewBtn = el("button", { class: "btn small", type: "button", text: "Review the game", onclick: () => review() });
  const newBtn = el("button", { class: "btn quiet small", type: "button", text: "New game", onclick: () => {
    delete games[player.id]; store.set(saveKey(player.id), null); setupCard(box, player, st, g.tc);
  } });
  async function review() {
    reviewBtn.disabled = true;
    fill(reviewBox, el("p", { class: "muted", text: "Checking every move with Stockfish…" }));
    try {
      const r = await post(`/api/twin/${player.id}/review`, { moves: g.moves, color: g.color, time_classes: g.tc ? g.tc.split(",") : null });
      g.review = r; persist(g); drawReview();
    } catch (e) { fill(reviewBox, el("p", { class: "err", text: e.message })); }
    finally { reviewBtn.disabled = false; }
  }
  function drawReview() {
    const r = g.review;
    if (!r) return;
    fill(reviewBox, el("section", { class: "sheet stack" }, el("h3", { text: "Game review" }),
      el("div", { class: "twin-score" },
        el("div", {}, el("b", { text: r.your_accuracy != null ? `${r.your_accuracy}%` : "–" }), el("span", { class: "small muted", text: "your accuracy" })),
        el("div", {}, el("b", { text: r.twin_accuracy != null ? `${r.twin_accuracy}%` : "–" }), el("span", { class: "small muted", text: "the Twin's" }))),
      r.left_book ? el("p", { class: "small", text: `The game left their own games at move ${r.left_book.move}${r.left_book.after ? `, after ${r.left_book.after}` : ""}. From there the Twin played from its model of them.` })
        : el("p", { class: "small", text: "The whole game stayed inside positions they've played before." }),
      r.mistakes.length ? el("h4", { text: "Your costliest moves" }) : el("p", { class: "small ok-text", text: "No inaccuracies, mistakes or blunders from you. Well played." }),
      ...r.mistakes.map((m) => el("div", { class: "prep-row" },
        el("div", {}, el("span", { class: "line", text: m.move }),
          el("p", { class: "small", text: `${m.classification[0].toUpperCase()}${m.classification.slice(1)}: cost ${m.win_loss}% of your winning chances.${m.best ? ` Better was ${m.best}.` : ""}` })),
        el("button", { class: "btn quiet small", type: "button", text: "Show", onclick: () => openPosition({ fen: m.fen, orientation: g.color,
          played_uci: m.played_uci, best_uci: m.best_uci, you_played: m.move, engine_best: m.best }, { title: `Move ${m.move}` }) })))));
  }
  fill(box, el("section", { class: "twin-game" },
    el("div", { class: "stack" }, boardHost,
      el("div", { class: "board-foot" }, el("div", { class: "row" },
        el("button", { class: "btn quiet small", type: "button", text: "Take back", onclick: takeBack }), resignBtn, newBtn), boardSettings(board))),
    el("aside", { class: "sheet twin-panel" }, status, why, sheet, el("div", { class: "row" }, reviewBtn))), reviewBox);
  drawMoves(); drawWhy(); drawStatus(); drawReview();
  if (!g.over && !yourTurn()) twinTurn();
}

// Used by the library's player page.
export const hasGame = (pid) => !!restore(pid);
