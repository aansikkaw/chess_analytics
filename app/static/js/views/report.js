// Game report card: one game, both players, every move checked by Stockfish, then written up by the coach.
// Route: #/report/<accountId>/<gameId>. Grades and accuracy per player, the evaluation graph, move classes,
// key moments on the board, phase grades, the coach's review, and an annotated PGN to download.
// Guided view (default): winning chances, the one best line, tips and a first-visit tour. Pro view: evaluations in
// pawns, engine depth, three engine lines, coordinates, FEN/PGN copy. Same page, same data (DESIGN.md).

import { api, el, evalText, fill, post, proView, store, svg, toast, track, winPct } from "../core.js";
import { Board, applyMove, boardSettings, coordsPref, uciArrow } from "../board.js";
import { pageKeys } from "../keys.js";
import { Chess, validateFen } from "../../vendor/chess.js";
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
const BAD = ["inaccuracy", "mistake", "miss", "blunder"];
/** Guided view: one habit to take away, from what went wrong on the move. */
const TIPS = {
  allowed_mate: "Before going after material, check that your king is safe.",
  missed_mate: "When the enemy king is exposed, look at every check: one of them may mate.",
  hanging_piece: "Before each move, ask which of your pieces it leaves undefended.",
  missed_tactic: "Look at checks, captures and threats first, for both sides.",
  missed_punish: "Your opponent had just gone wrong. Look for the move that punishes it.",
  let_slip: "When you're winning, trade pieces and keep the position simple.",
  time_trouble: "Spend less time early on, so you have it for moments like this.",
};
const TOUR_KEY = "pb:tour:report:v1";
const START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";

/** "12.Nxe6 Qc8 13.Nxf8", or "12…Nxg6 13.Qh5" when Black moves first, numbered from `fen`. */
function numberedFrom(fen, sans) {
  const [side, , , , , full] = fen.split(" ");
  let n = Number(full) || 1, white = side !== "b";
  return sans.map((san, i) => {
    const t = white ? `${n}.${san}` : i === 0 ? `${n}…${san}` : san;
    if (!white) n += 1;
    white = !white;
    return t;
  }).join(" ");
}

/** A FEN, or a PGN (its moves, from its own start position), from pasted text; null if it's neither. */
function parsePasted(text) {
  const t = (text || "").trim();
  if (!t) return null;
  if (!t.includes("[") && validateFen(t).ok) return { rootFen: t, moves: [] };
  const g = new Chess();
  try { g.loadPgn(t); } catch { return null; }
  const hist = g.history({ verbose: true });
  if (!hist.length) return null;
  return { rootFen: g.getHeaders().FEN || START, moves: hist.map((m) => ({ san: m.san, uci: m.from + m.to + (m.promotion || ""), fen: m.after })) };
}

let pasteDlg = null;
/** Paste a FEN or a PGN; `done({ rootFen, moves })` with what was read. */
function openPaste(done) {
  if (!pasteDlg) { pasteDlg = el("dialog", { class: "narrow", "aria-labelledby": "pasteTitle" }); document.body.append(pasteDlg); }
  const area = el("textarea", { id: "pasteText", rows: "6", spellcheck: "false", placeholder: "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1\n\nor a PGN: 1. e4 e5 2. Nf3 …" });
  const err = el("p", { class: "err small", role: "alert", hidden: true });
  const form = el("form", { class: "stack" }, el("label", { for: "pasteText", text: "Paste a FEN (one position) or a PGN (a game)" }), area, err,
    el("div", { class: "row" }, el("button", { class: "btn", type: "submit", text: "Show it on the board" }),
      el("button", { class: "btn quiet", type: "button", "data-close": "", text: "Cancel" })));
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const v = parsePasted(area.value);
    if (!v) { err.hidden = false; err.textContent = "That isn't a FEN or a PGN this board can read. Check it's the whole FEN (six parts) or a PGN with moves."; return; }
    pasteDlg.close();
    done(v);
  });
  fill(pasteDlg, el("div", { class: "dlg-head" }, el("h2", { id: "pasteTitle", text: "Paste a position or a game" }),
    el("button", { class: "x", type: "button", "data-close": "", "aria-label": "Close", text: "×" })), el("div", { class: "dlg-body" }, form));
  pasteDlg.showModal();
  if (navigator.clipboard && navigator.clipboard.readText) navigator.clipboard.readText().then((t) => { if (!area.value && parsePasted(t)) area.value = t.trim(); }).catch(() => {});
  area.focus();
}

