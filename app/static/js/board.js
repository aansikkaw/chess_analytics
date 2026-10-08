// The chessboard. Real pieces (Chessnut set, Apache-2.0), drag or click to move with legal-move dots,
// last-move and check highlights, animated moves, a promotion picker, move sounds, right-click arrows
// and circles, and colour themes. Rules for the board's own hints come from chess.js (BSD-2-Clause);
// the server still grades every puzzle answer.
//
// Arrows keep their meaning across the app: blue = you, red = coach/engine, green = drawn by hand.

import { Chess } from "../vendor/chess.js";
import { store } from "./core.js";

const FILES = "abcdefgh";
const PIECES = "/static/pieces/chessnut";
const EMPTY = "8/8/8/8/8/8/8/8 w - - 0 1";
export const THEMES = { green: "Green", brown: "Wood", blue: "Ice", slate: "Slate" };
const reduceMotion = () => window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

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
const colorOf = (p) => (p === p.toUpperCase() ? "white" : "black");
const pieceUrl = (p) => `${PIECES}/${colorOf(p) === "white" ? "w" : "b"}${p.toUpperCase()}.svg`;
const isSq = (s) => typeof s === "string" && /^[a-h][1-8]$/.test(s);

// ---- settings ---------------------------------------------------------------------------
export const boardTheme = () => (THEMES[store.get("pb:boardTheme")] ? store.get("pb:boardTheme") : "green");
export function setBoardTheme(t) {
  if (!THEMES[t]) return;
  store.set("pb:boardTheme", t);
  for (const b of document.querySelectorAll(".board")) b.dataset.theme = t;
}
export const soundEnabled = () => store.get("pb:sound") !== false;
export const setSoundEnabled = (on) => store.set("pb:sound", !!on);

// ---- sounds (synthesised, so there are no audio files to load) ------------------------------
let actx = null;
function tone(freq, start, dur, vol, type = "sine") {
  const o = actx.createOscillator(), g = actx.createGain();
  o.type = type;
  o.frequency.value = freq;
  g.gain.setValueAtTime(0.0001, start);
  g.gain.exponentialRampToValueAtTime(vol, start + 0.01);
  g.gain.exponentialRampToValueAtTime(0.0001, start + dur);
  o.connect(g).connect(actx.destination);
  o.start(start);
  o.stop(start + dur + 0.02);
}
function knock(start, { len = 0.06, freq = 1500, vol = 0.6, decay = 3.5 } = {}) {
  const buf = actx.createBuffer(1, Math.ceil(actx.sampleRate * len), actx.sampleRate);
  const d = buf.getChannelData(0);
  for (let i = 0; i < d.length; i++) d[i] = (Math.random() * 2 - 1) * Math.pow(1 - i / d.length, decay);
  const src = actx.createBufferSource();
  src.buffer = buf;
  const f = actx.createBiquadFilter();
  f.type = "bandpass";
  f.frequency.value = freq;
  f.Q.value = 1.1;
  const g = actx.createGain();
  g.gain.value = vol;
  src.connect(f).connect(g).connect(actx.destination);
  src.start(start);
}
export function playSound(kind) {
  if (!soundEnabled()) return;
  try {
    actx = actx || new (window.AudioContext || window.webkitAudioContext)();
    if (actx.state === "suspended") actx.resume();
    const t = actx.currentTime + 0.005;
    if (kind === "move") knock(t);
    else if (kind === "capture") { knock(t, { len: 0.09, freq: 900, vol: 0.9, decay: 2 }); knock(t + 0.035, { len: 0.05, freq: 1700, vol: 0.4 }); }
    else if (kind === "check") { knock(t); tone(880, t + 0.03, 0.14, 0.07); }
    else if (kind === "correct") { tone(660, t, 0.13, 0.09); tone(990, t + 0.09, 0.2, 0.09); }
    else if (kind === "complete") { tone(523, t, 0.12, 0.08); tone(659, t + 0.09, 0.12, 0.08); tone(784, t + 0.18, 0.26, 0.09); }
    else if (kind === "wrong") tone(196, t, 0.25, 0.06, "triangle");
  } catch { /* no audio: fine */ }
}

