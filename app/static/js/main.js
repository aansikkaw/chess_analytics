// Boot: config, analytics and error reporting, email links, then the landing page or the app.

import { S, api, initAnalytics, initErrorReporting, toast } from "./core.js";
import { captureEmailLink, handleEmailLinks, hasCoachInvite, initLanding, openAuth, showLanding } from "./landing.js";
import { initSupport } from "./support.js";

captureEmailLink(); // first: takes one-time tokens out of the URL before anything else can see it
initErrorReporting();
initSupport();

// The signed-in app loads only once someone is signed in, so the landing page stays light; then the shell and the page
// you're opening are fetched together rather than one import level at a time (other pages load when you go to them).
const VIEWS = ["diagnose", "train", "prepare", "coach", "students", "scan", "report"];
function preloadApp() {
  const area = (location.hash.replace(/^#\/?/, "").split("/")[0]) || "diagnose";
  const mods = ["shell.js", "keys.js", "palette.js", "views/common.js", ...(VIEWS.includes(area) ? [`views/${area}.js`] : [])];
  for (const m of mods) {
    const href = `/static/js/${m}`;
    if (document.querySelector(`link[href="${href}"]`)) continue;
    document.head.append(Object.assign(document.createElement("link"), { rel: "modulepreload", href }));
  }
}

async function signedIn(opts) {
  preloadApp();
  try { const { startApp } = await import("./shell.js"); await startApp(opts, signedOut); }
  catch (e) { if (e.status === 401) signedOut(); else throw e; }
}
function signedOut() {
  S.me = null; S.accountId = null; S.profile = null; S.chat = []; S.chatLog = [];
  history.replaceState(null, "", "/");
  showLanding();
}

async function boot() {
  // Both at once: the config, and a plain yes/no on being signed in (no 401 in a signed-out visitor's console).
  const [config, session] = await Promise.all([
    api("/api/config").catch(() => ({ platforms: ["lichess"], analytics: { provider: "none" } })),
    api("/api/session").catch(() => ({ signed_in: false })),
  ]);
  S.config = config;
  if (session.signed_in) preloadApp(); // start fetching the app while the rest of boot runs
  initAnalytics(S.config.analytics);
  initLanding(signedIn);
  await handleEmailLinks(); // opens the "new password" dialog for /reset links
  try {
    if (!session.signed_in) throw Object.assign(new Error("signed out"), { status: 401 });
    await signedIn({ me: session.me }); // the session check already carried /api/me
  } catch (e) {
    if (e.status !== 401) console.error(e);
    showLanding();
    if (hasCoachInvite()) { // came from a coach's invite: sign up (or in) to join
      toast("Your coach invited you. Create an account, or sign in, to join their squad.", 8000);
      openAuth("signup");
    }
  }
}

boot();

const secure = location.protocol === "https:" || location.hostname === "localhost";
if ("serviceWorker" in navigator && secure) {
  navigator.serviceWorker.register("/sw.js").catch(() => { /* offline support is optional */ });
}
