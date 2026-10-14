// A puzzle session on the board: the opponent's move, your answer (graded by the server), the rest of the
// line, hints, the solution, retry and explore. Used for your own mistakes and for Twin-predicted puzzles.

import { el, fill, post, track } from "../core.js";
import { Board, applyMove, boardSettings, playSound, sideToMove, uciArrow } from "../board.js";
import { setDueCount } from "../shell.js";

const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const whenText = (days) => (days == null ? "" : days >= 1 ? `${Math.round(days)} day${Math.round(days) === 1 ? "" : "s"}` : "10 minutes");
const THEME_LABEL = { mateIn1: "Mate in 1", mateIn2: "Mate in 2", mateIn3: "Mate in 3", mateIn4: "Mate in 4", mateIn5: "Mate in 5+",
  backRankMate: "Back-rank mate", hangingPiece: "Hanging piece", discoveredAttack: "Discovered attack", quietMove: "Quiet move",
  doubleCheck: "Double check", smotheredMate: "Smothered mate", capturingDefender: "Capture the defender", trappedPiece: "Trapped piece",
  advancedPawn: "Advanced pawn", defensiveMove: "Defensive move", xRayAttack: "X-ray attack", intermezzo: "In-between move",
  kingsideAttack: "Kingside attack", queensideAttack: "Queenside attack", exposedKing: "Exposed king", rookEndgame: "Rook endgame",
  pawnEndgame: "Pawn endgame", queenEndgame: "Queen endgame", bishopEndgame: "Bishop endgame", knightEndgame: "Knight endgame",
  underPromotion: "Underpromotion", attackingF2F7: "Attacking f2/f7", enPassant: "En passant" };
const HIDDEN_THEMES = new Set(["oneMove", "short", "long", "veryLong", "advantage", "crushing", "master", "masterVsMaster", "superGM", "mate"]);
export const themeLabel = (t) => THEME_LABEL[t] || cap(t.replace(/_/g, " ").replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase());

/** "+1.4", "-0.3", "M3" from White's point of view. */
function evalText(r) {
  if (!r || r.over) return r && r.over ? r.result : "";
  if (r.mate != null) return `${r.mate > 0 ? "" : "-"}M${Math.abs(r.mate)}`;
  const v = r.cp / 100;
  return `${v > 0 ? "+" : ""}${v.toFixed(1)}`;
}
const whiteShare = (r) => {
  if (!r || r.over) return r && r.result === "1-0" ? 100 : r && r.result === "0-1" ? 0 : 50;
  if (r.mate != null) return r.mate > 0 ? 100 : 0;
  return 50 + 50 * (2 / (1 + Math.exp(-0.00368208 * Math.max(-1000, Math.min(1000, r.cp)))) - 1);
};

/**
 * @param {HTMLElement} body  where the session renders
 * @param {object[]} queue  puzzles (answers not included)
 * @param {object} o  kind: "mine" | "predicted"; empty(session): the element shown when the queue runs out;
 *   onDone(puzzle, result): called after each puzzle (e.g. to refresh a status card)
 */
