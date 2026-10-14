// Scan: an over-the-board scoresheet (a photo, or the moves typed in any language) -> a legal game you confirm
// on the board -> saved to your games (analysed like any other) and/or your Opponent Library.

import { S, api, el, fill, isPro, post, svg, toast, track, upload } from "../core.js";
import { detectCorners, enhance, halves, loadImage, toBlob, warp } from "../sheetcv.js";
import { Board, boardSettings, sideToMove, uciArrow } from "../board.js";
import { pollJob, refreshMe, selectAccount } from "../shell.js";

const CONF_TEXT = { sure: "read clearly", check: "worth a look", guess: "a guess: please check" };

export async function render(main) {
  const head = el("div", { class: "page-head" }, el("div", {}, el("h1", { text: "Scan a scoresheet" }),
    el("p", { class: "sub", text: "Photograph an over-the-board scoresheet, or type the moves in any language. We read it, repair it with the rules of chess, and you confirm anything unclear on the board." })));
  const body = el("div", { class: "stack-lg" });
  main.replaceChildren(head, body);
  let info;
  try { info = await api("/api/scan"); } catch (e) { body.replaceChildren(el("p", { class: "err", text: e.message })); return; }
  const state = { tokens: null, decoded: null, locked: {}, sheet: {}, sel: null, language: "auto" };
  const review = el("div");
  body.replaceChildren(inputCard(info, state, review), review);
}

// ---------------- step 1: the photo or the typed moves ----------------
function inputCard(info, state, review) {
  const err = el("p", { class: "err", role: "alert", hidden: true });
  const busy = el("p", { class: "small muted", hidden: true });
  const showErr = (m) => { err.textContent = m || ""; err.hidden = !m; };

  // photo
  async function sendPhoto(blob) {
    showErr("");
    let prepared;
    try { prepared = await adjustPhoto(blob); }
    catch { prepared = { full: await shrink(blob) }; } // an image the browser can't process: send it as it is
    if (!prepared) return; // cancelled
    busy.hidden = false;
    busy.textContent = "Reading the scoresheet: every move is read, checked against the rules of chess, and the unclear ones read again… (about 20–40 seconds)";
    try {
      const file = (b, name) => (b ? new File([b], name, { type: b.type || "image/jpeg" }) : null);
      const r = await upload("/api/scan/photo", { file: file(prepared.full, "scoresheet.jpg"), left: file(prepared.left, "left.jpg"),
        right: file(prepared.right, "right.jpg") });
      track("Scoresheet photo", { cleaned: !!prepared.cleaned });
      Object.assign(state, { tokens: r.tokens, priors: r.priors, decoded: r.decoded, locked: {}, sheet: r.sheet, sel: null, source: "photo",
        model: r.model, checked: r.checked });
      info.left_today = Math.max(0, info.left_today - 1);
      renderReview(review, state, info);
      review.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) { showErr(e.message); }
    finally { busy.hidden = true; }
  }
  const pick = (capture) => {
    const input = el("input", { type: "file", accept: "image/*", hidden: true, ...(capture ? { capture: "environment" } : {}) });
    input.addEventListener("change", () => {
      const f = input.files[0];
      input.value = "";
      if (f) sendPhoto(f);
    });
    return input;
  };
  // "Take a photo" opens the camera inside the page (laptop webcams and phones alike). Browsers without
  // camera access fall back to the phone's camera app through the file input.
  const camIn = pick(true), fileIn = pick(false);
  const photoOk = info.photo && !info.paused && info.left_today > 0;
  const camBtn = el("button", { class: "btn", type: "button", text: "Take a photo", disabled: !photoOk,
    onclick: () => openCamera({ onPhoto: sendPhoto, fallback: () => camIn.click(), onError: showErr }) });
  const fileBtn = el("button", { class: "btn quiet", type: "button", text: "Choose a photo", disabled: !photoOk, onclick: () => fileIn.click() });
  const photoNote = !info.photo ? "Reading photos isn't set up on this server yet. Typing works in every language."
    : info.paused ? "Reading photos is paused for maintenance. Typing still works."
      : info.left_today > 0 ? `${info.left_today} of ${info.per_day} photo${info.per_day === 1 ? "" : "s"} left today${isPro() ? "" : " (Pro: 30)"}. The photo is read, then thrown away.`
        : `You've used today's ${info.per_day} photo${info.per_day === 1 ? "" : "s"}. Typing the moves is unlimited.`;
  const photo = el("div", { class: "scan-option" },
    el("h2", { text: "From a photo" }),
    el("p", { class: "small muted", text: "Lay the sheet flat, in good light, with every move in the frame. Both columns, top to bottom." }),
    el("div", { class: "row" }, camBtn, fileBtn, camIn, fileIn),
    el("p", { class: "tiny faint", text: photoNote }));

  // typed
  const ta = el("textarea", { id: "scanText", rows: "6", placeholder: "1. e4 e5 2. Nf3 Nc6 3. Bb5 a6 …\nAny language: 2. Sf3 Sc6 3. Lb5 (German), 2. Cf3 Cc6 3. Fb5 (French)…", spellcheck: "false" });
  const lang = el("select", { id: "scanLang", "aria-label": "Notation language" }, el("option", { value: "auto", text: "Detect the language" }),
    ...Object.entries(info.languages).map(([k, v]) => el("option", { value: k, text: v })));
  const read = el("button", { class: "btn quiet", type: "button", text: "Read the moves" });
  const doRead = async (quiet) => {
    const text = ta.value.trim();
    if (!text) { if (!quiet) showErr("Type some moves first."); return; }
    showErr("");
    try {
      const r = await post("/api/scan/decode", { text, language: lang.value });
      Object.assign(state, { tokens: r.tokens, priors: null, decoded: r.decoded, locked: {}, sel: null, source: "typed", language: lang.value });
      renderReview(review, state, info);
    } catch (e) { if (!quiet) showErr(e.message); }
  };
  read.addEventListener("click", () => { track("Scoresheet typed"); doRead(false); });
  let t = null;
  ta.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => doRead(true), 900); });
  lang.addEventListener("change", () => doRead(true));
  const typed = el("div", { class: "scan-option" },
    el("h2", { text: "Or type the moves" }),
    el("div", { class: "field" }, el("label", { for: "scanText", class: "sr", text: "Moves" }), ta),
    el("div", { class: "row" }, lang, read),
    el("p", { class: "tiny faint", text: "Write them as they are on the sheet. Slips, missing x's, 0-0 and long notation (e2-e4) are all fine." }));
  return el("section", { class: "sheet scan-input" }, el("div", { class: "scan-options" }, photo, typed), busy, err);
}

