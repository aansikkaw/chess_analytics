// The command palette (Ctrl/Cmd+K): type a few letters to jump to a game, an opening, a report card or a puzzle
// of the chess account you're looking at, or to any page. ↑ ↓ choose, Enter opens, Esc closes.

import { S, account, api, el, fill } from "./core.js";
import { openGame } from "./views/common.js";

const PAGES = [
  ["Diagnose", "#/diagnose", "Rating DNA, what's costing you, recent games"],
  ["Train: your mistakes", "#/train", "Puzzles from your own games"],
  ["Train: predicted for you", "#/train/predicted", "Puzzles your Twin predicts you'd miss"],
  ["Prepare", "#/prepare", "Prep Check, scouting and the Opponent Library"],
  ["Coach", "#/coach", "Ask about your play"],
  ["Scan a scoresheet", "#/scan", "Over-the-board games from a photo"],
  ["Plans", "#/plans", "Free, Pro, Event Pass, Coach"],
];
const GROUPS = [["page", "Pages"], ["opening", "Openings"], ["game", "Games"], ["report", "Report cards"], ["puzzle", "Puzzles"]];
// Typing a kind's name ("puzzle 12", "report vs anna") puts that kind first.
const KIND_WORDS = { page: ["page", "go"], opening: ["opening", "openings"], game: ["game", "games"], report: ["report", "reports", "card"],
  puzzle: ["puzzle", "puzzles"] };
const cache = new Map(); // account id -> { at, items }

async function items() {
  const out = PAGES.map(([title, href, sub]) => ({ kind: "page", title, sub, go: () => { location.hash = href; } }));
  const a = account();
  if (!a || !a.games) return out;
  const hit = cache.get(a.id);
  if (hit && Date.now() - hit.at < 60_000) return [...out, ...hit.items];
  const d = await api(`/api/accounts/${a.id}/search`);
  const res = (g) => (g.score === 1 ? "won" : g.score === 0 ? "lost" : g.score === 0.5 ? "drew" : g.result);
  const mine = [
    ...d.games.map((g) => ({ kind: "game", title: `vs ${g.opponent}`, sub: [res(g), g.opening, g.time_class, g.played_at].filter(Boolean).join(" · "),
      go: () => openGame(g.game_id, a.id) })),
    ...d.openings.map((o) => ({ kind: "opening", title: o.name, sub: `${o.games} game${o.games === 1 ? "" : "s"} · opens your latest`,
      go: () => openGame(o.latest_game_id, a.id) })),
    ...d.report_cards.map((g) => ({ kind: "report", title: `Report card: vs ${g.opponent}`, sub: [res(g), g.opening, g.played_at].filter(Boolean).join(" · "),
      go: () => { location.hash = `#/report/${a.id}/${encodeURIComponent(g.game_id)}`; } })),
    ...d.puzzles.map((p) => ({ kind: "puzzle", title: `Puzzle: your ${p.move_number}${p.color === "white" ? "." : "…"}${p.played_san}${p.opponent ? ` vs ${p.opponent}` : ""}`,
      sub: [p.due ? "due now" : "not due yet", ...(p.themes || []).slice(0, 2).map((t) => t.replace(/_/g, " "))].join(" · "),
      go: () => { location.hash = `#/train/puzzle/${p.id}`; } })),
  ];
  cache.set(a.id, { at: Date.now(), items: mine });
  return [...out, ...mine];
}

/** Every word of the query must appear in the title or the line under it. A word naming a kind moves that kind first. */
function filter(all, q) {
  const words = q.toLowerCase().split(/\s+/).filter(Boolean);
  const per = words.length ? 6 : 4;
  const isKind = (w) => Object.values(KIND_WORDS).some((ws) => ws.includes(w));
  const named = GROUPS.map(([k]) => k).filter((k) => words.some((w) => KIND_WORDS[k].includes(w)));
  const order = [...named, ...GROUPS.map(([k]) => k).filter((k) => !named.includes(k))];
  const text = words.filter((w) => !isKind(w)); // "game", "puzzle"… choose the order; the rest must match
  const picked = [];
  for (const kind of order) {
    const pool = all.filter((it) => it.kind === kind && text.every((w) => `${it.title} ${it.sub}`.toLowerCase().includes(w)));
    picked.push(...pool.slice(0, per));
  }
  return picked;
}

let dlg = null;
export async function openPalette() {
  if (dlg && dlg.open) { dlg.close(); return; }
  if (!S.me) return;
  const input = el("input", { type: "search", id: "paletteInput", role: "combobox", "aria-expanded": "true", "aria-controls": "paletteList",
    "aria-autocomplete": "list", autocomplete: "off", spellcheck: "false", placeholder: "Search games, openings, report cards, puzzles…" });
  const list = el("ul", { id: "paletteList", role: "listbox", "aria-label": "Results", class: "palette-list" });
  const status = el("p", { class: "tiny faint palette-status", "aria-live": "polite" });
  if (!dlg) { dlg = el("dialog", { class: "palette", "aria-label": "Command palette" }); document.body.append(dlg); }
  fill(dlg, el("div", { class: "palette-head" }, el("label", { class: "sr", for: "paletteInput", text: "Search" }), input), list, status,
    el("p", { class: "tiny faint palette-foot", text: "↑ ↓ to choose · Enter to open · Esc to close" }));
  dlg.showModal();
  input.focus();

  let all = [], shown = [], active = 0;
  const draw = () => {
    shown = filter(all, input.value);
    active = Math.min(active, Math.max(0, shown.length - 1));
    const rows = [];
    let group = null;
    shown.forEach((it, i) => {
      if (it.kind !== group) { group = it.kind; rows.push(el("li", { role: "presentation", class: "palette-group", text: GROUPS.find((g) => g[0] === group)[1] })); }
      rows.push(el("li", { role: "option", id: `pal-${i}`, class: "palette-item", "aria-selected": String(i === active),
        onmousedown: (e) => { e.preventDefault(); choose(i); }, onmousemove: () => { if (active !== i) { active = i; draw(); } } },
      el("span", { class: "palette-title", text: it.title }), el("span", { class: "palette-sub", text: it.sub })));
    });
    list.replaceChildren(...rows);
    if (shown.length) input.setAttribute("aria-activedescendant", `pal-${active}`); else input.removeAttribute("aria-activedescendant");
    const cur = list.querySelector("[aria-selected='true']");
    if (cur) cur.scrollIntoView({ block: "nearest" });
    status.textContent = shown.length ? "" : input.value ? "Nothing matches. Try an opponent's name, an opening or a move." : "";
  };
  const choose = (i) => { const it = shown[i]; if (!it) return; dlg.close(); it.go(); };
  input.addEventListener("input", () => { active = 0; draw(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { active = Math.min(shown.length - 1, active + 1); draw(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { active = Math.max(0, active - 1); draw(); e.preventDefault(); }
    else if (e.key === "Enter") { choose(active); e.preventDefault(); }
    else if (e.key === "Escape") { dlg.close(); e.preventDefault(); } // a search box would only clear itself
  });
  all = PAGES.map(([title, href, sub]) => ({ kind: "page", title, sub, go: () => { location.hash = href; } }));
  draw();
  try { all = await items(); } catch (e) { status.textContent = `Couldn't load your games: ${e.message}`; }
  if (dlg.open) draw();
}
