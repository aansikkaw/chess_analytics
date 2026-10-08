// Diagnose: Rating DNA and findings, recent games (open any on the board), openings, progress, knowledge bundle.

import { $, PLATFORM, S, account, api, el, hasFeature, openDialog, track } from "../core.js";
import { renderDNA, shareCard } from "../dna.js";
import { areaTabs, lineChart, lockCard, openConcept, openGame, openPosition, sampleRows, table } from "./common.js";
import { setDueCount } from "../shell.js";

export async function render(main, sub) {
  const a = account();
  main.replaceChildren(el("p", { class: "muted", text: "Loading your diagnosis…" }));
  let p;
  try {
    p = S.profile && S.profile.account.id === a.id ? S.profile : await api(`/api/accounts/${a.id}/profile`);
    S.profile = p;
  } catch (e) { main.replaceChildren(el("p", { class: "err", text: e.message })); return; }
  setDueCount(p.due_puzzles);
  const d = p.dna;
  const head = el("div", { class: "page-head" },
    el("div", {}, el("h1", { text: a.handle }), el("p", { class: "sub", text: `${PLATFORM[a.platform]}, ${d.games} analysed games` })),
    el("div", { class: "statline" },
      el("div", {}, el("b", { class: "figure", text: `${d.base_rating}${d.rating_is_estimated ? "?" : ""}` }), el("span", { text: "rating" })),
      el("div", {}, el("b", { class: "figure", text: `${d.overall_accuracy}%` }), el("span", { text: "accuracy" })),
      el("div", {}, el("b", { class: "figure", text: String(p.due_puzzles) }), el("span", {}, el("a", { href: "#/train", text: "puzzles waiting" })))));
  const tabs = areaTabs("diagnose", sub, [["", "Overview"], ["openings", "Openings", !hasFeature("repertoire")],
    ["progress", "Progress", !hasFeature("progress")], ["knowledge", "Knowledge bundle"]]);
  const body = el("div");
  main.replaceChildren(head, tabs, body);
  if (sub === "openings") return openings(body, a);
  if (sub === "progress") return progress(body, a);
  if (sub === "knowledge") return knowledge(body, a);
  overview(body, a, p);
}

function overview(body, a, p) {
  const d = p.dna;
  const dnaHost = el("div");
  renderDNA(dnaHost, d, { annotate: true });
  const share = el("button", { class: "btn quiet small", type: "button", text: "Share my Rating DNA", onclick: () => openShare(d, a) });
  const findings = el("ol", { class: "findings" }, ...(p.insights.length ? p.insights.map((i) =>
    el("li", { class: i.severity }, el("b", { class: "stat figure", text: i.stat }), el("span", { text: i.text })))
    : [el("li", {}, el("span"), el("span", { class: "muted", text: "Not enough games for findings yet. Sync more to see them." }))]));
  const note = el("p", { class: "small muted", text: `Each skill shifts your ${d.base_rating} by how accurately you play that kind of position, ` +
    `from ${d.moves} moves played while the game was still undecided.${d.rating_is_estimated ? " Your games had no ratings, so 1500 stands in." : ""}` });
  const games = table([
    ["Date", (g) => g.played_at, "num"],
    ["Opponent", (g) => g.opponent],
    ["Result", (g) => el("span", { class: g.score === 1 ? "res-w" : g.score === 0 ? "res-l" : "", text: g.score === 1 ? "Won" : g.score === 0 ? "Lost" : "Draw" })],
    ["Type", (g) => g.time_class || "–"],
    ["Accuracy", (g) => `${g.accuracy}%`, "num"],
    ["Opening", (g) => g.opening],
  ], p.recent_games, { onRow: (g) => openGame(g.game_id) });
  body.replaceChildren(
    el("div", { class: "grid-2" },
      el("section", { class: "sheet stack", "aria-labelledby": "dnaH" }, el("div", { class: "spread" }, el("h2", { id: "dnaH", text: "Rating DNA" }), share), dnaHost, note),
      el("section", { class: "sheet", "aria-labelledby": "fH" }, el("h2", { id: "fH", text: "What's costing you", style: { marginBottom: "10px" } }), findings)),
    el("section", { class: "sheet", style: { marginTop: "24px" }, "aria-labelledby": "gH" },
      el("div", { class: "section-head" }, el("h2", { id: "gH", text: "Recent games" }), el("span", { class: "small muted", text: "Open a game to replay it with your mistakes marked." })),
      games));
}

