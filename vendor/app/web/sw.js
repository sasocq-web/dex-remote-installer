const CACHE = "dex-remote-v1.0.0-0-modern-workbench0151-execution-progress275-steam-remote-recovery284-secure-payment-card294-gnome-codex3-draft-recovery295";
const ASSETS = [
  "/",
  "/index.html",
  "/styles.css?v=secure-payment-card-20260902-294-portable",
  "/workbench.css?v=modern-workbench-20260830-4",
  "/app.js?v=draft-recovery-20260903-295-portable",
  "/sites.js?v=dex-fast-open-20260902-290",
  "/automations.js?v=pc-managers-20260824-229b",
  "/release-status.json",
  "/operations.js?v=draft-recovery-20260903-295",
  "/workbench.js?v=modern-workbench-20260830-4",
  "/manifest.webmanifest",
  "/icons/codex-remoto-192.png",
  "/icons/codex-remoto-512.png",
];
self.addEventListener("install", event => event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(ASSETS)).then(() => self.skipWaiting())));
self.addEventListener("activate", event => event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key)))).then(() => self.clients.claim())));
self.addEventListener("fetch", event => {
  const path = new URL(event.request.url).pathname;
  if (event.request.method !== "GET" || path.startsWith("/api/") || path === "/release-progress.json" || path.startsWith("/novnc/") || path.startsWith("/remote-viewer")) return;
  event.respondWith(fetch(event.request).then(response => {
    const copy = response.clone();
    caches.open(CACHE).then(cache => cache.put(event.request, copy));
    return response;
  }).catch(() => caches.match(event.request).then(hit => hit || caches.match("/"))));
});

self.addEventListener("push", event => {
  event.waitUntil((async () => {
    let payload = {};
    try { payload = event.data?.json() || {}; }
    catch { payload = {title:"Dex Remoto", body:event.data?.text() || "Atividade concluída."}; }
    const windows = await self.clients.matchAll({type:"window", includeUncontrolled:true});
    if (windows.some(client => client.visibilityState === "visible" || client.focused)) {
      windows.forEach(client => client.postMessage({kind:"push-completion", payload}));
      return;
    }
    await self.registration.showNotification(payload.title || "Dex Remoto", {
      body:payload.body || "Atividade concluída.",
      icon:payload.icon || "/icons/codex-remoto-192.png",
      badge:payload.badge || "/icons/codex-remoto-192.png",
      tag:payload.tag || "dex-activity-completed",
      renotify:false,
      data:{url:payload.url || "/", threadId:payload.threadId || "", projectId:payload.projectId || ""},
    });
  })());
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  event.waitUntil((async () => {
    const data = event.notification.data || {};
    const targetUrl = new URL(data.url || "/", self.location.origin);
    const payload = {
      url:targetUrl.href,
      threadId:data.threadId || targetUrl.searchParams.get("thread") || "",
      projectId:data.projectId || targetUrl.searchParams.get("project") || "",
    };
    const windows = await self.clients.matchAll({type:"window", includeUncontrolled:true});
    const client = windows.find(item => new URL(item.url).origin === self.location.origin);
    if (client) {
      client.postMessage({kind:"open-notification-target", payload});
      let destination = client;
      try { destination = await client.navigate(payload.url) || client; }
      catch {
        destination = await self.clients.openWindow(payload.url).catch(() => null) || client;
      }
      return destination.focus();
    }
    return self.clients.openWindow(payload.url);
  })());
});
