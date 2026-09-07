const assert = require('node:assert/strict');
const vm = require('node:vm');
const { test } = require('node:test');
const { setup, markup, settle } = require('./web_controls_harness.js');

function terminalHarness(options) {
  const harness = setup(options);
  harness.runScript('terminalViewport');
  harness.terminal = vm.runInContext('TerminalViewport', harness.context);
  return harness;
}

test('selecting a terminal fills the viewport and requests fullscreen during the tap', async () => {
  const { terminal, document, calls } = terminalHarness();
  terminal.open(true);
  assert.equal(document.body.classList.contains('terminal-open'), true);
  assert.deepEqual(calls, [{ action: 'enter', navigationUI: 'hide' }]);
  assert.equal(document.body.style.getPropertyValue('--terminal-width'), '393px');
  assert.equal(document.body.style.getPropertyValue('--terminal-height'), '852px');
  await settle();
  terminal.close();
  await settle();
  assert.equal(document.body.classList.contains('terminal-open'), false);
  assert.equal(document.fullscreenElement, null);
});

test('rotation and keyboard resizing follow the visible area on phone and tablet', () => {
  const { terminal, window, document } = terminalHarness();
  terminal.open(false);
  for (const [width, height, offsetTop, offsetLeft] of [
    [852, 393, 0, 0], [852, 190, 24, 0],
    [768, 1024, 0, 0], [1024, 768, 0, 0], [1024, 350, 18, 0],
  ]) {
    Object.assign(window.visualViewport, { width, height, offsetTop, offsetLeft });
    window.visualViewport.dispatchEvent(new Event('resize'));
    const style = document.body.style;
    assert.equal(style.getPropertyValue('--terminal-width'), `${width}px`);
    assert.equal(style.getPropertyValue('--terminal-height'), `${height}px`);
    assert.equal(style.getPropertyValue('--terminal-top'), `${offsetTop}px`);
    assert.equal(style.getPropertyValue('--terminal-left'), `${offsetLeft}px`);
  }
});

test('native pinch zoom is preserved instead of shrinking the layout under it', () => {
  const { terminal, window, document } = terminalHarness();
  terminal.open(false);
  Object.assign(window.visualViewport, { width: 196.5, height: 426, scale: 2 });
  window.visualViewport.dispatchEvent(new Event('resize'));
  assert.equal(document.body.style.getPropertyValue('--terminal-width'), '393px');
});

test('the terminal also fits without VisualViewport or fullscreen support', () => {
  const { terminal, window, document, calls } = terminalHarness({ supported: false, viewport: false });
  terminal.open(true);
  window.innerWidth = 1024;
  window.innerHeight = 600;
  window.dispatchEvent(new Event('resize'));
  assert.equal(document.body.style.getPropertyValue('--terminal-height'), '600px');
  assert.equal(document.body.classList.contains('terminal-open'), true);
  assert.deepEqual(calls, []);
});

test('keeping columns toggles layout without rewriting ANSI output', () => {
  const { terminal, document } = terminalHarness();
  terminal.open(false);
  const output = document.getElementById('termContent');
  const button = document.getElementById('keepColumnsButton');
  output.textContent = 'name      status\nagent     working';
  button.dispatchEvent(new Event('click'));
  assert.equal(output.classList.contains('keep-columns'), true);
  assert.equal(button.attrs['aria-pressed'], 'true');
  assert.equal(output.textContent, 'name      status\nagent     working');
  button.dispatchEvent(new Event('click'));
  assert.equal(output.classList.contains('keep-columns'), false);
});

test('an output update cannot force fullscreen back on after Android exits', async () => {
  const { terminal, document, calls } = terminalHarness();
  terminal.open(true);
  await settle();
  document.fullscreenElement = null;
  document.dispatchEvent(new Event('fullscreenchange'));
  terminal.open(false);
  assert.equal(calls.length, 1);
  assert.equal(document.body.classList.contains('terminal-open'), true);
});

test('going back while fullscreen is pending cannot leave the list in fullscreen', async () => {
  const { terminal, document } = terminalHarness();
  terminal.open(true);
  terminal.close();
  await settle();
  assert.equal(document.fullscreenElement, null);
  assert.equal(document.body.classList.contains('terminal-open'), false);
});

test('opening and closing the real terminal view uses the viewport controller', () => {
  const harness = terminalHarness();
  const { document, context } = harness;
  Object.assign(context, {
    agents: [], activePane: null, paneLines: 200, userScrolledUp: false,
    refreshInterval: null, clearInterval() {}, setInterval() { return 1; },
    refreshPane() {}, render() {},
  });
  const start = markup.indexOf('function openTerminal(');
  const end = markup.indexOf('\nlet paneLines =', start);
  vm.runInContext(markup.slice(start, end), context);
  vm.runInContext('openTerminal("session:workspace:pane", false)', context);
  assert.equal(document.body.classList.contains('terminal-open'), true);
  assert.equal(context.activePane, 'session:workspace:pane');
  vm.runInContext('closeTerminal()', context);
  assert.equal(document.body.classList.contains('terminal-open'), false);
  assert.equal(context.activePane, null);
});
