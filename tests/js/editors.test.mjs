// State logic of the PEQ editor (peq_editor.js) and the crossover designer (crossover.js), driven without a DOM: a
// stub fetch plays the server, and the Alpine factories are called directly (no graph, so render() is a no-op).
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { beforeEach, describe, test } from 'node:test';
import vm from 'node:vm';

import { ROOT } from './load.mjs';

const listeners = [];
const toasts = [];
let confirmAnswer = true;
let calls = [];

globalThis.window = { confirm: () => confirmAnswer };
globalThis.document = { addEventListener: (name, fn) => listeners.push([name, fn]) };
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
// Every request waits until the test answers it: calls[k].reply(status, data).
globalThis.fetch = (url, opts = {}) =>
  new Promise((resolve) => {
    calls.push({
      url,
      method: opts.method || 'GET',
      body: opts.body === undefined ? undefined : JSON.parse(opts.body),
      reply: (status, data) => resolve({ ok: status < 300, status, json: async () => data }),
    });
  });

for (const path of ['app/static/js/dsp.js', 'app/static/js/peq_editor.js', 'app/static/js/crossover.js']) {
  vm.runInThisContext(readFileSync(new URL(path, ROOT), 'utf8'), { filename: path });
}
window.mdsp.toast = (message) => toasts.push(message);
const factories = {};
window.Alpine = { data: (name, factory) => (factories[name] = factory) };
for (const [name, fn] of listeners) if (name === 'alpine:init') fn();

const settle = () => new Promise((resolve) => setImmediate(resolve));
const FLAT = { b0: 1, b1: 0, b2: 0, a1: 0, a2: 0 };
const posts = () => calls.filter((c) => c.method === 'POST');

async function answer(call, status, data) {
  call.reply(status, data);
  await settle();
  await settle();
}

beforeEach(() => {
  calls = [];
  toasts.length = 0;
  confirmAnswer = true;
});

// --- PEQ editor ---------------------------------------------------------------------------------------------------

const PEQ = { type: 'peaking', freq: 100, q: 1, gain_db: -3 };

function bandView(k, { design = PEQ, bypass = true } = {}) {
  return { index: k, path: `outputs.0.peq.${k}`, design, compiled: FLAT, bypass, label: 'PEQ', source: 'designed', external: false };
}

async function peqEditor(bands = [bandView(0), bandView(1)]) {
  const ed = factories.peqEditor({ side: 'outputs', channel: 0, preset: 0, name: 'Out 1', fs: 96000, stateUrl: '/s', bandUrl: '/b/{band}', bankUrl: '/bank' });
  const loading = ed.load();
  await settle();
  await answer(calls.pop(), 200, { sync: {}, bands });
  await loading;
  return ed;
}

async function reload(ed, bands) {
  const loading = ed.load();
  await settle();
  await answer(calls.filter((c) => c.method === 'GET').at(-1), 200, { sync: {}, bands });
  await loading;
}

