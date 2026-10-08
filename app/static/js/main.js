// Boot: config, analytics and error reporting, email links, then the landing page or the app.

import { S, api, initAnalytics, initErrorReporting, toast } from "./core.js";
import { captureEmailLink, handleEmailLinks, hasCoachInvite, initLanding, openAuth, showLanding } from "./landing.js";
import { startApp } from "./shell.js";
import { initSupport } from "./support.js";

captureEmailLink(); // first: takes one-time tokens out of the URL before anything else can see it
initErrorReporting();
initSupport();

async function signedIn(opts) {
  try { await startApp(opts, signedOut); }
  catch (e) { if (e.status === 401) signedOut(); else throw e; }
}
function signedOut() {
  S.me = null; S.accountId = null; S.profile = null; S.chat = []; S.chatLog = [];
  history.replaceState(null, "", "/");
  showLanding();
}

async function boot() {
  try { S.config = await api("/api/config"); } catch { S.config = { platforms: ["lichess"], analytics: { provider: "none" } }; }
  initAnalytics(S.config.analytics);
  initLanding(signedIn);
  await handleEmailLinks(); // opens the "new password" dialog for /reset links
  try {
    S.me = await api("/api/me");
    await signedIn();
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
