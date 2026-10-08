// Pieces several views share: the game viewer (board + scoresheet), the position and concept
// dialogs, locked-Pro previews, small tables and the progress chart.

import { $, S, api, el, openDialog, svg, toast, track } from "../core.js";
import { Board, boardSettings, sideToMove, uciArrow } from "../board.js";

// ---------------- game viewer ----------------
export async function openGame(gameId, accountId = S.accountId) {
  const dlg = $("#boardDlg");
  $("#boardTitle").textContent = "Loading game…";
  $("#boardSub").textContent = "";
  $("#boardBody").replaceChildren(el("p", { class: "muted", text: "Fetching the moves…" }));
  openDialog(dlg);
  let g;
  try { g = await api(`/api/accounts/${accountId}/game/${encodeURIComponent(gameId)}`); }
  catch (e) { $("#boardBody").replaceChildren(el("p", { class: "err", text: e.message })); return; }
  track("Game opened");
  const resultWord = g.result === "1/2-1/2" ? "Draw" : (g.result === "1-0") === (g.color === "white") ? "Won" : "Lost";
  $("#boardTitle").textContent = `${g.color === "white" ? "You" : g.opponent} vs ${g.color === "white" ? g.opponent : "you"}`;
  $("#boardSub").textContent = `${resultWord}, ${g.opening}, ${g.played_at}${g.accuracy != null ? `. Your accuracy ${g.accuracy}%` : ""}`;
  const marks = new Map(g.marks.map((m) => [m.ply, m]));
  const boardHost = el("div");
  const board = new Board(boardHost, { fen: g.fens[0], orientation: g.color });
  const note = el("div", { "aria-live": "polite" });
  const sheet = el("div", { class: "scoresheet" });
  const counter = el("span", { class: "num" });
  let idx = 0;
  const myColor = g.color;
  const moveButtons = [];
  const rows = [];
  for (let i = 0; i < g.moves_san.length; i += 2) {
    const cells = [el("td", { text: String(i / 2 + 1) })];
    for (const k of [i, i + 1]) {
      if (k >= g.moves_san.length) { cells.push(el("td")); continue; }
      const mover = k % 2 === 0 ? "white" : "black";
      const m = marks.get(k);
      const b = el("button", { type: "button", class: mover === myColor ? "mine" : "theirs", text: g.moves_san[k],
        "aria-label": `Move ${Math.floor(k / 2) + 1}${mover === "black" ? " black" : ""}: ${g.moves_san[k]}${m ? `, ${m.classification}` : ""}`,
        onclick: () => go(k + 1) });
      if (m) b.append(el("span", { class: "mk", "aria-hidden": "true", text: m.classification === "blunder" ? "??" : m.classification === "mistake" ? "?" : "?!" }));
      moveButtons[k] = b;
      cells.push(el("td", {}, b));
    }
    rows.push(el("tr", {}, ...cells));
  }
  sheet.append(el("table", {}, el("tbody", {}, ...rows)));

  let shown = 0;
  function go(n) {
    shown = idx;
    idx = Math.max(0, Math.min(g.moves_san.length, n));
    const k = idx - 1; // the move just played
    const m = k >= 0 ? marks.get(k) : null;
    if (m) {
      // A mistake of yours: show the position before it, your move in blue and the engine's in red.
      board.set({ fen: g.fens[k], lastMove: k > 0 ? g.ucis[k - 1] : null, arrows: [uciArrow(g.ucis[k], "you"), uciArrow(m.best_uci, "coach")].filter(Boolean), marks: {} });
      note.replaceChildren(el("div", { class: "mark-note" }, el("b", { text: `${cap(m.classification)}: ${m.played}` }),
        ` cost ${m.win_loss}% of your winning chances. The engine preferred `, el("b", { text: m.best || "another move" }),
        m.line && m.line.length > 1 ? `, with ${m.line.join(" ")}` : "", "."));
    } else {
      const step = Math.abs(idx - shown) === 1; // one move: slide the piece and play its sound
      board.set({ fen: g.fens[idx], lastMove: k >= 0 ? g.ucis[k] : null, arrows: [], marks: {}, animate: step, sound: step && idx > shown });
      note.replaceChildren();
    }
    moveButtons.forEach((b, i) => b && b.setAttribute("aria-current", String(i === k)));
    if (moveButtons[k]) { // scroll the scoresheet only, never the dialog (which would hide the board on phones)
      const row = moveButtons[k].closest("tr");
      if (row.offsetTop < sheet.scrollTop || row.offsetTop + row.offsetHeight > sheet.scrollTop + sheet.clientHeight) {
        sheet.scrollTop = row.offsetTop - sheet.clientHeight / 2;
      }
    }
    counter.textContent = idx ? `Move ${Math.ceil(idx / 2)}` : "Start";
  }
  const nav = (label, fn, text) => el("button", { class: "btn quiet small", type: "button", "aria-label": label, text, onclick: fn });
  const controls = el("div", { class: "board-foot" },
    el("div", { class: "row" }, nav("First move", () => go(0), "⏮"), nav("Previous move", () => go(idx - 1), "◀"),
      nav("Next move", () => go(idx + 1), "▶"), nav("Last move", () => go(g.moves_san.length), "⏭")),
    counter, boardSettings(board));
  const jump = g.marks.filter((m) => (m.ply % 2 === 0 ? "white" : "black") === myColor).sort((a, b) => b.win_loss - a.win_loss)[0];
  const side = el("div", { class: "stack" },
    el("p", { class: "small muted" }, "Your moves are in blue ink. ", el("span", { class: "hand", style: { fontSize: "1.15rem", margin: "0 3px 0 2px" }, text: "?" }),
      "\u00a0marks a mistake; click it to see the engine's move."),
    sheet, note,
    jump ? el("button", { class: "btn quiet small", type: "button", text: "Jump to my costliest mistake", onclick: () => go(jump.ply + 1) }) : null,
    g.url ? el("a", { class: "small", href: g.url, target: "_blank", rel: "noopener", text: "Open on the original site" }) : null);
  $("#boardBody").replaceChildren(el("div", { class: "viewer" }, el("div", {}, boardHost, controls), side));
  dlg.onkeydown = (e) => {
    if (e.target.closest("input, textarea")) return;
    if (e.key === "ArrowRight") { go(idx + 1); e.preventDefault(); }
    if (e.key === "ArrowLeft") { go(idx - 1); e.preventDefault(); }
  };
  go(jump ? jump.ply + 1 : 0);
}
const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);