// ---- legal moves ------------------------------------------------------------------------
/** Legal moves from a FEN, grouped by origin square: Map(from -> [{to, promotion}]). */
export function legalMoves(fen) {
  const out = new Map();
  let game;
  try { game = new Chess(fen); } catch { return out; }
  for (const m of game.moves({ verbose: true })) {
    const list = out.get(m.from) || [];
    if (!list.some((x) => x.to === m.to)) list.push({ to: m.to, promotion: !!m.promotion });
    out.set(m.from, list);
  }
  return out;
}

/** Play a move (UCI or SAN) on a FEN. Returns {fen, san, uci, captured, check, mate} or null if illegal. */
export function applyMove(fen, move) {
  let game;
  try { game = new Chess(fen); } catch { return null; }
  let m = null;
  try {
    m = /^[a-h][1-8][a-h][1-8][qrbn]?$/.test(move)
      ? game.move({ from: move.slice(0, 2), to: move.slice(2, 4), promotion: move[4] || undefined })
      : game.move(move);
  } catch { return null; }
  if (!m) return null;
  return { fen: game.fen(), san: m.san, uci: m.from + m.to + (m.promotion || ""), captured: !!m.captured,
    check: game.inCheck(), mate: game.isCheckmate(), from: m.from, to: m.to };
}

// ---- the board ----------------------------------------------------------------------------
export class Board {
  /**
   * @param {HTMLElement} host
   * @param {object} o  fen, orientation ("white"|"black"), coords, interactive, locked,
   *   arrows [{from,to,kind:"you"|"coach"|"draw"}], marks {sq: "you"|"coach"|"last"|"sel"|"hint"},
   *   lastMove (uci), badge {sq, kind:"correct"|"wrong"|"best"|"good"}, draw (right-click arrows, default on
   *   for full-size boards), onMove(from, to, info) called after a legal move, autoApply (default true:
   *   the board shows the move at once), label (accessible name).
   */
  constructor(host, o = {}) {
    this.host = host;
    this.o = { fen: EMPTY, orientation: "white", coords: true, interactive: false, locked: false, arrows: [], marks: {},
      lastMove: null, badge: null, autoApply: true, ...o };
    if (this.o.draw === undefined) this.o.draw = this.o.coords !== false;
    this.sel = null;
    this.drawn = [];
    this.pieceEls = new Map();
    this.legal = { fen: null, map: new Map() };
    host.classList.add("board");
    host.classList.toggle("interactive", !!this.o.interactive);
    host.dataset.theme = boardTheme();
    const L = (cls) => { const d = document.createElement("div"); d.className = cls; return d; };
    this.el = { hl: L("b-hl"), coords: L("b-coords"), pieces: L("b-pieces"), dests: L("b-dests"), badge: L("b-badge-layer") };
    this.el.svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    this.el.svg.setAttribute("class", "b-arrows");
    this.el.svg.setAttribute("viewBox", "0 0 8 8");
    this.el.svg.setAttribute("aria-hidden", "true");
    host.replaceChildren(this.el.hl, this.el.coords, this.el.dests, this.el.pieces, this.el.svg, this.el.badge);
    this.updateLabel();
    this.syncPieces(false);
    this.renderStatic();
    if (this.o.interactive || this.o.draw) this.bindPointer();
  }

  // ---- public API ----
  set(o) {
    const prev = this.o.fen;
    const animate = o.animate !== false;
    const sound = o.sound;
    const next = { ...o };
    delete next.animate;
    delete next.sound;
    Object.assign(this.o, next);
    if ("interactive" in o) this.host.classList.toggle("interactive", !!o.interactive);
    const fenChanged = "fen" in o && o.fen !== prev;
    if (fenChanged) {
      this.sel = null;
      this.drawn = [];
      if (sound) playSound(sound === true ? this.inferSound(prev, o.fen) : sound);
    }
    if (fenChanged || "orientation" in o) this.syncPieces(fenChanged && animate && !("orientation" in o));
    if ("locked" in o && o.locked) this.sel = null;
    this.updateLabel();
    this.renderStatic();
  }
  flip() { this.set({ orientation: this.o.orientation === "white" ? "black" : "white" }); }
  get fen() { return this.o.fen; }

