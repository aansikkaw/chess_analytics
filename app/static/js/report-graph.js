// The report card's evaluation graph (a slider you can scrub), the move-class words and move numbering. A module of
// its own so the landing page's sample report card can use it without loading the whole report page (DESIGN.md).

import { el, evalText, svg } from "./core.js";

const cap = (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : "");
const num = (m) => `${m.move_number}${m.color === "white" ? "." : "..."}`;

// Move classes: the standard symbols with our own words (DESIGN.md: never another site's move tiers or colours).
// Tone: "err" is the coach's red pen, "good" is ink, "plain" carries no mark.
export const CLASS = {
  brilliant: ["!!", "Sound sacrifice", "good"], great: ["!", "Only good move", "good"], best: ["", "Engine's choice", "plain"],
  excellent: ["", "Good", "plain"], good: ["", "Good", "plain"], book: ["", "Book move", "plain"], forced: ["□", "Only move", "plain"],
  inaccuracy: ["?!", "Inaccuracy", "err"], mistake: ["?", "Mistake", "err"], miss: ["?", "Didn't punish", "err"],
  blunder: ["??", "Blunder", "err"],
};
export const BAD = ["inaccuracy", "mistake", "miss", "blunder"];

/** "12.Nxe6 Qc8 13.Nxf8", or "12…Nxg6 13.Qh5" when Black moves first, numbered from `fen`. */
export function numberedFrom(fen, sans) {
  const [side, , , , , full] = fen.split(" ");
  let n = Number(full) || 1, white = side !== "b";
  return sans.map((san, i) => {
    const t = white ? `${n}.${san}` : i === 0 ? `${n}…${san}` : san;
    if (!white) n += 1;
    white = !white;
    return t;
  }).join(" ");
}

/** The evaluation graph, as a slider you can scrub: drag along it (or use the arrow keys) and the board follows.
 *  Guided: your winning chances, labelled in words. Pro: White's side, with ±1 and ±3 pawns marked. */
export function evalGraph(rep, { you, pro, onPick }) {
  const s = rep.series;
  const n = Math.max(1, s.length - 1);
  const persp = pro ? "white" : you || "white";
  const val = (i) => (persp === "white" ? s[i] : 100 - s[i]);
  const W = 800, H = 160;
  const x = (i) => (i / n) * W, y = (v) => H - (v / 100) * H;
  const pts = s.map((_, i) => `${x(i).toFixed(1)},${y(val(i)).toFixed(1)}`).join(" ");
  const uid = Math.random().toString(36).slice(2, 7);
  const area = (clip, cls) => svg("polygon", { points: `0,${H / 2} ${pts} ${W},${H / 2}`, class: cls, "clip-path": `url(#${clip}-${uid})` });
  const guides = pro ? [75.1, 59.1, 40.9, 24.9] : [];
  const plot = svg("svg", { class: "eg-svg", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", "aria-hidden": "true" },
    svg("defs", {}, svg("clipPath", { id: `up-${uid}` }, svg("rect", { x: 0, y: 0, width: W, height: H / 2 })),
      svg("clipPath", { id: `dn-${uid}` }, svg("rect", { x: 0, y: H / 2, width: W, height: H / 2 }))),
    ...guides.map((g) => svg("line", { x1: 0, x2: W, y1: y(g), y2: y(g), class: "eg-guide" })),
    area("up", "eg-up"), area("dn", "eg-dn"),
    svg("line", { x1: 0, x2: W, y1: H / 2, y2: H / 2, class: "eg-mid" }),
    svg("polyline", { points: pts, class: "eg-line" }));
  const pct = (i) => `${(i / n) * 100}%`;
  const labels = pro
    ? [["+3", 75.1], ["+1", 59.1], ["−1", 40.9], ["−3", 24.9]]
    : [[you ? "You're winning" : `${cap(persp)} winning`, 92], ["Even", 50], [you ? "They're winning" : `${persp === "white" ? "Black" : "White"} winning`, 8]];
  const dots = rep.moves.filter((m) => BAD.includes(m.class)).map((m) => el("span", {
    class: `eg-dot ${m.color === (you || "white") ? "mine" : "theirs"} ${m.class}`, "aria-hidden": "true",
    style: { left: pct(m.ply + 1), top: `${100 - val(m.ply + 1)}%` } }));
  const cursor = el("span", { class: "eg-cursor", "aria-hidden": "true" });
  const tip = el("span", { class: "eg-tip", "aria-hidden": "true" });
  const node = el("div", { class: "eg-wrap", role: "slider", tabindex: "0", "aria-label": pro ? "Evaluation graph" : "Who was winning, move by move",
    "aria-valuemin": "0", "aria-valuemax": String(n), "aria-valuenow": "0" },
  plot, ...labels.map(([t, v]) => el("span", { class: "eg-label", style: { top: `${100 - v}%` }, text: t })), ...dots, cursor, tip);
  const describe = (i) => {
    const m = i > 0 ? rep.moves[i - 1] : null;
    const where = m ? `${num(m)}${m.san}${CLASS[m.class][0]}` : "Start";
    const v = val(i);
    return pro ? `${where} · ${evalText(m ? m.eval_white : null, null, 2).replace("–", `${Math.round(s[i])}%`)}`
      : `${where} · ${you ? "you" : persp} ${Math.round(v)}%`;
  };
  let pressed = false, raf = 0, last = -1;
  const at = (e) => { const r = node.getBoundingClientRect(); return Math.max(0, Math.min(n, Math.round(((e.clientX - r.left) / r.width) * n))); };
  const pick = (e) => {
    const i = at(e);
    if (i === last) return;
    last = i;
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => onPick(i));
  };
  node.addEventListener("pointerdown", (e) => { if (e.button !== 0) return; pressed = true; last = -1; node.setPointerCapture(e.pointerId); node.classList.add("scrubbing"); pick(e); });
  node.addEventListener("pointermove", (e) => { if (pressed) pick(e); });
  const release = () => { pressed = false; node.classList.remove("scrubbing"); };
  node.addEventListener("pointerup", release);
  node.addEventListener("pointercancel", release);
  node.addEventListener("keydown", (e) => {
    if (e.key === "Home") { onPick(0); e.preventDefault(); e.stopPropagation(); }
    if (e.key === "End") { onPick(n); e.preventDefault(); e.stopPropagation(); }
  });
  return { node, mark: (i) => {
    cursor.style.left = pct(i);
    tip.textContent = describe(i);
    tip.style.left = `${Math.min(88, Math.max(12, (i / n) * 100))}%`;
    node.setAttribute("aria-valuenow", String(i));
    node.setAttribute("aria-valuetext", describe(i));
  } };
}
