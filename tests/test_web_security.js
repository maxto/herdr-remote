const assert = require('node:assert/strict');

const security = require('../web/security.js');

const cleanedLocation = security.stripTokenFromUrl(
  'https://dashboard.example/app?token=real-secret&theme=dark#agents'
);
assert.deepEqual(cleanedLocation, {
  changed: true,
  relativeUrl: '/app?theme=dark#agents',
});
assert.equal(JSON.stringify(cleanedLocation).includes('real-secret'), false);

const migratedSessions = security.sanitizeSavedSessions(JSON.stringify([
  {
    name: 'Main',
    url: 'wss://relay.example?token=legacy-secret#token=fragment-secret&view=agents',
    token: 'real-secret',
  },
  { name: 'Backup', url: 'wss://backup.example' },
]));
assert.deepEqual(migratedSessions, {
  changed: true,
  sessions: [
    { name: 'Main', url: 'wss://relay.example/#view=agents' },
    { name: 'Backup', url: 'wss://backup.example' },
  ],
});
assert.equal(JSON.stringify(migratedSessions).includes('secret'), false);

const alreadySafeSessions = security.sanitizeSavedSessions(
  '[{"name":"Main","url":"wss://relay.example"}]'
);
assert.equal(alreadySafeSessions.changed, false);

const authenticatedConnection = security.createAuthenticatedConnection(
  'wss://relay.example/socket?token=legacy-secret#token=fragment-secret&view=agents',
  'real-secret'
);
assert.deepEqual(authenticatedConnection, {
  url: 'wss://relay.example/socket#view=agents',
  authMessage: { type: 'auth', protocol: 1, token: 'real-secret' },
});
assert.equal(authenticatedConnection.url.includes('secret'), false);

function fakeStorage(initial = {}) {
  const items = { ...initial };
  return {
    items,
    getItem: (key) => (key in items ? items[key] : null),
    setItem: (key, value) => { items[key] = value; },
    removeItem: (key) => { delete items[key]; },
  };
}

// The operator pastes the token once; it must survive a reload.
const emptyStorage = fakeStorage();
assert.equal(security.readStoredRelayToken(emptyStorage), '');

const filledStorage = fakeStorage({ herdr_relay_token: 'remembered-secret' });
assert.equal(security.readStoredRelayToken(filledStorage), 'remembered-secret');

const writeStorage = fakeStorage();
security.persistRelayToken(writeStorage, 'fresh-secret');
assert.deepEqual(writeStorage.items, { herdr_relay_token: 'fresh-secret' });

security.persistRelayToken(writeStorage, '');
assert.deepEqual(writeStorage.items, {}, 'an empty token clears the stored one');

security.persistRelayToken(writeStorage, 'another-secret');
security.forgetStoredRelayToken(writeStorage);
assert.deepEqual(writeStorage.items, {}, 'switching relays forgets the token');

function createSocketHarness() {
  const sockets = [];
  const statuses = [];
  const applicationMessages = [];
  const scheduledReconnects = [];
  const cancelledReconnects = [];
  const controller = security.createConnectionController({
    openSocket(url) {
      const socket = {
        closeCalls: [],
        sent: [],
        url,
        close(...args) { this.closeCalls.push(args); },
        send(raw) { this.sent.push(JSON.parse(raw)); },
      };
      sockets.push(socket);
      return socket;
    },
    onAuthenticated() {},
    onMessage(message) { applicationMessages.push(message); },
    onSocketChange() {},
    onStatus(status) { statuses.push(status); },
    scheduleReconnect(callback) {
      scheduledReconnects.push(callback);
      return callback;
    },
    cancelReconnect(handle) { cancelledReconnects.push(handle); },
  });
  return {
    applicationMessages,
    cancelledReconnects,
    controller,
    scheduledReconnects,
    sockets,
    statuses,
  };
}

for (const invalidFrame of [
  'not-json',
  JSON.stringify({ type: 'agents', agents: [] }),
  JSON.stringify({ type: 'auth_result', protocol: 2, ok: true }),
  JSON.stringify({ type: 'auth_result', protocol: 1, ok: false }),
]) {
  const harness = createSocketHarness();
  const socket = harness.controller.connect(authenticatedConnection, () => {});
  socket.onopen();
  socket.onmessage({ data: invalidFrame });

  assert.deepEqual(socket.closeCalls, [[1008, 'Unauthorized']]);
  assert.deepEqual(harness.applicationMessages, []);
  assert.deepEqual(harness.scheduledReconnects, []);
  assert.equal(harness.statuses.includes('connected'), false);
}

const lifecycleHarness = createSocketHarness();
const firstSocket = lifecycleHarness.controller.connect(authenticatedConnection, () => {
  throw new Error('stale socket must not reconnect');
});
firstSocket.onopen();
lifecycleHarness.controller.disconnect();
firstSocket.onclose();
assert.deepEqual(lifecycleHarness.scheduledReconnects, []);

const secondSocket = lifecycleHarness.controller.connect(authenticatedConnection, () => {});
secondSocket.onopen();
firstSocket.onopen();
firstSocket.onmessage({
  data: JSON.stringify({ type: 'auth_result', protocol: 1, ok: true }),
});
firstSocket.onerror();
firstSocket.onclose();
assert.equal(lifecycleHarness.statuses.at(-1), 'authenticating');

secondSocket.onmessage({
  data: JSON.stringify({ type: 'auth_result', protocol: 1, ok: true }),
});
assert.equal(lifecycleHarness.statuses.at(-1), 'connected');

// --- Admission is what "connected" means --------------------------------

const tokenlessConnection = security.createAuthenticatedConnection('wss://relay.example', '');

// An open socket is not proof of admission: a relay that wants a token closes
// a tokenless client moments later.
const tokenlessHarness = createSocketHarness();
const tokenlessSocket = tokenlessHarness.controller.connect(tokenlessConnection, () => {});
tokenlessSocket.onopen();
assert.equal(
  tokenlessHarness.statuses.includes('connected'),
  false,
  'an open socket alone must not read as connected'
);

tokenlessSocket.onmessage({ data: JSON.stringify({ type: 'agents', agents: [] }) });
assert.equal(tokenlessHarness.statuses.at(-1), 'connected');
assert.deepEqual(tokenlessHarness.applicationMessages, [{ type: 'agents', agents: [] }]);

// A rejection is final. Retrying the same credentials can only fail again.
const rejectedHarness = createSocketHarness();
const rejectedSocket = rejectedHarness.controller.connect(tokenlessConnection, () => {
  throw new Error('an unauthorized client must not reconnect');
});
rejectedSocket.onopen();
rejectedSocket.onclose({ code: 1008, reason: 'Unauthorized' });
assert.equal(rejectedHarness.statuses.at(-1), 'unauthorized');
assert.deepEqual(rejectedHarness.scheduledReconnects, []);

// An ordinary drop is not a rejection: it must still reconnect.
const droppedHarness = createSocketHarness();
const droppedSocket = droppedHarness.controller.connect(authenticatedConnection, () => {});
droppedSocket.onopen();
droppedSocket.onmessage({
  data: JSON.stringify({ type: 'auth_result', protocol: 1, ok: true }),
});
droppedSocket.onclose({ code: 1006 });
assert.equal(droppedHarness.scheduledReconnects.length, 1);
assert.equal(droppedHarness.statuses.at(-1), 'disconnected');

console.log('admission handling: ok');