export function puzzleSession(body, queue, o = {}) {
  const predicted = o.kind === "predicted";
  const session = { tried: 0, clean: 0, streak: 0, best: 0, beat: 0, missed: 0 };
  const showCount = (n) => { if (!predicted) setDueCount(n); };

  // ---- layout ----
  const boardHost = el("div", { class: "pz-board" });
  const evalFill = el("i"), evalNum = el("span");
  const evalBar = el("div", { class: "evalbar", hidden: true, "aria-hidden": "true" }, evalFill, evalNum);
  const statusIcon = el("span", { class: "pz-icon", "aria-hidden": "true" });
  const statusTitle = el("strong");
  const statusText = el("span");
  const status = el("div", { class: "pz-status", role: "status", "aria-live": "polite" }, statusIcon, el("div", {}, statusTitle, statusText));
  const context = el("p", { class: "small muted" });
  const why = el("ul", { class: "pz-why small", hidden: true });
  const after = el("div", { class: "pz-after", hidden: true });
  const themes = el("div", { class: "chips" });
  const stage = el("div", { class: "pz-stage", title: "Review stage" });
  const moveList = el("ol", { class: "pz-moves", "aria-label": "Moves played" });
  const actions = el("div", { class: "pz-actions" });
  const explorePanel = el("div", { class: "pz-explore", hidden: true });
  const stats = el("p", { class: "tiny faint" });
  const left = el("span", { class: "small muted" });
  const typed = el("input", { type: "text", id: `typedMove-${o.kind || "mine"}`, placeholder: "e.g. Nf3", autocomplete: "off", autocapitalize: "off",
    spellcheck: "false", maxlength: "10" });
  const typedErr = el("p", { class: "err small", role: "alert", hidden: true });
  const typedForm = el("form", { class: "move-input" }, el("label", { class: "sr", for: typed.id, text: "Type your move" }), typed,
    el("button", { class: "btn quiet small", type: "submit", text: "Play" }));

  let cur = null, mode = "idle", line = [], step = 0, firstAttempt = true, hinted = false, hintLevel = 0;
  let result = null, lastOpp = null, plies = [];
  const board = new Board(boardHost, { interactive: true, locked: true, label: "Puzzle board", onMove: (_f, _t, info) => onUserMove(info) });

  // ---- small helpers ----
  const setStatus = (kind, title, text = "") => {
    status.className = `pz-status ${kind}`;
    statusIcon.textContent = { turn: "", correct: "✓", wrong: "✗", done: "★", solution: "▶", explore: "⌕" }[kind] || "";
    statusIcon.dataset.side = kind === "turn" ? sideToMove(board.fen) : "";
    statusTitle.textContent = title;
    statusText.textContent = text;
  };
  const turnStatus = (text) => {
    const side = sideToMove(board.fen);
    setStatus("turn", `${cap(side)} to move`, text || (cur.kind === "prep" ? "What does your preparation say here?" : `Find the best move for ${side}.`));
  };
  const addPly = (san, fenBefore) => { plies.push({ san, fen: fenBefore }); renderMoves(); };
  const dropPly = () => { plies.pop(); renderMoves(); };
  function renderMoves() {
    const items = plies.map((p, i) => {
      const [, side, , , , full] = p.fen.split(" ");
      const num = side === "w" ? `${full}.` : i === 0 ? `${full}…` : "";
      return el("li", {}, num ? el("span", { class: "n", text: num }) : null, el("span", { text: p.san }));
    });
    moveList.replaceChildren(...items);
    moveList.hidden = !plies.length;
  }
  const lock = (on) => board.set({ locked: on });
  const renderStats = () => {
    const base = session.tried ? `This session: ${session.clean} of ${session.tried} solved first time${session.streak > 1 ? `, streak ${session.streak}` : ""}.` : "";
    stats.textContent = predicted && session.tried ? `${base} You beat ${session.beat} prediction${session.beat === 1 ? "" : "s"}.` : base;
  };
  const btn = (text, onclick, cls = "btn quiet") => el("button", { class: cls, type: "button", text, onclick });
  const showThemes = (p) => themes.replaceChildren(...p.themes.filter((t) => !HIDDEN_THEMES.has(t)).map((t) => el("span", { class: "tag", text: themeLabel(t) })));

  function renderActions() {
    const k = [];
    if (mode === "solve" || mode === "retry" || mode === "cont") {
      k.push(btn(hintLevel === 0 ? "Hint" : hintLevel === 1 ? "Show the move" : "Hint", hint, "btn quiet small"));
      k.push(btn("Show solution", showSolution, "btn quiet small"));
      if (mode === "solve" && firstAttempt) k.push(btn("Skip for now", () => { queue.push(cur); next(); }, "link quiet small"));
    } else if (mode === "done") {
      const n = btn(queue.length ? "Next puzzle" : "Finish", next, "btn");
      k.push(n, btn("Retry", retry, "btn quiet"), btn("Explore", enterExplore, "btn quiet"));
      setTimeout(() => n.focus(), 0);
    } else if (mode === "explore") {
      k.push(btn(queue.length ? "Next puzzle" : "Finish", next, "btn"), btn("Back to the puzzle", retry, "btn quiet"));
    }
    actions.replaceChildren(...k);
    typedForm.hidden = !["solve", "retry", "cont", "explore"].includes(mode);
  }

  // ---- a new puzzle ----
  async function next() {
    cur = queue.shift() || null;
    explorePanel.hidden = true; evalBar.hidden = true; after.hidden = true; why.hidden = true;
    if (!cur) { showCount(0); body.replaceChildren(o.empty ? o.empty(session) : el("p", { class: "muted", text: "All done." })); return; }
    const p = cur;
    mode = "intro"; line = []; step = 0; firstAttempt = true; hinted = false; hintLevel = 0; result = null; plies = [];
    renderMoves();
    left.textContent = queue.length ? `${queue.length} more after this one` : "Last one for now";
    const side = sideToMove(p.fen);
    if (predicted) {
      const pr = p.predicted || {};
      context.replaceChildren(el("b", { text: `Predicted for you · rated ${pr.rating}` }),
        pr.model === "basic" ? el("span", { class: "tag", text: "Basic model" }) : el("span", { class: "tag pro", text: "Your Twin" }));
      why.replaceChildren(...(pr.why_before || []).map((t) => el("li", { text: t })));
      why.hidden = !(pr.why_before || []).length;
      themes.replaceChildren(); // the theme would give it away: shown after you answer
    } else {
      const prep = p.kind === "prep";
      const vs = p.opponent ? ` vs ${p.opponent}` : "";
      context.textContent = prep
        ? (p.played_san ? `Repertoire drill. In a game you played ${p.played_san} here instead of your prep.` : "Repertoire drill from your ChessBase preparation.")
        : `From your${p.time_class ? ` ${p.time_class}` : ""} game${vs}${p.played_at ? `, ${p.played_at}` : ""}. You played ${p.played_san} here and lost ${Math.round(p.win_loss)}% of your winning chances.`;
      showThemes(p);
    }
    stage.replaceChildren(el("span", { class: "tiny faint", text: "Review stage" }),
      ...[1, 2, 3, 4, 5].map((b) => el("i", { class: b <= p.box ? "on" : "" })));
    typed.value = ""; typedErr.hidden = true;
    renderActions();
    if (p.prev_fen && p.prev_uci) { // play the opponent's move in, so you see what you're answering
      board.set({ fen: p.prev_fen, orientation: side, lastMove: null, arrows: [], marks: {}, badge: null, locked: true, animate: false });
      setStatus("turn", `${cap(side)} to move`, "Your opponent plays…");
      await wait(500);
      if (cur !== p) return;
      board.set({ fen: p.fen, lastMove: p.prev_uci, sound: true });
      await wait(250);
      if (cur !== p) return;
    } else {
      board.set({ fen: p.fen, orientation: side, lastMove: null, arrows: [], marks: {}, badge: null, animate: false });
    }
    lastOpp = p.prev_uci || null;
    mode = "solve";
    lock(false);
    turnStatus();
    renderActions();
  }

  // ---- the player's move ----
  async function onUserMove(info) {
    typedErr.hidden = true;
    if (mode === "explore") { addPly(info.san, info.before); analyse(); return; }
    if (mode === "cont") { await continuation(info); return; }
    if (mode !== "solve" && mode !== "retry") return;
    const p = cur;
    lock(true);
    addPly(info.san, info.before);
    let r;
    try {
      r = await post(`/api/puzzles/${p.id}/attempt`, { move: info.uci, practice: !firstAttempt, hinted });
    } catch (e) {
      setStatus("wrong", "Couldn't check that move", e.message);
      board.set({ fen: p.fen, lastMove: lastOpp, badge: null });
      dropPly();
      lock(false);
      return;
    }
    if (cur !== p) return;
    const wasFirst = firstAttempt;
    firstAttempt = false;
    if (!r.practice) result = r;
    line = r.line && r.line.length ? r.line : [r.solution_san];
    if (wasFirst) {
      session.tried++;
      track("Puzzle attempt", { verdict: r.verdict, kind: o.kind || "mine" });
      if (r.prediction) { if (r.prediction.missed) session.missed++; else session.beat++; }
    }
    if (r.verdict === "correct" || r.verdict === "also_good") {
      board.set({ badge: { sq: info.to, kind: "correct" } });
      playSound("correct");
      if (wasFirst && !hinted) { session.clean++; session.streak++; session.best = Math.max(session.best, session.streak); }
      else if (wasFirst) session.streak = 0;
      renderStats();
      const found = info.uci.slice(0, 4) === r.solution.slice(0, 4);
      if (found && line.length >= 3) { // keep going along the line
        setStatus("correct", r.verdict === "correct" ? "Best move!" : "Good move", "Now the reply…");
        mode = "cont"; step = 1;
        renderActions();
        await wait(700);
        if (cur !== p) return;
        await opponentAndContinue(p);
      } else {
        finish(true, r.verdict === "also_good" ? `${r.note} The engine's first choice was ${r.solution_san}.` : r.note || "");
      }
      return;
    }
    board.set({ badge: { sq: info.to, kind: "wrong" } });
    playSound("wrong");
    if (wasFirst) { session.streak = 0; renderStats(); }
    setStatus("wrong", `${r.your_move} isn't it`, r.kind === "prep" && r.note ? r.note : wasFirst ? "Try again, or see the solution. It comes back in 10 minutes." : "Try again, or see the solution.");
    mode = "retry";
    renderActions();
    await wait(850);
    if (cur !== p || mode !== "retry") return;
    board.set({ fen: p.fen, lastMove: lastOpp, badge: null });
    dropPly();
    lock(false);
  }

  async function opponentAndContinue(p) {
    const r = applyMove(board.fen, line[step]);
    if (!r) { finish(true); return; }
    addPly(r.san, board.fen);
    board.set({ fen: r.fen, lastMove: r.uci, badge: null, sound: true });
    lastOpp = r.uci;
    step++;
    if (step >= line.length) { finish(true); return; }
    await wait(200);
    if (cur !== p) return;
    turnStatus("Keep going: find the next move.");
    lock(false);
  }

  async function continuation(info) {
    const p = cur;
    lock(true);
    addPly(info.san, info.before);
    const want = applyMove(info.before, line[step]);
    if ((want && want.uci === info.uci) || info.mate) {
      board.set({ badge: { sq: info.to, kind: "correct" } });
      playSound("correct");
      step++;
      if (step >= line.length || info.mate) { finish(true); return; }
      setStatus("correct", "Correct", "Now the reply…");
      await wait(650);
      if (cur !== p) return;
      await opponentAndContinue(p);
      return;
    }
    board.set({ badge: { sq: info.to, kind: "wrong" } });
    playSound("wrong");
    setStatus("wrong", `${info.san} isn't the move`, "Try again from here, or see the rest of the line.");
    await wait(850);
    if (cur !== p || mode !== "cont") return;
    board.set({ fen: info.before, lastMove: lastOpp, badge: null });
    dropPly();
    lock(false);
  }

  function finish(solved, note = "") {
    mode = "done";
    lock(true);
    const clean = solved && result && !result.practice && !hinted && result.verdict !== "wrong";
    const when = result && !result.practice ? whenText(result.next_review_in_days) : "";
    setStatus("done", clean ? "Solved" : solved ? "Puzzle complete" : "Solution",
      [note, when ? `It comes back in ${when}.` : ""].filter(Boolean).join(" "));
    if (clean) playSound("complete");
    if (predicted) showPrediction();
    showCount(queue.length);
    renderActions();
    if (o.onDone) o.onDone(cur, result);
  }

  // After a predicted puzzle: did the prediction come true, and why we expected it.
  function showPrediction() {
    const r = result || {};
    showThemes(cur);
    why.hidden = true;
    const pred = r.prediction;
    const pct = r.predicted_miss != null ? r.predicted_miss : Math.round(100 * ((cur.predicted || {}).p_miss || 0));
    const head = pred
      ? (pred.missed ? el("p", { class: "pz-verdict miss" }, el("b", { text: `Predicted: ${pct}% you'd miss it. You did.` }),
        " That's what it's for: now it's in your deck, and it comes back until it sticks.")
        : el("p", { class: "pz-verdict beat" }, el("b", { text: `Predicted: ${pct}% you'd miss it. You beat the prediction!` })))
      : el("p", { class: "pz-verdict" }, el("b", { text: `Predicted miss: ${pct}%.` }));
    const reasons = (r.why || []).map((t) => el("li", { text: t }));
    const src = r.bank_id && !String(r.bank_id).startsWith("pb")
      ? el("p", { class: "tiny faint" }, "Puzzle from the ", el("a", { href: r.game_url || "https://database.lichess.org/#puzzles", target: "_blank", rel: "noopener", text: "Lichess puzzle database" }), " (CC0).")
      : null;
    fill(after, head, reasons.length ? el("ul", { class: "pz-why small" }, ...reasons) : null,
      pred ? el("p", { class: "tiny faint", text: `Your puzzle rating is now ${pred.rating}.` }) : null, src);
    after.hidden = false;
  }

  function retry() {
    const p = cur;
    if (!p) return;
    mode = "retry"; step = 0; plies = []; hintLevel = 0;
    renderMoves();
    explorePanel.hidden = true; evalBar.hidden = true;
    board.set({ fen: p.fen, lastMove: p.prev_uci || null, badge: null, arrows: [], marks: {}, movable: null });
    lastOpp = p.prev_uci || null;
    lock(false);
    turnStatus("Practice run: this doesn't change when it comes back.");
    renderActions();
  }

  async function hint() {
    const p = cur;
    try {
      hintLevel = Math.min(2, hintLevel + 1);
      if (mode === "cont") { // the line is known: hint from it
        const want = applyMove(board.fen, line[step]);
        if (want) board.set({ marks: { [want.from]: "hint" }, arrows: hintLevel >= 2 ? [{ from: want.from, to: want.to, kind: "hint" }] : [] });
      } else {
        const h = await post(`/api/puzzles/${p.id}/hint`, { level: hintLevel });
        if (cur !== p) return;
        hinted = true;
        board.set({ marks: { [h.from]: "hint" }, arrows: h.to ? [{ from: h.from, to: h.to, kind: "hint" }] : [] });
      }
      setStatus("turn", hintLevel >= 2 ? "Here's the move" : "Hint: move this piece", firstAttempt && mode === "solve" ? "With a hint, it comes back sooner." : "");
      renderActions();
    } catch (e) { setStatus("wrong", "No hint right now", e.message); }
  }

  async function showSolution() {
    const p = cur;
    lock(true);
    mode = "solution";
    renderActions();
    if (!line.length) { // nothing attempted yet: fetch the answer (this counts as a miss)
      let r;
      try { r = await post(`/api/puzzles/${p.id}/reveal`, { practice: !firstAttempt }); }
      catch (e) { setStatus("wrong", "Couldn't load the solution", e.message); mode = "solve"; lock(false); renderActions(); return; }
      if (!r.practice) result = r;
      if (firstAttempt) { session.tried++; session.streak = 0; if (r.prediction) session.missed++; renderStats(); }
      firstAttempt = false;
      line = r.line && r.line.length ? r.line : [r.solution_san];
    }
    if (cur !== p) return;
    setStatus("solution", "Solution", line.join(" "));
    plies = [];
    renderMoves();
    board.set({ fen: p.fen, lastMove: p.prev_uci || null, badge: null, arrows: [], marks: {} });
    await wait(450);
    for (let i = 0; i < line.length; i++) {
      if (cur !== p || mode !== "solution") return;
      const m = applyMove(board.fen, line[i]);
      if (!m) break;
      addPly(m.san, board.fen);
      board.set({ fen: m.fen, lastMove: m.uci, sound: true, arrows: i === 0 ? [uciArrow(m.uci, "coach")] : [] });
      await wait(800);
    }
    if (cur === p && mode === "solution") finish(false, `The move was ${line[0]}.`);
  }

  // ---- explore freely, with the engine ----
  let analyseTimer = null, analyseSeq = 0;
  function enterExplore() {
    mode = "explore";
    evalBar.hidden = false;
    explorePanel.hidden = false;
    board.set({ badge: null, arrows: [], marks: {} });
    lock(false);
    setStatus("explore", "Explore", "Move either side freely. Nothing here is graded.");
    renderActions();
    analyse();
  }
  function analyse() {
    clearTimeout(analyseTimer);
    const seq = ++analyseSeq;
    const fen = board.fen;
    fill(explorePanel, el("p", { class: "small muted", text: "Engine thinking…" }));
    analyseTimer = setTimeout(async () => {
      let r;
      try { r = await post("/api/engine/analyse", { fen }); }
      catch (e) { if (seq === analyseSeq) fill(explorePanel, el("p", { class: "small err", text: e.message })); return; }
      if (seq !== analyseSeq || mode !== "explore") return;
      const w = whiteShare(r);
      const flip = board.o.orientation === "black";
      evalFill.style.height = `${flip ? 100 - w : w}%`;
      evalBar.classList.toggle("flip", flip);
      evalNum.textContent = evalText(r);
      evalNum.className = w >= 50 ? "w" : "b";
      fill(explorePanel,
        el("p", { class: "small" }, el("b", { text: `Engine: ${evalText(r) || "game over"}` }), r.line && r.line.length ? ` · ${r.line.join(" ")}` : ""),
        r.best ? el("div", { class: "row" },
          btn("Show best move", () => board.set({ arrows: [uciArrow(r.best, "coach")] }), "btn quiet small"),
          btn("Play it", () => { const m = applyMove(fen, r.best); if (m) { addPly(m.san, fen); board.set({ fen: m.fen, lastMove: m.uci, sound: true }); analyse(); } }, "btn quiet small"),
          btn("Undo", () => { const last = plies.pop(); renderMoves(); if (last) { board.set({ fen: last.fen, lastMove: null }); analyse(); } }, "link quiet small"))
          : null);
    }, 250);
  }

  // ---- typed moves (keyboard, screen readers) ----
  typedForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = typed.value.trim();
    if (!text || board.o.locked) return;
    const before = board.fen;
    const r = applyMove(before, text);
    if (!r) { typedErr.textContent = `${text} isn't a legal move here. Type it like Nf3, exd5, O-O or e8=Q.`; typedErr.hidden = false; return; }
    typed.value = "";
    board.set({ fen: r.fen, lastMove: r.uci, badge: null, arrows: [], marks: {} });
    playSound(r.check ? "check" : r.captured ? "capture" : "move");
    onUserMove({ ...r, before });
  });

  body.replaceChildren(el("div", { class: "pz" },
    el("div", { class: "pz-left" }, el("div", { class: "pz-boardrow" }, evalBar, boardHost),
      el("div", { class: "board-foot" }, left, boardSettings(board))),
    el("aside", { class: "pz-panel sheet" }, status, context, why, themes, after, stage, moveList, actions, explorePanel,
      el("details", { class: "pz-type" }, el("summary", { class: "small muted", text: "Type a move instead" }), typedForm, typedErr),
      stats,
      el("p", { class: "tiny faint", text: "Drag or click a piece; dots show where it can go. Right-click to draw arrows." }))));
  renderStats();
  next();
  return session;
}
