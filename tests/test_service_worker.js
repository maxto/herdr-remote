// The installed app must survive a relay it cannot reach.
//
// Android freezes a backgrounded page and takes the socket with it, and a
// tunnel can blink. With nothing in the cache a refresh in standalone mode
// lands on Chrome's error page, which carries no reload button: the app is a
// dead shell until it is force-quit. The worker therefore keeps a copy of the
// two files the page needs to boot.
//
// The copy is a fallback and never a source. The worker asks the network
// first, so a relay that answers always wins and a cached page can never end
// up speaking a stale protocol to a relay that has moved on.

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', 'web', 'sw.js'), 'utf8');
const ORIGIN = 'https://relay.example';

function fakeResponse(body, { ok = true, status = 200 } = {}) {
  return { body, ok, status, clone() { return fakeResponse(body, { ok, status }); } };
}

// A Cache API stub that records what the worker asks of it, so a test can tell
// "answered from the network" apart from "answered from the cache".
function fakeCaches() {
  const stored = new Map();
  const reads = [];
  const key = (request) => (typeof request === 'string' ? request : request.url);
  const cache = {
    async put(request, response) { stored.set(key(request), response); },
    async match(request) { reads.push(key(request)); return stored.get(key(request)); },
  };
  return {
    stored,
    reads,
    api: { async open() { return cache; }, async match(request) { return cache.match(request); } },
  };
}

function fakeRequest(url, { mode = 'no-cors', method = 'GET' } = {}) {
  return { url: new URL(url, ORIGIN).href, mode, method };
}

// Loads web/sw.js for real, with the globals a service worker expects.
function loadWorker({ fetch, caches }) {
  const listeners = new Map();
  const self = {
    addEventListener(type, handler) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(handler);
    },
    skipWaiting() {},
    clients: { claim() {}, async matchAll() { return []; }, async openWindow() {} },
    registration: { async showNotification() {}, pushManager: { async subscribe() {} } },
    location: { origin: ORIGIN },
  };
  const context = { self, fetch, caches, URL, console, setTimeout, Promise };
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(SOURCE, context, { filename: 'sw.js' });

  // Dispatch a fetch event the way the browser would. A worker that answers
  // decides the response; one that stays out of the way leaves the request to
  // the network, which is the browser's own job and so is done here too.
  return function dispatchFetch(request) {
    let answer;
    const event = {
      request,
      respondWith(promise) { answer = promise; },
      waitUntil(promise) { return promise; },
    };
    for (const handler of listeners.get('fetch') || []) handler(event);
    return answer === undefined ? fetch(request) : answer;
  };
}

const navigation = () => fakeRequest('/', { mode: 'navigate' });

async function main() {
  // --- A reachable relay always wins ---------------------------------------

  {
    const caches = fakeCaches();
    const fresh = fakeResponse('fresh page');
    const dispatch = loadWorker({ fetch: async () => fresh, caches: caches.api });

    const served = await dispatch(navigation());

    assert.equal(served.body, 'fresh page', 'a reachable relay must serve the page');
    assert.deepEqual(caches.reads, [], 'the cache must not be consulted while the relay answers');
  }

  // --- The network fills the cache as it goes ------------------------------

  {
    const caches = fakeCaches();
    const dispatch = loadWorker({ fetch: async () => fakeResponse('page v2'), caches: caches.api });

    await dispatch(navigation());

    const saved = [...caches.stored.values()].map((response) => response.body);
    assert.deepEqual(saved, ['page v2'], 'a good response must leave a copy behind');
  }

  // --- An unreachable relay falls back to that copy ------------------------

  {
    const caches = fakeCaches();
    let reachable = true;
    const dispatch = loadWorker({
      fetch: async () => {
        if (!reachable) throw new TypeError('Failed to fetch');
        return fakeResponse('page v2');
      },
      caches: caches.api,
    });

    await dispatch(navigation());
    reachable = false;
    const served = await dispatch(navigation());

    assert.equal(served.body, 'page v2', 'a dropped relay must still open the app');
  }

  // --- The helper the page cannot boot without -----------------------------

  {
    const caches = fakeCaches();
    let reachable = true;
    const dispatch = loadWorker({
      fetch: async () => {
        if (!reachable) throw new TypeError('Failed to fetch');
        return fakeResponse('security helper');
      },
      caches: caches.api,
    });

    await dispatch(fakeRequest('/security.js'));
    reachable = false;
    const served = await dispatch(fakeRequest('/security.js'));

    // index.html loads ./security.js, so a cached page without it boots into a
    // blank screen — which is the dead end the cache exists to prevent.
    assert.equal(served.body, 'security helper', 'security.js must be part of the shell');
  }

  // --- Nothing else is cached ----------------------------------------------

  {
    const caches = fakeCaches();
    const dispatch = loadWorker({ fetch: async () => fakeResponse('font bytes'), caches: caches.api });

    // The font is decorative and declared font-display: swap; the icons are the
    // browser's to keep. Holding a megabyte of them would buy nothing.
    await dispatch(fakeRequest('/HackNerdFont-Regular.woff2'));

    assert.equal(caches.stored.size, 0, 'only the boot files belong in the cache');
  }

  // --- A failure with an empty cache stays a failure -----------------------

  {
    const caches = fakeCaches();
    const dispatch = loadWorker({
      fetch: async () => { throw new TypeError('Failed to fetch'); },
      caches: caches.api,
    });

    // First run, no copy yet. The worker must let the error through rather than
    // resolve with undefined, which Chrome reports as a worker bug instead of
    // the network error it is.
    await assert.rejects(dispatch(navigation()), /Failed to fetch/);
  }

  // --- A relay that answers badly is not cached ----------------------------

  {
    const caches = fakeCaches();
    const dispatch = loadWorker({
      fetch: async () => fakeResponse('Bad Gateway', { ok: false, status: 502 }),
      caches: caches.api,
    });

    const served = await dispatch(navigation());

    assert.equal(served.status, 502, 'the real answer must reach the page');
    assert.equal(caches.stored.size, 0, 'an error page must never become the shell');
  }
}

main().then(
  () => console.log('service worker offline shell: ok'),
  (error) => { console.error(error); process.exit(1); }
);