  // ---- geometry ----
  cr(sq) {
    const f = FILES.indexOf(sq[0]), r = Number(sq[1]) - 1;
    const flip = this.o.orientation === "black";
    return { c: flip ? 7 - f : f, r: flip ? r : 7 - r };
  }
  sqAtPoint(clientX, clientY) {
    const b = this.host.getBoundingClientRect();
    let c = Math.floor(((clientX - b.left) / b.width) * 8), r = Math.floor(((clientY - b.top) / b.height) * 8);
    if (c < 0 || c > 7 || r < 0 || r > 7) return null;
    if (this.o.orientation === "black") { c = 7 - c; r = 7 - r; }
    return FILES[c] + (8 - r);
  }
  squareDiv(sq, cls) {
    const d = document.createElement("div");
    const { c, r } = this.cr(sq);
    d.className = `b-sq ${cls}`;
    d.style.transform = `translate(${c * 100}%, ${r * 100}%)`;
    return d;
  }
  pieceAt(sq) { return parseFen(this.o.fen)[sq]; }
  updateLabel() {
    const turn = sideToMove(this.o.fen);
    this.host.setAttribute("role", "img");
    this.host.setAttribute("aria-label", this.o.label || `Chess position, ${turn} to move`);
  }

  // ---- pieces (keyed by square, so moves can slide) ----
  makePiece(p) {
    const d = document.createElement("div");
    d.className = `b-piece ${colorOf(p)[0]}${p.toLowerCase()}`;
    d.style.backgroundImage = `url(${pieceUrl(p)})`;
    return d;
  }
  place(el, sq, fromSq = null) {
    const to = this.cr(sq);
    const target = `translate(${to.c * 100}%, ${to.r * 100}%)`;
    if (fromSq && !reduceMotion()) {
      const f = this.cr(fromSq);
      el.classList.remove("anim");
      el.style.transform = `translate(${f.c * 100}%, ${f.r * 100}%)`;
      void el.offsetWidth; // start the transition from the old square
      el.classList.add("anim");
      el.addEventListener("transitionend", () => el.classList.remove("anim"), { once: true });
    }
    el.style.transform = target;
    el.dataset.sq = sq;
  }
  syncPieces(animate) {
    const next = parseFen(this.o.fen);
    const kept = new Map(), vanished = [];
    for (const [sq, rec] of this.pieceEls) {
      if (next[sq] === rec.p) kept.set(sq, rec);
      else vanished.push([sq, rec]);
    }
    const moved = new Set();
    const dist = (a, b) => Math.abs(FILES.indexOf(a[0]) - FILES.indexOf(b[0])) + Math.abs(a[1] - b[1]);
    for (const [sq, p] of Object.entries(next)) {
      if (kept.has(sq)) continue;
      let best = -1, bd = 99;
      vanished.forEach(([vsq, rec], i) => { if (rec.p === p && dist(vsq, sq) < bd) { bd = dist(vsq, sq); best = i; } });
      if (best >= 0) {
        const [[fromSq, rec]] = vanished.splice(best, 1);
        kept.set(sq, rec);
        this.place(rec.el, sq, animate ? fromSq : null);
        moved.add(rec);
      } else {
        const el = this.makePiece(p);
        this.el.pieces.append(el);
        kept.set(sq, { el, p });
        this.place(el, sq);
        moved.add(kept.get(sq));
      }
    }
    for (const [, rec] of vanished) { // captured or removed
      if (animate && !reduceMotion()) { rec.el.classList.add("gone"); setTimeout(() => rec.el.remove(), 200); }
      else rec.el.remove();
    }
    for (const [sq, rec] of kept) if (!moved.has(rec)) this.place(rec.el, sq);
    this.pieceEls = kept;
  }