// ---------------- a single position (coach citations, prep rows, leaks) ----------------
export function openPosition(c, { title } = {}) {
  const dlg = $("#boardDlg");
  $("#boardTitle").textContent = title || (c.opponent ? `Move ${c.move} vs ${c.opponent}` : c.move ? `Move ${c.move}` : "Position");
  $("#boardSub").textContent = [c.opening, c.date].filter(Boolean).join(", ");
  const host = el("div");
  new Board(host, { fen: c.fen, orientation: c.orientation || sideToMove(c.fen),
    arrows: [uciArrow(c.played_uci, "you"), uciArrow(c.best_uci, "coach")].filter(Boolean) });
  const rows = [["You played", c.you_played], ["Engine's move", c.engine_best], ["Engine line", (c.engine_line || []).join(" ")],
    ["Evaluation", c.eval_before && c.eval_after ? `${c.eval_before} to ${c.eval_after}` : null], ["Phase", c.phase],
    ["Patterns", (c.patterns || []).join(", ").replace(/_/g, " ")], ["Clock", c.clock_seconds != null ? `${Math.round(c.clock_seconds)} seconds left` : null]]
    .filter(([, v]) => v);
  const table = el("table", {}, el("tbody", {}, ...rows.map(([k, v]) => el("tr", {}, el("th", { text: k, scope: "row" }), el("td", { text: String(v) })))));
  $("#boardBody").replaceChildren(el("div", { class: "viewer" }, host, el("div", { class: "stack" },
    (c.played_uci || c.best_uci) ? el("div", { class: "legend-ink" }, el("span", {}, el("i", { style: { background: "#2b44c9" } }), "you played"),
      el("span", {}, el("i", { style: { background: "#c9372c" } }), "the engine's move")) : null,
    table, c.game_url ? el("a", { class: "small", href: c.game_url, target: "_blank", rel: "noopener", text: "Open the game on the original site" }) : null)));
  openDialog(dlg);
}

