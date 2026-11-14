const CACHE_KEY = "scandoc-ai-v1.0.0";
const CORE_ASSETS = [
  "./",
  "./index.html",
  "./manifest.json",
  "./icon.svg",
  "./apple-touch-icon.png",
  "./assets/icon.svg",
  "./data/embedded_data.js"
];

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_KEY).then((cache) => cache.addAll(CORE_ASSETS).catch(() => {}))
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE_KEY).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.pathname.startsWith("/api/") || url.pathname.endsWith("endpoint.json")) {
    return;
  }
  event.respondWith(
    fetch(event.request)
      .then((res) => {
        if (res && res.status === 200) {
          const clone = res.clone();
          caches.open(CACHE_KEY).then((c) => c.put(event.request, clone)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(event.request))
  );
});
