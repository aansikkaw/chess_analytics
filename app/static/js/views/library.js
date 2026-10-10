// Opponent Library: a ChessBase-style database of the players you prepare against.
// Left: your saved players (tick several to study them together). Right: Explorer (position search on a
// real board), Games (filter, open, export PGN) and the selected player's page (report, notes, tags).

import { S, account, api, del, el, fill, hasFeature, patch, post, showErr, toast, track, upload } from "../core.js";
import { Board, applyMove, boardSettings } from "../board.js";
import { lockCard, openGame, sampleRows, table } from "./common.js";
import { scoutReport } from "./prepare.js";

const START = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1";
const PLAT = { lichess: "Lichess", chesscom: "Chess.com", pgn: "PGN" };
const CLASSES = ["bullet", "blitz", "rapid", "classical"];

// Kept while you move between tabs, so the board and your selection survive.
const L = { data: null, selected: new Set(), tab: "explore", open: null, filters: {}, startFen: null, path: [], query: "", tag: "", page: 0 };

const since = (ts) => {
  if (!ts) return "never";
  const m = Math.round((Date.now() / 1000 - ts) / 60);
  return m < 2 ? "just now" : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} days ago`;
};
const resultTag = (score) => el("span", { class: `tag ${score === 1 ? "ok" : score === 0 ? "red" : ""}`, text: score === 1 ? "Won" : score === 0 ? "Lost" : "Draw" });

export async function render(body) {
  if (!hasFeature("scout")) {
    body.replaceChildren(lockCard({ found: null, title: "Your own Mega Database of opponents",
      blurb: "Save every player you prepare against. Their games update with one click, and you can search positions across all of them, ChessBase-style.",
      sample: sampleRows(6) }));
    return;
  }
  body.replaceChildren(el("p", { class: "muted", text: "Opening your library…" }));
  try { L.data = await api("/api/library"); }
  catch (e) { body.replaceChildren(el("p", { class: "err", text: e.message })); return; }
  const ids = new Set(L.data.players.map((p) => p.id));
  L.selected = new Set([...L.selected].filter((i) => ids.has(i)));
  if (!L.selected.size && L.data.players.length) L.data.players.forEach((p) => L.selected.add(p.id));
  if (L.open && !ids.has(L.open)) L.open = null;
  draw(body);
}

function draw(body) {
  const side = el("aside", { class: "lib-side sheet" });
  const main = el("div", { class: "lib-main" });
  body.replaceChildren(el("div", { class: "stack-lg" }, header(body), L.data.players.length
    ? el("div", { class: "lib-layout" }, side, main)
    : el("section", { class: "sheet stack lib-empty" }, el("h3", { text: "Your library is empty" }),
      el("p", { class: "muted", text: "Add a Lichess or Chess.com player above, or upload a ChessBase/PGN file of an over-the-board opponent. Everyone you scout is saved here too." }))));
  if (!L.data.players.length) return;
  drawSide(side, main, body);
  drawMain(main, body);
}

// ---------------- header: add a player, upload PGN ----------------
function header(body) {
  const platforms = (S.config && S.config.platforms) || ["lichess"];
  const plat = el("select", { id: "libPlat" }, ...platforms.map((p) => el("option", { value: p, text: PLAT[p] })));
  const handle = el("input", { type: "text", id: "libHandle", autocomplete: "off", autocapitalize: "off", spellcheck: "false", maxlength: "30" });
  const cap = (S.me.limits && S.me.limits.max_games_per_sync) || 200;
  const games = el("select", { id: "libGames" }, ...[100, 200, 500].filter((n) => n <= cap).map((n) => el("option", { value: n, text: `${n} games`, selected: n === 200 })));
  const err = el("p", { class: "err", role: "alert", hidden: true });
  const btn = el("button", { class: "btn", type: "submit", text: "Add player" });
  const form = el("form", { class: "form-row" },
    el("div", { class: "field narrow" }, el("label", { for: "libPlat", text: "Site" }), plat),
    el("div", { class: "field" }, el("label", { for: "libHandle", text: "Username" }), handle),
    el("div", { class: "field narrow" }, el("label", { for: "libGames", text: "Download" }), games), btn);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    showErr(err, "");
    if (!handle.value.trim()) { showErr(err, "Type their username."); return; }
    btn.disabled = true; btn.textContent = "Downloading…";
    try {
      const r = await post("/api/library", { platform: plat.value, handle: handle.value.trim(), max_games: Number(games.value) });
      track("Library add", { platform: plat.value });
      toast(r.downloaded ? `${r.player.handle}: ${r.new_games} games saved.` : `${r.player.handle} was already in the library, so it opened instantly.`);
      L.selected.add(r.player.id); L.open = r.player.id; L.tab = "player";
      await render(body);
    } catch (ex) { showErr(err, ex.message); }
    finally { btn.disabled = false; btn.textContent = "Add player"; }
  });
  const used = L.data.players.length;
  return el("section", { class: "sheet stack" },
    el("div", { class: "row between" }, el("h2", { text: "Opponent Library" }),
      el("span", { class: "small muted", text: `${used} of ${L.data.limit} players · ${L.data.downloads_left} downloads left today` })),
    el("p", { class: "small muted", text: "Players are saved with their games. Opening one is instant and free; Update fetches only games played since the last download." }),
    form, err, uploadBox(body));
}

function uploadBox(body) {
  const file = el("input", { type: "file", id: "libFile", accept: ".pgn,.zip", hidden: true });
  const out = el("div", { class: "stack" });
  const pick = el("button", { class: "btn quiet small", type: "button", text: "Upload a ChessBase / PGN file", onclick: () => file.click() });
  const send = async (name) => {
    out.replaceChildren(el("p", { class: "small muted", text: "Reading the file…" }));
    try {
      const r = await upload("/api/library/upload", { file: file.files[0], name: name || "" });
      if (r.choose) {
        const sel = el("select", { id: "libPgnName" }, ...r.choose.map((x) => el("option", { value: x.name, text: `${x.name} (${x.games} games)` })));
        fill(out, el("div", { class: "form-row" }, el("div", { class: "field" }, el("label", { for: "libPgnName", text: "Whose games are these?" }), sel),
          el("button", { class: "btn", type: "button", text: "Add to library", onclick: () => send(sel.value) })));
        return;
      }
      toast(r.message);
      L.selected.add(r.player.id); L.open = r.player.id; L.tab = "player";
      await render(body);
    } catch (e) { fill(out, el("p", { class: "err", text: e.message })); }
  };
  file.addEventListener("change", () => { if (file.files[0]) send(""); });
  return el("div", { class: "stack" }, el("div", { class: "row" }, pick, file,
    el("span", { class: "tiny faint", text: "For over-the-board opponents. Private to you." })), out);
}

// ---------------- left: saved players ----------------
function drawSide(side, main, body) {
  const q = el("input", { type: "search", placeholder: "Find a player", "aria-label": "Find a player", value: L.query });
  const list = el("ul", { class: "lib-list" });
  const tags = L.data.tags.length ? el("div", { class: "chips small-chips" },
    el("button", { class: `chip${L.tag ? "" : " on"}`, type: "button", text: "All", onclick: () => { L.tag = ""; drawSide(side, main, body); } }),
    ...L.data.tags.map((t) => el("button", { class: `chip${L.tag === t ? " on" : ""}`, type: "button", text: t, onclick: () => { L.tag = t; drawSide(side, main, body); } }))) : null;
  const visible = () => L.data.players.filter((p) => (!L.tag || p.tags.includes(L.tag)) && p.handle.toLowerCase().includes(L.query.toLowerCase()));
  const count = el("span", { class: "tiny faint" });
  const refreshCount = () => { count.textContent = `${L.selected.size} selected`; };
  const rows = () => list.replaceChildren(...visible().map((p) => {
    const box = el("input", { type: "checkbox", "aria-label": `Include ${p.handle}` });
    box.checked = L.selected.has(p.id);
    box.addEventListener("change", () => { box.checked ? L.selected.add(p.id) : L.selected.delete(p.id); refreshCount(); L.page = 0; drawMain(main, body); });
    return el("li", { class: L.open === p.id ? "open" : "" }, box,
      el("button", { class: "lib-name", type: "button", onclick: () => { L.open = p.id; L.tab = "player"; drawSide(side, main, body); drawMain(main, body); } },
        el("b", { text: p.handle }),
        el("span", { class: "tiny faint", text: `${PLAT[p.platform]} · ${p.games} games${p.score_pct != null ? ` · ${p.score_pct}%` : ""}` }),
        p.tags.length ? el("span", { class: "lib-tags" }, ...p.tags.map((t) => el("span", { class: "tag", text: t }))) : null));
  }));
  q.addEventListener("input", () => { L.query = q.value; rows(); });
  const all = el("button", { class: "link quiet small", type: "button", text: "Select all", onclick: () => { visible().forEach((p) => L.selected.add(p.id)); rows(); refreshCount(); drawMain(main, body); } });
  const none = el("button", { class: "link quiet small", type: "button", text: "None", onclick: () => { L.selected.clear(); rows(); refreshCount(); drawMain(main, body); } });
  rows(); refreshCount();
  fill(side, q, tags, el("div", { class: "row between" }, el("div", { class: "row" }, all, none), count), list);
}

// ---------------- right: tabs ----------------
function drawMain(main, body) {
  const tabs = [["explore", "Explorer"], ["games", "Games"], ["player", L.open ? playerName(L.open) : "Player"]];
  const nav = el("nav", { class: "tabs", "aria-label": "Library" }, ...tabs.map(([k, label]) =>
    el("a", { href: "#/prepare/library", "aria-current": L.tab === k ? "page" : null, text: label,
      onclick: (e) => { e.preventDefault(); if (k === "player" && !L.open) { toast("Click a player's name on the left first."); return; } L.tab = k; drawMain(main, body); } })));
  const content = el("div", { class: "stack-lg" });
  fill(main, nav, L.tab === "player" ? null : filtersBox(() => drawMain(main, body)), content);
  if (L.tab === "explore") explorer(content);
  else if (L.tab === "games") gamesTab(content);
  else playerTab(content, body);
}
const playerName = (id) => (L.data.players.find((p) => p.id === id) || {}).handle || "Player";

function filtersBox(onApply) {
  const f = L.filters;
  const sel = (id, label, opts, val) => el("div", { class: "field narrow" }, el("label", { for: id, text: label }),
    el("select", { id }, ...opts.map(([v, t]) => el("option", { value: v, text: t, selected: (val || "") === v }))));
  const color = sel("lfColor", "They played", [["", "Either colour"], ["white", "White"], ["black", "Black"]], f.color);
  const result = sel("lfResult", "Their result", [["", "Any result"], ["win", "Won"], ["draw", "Drew"], ["loss", "Lost"]], f.result);
  const boxes = CLASSES.map((c) => { const b = el("input", { type: "checkbox", value: c }); b.checked = (f.time_classes || []).includes(c); return b; });
  const opening = el("input", { type: "text", id: "lfOpening", maxlength: "80", placeholder: "e.g. Sicilian", value: f.opening || "" });
  const opponent = el("input", { type: "text", id: "lfOpp", maxlength: "60", value: f.opponent || "" });
  const from = el("input", { type: "date", id: "lfFrom", value: f.date_from || "" });
  const to = el("input", { type: "date", id: "lfTo", value: f.date_to || "" });
  const active = Object.values(f).filter((v) => (Array.isArray(v) ? v.length : v)).length;
  const apply = (e) => {
    e && e.preventDefault();
    L.filters = { color: color.querySelector("select").value || null, result: result.querySelector("select").value || null,
      time_classes: boxes.filter((b) => b.checked).map((b) => b.value), opening: opening.value.trim() || null,
      opponent: opponent.value.trim() || null, date_from: from.value || null, date_to: to.value || null };
    L.page = 0;
    onApply();
  };
  const form = el("form", { class: "stack" },
    el("div", { class: "form-row" }, color, result,
      el("div", { class: "field" }, el("label", { for: "lfOpening", text: "Opening name" }), opening),
      el("div", { class: "field" }, el("label", { for: "lfOpp", text: "Their opponent" }), opponent)),
    el("div", { class: "form-row" },
      el("fieldset", { class: "field lf-classes" }, el("legend", { text: "Time controls" }),
        el("div", { class: "row" }, ...boxes.map((b) => el("label", { class: "check" }, b, b.value)))),
      el("div", { class: "field narrow" }, el("label", { for: "lfFrom", text: "From" }), from),
      el("div", { class: "field narrow" }, el("label", { for: "lfTo", text: "To" }), to)),
    el("div", { class: "row" }, el("button", { class: "btn small", type: "submit", text: "Apply filters" }),
      el("button", { class: "btn quiet small", type: "button", text: "Clear", onclick: () => { L.filters = {}; L.page = 0; onApply(); } })));
  form.addEventListener("submit", apply);
  const d = el("details", { class: "sheet lib-filters" }, el("summary", { text: active ? `Filters (${active} on)` : "Filters" }), form);
  if (active) d.open = false;
  return d;
}

const query = () => ({ players: [...L.selected], ...Object.fromEntries(Object.entries(L.filters).filter(([, v]) => (Array.isArray(v) ? v.length : v))) });

// ---------------- Explorer: position search on a real board ----------------
function explorer(content) {
  if (!L.selected.size) { content.replaceChildren(el("p", { class: "muted", text: "Tick at least one player on the left." })); return; }
  const boardHost = el("div");
  const curFen = () => (L.path.length ? L.path[L.path.length - 1].fen : L.startFen || START);
  const board = new Board(boardHost, { fen: curFen(), interactive: true, label: "Explorer board",
    lastMove: L.path.length ? L.path[L.path.length - 1].uci : null, onMove: (_f, _t, info) => { L.path.push({ uci: info.uci, san: info.san, fen: info.fen }); refresh(); } });
  const line = el("p", { class: "lib-line" });
  const movesBox = el("div", { class: "stack" });
  const gamesBox = el("div", { class: "stack" });
  const fen = el("input", { type: "text", id: "lfFen", placeholder: "Paste a FEN to start from any position", "aria-label": "FEN" });
  const back = () => { if (L.path.length) { L.path.pop(); board.set({ fen: curFen(), lastMove: L.path.length ? L.path[L.path.length - 1].uci : null }); refresh(); } };
  const reset = () => { L.path = []; L.startFen = null; board.set({ fen: START, lastMove: null }); refresh(); };
  const play = (uci) => {
    const r = applyMove(curFen(), uci);
    if (!r) return;
    L.path.push({ uci: r.uci, san: r.san, fen: r.fen });
    board.set({ fen: r.fen, lastMove: r.uci, sound: true });
    refresh();
  };
  let seq = 0;
  async function refresh() {
    const n = ++seq;
    line.replaceChildren(...lineText());
    fill(movesBox, el("p", { class: "small muted", text: "Searching…" }));
    let r;
    try { r = await post("/api/library/explore", { ...query(), fen: L.startFen, moves: L.path.map((p) => p.uci) }); }
    catch (e) { if (n === seq) fill(movesBox, el("p", { class: "err", text: e.message })); return; }
    if (n !== seq) return;
    const wdl = (m) => el("div", { class: "wdl", title: `White ${m.white_pct}% · Draw ${m.draw_pct}% · Black ${m.black_pct}%` },
      el("i", { class: "w", style: { width: `${m.white_pct}%` }, text: m.white_pct >= 14 ? `${m.white_pct}%` : "" }),
      el("i", { class: "d", style: { width: `${m.draw_pct}%` }, text: m.draw_pct >= 14 ? `${m.draw_pct}%` : "" }),
      el("i", { class: "b", style: { width: `${m.black_pct}%` }, text: m.black_pct >= 14 ? `${m.black_pct}%` : "" }));
    fill(movesBox,
      el("div", { class: "row between" }, el("h3", { text: r.games ? `${r.games} game${r.games === 1 ? "" : "s"} reached this position` : "No games reached this position" }),
        el("span", { class: "tiny faint", text: `${r.turn === "white" ? "White" : "Black"} to move` })),
      r.moves.length ? table([["Move", (m) => el("b", { text: m.san })], ["Games", (m) => m.games, "num"], ["White / Draw / Black", wdl],
        ["Their score", (m) => `${m.their_score_pct}%`, "num"]], r.moves, { onRow: (m) => play(m.uci) })
        : el("p", { class: "small muted", text: r.games ? "The game ended here." : "Try another move order, fewer filters, or more players." }));
    fill(gamesBox, r.list.length ? el("h3", { text: "Games" }) : null,
      r.list.length ? table([["Date", (g) => g.played_at], ["Player", (g) => g.player], ["As", (g) => g.color], ["Opponent", (g) => g.opponent],
        ["Result", (g) => resultTag(g.score)], ["Opening", (g) => g.opening], ["Reached at", (g) => `move ${g.move}`, "num"]], r.list,
      { onRow: (g) => openGame(null, null, `/api/library/games/${g.id}`) }) : null);
  }
  function lineText() {
    if (!L.path.length) return [el("span", { class: "muted", text: L.startFen ? "From your FEN position" : "Starting position" })];
    const startBlack = (L.startFen || START).split(" ")[1] === "b";
    const startNo = Number((L.startFen || START).split(" ")[5]) || 1;
    return L.path.map((p, i) => {
      const ply = i + (startBlack ? 1 : 0);
      const no = startNo + Math.floor(ply / 2);
      const label = ply % 2 === 0 ? `${no}.${p.san}` : i === 0 ? `${no}...${p.san}` : p.san;
      return el("button", { class: "link quiet", type: "button", text: `${label} `, onclick: () => {
        L.path = L.path.slice(0, i + 1); board.set({ fen: curFen(), lastMove: L.path[i].uci }); refresh();
      } });
    });
  }
  const fenForm = el("form", { class: "row" }, fen, el("button", { class: "btn quiet small", type: "submit", text: "Set up" }));
  fenForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const v = fen.value.trim();
    if (!v) return;
    if (!/^([1-8pnbrqkPNBRQK]+\/){7}[1-8pnbrqkPNBRQK]+ [wb] /.test(v)) { toast("That doesn't look like a FEN."); return; }
    L.startFen = v; L.path = []; board.set({ fen: v, lastMove: null }); refresh();
  });
  const nav = (label, fn, text) => el("button", { class: "btn quiet small", type: "button", "aria-label": label, text, onclick: fn });
  fill(content, el("div", { class: "lib-explorer" },
    el("div", { class: "stack" }, boardHost,
      el("div", { class: "board-foot" }, el("div", { class: "row" }, nav("Back to the start", reset, "⏮"), nav("Take back a move", back, "◀")), boardSettings(board)),
      fenForm,
      el("p", { class: "tiny faint", text: "Play moves on the board (either side) or click a row. Positions match by any move order." })),
    el("div", { class: "stack" }, line, movesBox)), gamesBox);
  refresh();
}

// ---------------- Games list ----------------
async function gamesTab(content) {
  if (!L.selected.size) { content.replaceChildren(el("p", { class: "muted", text: "Tick at least one player on the left." })); return; }
  content.replaceChildren(el("p", { class: "muted", text: "Loading games…" }));
  const per = 50;
  let r;
  try { r = await post("/api/library/games", { ...query(), limit: per, offset: L.page * per }); }
  catch (e) { content.replaceChildren(el("p", { class: "err", text: e.message })); return; }
  const qs = new URLSearchParams();
  const q = query();
  qs.set("players", q.players.join(","));
  for (const [k, v] of Object.entries(q)) if (k !== "players") qs.set(k, Array.isArray(v) ? v.join(",") : v);
  const pages = Math.max(1, Math.ceil(r.total / per));
  const pager = el("div", { class: "row" },
    el("button", { class: "btn quiet small", type: "button", text: "◀ Newer", disabled: L.page === 0, onclick: () => { L.page--; gamesTab(content); } }),
    el("span", { class: "small muted", text: `Page ${L.page + 1} of ${pages}` }),
    el("button", { class: "btn quiet small", type: "button", text: "Older ▶", disabled: L.page + 1 >= pages, onclick: () => { L.page++; gamesTab(content); } }));
  fill(content,
    el("div", { class: "row between" }, el("h3", { text: `${r.total} game${r.total === 1 ? "" : "s"}${r.score_pct != null ? ` · they scored ${r.score_pct}%` : ""}` }),
      el("a", { class: "btn quiet small", href: `/api/library/export.pgn?${qs}`, download: "", text: "Export PGN for ChessBase" })),
    r.games.length ? table([["Date", (g) => g.played_at], ["Player", (g) => g.player], ["As", (g) => g.color], ["Opponent", (g) => g.opponent],
      ["Result", (g) => resultTag(g.score)], ["Opening", (g) => g.opening], ["Moves", (g) => g.moves, "num"], ["Time", (g) => g.time_class || "–"]],
    r.games, { onRow: (g) => openGame(null, null, `/api/library/games/${g.id}`) }) : el("p", { class: "muted", text: "No games match these filters." }),
    r.total > per ? pager : null);
}

// ---------------- one player: report, notes, tags ----------------
async function playerTab(content, body) {
  const p = L.data.players.find((x) => x.id === L.open);
  if (!p) { content.replaceChildren(el("p", { class: "muted", text: "Click a player's name on the left." })); return; }
  const notes = el("textarea", { id: "libNotes", maxlength: "4000", rows: "4", placeholder: "What to expect: openings, habits, how you beat them last time…" });
  notes.value = p.notes || "";
  const tags = el("input", { type: "text", id: "libTags", maxlength: "200", placeholder: "e.g. club, round 3", value: p.tags.join(", ") });
  const save = el("button", { class: "btn small", type: "button", text: "Save notes", onclick: async () => {
    try {
      const r = await patch(`/api/library/${p.id}`, { notes: notes.value, tags: tags.value.split(",").map((t) => t.trim()).filter(Boolean) });
      Object.assign(p, r.player); toast("Saved."); await render(body);
    } catch (e) { toast(e.message); }
  } });
  const update = el("button", { class: "btn quiet small", type: "button", text: p.platform === "pgn" ? "Upload a newer file above to add games" : "Update games", disabled: p.platform === "pgn",
    onclick: async () => {
      update.disabled = true; update.textContent = "Checking for new games…";
      try { const r = await post(`/api/library/${p.id}/update`); toast(r.message); await render(body); }
      catch (e) { toast(e.message); update.disabled = false; update.textContent = "Update games"; }
    } });
  const remove = el("button", { class: "btn quiet small danger-text", type: "button", text: "Remove from library", onclick: async (e) => {
    const b = e.currentTarget;
    if (b.dataset.confirm !== "1") { b.dataset.confirm = "1"; b.textContent = "Click again to remove"; return; }
    try { await del(`/api/library/${p.id}`); L.selected.delete(p.id); L.open = null; L.tab = "explore"; toast(`${p.handle} removed.`); await render(body); }
    catch (ex) { toast(ex.message); }
  } });
  const report = el("div", { class: "stack-lg" }, el("p", { class: "muted", text: "Building the report from saved games…" }));
  content.replaceChildren(
    el("section", { class: "sheet stack" },
      el("div", { class: "row between" }, el("div", {}, el("h2", { text: p.handle }),
        el("p", { class: "small muted", text: `${PLAT[p.platform]} · ${p.games} games saved · last game ${p.last_game || "–"} · updated ${since(p.fetched_at)}` })),
        el("div", { class: "row" }, update, remove)),
      el("div", { class: "field" }, el("label", { for: "libNotes", text: "Your notes" }), notes),
      el("div", { class: "form-row" }, el("div", { class: "field" }, el("label", { for: "libTags", text: "Tags (comma-separated)" }), tags), save),
      el("div", { class: "row" },
        el("button", { class: "btn quiet small", type: "button", text: "Explore only this player", onclick: () => { L.selected = new Set([p.id]); L.tab = "explore"; render(body); } }),
        el("button", { class: "btn quiet small", type: "button", text: "Their games", onclick: () => { L.selected = new Set([p.id]); L.tab = "games"; render(body); } }))),
    report);
  const a = account();
  try {
    const r = await api(`/api/library/${p.id}/report?games=500${a && a.games ? `&account_id=${a.id}` : ""}`);
    scoutReport(report, r);
  } catch (e) { fill(report, el("p", { class: "err", text: e.message })); }
}

/** Scout tab helper: the most recently opened saved players, as one-click chips. */
export async function savedChips(onOpen) {
  let d;
  try { d = await api("/api/library"); } catch { return null; }
  if (!d.players.length) return null;
  return el("div", { class: "stack" }, el("p", { class: "small muted", text: "From your library (opens instantly, no download):" }),
    el("div", { class: "chips" }, ...d.players.slice(0, 10).map((p) => el("button", { class: "chip", type: "button", onclick: () => onOpen(p),
      text: `${p.handle} · ${p.games}` })),
    el("a", { class: "chip", href: "#/prepare/library", text: "Open the library →" })));
}
