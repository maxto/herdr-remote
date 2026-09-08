const assert = require('node:assert/strict');
const vm = require('node:vm');
const { test } = require('node:test');
const { setup, markup } = require('./web_controls_harness.js');

function terminalHarness(options) {
  const harness = setup(options);
  harness.runScript('terminalViewport');
  harness.terminal = vm.runInContext('TerminalViewport', harness.context);
  return harness;
}

test('selecting a terminal fills the available app viewport', () => {
  const { terminal, document } = terminalHarness();
  terminal.open();
  assert.equal(document.body.classList.contains('terminal-open'), true);
  assert.equal(document.body.style.getPropertyValue('--terminal-width'), '393px');
  assert.equal(document.body.style.getPropertyValue('--terminal-height'), '852px');
  terminal.close();
  assert.equal(document.body.classList.contains('terminal-open'), false);
});

test('rotation and keyboard resizing follow the visible area on phone and tablet', () => {
  const { terminal, window, document } = terminalHarness();
  terminal.open();
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
  terminal.open();
  Object.assign(window.visualViewport, { width: 196.5, height: 426, scale: 2 });
  window.visualViewport.dispatchEvent(new Event('resize'));
  assert.equal(document.body.style.getPropertyValue('--terminal-width'), '393px');
});

test('the terminal also fits without VisualViewport support', () => {
  const { terminal, window, document } = terminalHarness({ viewport: false });
  terminal.open();
  window.innerWidth = 1024;
  window.innerHeight = 600;
  window.dispatchEvent(new Event('resize'));
  assert.equal(document.body.style.getPropertyValue('--terminal-height'), '600px');
  assert.equal(document.body.classList.contains('terminal-open'), true);
});

test('keeping columns toggles layout without rewriting ANSI output', () => {
  const { terminal, document } = terminalHarness();
  terminal.open();
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

test('an output update preserves the current controls state', () => {
  const { terminal, document } = terminalHarness();
  terminal.open();
  const button = document.getElementById('terminalControlsToggle');
  // Whatever the default is, a second open must not undo a deliberate choice.
  button.dispatchEvent(new Event('click'));
  const chosen = button.attrs['aria-expanded'];
  terminal.open();
  assert.equal(button.attrs['aria-expanded'], chosen);
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
  vm.runInContext('openTerminal("session:workspace:pane")', context);
  assert.equal(document.body.classList.contains('terminal-open'), true);
  assert.equal(context.activePane, 'session:workspace:pane');
  vm.runInContext('closeTerminal()', context);
  assert.equal(document.body.classList.contains('terminal-open'), false);
  assert.equal(context.activePane, null);
});

test('the floating button changes between show and hide controls icons', () => {
  const { terminal, document } = terminalHarness();
  const button = document.getElementById('terminalControlsToggle');
  const icon = document.getElementById('terminalControlsIcon');
  terminal.open();
  const openedPath = icon.attrs.d;
  const openedState = button.attrs['aria-expanded'];
  button.dispatchEvent(new Event('click'));
  assert.notEqual(button.attrs['aria-expanded'], openedState);
  assert.notEqual(icon.attrs.d, openedPath);
  button.dispatchEvent(new Event('click'));
  assert.equal(button.attrs['aria-expanded'], openedState);
  assert.equal(icon.attrs.d, openedPath);
});

test('entering a terminal shows its controls rather than hiding them', () => {
  const { terminal, document } = terminalHarness();
  const view = document.getElementById('terminalView');
  terminal.open();
  // Opening onto bare output reads as a full-screen view with no way in: the
  // input, the keys and the mode switcher should be there to start with.
  assert.equal(view.classList.contains('controls-hidden'), false);
  const toggle = document.getElementById('terminalControlsToggle');
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
});

test('the controls toggle still hides and restores them', () => {
  const { terminal, document } = terminalHarness();
  const view = document.getElementById('terminalView');
  const toggle = document.getElementById('terminalControlsToggle');
  terminal.open();
  toggle.dispatchEvent(new Event('click'));
  assert.equal(view.classList.contains('controls-hidden'), true);
  toggle.dispatchEvent(new Event('click'));
  assert.equal(view.classList.contains('controls-hidden'), false);
});
