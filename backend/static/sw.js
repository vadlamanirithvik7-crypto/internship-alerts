/* Public demo only. Never cache private workspace responses. */
const CACHE = "radar-expo-v1";
const BASIC = [
  "/",
  "/applications",
  "/profile",
  "/profile?profile=2",
  "/profile?profile=3",
  "/system",
  "/companies",
  "/filters",
  "/about",
  "/static/style.css",
  "/static/app.js",
  "/static/icon.svg",
  "/static/offline.html",
  "/manifest.webmanifest",
];
const ROUTES = [...BASIC];
for (let profile = 1; profile <= 3; profile++) {
  ROUTES.push(`/?profile=${profile}`);
  for (let id = 1; id <= 18; id++)
    ROUTES.push(`/jobs/${id}?profile=${profile}`);
}
for (let id = 1; id <= 18; id++) ROUTES.push(`/jobs/${id}`);
self.addEventListener("install", (event) =>
  event.waitUntil(
    (async () => {
      const cache = await caches.open(CACHE);
      for (const path of ROUTES) {
        const response = await fetch(path);
        if (!response.ok || response.headers.get("X-Radar-Mode") !== "demo")
          throw new Error("Demo response required");
        await cache.put(path, response);
      }
      await self.skipWaiting();
    })(),
  ),
);
self.addEventListener("activate", (event) =>
  event.waitUntil(
    (async () => {
      for (const key of await caches.keys())
        if (key.startsWith("radar-expo-") && key !== CACHE)
          await caches.delete(key);
      await self.clients.claim();
      for (const client of await self.clients.matchAll())
        client.postMessage("radar-offline-ready");
    })(),
  ),
);
self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET" || new URL(req.url).origin !== self.location.origin)
    return;
  event.respondWith(
    (async () => {
      const cache = await caches.open(CACHE);
      if (new URL(req.url).pathname.startsWith("/static/")) {
        const asset = await cache.match(req);
        if (asset) return asset;
      }
      try {
        const response = await Promise.race([
          fetch(req),
          new Promise((_, reject) =>
            setTimeout(() => reject(new Error("offline")), 2500),
          ),
        ]);
        if (response.ok && response.headers.get("X-Radar-Mode") === "demo")
          await cache.put(req, response.clone());
        return response;
      } catch {
        return (
          (await cache.match(req)) ||
          (req.mode === "navigate"
            ? await cache.match("/static/offline.html")
            : Response.error())
        );
      }
    })(),
  );
});
