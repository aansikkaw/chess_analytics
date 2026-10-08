// Prepare: Prep Check (your ChessBase repertoire vs your games) and Opponent Scout. Both Pro.

import { S, account, api, del, el, fill, hasFeature, post, showErr, sleep, toast, track, upload } from "../core.js";
import { areaTabs, lockCard, openPosition, sampleRows, table } from "./common.js";

export function render(main, sub) {
  const head = el("div", { class: "page-head" }, el("div", {}, el("h1", { text: "Prepare" }),
    el("p", { class: "sub", text: "Check your preparation against how you really play, and scout your next opponent." })));
  const body = el("div");
  main.replaceChildren(head, areaTabs("prepare", sub, [["", "Prep Check", !hasFeature("prep")], ["scout", "Opponent Scout", !hasFeature("scout")]]), body);
  if (sub === "scout") scout(body);
  else prepCheck(body);
}

// ---------------- Prep Check ----------------
async function prepCheck(body) {
  const a = account();
  if (!hasFeature("prep")) {
    let t = {};
    try { t = await api(`/api/accounts/${a.id}/teasers`); } catch { /* optional */ }
    body.replaceChildren(lockCard({ found: t.lines ?? null, foundLabel: "opening lines found in your games to check",
      title: "Find where you forget your own preparation",
      blurb: "Upload the repertoire you keep in ChessBase. Pro compares it with every game you've played: where you left your prep, which replies it doesn't cover, and which lines end badly. Then it drills those positions.",
      sample: sampleRows(6) }));
    return;
  }
  const file = el("input", { type: "file", id: "prepFile", accept: ".pgn,.zip", required: true });
  const color = el("select", { id: "prepColor" }, el("option", { value: "auto", text: "Detect it" }), el("option", { value: "white", text: "White" }), el("option", { value: "black", text: "Black" }));
  const name = el("input", { type: "text", id: "prepName", maxlength: "60", placeholder: "e.g. 1.e4 main lines" });
  const err = el("p", { class: "err", role: "alert", hidden: true });
  const stage = el("p", { class: "small muted", "aria-live": "polite" });
  const btn = el("button", { class: "btn", type: "submit", text: "Check my prep" });
  const form = el("form", { class: "stack" },
    el("div", { class: "form-row" },
      el("div", { class: "field" }, el("label", { for: "prepFile", text: "Repertoire file (.pgn or .zip)" }), file),
      el("div", { class: "field narrow" }, el("label", { for: "prepColor", text: "Colour" }), color),
      el("div", { class: "field" }, el("label", { for: "prepName", text: "Name (optional)" }), name), btn), stage, err);
  const out = el("div", { class: "stack-lg" });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showErr(err, "");
    if (!file.files[0]) { showErr(err, "Choose your repertoire file first."); return; }
    btn.disabled = true;
    try {
      const r = await upload(`/api/accounts/${a.id}/repertoires`, { file: file.files[0], color: color.value, name: name.value });
      track("Prep check started");
      for (;;) {
        const j = await api(`/api/jobs/${r.job_id}`);
        stage.textContent = j.state === "queued" ? "Waiting for the engine…" : `${j.stage}…`;
        if (j.state === "error") throw new Error(j.error);
        if (j.state === "done") break;
        await sleep(1000);
      }
      stage.textContent = "";
      file.value = ""; name.value = "";
      toast("Prep Check done. Its drills are in Train.");
      list(out, a);
    } catch (ex) { stage.textContent = ""; showErr(err, ex.message); }
    finally { btn.disabled = false; }
  });
  body.replaceChildren(el("div", { class: "stack-lg" },
    el("section", { class: "sheet stack" }, el("h2", { text: "Upload your repertoire" }),
      el("p", { class: "small muted" }, "In ChessBase, put your repertoire with all its variations into a PGN database (File, New, Database, choose PGN), copy your repertoire games into it, and upload that file. One colour per file."),
      form), out));
  list(out, a);
}

async function list(out, a) {
  try {
    const reps = await api(`/api/accounts/${a.id}/repertoires`);
    out.replaceChildren(...(reps.length ? reps.map((r) => report(r, () => list(out, a))) : [el("p", { class: "muted", text: "No repertoire uploaded yet." })]));
  } catch (e) { out.replaceChildren(el("p", { class: "err", text: e.message })); }
}

