// herdr-app service worker — Web Push notifications
self.addEventListener('install', (e) => { self.skipWaiting(); });
self.addEventListener('activate', (e) => { e.waitUntil(self.clients.claim()); });

// Chrome will not offer to install a page whose worker ignores fetch: without
// this it proposes a home-screen shortcut instead of the standalone app the
// manifest asks for.
//
// Nothing is cached on purpose. The relay serves the dashboard live and the two
// speak a protocol that changes together — a cached page would keep chunking
// attachments the way the relay no longer accepts. Passing the request through
// without responding leaves it to the network.
self.addEventListener('fetch', () => {});

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
