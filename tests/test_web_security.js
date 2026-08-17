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
  { name: 'Main', url: 'wss://relay.example', token: 'real-secret' },
  { name: 'Backup', url: 'wss://backup.example' },
]));
assert.deepEqual(migratedSessions, {
  changed: true,
  sessions: [
    { name: 'Main', url: 'wss://relay.example' },
    { name: 'Backup', url: 'wss://backup.example' },
  ],
});
assert.equal(JSON.stringify(migratedSessions).includes('real-secret'), false);

const alreadySafeSessions = security.sanitizeSavedSessions(
  '[{"name":"Main","url":"wss://relay.example"}]'
);
assert.equal(alreadySafeSessions.changed, false);