/** A live camera view in a dialog: frame the sheet, take the photo, check it, retake or use it. */
async function openCamera({ onPhoto, fallback, onError }) {
  if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { fallback(); return; }
  const video = el("video", { class: "cam-video", autoplay: true, playsinline: true, muted: true });
  video.muted = true; // iOS needs the property, not just the attribute, to play inline
  const still = el("canvas", { class: "cam-still", hidden: true });
  const status = el("p", { class: "small muted", role: "status", text: "Starting the camera…" });
  const shutter = el("button", { class: "btn cam-shutter", type: "button", text: "Take photo", disabled: true });
  const flip = el("button", { class: "btn quiet small", type: "button", text: "Switch camera", hidden: true });
  const retake = el("button", { class: "btn quiet", type: "button", text: "Retake", hidden: true });
  const use = el("button", { class: "btn", type: "button", text: "Read this sheet", hidden: true });
  const closeBtn = el("button", { class: "x", type: "button", "aria-label": "Close the camera", text: "×" });
  const dlg = el("dialog", { class: "cam-dlg", "aria-labelledby": "camTitle" },
    el("div", { class: "dlg-head" }, el("h2", { id: "camTitle", text: "Photograph the scoresheet" }), closeBtn),
    el("div", { class: "dlg-body stack" },
      el("div", { class: "cam-stage" }, video, still, el("div", { class: "cam-guide", "aria-hidden": "true" })),
      status, el("div", { class: "row cam-controls" }, shutter, retake, use, flip)));
  document.body.append(dlg);
  let stream = null, facing = "environment", photo = null;
  const stop = () => { if (stream) stream.getTracks().forEach((t) => t.stop()); stream = null; };
  dlg.addEventListener("close", () => { stop(); dlg.remove(); }); // the camera light goes off however it's closed
  closeBtn.addEventListener("click", () => dlg.close());

  async function start() {
    stop();
    shutter.disabled = true;
    status.textContent = "Starting the camera… If your browser asks, choose Allow.";
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: false,
        video: { facingMode: { ideal: facing }, width: { ideal: 2560 }, height: { ideal: 1440 } } });
    } catch (e) {
      dlg.close();
      if (e.name === "NotAllowedError" || e.name === "SecurityError") {
        onError("Camera access is blocked for this site. Allow it (the camera icon in the address bar, or your browser's site settings), or use Choose a photo.");
      } else if (e.name === "NotFoundError" || e.name === "OverconstrainedError") {
        onError("No camera found on this device. Use Choose a photo instead.");
      } else {
        fallback();
      }
      return;
    }
    if (!dlg.isConnected) { stop(); return; } // closed while the browser was asking for permission
    video.srcObject = stream;
    video.classList.toggle("mirror", facing === "user");
    await video.play().catch(() => {});
    status.textContent = "Fit the whole sheet inside the frame, flat and in good light. Then take the photo.";
    shutter.disabled = false;
    shutter.focus();
    try {
      const cams = (await navigator.mediaDevices.enumerateDevices()).filter((d) => d.kind === "videoinput");
      flip.hidden = cams.length < 2;
    } catch { flip.hidden = true; }
  }

  shutter.addEventListener("click", () => {
    const w = video.videoWidth, h = video.videoHeight;
    if (!w || !h) return;
    const scale = Math.min(1, 2000 / Math.max(w, h));
    still.width = Math.round(w * scale);
    still.height = Math.round(h * scale);
    still.getContext("2d").drawImage(video, 0, 0, still.width, still.height);
    photo = new Promise((res) => still.toBlob(res, "image/jpeg", 0.88));
    stop(); // freeze: the camera light goes off while you check the photo
    video.hidden = true; still.hidden = false;
    shutter.hidden = true; flip.hidden = true; retake.hidden = false; use.hidden = false;
    status.textContent = "Can you read every move? If not, retake it.";
    use.focus();
  });
  retake.addEventListener("click", () => {
    photo = null;
    video.hidden = false; still.hidden = true;
    shutter.hidden = false; retake.hidden = true; use.hidden = true;
    start();
  });
  use.addEventListener("click", async () => {
    const blob = photo && (await photo);
    if (!blob) return;
    dlg.close();
    onPhoto(blob);
  });
  flip.addEventListener("click", () => { facing = facing === "environment" ? "user" : "environment"; start(); });
  dlg.showModal();
  start();
}