function report(rep, refresh) {
  const r = rep.report;
  const rm = el("button", { class: "link quiet small", type: "button", text: "Delete", onclick: async () => {
    if (rm.dataset.confirm !== "1") { rm.dataset.confirm = "1"; rm.textContent = "Click again to delete"; return; }
    await del(`/api/repertoires/${rep.id}`); refresh();
  } });
  const head = el("div", { class: "spread" }, el("div", {}, el("p", { class: "small faint", text: `${rep.color === "white" ? "White" : "Black"} repertoire` }), el("h2", { text: rep.name })), rm);
  if (!r) return el("section", { class: "sheet" }, head, el("p", { class: "muted", text: "Still checking. This updates in a moment." }));
  const s = r.summary;
  const show = (fen, title, extra = {}) => el("button", { class: "btn quiet small", type: "button", text: "Show position", onclick: () => openPosition({ fen, orientation: rep.color, ...extra }, { title }) });
  const rows = (items, make, empty) => (items.length ? items.slice(0, 8).map(make) : [el("p", { class: "small muted", text: empty })]);
  return el("section", { class: "sheet stack" }, head,
    el("div", { class: "kpis" },
      el("div", {}, el("b", { class: "figure", text: `${s.followed_pct}%` }), el("span", { text: "of games stayed in your prep" })),
      el("div", {}, el("b", { class: "figure", text: String(s.avg_moves_in_prep) }), el("span", { text: "moves in prep, on average" })),
      el("div", {}, el("b", { class: "figure res-l", text: String(r.deviations.length) }), el("span", { text: "places you left it" })),
      el("div", {}, el("b", { class: "figure", text: String(r.gaps.length) }), el("span", { text: "gaps" })),
      el("div", {}, el("b", { class: "figure", text: String(r.drills.length) }), el("span", { text: "drills added" }))),
    el("h3", { text: "Where you left your own preparation" }),
    ...rows(r.deviations, (d) => el("div", { class: "prep-row" },
      el("div", {}, el("span", { class: "line", text: `After ${d.line}` }),
        el("p", { class: "small" }, "Your prep: ", el("b", { text: d.prep_moves.join(", ") }), ". You played ", el("b", { class: "res-l", text: d.played }), ` ${d.times} time${d.times === 1 ? "" : "s"} and scored ${d.score_pct}%.`)),
      show(d.fen, `After ${d.line}`)), "None: whenever your prep had an answer, you played it."),
    el("h3", { text: "Replies your prep doesn't cover" }),
    ...rows(r.gaps, (g) => el("div", { class: "prep-row" },
      el("div", {}, el("span", { class: "line", text: `After ${g.line}` }),
        el("p", { class: "small" }, `Opponents played this ${g.times} time${g.times === 1 ? "" : "s"}.`, g.engine_reply ? el("span", {}, " The engine suggests adding ", el("b", { text: g.engine_reply }), ` (${g.engine_eval} for you).`) : "")),
      show(g.fen_after, `After ${g.line}`)), "None yet: opponents haven't left your prep before you did."),
    el("h3", { text: "Prepared lines that end badly for you" }),
    ...rows(r.holes, (h) => el("div", { class: "prep-row" },
      el("div", {}, el("span", { class: "line", text: h.line }), el("p", { class: "small", text: `The engine gives ${h.eval_for_you} for you at the end of this line.` })),
      show(h.fen, h.line)), "None: every checked line ends at −0.5 or better for you."),
    el("p", { class: "small muted", text: `${s.positions} prepared positions in ${s.chapters} chapter${s.chapters === 1 ? "" : "s"}, checked against ${s.games} games.` }));
}