/** Engine lines for a position, cached per page. `n` 1 (Guided best line) or 3 (Pro). */
const lineCache = new Map();
async function engineLines(fen, n) {
  const key = `${n}|${fen}`;
  if (!lineCache.has(key)) lineCache.set(key, post("/api/engine/analyse", { fen, lines: n }).catch((e) => { lineCache.delete(key); throw e; }));
  return lineCache.get(key);
}

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

  // ---- the analysis board: the game, your own lines, the engine (DESIGN.md: board, eval bar, move list) ----
  const pro = proView();
  body.classList.add("report-page");
  const boardHost = el("div", { class: "report-board" });
  const board = new Board(boardHost, { fen: START, orientation: you || "white", label: "Game board", coords: coordsPref(pro), followCoords: true,
    interactive: true, onMove: (_f, _t, info) => played(info) });
  const evalFill = el("div", { class: "evalbar-fill" });
  const evalBar = el("div", { class: "evalbar-v", role: "img", "aria-label": "Evaluation" }, evalFill, el("div", { class: "evalbar-mid", "aria-hidden": "true" }));
  const note = el("div", { class: "report-note", "aria-live": "polite" });
  const list = el("ol", { class: "report-moves" });
  const items = [], rows = [];
  for (let i = 0; i < moves.length; i += 2) {
    const li = el("li", {}, el("span", { class: "n", text: `${moves[i].move_number}.` }));
    for (const m of [moves[i], moves[i + 1]].filter(Boolean)) {
      const [sym, label, color] = CLASS[m.class];
      const b = el("button", { type: "button", class: `mv c-${m.class}`, "aria-label": `${num(m)}${m.san}, ${label}`, onclick: () => go(m.ply + 1) },
        m.san, sym ? el("b", { style: { color }, text: sym }) : null, pro ? el("span", { class: "mv-eval", text: evalText(m.eval_white, null, 1) }) : null);
      items[m.ply] = b;
      li.append(b);
    }
    rows.push(li);
    list.append(li);
  }
  const varLi = el("li", { class: "variation", hidden: true });

  let cur = 0;           // plies of the game played on the board
  let shownPly = 0;      // the game position on the board: cur, or cur - 1 while a mistake is shown with its better move
  let vari = null;       // your own line: { rootPly (null for a pasted position), rootFen, moves: [{ san, uci, fen }], idx }
  let exploring = false; // the board shows your line, not the game
  let engineOn = store.get("pb:engine") !== false;
  let lastEval = { white: null, text: "" };

  const fenAfter = new Map();
  const afterFen = (m) => {
    if (!fenAfter.has(m.ply)) {
      const next = moves[m.ply + 1];
      fenAfter.set(m.ply, next ? next.fen : rep.final_fen || (applyMove(m.fen, m.uci) || {}).fen || m.fen);
    }
    return fenAfter.get(m.ply);
  };
  const gameFen = (n) => (n === 0 ? (moves.length ? moves[0].fen : START) : afterFen(moves[n - 1]));

  /** The bar beside the board: the bottom side's winning chances, filled from the bottom. `white` is 0-100 or null (unknown). */
  function setEval(white, text) {
    lastEval = { white, text };
    const bottom = board.o.orientation;
    const known = white != null;
    evalBar.dataset.bottom = bottom === (you || "white") ? "you" : "opponent";
    evalBar.classList.toggle("unknown", !known);
    evalFill.style.height = `${known ? (bottom === "white" ? white : 100 - white) : 50}%`;
    evalBar.setAttribute("aria-label", known ? `Evaluation: ${text}` : "Evaluation: engine off");
    evalBar.title = known ? text : "Engine off";
  }
  const evalLabel = (white, cp = null, mate = null) =>
    pro && (cp != null || mate != null) ? evalText(cp, mate) : `White's winning chances ${Math.round(white)}%`;
  const fromEngine = (r) => {
    const ln = r.lines && r.lines[0];
    if (!ln) return null;
    return ln.mate != null ? (ln.mate > 0 ? 100 : 0) : winPct(ln.cp);
  };

  function markCurrent() {
    items.forEach((b) => b && b.removeAttribute("aria-current"));
    const m = cur > 0 ? moves[cur - 1] : null;
    if (!exploring && m && items[m.ply]) { items[m.ply].setAttribute("aria-current", "true"); items[m.ply].scrollIntoView({ block: "nearest", inline: "nearest" }); }
    backBtn.style.visibility = exploring ? "visible" : "hidden";
  }

  function go(n, { showBest = true } = {}) {
    exploring = false;
    cur = Math.max(0, Math.min(moves.length, n));
    const m = cur > 0 ? moves[cur - 1] : null;
    const bad = m && BAD.includes(m.class);
    if (bad && showBest) { // the position before the mistake: what was played (blue) and what was better (red)
      shownPly = cur - 1;
      board.set({ fen: m.fen, lastMove: m.ply > 0 ? moves[m.ply - 1].uci : null, arrows: [uciArrow(m.uci, "you"), uciArrow(m.best, "coach")].filter(Boolean) });
    } else {
      shownPly = cur;
      board.set({ fen: gameFen(cur), lastMove: m ? m.uci : null, arrows: [] });
    }
    markCurrent();
    drawVariation();
    graph.mark(cur);
    const white = rep.series ? rep.series[shownPly] : null;
    setEval(white, evalLabel(white, shownPly > 0 ? moves[shownPly - 1].eval_white : null));
    if (pro) showLines(board.fen);
    if (!m) { fill(note, el("p", { class: "small muted", text: "Start position. Use the arrows, the graph or the move list, or play a move on the board." })); return; }
    const [sym, label, color] = CLASS[m.class];
    const head = el("p", {}, el("b", { class: "mono", text: `${num(m)}${m.san}${sym}` }), " ", el("span", { class: "cls", style: { background: color }, text: label }));
    if (pro) {
      const before = m.ply > 0 ? `${evalText(moves[m.ply - 1].eval_white)} → ` : "";
      head.append(el("span", { class: "mono note-eval", title: "Stockfish's evaluation before → after the move, from White's side",
        text: ` ${before}${evalText(m.eval_white)}` }),
        el("span", { class: "tiny faint", text: ` · depth ${rep.depth}` }), m.loss >= 1 ? el("span", { class: "tiny faint", text: ` · −${Math.round(m.loss)}% win chance` }) : "");
      fill(note, head, bad && m.best_san ? el("p", { class: "small" }, "Best was ", el("b", { class: "mono", text: m.best_san }), ".") : null);
      return;
    }
    // Guided: winning chances for the side that moved, then the better move and its line, then one tip.
    const who = m.color === you ? "Your" : `${cap(m.color)}'s`;
    const chances = el("p", { class: "small" }, `${who} winning chances: `, el("b", { text: `${Math.round(m.win_before)}%` }), " → ",
      el("b", { text: `${Math.round(m.win_after)}%` }), m.loss >= 1 ? el("span", { class: "muted", text: ` (−${Math.round(m.loss)}%)` }) : "");
    const bestLine = el("p", { class: "small best-line" });
    const tip = bad ? (m.tags || []).map((t) => TIPS[t]).find(Boolean) || "Compare your move with the red arrow: what does it do that yours doesn't?" : null;
    fill(note, head, chances,
      bad && m.best_san ? el("p", { class: "small" }, "Better was ", el("b", { text: m.best_san }), ". The red arrow shows it; the blue one, the move played.") : null,
      bad ? bestLine : null,
      tip ? el("p", { class: "small tip" }, el("b", { text: "Tip: " }), tip) : null,
      m.class === "great" ? el("p", { class: "small muted", text: "The only good move here, and it was found." }) : null,
      m.class === "brilliant" ? el("p", { class: "small muted", text: "A sound sacrifice: giving material for something better." }) : null,
      m.class === "book" ? el("p", { class: "small muted", text: "Known opening theory." }) : null);
    if (bad && engineOn) {
      const at = cur;
      engineLines(m.fen, 1).then((r) => {
        if (at !== cur || exploring || !r.lines || !r.lines.length) return;
        fill(bestLine, el("span", { class: "muted", text: "Best line: " }), el("span", { class: "mono", text: r.lines[0].text }));
      }).catch(() => {});
    }
  }

  // ---- your own lines: play a move on the board (or Space for the engine's) to explore from any position ----
  function played(info) {
    if (!exploring && moves[shownPly] && moves[shownPly].uci === info.uci) { go(shownPly + 1, { showBest: false }); return; }
    if (exploring && vari.idx < vari.moves.length && vari.moves[vari.idx].uci === info.uci) { vari.idx += 1; showVariation(); return; }
    if (!exploring) vari = { rootPly: shownPly, rootFen: info.before, moves: [], idx: 0 };
    vari.moves = vari.moves.slice(0, vari.idx);
    vari.moves.push({ san: info.san, uci: info.uci, fen: info.fen });
    vari.idx = vari.moves.length;
    showVariation();
  }
  function showVariation({ animate = true } = {}) {
    exploring = true;
    const fen = vari.idx ? vari.moves[vari.idx - 1].fen : vari.rootFen;
    const last = vari.idx ? vari.moves[vari.idx - 1].uci : null;
    board.set({ fen, lastMove: last, arrows: [], marks: {}, animate });
    markCurrent();
    drawVariation();
    const line = numberedFrom(vari.rootFen, vari.moves.slice(0, vari.idx).map((v) => v.san));
    const evalP = el("p", { class: "small" });
    fill(note, el("p", {}, el("b", { text: vari.rootPly == null ? "Your position" : "Your line" }), " ",
      el("span", { class: "mono", text: line || "(start)" })),
    el("p", { class: "small muted", text: vari.rootPly == null ? "Pasted, not from this game." : "Not played in the game. Esc or Back to game returns." }), evalP);
    if (pro) showLines(fen);
    if (!engineOn) { setEval(null, ""); fill(evalP, el("span", { class: "muted", text: "Engine off. Press E to turn it on." })); return; }
    engineLines(fen, pro ? 3 : 1).then((r) => {
      if (board.fen !== fen || !exploring) return;
      if (r.over) { setEval(r.result === "1-0" ? 100 : r.result === "0-1" ? 0 : 50, `Game over: ${r.result}`); fill(evalP, `Game over: ${r.result}`); return; }
      const white = fromEngine(r), ln = r.lines[0];
      setEval(white, evalLabel(white, ln.cp, ln.mate));
      if (!pro) fill(evalP, `White's winning chances: `, el("b", { text: `${Math.round(white)}%` }), ". Best here: ", el("span", { class: "mono", text: ln.text }));
    }).catch((e) => fill(evalP, el("span", { class: "muted", text: e.status === 429 ? e.message : "The engine isn't available right now." })));
  }
  function backToGame() { if (exploring) go(cur); }
  /** Your line in the move list, as a branch under the move it starts from. */
  function drawVariation() {
    if (!vari) { varLi.hidden = true; return; }
    const at = vari.rootPly == null || vari.rootPly === 0 ? 0 : Math.floor((vari.rootPly - 1) / 2) + 1;
    const anchor = rows[at] || null;
    if (varLi.nextSibling !== anchor || varLi.parentNode !== list) list.insertBefore(varLi, anchor);
    const [side, , , , , full] = vari.rootFen.split(" ");
    let n = Number(full) || 1, white = side !== "b";
    const btns = vari.moves.map((v, i) => {
      const label = white ? `${n}.${v.san}` : i === 0 ? `${n}…${v.san}` : v.san;
      if (!white) n += 1;
      white = !white;
      const b = el("button", { type: "button", class: "mv var-mv", text: label, onclick: () => { vari.idx = i + 1; showVariation(); } });
      if (exploring && vari.idx === i + 1) b.setAttribute("aria-current", "true");
      return b;
    });
    fill(varLi, el("span", { class: "var-label", text: vari.rootPly == null ? "Pasted" : "Your line" }), ...btns);
    varLi.hidden = false;
  }

  // ---- keys and buttons ----
  function step(d) {
    if (!exploring) { go(cur + d); return; }
    if (d < 0 && vari.idx === 0) { backToGame(); return; }
    vari.idx = Math.max(0, Math.min(vari.moves.length, vari.idx + d));
    showVariation();
  }
  function jump(end) {
    if (exploring) { vari.idx = end ? vari.moves.length : 0; showVariation(); } else go(end ? moves.length : 0);
  }
  function flip() { board.flip(); setEval(lastEval.white, lastEval.text); }
  function toggleEngine() {
    engineOn = !engineOn;
    store.set("pb:engine", engineOn);
    engineBtn.setAttribute("aria-pressed", String(engineOn));
    engineBtn.lastChild.textContent = engineOn ? "Engine on" : "Engine off";
    if (exploring) showVariation({ animate: false }); else go(cur, { showBest: shownPly !== cur });
  }
  async function playBest() {
    const fen = board.fen;
    let uci = !exploring && moves[shownPly] ? moves[shownPly].best : null;
    if (!uci) {
      if (!engineOn) { toast("Turn the engine on (E) to play its best move."); return; }
      try { uci = (await engineLines(fen, pro ? 3 : 1)).best; } catch (e) { toast(e.message); return; }
      if (board.fen !== fen) return;
    }
    const r = uci ? applyMove(fen, uci) : null;
    if (!r) return; // game over
    board.set({ fen: r.fen, lastMove: r.uci, arrows: [], marks: {}, sound: true });
    played({ ...r, before: fen });
  }
  pageKeys(body, "Analysis board", [
    ["←", "Previous move"], ["→", "Next move"], ["↑", "Start"], ["↓", "End"], ["F", "Flip the board"], ["E", "Engine on or off"],
    ["Space", "Play the engine's best move"], ["Esc", "Back to the game, from your own line"],
  ], (e) => {
    if (e.key === " " && e.target.closest && e.target.closest("button, a, summary")) return false; // Space still presses buttons
    switch (e.key) {
      case "ArrowLeft": step(-1); return true;
      case "ArrowRight": step(1); return true;
      case "ArrowUp": jump(false); return true;
      case "ArrowDown": jump(true); return true;
      case "f": case "F": flip(); return true;
      case "e": case "E": toggleEngine(); return true;
      case " ": playBest(); return true;
      case "Escape": if (exploring) { backToGame(); return true; } return false;
      default: return false;
    }
  });

  const navBtn = (label, path, f) => el("button", { class: "btn quiet small icon-btn", type: "button", "aria-label": label, title: label, onclick: f },
    svg("svg", { width: 16, height: 16, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", "stroke-width": 2, "stroke-linecap": "round",
      "stroke-linejoin": "round", "aria-hidden": "true" }, ...path.map((d) => svg("path", { d }))));
  const nav = el("div", { class: "row report-nav" },
    navBtn("Start (↑)", ["M6 5v14", "M18 6l-8 6 8 6z"], () => jump(false)), navBtn("Previous move (←)", ["M15 6l-6 6 6 6"], () => step(-1)),
    navBtn("Next move (→)", ["M9 6l6 6-6 6"], () => step(1)), navBtn("End (↓)", ["M18 5v14", "M6 6l8 6-8 6z"], () => jump(true)));
  const engineBtn = el("button", { class: "btn quiet small", type: "button", "aria-pressed": String(engineOn), title: "Engine on or off (E)",
    onclick: toggleEngine }, el("span", { class: "engine-dot", "aria-hidden": "true" }), el("span", { text: engineOn ? "Engine on" : "Engine off" }));
  const bestBtn = el("button", { class: "btn quiet small", type: "button", title: "Play the engine's best move (Space)", text: "Best move", onclick: playBest });
  const backBtn = el("button", { class: "btn small back-btn", type: "button", text: "Back to game", onclick: backToGame, style: { visibility: "hidden" } });

  // ---- Pro: three engine lines for the position on the board ----
  const linesBox = el("section", { class: "engine-lines", "aria-label": "Engine lines", "aria-live": "polite" });
  let linesTimer = null;
  function showLines(fen) {
    clearTimeout(linesTimer);
    if (!engineOn) { fill(linesBox, el("p", { class: "small muted", text: "Engine off. Press E to turn it on." })); return; }
    fill(linesBox, el("p", { class: "tiny faint", text: "Engine thinking…" }));
    linesTimer = setTimeout(async () => { // stepping quickly through moves asks the engine once, for where you stop
      try {
        const r = await engineLines(fen, 3);
        if (board.fen !== fen) return;
        if (r.over) { fill(linesBox, el("p", { class: "small muted", text: `Game over: ${r.result}` })); return; }
        fill(linesBox, el("div", { class: "spread" }, el("b", { class: "small", text: "Engine lines" }),
          el("span", { class: "tiny faint", text: `Stockfish · depth ${r.depth}` })),
        el("ol", { class: "lines-list" }, ...r.lines.map((ln) => el("li", {},
          el("span", { class: "mono line-eval", text: evalText(ln.cp, ln.mate) }), el("span", { class: "mono line-moves", text: ln.text })))));
      } catch (e) { fill(linesBox, el("p", { class: "small muted", text: e.status === 429 ? e.message : "The engine isn't available right now." })); }
    }, 250);
  }

  const graph = evalGraph(rep, (n) => go(n));

  // ---- the position: copy, paste, open on Lichess ----
  const copy = async (text, what) => {
    try { await navigator.clipboard.writeText(text); toast(`${what} copied.`); } catch { toast(`Couldn't copy the ${what}. Your browser blocked it.`); }
  };
  /** The game's annotated PGN, or, while you explore, the moves on the board: the game up to your line, then your line. */
  async function currentPgn() {
    if (!exploring) {
      const r = await fetch(`${url}.pgn`, { credentials: "same-origin" });
      if (!r.ok) throw new Error("Couldn't load the PGN.");
      return r.text();
    }
    const g = vari.rootPly == null ? new Chess(vari.rootFen) : new Chess();
    if (vari.rootPly != null) for (const m of moves.slice(0, vari.rootPly)) g.move(m.san);
    for (const v of vari.moves.slice(0, vari.idx)) g.move(v.san);
    g.setHeader("White", rep.white || "?");
    g.setHeader("Black", rep.black || "?");
    return g.pgn();
  }
  const copyPgn = async () => { try { copy(await currentPgn(), "PGN"); } catch (e) { toast(e.message); } };
  const lichessUrl = () => `https://lichess.org/analysis/standard/${board.fen.replace(/ /g, "_")}`;
  const menuItem = (text, onclick) => el("button", { type: "button", class: "menu-item", text, onclick: (e) => { e.currentTarget.closest("details").open = false; onclick(); } });
  const posMenu = el("details", { class: "board-menu pos-menu" },
    el("summary", { class: "btn quiet small", text: "Position" }),
    el("div", { class: "board-menu-panel menu-list" },
      menuItem("Copy FEN", () => copy(board.fen, "FEN")),
      menuItem("Copy PGN", copyPgn),
      menuItem("Paste a FEN or PGN…", () => openPaste((v) => { vari = { rootPly: null, idx: v.moves.length, ...v }; showVariation({ animate: false }); })),
      el("a", { class: "menu-item", href: "#", target: "_blank", rel: "noopener", text: "Open on Lichess",
        onclick: (e) => { e.currentTarget.href = lichessUrl(); e.currentTarget.closest("details").open = false; } })));
  posMenu.addEventListener("toggle", () => {
    const close = (e) => { if (!posMenu.contains(e.target) || (e.type === "keydown" && e.key === "Escape")) posMenu.open = false; };
    if (posMenu.open) { document.addEventListener("pointerdown", close); document.addEventListener("keydown", close); posMenu._close = close; }
    else if (posMenu._close) { document.removeEventListener("pointerdown", posMenu._close); document.removeEventListener("keydown", posMenu._close); }
  });
  const copyRow = pro ? el("div", { class: "row copy-row" },
    el("button", { class: "btn quiet small", type: "button", text: "Copy FEN", onclick: () => copy(board.fen, "FEN") }),
    el("button", { class: "btn quiet small", type: "button", text: "Copy PGN", onclick: copyPgn })) : null;

  // ---- the grade cards ----
  const gradeCard = (c) => {
    const s = rep.sides[c];
    const name = rep[c] || cap(c);
    return el("div", { class: `grade-card ${you === c ? "you" : ""}` },
      el("div", { class: "grade-top" }, el("span", { class: `piece ${c}`, "aria-hidden": "true" }),
        el("div", {}, el("b", { text: name }), el("span", { class: "tiny faint", text: you === c ? `${cap(c)} · you` : cap(c) }))),
      el("div", { class: "grade-main" }, el("span", { class: `grade g-${s.grade || "none"}`, text: s.grade || "–" }),
        el("div", {}, el("b", { class: "figure", text: s.accuracy != null ? `${s.accuracy}%` : "–" }), el("span", { class: "tiny faint", text: "accuracy" }))),
      el("p", { class: "tiny faint", text: !pro ? `${s.moves} moves` : s.acpl != null ? `Average loss ${s.acpl} centipawns a move · ${s.moves} moves` : "" }),
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
        el("span", { class: "tiny faint", text: pro ? `Stockfish depth ${rep.depth}, both players, top two moves in every position.`
          : "Every move from both players, checked by Stockfish." }))),
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
        el("b", { class: pro ? "mono" : "", text: `${k.label}${CLASS[k.class][0]}` }),
        el("span", { class: "tiny faint", text: ` ${k.color === you ? "you" : k.color} · ` + (pro && moves[k.ply]
          ? `${evalText(k.ply > 0 ? moves[k.ply - 1].eval_white : 0)} → ${evalText(moves[k.ply].eval_white)}` : `−${Math.round(k.loss)}%`) }),
        el("span", { class: "small", text: k.explain }),
        k.line && k.line.length ? el("span", { class: "tiny faint", text: `Engine line: ${k.line.join(" ")}` }) : null))))
      : el("p", { class: "small muted", text: "No big swings: a clean game from both sides." }));

  // ---- the coach's review ----
  const coach = coachCard(d, url, () => load(body, accountId, gameId, head), (ply) => go(ply + 1));

  fill(body, hero, el("section", { class: "sheet report-graph" }, el("div", { class: "spread" }, el("h3", { text: "Evaluation" }),
    el("span", { class: "tiny faint", text: "White's winning chances move by move. Click to jump." })), graph.node),
  el("div", { class: "report-main" },
    el("div", { class: "report-left" }, el("div", { class: "board-row" }, evalBar, boardHost),
      el("div", { class: "board-foot" }, nav, backBtn), el("div", { class: "board-foot" }, engineBtn, bestBtn, posMenu, boardSettings(board)),
      copyRow, note, pro ? linesBox : null, el("div", { class: "report-movebox" }, list)),
    el("div", { class: "report-right stack-lg" }, coach, km)),
  el("div", { class: "grid-2 even" }, phases, classes));
  const firstKey = rep.key_moments.find((k) => k.color === you) || rep.key_moments[0];
  go(firstKey ? firstKey.ply + 1 : 0);
  if (!pro && !store.get(TOUR_KEY)) {
    tour([
      { target: hero, title: "How the game was decided", text: "The verdict in one line, and both players' grades. A is 90% accuracy or better." },
      { target: graph.node, title: "Who was winning", text: "Your winning chances move by move. Dots mark the mistakes; click anywhere to jump there." },
      { target: boardHost, title: "Blue is you, red is the coach", text: "On a mistake, the blue arrow is the move played and the red arrow is the better one." },
      { target: list, title: "Every move, marked", text: "Click any move, or use ← and →. Play a move on the board to try your own line. Press ? for every shortcut." },
    ]);
  }
}

