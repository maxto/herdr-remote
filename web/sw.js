// herdr-app service worker — Web Push notifications and the offline shell
const SHELL = 'herdr-shell-v1';

self.addEventListener('install', (e) => { self.skipWaiting(); });
self.addEventListener('activate', (e) => {
  e.waitUntil((async () => {
    const names = await caches.keys();
    await Promise.all(names.filter((name) => name !== SHELL).map((name) => caches.delete(name)));
    await self.clients.claim();
  })());
});

// The two files the page needs to boot, and no others. Everything else is
// inline; the font is declared font-display: swap and the icons are the
// browser's to keep, so holding a megabyte of them would buy nothing.
//
// A navigation is keyed to "/" however it was entered — the relay serves the
// dashboard for both / and /index.html, and the start_url carries no query.
function shellKey(request) {
  if (request.method && request.method !== 'GET') return null;
  if (request.mode === 'navigate') return '/';
  const path = new URL(request.url).pathname;
  return path === '/security.js' ? path : null;
}

// Network first, always. The relay serves the dashboard live and the two speak
// a protocol that changes together, so a cached page must never be able to
// keep chunking attachments the way the relay no longer accepts: a relay that
// answers wins, and its answer replaces the copy on the way past.
//
// The copy is there for the case where there is no protocol to be wrong about.
// Android freezes a backgrounded page and takes the socket with it, and a
// tunnel can blink; with nothing kept, a refresh in standalone mode landed on
// Chrome's error page, which carries no reload button — the app stayed dead
// until it was force-quit. Now it opens, shows its last snapshot, says offline
// and retries on its own.
// Storage is allowed to fail. A phone can be out of room, and a browser told
// to clear site data makes the Cache API throw outright — neither is a reason
// to refuse a page the relay has just handed over. The copy is an
// optimisation, never a dependency, so both sides of it swallow their own
// errors and leave the network's answer untouched.
async function keepInShell(key, response) {
  try {
    const cache = await caches.open(SHELL);
    await cache.put(key, response);
  } catch (unstorable) {
    // Nothing to do and nothing to say: the page is already on its way.
  }
}

async function fromShell(key) {
  try {
    const cache = await caches.open(SHELL);
    return await cache.match(key);
  } catch (unreadable) {
    return undefined;
  }
}

async function shellResponse(request, key) {
  let response;
  try {
    response = await fetch(request);
  } catch (unreachable) {
    const cached = await fromShell(key);
    if (cached) return cached;
    // The relay cannot be reached and nothing was kept. Let the network error
    // through: resolving with nothing would be reported as a broken worker
    // rather than the failure it is, and a storage error raised from here
    // would send whoever reads it hunting the wrong fault.
    throw unreachable;
  }
  if (response && response.ok) await keepInShell(key, response.clone());
  return response;
}

// Handling fetch is also what makes the app installable — Chrome offers a
// home-screen shortcut instead of the standalone app for a worker that ignores
// it. A request outside the shell is left to the network untouched.
self.addEventListener('fetch', (event) => {
  const key = shellKey(event.request);
  if (key) event.respondWith(shellResponse(event.request, key));
});

// Every push shows something. The subscription is userVisibleOnly, which Chrome
// enforces: a handler that displays nothing gets Chrome's own "site updated in
// background" notice instead, and that placeholder replaced the real news it
// arrived after. Withdrawing a stale notification is the dashboard's job now.
self.addEventListener('push', (event) => {
  let data = { title: '🐑 herdr-app', body: 'Agent needs attention', url: '/' };
  try {
    if (event.data) data = { ...data, ...event.data.json() };
  } catch (e) {}
  event.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body,
      icon: '/logo.svg',
      badge: '/logo.svg',
      tag: data.tag || 'herdr-blocked',
      renotify: true,
      data: { url: data.url },
    })
  );
});

// A browser may replace a subscription whenever it likes, and the page cannot
// be relied on to be open when it happens. Resubscribing here keeps the handset
// reachable; the dashboard hands the new endpoint to the relay the next time it
// connects. The original key has to be reused, or the new subscription is one
// the relay's VAPID keys cannot sign for.
self.addEventListener('pushsubscriptionchange', (event) => {
  const key = event.oldSubscription && event.oldSubscription.options
    ? event.oldSubscription.options.applicationServerKey
    : null;
  if (!key) return;
  event.waitUntil(
    self.registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = event.notification.data?.url || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if (client.url.includes(self.location.origin)) {
          client.focus();
          client.postMessage({ type: 'navigate', url });
          return;
        }
      }
      return self.clients.openWindow(url);
    })
  );
});
