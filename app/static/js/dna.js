// Rating DNA: one bar per skill, growing left (red pen: below your rating) or right (blue ink: above it)
// from a centre line at your overall rating, like an engine's evaluation graph. The coach circles
// the skill that costs you the most. Also draws the shareable PNG card.

import { el, signed, svg } from "./core.js";

const PEN_ELLIPSE = "M6 23 C 4 9, 40 2, 66 4 C 92 6, 99 14, 96 26 C 93 39, 60 45, 34 43 C 12 41, 2 33, 7 20 C 9 15, 15 11, 22 9";

export function weakestSkill(dna) {
  const pool = dna.skills.filter((s) => !s.low_confidence);
  const w = (pool.length ? pool : dna.skills).reduce((a, b) => (b.rating < a.rating ? b : a), dna.skills[0]);
  return w && w.rating < dna.base_rating - 10 ? w : null;
}

export function renderDNA(host, dna, { annotate = true, animate = false } = {}) {
  const base = dna.base_rating;
  const offs = dna.skills.map((s) => Math.abs(s.rating - base));
  const max = Math.max(50, Math.ceil(Math.max(...offs, 1) / 25) * 25);
  const weak = annotate ? weakestSkill(dna) : null;
  const box = el("div", { class: `dna${animate ? " animate" : ""}`, role: "group", "aria-label": `Rating DNA around ${base}` });
  box.append(el("div", { class: "dna-axis", "aria-hidden": "true" }, el("span"),
    el("div", { class: "scale num" }, el("span", { text: `${base - max}` }), el("span", { text: `${base}${dna.rating_is_estimated ? "?" : ""}` }),
      el("span", { text: `${base + max}` })), el("span")));
  dna.skills.forEach((s, i) => {
    const off = s.rating - base;
    const pct = Math.min(50, (50 * Math.abs(off)) / max);
    const row = el("div", { class: `dna-row${weak && weak.key === s.key ? " weakest" : ""}` },
      el("div", { class: "name" }, s.label, el("small", { text: s.low_confidence ? `${s.moves} moves, low confidence` : `${s.accuracy}% accurate` })),
      el("div", { class: "dna-track", role: "img", "aria-label": `${s.label}: ${s.rating}, ${signed(off)} against your ${base}` },
        el("div", { class: "grid" }),
        el("div", { class: `dna-bar ${off >= 0 ? "up" : "down"}${s.low_confidence ? " low" : ""}`, style: { width: `${pct}%`, "--i": i } })),
      el("div", { class: "val" }, el("b", { class: "figure", text: String(s.rating) }), el("span", { text: signed(off) })));
    if (weak && weak.key === s.key) {
      row.style.marginBottom = "26px";
      const pen = el("div", { class: "pen", "aria-hidden": "true", style: { right: "-12px", top: "-6px", width: "calc(4.6rem + 24px)", height: "50px" } },
        svg("svg", { viewBox: "0 0 100 46", preserveAspectRatio: "none", width: "100%", height: "100%" },
          svg("path", { d: PEN_ELLIPSE, fill: "none", stroke: "currentColor", "stroke-width": "2.2", "stroke-linecap": "round", "vector-effect": "non-scaling-stroke" })),
        el("span", { class: "note", style: { right: "6px", top: "50px", transform: "rotate(-2.5deg)" }, text: `costing you ~${Math.abs(off)} pts` }));
      row.append(pen);
    }
    box.append(row);
  });
  host.replaceChildren(box, el("div", { class: "dna-legend" },
    el("span", {}, el("i", { style: { background: "var(--blue)" } }), "above your rating"),
    el("span", {}, el("i", { style: { background: "var(--red)" } }), "below your rating"),
    el("span", {}, el("i", { style: { background: "var(--rule-2)" } }), "striped: too few moves to be sure")));
  return box;
}

