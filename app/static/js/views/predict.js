// Train → Predicted for you: puzzles from the Lichess database that a Twin of you, trained on your own games,
// expects you to miss. The status card shows your puzzle rating, your Twin and how good the predictions have been.

import { S, account, api, el, fill, isPro, post, toast, track } from "../core.js";
import { followJob } from "./common.js";
import { puzzleSession, themeLabel } from "./puzzles.js";

export async function render(body) {
  const a = account();
  body.replaceChildren(el("p", { class: "muted", text: "Loading your predictions…" }));
  let d;
  try { d = await api(`/api/accounts/${a.id}/predicted`); }
  catch (e) { body.replaceChildren(el("p", { class: "err", text: e.message })); return; }
  const progress = el("div", { class: "pred-progress" });
  const card = statusCard(d, body, progress);
  const play = el("div");
  fill(body, card, progress, play);
  if (d.job_id) followJob(progress, d.job_id, () => render(body));
  if (!d.puzzles.length) { play.replaceChildren(emptyState(d, body, progress)); return; }
  puzzleSession(play, d.puzzles, { kind: "predicted", empty: (session) => emptyState(d, body, progress, session) });
}

async function refresh(body, progress, twin, button) {
  if (button) button.disabled = true;
  try {
    const r = await post(`/api/accounts/${account().id}/predicted/refresh`, { twin });
    track(twin ? "Twin of you built" : "Predicted puzzles");
    if (r.message) toast(r.message, 6000);
    if (r.job_id) followJob(progress, r.job_id, (j) => {
      if (j.twin_note) toast(j.twin_note, 8000);
      else if (j.predicted === 0 && !twin) toast("No new puzzles fit right now: you've seen the ones prepared at your level. More are prepared as the bank grows.", 8000);
      render(body);
    });
  } catch (e) {
    toast(e.upgrade ? `${e.message}` : e.message, 7000);
    if (button) button.disabled = false;
  }
}

function statusCard(d, body, progress) {
  const st = d.status;
  const t = st.twin;
  // 1. your puzzle rating
  const rating = el("div", { class: "pred-cell" },
    el("p", { class: "small faint", text: "Your puzzle rating" }),
    el("p", { class: "pred-big" }, el("b", { class: "figure", text: String(st.puzzle_rating) }),
      el("span", { class: "small muted", text: st.rating_sure ? ` ±${st.rd}` : " (still learning)" })),
    el("p", { class: "tiny faint", text: "Starts from your game rating and moves with every first try." }));
  // 2. your Twin
  const canStudy = Math.max(0, Math.min(d.study_max - st.studied, st.games - st.studied));
  const twinBtn = el("button", { class: t ? "btn quiet small" : "btn small", type: "button",
    text: t ? (canStudy ? `Teach it ${Math.min(canStudy, 100)} more games` : "Refit my Twin") : "Build my Twin" });
  twinBtn.addEventListener("click", () => refresh(body, progress, true, twinBtn));
  const enough = st.games >= d.min_games;
  if (!enough || d.paused) twinBtn.disabled = true;
  const twinCell = el("div", { class: "pred-cell" }, el("p", { class: "small faint", text: "Your Twin" }));
  if (t) {
    const f = t.faithfulness || {};
    twinCell.append(
      el("p", { class: "pred-big" }, el("b", { class: "figure", text: String(t.games) }), el("span", { class: "small muted", text: " games learned" })),
      t.style && t.style.length ? el("div", { class: "chips small-chips" }, ...t.style.slice(0, 3).map((s) => el("span", { class: "chip static", text: s.text }))) : null,
      f.twin_top3 != null ? el("p", { class: "tiny faint", text: `Tested on games it didn't learn from: your real move was among its top three choices ${f.twin_top3}% of the time.` }) : null,
      el("div", { class: "row" }, twinBtn));
  } else {
    twinCell.append(
      el("p", { class: "small", text: enough ? "Not built yet. It studies your games to learn your instincts: captures or quiet moves, checks, trades, retreats. Then predictions include your personal blind spots."
        : `Your Twin needs at least ${d.min_games} analysed games; you have ${st.games}.` }),
      el("div", { class: "row" }, twinBtn,
        el("span", { class: "tiny faint", text: enough ? `Studies up to ${Math.min(canStudy || st.games, 100)} games, about ${Math.max(1, Math.round(Math.min(canStudy || st.games, 100) * 1.1 / 60))} min.` : "" })));
  }
  // 3. how good the predictions are
  const tr = st.track;
  const trackCell = el("div", { class: "pred-cell" }, el("p", { class: "small faint", text: "How well it predicts you" }));
  if (tr && tr.attempts) {
    trackCell.append(el("p", { class: "pred-big" }, el("b", { class: "figure", text: `${tr.missed}/${tr.attempts}` }), el("span", { class: "small muted", text: " missed" })),
      el("p", { class: "tiny faint", text: `It expected ${tr.expected_misses} misses. Closer is better; the Brier score is ${tr.brier} (0 is perfect, 0.25 is a coin toss).` }));
  } else {
    trackCell.append(el("p", { class: "small muted", text: "Answer a few puzzles and this shows how many misses it expected and how many there were." }));
  }
  if (st.weak_themes.length) {
    trackCell.append(el("p", { class: "tiny faint", text: "Harder for you than your rating suggests:" }),
      el("div", { class: "chips small-chips" }, ...st.weak_themes.map((w) => el("span", { class: "chip static", text: themeLabel(w.theme) }))));
  }
  // the deck
  const n = Math.min(10, d.left_today);
  const more = el("button", { class: "btn", type: "button", text: n ? `Predict my next ${n}` : "Today's puzzles are used", disabled: !n || d.paused });
  more.addEventListener("click", () => refresh(body, progress, false, more));
  const owner = S.me.is_admin && st.bank.sample_only
    ? el("p", { class: "notice blue small" }, "Owner: only the built-in sample of ", el("b", { text: String(st.bank.puzzles) }),
      " puzzles is loaded. For Lichess's full database (millions of rated puzzles) run ", el("code", { text: "python scripts/import_puzzles.py --download" }), ".")
    : null;
  return el("section", { class: "sheet stack pred-head" },
    el("div", { class: "pred-grid" }, rating, twinCell, trackCell),
    el("div", { class: "spread pred-deck" },
      el("p", { class: "small muted" }, el("b", { text: `${st.due} waiting` }), ` · ${d.left_today} of ${d.per_day} new today`,
        isPro() ? "" : " · Pro: 40 a day", d.paused ? " · new predictions are paused for maintenance" : ""), more),
    owner);
}

function emptyState(d, body, progress, session) {
  const n = Math.min(10, d.left_today);
  const btn = el("button", { class: "btn", type: "button", text: `Predict my next ${n}`, disabled: !n || d.paused });
  btn.addEventListener("click", () => refresh(body, progress, false, btn));
  const done = session && session.tried;
  return el("div", { class: "empty stack" },
    el("h3", { text: done ? `Done: you beat ${session.beat} of ${session.tried} predictions` : "No predicted puzzles waiting" }),
    el("p", { text: n ? "Each pick is a Lichess-rated puzzle near your level that your Twin expects you to miss, and why. Misses come back on a schedule until they stick."
      : `You've had today's ${d.per_day} new puzzles. Missed ones come back on schedule; new ones tomorrow.` }),
    n ? el("div", { class: "row", style: { justifyContent: "center" } }, btn) : null);
}
