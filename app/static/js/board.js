// An SVG chessboard: pieces, highlights, arrows (blue ink = you, red pen = coach/engine),
// click-to-move and drag-to-move. Move legality is checked by the server, never here.

import { svg } from "./core.js";

const FILES = "abcdefgh";
const GLYPH = { k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟" };
const VS = "︎"; // text presentation, never emoji
const SQ = 100;
let uid = 0;

export function parseFen(fen) {
  const map = {};
  (fen || "").split(" ")[0].split("/").forEach((row, r) => {
    let f = 0;
    for (const ch of row) {
      if (/\d/.test(ch)) f += Number(ch);
      else { map[FILES[f] + (8 - r)] = ch; f++; }
    }
  });
  return map;
}
export const sideToMove = (fen) => ((fen || "").split(" ")[1] === "b" ? "black" : "white");

export class Board {
  /**
   * @param {HTMLElement} host
   * @param {object} o  fen, orientation ("white"|"black"), coords (bool), interactive (bool),
   *                    arrows [{from,to,kind:"you"|"coach"}], marks {sq: "you"|"coach"|"last"|"sel"},
   *                    onMove(from, to), label (accessible name)
   */
  constructor(host, o = {}) {
    this.host = host;
    this.o = { fen: "8/8/8/8/8/8/8/8 w - - 0 1", orientation: "white", coords: true, interactive: false, arrows: [], marks: {}, ...o };
    this.id = `b${++uid}`;
    this.selected = null;
    host.classList.add("board");
    this.render();
    if (this.o.interactive) this.bindPointer();
  }

  set(o) {
    Object.assign(this.o, o);
    if ("fen" in o) this.selected = null;
    this.render();
  }

  xy(sq) {
    const f = FILES.indexOf(sq[0]);
    const r = Number(sq[1]) - 1;
    const flip = this.o.orientation === "black";
    return { x: (flip ? 7 - f : f) * SQ, y: (flip ? r : 7 - r) * SQ };
  }

  sqAt(px, py) {
    const flip = this.o.orientation === "black";
    let c = Math.floor(px / SQ), r = Math.floor(py / SQ);
    if (c < 0 || c > 7 || r < 0 || r > 7) return null;
    if (flip) { c = 7 - c; r = 7 - r; }
    return FILES[c] + (8 - r);
  }

  render() {
    const o = this.o;
    const pieces = parseFen(o.fen);
    this.pieces = pieces;
    const turn = sideToMove(o.fen);
    const root = svg("svg", { viewBox: "0 0 800 800", role: "img",
      "aria-label": o.label || `Chess position, ${turn} to move` });
    const defs = svg("defs");
    for (const kind of ["you", "coach"]) {
      defs.append(svg("marker", { id: `${this.id}-${kind}`, viewBox: "0 0 10 10", refX: "5", refY: "5", markerWidth: "2.6", markerHeight: "2.6", orient: "auto-start-reverse" },
        svg("path", { d: "M0 0 L10 5 L0 10 z", fill: kind === "you" ? "#2b44c9" : "#c9372c" })));
    }
    root.append(defs);

    for (let r = 0; r < 8; r++) {
      for (let f = 0; f < 8; f++) {
        root.append(svg("rect", { x: f * SQ, y: r * SQ, width: SQ, height: SQ, class: (r + f) % 2 === 0 ? "sq-l" : "sq-d" }));
      }
    }
    const marks = { ...(o.marks || {}) };
    if (this.selected) marks[this.selected] = "sel";
    for (const [sq, kind] of Object.entries(marks)) {
      if (!/^[a-h][1-8]$/.test(sq)) continue;
      const { x, y } = this.xy(sq);
      root.append(svg("rect", { x, y, width: SQ, height: SQ, class: `hl-${kind}` }));
    }
    if (o.coords) {
      const flip = o.orientation === "black";
      for (let i = 0; i < 8; i++) {
        const file = flip ? FILES[7 - i] : FILES[i];
        const rank = flip ? i + 1 : 8 - i;
        root.append(svg("text", { x: i * SQ + 90, y: 790, "text-anchor": "end", class: `coord ${(7 + i) % 2 === 0 ? "on-l" : "on-d"}`, text: file }));
        root.append(svg("text", { x: 6, y: i * SQ + 18, class: `coord ${i % 2 === 0 ? "on-l" : "on-d"}`, text: String(rank) }));
      }
    }
    const pieceLayer = svg("g");
    for (const [sq, p] of Object.entries(pieces)) {
      const { x, y } = this.xy(sq);
      const white = p === p.toUpperCase();
      const mine = o.interactive && (white ? "white" : "black") === turn;
      const t = svg("text", { x: x + SQ / 2, y: y + SQ / 2 + 4, class: `pc ${white ? "w" : "b"}${mine ? " mine" : ""}`, "data-sq": sq,
        text: GLYPH[p.toLowerCase()] + VS });
      pieceLayer.append(t);
    }
    root.append(pieceLayer);
    for (const a of o.arrows || []) {
      if (!a || !a.from || !a.to || a.from === a.to) continue;
      const p1 = this.xy(a.from), p2 = this.xy(a.to);
      const x1 = p1.x + SQ / 2, y1 = p1.y + SQ / 2, x2 = p2.x + SQ / 2, y2 = p2.y + SQ / 2;
      const len = Math.hypot(x2 - x1, y2 - y1);
      const k = (len - 34) / len;
      root.append(svg("line", { x1, y1, x2: x1 + (x2 - x1) * k, y2: y1 + (y2 - y1) * k, class: `arrow ${a.kind || "coach"}`,
        "stroke-width": a.width || 15, "marker-end": `url(#${this.id}-${a.kind || "coach"})` }));
    }
    this.svgEl = root;
    this.host.replaceChildren(root);
  }

  toSvg(e) {
    const pt = this.svgEl.createSVGPoint();
    pt.x = e.clientX; pt.y = e.clientY;
    return pt.matrixTransform(this.svgEl.getScreenCTM().inverse());
  }

  bindPointer() {
    let drag = null;
    const turn = () => sideToMove(this.o.fen);
    const isMine = (sq) => {
      const p = this.pieces[sq];
      return p && ((p === p.toUpperCase() ? "white" : "black") === turn());
    };
    this.host.addEventListener("pointerdown", (e) => {
      if (!this.o.interactive || this.o.locked) return;
      const { x, y } = this.toSvg(e);
      const sq = this.sqAt(x, y);
      if (!sq) return;
      if (this.selected && !isMine(sq)) { // second click: try the move
        const from = this.selected;
        this.selected = null;
        this.render();
        this.o.onMove && this.o.onMove(from, sq);
        return;
      }
      if (!isMine(sq)) return;
      e.preventDefault();
      this.selected = sq;
      this.render();
      const node = this.svgEl.querySelector(`[data-sq="${sq}"]`);
      if (node) {
        const ghost = node.cloneNode(true);
        ghost.classList.add("ghost");
        node.style.opacity = ".35";
        this.svgEl.append(ghost);
        drag = { from: sq, ghost, moved: false };
        this.host.setPointerCapture(e.pointerId);
      }
    });
    this.host.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const { x, y } = this.toSvg(e);
      drag.moved = true;
      drag.ghost.setAttribute("x", x);
      drag.ghost.setAttribute("y", y + 4);
    });
    const finish = (e) => {
      if (!drag) return;
      const d = drag;
      drag = null;
      const { x, y } = this.toSvg(e);
      const to = this.sqAt(x, y);
      if (d.moved && to && to !== d.from) {
        this.selected = null;
        this.render();
        this.o.onMove && this.o.onMove(d.from, to);
      } else {
        this.render(); // a click: keep the piece selected for click-to-move
      }
    };
    this.host.addEventListener("pointerup", finish);
    this.host.addEventListener("pointercancel", () => { drag = null; this.render(); });
  }
}

/** A small static board, e.g. beside a coach answer. */
export function miniBoard(fen, { orientation, arrows = [], marks = {}, label, large = false } = {}) {
  const host = document.createElement("div");
  host.className = large ? "mini large" : "mini";
  new Board(host, { fen, orientation: orientation || sideToMove(fen), coords: false, arrows, marks, label });
  return host;
}

export const uciArrow = (uci, kind) => (uci && uci.length >= 4 ? { from: uci.slice(0, 2), to: uci.slice(2, 4), kind } : null);
export const uciMarks = (uci, kind) => (uci && uci.length >= 4 ? { [uci.slice(0, 2)]: kind, [uci.slice(2, 4)]: kind } : {});