/** A 1200×630 PNG for WhatsApp, Instagram, X: handle, rating, the seven bars, the weakest skill. */
export async function shareCard(dna, handle, platform) {
  try { await Promise.all([document.fonts.load('800 60px "Archivo"'), document.fonts.load('600 40px "Caveat"'), document.fonts.load('40px "Pieces"')]); } catch { /* fallback fonts */ }
  const W = 1200, H = 630;
  const c = document.createElement("canvas");
  c.width = W; c.height = H;
  const g = c.getContext("2d");
  const ink = "#1c2235", blue = "#2b44c9", red = "#c9372c", rule = "#d8dde6", muted = "#545d73";
  g.fillStyle = "#eef1f5"; g.fillRect(0, 0, W, H);
  g.fillStyle = "#ffffff"; roundRect(g, 40, 40, W - 80, H - 80, 18); g.fill();
  const stretch = (v) => { if ("fontStretch" in g) g.fontStretch = v; };

  g.fillStyle = blue; g.font = '44px "Pieces"'; g.fillText("♜︎", 84, 118);
  stretch("expanded"); g.fillStyle = ink; g.font = '800 26px "Archivo", sans-serif'; g.fillText("Plateau Breaker", 132, 108);
  stretch("normal"); g.fillStyle = muted; g.font = '500 22px "Archivo", sans-serif'; g.fillText(`${platform} · Rating DNA`, 84, 176);
  stretch("expanded"); g.fillStyle = ink; g.font = '800 54px "Archivo", sans-serif'; g.fillText(trunc(g, handle, 470), 84, 238);
  stretch("condensed"); g.font = '800 120px "Archivo", sans-serif'; g.fillText(String(dna.base_rating), 84, 380);
  stretch("normal"); g.fillStyle = muted; g.font = '500 24px "Archivo", sans-serif';
  g.fillText(`overall, from ${dna.games} games`, 84, 420);
  const weak = weakestSkill(dna);
  if (weak) {
    g.save(); g.translate(84, 500); g.rotate(-0.035);
    g.fillStyle = red; g.font = '600 40px "Caveat", cursive';
    g.fillText(`${weak.label}: costing me ~${dna.base_rating - weak.rating} pts`, 0, 0);
    g.restore();
  }

  const x0 = 620, x1 = 1110, mid = (x0 + x1) / 2, top = 96, rowH = 62;
  const max = Math.max(50, Math.ceil(Math.max(...dna.skills.map((s) => Math.abs(s.rating - dna.base_rating)), 1) / 25) * 25);
  g.strokeStyle = rule; g.lineWidth = 1;
  for (let k = 0; k <= 4; k++) { const x = x0 + ((x1 - x0) * k) / 4; g.beginPath(); g.moveTo(x, top - 10); g.lineTo(x, top + rowH * dna.skills.length); g.stroke(); }
  dna.skills.forEach((s, i) => {
    const y = top + i * rowH;
    const off = s.rating - dna.base_rating;
    const w = ((x1 - x0) / 2) * Math.min(1, Math.abs(off) / max);
    g.fillStyle = off >= 0 ? blue : red;
    roundRect(g, off >= 0 ? mid : mid - w, y + 30, Math.max(w, 2), 18, 3); g.fill();
    stretch("normal"); g.fillStyle = ink; g.font = '600 21px "Archivo", sans-serif'; g.fillText(s.label, x0, y + 20);
    stretch("condensed"); g.font = '800 24px "Archivo", sans-serif'; g.textAlign = "right"; g.fillStyle = off < 0 ? red : ink;
    g.fillText(`${s.rating}`, x1, y + 20); g.textAlign = "left";
  });
  g.fillStyle = ink; g.fillRect(mid - 1, top - 14, 2, rowH * dna.skills.length + 4);
  stretch("normal"); g.fillStyle = muted; g.font = '500 20px "Archivo", sans-serif';
  g.fillText(location.host, 84, H - 70);
  return new Promise((res) => c.toBlob(res, "image/png"));
}

function roundRect(g, x, y, w, h, r) {
  g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath();
}
function trunc(g, text, width) {
  let t = text;
  while (g.measureText(t).width > width && t.length > 3) t = t.slice(0, -2);
  return t === text ? t : `${t}…`;
}