/** "Fit the corners to the sheet": the detected sheet outline with four draggable corners, a live preview of the
 *  flattened, cleaned page, and the choice to send it cleaned (recommended) or as photographed. Resolves to
 *  {full, left, right, cleaned} blobs, or null if cancelled. */
async function adjustPhoto(blob) {
  const img = await loadImage(blob);
  const W = img.width, H = img.height;
  let corners = detectCorners(img);
  const view = document.createElement("canvas");
  const vs = Math.min(1, 1100 / Math.max(W, H));
  view.width = Math.round(W * vs); view.height = Math.round(H * vs);
  view.getContext("2d").drawImage(img, 0, 0, view.width, view.height);
  view.className = "adj-photo";
  const poly = svg("polygon", { class: "adj-quad" });
  const r = Math.max(W, H) * 0.022;
  const handles = corners.map((_, i) => svg("circle", { class: "adj-handle", r, tabindex: "0", role: "slider",
    "aria-label": ["Top-left corner", "Top-right corner", "Bottom-right corner", "Bottom-left corner"][i] }));
  const overlay = svg("svg", { class: "adj-overlay", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none" }, poly, ...handles);
  const preview = el("canvas", { class: "adj-preview" });
  const clean = el("input", { type: "checkbox", id: "adjClean", checked: true });
  const note = el("p", { class: "tiny faint", text: "Drag the corners onto the sheet's corners. The preview shows what the reader will see." });
  const go = el("button", { class: "btn", type: "button", text: "Read this sheet" });
  const whole = el("button", { class: "btn quiet small", type: "button", text: "Use the whole photo" });
  const cancel = el("button", { class: "btn quiet small", type: "button", text: "Cancel" });
  const dlg = el("dialog", { class: "adj-dlg", "aria-labelledby": "adjTitle" },
    el("div", { class: "dlg-head" }, el("h2", { id: "adjTitle", text: "Fit the corners to the sheet" }),
      el("button", { class: "x", type: "button", "aria-label": "Close", text: "×", onclick: () => dlg.close() })),
    el("div", { class: "dlg-body adj-body" },
      el("div", { class: "adj-stage" }, view, overlay),
      el("div", { class: "stack adj-side" }, el("p", { class: "small faint", text: "Preview" }), preview,
        el("label", { class: "check", for: "adjClean" }, clean, "Remove shadows and boost contrast (recommended)"), note)),
    el("div", { class: "dlg-foot" }, go, whole, cancel)); // always in view, even on a phone
  document.body.append(dlg);

  const draw = () => {
    poly.setAttribute("points", corners.map((c) => c.join(",")).join(" "));
    handles.forEach((h, i) => { h.setAttribute("cx", corners[i][0]); h.setAttribute("cy", corners[i][1]); });
  };
  let timer = null;
  const refresh = () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      const small = warp(img, corners, 520);
      const shown = clean.checked ? enhance(small) : small;
      preview.width = shown.width; preview.height = shown.height;
      preview.getContext("2d").drawImage(shown, 0, 0);
    }, 60);
  };
  const toImage = (e) => {
    const pt = overlay.createSVGPoint();
    pt.x = e.clientX; pt.y = e.clientY;
    const p = pt.matrixTransform(overlay.getScreenCTM().inverse());
    return [Math.max(0, Math.min(W, p.x)), Math.max(0, Math.min(H, p.y))];
  };
  handles.forEach((h, i) => {
    h.addEventListener("pointerdown", (e) => { e.preventDefault(); h.setPointerCapture(e.pointerId); h.classList.add("drag"); });
    h.addEventListener("pointermove", (e) => { if (!h.hasPointerCapture(e.pointerId)) return; corners[i] = toImage(e); draw(); });
    h.addEventListener("pointerup", (e) => { h.releasePointerCapture(e.pointerId); h.classList.remove("drag"); refresh(); });
    h.addEventListener("keydown", (e) => { // arrow keys nudge a corner
      const step = Math.max(W, H) / 100, d = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
      if (!d) return;
      e.preventDefault();
      corners[i] = [Math.max(0, Math.min(W, corners[i][0] + d[0])), Math.max(0, Math.min(H, corners[i][1] + d[1]))];
      draw(); refresh();
    });
  });
  clean.addEventListener("change", refresh);
  whole.addEventListener("click", () => { corners = [[0, 0], [W, 0], [W, H], [0, H]]; draw(); refresh(); });
  draw();
  dlg.showModal();
  refresh();
  return await new Promise((resolve) => {
    let done = false;
    dlg.addEventListener("close", () => { dlg.remove(); if (!done) resolve(null); });
    cancel.addEventListener("click", () => dlg.close());
    go.addEventListener("click", async () => {
      go.disabled = true;
      go.textContent = "Preparing…";
      await new Promise((r2) => setTimeout(r2, 30)); // let the button repaint before the heavy work
      const flat = warp(img, corners, 2000);
      const page = clean.checked ? enhance(flat) : flat;
      const [l, rr] = halves(page);
      const out = { full: await toBlob(page), left: await toBlob(l), right: await toBlob(rr), cleaned: clean.checked };
      done = true;
      dlg.close();
      resolve(out);
    });
  });
}

