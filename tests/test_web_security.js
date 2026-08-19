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

const removedKeys = [];
security.clearLegacyRelayToken({
  removeItem(key) {
    removedKeys.push(key);
  },
});
assert.deepEqual(removedKeys, ['herdr_relay_token']);

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

// --- Credential Management ---------------------------------------------
// The page stores no token, so retrieval goes through the platform manager.

function fakeCredentialEnvironment({ credential, getThrows, stored } = {}) {
  return {
    credentials: {
      async get() {
        if (getThrows) throw new Error('user denied');
        return credential || null;
      },
      async store(value) {
        stored.push(value);
      },
    },
    PasswordCredential: class {
      constructor({ id, password }) {
        this.id = id;
        this.password = password;
      }
    },
  };
}

(async () => {
  const missing = await security.requestStoredCredential({});
  assert.equal(missing, null, 'no credentials API means no credential');

  const denied = await security.requestStoredCredential(
    fakeCredentialEnvironment({ getThrows: true })
  );
  assert.equal(denied, null, 'a rejected prompt must not throw');

  const empty = await security.requestStoredCredential(fakeCredentialEnvironment({}));
  assert.equal(empty, null, 'no stored credential means null');

  const found = await security.requestStoredCredential(
    fakeCredentialEnvironment({
      credential: { id: 'wss://relay.example', password: 'stored-secret' },
    })
  );
  assert.deepEqual(found, { url: 'wss://relay.example', token: 'stored-secret' });

  const stored = [];
  const environment = fakeCredentialEnvironment({ stored });
  assert.equal(
    await security.storeRelayCredential(environment, 'wss://relay.example', 'secret'),
    true
  );
  assert.deepEqual(
    stored.map((entry) => ({ id: entry.id, password: entry.password })),
    [{ id: 'wss://relay.example', password: 'secret' }]
  );

  assert.equal(
    await security.storeRelayCredential(environment, 'wss://relay.example', ''),
    false,
    'an empty token is never stored'
  );
  assert.equal(
    await security.storeRelayCredential({}, 'wss://relay.example', 'secret'),
    false,
    'an unsupported browser reports failure instead of throwing'
  );

  console.log('credential management: ok');
})();