  // ---- highlights, dots, coordinates, arrows ----
  legalMap() {
    if (this.legal.fen !== this.o.fen) this.legal = { fen: this.o.fen, map: legalMoves(this.o.fen) };
    return this.legal.map;
  }
  checkSquare() {
    let game;
    try { game = new Chess(this.o.fen); } catch { return null; }
    if (!game.inCheck()) return null;
    const k = game.turn() === "w" ? "K" : "k";
    return Object.entries(parseFen(this.o.fen)).find(([, p]) => p === k)?.[0] || null;
  }
  renderStatic() {
    const o = this.o, hl = [];
    const add = (sq, cls) => { if (isSq(sq)) hl.push(this.squareDiv(sq, cls)); };
    if (o.lastMove && o.lastMove.length >= 4) { add(o.lastMove.slice(0, 2), "last"); add(o.lastMove.slice(2, 4), "last"); }
    for (const [sq, k] of Object.entries(o.marks || {})) add(sq, `mark-${k}`);
    if (this.sel) add(this.sel, "sel");
    if (this.hover) add(this.hover, "hover");
    const chk = o.fen !== EMPTY ? this.checkSquare() : null;
    if (chk) add(chk, "check");
    this.el.hl.replaceChildren(...hl);

    const dests = this.sel ? (this.legalMap().get(this.sel) || []) : [];
    const occupied = parseFen(o.fen);
    this.el.dests.replaceChildren(...dests.map((m) => this.squareDiv(m.to, occupied[m.to] ? "dest cap" : "dest")));

    const coords = [];
    if (o.coords) {
      const flip = o.orientation === "black";
      for (let i = 0; i < 8; i++) {
        const rankSq = (flip ? "h" : "a") + (flip ? i + 1 : 8 - i);
        const fileSq = (flip ? FILES[7 - i] : FILES[i]) + (flip ? 8 : 1);
        coords.push(this.coordDiv(rankSq, rankSq[1], "rank"), this.coordDiv(fileSq, fileSq[0], "file"));
      }
    }
    this.el.coords.replaceChildren(...coords);

    if (o.badge && isSq(o.badge.sq)) {
      const d = this.squareDiv(o.badge.sq, "badge-sq");
      const icon = { correct: "✓", wrong: "✗", best: "★", good: "✓", hint: "?" }[o.badge.kind] || "•";
      const b = document.createElement("span");
      b.className = `b-badge ${o.badge.kind}`;
      b.textContent = icon;
      d.append(b);
      this.el.badge.replaceChildren(d);
    } else this.el.badge.replaceChildren();
    this.renderArrows();
  }
  coordDiv(sq, text, kind) {
    const d = this.squareDiv(sq, `coord ${kind} ${(FILES.indexOf(sq[0]) + Number(sq[1])) % 2 === 1 ? "on-d" : "on-l"}`);
    d.textContent = text;
    return d;
  }
  renderArrows() {
    const NS = "http://www.w3.org/2000/svg";
    const mk = (tag, attrs) => { const n = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); return n; };
    const svgEl = this.el.svg;
    const defs = mk("defs", {});
    for (const kind of ["you", "coach", "draw", "hint"]) {
      const m = mk("marker", { id: `ah-${kind}-${this.uid()}`, viewBox: "0 0 10 10", refX: "4.2", refY: "5", markerWidth: "3.2", markerHeight: "3.2", orient: "auto" });
      m.append(mk("path", { d: "M0 0 L10 5 L0 10 z", class: `ah ${kind}` }));
      defs.append(m);
    }
    const kids = [defs];
    const center = (sq) => { const { c, r } = this.cr(sq); return [c + 0.5, r + 0.5]; };
    for (const a of [...(this.o.arrows || []), ...this.drawn]) {
      if (!a) continue;
      const kind = a.kind || "coach";
      if (a.circle && isSq(a.circle)) {
        const [x, y] = center(a.circle);
        kids.push(mk("circle", { cx: x, cy: y, r: 0.45, class: `circ ${kind}` }));
        continue;
      }
      if (!isSq(a.from) || !isSq(a.to) || a.from === a.to) continue;
      const [x1, y1] = center(a.from), [x2, y2] = center(a.to);
      const dx = x2 - x1, dy = y2 - y1;
      const knight = (Math.abs(dx) === 1 && Math.abs(dy) === 2) || (Math.abs(dx) === 2 && Math.abs(dy) === 1);
      const shorten = (ax, ay, bx, by) => { const len = Math.hypot(bx - ax, by - ay); const k = (len - 0.42) / len; return [ax + (bx - ax) * k, ay + (by - ay) * k]; };
      let d;
      if (knight) { // an L, like a knight's path
        const [cx, cy] = Math.abs(dy) === 2 ? [x1, y2] : [x2, y1];
        const [ex, ey] = shorten(cx, cy, x2, y2);
        d = `M${x1} ${y1} L${cx} ${cy} L${ex} ${ey}`;
      } else {
        const [ex, ey] = shorten(x1, y1, x2, y2);
        d = `M${x1} ${y1} L${ex} ${ey}`;
      }
      kids.push(mk("path", { d, class: `arr ${kind}`, "marker-end": `url(#ah-${kind}-${this.uid()})` }));
    }
    svgEl.replaceChildren(...kids);
  }
  uid() { if (!this._uid) this._uid = Math.random().toString(36).slice(2, 8); return this._uid; }
  inferSound(prevFen, fen) {
    const n = (f) => Object.keys(parseFen(f)).length;
    let check = false;
    try { check = new Chess(fen).inCheck(); } catch { /* ignore */ }
    return check ? "check" : n(fen) < n(prevFen) ? "capture" : "move";
  }

  // ---- interaction ----
  canMove() { return this.o.interactive && !this.o.locked; }
  isMine(sq) {
    const p = this.pieceAt(sq);
    return !!p && colorOf(p) === sideToMove(this.o.fen) && (!this.o.movable || this.o.movable === colorOf(p));
  }
  isDest(from, to) { return (this.legalMap().get(from) || []).some((m) => m.to === to); }

  bindPointer() {
    const h = this.host;
    h.addEventListener("contextmenu", (e) => e.preventDefault());
    h.addEventListener("pointerdown", (e) => this.onDown(e));
    h.addEventListener("pointermove", (e) => this.onMoveP(e));
    h.addEventListener("pointerup", (e) => this.onUp(e));
    h.addEventListener("pointercancel", () => this.cancelDrag());
  }
  onDown(e) {
    const sq = this.sqAtPoint(e.clientX, e.clientY);
    if (!sq) return;
    if (e.button === 2) { if (this.o.draw) { this.drawFrom = sq; e.preventDefault(); } return; }
    if (e.button !== 0) return;
    if (this.drawn.length) { this.drawn = []; this.renderArrows(); }
    if (!this.canMove()) return;
    if (this.sel && this.sel !== sq && this.isDest(this.sel, sq)) { this.tryMove(this.sel, sq, true); return; }
    if (!this.isMine(sq)) { if (this.sel) { this.sel = null; this.renderStatic(); } return; }
    e.preventDefault();
    const wasSelected = this.sel === sq;
    this.sel = sq;
    this.renderStatic();
    const rec = this.pieceEls.get(sq);
    this.drag = { from: sq, x0: e.clientX, y0: e.clientY, started: false, el: rec && rec.el, wasSelected };
    try { this.host.setPointerCapture(e.pointerId); } catch { /* ignore */ }
  }
  onMoveP(e) {
    const d = this.drag;
    if (!d || !d.el) return;
    if (!d.started && Math.hypot(e.clientX - d.x0, e.clientY - d.y0) < 4) return;
    d.started = true;
    const b = this.host.getBoundingClientRect(), size = b.width / 8;
    d.el.classList.remove("anim");
    d.el.classList.add("dragging");
    d.el.style.transform = `translate(${e.clientX - b.left - size / 2}px, ${e.clientY - b.top - size / 2}px) scale(1.12)`;
    const over = this.sqAtPoint(e.clientX, e.clientY);
    if (over !== this.hover) { this.hover = over; this.renderStatic(); }
  }
  onUp(e) {
    if (e.button === 2 && this.drawFrom) {
      const to = this.sqAtPoint(e.clientX, e.clientY), from = this.drawFrom;
      this.drawFrom = null;
      if (!to) return;
      const same = (a) => (from === to ? a.circle === to : a.from === from && a.to === to);
      const i = this.drawn.findIndex(same);
      if (i >= 0) this.drawn.splice(i, 1);
      else this.drawn.push(from === to ? { circle: to, kind: "draw" } : { from, to, kind: "draw" });
      this.renderArrows();
      return;
    }
    const d = this.drag;
    if (!d) return;
    this.drag = null;
    this.hover = null;
    if (d.started) {
      d.el.classList.remove("dragging");
      const to = this.sqAtPoint(e.clientX, e.clientY);
      if (to && to !== d.from && this.isDest(d.from, to)) { this.tryMove(d.from, to, false); return; }
      this.place(d.el, d.from); // not a legal square: back where it was
      if (to && to !== d.from) playSound("wrong");
      this.renderStatic();
      return;
    }
    if (d.wasSelected) { this.sel = null; } // second click on the same piece: deselect
    this.renderStatic();
  }
  cancelDrag() {
    if (this.drag && this.drag.el) { this.drag.el.classList.remove("dragging"); this.place(this.drag.el, this.drag.from); }
    this.drag = null;
    this.hover = null;
    this.renderStatic();
  }

  async tryMove(from, to, animate) {
    const needsPromo = (this.legalMap().get(from) || []).some((m) => m.to === to && m.promotion);
    let promo = "";
    if (needsPromo) {
      promo = await this.askPromotion(to, sideToMove(this.o.fen));
      if (!promo) { this.sel = null; this.syncPieces(false); this.renderStatic(); return; }
    }
    const before = this.o.fen;
    const r = applyMove(before, from + to + promo);
    if (!r) return;
    this.sel = null;
    if (this.o.autoApply !== false) this.set({ fen: r.fen, lastMove: r.uci, animate, badge: null, arrows: [], marks: {} });
    playSound(r.check ? "check" : r.captured ? "capture" : "move");
    this.o.onMove && this.o.onMove(from, to, { ...r, before, promotion: promo || null });
  }

  askPromotion(to, color) {
    return new Promise((resolve) => {
      const { c, r } = this.cr(to);
      const down = r === 0; // the picker grows away from the edge it starts at
      const wrap = document.createElement("div");
      wrap.className = "b-promo";
      const done = (v) => { wrap.remove(); resolve(v); };
      wrap.addEventListener("pointerdown", (e) => { if (e.target === wrap) done(""); });
      ["q", "n", "r", "b"].forEach((p, i) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "b-promo-pc";
        btn.setAttribute("aria-label", `Promote to ${{ q: "queen", n: "knight", r: "rook", b: "bishop" }[p]}`);
        btn.style.transform = `translate(${c * 100}%, ${(down ? r + i : r - i) * 100}%)`;
        btn.style.backgroundImage = `url(${pieceUrl(color === "white" ? p.toUpperCase() : p)})`;
        btn.addEventListener("pointerdown", (e) => e.stopPropagation());
        btn.addEventListener("click", () => done(p));
        wrap.append(btn);
      });
      this.host.append(wrap);
      wrap.querySelector("button").focus();
    });
  }
}

