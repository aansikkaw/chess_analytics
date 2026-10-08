// The plan table, shared by the landing page and the app's Plans page.

import { $$, S, api, currency, el, isPro, post, setCurrency, toast, track } from "./core.js";

let cache = null;
export async function loadPlans() {
  if (!cache) cache = await api("/api/plans");
  return cache;
}

export async function renderPlans(host, { onChoose } = {}) {
  let plans;
  try { plans = await loadPlans(); } catch (e) { host.replaceChildren(el("p", { class: "err", text: e.message })); return; }
  const cur = currency();
  syncCurrencyButtons();
  const current = S.me ? (isPro() ? (S.me.plan_expires_at ? "event" : "pro") : "free") : null;
  host.replaceChildren(...plans.map((p) => {
    const pr = p.price || {};
    const main = cur === "inr" ? pr.inr : pr.usd;
    const annual = cur === "inr" ? pr.annual_inr : pr.annual_usd;
    let action;
    if (current === p.id) action = el("span", { class: "tag ok", text: "Your plan" });
    else if (p.id === "free") action = S.me ? null : el("button", { class: "btn quiet", type: "button", text: "Start free", onclick: () => onChoose && onChoose(p) });
    else action = el("button", { class: p.featured ? "btn" : "btn quiet", type: "button",
      text: p.waitlist ? "Join the waitlist" : p.id === "event" ? "Get an Event Pass" : "Choose Pro",
      onclick: (e) => (onChoose ? onChoose(p, e.currentTarget) : requestPlan(p, e.currentTarget)) });
    return el("div", { class: `plan${p.featured ? " featured" : ""}` },
      el("h3", { text: p.name }),
      el("p", { class: "tagline", text: p.tagline }),
      el("div", { class: "price figure" }, main || "", pr.period ? el("small", { text: pr.period === "one-off" ? "one-off" : `a ${pr.period}` }) : null),
      annual ? el("p", { class: "alt", text: `or ${annual} a year` }) : el("p", { class: "alt", text: " " }),
      el("ul", {}, ...p.points.map((t) => el("li", { text: t }))),
      action);
  }));
}

export async function requestPlan(p, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await post("/api/upgrade-request", { plan: p.id });
    track("Upgrade request", { plan: p.id });
    if (btn) btn.replaceWith(el("span", { class: "small ok-text", text: r.message }));
    else toast(r.message);
  } catch (e) {
    if (btn) btn.disabled = false;
    toast(e.message);
  }
}

export function syncCurrencyButtons() {
  for (const b of $$("[data-cur]")) b.setAttribute("aria-pressed", String(b.dataset.cur === currency()));
}
export function bindCurrency(rerender) {
  for (const b of $$("[data-cur]")) {
    b.onclick = () => { setCurrency(b.dataset.cur); syncCurrencyButtons(); rerender(); };
  }
}
