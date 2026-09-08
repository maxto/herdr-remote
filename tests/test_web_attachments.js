const assert = require('node:assert/strict');
const vm = require('node:vm');
const { test } = require('node:test');
const { setup, settle } = require('./web_controls_harness.js');

function uploadHarness() {
  const harness = setup();
  harness.runScript('terminalUpload');
  harness.upload = vm.runInContext('TerminalUpload', harness.context);
  harness.sent = [];
  harness.socket = {
    readyState: 1,
    bufferedAmount: 0,
    send(payload) { harness.sent.push(JSON.parse(payload)); },
  };
  return harness;
}

function fakeFile(name, type, size, byte = 65) {
  const bytes = new Uint8Array(size).fill(byte);
  return { name, type, size, arrayBuffer: async () => bytes.buffer };
}

// The guard that was missing is the whole reason the first attempt died: an
// oversized photo reached the socket, blew past the frame ceiling, and the
// server closed the connection without ever recording the upload.
test('an oversized file never reaches the socket', () => {
  const { upload, sent } = uploadHarness();
  const accepted = upload.choose(fakeFile('huge.png', 'image/png', 11 * 1024 * 1024));
  assert.equal(accepted, false);
  assert.equal(sent.length, 0);
  assert.match(upload.status(), /10 MiB/);
  assert.equal(upload.hasFile(), false);
});

test('each type is judged against its own ceiling', () => {
  const { upload } = uploadHarness();
  assert.equal(upload.choose(fakeFile('a.txt', 'text/plain', 2 * 1024 * 1024)), false);
  assert.match(upload.status(), /1 MiB/);
  assert.equal(upload.choose(fakeFile('a.pdf', 'application/pdf', 20 * 1024 * 1024)), true);
});

test('a type the relay would refuse is refused here first', () => {
  const { upload, sent } = uploadHarness();
  assert.equal(upload.choose(fakeFile('a.zip', 'application/zip', 10)), false);
  assert.equal(sent.length, 0);
});

test('an upload walks begin, chunk and commit', async () => {
  const harness = uploadHarness();
  const { upload, socket, sent } = harness;
  upload.choose(fakeFile('nota.txt', 'text/plain', 8));
  const done = upload.send(socket, 'demo:w1:p1', 'guarda qui');

  await settle();
  assert.equal(sent[0].type, 'attachment_begin');
  assert.equal(sent[0].size, 8);
  upload.handleMessage({ type: 'command_result', command: 'attachment_begin',
    request_id: sent[0].request_id, upload_id: 'up-1', ok: true });

  await settle();
  assert.equal(sent[1].type, 'attachment_chunk');
  assert.equal(sent[1].index, 0);
  upload.handleMessage({ type: 'command_result', command: 'attachment_chunk',
    request_id: sent[0].request_id, index: 0, ok: true });

  await settle();
  const commit = sent.find(message => message.type === 'attachment_commit');
  assert.ok(commit, 'the upload must be sealed');
  assert.equal(commit.text, 'guarda qui');
  upload.handleMessage({ type: 'command_result', command: 'attachment_commit',
    request_id: sent[0].request_id, ok: true });
  await done;
  assert.equal(upload.hasFile(), false, 'a delivered file leaves the composer');
});

test('only one chunk is in flight, so the browser cannot queue the whole file', async () => {
  const harness = uploadHarness();
  const { upload, socket, sent } = harness;
  const chunk = 256 * 1024;
  upload.choose(fakeFile('big.pdf', 'application/pdf', chunk * 3));
  upload.send(socket, 'demo:w1:p1', '');

  await settle();
  upload.handleMessage({ type: 'command_result', command: 'attachment_begin',
    request_id: sent[0].request_id, upload_id: 'up-2', ok: true });

  await settle();
  const chunks = () => sent.filter(message => message.type === 'attachment_chunk');
  assert.equal(chunks().length, 1, 'the second chunk must wait for the first ack');

  upload.handleMessage({ type: 'command_result', command: 'attachment_chunk',
    request_id: sent[0].request_id, index: 0, ok: true });
  await settle();
  assert.equal(chunks().length, 2);
  assert.equal(upload.progress() > 0 && upload.progress() < 1, true);
});

test('cancelling mid-flight tells the relay instead of leaving it holding a file', async () => {
  const harness = uploadHarness();
  const { upload, socket, sent } = harness;
  upload.choose(fakeFile('a.pdf', 'application/pdf', 512 * 1024));
  upload.send(socket, 'demo:w1:p1', '');
  await settle();
  upload.handleMessage({ type: 'command_result', command: 'attachment_begin',
    request_id: sent[0].request_id, upload_id: 'up-3', ok: true });
  await settle();

  upload.cancel(socket);
  await settle();
  const abort = sent.find(message => message.type === 'attachment_abort');
  assert.ok(abort, 'the relay must be told to let go');
  assert.equal(abort.upload_id, 'up-3');
  assert.equal(upload.hasFile(), false);
});

test('a refusal from the relay keeps the file so the operator can retry', async () => {
  const harness = uploadHarness();
  const { upload, socket, sent } = harness;
  upload.choose(fakeFile('a.txt', 'text/plain', 4));
  upload.send(socket, 'demo:w1:p1', 'ciao');
  await settle();
  upload.handleMessage({ type: 'error', request_id: sent[0].request_id,
    message: 'The relay is busy with other uploads; try again shortly' });
  await settle();
  assert.equal(upload.hasFile(), true, 'a refused file is not thrown away');
  assert.match(upload.status(), /busy/);
});
