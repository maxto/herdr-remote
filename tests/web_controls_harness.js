const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const markup = fs.readFileSync(path.join(__dirname, '../web/index.html'), 'utf8');

function setup({ viewport = true } = {}) {
  const elements = new Map();
  const document = new EventTarget();
  const window = new EventTarget();
  window.innerWidth = 393;
  window.innerHeight = 852;
  window.requestAnimationFrame = callback => callback();
  window.visualViewport = viewport ? Object.assign(new EventTarget(), {
    width: 393, height: 852, offsetTop: 0, offsetLeft: 0, scale: 1,
  }) : null;

  function element(attributes = {}) {
    const node = new EventTarget();
    node.attrs = attributes;
    node.hidden = Object.hasOwn(attributes, 'hidden');
    node.disabled = false;
    node.textContent = '';
    node.style = {
      setProperty(name, value) { this[name] = value; },
      getPropertyValue(name) { return this[name]; },
    };
    const classes = new Set((attributes.class || '').split(/\s+/));
    node.classList = {
      add: name => classes.add(name),
      remove: name => classes.delete(name),
      contains: name => classes.has(name),
      toggle(name, force = !classes.has(name)) {
        if (force) classes.add(name);
        else classes.delete(name);
        return force;
      },
    };
    node.setAttribute = (name, value) => { node.attrs[name] = value; };
    node.getAttribute = name => node.attrs[name];
    node.getBoundingClientRect = () => ({ height: 48 });
    node.scrollHeight = 1000;
    node.clientHeight = 700;
    node.scrollTop = 300;
    node.blur = () => { document.activeElement = document.body; };
    node.focus = () => { document.activeElement = node; };
    node.replaceChildren = () => {};
    return node;
  }

  document.body = element();
  document.documentElement = element();
  document.documentElement.clientWidth = 393;
  document.documentElement.clientHeight = 852;
  document.activeElement = document.body;
  document.getElementById = id => {
    if (!elements.has(id)) {
      const tag = markup.match(new RegExp(`<[^>]+\\bid="${id}"[^>]*>`));
      assert.ok(tag, `the dashboard must contain ${id}`);
      const attrs = Object.fromEntries([...tag[0].matchAll(/([\w-]+)="([^"]*)"/g)].map(m => [m[1], m[2]]));
      if (/\bhidden\b/.test(tag[0])) attrs.hidden = '';
      elements.set(id, element(attrs));
    }
    return elements.get(id);
  };
  const context = vm.createContext({ document, window });
  function runScript(id) {
    const script = markup.match(new RegExp(`<script id="${id}">([\\s\\S]*?)<\\/script>`));
    assert.ok(script, `the dashboard must load ${id}`);
    vm.runInContext(script[1], context);
  }
  return {
    document, window, context, runScript,
  };
}

module.exports = { setup, markup, settle: () => new Promise(resolve => setImmediate(resolve)) };