describe('peqEditor', () => {
  test('a bypass toggle made while a design is on its way is still sent', async () => {
    const ed = await peqEditor();
    ed.bands[0].form.freq = 200;
    ed.commitForm(ed.bands[0]);
    await settle();
    ed.setEnabled(ed.bands[0], true);
    ed.bands[0].form.freq = 300;
    ed.commitForm(ed.bands[0]);
    assert.equal(posts().length, 1);
    await answer(posts()[0], 200, { sync: {}, band: bandView(0, { design: { ...PEQ, freq: 200 } }) });
    assert.equal(posts().length, 2);
    const second = posts()[1].body;
    assert.equal(second.bypass, false);
    assert.equal(second.design.freq, 300);
    await answer(posts()[1], 200, { sync: {}, band: bandView(0, { design: { ...PEQ, freq: 300 }, bypass: false }) });
    assert.equal(ed.bands[0].bypass, false);
    assert.equal(ed.bands[0].form.freq, 300);
  });

  test('a newer design replaces a waiting raw change, and the other way round', async () => {
    const ed = await peqEditor();
    ed.commitForm(ed.bands[0]);
    await settle();
    ed.queue(ed.bands[0], { raw: FLAT });
    ed.queue(ed.bands[0], { design: PEQ });
    assert.deepEqual(Object.keys(ed.bands[0].queued), ['design']);
    ed.queue(ed.bands[0], { raw: FLAT });
    assert.deepEqual(Object.keys(ed.bands[0].queued), ['raw']);
  });

  test('a reload keeps a typed value that is not committed yet', async () => {
    const ed = await peqEditor();
    const band = ed.bands[0];
    band.form.q = 4;
    ed.onFieldInput(band);
    await reload(ed, [bandView(0), bandView(1, { bypass: false })]);
    assert.equal(ed.bands[0], band);
    assert.equal(ed.bands[0].form.q, 4);
    assert.equal(ed.bands[1].bypass, false); // the other band follows the server
    ed.commitForm(ed.bands[0]);
    await settle();
    assert.equal(posts().length, 1);
    assert.equal(posts()[0].body.design.q, 4);
    await answer(posts()[0], 200, { sync: {}, band: bandView(0, { design: { ...PEQ, q: 4 } }) });
    await reload(ed, [bandView(0, { design: { ...PEQ, q: 4 }, bypass: false }), bandView(1)]);
    assert.equal(ed.bands[0].bypass, false); // committed: reloads apply again
  });

  test('typing while a request is on its way keeps the typed value', async () => {
    const ed = await peqEditor();
    ed.bands[0].form.freq = 200;
    ed.commitForm(ed.bands[0]);
    await settle();
    ed.bands[0].form.gain_db = -6;
    ed.onFieldInput(ed.bands[0]);
    await answer(posts()[0], 200, { sync: {}, band: bandView(0, { design: { ...PEQ, freq: 200 }, bypass: false }) });
    assert.equal(ed.bands[0].form.gain_db, -6);
    assert.equal(ed.bands[0].bypass, false); // stored fields still follow the reply
  });

  test('a refused change puts the stored band back', async () => {
    const ed = await peqEditor();
    ed.bands[0].form.freq = 250;
    ed.commitForm(ed.bands[0]);
    await settle();
    await answer(posts()[0], 422, { error: 'invalid', detail: 'nope', band: bandView(0) });
    assert.equal(ed.bands[0].form.freq, 100);
    assert.equal(ed.bands[0].inflight, false);
    assert.deepEqual(toasts, ['Band 1: nope']);
  });

  test('confirmation: asks, then repeats with confirm; a "no" reverts', async () => {
    const ed = await peqEditor();
    ed.bands[0].form.gain_db = 12;
    ed.commitForm(ed.bands[0]);
    await settle();
    await answer(posts()[0], 409, { error: 'confirmation_required', detail: 'raising it needs confirmation', band: bandView(0) });
    assert.equal(posts()[1].body.confirm, true);
    await answer(posts()[1], 200, { sync: {}, band: bandView(0, { design: { ...PEQ, gain_db: 12 } }) });
    assert.equal(ed.bands[0].form.gain_db, 12);

    confirmAnswer = false;
    ed.bands[1].form.gain_db = 12;
    ed.commitForm(ed.bands[1]);
    await settle();
    await answer(posts()[2], 409, { error: 'confirmation_required', detail: 'raising it needs confirmation', band: bandView(1) });
    assert.equal(posts().length, 3);
    assert.equal(ed.bands[1].form.gain_db, -3);
    assert.deepEqual(toasts, []);
  });

  test('typed values are clamped to the field ranges', async () => {
    const ed = await peqEditor();
    Object.assign(ed.bands[0].form, { gain_db: 200, q: 500, freq: 99999 });
    ed.commitForm(ed.bands[0]);
    await settle();
    const { design } = posts()[0].body;
    assert.deepEqual([design.gain_db, design.q, design.freq], [20, 30, 24000]);
  });
});

// --- crossover designer -------------------------------------------------------------------------------------------

