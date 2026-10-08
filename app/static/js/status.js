// The public status page: one line per part of the service, refreshed every minute.

import { $, S, api, el, initErrorReporting } from "./core.js";
import { initSupport } from "./support.js";

const NAMES = {
  database: ["Accounts and data", "Sign-in, your games and puzzles"],
  engine: ["Stockfish", "Checking puzzle answers and positions"],
  imports: ["Game imports", "Downloading and analysing games"],
  coach: ["AI coach", "Answers to your questions"],
  backups: ["Backups", "Daily copy of the database"],
  email: ["Email", "Verification and password reset"],
  lichess: ["Lichess", "Fetching games from lichess.org"],
  chesscom: ["Chess.com", "Fetching games from chess.com"],
};
function since(ts) {
  const m = Math.round((Date.now() / 1000 - ts) / 60);
  if (m < 2) return "just now";
  if (m < 60) return `${m} min ago`;
  if (m < 1440) return `${Math.round(m / 60)} h ago`;
  return `${Math.round(m / 1440)} days ago`;
}
const HEADLINE = { ok: "Everything is working", degraded: "Some parts are slow or unavailable", down: "The service is down" };

function detail(key, c) {
  if (key === "imports") return c.queued || c.running ? `${c.running} running, ${c.queued} waiting${c.oldest_wait_s ? `; longest wait ${Math.round(c.oldest_wait_s / 60)} min` : ""}` : "Nothing waiting.";
  if (key === "coach") return c.recent_fallbacks && !c.recent_ai_answers ? "The AI model isn't answering; the built-in coach is standing in." : (c.providers || []).join(", then ");
  if (key === "backups") return c.last_at ? `Last backup ${Math.round(c.age_hours)} hours ago${c.last_restore_test_at ? `; last restore test ${new Date(c.last_restore_test_at * 1000).toLocaleDateString()}` : ""}.` : c.detail || "";
  if (key === "email") {
    if (!c.configured) return "Not set up on this server: emails are saved for the owner instead.";
    if (!c.ok) return c.detail || "Emails aren't going out right now.";
    return c.last_ok_at ? `Sending normally. Last email went out ${since(c.last_ok_at)}.` : "Set up. No emails sent yet.";
  }
  if (c.latency_ms != null) return `Responding in ${c.latency_ms} ms.`;
  return c.detail || "";
}

async function refresh() {
  try {
    const s = await api("/api/status");
    S.config = { support_email: s.support_email };
    $("#dot").className = `status-dot ${s.status === "ok" ? "" : s.status}`;
    $("#headline").textContent = HEADLINE[s.status] || s.status;
    $("#checked").textContent = `Checked ${new Date(s.checked_at * 1000).toLocaleTimeString()}. Version ${s.version}.`;
    $("#checks").replaceChildren(...Object.entries(s.checks).map(([k, c]) => {
      const [name, what] = NAMES[k] || [k, ""];
      return el("li", {}, el("div", {}, el("b", { text: name }), el("span", { class: "small muted", text: ` ${what}` })),
        el("span", { class: `state ${c.configured === false ? "" : c.ok ? "ok" : "bad"}`, text: c.configured === false ? "Not set up" : c.ok ? "Working" : "Problem" }),
        el("span", { class: "detail", text: detail(k, c) }));
    }));
  } catch (e) {
    $("#dot").className = "status-dot down";
    $("#headline").textContent = "Can't reach the service";
    $("#checked").textContent = e.message;
  }
}

initErrorReporting();
initSupport();
refresh();
setInterval(refresh, 60_000);