async function openShare(dna, a) {
  const img = $("#shareImg"), dl = $("#shareDl"), btn = $("#shareBtn");
  img.removeAttribute("src");
  openDialog($("#shareDlg"));
  const blob = await shareCard(dna, a.handle, PLATFORM[a.platform]);
  const url = URL.createObjectURL(blob);
  img.src = url;
  dl.href = url;
  const file = new File([blob], "rating-dna.png", { type: "image/png" });
  const canShare = navigator.canShare && navigator.canShare({ files: [file] });
  btn.hidden = !canShare;
  btn.onclick = async () => {
    try { await navigator.share({ files: [file], title: "My Rating DNA", text: `My chess skills, rated: ${location.origin}` }); track("DNA shared"); }
    catch { /* cancelled */ }
  };
  dl.onclick = () => track("DNA downloaded");
}

// ---------------- openings (Pro) ----------------
async function openings(body, a) {
  if (!hasFeature("repertoire")) return teaser(body, a, "openings");
  body.replaceChildren(el("p", { class: "muted", text: "Building your opening tree…" }));
  try {
    const r = await api(`/api/accounts/${a.id}/repertoire`);
    const leakRows = r.leaks.map((l) => el("div", { class: "prep-row" },
      el("div", {}, el("span", { class: "line", text: `${l.color === "white" ? "As White" : "As Black"}: ${l.line}` }),
        el("p", { class: "small muted", text: `${l.games} games, you score ${l.score_pct}%, evaluation after the opening ${l.avg_eval_after_opening ?? "–"}` }),
        l.costliest_move ? el("p", { class: "small" }, "Costliest move: ", el("b", { text: l.costliest_move.move }),
          l.costliest_move.times_played > 1 ? ` (played ${l.costliest_move.times_played} times)` : "", l.costliest_move.engine_best ? `; the engine prefers ${l.costliest_move.engine_best}` : "") : null),
      l.costliest_move && l.costliest_move.fen ? el("button", { class: "btn quiet small", type: "button", text: "Show position",
        onclick: () => openPosition({ ...l.costliest_move, you_played: l.costliest_move.move, move: "" }) }) : el("span")));
    const recurring = r.recurring_mistakes.map((m) => el("div", { class: "prep-row" },
      el("div", {}, el("p", {}, el("b", { class: "hand", style: { fontSize: "1.3rem" }, text: `${m.times}×` }), ` you played ${m.you_played} here; the engine prefers ${m.engine_best}.`),
        el("p", { class: "small muted", text: `${m.phase}, against ${m.opponents.join(", ")}. ${m.total_winning_chances_lost}% of winning chances lost in total.` })),
      el("button", { class: "btn quiet small", type: "button", text: "Show position", onclick: () => openPosition({ ...m, move: "", patterns: [m.phase] }) })));
    const tree = (rows, title) => el("section", { class: "sheet" }, el("h3", { text: title, style: { marginBottom: "10px" } }), table([
      ["Line", (x) => x.line, "num"], ["Games", (x) => x.games, "num"],
      ["Score", (x) => el("span", { class: x.score_pct < 45 ? "res-l" : x.score_pct > 60 ? "res-w" : "", text: `${x.score_pct}%` })],
      ["Opening accuracy", (x) => (x.opening_accuracy != null ? `${x.opening_accuracy}%` : "–"), "num"],
      ["Eval after the opening", (x) => (x.avg_eval_after_opening != null ? x.avg_eval_after_opening.toFixed(2) : "–"), "num"]], rows));
    body.replaceChildren(el("div", { class: "stack-lg" },
      el("section", { class: "sheet" }, el("h2", { text: "Leaks" }), el("p", { class: "small muted", text: "Lines you've played 3 or more times that score under 45% or leave the opening worse." }),
        ...(leakRows.length ? leakRows : [el("p", { class: "muted", text: "No leaks found. Nice." })])),
      el("section", { class: "sheet" }, el("h2", { text: "Mistakes you repeat" }), el("p", { class: "small muted", text: "The same wrong move in the same position, more than once." }),
        ...(recurring.length ? recurring : [el("p", { class: "muted", text: "None yet." })])),
      el("div", { class: "grid-2 even" }, tree(r.white, "Your openings as White"), tree(r.black, "Your openings as Black"))));
  } catch (e) { body.replaceChildren(el("p", { class: "err", text: e.message })); }
}

