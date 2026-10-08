// "Contact support": works signed in or out; messages are stored and emailed to SUPPORT_EMAIL.

import { $, S, el, openDialog, post, showErr, track } from "./core.js";

export function openSupport(subject = "") {
  const signedIn = !!S.me;
  $("#supportEmailField").hidden = signedIn;
  $("#supportLead").textContent = signedIn ? `We'll reply to ${S.me.email}.` : "Tell us what happened and we'll reply by email.";
  if (S.config && S.config.support_email) {
    $("#supportLead").append(" Or email ", el("a", { href: `mailto:${S.config.support_email}`, text: S.config.support_email }), ".");
  }
  $("#supportSubject").value = subject;
  showErr($("#supportErr"), "");
  $("#supportOk").hidden = true;
  $("#supportForm").hidden = false;
  $("#supportEmail").toggleAttribute("autofocus", !signedIn);
  $("#supportSubject").toggleAttribute("autofocus", signedIn);
  openDialog($("#supportDlg"));
}

export function initSupport() {
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-support]");
    if (t) { e.preventDefault(); openSupport(); }
  });
  $("#supportForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = $("#supportBtn");
    showErr($("#supportErr"), "");
    btn.disabled = true;
    try {
      const r = await post("/api/support", { email: $("#supportEmail").value.trim() || null, subject: $("#supportSubject").value.trim(),
        message: $("#supportMsg").value.trim(), page: location.pathname + location.hash });
      $("#supportMsg").value = "";
      const ok = $("#supportOk"); ok.textContent = r.message; ok.hidden = false;
      track("Support message");
    } catch (ex) { showErr($("#supportErr"), ex.status === 422 ? "Add a subject and a message of at least a few words." : ex.message); }
    finally { btn.disabled = false; }
  });
}
