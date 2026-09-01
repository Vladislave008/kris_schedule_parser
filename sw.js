/* Service Worker — кэш для офлайн-режима */
const VERSION = "v1.3.0";
const STATIC_CACHE = `schedule-static-${VERSION}`;
const PYODIDE_CACHE = "schedule-pyodide-v1";
const staticAssets = [
  "./",
  "./index.html",
  "./styles.css",
  "./app.js",
  "./parser.py",
  "./manifest.webmanifest",
  "./icon-192.png",
  "./icon-512.png",
  "./icon-maskable.png",
];
const pyodideBase = "https://cdn.jsdelivr.net/pyodide/v314.0.6/full/";

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => cache.addAll(staticAssets))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== STATIC_CACHE && k !== PYODIDE_CACHE)
          .map((k) => caches.delete(k))
      )
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Pyodide runtime: cache-once, потом офлайн используем кэш
  if (url.href.startsWith(pyodideBase)) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        if (cached) return cached;
        return fetch(event.request).then((resp) => {
          if (resp.ok) {
            const clone = resp.clone();
            caches.open(PYODIDE_CACHE).then((c) => c.put(event.request, clone));
          }
          return resp;
        });
      })
    );
    return;
  }

  // Локальные файлы: network-first (всегда свежее из dev), кэш как фолбэк офлайн.
  // Так правки parser.py / app.js сразу подхватываются, не застревая в кэше.
  event.respondWith(
    fetch(event.request)
      .then((resp) => {
        const clone = resp.clone();
        if (resp.ok && STATIC_CACHE) {
          caches.open(STATIC_CACHE).then((c) => c.put(event.request, clone));
        }
        return resp;
      })
      .catch(() => caches.match(event.request).then((r) => r || (event.request.mode === "navigate" ? caches.match("./index.html") : undefined)))
  );
});