const LR4 = { kind: 'lp', family: 'lr', order: 4, freq: 80, freq_hi: null, bessel_norm: 'phase' };
const BESSEL_MAG = { kind: 'lp', family: 'bessel', order: 4, freq: 80, freq_hi: null, bessel_norm: 'mag' };

function groupView(k, { design = LR4, bypass = true, compiled = [FLAT, FLAT, FLAT, FLAT] } = {}) {
  return { index: k, path: `outputs.0.crossover.${k}`, design, compiled, bypass, label: 'X', biquads_used: 0 };
}

async function crossoverEditor(groups = [groupView(0), groupView(1)]) {
  const ed = factories.crossoverEditor({ channel: 0, preset: 0, name: 'Out 1', nBiquads: 4, fs: 96000, stateUrl: '/x', groupUrl: '/g/{group}', presetUrl: '/x' });
  const loading = ed.load();
  await settle();
  await answer(calls.pop(), 200, { sync: {}, groups, presets: [] });
  await loading;
  return ed;
}

async function reloadGroups(ed, groups) {
  const loading = ed.load();
  await settle();
  await answer(calls.filter((c) => c.method === 'GET').at(-1), 200, { sync: {}, groups });
  await loading;
}

describe('crossoverEditor', () => {
  test('a Bessel group keeps its normalization when a field changes', async () => {
    const ed = await crossoverEditor([groupView(0, { design: BESSEL_MAG }), groupView(1)]);
    assert.equal(ed.groups[0].form.bessel_norm, 'mag');
    ed.groups[0].form.freq = 81;
    ed.commit(ed.groups[0]);
    await settle();
    assert.deepEqual(posts()[0].body.design, { kind: 'lp', family: 'bessel', order: 4, freq: 81, bessel_norm: 'mag' });

    ed.groups[1].form.freq = 90; // not Bessel: no normalization sent
    ed.commit(ed.groups[1]);
    await settle();
    assert.equal('bessel_norm' in posts()[1].body.design, false);
  });

  test('an enable toggle made while a design is on its way is still sent', async () => {
    const ed = await crossoverEditor();
    ed.groups[0].form.freq = 70;
    ed.commit(ed.groups[0]);
    await settle();
    ed.setEnabled(ed.groups[0], true);
    ed.groups[0].form.freq = 60;
    ed.commit(ed.groups[0]);
    await answer(posts()[0], 200, { sync: {}, group: groupView(0, { design: { ...LR4, freq: 70 } }) });
    const second = posts()[1].body;
    assert.equal(second.bypass, false);
    assert.equal(second.design.freq, 60);
  });

  test('a reload keeps a typed frequency and typed drawer rows', async () => {
    const ed = await crossoverEditor();
    ed.groups[0].form.freq = 75;
    ed.onInput(ed.groups[0]);
    ed.toggleDrawer(ed.groups[1]);
    ed.rows[0].b0 = '0.5';
    const lp = { b0: 0.1, b1: 0.2, b2: 0.1, a1: 0.5, a2: -0.2 };
    await reloadGroups(ed, [groupView(0), groupView(1, { compiled: [lp, FLAT, FLAT, FLAT] })]);
    assert.equal(ed.groups[0].form.freq, 75);
    assert.equal(ed.rows[0].b0, '0.5');
    assert.deepEqual(ed.groups[1].compiled[0], lp);

    // Untouched rows follow the server.
    ed.toggleDrawer(ed.groups[1]);
    ed.toggleDrawer(ed.groups[1]);
    await reloadGroups(ed, [groupView(0), groupView(1)]);
    assert.equal(ed.rows[0].b0, '1');
  });

  test('a refused change puts the stored group back', async () => {
    const ed = await crossoverEditor();
    ed.groups[0].form.freq = 70;
    ed.commit(ed.groups[0]);
    await settle();
    await answer(posts()[0], 422, { error: 'invalid', detail: 'nope', group: groupView(0) });
    assert.equal(ed.groups[0].form.freq, 80);
    assert.deepEqual(toasts, ['Group 1: nope']);
  });
});