/** Make a phone photo small enough to send quickly (longest side 2000 px, JPEG). */
async function shrink(file) {
  let bmp;
  try { bmp = await createImageBitmap(file); } catch { return file; }
  const scale = Math.min(1, 2000 / Math.max(bmp.width, bmp.height));
  if (scale === 1 && file.size < 3_500_000 && /jpe?g|png|webp/.test(file.type)) return file;
  const c = document.createElement("canvas");
  c.width = Math.round(bmp.width * scale);
  c.height = Math.round(bmp.height * scale);
  c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
  return await new Promise((res) => c.toBlob((b) => res(b || file), "image/jpeg", 0.85));
}

// ---------------- step 2: confirm the moves on the board ----------------
function renderReview(host, state, info) {
  const d = state.decoded;
  if (!d || !d.moves.length) {
    host.replaceChildren(el("div", { class: "empty" }, el("h3", { text: "No legal moves yet" }),
      el("p", { text: "The first move couldn't be read as a legal move. Check the first few moves and try again." })));
    return;
  }
  const boardHost = el("div", { class: "pz-board" });
  const board = new Board(boardHost, { interactive: true, locked: true, label: "Scoresheet board", onMove: (_f, _t, mv) => playedFix(mv) });
  const sheetBox = el("div", { class: "scoresheet scan-sheet" });
  const fix = el("div", { class: "scan-fix", "aria-live": "polite" });
  const summary = el("p", { class: "small" });
  const notes = el("ul", { class: "notes small" });
  const nextBtn = el("button", { class: "btn quiet small", type: "button", text: "Next to check" });
  const nav = el("div", { class: "row scan-nav" });
  let view = d.moves.length; // plies shown on the board

  const firstUnsure = (from = -1) => {
    const ms = state.decoded.moves;
    for (let k = 1; k <= ms.length; k++) {
      const i = (from + k) % ms.length;
      if (ms[i].confidence !== "sure" && !(ms[i].token in state.locked)) return i;
    }
    return null;
  };
  nextBtn.addEventListener("click", () => { const i = firstUnsure(state.sel ?? -1); if (i != null) select(i); else toast("Nothing left to check."); });

  function drawSheet() {
    const ms = state.decoded.moves;
    const rows = [];
    for (let i = 0; i < ms.length; i += 2) {
      const cells = [el("td", { text: String(i / 2 + 1) })];
      for (const k of [i, i + 1]) {
        if (k >= ms.length) { cells.push(el("td")); continue; }
        const m = ms[k];
        const locked = m.token in state.locked;
        const conf = locked ? "locked" : m.confidence;
        const b = el("button", { type: "button", class: `conf-${conf}`, "aria-current": String(state.sel === k),
          "aria-label": `Move ${Math.floor(k / 2) + 1}${k % 2 ? " black" : ""}: ${m.san}, ${locked ? "confirmed" : CONF_TEXT[m.confidence]}${m.raw && m.raw !== m.san ? `, written ${m.raw}` : ""}`,
          onclick: () => select(k) }, el("span", { text: m.san }),
        m.raw && m.read !== m.san.replace(/[+#=]/g, "") && m.raw !== m.san ? el("small", { text: m.raw }) : null);
        cells.push(el("td", {}, b));
      }
      rows.push(el("tr", {}, ...cells));
    }
    sheetBox.replaceChildren(el("table", {}, el("tbody", {}, ...rows)));
    const c = state.decoded.counts;
    const confirmed = Object.keys(state.locked).length;
    const toCheck = state.decoded.moves.filter((m) => m.confidence !== "sure" && !(m.token in state.locked)).length;
    fill(summary, el("b", { text: `${ms.length} moves` }), ` read as ${state.decoded.language_name} notation: ${c.sure} clear`,
      toCheck ? `, ${toCheck} to check` : "", confirmed ? `, ${confirmed} confirmed by you` : "", ".");
    nextBtn.hidden = !toCheck;
    const n = [];
    if (state.decoded.skipped.length) n.push(el("li", { text: `${state.decoded.skipped.length} entr${state.decoded.skipped.length === 1 ? "y" : "ies"} couldn't be placed in a legal game and ${state.decoded.skipped.length === 1 ? "was" : "were"} skipped (e.g. "${state.tokens[state.decoded.skipped[0]][0]}").` }));
    if (state.decoded.unused) n.push(el("li", { text: `The game ended (checkmate or stalemate) before the last ${state.decoded.unused} entr${state.decoded.unused === 1 ? "y" : "ies"} on the sheet.` }));
    notes.replaceChildren(...n);
    notes.hidden = !n.length;
  }

  function show(plies, arrows = []) {
    view = Math.max(0, Math.min(state.decoded.moves.length, plies));
    const ms = state.decoded.moves;
    board.set({ fen: state.decoded.fens[view], lastMove: view ? ms[view - 1].uci : null, arrows, locked: true });
  }
  nav.replaceChildren(
    el("button", { class: "btn quiet small", type: "button", "aria-label": "Start", text: "⏮", onclick: () => { state.sel = null; drawSheet(); fix.replaceChildren(); show(0); } }),
    el("button", { class: "btn quiet small", type: "button", "aria-label": "Back", text: "◀", onclick: () => { state.sel = null; drawSheet(); fix.replaceChildren(); show(view - 1); } }),
    el("button", { class: "btn quiet small", type: "button", "aria-label": "Forward", text: "▶", onclick: () => { state.sel = null; drawSheet(); fix.replaceChildren(); show(view + 1); } }),
    el("button", { class: "btn quiet small", type: "button", "aria-label": "End", text: "⏭", onclick: () => { state.sel = null; drawSheet(); fix.replaceChildren(); show(state.decoded.moves.length); } }));

  // Select a move: the position before it, its reading, the alternatives, and "play it on the board".
  function select(k) {
    state.sel = k;
    drawSheet();
    const m = state.decoded.moves[k];
    show(k, [uciArrow(m.uci, "you")]);
    board.set({ locked: false }); // play a different move to correct it
    const who = `Move ${Math.floor(k / 2) + 1}, ${sideToMove(state.decoded.fens[k])}`;
    const alts = m.alternatives.filter((a) => a.uci !== m.uci).slice(0, 4);
    fill(fix,
      el("p", {}, el("b", { text: who }), m.raw ? ` · written "${m.raw}"` : " · blank on the sheet", ` · read as `, el("b", { text: m.san }),
        m.token in state.locked ? el("span", { class: "tag ok", text: "confirmed" }) : el("span", { class: `tag ${m.confidence === "guess" ? "red" : ""}`, text: CONF_TEXT[m.confidence] })),
      el("div", { class: "row" },
        el("button", { class: "btn small", type: "button", text: `${m.san} is right`, onclick: () => confirmMove(k) }),
        ...alts.map((a) => el("button", { class: "chip", type: "button", text: a.san, onclick: () => choose(k, a.uci) }))),
      el("p", { class: "tiny faint", text: "Or play the right move on the board. Everything after it is re-read to fit." }));
  }

  function confirmMove(k) {
    const m = state.decoded.moves[k];
    state.locked[m.token] = m.uci;
    drawSheet();
    const i = firstUnsure(k);
    if (i != null) select(i); else { fix.replaceChildren(el("p", { class: "small ok-text", text: "All moves confirmed or clear." })); show(state.decoded.moves.length); }
  }

  async function choose(k, uci) {
    const ms = state.decoded.moves;
    const tok = ms[k].token;
    const locked = { ...state.locked };
    for (let i = 0; i < k; i++) if (!(ms[i].token in locked)) locked[ms[i].token] = ms[i].uci; // everything before stays as it is
    locked[tok] = uci;
    try {
      const r = await post("/api/scan/decode", { tokens: state.tokens, priors: state.priors || null, locked, language: state.decoded.language });
      state.decoded = r.decoded;
      const kept = new Set(r.decoded.moves.map((m) => m.token));
      state.locked = Object.fromEntries(Object.entries(state.locked).filter(([t]) => kept.has(Number(t))));
      state.locked[tok] = uci;
      track("Scoresheet fix");
      drawSheet();
      const i = firstUnsure(k);
      if (i != null) select(i); else { state.sel = null; drawSheet(); fix.replaceChildren(el("p", { class: "small ok-text", text: "All moves confirmed or clear." })); show(k + 1); }
    } catch (e) { toast(e.message); }
  }
  function playedFix(mv) {
    if (state.sel == null) return;
    choose(state.sel, mv.uci);
  }

  const save = saveCard(state, info);
  host.replaceChildren(el("section", { class: "stack" }, el("h2", { text: "Check the moves" }),
    el("div", { class: "pz scan-review" },
      el("div", { class: "pz-left" }, el("div", { class: "pz-boardrow" }, boardHost),
        el("div", { class: "board-foot" }, nav, boardSettings(board))),
      el("aside", { class: "pz-panel sheet" }, summary,
        el("div", { class: "legend-ink scan-legend" }, el("span", {}, el("i", { class: "k-sure" }), "clear"), el("span", {}, el("i", { class: "k-check" }), "worth a look"),
          el("span", {}, el("i", { class: "k-guess" }), "a guess"), el("span", {}, el("i", { class: "k-locked" }), "confirmed")),
        sheetBox, el("div", { class: "row" }, nextBtn), fix, notes))), save);
  drawSheet();
  const first = firstUnsure();
  if (first != null) select(first); else show(state.decoded.moves.length);
}

// ---------------- step 3: save ----------------
function saveCard(state, info) {
  const sh = state.sheet || {};
  const ms = state.decoded.moves;
  const lastSan = ms.length ? ms[ms.length - 1].san : "";
  const mateResult = lastSan.endsWith("#") ? (ms.length % 2 ? "1-0" : "0-1") : "";
  const accounts = info.pgn_accounts || [];
  const myName = (n) => accounts.some((a) => a.handle.toLowerCase() === (n || "").toLowerCase());
  let color = myName(sh.black) && !myName(sh.white) ? "black" : "white";
  const seg = el("div", { class: "seg", role: "radiogroup", "aria-label": "Your colour" });
  const segBtns = ["white", "black"].map((c) => el("button", { type: "button", role: "radio", "aria-checked": String(c === color), text: c === "white" ? "White" : "Black",
    onclick: () => { color = c; for (const b of segBtns) b.setAttribute("aria-checked", String(b.textContent.toLowerCase() === c)); swapNames(); } }));
  seg.append(...segBtns);
  const you = el("input", { type: "text", id: "scanYou", maxlength: "60", value: (color === "white" ? sh.white : sh.black) || (accounts[0] || {}).handle || "" });
  const opp = el("input", { type: "text", id: "scanOpp", maxlength: "60", value: (color === "white" ? sh.black : sh.white) || "" });
  function swapNames() { const a = you.value; you.value = opp.value; opp.value = a; }
  const result = el("select", { id: "scanResult" }, el("option", { value: "", text: "Choose…" }),
    ...[["1-0", "1-0 (White won)"], ["0-1", "0-1 (Black won)"], ["1/2-1/2", "½-½ (draw)"]].map(([v, t]) => el("option", { value: v, text: t, selected: (sh.result || mateResult) === v })));
  const date = el("input", { type: "date", id: "scanDate", value: isoDate(sh.date) });
  const event = el("input", { type: "text", id: "scanEvent", maxlength: "60", value: sh.event || "", placeholder: "e.g. Club championship, round 3" });
  const where = el("select", { id: "scanWhere" }, ...accounts.map((a) => el("option", { value: String(a.id), text: `My games: ${a.handle}` })),
    el("option", { value: "new", text: accounts.length ? "My games: new account in your name" : "My games" }));
  const lib = el("input", { type: "checkbox", id: "scanLib", disabled: !info.library });
  const err = el("p", { class: "err", role: "alert", hidden: true });
  const out = el("div");
  const saveBtn = el("button", { class: "btn", type: "button", text: "Save and get the report card" });
  const dl = el("button", { class: "btn quiet", type: "button", text: "Download PGN" });
  const field = (id, label, input) => el("div", { class: "field" }, el("label", { for: id, text: label }), input);
  dl.addEventListener("click", () => {
    const res = result.value || "*";
    const w = color === "white" ? you.value : opp.value, b = color === "white" ? opp.value : you.value;
    const clean = (v, d) => (v || "").replace(/["\\[\]]/g, "").trim() || d;
    const pgn = [["Event", clean(event.value, "Over-the-board game")], ["Site", "Over the board"], ["Date", (date.value || "????.??.??").replaceAll("-", ".")],
      ["White", clean(w, "?")], ["Black", clean(b, "?")], ["Result", res]].map(([k, v]) => `[${k} "${v}"]`).join("\n")
      + `\n\n${state.decoded.movetext} ${res}\n`;
    const a = el("a", { href: URL.createObjectURL(new Blob([pgn], { type: "application/x-chess-pgn" })), download: `${clean(w, "white")}-vs-${clean(b, "black")}.pgn`.replace(/\s+/g, "_") });
    document.body.append(a); a.click(); a.remove();
  });
  saveBtn.addEventListener("click", async () => {
    err.hidden = true;
    if (!result.value) { err.textContent = "Choose the result."; err.hidden = false; return; }
    const unsure = state.decoded.moves.filter((m) => m.confidence === "guess" && !(m.token in state.locked)).length;
    if (unsure && saveBtn.dataset.confirm !== "1") {
      saveBtn.dataset.confirm = "1";
      saveBtn.textContent = `Save anyway (${unsure} guessed move${unsure === 1 ? "" : "s"})`;
      err.textContent = "Some moves are still guesses. Check them on the sheet above, or save anyway.";
      err.hidden = false;
      return;
    }
    saveBtn.disabled = true;
    const w = color === "white" ? you.value : opp.value, b = color === "white" ? opp.value : you.value;
    const body = { moves: state.decoded.moves.map((m) => m.uci), color, result: result.value, white: w.trim(), black: b.trim(),
      date: date.value, event: event.value.trim(), target: lib.checked ? "both" : "games" };
    if (where.value === "new") body.name = you.value.trim(); else body.account_id = Number(where.value);
    try {
      const r = await post("/api/scan/save", body);
      track("Scoresheet saved", { library: lib.checked });
      await refreshMe();
      if (r.account_id) { selectAccount(r.account_id, { silent: true }); if (r.job_id) pollJob(r.job_id, r.account_id); }
      fill(out, el("div", { class: "notice blue" }, el("b", { text: "Saved." }),
        r.job_id ? " Stockfish is analysing it now; it joins your Rating DNA, puzzles and Twin like any other game. " : " ",
        r.library_player ? "It's also in your Opponent Library. " : "",
        r.game_id && r.account_id ? el("a", { class: "btn small", href: `#/report/${r.account_id}/${encodeURIComponent(r.game_id)}`, text: "Open the report card" }) : null,
        " ", el("a", { href: "#/diagnose", text: "Open your games" }), " · ",
        el("a", { href: "#/scan", text: "Scan another", onclick: (e) => { e.preventDefault(); window.scrollTo(0, 0); render(document.querySelector("#view")); } })));
      saveBtn.hidden = true;
    } catch (e) { err.textContent = e.message; err.hidden = false; saveBtn.disabled = false; }
  });
  return el("section", { class: "sheet stack scan-save" },
    el("h2", { text: "Save the game" }),
    el("div", { class: "scan-form" },
      el("div", { class: "field" }, el("span", { class: "label", text: "You played" }), seg),
      field("scanYou", "Your name", you), field("scanOpp", "Opponent", opp), field("scanResult", "Result", result),
      field("scanDate", "Date", date), field("scanEvent", "Event", event), field("scanWhere", "Save to", where)),
    el("label", { class: "check" }, lib, "Also add it to my Opponent Library under my opponent's name", info.library ? null : el("span", { class: "tag pro", text: "Pro" })),
    el("div", { class: "row" }, saveBtn, dl), err, out,
    S.me && !S.me.email_verified ? el("p", { class: "tiny faint", text: "Tip: confirm your email if saving asks you to." }) : null);
}

function isoDate(d) {
  const s = (d || "").trim();
  let m = s.match(/^(\d{4})[-./](\d{1,2})[-./](\d{1,2})$/);
  if (m) return `${m[1]}-${m[2].padStart(2, "0")}-${m[3].padStart(2, "0")}`;
  m = s.match(/^(\d{1,2})[-./](\d{1,2})[-./](\d{4})$/);
  if (m) return `${m[3]}-${m[2].padStart(2, "0")}-${m[1].padStart(2, "0")}`;
  return "";
}
