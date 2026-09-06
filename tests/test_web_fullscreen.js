const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');

const markup = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');

function setup({ supported = true, reject = false } = {}) {
  const script = markup.match(/<script id="fullscreenControls">([\s\S]*?)<\/script>/);
  assert.ok(script, 'the dashboard must load its fullscreen controls');
  const elements = new Map();
  const calls = [];
  const document = new EventTarget();
  document.fullscreenEnabled = supported;
  document.fullscreenElement = null;
  document.getElementById = id => {
    if (!elements.has(id)) {
      const tag = markup.match(new RegExp(`<[^>]+\\bid="${id}"[^>]*>`));
      assert.ok(tag, `the dashboard must contain ${id}`);
      const element = new EventTarget();
      element.hidden = /\bhidden\b/.test(tag[0]);
      element.disabled = false;
      element.textContent = '';
      element.attrs = {};
      element.setAttribute = (name, value) => { element.attrs[name] = value; };
      elements.set(id, element);
    }
    return elements.get(id);
  };
  document.documentElement = {
    requestFullscreen(options) {
      calls.push({ action: 'enter', navigationUI: options.navigationUI });
      if (reject) return Promise.reject(new Error('browser refused'));
      document.fullscreenElement = this;
      document.dispatchEvent(new Event('fullscreenchange'));
      return Promise.resolve();
    },
  };
  document.exitFullscreen = () => {
    calls.push({ action: 'exit' });
    if (reject) return Promise.reject(new Error('browser refused'));
    document.fullscreenElement = null;
    document.dispatchEvent(new Event('fullscreenchange'));
    return Promise.resolve();
  };
  vm.runInNewContext(script[1], { document });
  return {
    document, calls,
    button: document.getElementById('fullscreenButton'),
    icon: document.getElementById('fullscreenIcon'),
    status: document.getElementById('fullscreenStatus'),
  };
}

const settle = () => new Promise(resolve => setImmediate(resolve));

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
