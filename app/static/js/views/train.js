// Train: your own mistakes as puzzles, puzzles predicted for you by your Twin, the "Think here" drill, and the
// week's plan. Puzzles are played out on the board (see puzzles.js); the server grades the first move.

import { S, account, api, el, patch, store, track } from "../core.js";
import { areaTabs } from "./common.js";
import { puzzleSession } from "./puzzles.js";
import * as predicted from "./predict.js";
import * as think from "./think.js";
import { setDueCount } from "../shell.js";

const SUBS = {
  "": "Your own mistakes come back until you stop making them: after 1, 3, 7, 14 and 30 days.",
  predicted: "Puzzles you haven't failed yet but probably would: picked by a Twin of you, trained on your own games.",
  think: "Find the moments in your games that needed a long think, and learn to feel them coming.",
  plan: "Seven days of work aimed at your two weakest skills.",
};

export function render(main, sub) {
  const key = SUBS[sub || ""] ? sub || "" : "";
  const head = el("div", { class: "page-head" }, el("div", {}, el("h1", { text: "Train" }), el("p", { class: "sub", text: SUBS[key] })));
  const body = el("div");
  const hw = el("div");
  main.replaceChildren(head, hw, areaTabs("train", key, [["", "Your mistakes"], ["predicted", "Predicted for you"], ["think", "Think here"],
    ["plan", "This week's plan"]]), body);
  if (S.me.coaches && S.me.coaches.length && !key) homeworkCard(hw);
  if (key === "plan") plan(body);
  else if (key === "predicted") predicted.render(body);
  else if (key === "think") think.render(body);
  else mine(body, sub === "puzzle" ? Number(location.hash.split("/")[3]) || null : null);
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

// ---------------- your own mistakes ----------------
/** Your due puzzles. `first` (from #/train/puzzle/<id>, e.g. the command palette) goes to the front, due or not. */
async function mine(body, first = null) {
  const a = account();
  body.replaceChildren(el("p", { class: "muted", text: "Loading your puzzles…" }));
  let queue;
  try {
    queue = await api(`/api/accounts/${a.id}/puzzles?due=true&limit=50`);
    if (first) {
      let pick = queue.find((q) => q.id === first);
      if (!pick) pick = (await api(`/api/accounts/${a.id}/puzzles?due=false&limit=100`)).find((q) => q.id === first);
      if (pick) queue = [pick, ...queue.filter((q) => q.id !== first)];
    }
  } catch (e) { body.replaceChildren(el("p", { class: "err", text: e.message })); return; }
  S.puzzles = queue;
  setDueCount(queue.length);
  if (!queue.length) { body.replaceChildren(emptyState({ tried: 0 })); return; }
  puzzleSession(body, queue, { kind: "mine", empty: emptyState });
}

function emptyState(session) {
  return el("div", { class: "empty" }, el("h3", { text: session.tried ? "All done for now" : "No puzzles due" }),
    el("p", { text: session.tried
      ? `You solved ${session.clean} of ${session.tried} first time${session.best > 1 ? `, with a best streak of ${session.best}` : ""}. Solved puzzles come back on schedule.`
      : "You're up to date. New puzzles arrive with every sync, and solved ones return on schedule." }),
    el("div", { class: "row", style: { justifyContent: "center" } },
      el("a", { class: "btn", href: "#/train/predicted", text: "Try puzzles predicted for you" }),
      el("a", { class: "btn quiet", href: "#/train/plan", text: "See this week's plan" })));
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
