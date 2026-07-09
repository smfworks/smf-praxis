/* Minimal service worker for PWA install + offline shell shell. */
var CACHE = "praxis-shell-v1";
var ASSETS = [
  "/",
  "/web/shell.css",
  "/web/shell.js",
  "/web/home.css",
  "/web/home.js",
  "/web/friendliness.css",
  "/web/friendliness.js",
  "/web/growth.css",
  "/web/growth.js",
  "/web/icon.svg",
  "/web/manifest.webmanifest"
];

self.addEventListener("install", function (e) {
  e.waitUntil(caches.open(CACHE).then(function (c) { return c.addAll(ASSETS); }));
  self.skipWaiting();
});

self.addEventListener("activate", function (e) {
  e.waitUntil(self.clients.claim());
});

self.addEventListener("fetch", function (e) {
  var url = new URL(e.request.url);
  if (url.pathname.indexOf("/api/") === 0 || url.pathname === "/events") {
    return; // network only for APIs
  }
  e.respondWith(
    caches.match(e.request).then(function (hit) {
      return hit || fetch(e.request).then(function (resp) {
        return resp;
      }).catch(function () {
        return caches.match("/");
      });
    })
  );
});