// ---------------- progress (Pro) ----------------
async function progress(body, a) {
  if (!hasFeature("progress")) return teaser(body, a, "progress");
  try {
    const r = await api(`/api/accounts/${a.id}/progress`);
    if (!r.buckets.length) { body.replaceChildren(el("div", { class: "empty" }, el("p", { text: r.note || "Not enough dated games yet." }))); return; }
    const accs = r.buckets.map((b) => b.accuracy), bl = r.buckets.map((b) => b.blunders_per_100);
    const chart = lineChart(r.buckets.map((b) => ({ label: b.period.replace(/^\d{4}-/, ""), a: b.accuracy, b: b.blunders_per_100 })), {
      label: "Accuracy and blunders per 100 moves over time",
      leftRange: niceRange(Math.min(...accs) - 1, Math.max(...accs) + 1), rightRange: niceRange(0, Math.max(1, Math.max(...bl) * 1.15), 0.5) });
    const t = r.trend;
    const delta = (v, good, unit = "") => (v == null ? el("b", { class: "figure", text: "–" })
      : el("b", { class: `figure ${v === 0 ? "" : (v > 0) === (good === "up") ? "res-w" : "res-l"}`, text: `${v > 0 ? "+" : ""}${v}${unit}` }));
    const skill = (k) => ({ openings: "Openings", middlegame: "Middlegame", endgames: "Endgames", tactics: "Tactics", time: "Time", converting: "Converting", defending: "Defending" }[k] || "–");
    body.replaceChildren(el("div", { class: "stack-lg" },
      t ? el("section", { class: "sheet" }, el("h2", { text: `${t.from} to ${t.to}`, style: { marginBottom: "12px" } }), el("div", { class: "kpis" },
        el("div", {}, delta(t.accuracy_change, "up", " pts"), el("span", { text: "accuracy" })),
        el("div", {}, delta(t.blunder_rate_change, "down"), el("span", { text: "blunders per 100 moves" })),
        el("div", {}, delta(t.rating_change, "up"), el("span", { text: "rating" })),
        el("div", {}, el("b", { class: "figure", text: skill(t.most_improved) }), el("span", { text: "most improved" })),
        el("div", {}, el("b", { class: "figure", text: skill(t.most_declined) }), el("span", { text: "needs attention" })))) : null,
      el("section", { class: "sheet stack" }, el("h2", { text: `By ${r.granularity}` }), chart,
        el("div", { class: "legend-ink" }, el("span", {}, el("i", { style: { background: "var(--blue)" } }), "accuracy % (left axis)"),
          el("span", {}, el("i", { style: { background: "var(--red)" } }), "blunders per 100 moves (right axis)"))),
      el("section", { class: "sheet" }, table([["Period", (b) => b.period, "num"], ["Games", (b) => b.games, "num"], ["Score", (b) => `${b.score_pct}%`, "num"],
        ["Accuracy", (b) => `${b.accuracy}%`, "num"], ["Blunders per 100", (b) => b.blunders_per_100, "num"], ["Rating", (b) => b.rating ?? "–", "num"]], r.buckets))));
  } catch (e) { body.replaceChildren(el("p", { class: "err", text: e.message })); }
}