// ---------------- Opponent Scout ----------------
async function scout(body) {
  const a = account();
  if (!hasFeature("scout")) {
    body.replaceChildren(lockCard({ found: null, title: "Scout your next opponent in 30 seconds",
      blurb: "Their main lines with an engine check, where they score badly, how they lose (on time, early collapses), and where your repertoire meets theirs.",
      sample: sampleRows(6) }));
    return;
  }
  const platforms = (S.config && S.config.platforms) || ["lichess"];
  const plat = el("select", { id: "scPlat" }, ...platforms.map((p) => el("option", { value: p, text: p === "lichess" ? "Lichess" : "Chess.com" })));
  const handle = el("input", { type: "text", id: "scHandle", autocomplete: "off", autocapitalize: "off", spellcheck: "false", required: true, maxlength: "30" });
  const err = el("p", { class: "err", role: "alert", hidden: true });
  const btn = el("button", { class: "btn", type: "submit", text: "Scout them" });
  const out = el("div", { class: "stack-lg" });
  const form = el("form", { class: "form-row" },
    el("div", { class: "field narrow" }, el("label", { for: "scPlat", text: "Where they play" }), plat),
    el("div", { class: "field" }, el("label", { for: "scHandle", text: "Their username" }), handle), btn);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showErr(err, "");
    if (!handle.value.trim()) { showErr(err, "Type their username."); return; }
    btn.disabled = true;
    out.replaceChildren(el("p", { class: "muted", text: "Downloading their last 100 rated games and building the report…" }));
    try {
      const r = await post("/api/scout", { platform: plat.value, handle: handle.value.trim(), account_id: a.games ? a.id : null });
      track("Scout report");
      scoutReport(out, r);
    } catch (ex) { out.replaceChildren(); showErr(err, ex.message); }
    finally { btn.disabled = false; }
  });
  body.replaceChildren(el("div", { class: "stack-lg" },
    el("section", { class: "sheet stack" }, el("h2", { text: "Who are you playing next?" }), form, err,
      el("p", { class: "small muted", text: `Uses their last 100 rated games. Your own games show where your openings meet theirs. ${S.me.usage_today.scout} of ${S.me.limits.scouts_per_day} reports used today.` })),
    out));
}

function scoutReport(out, r) {
  const h = r.habits || {};
  const sideCols = [["They're", (x) => x.their_color], ["Line", (x) => x.line, "num"], ["Games", (x) => x.games, "num"], ["Their score", (x) => `${x.their_score_pct}%`, "num"]];
  const lineCols = [["Line", (x) => x.line, "num"], ["Games", (x) => x.games, "num"], ["Share", (x) => `${x.share_pct}%`, "num"], ["Their score", (x) => `${x.their_score_pct}%`, "num"]];
  const sec = (title, node, sub) => el("section", { class: "sheet stack" }, el("h3", { text: title }), sub ? el("p", { class: "small muted", text: sub }) : null, node);
  fill(out,
    el("section", { class: "sheet stack" }, el("p", { class: "small faint", text: `Scouting report from ${r.games} games` }), el("h2", { text: r.opponent }),
      el("ul", { class: "notes" }, ...r.insights.map((t) => el("li", { text: t })))),
    r.steer_into.length ? sec("Steer into these lines", table(sideCols, r.steer_into), "They score badly here.") : null,
    r.avoid.length ? sec("Avoid these lines", table(sideCols, r.avoid), "Their best results.") : null,
    r.prep.length ? sec("Their main lines, checked by the engine", table([["They're", (x) => x.their_color], ["Line", (x) => x.line, "num"], ["Games", (x) => x.games, "num"], ["Eval for you", (x) => x.eval_for_you ?? "–", "num"]], r.prep)) : null,
    r.battleground && r.battleground.length ? sec("Where your openings meet", table([["You're", (x) => x.you_play], ["Line", (x) => x.line, "num"],
      ["Your score", (x) => `${x.your_score_pct}% (${x.your_games})`, "num"], ["Their score", (x) => `${x.their_score_pct}% (${x.their_games})`, "num"]], r.battleground)) : null,
    sec("Habits", el("table", { class: "kv" }, el("tbody", {}, ...[
      ["Average game length", `${h.avg_game_length_moves ?? "–"} moves`], ["Losses on time", `${h.losses_on_time_pct ?? 0}% of losses`],
      ["Losses before move 25", `${h.losses_before_move_25_pct ?? 0}% of losses`],
      ["Clock left at move 30", h.median_clock_left_at_move_30_pct != null ? `${h.median_clock_left_at_move_30_pct}% (median)` : "–"]]
      .map(([k, v]) => el("tr", {}, el("th", { scope: "row", text: k }), el("td", { class: "num", text: v })))))),
    el("div", { class: "grid-2 even" }, sec("Their openings as White", table(lineCols, r.as_white || [])), sec("Their openings as Black", table(lineCols, r.as_black || []))));
}
