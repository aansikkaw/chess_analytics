// Game report card: one game, both players, every move checked by Stockfish, then written up by the coach.
// Route: #/report/<accountId>/<gameId>. Grades and accuracy per player, the evaluation graph, move classes,
// key moments on the board, phase grades, the coach's review, and an annotated PGN to download.
// Guided view (default): winning chances, the one best line, tips and a first-visit tour. Pro view: evaluations in
// pawns, engine depth, three engine lines, coordinates, FEN/PGN copy. Same page, same data (DESIGN.md).

import { api, el, evalText, fill, post, proView, store, svg, toast, track, winPct } from "../core.js";
import { Board, applyMove, boardSettings, coordsPref, miniBoard, uciArrow } from "../board.js";
import { pageKeys } from "../keys.js";
import { Chess, validateFen } from "../../vendor/chess.js";
import { followJob } from "./common.js";

// Move classes: the standard symbols with our own words (DESIGN.md: never another site's move tiers or colours).
// Tone: "err" is the coach's red pen, "good" is ink, "plain" carries no mark.
const CLASS = {
  brilliant: ["!!", "Sound sacrifice", "good"], great: ["!", "Only good move", "good"], best: ["", "Engine's choice", "plain"],
  excellent: ["", "Good", "plain"], good: ["", "Good", "plain"], book: ["", "Book move", "plain"], forced: ["□", "Only move", "plain"],
  inaccuracy: ["?!", "Inaccuracy", "err"], mistake: ["?", "Mistake", "err"], miss: ["?", "Didn't punish", "err"],
  blunder: ["??", "Blunder", "err"],
};
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
      const [sym, label, tone] = CLASS[m.class];
      const b = el("button", { type: "button", class: `mv c-${m.class}`, "aria-label": `${num(m)}${m.san}, ${label}`, onclick: () => go(m.ply + 1) },
        m.san, sym ? el("b", { class: `sym ${tone}`, text: sym }) : null, pro ? el("span", { class: "mv-eval", text: evalText(m.eval_white, null, 1) }) : null);
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
    const [sym, label, tone] = CLASS[m.class];
    const head = el("p", {}, el("b", { class: "mono", text: `${num(m)}${m.san}` }), sym ? el("b", { class: `mono sym ${tone}`, text: sym }) : null, " ",
      el("span", { class: `cls-word ${tone}`, text: label }));
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

  const graph = evalGraph(rep, { you, pro, onPick: (n) => go(n) });

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

  // ---- hero: the result, both players' accuracy and the verdict, in one calm row ----
  const resultWord = (() => {
    const winner = res === "1-0" ? "white" : res === "0-1" ? "black" : res.startsWith("1/2") ? "draw" : null;
    if (!winner) return "Unfinished";
    if (winner === "draw") return "Draw";
    return you ? (winner === you ? "You won" : "You lost") : `${cap(winner)} won`;
  })();
  const playerCell = (c) => {
    const sd = rep.sides[c];
    const grade = sd.grade || "–";
    return el("div", { class: `rc-cell${you === c ? " you" : ""}` },
      el("dt", {}, el("span", { class: "rc-name", text: rep[c] || cap(c) }), el("span", { class: "rc-side", text: you === c ? `${cap(c)} · you` : cap(c) })),
      el("dd", { class: "rc-figure" }, el("span", { class: "figure", text: sd.accuracy != null ? `${sd.accuracy}%` : "–" }),
        pro ? el("span", { class: "rc-grade-plain", text: grade }) : el("span", { class: `grade small g-${sd.grade || "none"}`, "aria-label": `grade ${grade}`, text: grade })),
      el("dd", { class: "rc-sub", text: pro ? (sd.acpl != null ? `${sd.acpl} cp lost a move · ${sd.moves} moves` : `${sd.moves} moves`) : "accuracy" }));
  };
  const op = rep.opening || {};
  const lb = op.left_book;
  const hero = el("section", { class: "sheet rc-hero", "aria-label": "Result" },
    el("div", { class: "rc-verdict" },
      el("h2", { text: (d.ai && d.ai.headline) || rep.verdict }),
      op.name ? el("p", { class: "small muted" }, el("b", { class: pro ? "mono" : "", text: `${op.eco} ${op.name}` }),
        lb != null ? ` · book until move ${Math.floor(lb / 2) + 1}` : "") : null),
    el("dl", { class: "rc-scores" },
      el("div", { class: "rc-cell rc-result" }, el("dt", { text: "Result" }), el("dd", { class: "rc-figure" }, el("span", { class: "figure", text: res.replace("-", "–") })),
        el("dd", { class: "rc-sub", text: resultWord })),
      playerCell("white"), playerCell("black")),
    el("p", { class: "rc-foot tiny faint" },
      pro ? `Stockfish depth ${rep.depth}, both players, top two moves in every position. Accuracy uses Lichess's formula. `
        : "Every move from both players, checked by Stockfish. A is 90% accuracy or better. ",
      el("a", { href: `${url}.pgn`, download: "", text: "Download annotated PGN" })));

  // ---- key moments: cards with a mini-board, synced with the board ----
  const boardRow = el("div", { class: "board-row" }, evalBar, boardHost);
  const jumpTo = (ply) => {
    go(ply + 1);
    if (matchMedia("(max-width: 900px)").matches) {
      boardRow.scrollIntoView({ block: "start", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    }
  };
  const momentCard = (k) => {
    const [sym, label, tone] = CLASS[k.class];
    const who = you ? (k.color === you ? "you" : "opponent") : k.color;
    const before = k.ply > 0 && moves[k.ply - 1] ? moves[k.ply - 1].eval_white : null;
    const after = moves[k.ply] ? moves[k.ply].eval_white : null;
    return el("li", {}, el("button", { type: "button", class: `km-card${k.color === you ? " mine" : ""}`, onclick: () => jumpTo(k.ply),
      "aria-label": `${k.label}${sym}, ${label}, ${who}. Jump to this move.` },
    miniBoard(k.fen, { orientation: you || "white", arrows: [uciArrow(k.uci, "you"), uciArrow(k.best, "coach")].filter(Boolean),
      label: `${k.label}: played ${k.san}, better ${k.best_san || "–"}` }),
    el("span", { class: "km-body" },
      el("span", { class: "km-head" }, el("b", { class: "mono", text: k.label }), el("b", { class: `mono sym ${tone}`, text: sym }),
        el("span", { class: "km-who", text: `${label} · ${who}` })),
      pro
        ? el("span", { class: "km-text" }, el("span", { class: "mono km-swing", text: `${evalText(before)} → ${evalText(after)}` }),
          k.best_san ? el("span", { class: "mono km-line", text: `Best ${numberedFrom(k.fen, k.line && k.line.length ? k.line : [k.best_san])}` }) : null)
        : el("span", { class: "km-text" }, el("span", { text: k.explain }),
          Math.round(k.loss) >= 1 ? el("span", { class: "km-cost", text: `−${Math.round(k.loss)}% winning chances` })
            : el("span", { class: "km-cost", text: "The game was already decided here." })))));
  };
  const km = el("section", { class: "rc-moments", "aria-labelledby": "kmTitle" }, el("h2", { id: "kmTitle", text: "Key moments" }),
    rep.key_moments.length ? el("ol", { class: "km-list" }, ...rep.key_moments.map(momentCard))
      : el("p", { class: "small muted", text: "No big swings: a clean game from both sides." }));

  // ---- phase grades: one compact table ----
  const phaseCell = (c, p) => {
    const ph = rep.sides[c].phases[p];
    if (!ph.moves) return el("td", { class: "faint", text: "–" });
    return el("td", {}, pro
      ? el("span", { class: "mono", text: `${ph.accuracy ?? "–"}%` })
      : el("span", { class: `grade small g-${ph.grade || "none"}`, text: ph.grade || "–" }),
    pro ? el("span", { class: "rc-grade-plain", text: ph.grade || "–" }) : el("span", { class: "muted", text: ` ${ph.accuracy ?? "–"}%` }),
    el("span", { class: "tiny faint", text: ` ${ph.moves} moves` }));
  };
  const errs = (c) => { const n = rep.sides[c].counts; return [n.inaccuracy || 0, (n.mistake || 0) + (n.miss || 0), n.blunder || 0]; };
  const errRow = (c) => el("td", {}, ...errs(c).flatMap((v, i) => [i ? el("span", { class: "faint", text: " · " }) : null,
    el("span", { class: v ? "rc-err" : "faint", text: String(v) })]));
  const phases = el("section", { class: "sheet rc-phases", "aria-labelledby": "phTitle" },
    el("h2", { id: "phTitle", text: "Grades by phase" }),
    el("table", { class: "rc-table" },
      el("thead", {}, el("tr", {}, el("th", { scope: "col", text: "Phase" }), el("th", { scope: "col", text: rep.white || "White" }),
        el("th", { scope: "col", text: rep.black || "Black" }))),
      el("tbody", {}, ...["opening", "middlegame", "endgame"].map((p) => el("tr", {}, el("th", { scope: "row", text: cap(p) }), phaseCell("white", p), phaseCell("black", p))),
        el("tr", { class: "rc-errors" }, el("th", { scope: "row", text: pro ? "?! · ? · ??" : "Inaccuracies · mistakes · blunders" }), errRow("white"), errRow("black")))),
    el("p", { class: "tiny faint", text: pro ? "Accuracy and grade per phase, both players. Forced moves are left out."
      : "A 90% or better, B 80%, C 70%, D 60%, F below. Fewer than three moves in a phase: no grade." }));

  // ---- the coach's review, as prose ----
  const coach = coachCard(d, url, () => load(body, accountId, gameId, head), jumpTo);

  fill(body, hero,
    el("section", { class: "sheet rc-graph", "aria-labelledby": "egTitle" },
      el("div", { class: "spread" }, el("h2", { id: "egTitle", text: pro ? "Evaluation" : "Who was winning" }),
        el("span", { class: "tiny faint", text: pro ? "White's side, ±1 and ±3 pawns marked. Drag along it, or use ← →." : "Drag along it, or use ← →, and the board follows." })),
      graph.node),
    el("div", { class: "report-main" },
      el("div", { class: "report-left" }, boardRow,
        el("div", { class: "board-foot" }, nav, backBtn), el("div", { class: "board-foot" }, engineBtn, bestBtn, posMenu, boardSettings(board)),
        copyRow, note, pro ? linesBox : null, el("div", { class: "report-movebox" }, list)),
      el("div", { class: "report-right" }, km, coach, phases)));
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
  const phase = (k) => (ai[k] ? el("p", {}, el("b", { text: `${cap(k)}. ` }), ai[k]) : null);
  return el("section", { class: "sheet rc-review", "aria-labelledby": "reviewTitle" },
    el("div", { class: "spread" }, el("h2", { id: "reviewTitle", text: "Your coach's review" }),
      el("span", { class: "tiny faint", text: ai.source === "ai" ? "AI coach" : "Built-in coach" })),
    el("article", { class: "prose" },
      ai.summary ? el("p", { class: "prose-lead", text: ai.summary }) : null,
      phase("opening"), phase("middlegame"), phase("endgame"),
      ai.strengths && ai.strengths.length ? el("h3", { text: "What went well" }) : null,
      ai.strengths && ai.strengths.length ? el("ul", { class: "prose-good" }, ...ai.strengths.map((t) => el("li", { text: t }))) : null,
      ai.lessons && ai.lessons.length ? el("h3", { text: "Take into your next game" }) : null,
      ai.lessons && ai.lessons.length ? el("ol", { class: "prose-lessons" }, ...ai.lessons.map((l) => el("li", {},
        el("b", { text: l.title.replace(/\.$/, "") + ". " }), l.detail, " ",
        l.ply != null ? el("button", { class: "link", type: "button", text: "Show me", onclick: () => jump(l.ply) }) : null))) : null,
      ai.training && ai.training.length ? el("h3", { text: "Train next" }) : null,
      ai.training && ai.training.length ? el("ul", { class: "prose-train" }, ...ai.training.map((t) => el("li", {}, FEATURE_LINK[t.feature]
        ? el("a", { href: FEATURE_LINK[t.feature], text: t.task }) : el("b", { text: t.task }), t.why ? ` ${t.why.replace(/^./, (c) => c.toLowerCase())}` : ""))) : null),
    el("p", { class: "tiny faint", text: `${src}. Every move it mentions was checked against the engine's analysis${ai.removed && ai.removed.length ? "; sentences it couldn't check were removed" : ""}.` }),
    askBtn ? el("div", { class: "row" }, askBtn, el("span", { class: "tiny faint", text: `${d.ai_left} AI write-up${d.ai_left === 1 ? "" : "s"} left today` })) : null,
    out);
}

/** The evaluation graph, as a slider you can scrub: drag along it (or use the arrow keys) and the board follows.
 *  Guided: your winning chances, labelled in words. Pro: White's side, with ±1 and ±3 pawns marked. */
function evalGraph(rep, { you, pro, onPick }) {
  const s = rep.series;
  const n = Math.max(1, s.length - 1);
  const persp = pro ? "white" : you || "white";
  const val = (i) => (persp === "white" ? s[i] : 100 - s[i]);
  const W = 800, H = 160;
  const x = (i) => (i / n) * W, y = (v) => H - (v / 100) * H;
  const pts = s.map((_, i) => `${x(i).toFixed(1)},${y(val(i)).toFixed(1)}`).join(" ");
  const uid = Math.random().toString(36).slice(2, 7);
  const area = (clip, cls) => svg("polygon", { points: `0,${H / 2} ${pts} ${W},${H / 2}`, class: cls, "clip-path": `url(#${clip}-${uid})` });
  const guides = pro ? [75.1, 59.1, 40.9, 24.9] : [];
  const plot = svg("svg", { class: "eg-svg", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", "aria-hidden": "true" },
    svg("defs", {}, svg("clipPath", { id: `up-${uid}` }, svg("rect", { x: 0, y: 0, width: W, height: H / 2 })),
      svg("clipPath", { id: `dn-${uid}` }, svg("rect", { x: 0, y: H / 2, width: W, height: H / 2 }))),
    ...guides.map((g) => svg("line", { x1: 0, x2: W, y1: y(g), y2: y(g), class: "eg-guide" })),
    area("up", "eg-up"), area("dn", "eg-dn"),
    svg("line", { x1: 0, x2: W, y1: H / 2, y2: H / 2, class: "eg-mid" }),
    svg("polyline", { points: pts, class: "eg-line" }));
  const pct = (i) => `${(i / n) * 100}%`;
  const labels = pro
    ? [["+3", 75.1], ["+1", 59.1], ["−1", 40.9], ["−3", 24.9]]
    : [[you ? "You're winning" : `${cap(persp)} winning`, 92], ["Even", 50], [you ? "They're winning" : `${persp === "white" ? "Black" : "White"} winning`, 8]];
  const dots = rep.moves.filter((m) => BAD.includes(m.class)).map((m) => el("span", {
    class: `eg-dot ${m.color === (you || "white") ? "mine" : "theirs"} ${m.class}`, "aria-hidden": "true",
    style: { left: pct(m.ply + 1), top: `${100 - val(m.ply + 1)}%` } }));
  const cursor = el("span", { class: "eg-cursor", "aria-hidden": "true" });
  const tip = el("span", { class: "eg-tip", "aria-hidden": "true" });
  const node = el("div", { class: "eg-wrap", role: "slider", tabindex: "0", "aria-label": pro ? "Evaluation graph" : "Who was winning, move by move",
    "aria-valuemin": "0", "aria-valuemax": String(n), "aria-valuenow": "0" },
  plot, ...labels.map(([t, v]) => el("span", { class: "eg-label", style: { top: `${100 - v}%` }, text: t })), ...dots, cursor, tip);
  const describe = (i) => {
    const m = i > 0 ? rep.moves[i - 1] : null;
    const where = m ? `${num(m)}${m.san}${CLASS[m.class][0]}` : "Start";
    const v = val(i);
    return pro ? `${where} · ${evalText(m ? m.eval_white : null, null, 2).replace("–", `${Math.round(s[i])}%`)}`
      : `${where} · ${you ? "you" : persp} ${Math.round(v)}%`;
  };
  let pressed = false, raf = 0, last = -1;
  const at = (e) => { const r = node.getBoundingClientRect(); return Math.max(0, Math.min(n, Math.round(((e.clientX - r.left) / r.width) * n))); };
  const pick = (e) => {
    const i = at(e);
    if (i === last) return;
    last = i;
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => onPick(i));
  };
  node.addEventListener("pointerdown", (e) => { if (e.button !== 0) return; pressed = true; last = -1; node.setPointerCapture(e.pointerId); node.classList.add("scrubbing"); pick(e); });
  node.addEventListener("pointermove", (e) => { if (pressed) pick(e); });
  const release = () => { pressed = false; node.classList.remove("scrubbing"); };
  node.addEventListener("pointerup", release);
  node.addEventListener("pointercancel", release);
  node.addEventListener("keydown", (e) => {
    if (e.key === "Home") { onPick(0); e.preventDefault(); e.stopPropagation(); }
    if (e.key === "End") { onPick(n); e.preventDefault(); e.stopPropagation(); }
  });
  return { node, mark: (i) => {
    cursor.style.left = pct(i);
    tip.textContent = describe(i);
    tip.style.left = `${Math.min(88, Math.max(12, (i / n) * 100))}%`;
    node.setAttribute("aria-valuenow", String(i));
    node.setAttribute("aria-valuetext", describe(i));
  } };
}