/** A small static board, e.g. beside a coach answer. */
export function miniBoard(fen, { orientation, arrows = [], marks = {}, label, large = false, lastMove = null } = {}) {
  const host = document.createElement("div");
  host.className = large ? "mini large" : "mini";
  new Board(host, { fen, orientation: orientation || sideToMove(fen), coords: false, draw: false, arrows, marks, label, lastMove });
  return host;
}

export const uciArrow = (uci, kind) => (uci && uci.length >= 4 ? { from: uci.slice(0, 2), to: uci.slice(2, 4), kind } : null);
export const uciMarks = (uci, kind) => (uci && uci.length >= 4 ? { [uci.slice(0, 2)]: kind, [uci.slice(2, 4)]: kind } : {});

/** A theme and sound picker for a board's footer. */
export function boardSettings(board) {
  const wrap = document.createElement("div");
  wrap.className = "board-settings";
  const sel = document.createElement("select");
  sel.setAttribute("aria-label", "Board colours");
  for (const [k, v] of Object.entries(THEMES)) {
    const opt = document.createElement("option");
    opt.value = k;
    opt.textContent = v;
    opt.selected = k === boardTheme();
    sel.append(opt);
  }
  sel.addEventListener("change", () => setBoardTheme(sel.value));
  const snd = document.createElement("button");
  snd.type = "button";
  snd.className = "link quiet small";
  const label = () => { snd.textContent = soundEnabled() ? "Sound on" : "Sound off"; snd.setAttribute("aria-pressed", String(soundEnabled())); };
  label();
  snd.addEventListener("click", () => { setSoundEnabled(!soundEnabled()); label(); if (soundEnabled()) playSound("move"); });
  const flip = document.createElement("button");
  flip.type = "button";
  flip.className = "link quiet small";
  flip.textContent = "Flip board";
  flip.addEventListener("click", () => board.flip());
  wrap.append(sel, snd, flip);
  return wrap;
}
