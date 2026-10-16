// Keyboard shortcuts. Two everywhere in the app: Ctrl/Cmd+K opens the command palette, ? lists every shortcut.
// A page adds its own with pageKeys() (the report card's analysis board does); they work while that page is
// on screen. Shortcuts never fire while you type in a field, and an open dialog keeps the keyboard to itself.

import { el, fill } from "./core.js";
import { openPalette } from "./palette.js";

const GLOBAL = [
  ["Ctrl K  /  ⌘ K", "Command palette: jump to a game, opening, report card or puzzle"],
  ["?", "Show these shortcuts"],
  ["Esc", "Close a dialog or menu"],
];
let page = null; // { node, title, list: [[keys, what]], handle(e) -> true when the key was used }

/** Shortcuts for the page showing `node`. `handle(e)` returns true if it used the key. */
export function pageKeys(node, title, list, handle) { page = { node, title, list, handle }; }

const typing = (t) => !!(t && t.closest && t.closest("input, textarea, select, [contenteditable='true']"));

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") {
    if (document.getElementById("app")?.hidden) return; // signed out: nothing to search
    e.preventDefault();
    openPalette();
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey || e.defaultPrevented || typing(e.target)) return;
  if (document.querySelector("dialog[open]") || document.querySelector(".board-menu[open]")) return;
  if (e.key === "?") { e.preventDefault(); openHelp(); return; }
  if (page && page.node.isConnected && page.handle(e)) e.preventDefault();
});

let help = null;
/** The list of every shortcut that works on this page. */
export function openHelp() {
  if (!help) {
    help = el("dialog", { class: "narrow keys-dlg", "aria-labelledby": "keysTitle" },
      el("div", { class: "dlg-head" }, el("h2", { id: "keysTitle", text: "Keyboard shortcuts" }),
        el("button", { class: "x", type: "button", "data-close": "", "aria-label": "Close", text: "×" })),
      el("div", { class: "dlg-body" }));
    document.body.append(help);
  }
  const table = (rows) => el("table", { class: "keys-table" }, el("tbody", {}, ...rows.map(([k, what]) =>
    el("tr", {}, el("th", { scope: "row" }, ...k.split("  /  ").flatMap((alt, i) => [i ? " or " : "",
      ...alt.split(" ").map((part) => el("kbd", { text: part }))])), el("td", { text: what })))));
  const here = page && page.node.isConnected ? page : null;
  fill(help.querySelector(".dlg-body"),
    here ? el("h3", { text: here.title }) : null, here ? table(here.list) : null,
    el("h3", { text: "Everywhere" }), table(GLOBAL));
  help.showModal();
}
