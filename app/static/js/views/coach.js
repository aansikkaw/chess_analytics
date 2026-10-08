// Coach: ask about your games. Answers arrive as summary, evidence (with the positions on small boards) and one next step.

import { S, account, el, post, track } from "../core.js";
import { miniBoard, uciArrow } from "../board.js";
import { openConcept, openPosition } from "./common.js";

const LINK_RE = /\[([^\]]+)\]\((\/[^)\s]+?)\.md\)/g;
const SUGGEST = [
  "What should I study this week?",
  "Why do I lose won positions?",
  "Show me my worst mistake and what I should have been thinking",
  "How is my time management?",
  "Which openings are costing me points?",
  "Where do I forget my preparation?",
];

export function render(main) {
  const a = account();
  const chat = el("div", { class: "chat", "aria-live": "polite" });
  const input = el("input", { type: "text", id: "chatInput", autocomplete: "off", placeholder: "Ask about your games", maxlength: "2000" });
  const form = el("form", { class: "chat-form" }, el("label", { class: "sr", for: "chatInput", text: "Message the coach" }), input,
    el("button", { class: "btn", type: "submit", text: "Ask" }));
  const chips = el("div", { class: "chips" }, ...SUGGEST.map((q) => el("button", { class: "chip", type: "button", text: q, onclick: () => ask(q) })));
  const models = (S.config && S.config.coach_models) || [];
  main.replaceChildren(
    el("div", { class: "page-head" }, el("div", {}, el("h1", { text: "Coach" }),
      el("p", { class: "sub", text: models.length
        ? "Every move and evaluation in an answer is checked against your analysed games first; anything that can't be checked is flagged."
        : "Running the built-in coach: rule-based answers with links to your positions. The server owner can switch on an AI model." }))),
    el("div", { class: "chat-wrap" }, chat, el("div", { class: "stack" }, S.chat.length ? null : chips, form)));
  if (!S.chat.length) {
    chat.append(el("div", { class: "msg a" }, el("p", { class: "summary", text: `I've read ${a.handle}'s ${a.games} analysed games. Ask me anything, or start with one of these:` })));
  } else {
    for (const m of S.chatLog || []) chat.append(m);
  }
  form.addEventListener("submit", (e) => { e.preventDefault(); const q = input.value.trim(); input.value = ""; if (q) ask(q); });

  async function ask(q) {
    chips.remove();
    add(el("div", { class: "msg u", text: q }));
    const thinking = add(el("div", { class: "msg a thinking", text: "Reading your games…" }));
    try {
      const r = await post(`/api/accounts/${a.id}/coach`, { message: q, history: S.chat });
      thinking.remove();
      Object.assign(S.citations, r.citations);
      add(answer(r));
      S.chat.push({ role: "user", content: q }, { role: "assistant", content: r.reply });
      S.chat = S.chat.slice(-20);
      track("Coach question", { mode: r.mode });
    } catch (e) {
      thinking.remove();
      add(el("div", { class: "msg a" }, el("p", {}, e.message, e.upgrade ? el("span", {}, " ", el("a", { href: "#/plans", text: "See Pro" })) : null)));
    }
    input.focus();
  }
  function add(node) {
    chat.append(node);
    S.chatLog = [...chat.children];
    node.scrollIntoView({ block: "nearest", behavior: "smooth" });
    return node;
  }
}

function rich(text) {
  const frag = document.createDocumentFragment();
  let last = 0;
  for (const m of text.matchAll(LINK_RE)) {
    frag.append(text.slice(last, m.index));
    const [label, id] = [m[1], m[2]];
    const card = S.citations[id];
    frag.append(el("button", { class: `cite${card && card.fen ? " coach" : ""}`, type: "button", text: label,
      onclick: () => (card && card.fen ? openPosition(card) : openConcept(id)) }));
    last = m.index + m[0].length;
  }
  frag.append(text.slice(last));
  return frag;
}

function answer(r) {
  const a = r.answer;
  const m = el("div", { class: "msg a" });
  if (r.notice) m.append(el("p", { class: "notice small", text: r.notice, style: { marginBottom: "12px" } }));
  if (!a) { m.append(el("p", {}, rich(r.reply))); return m; }
  m.append(el("p", { class: "summary" }, rich(a.summary)));
  const evidenceBoards = (a.evidence || []).some((e) => e.concept_id && S.citations[e.concept_id] && S.citations[e.concept_id].fen);
  if (!evidenceBoards) {
    // The answer cites a position only in its text: show the first one on a board too.
    const cited = [...`${a.summary} ${a.next_step || ""}`.matchAll(LINK_RE)].map((x) => S.citations[x[2]]).find((c) => c && c.fen);
    if (cited) {
      m.append(el("div", { class: "row", style: { alignItems: "flex-start", gap: "14px", marginTop: "12px" } },
        el("button", { class: "cite", type: "button", style: { textDecoration: "none" }, "aria-label": "Open this position", onclick: () => openPosition(cited) },
          miniBoard(cited.fen, { arrows: [uciArrow(cited.played_uci, "you"), uciArrow(cited.best_uci, "coach")].filter(Boolean), large: true,
            label: `You played ${cited.you_played}; the engine preferred ${cited.engine_best}` })),
        el("div", { class: "legend-ink", style: { flexDirection: "column", gap: "6px" } },
          el("span", {}, el("i", { style: { background: "#2b44c9" } }), `you played ${cited.you_played}`),
          el("span", {}, el("i", { style: { background: "#c9372c" } }), `the engine's ${cited.engine_best}`))));
    }
  }
  if (a.evidence && a.evidence.length) {
    m.append(el("ul", { class: "evidence" }, ...a.evidence.map((e) => {
      const card = e.concept_id ? S.citations[e.concept_id] : null;
      const li = el("li");
      if (card && card.fen) {
        const mb = miniBoard(card.fen, { arrows: [uciArrow(card.played_uci, "you"), uciArrow(card.best_uci, "coach")].filter(Boolean),
          label: `${card.move || "Position"}: you played ${card.you_played}, the engine preferred ${card.engine_best}` });
        const open = el("button", { class: "cite", type: "button", style: { textDecoration: "none" }, "aria-label": `Open ${e.label || "position"}`, onclick: () => openPosition(card) }, mb);
        li.append(open);
      }
      li.append(el("div", {}, el("p", {}, rich(e.point)),
        e.concept_id && !(card && card.fen) ? el("button", { class: "cite small", type: "button", text: e.label || "Details", onclick: () => openConcept(e.concept_id) }) : null));
      return li;
    })));
  }
  if (a.next_step) m.append(el("div", { class: "nextstep" }, el("b", { text: "Next step" }), el("span", {}, rich(a.next_step))));
  const n = Object.keys(r.citations || {}).length;
  m.append(el("div", { class: "meta" },
    r.mode === "agent" ? (r.grounded ? el("span", { class: "ok", text: `Checked against your games${n ? `, ${n} position${n === 1 ? "" : "s"} cited` : ""}` })
      : el("span", { class: "warn", text: "Some details couldn't be checked; they're marked above." })) : el("span", { text: "Built-in coach" }),
    r.model ? el("span", { text: r.model }) : null));
  return m;
}
