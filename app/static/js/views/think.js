// Train → Think here: the critical moments in your games that you played fast, and the routine ones you burned
// time on. The drill asks first "stop and think, or just play?", then (if it was critical) for the move.

import { account, api, el, fill, post, svg, toast, track } from "../core.js";
import { Board, applyMove, boardSettings, playSound, sideToMove, uciArrow } from "../board.js";
import { followJob, secs } from "./common.js";

const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

export async function render(body) {
  const a = account();
  body.replaceChildren(el("p", { class: "muted", text: "Loading…" }));
  let d;
  try { d = await api(`/api/accounts/${a.id}/think`); }
  catch (e) { body.replaceChildren(el("p", { class: "err", text: e.message })); return; }
  const progress = el("div");
  if (d.job_id) setTimeout(() => followJob(progress, d.job_id, () => render(body)));
  if (!d.scanned) { fill(body, intro(d, body, progress), progress); return; }
  const drillHost = el("div");
  fill(body, statsCard(d, body, progress), progress, drillHost);
  let items = [];
  try { items = await api(`/api/accounts/${a.id}/think/drill?limit=20`); } catch (e) { toast(e.message); }
  if (!items.length) {
    drillHost.replaceChildren(el("div", { class: "empty" }, el("h3", { text: "Nothing to drill right now" }),
      el("p", { text: d.stats.critical ? "Every rushed moment and over-thought move is scheduled for later. Scan more games, or come back tomorrow."
        : "No rushed critical moments found in the games scanned so far. Scan more games to be sure." })));
    return;
  }
  drill(drillHost, items, () => render(body));
}

async function scan(body, progress, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await post(`/api/accounts/${account().id}/think/scan`);
    track("Think scan");
    if (r.job_id) followJob(progress, r.job_id, () => render(body));
    else { toast(r.message, 7000); if (btn) btn.disabled = false; }
  } catch (e) { toast(e.message, 7000); if (btn) btn.disabled = false; }
}

function intro(d, body, progress) {
  const n = d.can_scan;
  const btn = el("button", { class: "btn", type: "button", text: n ? `Time my moves in ${n} games` : "Nothing to scan yet", disabled: !n || d.paused });
  btn.addEventListener("click", () => scan(body, progress, btn));
  return el("section", { class: "sheet stack think-intro" },
    el("h2", { text: "Do you think at the right moments?" }),
    el("p", { class: "muted", text: "Most club players don't lose because they can't find good moves. They lose because they play a critical position as fast as a routine one. This finds those moments in your own games." }),
    el("ol", { class: "twin-steps small" },
      el("li", { text: "From the clock times in your games, how long you took over every move." }),
      el("li", { text: "With Stockfish, which positions were critical: one move clearly better than all the others." }),
      el("li", { text: "Where you rushed a critical moment, and where you burned time on a routine move. Then you drill exactly those." })),
    el("p", { class: "small muted", text: d.clock_games ? `${d.clock_games} of your ${d.games} games have clock times.`
      : "None of your games have clock times yet. Lichess and Chess.com games do; PGN files need [%clk] comments." }),
    el("div", { class: "row" }, btn));
}

function statsCard(d, body, progress) {
  const s = d.stats;
  const big = (value, label, sub, cls = "") => el("div", { class: `think-stat ${cls}` }, el("b", { class: "figure", text: value }),
    el("span", { text: label }), sub ? el("small", { class: "tiny faint", text: sub }) : null);
  const fast = Object.entries(s.fast_below || {}).map(([tc, v]) => `${v}s in ${tc}`).join(", ");
  const scanBtn = d.can_scan ? el("button", { class: "btn quiet small", type: "button", text: `Scan ${d.can_scan} more games` }) : null;
  if (scanBtn) scanBtn.addEventListener("click", () => scan(body, progress, scanBtn));
  const worst = Object.entries(s.phases || {}).sort((a, b) => b[1].lost - a[1].lost)[0];
  return el("section", { class: "sheet stack think-head" },
    el("div", { class: "think-stats" },
      big(String(s.rushed), "critical moments rushed", s.critical ? `${s.rushed_pct}% of ${s.critical} critical moments` : "no critical moments yet", s.rushed ? "bad" : ""),
      big(s.points_per_10_games != null ? `${s.points_per_10_games}` : "–", "points lost per 10 games",
        s.rating_estimate ? `worth about +${s.rating_estimate} rating if you'd stopped to think (estimate)` : null),
      big(s.time_ratio != null ? `${s.time_ratio}×` : "–", "time on critical vs routine moves",
        s.time_critical_s != null ? `${secs(s.time_critical_s)} vs ${secs(s.time_routine_s)} (median). Higher is better.` : null),
      big(s.overthought ? secs(s.overthought_s) : "None", "spent over-thinking routine moves",
        s.overthought ? `${s.overthought} move${s.overthought === 1 ? "" : "s"}` : "no long thinks on routine moves")),
    bucketChart(s.buckets),
    el("p", { class: "small muted" },
      `Fast means under ${fast || "your usual pace"} (60% of your usual time per move). `,
      s.rushed ? `${s.rushed_with_time} of the ${s.rushed} rushed moments happened with plenty of time left on your clock. ` : "",
      worst ? `Rushing costs you most in the ${worst[0]}.` : ""),
    el("div", { class: "spread" }, el("span", { class: "tiny faint", text: `${d.scanned} games scanned, ${s.moves} of your moves timed.` }), scanBtn));
}