// ---------------- OKF concepts ----------------
export async function openConcept(id) {
  try {
    const c = await api(`/api/accounts/${S.accountId}/concept?id=${encodeURIComponent(id)}`);
    if (c.type === "Critical Moment" && c.frontmatter.fen) {
      const f = c.frontmatter;
      openPosition({ ...f, you_played: f.played, engine_best: f.best, engine_line: f.line, game_url: f.resource });
      return;
    }
    $("#conceptType").textContent = `${c.type}, ${c.trust}`;
    $("#conceptTitle").textContent = c.title;
    S.conceptId = c.id;
    $("#conceptBody").replaceChildren(...(c.description ? [el("p", { class: "muted", text: c.description })] : []), ...renderMd(c.body));
    openDialog($("#conceptDlg"));
  } catch (e) { toast(e.message); }
}

function inlineMd(text) {
  const frag = document.createDocumentFragment();
  const re = /\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)\s]+)\)/g;
  let last = 0;
  for (const m of text.matchAll(re)) {
    frag.append(text.slice(last, m.index));
    if (m[1]) frag.append(el("b", { text: m[1] }));
    else if (m[2]) frag.append(el("code", { text: m[2] }));
    else if (/^https?:\/\//.test(m[4])) frag.append(el("a", { href: m[4], target: "_blank", rel: "noopener", text: m[3] }));
    else frag.append(el("button", { class: "cite", type: "button", text: m[3], onclick: () => openConcept(resolveLink(S.conceptId, m[4])) }));
    last = m.index + m[0].length;
  }
  frag.append(text.slice(last));
  return frag;
}
function resolveLink(fromId, target) {
  target = target.split("#")[0];
  let parts;
  if (target.startsWith("/")) parts = target.split("/").filter(Boolean);
  else {
    parts = (fromId || "/").split("/").filter(Boolean); parts.pop();
    for (const seg of target.split("/")) { if (seg === "..") parts.pop(); else if (seg && seg !== ".") parts.push(seg); }
  }
  let id = "/" + parts.join("/");
  if (id.endsWith("/index.md")) id = id.slice(0, -9);
  return id.replace(/\.md$/, "");
}
function renderMd(body) {
  const out = []; const lines = body.split("\n"); let i = 0;
  while (i < lines.length) {
    const ln = lines[i];
    if (/^#{1,6}\s/.test(ln)) { out.push(el("h4", { text: ln.replace(/^#+\s/, "") })); i++; continue; }
    if (/^\s*[*-]\s/.test(ln)) {
      const ul = el("ul");
      while (i < lines.length && /^\s*[*-]\s/.test(lines[i])) { ul.append(el("li", {}, inlineMd(lines[i].replace(/^\s*[*-]\s/, "")))); i++; }
      out.push(ul); continue;
    }
    if (/^\|/.test(ln)) {
      const rows = [];
      while (i < lines.length && /^\|/.test(lines[i])) { if (!/^\|[\s:|-]+\|$/.test(lines[i])) rows.push(lines[i]); i++; }
      const cells = (r) => r.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const [head, ...rest] = rows;
      out.push(el("div", { class: "scroll" }, el("table", {}, el("thead", {}, el("tr", {}, ...cells(head).map((c) => el("th", {}, inlineMd(c))))),
        el("tbody", {}, ...rest.map((r) => el("tr", {}, ...cells(r).map((c) => el("td", {}, inlineMd(c)))))))));
      continue;
    }
    if (ln.trim()) out.push(el("p", {}, inlineMd(ln)));
    i++;
  }
  return out;
}

// ---------------- locked Pro previews ----------------
/** A blurred sample with a real count from the player's own data on top. */
export function lockCard({ found, foundLabel, title, blurb, sample }) {
  return el("div", { class: "sheet lock-card" },
    el("div", { class: "blurred", "aria-hidden": "true" }, sample),
    el("div", { class: "over" }, el("div", {},
      found != null ? el("b", { class: "found figure", text: String(found) }) : null,
      found != null ? el("p", { class: "small muted", text: foundLabel }) : null,
      el("h3", { text: title }), el("p", { class: "small muted", text: blurb }),
      el("a", { class: "btn", href: "#/plans", text: "See Pro and the Event Pass" }))));
}
export function sampleRows(n = 5) {
  return el("div", { class: "stack" }, ...Array.from({ length: n }, (_, i) => el("div", { class: "prep-row" },
    el("span", { class: "line", text: ["1.e4 c5 2.Nf3 d6 3.d4", "1.d4 Nf6 2.c4 e6 3.Nc3", "1.e4 e5 2.Nf3 Nc6 3.Bb5 a6", "1.c4 e5 2.Nc3 Nf6", "1.e4 e6 2.d4 d5 3.Nd2"][i % 5] }),
    el("span", { class: "tag red", text: `${30 + i * 7}%` }))));
}

export function table(cols, rows, { onRow } = {}) {
  return el("div", { class: "scroll" }, el("table", {},
    el("thead", {}, el("tr", {}, ...cols.map((c) => el("th", { text: c[0], scope: "col" })))),
    el("tbody", {}, ...rows.map((r) => {
      const tr = el("tr", { class: onRow ? "clickable" : null }, ...cols.map((c) => {
        const v = c[1](r);
        return el("td", { class: c[2] || null }, v instanceof Node ? v : String(v ?? "–"));
      }));
      if (onRow) {
        tr.tabIndex = 0;
        tr.addEventListener("click", () => onRow(r));
        tr.addEventListener("keydown", (e) => { if (e.key === "Enter") onRow(r); });
      }
      return tr;
    }))));
}

export function lineChart(points, { label, leftRange, rightRange }) {
  const W = 680, H = 230, L = 44, R = 46, T = 14, B = 32;
  const xs = (i) => L + (points.length === 1 ? (W - L - R) / 2 : (i * (W - L - R)) / (points.length - 1));
  const [a0, a1] = leftRange, [b0, b1] = rightRange;
  const ya = (v) => T + (H - T - B) * (1 - (v - a0) / (a1 - a0 || 1));
  const yb = (v) => T + (H - T - B) * (1 - (v - b0) / (b1 - b0 || 1));
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img", "aria-label": label });
  for (let k = 0; k <= 4; k++) {
    const y = T + ((H - T - B) * k) / 4;
    root.append(svg("line", { x1: L, x2: W - R, y1: y, y2: y, class: "grid" }),
      svg("text", { x: L - 8, y: y + 4, "text-anchor": "end", text: (a1 - ((a1 - a0) * k) / 4).toFixed(0) }),
      svg("text", { x: W - R + 8, y: y + 4, text: String(+(b1 - ((b1 - b0) * k) / 4).toFixed(2)) }));
  }
  const step = Math.ceil(points.length / 10);
  points.forEach((p, i) => { if (i % step === 0) root.append(svg("text", { x: xs(i), y: H - 10, "text-anchor": "middle", text: p.label })); });
  root.append(svg("path", { d: points.map((p, i) => `${i ? "L" : "M"}${xs(i)},${ya(p.a)}`).join(""), class: "s1" }),
    svg("path", { d: points.map((p, i) => `${i ? "L" : "M"}${xs(i)},${yb(p.b)}`).join(""), class: "s2" }));
  points.forEach((p, i) => root.append(svg("circle", { cx: xs(i), cy: ya(p.a), r: 3.5, class: "d1" }, svg("title", { text: `${p.label}: ${p.a}% accuracy` })),
    svg("circle", { cx: xs(i), cy: yb(p.b), r: 3, class: "d2" }, svg("title", { text: `${p.label}: ${p.b} blunders per 100 moves` }))));
  return root;
}

export function areaTabs(area, current, items) {
  return el("nav", { class: "tabs", "aria-label": "Sections" }, ...items.map(([key, label, pro]) =>
    el("a", { href: `#/${area}${key ? `/${key}` : ""}`, "aria-current": (current || "") === key ? "page" : null },
      label, pro ? el("span", { class: "tag pro", text: "Pro" }) : null)));
}