/** An axis range whose 4 gridline steps land on round numbers. */
function niceRange(lo, hi, unit = 1) {
  const step = Math.max(unit, Math.ceil((hi - lo) / 4 / unit) * unit);
  const start = Math.floor(lo / step) * step;
  return [start, start + 4 * step];
}

async function teaser(body, a, which) {
  let t = {};
  try { t = await api(`/api/accounts/${a.id}/teasers`); } catch { /* counts are a bonus */ }
  if (which === "openings") {
    body.replaceChildren(lockCard({ found: t.leaks ?? null, foundLabel: t.leaks === 1 ? "leak found in your openings" : "leaks found in your openings",
      title: "See which opening lines cost you points",
      blurb: `We checked ${t.lines ?? "your"} lines. Pro shows the exact move where each one goes wrong${t.recurring_mistakes ? `, and ${t.recurring_mistakes} mistakes you keep repeating` : ""}.`,
      sample: sampleRows(6) }));
  } else {
    const dir = t.accuracy_trend === "up" ? "Your accuracy is trending up." : t.accuracy_trend === "down" ? "Your accuracy is trending down." : "";
    body.replaceChildren(lockCard({ found: t.progress_periods ?? null, foundLabel: "periods of games to compare",
      title: "Know whether your training is working", blurb: `${dir} Pro tracks accuracy, blunder rate and every skill month by month.`.trim(),
      sample: sampleRows(5) }));
  }
}

// ---------------- knowledge bundle ----------------
async function knowledge(body, a) {
  try {
    const k = await api(`/api/accounts/${a.id}/knowledge`);
    const order = ["Player Profile", "Skill Assessment", "Mistake Pattern", "Repertoire Check", "Opening Line", "Recurring Mistakes", "Progress Report", "Critical Moment", "Game", "Principle"];
    const types = Object.keys(k.types).sort((x, y) => ((order.indexOf(x) + 99) % 99) - ((order.indexOf(y) + 99) % 99));
    body.replaceChildren(el("div", { class: "stack-lg" },
      el("section", { class: "sheet stack" }, el("h2", { text: "Everything the coach knows about your games" }),
        el("p", { class: "muted", text: "Your skills, mistake patterns, critical moments, games and openings, stored as plain markdown files in Google's open OKF format. The coach reads these and links to them. Download them to keep, or use them with any OKF tool." }),
        el("div", { class: "kpis" }, el("div", {}, el("b", { class: "figure", text: String(k.concepts) }), el("span", { text: "concepts" })),
          el("div", {}, el("b", { class: "figure", text: String(k.trust["machine-confirmed"] || 0) }), el("span", { text: "checked by Stockfish" })),
          el("div", {}, el("b", { class: "figure", text: k.conformance_problems.length ? String(k.conformance_problems.length) : "Valid" }), el("span", { text: "OKF format check" }))),
        el("div", { class: "row" }, el("a", { class: "btn quiet", href: `/api/accounts/${a.id}/bundle.zip`, download: true, text: "Download the bundle (.zip)" })),
        k.hidden_pro_concepts ? el("p", { class: "small muted", text: `${k.hidden_pro_concepts} Pro concepts (openings, progress, prep) are hidden on the Free plan.` }) : null),
      ...types.map((t) => el("details", { class: "sheet", open: ["Player Profile", "Skill Assessment", "Mistake Pattern"].includes(t) || null },
        el("summary", {}, el("b", { text: t }), el("span", { class: "muted", text: ` (${k.types[t].length})` })),
        el("ul", { style: { margin: "12px 0 0", paddingLeft: "18px", display: "grid", gap: "6px" } }, ...k.types[t].slice(0, 200).map((c) => el("li", {},
          el("button", { class: "cite", type: "button", text: c.title, onclick: () => openConcept(c.id) }),
          el("span", { class: "small muted", text: ` ${c.description || ""}` }))))))));
  } catch (e) { body.replaceChildren(el("p", { class: "err", text: e.message })); }
}
