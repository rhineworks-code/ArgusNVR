// CamSpire NVR – Service Worker: App-Installation, Offline-Grundgerüst, Push-Benachrichtigungen
const VERSION = 'camspire-v1.4.0';
const SHELL = ['/', '/static/style.css', '/static/app.js', '/static/settings.js', '/static/history.js', '/static/account.js',
  '/static/icon.svg', '/static/icons/icon-192.png', '/static/icons/badge-96.png'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// Netzwerk zuerst (immer aktuell), Cache nur als Rückfall – API, Video und Live-Bild nie aus dem Cache
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin) return;
  if (url.pathname.startsWith('/api/') || url.pathname.startsWith('/go2rtc/') || url.pathname.startsWith('/media/')) return;
  e.respondWith(
    fetch(e.request).then((r) => {
      if (r.ok && (url.pathname === '/' || url.pathname.startsWith('/static/'))) {
        const copy = r.clone(); caches.open(VERSION).then((c) => c.put(e.request, copy));
      }
      return r;
    }).catch(() => caches.match(e.request).then((r) => r || caches.match('/')))
  );
});

self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { title: 'CamSpire', body: e.data && e.data.text() }; }
  const opts = {
    body: d.body || '',
    icon: '/static/icons/icon-192.png',
    badge: '/static/icons/badge-96.png',
    tag: d.tag || undefined,
    renotify: !!d.tag,
    timestamp: d.ts ? d.ts * 1000 : Date.now(),
    data: { url: d.url || '/' },
    vibrate: [120, 60, 120],
  };
  if (d.image) opts.image = d.image;
  e.waitUntil(self.registration.showNotification(d.title || 'CamSpire NVR', opts));
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const target = new URL(e.notification.data?.url || '/', self.location.origin).href;
  e.waitUntil((async () => {
    const wins = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const w of wins) {
      if (new URL(w.url).origin === self.location.origin) {
        await w.focus();
        w.postMessage({ type: 'navigate', url: target });
        return;
      }
    }
    await self.clients.openWindow(target);
  })());
});
