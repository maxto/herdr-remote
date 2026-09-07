const assert = require('node:assert/strict');
const { test } = require('node:test');
const { setup, settle } = require('./web_controls_harness.js');

test('a tap requests fullscreen immediately and the next tap exits', async () => {
  const { button, icon, calls } = setup();
  assert.equal(button.hidden, false);
  assert.equal(button.attrs['aria-pressed'], 'false');
  const enterIcon = icon.attrs.d;
  button.dispatchEvent(new Event('click'));
  assert.deepEqual(calls, [{ action: 'enter', navigationUI: 'hide' }],
    'requestFullscreen must run within the tap, before user activation expires');
  await settle();
  assert.equal(button.attrs['aria-pressed'], 'true');
  assert.notEqual(icon.attrs.d, enterIcon);
  button.dispatchEvent(new Event('click'));
  await settle();
  assert.deepEqual(calls, [{ action: 'enter', navigationUI: 'hide' }, { action: 'exit' }]);
  assert.equal(button.attrs['aria-pressed'], 'false');
  assert.equal(icon.attrs.d, enterIcon);
});

test('leaving fullscreen through Android updates the button', async () => {
  const { button, document, icon } = setup();
  const enterIcon = icon.attrs.d;
  button.dispatchEvent(new Event('click'));
  await settle();
  document.fullscreenElement = null;
  document.dispatchEvent(new Event('fullscreenchange'));
  assert.equal(button.attrs['aria-pressed'], 'false');
  assert.equal(icon.attrs.d, enterIcon);
});

test('unsupported browsers keep the fullscreen control hidden', () => {
  const { button } = setup({ supported: false });
  assert.equal(button.hidden, true);
});

test('a rejected enter or exit reports an error and preserves the actual state', async () => {
  for (const active of [false, true]) {
    const { button, document, status } = setup({ reject: true });
    document.fullscreenElement = active ? document.documentElement : null;
    document.dispatchEvent(new Event('fullscreenchange'));
    button.dispatchEvent(new Event('click'));
    await settle();
    assert.equal(button.disabled, false);
    assert.equal(button.attrs['aria-pressed'], String(active));
    assert.ok(status.textContent.trim(), 'the user must see why the button did not work');
    assert.equal(status.hidden, false);
  }
});
