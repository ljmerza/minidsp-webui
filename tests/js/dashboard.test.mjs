// app/static/js/dashboard.js: the dashboard output cards' quick controls, driven against a tiny fake DOM, a stub
// fetch that waits for the test to answer, and a stub htmx that records re-fetches.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { beforeEach, describe, test } from 'node:test';
import vm from 'node:vm';

import { ROOT } from './load.mjs';

// --- fake DOM: enough selector support for [data-x], [data-x="v"] and .class ------------------------------------

function matches(el, selector) {
  const attr = selector.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
  if (attr) {
    const key = attr[1].replace(/^data-/, '').replace(/-(\w)/g, (_, c) => c.toUpperCase());
    if (!attr[1].startsWith('data-') || !(key in el.dataset)) return false;
    return attr[2] === undefined || el.dataset[key] === attr[2];
  }
  if (selector.startsWith('.')) return el.className.split(' ').includes(selector.slice(1));
  throw new Error(`unsupported selector ${selector}`);
}

class El {
  constructor(className = '', dataset = {}, children = []) {
    this.className = className;
    this.dataset = { ...dataset };
    this.children = children;
    this.parent = null;
    this.attributes = {};
    this.textContent = '';
    this.disabled = false;
    this.listeners = {};
    for (const c of children) c.parent = this;
    this.classList = {
      toggle: (name, on) => {
        const set = new Set(this.className.split(' ').filter(Boolean));
        if (on) set.add(name);
        else set.delete(name);
        this.className = [...set].join(' ');
      },
    };
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  focus() {
    document.activeElement = this;
  }

  addEventListener(name, fn) {
    (this.listeners[name] ||= []).push(fn);
  }

  closest(selector) {
    for (let el = this; el; el = el.parent) if (matches(el, selector)) return el;
    return null;
  }

  contains(other) {
    for (let el = other; el; el = el.parent) if (el === this) return true;
    return false;
  }

  querySelectorAll(selector) {
    const out = [];
    const walk = (el) => {
      for (const c of el.children) {
        if (matches(c, selector)) out.push(c);
        walk(c);
      }
    };
    walk(this);
    return out;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

function makeCard(o, { gain = 0, mute = false, ceiling = 12, neverSynced = false } = {}) {
  const button = (action, step) => new El('btn-small', step === undefined ? { action } : { action, step: String(step) });
  return new El('output-card', {
    output: String(o), name: `Out ${o + 1}`, gain: String(gain), mute: String(mute), ceiling: String(ceiling),
    gainMin: '-72', neverSynced: String(neverSynced),
  }, [
    new El('', { gainText: '' }),
    new El('output-quick', {}, [button('mute'), button('nudge', -0.5), button('nudge', 0.5)]),
    new El('sync-badge sync-synced', { syncBadge: '' }, [new El('sync-icon'), new El('sync-text')]),
  ]);
}

function makePage(cards, preset = '0') {
  const list = new El('output-cards', { editUrl: `/ui/devices/1/presets/${preset}/edit`, preset }, cards);
  const box = new El('output-cards-box', { outputCards: '' }, [list]);
  const section = new El('card outputs-card', {}, [box]);
  return { section, box, list };
}

// --- globals the script expects ---------------------------------------------------------------------------------

let calls = [];
let refreshes = [];
let toasts = [];
let toastOptions = [];
let confirms = [];
let confirmAnswer = true;
const listeners = [];

globalThis.window = {
  confirm: (message) => {
    confirms.push(message);
    return confirmAnswer;
  },
  htmx: { trigger: (el, name) => refreshes.push([el, name]) },
};
globalThis.htmx = window.htmx; // app.js (number formatting) looks for the htmx global
globalThis.document = { addEventListener: (name, fn) => listeners.push([name, fn]), activeElement: null };
globalThis.fetch = (url, opts = {}) =>
  new Promise((resolve) => {
    calls.push({
      url,
      headers: opts.headers,
      body: JSON.parse(opts.body),
      reply: (status, data) => resolve({ ok: status < 300, status, json: async () => data }),
    });
  });

vm.runInThisContext(readFileSync(new URL('app/static/js/app.js', ROOT), 'utf8'), { filename: 'app.js' });
vm.runInThisContext(readFileSync(new URL('app/static/js/dashboard.js', ROOT), 'utf8'), { filename: 'dashboard.js' });
window.mdsp.toast = (message, level, options) => {
  toasts.push(message);
  toastOptions.push(options);
};
const factories = {};
window.Alpine = { data: (name, factory) => (factories[name] = factory) };
for (const [name, fn] of listeners) if (name === 'alpine:init') fn();

const settle = () => new Promise((resolve) => setImmediate(resolve));

function mount(cards, preset) {
  const page = makePage(cards, preset);
  const component = factories.outputCards();
  component.$el = page.section;
  component.init();
  const click = (target) => page.section.listeners.click[0]({ target });
  const master = (data) => page.section.listeners['sse:master'][0]({ detail: { data: JSON.stringify(data) } });
  return { ...page, component, click, master };
}

const nudge = (card, step) => card.querySelectorAll('[data-action="nudge"]').find((b) => b.dataset.step === String(step));
const muteButton = (card) => card.querySelector('[data-action="mute"]');

beforeEach(() => {
  calls = [];
  refreshes = [];
  toasts = [];
  toastOptions = [];
  confirms = [];
  confirmAnswer = true;
});

describe('nudgedGain', () => {
  const { nudgedGain } = window.mdsp.dashboard;
  test('steps on the 0.1 dB grid inside the floor and the ceiling', () => {
    assert.equal(nudgedGain(0, 0.5, -72, 12), 0.5);
    assert.equal(nudgedGain(-3.04, -0.5, -72, 12), -3.5);
    assert.equal(nudgedGain(11.8, 0.5, -72, 12), 12);
    assert.equal(nudgedGain(12, 0.5, -72, 12), null);
    assert.equal(nudgedGain(-72, -0.5, -72, 12), null);
    assert.equal(nudgedGain(NaN, 0.5, -72, 12), null);
  });
});

describe('outputCards', () => {
  test('a nudge shows at once, posts through the edit endpoint and re-fetches the cards', async () => {
    const card = makeCard(0, { gain: -3 });
    const page = mount([card]);
    page.click(nudge(card, 0.5));
    assert.equal(card.dataset.gain, '-2.5');
    assert.equal(card.querySelector('[data-gain-text]').textContent, '−2.5 dB');
    assert.match(card.querySelector('[data-sync-badge]').className, /\bsync-pending\b/);
    assert.equal(card.querySelector('.sync-icon').textContent, '⟳');
    assert.equal(calls.length, 1);
    assert.equal(calls[0].url, '/ui/devices/1/presets/0/edit');
    assert.equal(calls[0].headers['HX-Request'], 'true');
    assert.deepEqual(calls[0].body, { values: { 'outputs.0.gain_db': -2.5 }, commit: true, confirm: false });
    assert.equal(refreshes.length, 0);
    calls[0].reply(200, { values: { 'outputs.0.gain_db': -2.5 } });
    await settle();
    assert.deepEqual(refreshes, [[page.box, 'refresh']]);
  });

  test('one request per output in flight; the newest values wait and are sent together', async () => {
    const card = makeCard(1, { gain: 0 });
    const page = mount([card]);
    const up = nudge(card, 0.5);
    page.click(up);
    page.click(up);
    page.click(up);
    page.click(muteButton(card));
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0].body.values, { 'outputs.1.gain_db': 0.5 });
    calls[0].reply(200, {});
    await settle();
    assert.equal(calls.length, 2);
    assert.deepEqual(calls[1].body.values, { 'outputs.1.gain_db': 1.5, 'outputs.1.mute': true });
    assert.equal(refreshes.length, 0); // still busy
    calls[1].reply(200, {});
    await settle();
    assert.equal(refreshes.length, 1);
  });

  test('the buttons stop at the ceiling and the floor', () => {
    const card = makeCard(0, { gain: 11.5, ceiling: 12 });
    const page = mount([card]);
    page.click(nudge(card, 0.5));
    assert.equal(card.dataset.gain, '12');
    assert.equal(nudge(card, 0.5).disabled, true);
    page.click(nudge(card, 0.5)); // a disabled button does nothing
    assert.equal(calls.length, 1);
  });

  test('mute toggles; un-muting a never-synced output asks first', async () => {
    const card = makeCard(2, { mute: true, neverSynced: true });
    const page = mount([card]);
    confirmAnswer = false;
    page.click(muteButton(card));
    assert.equal(confirms.length, 1);
    assert.match(confirms[0], /Out 3 has never been sent/);
    assert.equal(calls.length, 0);
    assert.equal(card.dataset.mute, 'true');

    confirmAnswer = true;
    page.click(muteButton(card));
    assert.deepEqual(calls[0].body.values, { 'outputs.2.mute': false });
    assert.equal(muteButton(card).attributes['aria-pressed'], 'false');
    assert.equal(muteButton(card).textContent, 'Mute');
    calls[0].reply(200, {});
    await settle();

    page.click(muteButton(card)); // muting never asks
    assert.equal(confirms.length, 2);
    assert.deepEqual(calls[1].body.values, { 'outputs.2.mute': true });
    assert.equal(muteButton(card).attributes['aria-pressed'], 'true');
    assert.match(card.querySelector('[data-gain-text]').textContent, /\(muted\)$/);
  });

  test('mute leaves an Undo toast that puts the previous state back', async () => {
    const card = makeCard(0, { mute: false });
    card.dataset.name = 'Shakers';
    const page = mount([card]);
    page.click(muteButton(card));
    assert.deepEqual(toasts, ['Shakers muted.']);
    const action = toastOptions[0].action;
    assert.equal(action.label, 'Undo');
    calls[0].reply(200, {});
    await settle();
    action.onClick();
    assert.equal(card.dataset.mute, 'false');
    assert.equal(muteButton(card).textContent, 'Mute');
    assert.deepEqual(calls[1].body.values, { 'outputs.0.mute': false });
    assert.equal(toasts.length, 1); // the undo itself leaves no new toast

    // un-muting says so too, and its Undo mutes again
    calls[1].reply(200, {});
    await settle();
    page.click(muteButton(card));
    page.click(muteButton(card));
    assert.deepEqual(toasts.slice(1), ['Shakers muted.', 'Shakers un-muted.']);
    // an Undo for a state that changed since does nothing
    toastOptions[1].action.onClick();
    assert.equal(card.dataset.mute, 'false');
  });

  test('Undo after the playing preset changed leaves the other preset alone', async () => {
    const card = makeCard(1, { mute: false });
    card.dataset.name = 'Shakers';
    const page = mount([card], '0');
    page.click(muteButton(card));
    calls[0].reply(200, {});
    await settle();
    // the device switched to preset 2 and the cards were re-fetched: the same output there is muted too
    const other = makeCard(1, { mute: true });
    const list = new El('output-cards', { editUrl: '/ui/devices/1/presets/1/edit', preset: '1' }, [other]);
    page.box.children = [list];
    list.parent = page.box;
    toastOptions[0].action.onClick();
    assert.equal(calls.length, 1); // nothing posted to preset 2
    assert.equal(other.dataset.mute, 'true');
    assert.deepEqual(toasts, ['Shakers muted.', 'Shakers was left as it is: the device has switched presets since.']);
  });

  test('a big step asks and resends with confirm; a refusal toasts and re-fetches the stored values', async () => {
    const card = makeCard(0);
    const page = mount([card]);
    page.click(nudge(card, 0.5));
    calls[0].reply(409, { error: 'confirmation_required', detail: 'gain raise of 6.5 dB is above 6 dB' });
    await settle();
    assert.match(confirms[0], /above 6 dB/);
    assert.equal(calls.length, 2);
    assert.equal(calls[1].body.confirm, true);
    calls[1].reply(422, { error: 'invalid', detail: 'outputs.0.gain_db: above the ceiling' });
    await settle();
    assert.deepEqual(toasts, ['outputs.0.gain_db: above the ceiling']);
    assert.equal(refreshes.length, 1);

    confirmAnswer = false;
    page.click(nudge(card, 0.5));
    calls[2].reply(409, { error: 'confirmation_required', detail: 'big step' });
    await settle();
    assert.equal(calls.length, 3); // declined: not resent
    assert.equal(refreshes.length, 2);
  });

  test('re-fetches only when the active preset changes, and holds swaps while a change is on its way', async () => {
    const card = makeCard(0);
    const page = mount([card], '0');
    page.master({ master: { volume: -20, preset: 0 } });
    page.master({ master: { volume: -19.5 } });
    assert.equal(refreshes.length, 0);
    page.master({ master: { preset: 2 } });
    assert.equal(refreshes.length, 1);

    page.click(nudge(card, 0.5));
    const swap = { detail: { shouldSwap: true } };
    page.box.listeners['htmx:beforeSwap'][0](swap);
    assert.equal(swap.detail.shouldSwap, false);
    calls[0].reply(200, {});
    await settle();
    const later = { detail: { shouldSwap: true } };
    page.box.listeners['htmx:beforeSwap'][0](later);
    assert.equal(later.detail.shouldSwap, true);
  });

  test('re-fetches when Mute all changes, since every health badge then says Silent', () => {
    const page = mount([makeCard(0)], '0');
    page.list.dataset.masterMute = 'false';
    page.master({ master: { volume: -20, mute: false, preset: 0 } });
    assert.equal(refreshes.length, 0);
    page.master({ master: { mute: true } });
    assert.equal(refreshes.length, 1);
    page.list.dataset.masterMute = ''; // cards not loaded: nothing to compare with
    page.master({ master: { mute: false } });
    assert.equal(refreshes.length, 1);
  });
});

// --- Home's level rule (phase 9 design §4.3; app/web/home.py level_step is the same rule) -------------------------

describe('levelStep', () => {
  const { levelStep } = window.mdsp.dashboard;
  const shakers = (output, routes) => ({ index: 0, output, ceiling: 12, gainMin: -72, routes });

  test('+ below the limit raises the output level', () => {
    assert.deepEqual(levelStep(shakers(11, [{ input: 0, route: 4 }]), 1), {
      values: { 'outputs.0.gain_db': 11.5 },
      target: 'output',
    });
    assert.deepEqual(levelStep(shakers(11.8, [{ input: 0, route: 4 }]), 1).values, { 'outputs.0.gain_db': 12 });
  });

  test('+ at the limit raises every enabled route that is still below it', () => {
    const plan = levelStep(shakers(12, [{ input: 0, route: 4 }, { input: 1, route: 12 }, { input: 2, route: 11.8 }]), 1);
    assert.equal(plan.target, 'routes');
    assert.deepEqual(plan.values, { 'inputs.0.routing.0.gain_db': 4.5, 'inputs.2.routing.0.gain_db': 12 });
  });

  test('+ with the output and every route at the limit, or nothing routed, does nothing', () => {
    assert.deepEqual(levelStep(shakers(12, [{ input: 0, route: 12 }]), 1), { values: {}, target: 'none' });
    assert.deepEqual(levelStep(shakers(12, []), 1), { values: {}, target: 'none' });
  });

  test('− lowers the output level only, down to the minimum', () => {
    assert.deepEqual(levelStep(shakers(12, [{ input: 0, route: 12 }]), -1), {
      values: { 'outputs.0.gain_db': 11.5 },
      target: 'output',
    });
    assert.deepEqual(levelStep(shakers(-71.8, []), -1).values, { 'outputs.0.gain_db': -72 });
    assert.deepEqual(levelStep(shakers(-72, []), -1), { values: {}, target: 'none' });
  });
});

describe('totalView', () => {
  const { totalView } = window.mdsp.dashboard;
  test('the big number and its breakdown read as the summary engine writes them', () => {
    assert.deepEqual(totalView({ output: 12, routes: [{ name: 'In 1', in: 0, route: 4 }] }), {
      text: '+16.0 dB',
      breakdown: 'route +4 + output +12',
    });
    assert.deepEqual(totalView({ output: 0, routes: [] }), { text: '0.0 dB', breakdown: 'output 0' });
    assert.deepEqual(totalView({ output: -3.5, routes: [{ name: 'In 1', in: -2, route: 0 }] }), {
      text: '−5.5 dB',
      breakdown: 'In 1 level −2 + route 0 + output −3.5',
    });
    assert.deepEqual(
      totalView({ output: 12, routes: [{ name: 'In 1', in: 0, route: 4 }, { name: 'In 2', in: 0, route: -2 }] }),
      { text: '+10 to +16 dB', breakdown: 'In 1: route +4 + output +12 · In 2: route −2 + output +12' },
    );
  });
});

describe('Home cards', () => {
  function homeCard(o, { gain, routes, ceiling = 12 }) {
    const step = (s) => new El('home-step', { action: 'level', step: String(s) });
    return new El('output-card home-card', {
      output: String(o), name: 'Shakers', gain: String(gain), mute: 'false', ceiling: String(ceiling),
      gainMin: '-72', neverSynced: 'false', routes: JSON.stringify(routes),
    }, [
      new El('home-level', {}, [step(-0.5), new El('home-total-value', { totalText: '' }), step(0.5)]),
      new El('home-breakdown', { breakdown: '' }),
      new El('home-note', { note: 'routes' }),
      new El('home-note', { note: 'max' }),
      new El('home-note', { note: 'min' }),
    ]);
  }
  const levelButton = (card, s) => card.querySelectorAll('[data-action="level"]').find((b) => b.dataset.step === String(s));
  const note = (card, name) => card.querySelectorAll('[data-note]').find((n) => n.dataset.note === name);

  test('+ at the output limit raises the route, and the total and breakdown follow', async () => {
    const card = homeCard(0, { gain: 12, routes: [{ input: 0, name: 'In 1', in: 0, route: 4 }] });
    const page = mount([card]);
    page.click(levelButton(card, 0.5));
    assert.deepEqual(calls[0].body.values, { 'inputs.0.routing.0.gain_db': 4.5 });
    assert.equal(card.querySelector('[data-total-text]').textContent, '+16.5 dB');
    assert.equal(card.querySelector('[data-breakdown]').textContent, 'route +4.5 + output +12');
    assert.equal(note(card, 'routes').hidden, false);
    assert.equal(note(card, 'max').hidden, true);
    assert.equal(JSON.parse(card.dataset.routes)[0].route, 4.5);
    calls[0].reply(200, {});
    await settle();
  });

  test('at both limits + is disabled and the maximum note shows; − lowers the output only', async () => {
    const card = homeCard(0, { gain: 11.5, routes: [{ input: 0, name: 'In 1', in: 0, route: 12 }] });
    const page = mount([card]);
    page.click(levelButton(card, 0.5));
    assert.deepEqual(calls[0].body.values, { 'outputs.0.gain_db': 12 });
    assert.equal(levelButton(card, 0.5).disabled, true);
    assert.equal(note(card, 'max').hidden, false);
    assert.equal(note(card, 'routes').hidden, true);
    calls[0].reply(200, {});
    await settle();
    page.click(levelButton(card, -0.5));
    assert.deepEqual(calls[1].body.values, { 'outputs.0.gain_db': 11.5 });
    assert.equal(levelButton(card, 0.5).disabled, false);
    calls[1].reply(200, {});
    await settle();
  });
});
