// Train: your own mistakes as puzzles (drag, click or type the move), and the week's plan.

import { S, account, api, el, post, store, track } from "../core.js";
import { Board, sideToMove, uciArrow, uciMarks } from "../board.js";
import { areaTabs } from "./common.js";
import { setDueCount } from "../shell.js";

export function render(main, sub) {
  const head = el("div", { class: "page-head" }, el("div", {}, el("h1", { text: "Train" }),
    el("p", { class: "sub", text: "Your own mistakes come back until you stop making them: after 1, 3, 7, 14 and 30 days." })));
  const body = el("div");
  main.replaceChildren(head, areaTabs("train", sub, [["", "Puzzles"], ["plan", "This week's plan"]]), body);
  if (sub === "plan") plan(body);
  else puzzles(body);
}

// ---------------- puzzles ----------------
async function puzzles(body) {
  const a = account();
  body.replaceChildren(el("p", { class: "muted", text: "Loading your puzzles…" }));
  try { S.puzzles = await api(`/api/accounts/${a.id}/puzzles?due=true&limit=50`); }
  catch (e) { body.replaceChildren(el("p", { class: "err", text: e.message })); return; }
  setDueCount(S.puzzles.length);
  if (!S.puzzles.length) {
    body.replaceChildren(el("div", { class: "empty" }, el("h3", { text: "No puzzles due" }),
      el("p", { text: "You're up to date. New puzzles arrive with every sync, and solved ones return on schedule." }),
      el("a", { class: "btn quiet", href: "#/train/plan", text: "See this week's plan" })));
    return;
  }
  const boardHost = el("div", { class: "board-wrap" });
  const title = el("h2");
  const meta = el("p", { class: "small faint" });
  const prompt = el("p", { class: "muted" });
  const themes = el("div", { class: "chips" });
  const verdict = el("div", { class: "verdict", "aria-live": "polite", hidden: true });
  const left = el("span", { class: "small muted" });
  const typed = el("input", { type: "text", id: "typedMove", placeholder: "e.g. Nf3", autocomplete: "off", autocapitalize: "off", spellcheck: "false", maxlength: "10" });
  const typedForm = el("form", { class: "move-input" }, el("label", { class: "sr", for: "typedMove", text: "Type your move" }), typed,
    el("button", { class: "btn quiet", type: "submit", text: "Play move" }));
  const next = el("button", { class: "btn", type: "button", text: "Next puzzle", hidden: true });
  const skip = el("button", { class: "btn quiet", type: "button", text: "Skip for now" });
  const flip = el("button", { class: "link quiet small", type: "button", text: "Flip board" });
  let cur = null, locked = false, orientation = "white";
  const board = new Board(boardHost, { interactive: true, onMove: (from, to) => attempt(`${from}${to}`) });

  function show() {
    cur = S.puzzles.shift() || null;
    locked = false;
    verdict.hidden = true;
    next.hidden = true; skip.hidden = !cur; typedForm.hidden = !cur;
    left.textContent = cur ? `${S.puzzles.length} more after this one` : "";
    if (!cur) { render(document.querySelector("#view")); return; }
    const side = sideToMove(cur.fen);
    orientation = side;
    const prep = cur.kind === "prep";
    title.textContent = prep ? `${cap(side)} to move: what's your preparation?` : `${cap(side)} to move. Find the better move.`;
    meta.textContent = `${prep ? "Repertoire drill" : "From your game"}, move ${cur.fen.split(" ")[5]}, review stage ${cur.box} of 5`;
    prompt.textContent = prep
      ? (cur.played_san ? `In a game you played ${cur.played_san} here instead.` : "What does your ChessBase preparation say here?")
      : `In the game you played ${cur.played_san} and lost ${Math.round(cur.win_loss)}% of your winning chances.`;
    themes.replaceChildren(...cur.themes.map((t) => el("span", { class: "tag", text: t.replace(/_/g, " ") })));
    board.set({ fen: cur.fen, orientation, arrows: [], marks: {}, locked: false, label: `Puzzle: ${side} to move` });
    typed.value = "";
  }

  async function attempt(move) {
    if (!cur || locked) return;
    locked = true;
    board.set({ locked: true });
    try {
      const r = await post(`/api/puzzles/${cur.id}/attempt`, { move });
      track("Puzzle attempt", { verdict: r.verdict });
      const when = r.next_review_in_days >= 1 ? `${Math.round(r.next_review_in_days)} days` : "a few minutes";
      const line = r.line && r.line.length ? ` The line: ${r.line.join(" ")}.` : "";
      verdict.className = `verdict ${r.verdict}`;
      verdict.textContent = r.verdict === "correct" ? `Correct: ${r.solution_san}.${r.note ? ` ${r.note}` : ""}${line} It comes back in ${when}.`
        : r.verdict === "also_good" ? `${r.your_move} works too. ${r.note} The engine's first choice was ${r.solution_san}.${line} It comes back in ${when}.`
        : r.kind === "prep" ? `${r.your_move} isn't your preparation. ${r.note || `Your prep: ${r.solution_san}.`} You'll see this again in ${when}.`
        : `${r.your_move} isn't it. The move was ${r.solution_san}.${line} You'll see this again in ${when}.`;
      verdict.hidden = false;
      const arrows = [r.verdict === "correct" ? null : uciArrow(r.your_uci, "you"), uciArrow(r.solution, "coach")].filter(Boolean);
      board.set({ arrows, marks: { ...uciMarks(r.your_uci, "you"), ...uciMarks(r.solution, "coach") } });
      next.hidden = false; skip.hidden = true; typedForm.hidden = true;
      setDueCount(S.puzzles.length);
      next.focus();
    } catch (e) {
      verdict.className = "verdict wrong"; verdict.textContent = e.message; verdict.hidden = false;
      locked = false;
      board.set({ locked: false });
    }
  }

  typedForm.addEventListener("submit", (e) => { e.preventDefault(); if (typed.value.trim()) attempt(typed.value.trim()); });
  next.addEventListener("click", show);
  skip.addEventListener("click", () => { if (cur) S.puzzles.push(cur); show(); });
  flip.addEventListener("click", () => { orientation = orientation === "white" ? "black" : "white"; board.set({ orientation }); });

  body.replaceChildren(el("div", { class: "puzzle" },
    el("div", { class: "puzzle-head" }, meta, title, prompt, themes),
    el("div", { class: "puzzle-board" }, boardHost, el("div", { class: "board-foot" }, left, flip)),
    el("div", { class: "puzzle-side" }, verdict, typedForm, el("div", { class: "row" }, skip, next),
      el("p", { class: "small faint", text: "Drag a piece, click it and then its square, or type the move. Pawns promote to a queen." }))));
  show();
}
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

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
