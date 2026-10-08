// Train: your own mistakes as puzzles, played out on the board (the opponent's move, your answer, the
// engine's line), and the week's plan. The server grades the first move; the follow-up is practice.

import { S, account, api, el, fill, patch, post, store, track } from "../core.js";
import { Board, applyMove, boardSettings, playSound, sideToMove, uciArrow } from "../board.js";
import { areaTabs } from "./common.js";
import { setDueCount } from "../shell.js";

export function render(main, sub) {
  const head = el("div", { class: "page-head" }, el("div", {}, el("h1", { text: "Train" }),
    el("p", { class: "sub", text: "Your own mistakes come back until you stop making them: after 1, 3, 7, 14 and 30 days." })));
  const body = el("div");
  const hw = el("div");
  main.replaceChildren(head, hw, areaTabs("train", sub, [["", "Puzzles"], ["plan", "This week's plan"]]), body);
  if (S.me.coaches && S.me.coaches.length) homeworkCard(hw);
  if (sub === "plan") plan(body);
  else puzzles(body);
}

// Homework a coach has set (for students who joined a coach's squad).
async function homeworkCard(host) {
  let squads;
  try { squads = await api("/api/me/coaching"); } catch { return; }
  const items = squads.flatMap((sq) => sq.assignments.map((a) => ({ ...a, coach: sq.coach_email })));
  if (!items.length) return;
  const open = items.filter((a) => !a.done_at);
  const list = el("ul", { class: "homework" }, ...items.slice(0, 8).map((a) => {
    const cb = el("input", { type: "checkbox", id: `my-hw-${a.id}` });
    cb.checked = !!a.done_at;
    const li = el("li", { class: a.done_at ? "done" : "" }, cb,
      el("label", { for: cb.id }, el("span", { text: a.title }), a.detail ? el("span", { class: "tiny muted", text: a.detail }) : null),
      el("span", { class: "tiny faint", text: a.done_at ? "done" : a.due_at ? `due ${new Date(a.due_at * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" })}` : "" }));
    cb.addEventListener("change", async () => {
      try { await patch(`/api/assignments/${a.id}`, { done: cb.checked }); li.classList.toggle("done", cb.checked); track("Homework ticked"); }
      catch (e) { cb.checked = !cb.checked; }
    });
    return li;
  }));
  host.replaceChildren(el("section", { class: "sheet stack hw-card" },
    el("div", { class: "row between" }, el("h2", { text: "Homework from your coach" }),
      el("span", { class: "tiny faint", text: open.length ? `${open.length} to do` : "All done" })), list));
}

const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const whenText = (days) => (days == null ? "" : days >= 1 ? `${Math.round(days)} day${Math.round(days) === 1 ? "" : "s"}` : "10 minutes");

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

