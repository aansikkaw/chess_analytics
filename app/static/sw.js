// Plateau Breaker service worker: makes the app installable and opens instantly.
// Pages and code: network first, cached copy if offline. API calls are never cached.
const VERSION = "pb-v0.6.0";
const SHELL = ["/", "/static/app.css", "/static/js/main.js", "/static/js/core.js", "/static/js/board.js", "/static/js/dna.js",
  "/static/js/landing.js", "/static/js/plans.js", "/static/js/shell.js", "/static/js/support.js", "/static/js/views/common.js",
  "/static/js/views/diagnose.js", "/static/js/views/train.js", "/static/js/views/prepare.js", "/static/js/views/coach.js",
  "/static/js/views/students.js", "/static/vendor/chess.js",
  "/static/fonts/archivo-latin-standard-normal.woff2", "/static/fonts/chess-pieces.woff2", "/static/icons/icon-192.png"];

self.addEventListener("install", (e) => {
  // cache: "reload" skips the browser's HTTP cache, so a new version never stores stale files.
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL.map((u) => new Request(u, { cache: "reload" })))).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.startsWith("/api/")) return;
  const isPage = e.request.mode === "navigate";
  e.respondWith(
    fetch(e.request) // static files are served with Cache-Control: no-cache, so this revalidates
      .then((res) => {
        // Never cache pages with a query string: /reset and /verify links carry one-time tokens.
        if (res.ok && !url.search && (isPage || url.pathname.startsWith("/static/"))) {
          const copy = res.clone();
          caches.open(VERSION).then((c) => c.put(e.request, copy));
        }
        return res;
      })
      .catch(async () => (await caches.match(e.request, { ignoreSearch: isPage })) || (isPage && (await caches.match("/")))
        || new Response("You're offline.", { status: 503, headers: { "Content-Type": "text/plain" } })),
  );
});