/** Median think time by how critical the position was: a good player's bars rise from left to right. */
function bucketChart(buckets) {
  const max = Math.max(1, ...buckets.map((b) => b.median_s || 0));
  const W = 360, H = 120, bw = 64, gap = (W - 4 * bw) / 5;
  const bars = buckets.map((b, i) => {
    const h = b.median_s ? Math.max(3, (b.median_s / max) * (H - 34)) : 0;
    const x = gap + i * (bw + gap);
    return [svg("rect", { x, y: H - 18 - h, width: bw, height: h, rx: 3, class: i >= 2 ? "crit" : "calm" }),
      svg("text", { x: x + bw / 2, y: H - 22 - h, class: "val", "text-anchor": "middle" }, b.median_s != null ? secs(b.median_s) : "–"),
      svg("text", { x: x + bw / 2, y: H - 4, class: "lbl", "text-anchor": "middle" }, b.label)];
  });
  return el("figure", { class: "think-chart" },
    svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Median time per move: ${buckets.map((b) => `${b.label} ${b.median_s ?? "none"} seconds`).join(", ")}` }, ...bars.flat()),
    el("figcaption", { class: "tiny faint", text: "Your median time per move, from routine positions (left) to only-moves (right)." }));
}

// ---------------- the drill ----------------
function drill(host, queue, onFinish) {
  const boardHost = el("div", { class: "pz-board" });
  const statusTitle = el("strong"), statusText = el("span");
  const status = el("div", { class: "pz-status", role: "status", "aria-live": "polite" }, el("span", { class: "pz-icon", "aria-hidden": "true" }),
    el("div", {}, statusTitle, statusText));
  const context = el("p", { class: "small muted" });
  const ask = el("div", { class: "think-ask" });
  const detail = el("div", { class: "stack small" });
  const actions = el("div", { class: "pz-actions" });
  const tally = el("p", { class: "tiny faint" });
  const left = el("span", { class: "small muted" });
  const timer = el("span", { class: "tiny faint num" });
  const session = { calls: 0, right: 0, found: 0, critical: 0 };
  let cur = null, mode = "idle", started = 0, tick = null;
  const board = new Board(boardHost, { interactive: true, locked: true, label: "Drill board", onMove: (_f, _t, info) => onMove(info) });
  const setStatus = (kind, title, text = "") => {
    status.className = `pz-status ${kind}`;
    status.firstChild.textContent = { correct: "✓", wrong: "✗", done: "★" }[kind] || "";
    status.firstChild.dataset.side = kind === "turn" ? sideToMove(board.fen) : "";
    statusTitle.textContent = title; statusText.textContent = text;
  };
  const btn = (text, onclick, cls = "btn quiet") => el("button", { class: cls, type: "button", text, onclick });
  const renderTally = () => { tally.textContent = session.calls ? `This session: ${session.right} of ${session.calls} calls right${session.critical ? `, ${session.found} of ${session.critical} critical moves found` : ""}.` : ""; };

  async function next() {
    clearInterval(tick);
    cur = queue.shift() || null;
    if (!cur) { onFinish(); return; }
    const p = cur;
    mode = "ask";
    detail.replaceChildren(); actions.replaceChildren();
    left.textContent = queue.length ? `${queue.length} more after this one` : "Last one";
    const side = sideToMove(p.fen);
    const clock = p.clock != null ? `, ${secs(p.clock)} on your clock` : "";
    context.textContent = `Move ${p.move_number} of your ${p.time_class || ""} game vs ${p.opponent || "your opponent"}${p.played_at ? ` (${p.played_at})` : ""}${clock}.`;
    if (p.prev_fen && p.prev_uci) {
      board.set({ fen: p.prev_fen, orientation: side, lastMove: null, arrows: [], marks: {}, badge: null, locked: true, animate: false });
      await wait(450);
      if (cur !== p) return;
      board.set({ fen: p.fen, lastMove: p.prev_uci, sound: true });
    } else {
      board.set({ fen: p.fen, orientation: side, lastMove: null, arrows: [], marks: {}, badge: null, locked: true, animate: false });
    }
    setStatus("turn", `${cap(side)} to move`, "Is this a moment to stop and think?");
    started = Date.now();
    timer.textContent = "";
    tick = setInterval(() => { timer.textContent = `You've looked for ${Math.round((Date.now() - started) / 1000)}s`; }, 1000);
    const think = btn("Stop and think", () => call("think"), "btn think-btn");
    const play = btn("Just play", () => call("play"), "btn quiet think-btn");
    think.setAttribute("aria-keyshortcuts", "T"); play.setAttribute("aria-keyshortcuts", "P");
    ask.replaceChildren(el("p", { class: "small", text: "Critical (one move matters) or routine (several moves are fine)?" }),
      el("div", { class: "row" }, think, play), el("p", { class: "tiny faint", text: "Keys: T to think, P to play." }));
    setTimeout(() => think.focus(), 0);
  }

  async function call(choice) {
    if (mode !== "ask") return;
    const p = cur;
    mode = "wait";
    clearInterval(tick);
    ask.replaceChildren();
    let r;
    try { r = await post(`/api/think/${p.id}/call`, { call: choice }); }
    catch (e) { setStatus("wrong", "Couldn't check that", e.message); mode = "ask"; return; }
    if (cur !== p) return;
    session.calls++; if (r.right) session.right++;
    track("Think call", { right: r.right });
    renderTally();
    if (r.critical) {
      session.critical++;
      setStatus(r.right ? "correct" : "wrong", r.right ? "Right: it's critical" : "This one was critical", "Find the move.");
      playSound(r.right ? "correct" : "wrong");
      detail.replaceChildren(el("p", { text: r.right ? "One move matters here." : `In the game you took ${secs(r.spent)} here (fast is under ${r.fast_below}s for you).` }));
      mode = "move";
      board.set({ locked: false });
      actions.replaceChildren(btn("Show the move", () => move(null, true), "btn quiet small"));
      return;
    }
    // routine: show the fine moves, and how long you spent in the game
    setStatus(r.right ? "correct" : "wrong", r.right ? "Right: a routine move" : "This one was routine", r.message);
    playSound(r.right ? "correct" : "wrong");
    const arrows = (r.fine_moves || []).slice(0, 4).map((san) => applyMove(p.fen, san)).filter(Boolean).map((m) => uciArrow(m.uci, "draw"));
    board.set({ arrows });
    detail.replaceChildren(el("p", {}, "Any of these is fine: ", el("b", { text: (r.fine_moves || []).join(", ") || "several moves" }), "."));
    done();
  }

  async function onMove(info) {
    if (mode !== "move") return;
    board.set({ locked: true });
    await move(info, false);
  }

  async function move(info, reveal) {
    const p = cur;
    mode = "wait";
    let r;
    try { r = await post(`/api/think/${p.id}/move`, reveal ? { move: "", reveal: true } : { move: info.uci }); }
    catch (e) {
      setStatus("wrong", "Couldn't check that move", e.message);
      board.set({ fen: p.fen, lastMove: p.prev_uci, badge: null });
      mode = "move"; board.set({ locked: false });
      return;
    }
    if (cur !== p) return;
    const good = r.verdict === "correct" || r.verdict === "also_good";
    if (good) session.found++;
    renderTally();
    if (info) board.set({ badge: { sq: info.to, kind: good ? "correct" : "wrong" } });
    playSound(good ? "correct" : "wrong");
    await wait(good ? 300 : 700);
    if (cur !== p) return;
    const game = applyMove(p.fen, r.played_san);
    board.set({ fen: p.fen, lastMove: p.prev_uci, badge: null,
      arrows: [uciArrow(r.best, "coach"), game && game.uci !== r.best ? uciArrow(game.uci, "you") : null].filter(Boolean) });
    setStatus(good ? "correct" : "wrong", good ? (r.verdict === "correct" ? "Found it" : "Good move") : reveal ? "The move" : `${r.your_move} isn't it`,
      `Best: ${r.best_san}${r.second_san ? `. The next best, ${r.second_san}, was ${Math.round(r.gap)} points worse` : ""}.`);
    detail.replaceChildren(el("p", {}, "In the game you took ", el("b", { text: secs(r.spent) }), " and played ", el("b", { text: r.played_san }),
      r.loss >= 1 ? `, losing ${Math.round(r.loss)}% of your winning chances.` : ", and got away with it."),
    el("p", { class: "legend-ink" }, el("span", {}, el("i", { style: { background: "#c9372c" } }), "Best move"), el("span", {}, el("i", { style: { background: "#2b44c9" } }), "Your move in the game")));
    done();
  }

  function done() {
    mode = "done";
    const n = btn(queue.length ? "Next position" : "Finish", next, "btn");
    actions.replaceChildren(n);
    setTimeout(() => n.focus(), 0);
  }

  const onKey = (e) => {
    if (!host.isConnected) { document.removeEventListener("keydown", onKey); clearInterval(tick); return; }
    if (mode !== "ask" || e.target.closest("input, textarea, select")) return;
    if (e.key === "t" || e.key === "T") call("think");
    if (e.key === "p" || e.key === "P") call("play");
  };
  document.addEventListener("keydown", onKey);

  host.replaceChildren(el("div", { class: "pz think-drill" },
    el("div", { class: "pz-left" }, el("div", { class: "pz-boardrow" }, boardHost),
      el("div", { class: "board-foot" }, left, timer, boardSettings(board))),
    el("aside", { class: "pz-panel sheet" }, el("p", { class: "small faint", text: "Think here · drill" }), status, context, ask, detail, actions, tally)));
  renderTally();
  next();
}