// ---------------- puzzles ----------------
async function puzzles(body) {
  const a = account();
  body.replaceChildren(el("p", { class: "muted", text: "Loading your puzzles…" }));
  let queue;
  try { queue = await api(`/api/accounts/${a.id}/puzzles?due=true&limit=50`); }
  catch (e) { body.replaceChildren(el("p", { class: "err", text: e.message })); return; }
  S.puzzles = queue;
  setDueCount(queue.length);
  const session = S.trainSession || (S.trainSession = { tried: 0, clean: 0, streak: 0, best: 0 });
  if (!queue.length) { body.replaceChildren(emptyState(session)); return; }

  // ---- layout ----
  const boardHost = el("div", { class: "pz-board" });
  const evalFill = el("i"), evalNum = el("span");
  const evalBar = el("div", { class: "evalbar", hidden: true, "aria-hidden": "true" }, evalFill, evalNum);
  const statusIcon = el("span", { class: "pz-icon", "aria-hidden": "true" });
  const statusTitle = el("strong");
  const statusText = el("span");
  const status = el("div", { class: "pz-status", role: "status", "aria-live": "polite" }, statusIcon, el("div", {}, statusTitle, statusText));
  const context = el("p", { class: "small muted" });
  const themes = el("div", { class: "chips" });
  const stage = el("div", { class: "pz-stage", title: "Review stage" });
  const moveList = el("ol", { class: "pz-moves", "aria-label": "Moves played" });
  const actions = el("div", { class: "pz-actions" });
  const explorePanel = el("div", { class: "pz-explore", hidden: true });
  const stats = el("p", { class: "tiny faint" });
  const left = el("span", { class: "small muted" });
  const typed = el("input", { type: "text", id: "typedMove", placeholder: "e.g. Nf3", autocomplete: "off", autocapitalize: "off", spellcheck: "false", maxlength: "10" });
  const typedErr = el("p", { class: "err small", role: "alert", hidden: true });
  const typedForm = el("form", { class: "move-input" }, el("label", { class: "sr", for: "typedMove", text: "Type your move" }), typed,
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
    stats.textContent = session.tried ? `This session: ${session.clean} of ${session.tried} solved first time${session.streak > 1 ? `, streak ${session.streak}` : ""}.` : "";
  };
  const btn = (text, onclick, cls = "btn quiet") => el("button", { class: cls, type: "button", text, onclick });

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
    explorePanel.hidden = true; evalBar.hidden = true;
    if (!cur) { setDueCount(0); body.replaceChildren(emptyState(session)); return; }
    const p = cur;
    mode = "intro"; line = []; step = 0; firstAttempt = true; hinted = false; hintLevel = 0; result = null; plies = [];
    renderMoves();
    left.textContent = queue.length ? `${queue.length} more due after this one` : "Last one for now";
    const side = sideToMove(p.fen);
    const prep = p.kind === "prep";
    const vs = p.opponent ? ` vs ${p.opponent}` : "";
    context.textContent = prep
      ? (p.played_san ? `Repertoire drill. In a game you played ${p.played_san} here instead of your prep.` : "Repertoire drill from your ChessBase preparation.")
      : `From your${p.time_class ? ` ${p.time_class}` : ""} game${vs}${p.played_at ? `, ${p.played_at}` : ""}. You played ${p.played_san} here and lost ${Math.round(p.win_loss)}% of your winning chances.`;
    themes.replaceChildren(...p.themes.map((t) => el("span", { class: "tag", text: t.replace(/_/g, " ") })));
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
    if (wasFirst) { session.tried++; track("Puzzle attempt", { verdict: r.verdict }); }
    if (r.verdict === "correct" || r.verdict === "also_good") {
      board.set({ badge: { sq: info.to, kind: "correct" } });
      playSound("correct");
      if (wasFirst && !hinted) { session.clean++; session.streak++; session.best = Math.max(session.best, session.streak); }
      else if (wasFirst) session.streak = 0;
      renderStats();
      const found = info.uci.slice(0, 4) === r.solution.slice(0, 4);
      if (found && line.length >= 3) { // keep going along the engine's line
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
    setStatus("wrong", `${info.san} isn't the engine's move`, "Try again from here, or see the rest of the line.");
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
    setDueCount(queue.length);
    renderActions();
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
    let r = null;
    if (!line.length) { // nothing attempted yet: fetch the answer (this counts as a miss)
      try { r = await post(`/api/puzzles/${p.id}/reveal`, { practice: !firstAttempt }); }
      catch (e) { setStatus("wrong", "Couldn't load the solution", e.message); mode = "solve"; lock(false); renderActions(); return; }
      if (!r.practice) result = r;
      if (firstAttempt) { session.tried++; session.streak = 0; renderStats(); }
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
    if (cur === p && mode === "solution") finish(false, `The engine's move was ${line[0]}.`);
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
    el("aside", { class: "pz-panel sheet" }, status, context, themes, stage, moveList, actions, explorePanel,
      el("details", { class: "pz-type" }, el("summary", { class: "small muted", text: "Type a move instead" }), typedForm, typedErr),
      stats,
      el("p", { class: "tiny faint", text: "Drag or click a piece; dots show where it can go. Right-click to draw arrows." }))));
  renderStats();
  next();
}

function emptyState(session) {
  return el("div", { class: "empty" }, el("h3", { text: session.tried ? "All done for now" : "No puzzles due" }),
    el("p", { text: session.tried
      ? `You solved ${session.clean} of ${session.tried} first time${session.best > 1 ? `, with a best streak of ${session.best}` : ""}. Solved puzzles come back on schedule.`
      : "You're up to date. New puzzles arrive with every sync, and solved ones return on schedule." }),
    el("a", { class: "btn quiet", href: "#/train/plan", text: "See this week's plan" }));
}

// ---------------- the week's plan ----------------
async function plan(body) {
  const a = account();
  let p;
  try { p = await api(`/api/accounts/${a.id}/plan`); }
  catch (e) { body.replaceChildren(el("p", { class: "err", text: e.message })); return; }
  const key = `pb:plan:${a.id}`;
  const done = store.get(key) || {};
  const days = p.days.map((d) => el("article", { class: "day" },
    el("header", {}, el("h3", { text: `Day ${d.day}` }), el("span", { class: "tiny faint", text: `${d.minutes} min` })),
    el("span", { class: "focus", text: d.focus }),
    el("ul", {}, ...d.items.map((it, i) => {
      const k = `${d.day}-${it.task}`;
      const id = `plan-${d.day}-${i}`;
      const cb = el("input", { type: "checkbox", id });
      cb.checked = !!done[k];
      const li = el("li", { class: cb.checked ? "done" : "" }, cb, el("label", { for: id, text: it.task }), el("span", { class: "min", text: `${it.minutes} min` }));
      cb.addEventListener("change", () => { done[k] = cb.checked; li.classList.toggle("done", cb.checked); store.set(key, done); });
      return li;
    }))));
  body.replaceChildren(el("div", { class: "stack" },
    el("p", { class: "muted", text: p.focus.length ? `This week's focus: ${p.focus.join(" and ")}, your two weakest skills.` : "A balanced week." }),
    el("div", { class: "week" }, ...days)));
}