/** A short first-visit tour: one card at the bottom of the screen, the part it talks about outlined. */
function tour(steps) {
  let i = 0;
  const box = el("div", { class: "tour", role: "dialog", "aria-labelledby": "tourTitle" });
  const done = () => {
    store.set(TOUR_KEY, 1);
    document.querySelectorAll(".tour-target").forEach((n) => n.classList.remove("tour-target"));
    document.removeEventListener("keydown", onKey);
    window.removeEventListener("hashchange", done);
    box.remove();
  };
  const onKey = (e) => { if (e.key === "Escape") done(); };
  function show() {
    document.querySelectorAll(".tour-target").forEach((n) => n.classList.remove("tour-target"));
    const st = steps[i];
    st.target.classList.add("tour-target");
    st.target.scrollIntoView({ block: "center", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    const last = i === steps.length - 1;
    const next = el("button", { class: "btn small", type: "button", text: last ? "Done" : "Next", onclick: () => { if (last) done(); else { i += 1; show(); } } });
    fill(box, el("p", { class: "tiny faint", text: `${i + 1} of ${steps.length}` }), el("h3", { id: "tourTitle", text: st.title }), el("p", { class: "small", text: st.text }),
      el("div", { class: "row" }, next, el("button", { class: "btn quiet small", type: "button", text: "Skip the tour", onclick: done })));
    next.focus({ preventScroll: true });
  }
  document.addEventListener("keydown", onKey);
  window.addEventListener("hashchange", done, { once: true }); // leaving the page ends the tour
  document.body.append(box);
  show();
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
