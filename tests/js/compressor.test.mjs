// The compressor tab's threshold field (compressor.js), driven without a DOM: a stub fetch plays the JSON API. The
// threshold is a signed, string-backed field (phase 9 P01): a typed "-" must never turn "-10" into "10".
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { beforeEach, describe, test } from 'node:test';
import vm from 'node:vm';

import { ROOT } from './load.mjs';

const listeners = [];
const toasts = [];
let calls = [];

globalThis.window = { confirm: () => true };
globalThis.document = { addEventListener: (name, fn) => listeners.push([name, fn]) };
globalThis.fetch = (url, opts = {}) =>
  new Promise((resolve) => {
    calls.push({
      url,
      method: opts.method || 'GET',
      body: opts.body === undefined ? undefined : JSON.parse(opts.body),
      reply: (status, data) => resolve({ ok: status < 300, status, json: async () => data }),
    });
  });

for (const path of ['app/static/js/peq_editor.js', 'app/static/js/compressor.js']) {
  vm.runInThisContext(readFileSync(new URL(path, ROOT), 'utf8'), { filename: path });
}
window.mdsp.toast = (message) => toasts.push(message);
const factories = {};
window.Alpine = { data: (name, factory) => (factories[name] = factory) };
for (const [name, fn] of listeners) if (name === 'alpine:init') fn();

const settle = () => new Promise((resolve) => setImmediate(resolve));
const puts = () => calls.filter((c) => c.method === 'PUT');
const RANGES = { threshold_db: [-90, 0], ratio: [1, 50], attack_ms: [1, 1000], release_ms: [1, 3000] };

function view(threshold = -20) {
  return {
    compressor: { threshold_db: threshold, ratio: 4, attack_ms: 15, release_ms: 30, bypass: false },
    sync_status: {},
    presets: {},
    ranges: RANGES,
    notes: [],
  };
}

async function editor(threshold = -20) {
  const ed = factories.compressorEditor({ channel: 0, preset: 0, url: '/c', stepDelayMs: 0 });
  const loading = ed.load();
  await settle();
  calls.pop().reply(200, view(threshold));
  await loading;
  return ed;
}

beforeEach(() => {
  calls = [];
  toasts.length = 0;
});

describe('compressor threshold', () => {
  test('the text field follows the stored threshold', async () => {
    const ed = await editor(-36);
    assert.equal(ed.thresholdText, '-36');
  });

  test('typing "-10" key by key sends -10, never +10', async () => {
    const ed = await editor(0);
    for (const text of ['-', '-1', '-10']) {
      ed.thresholdText = text;
      ed.onThresholdInput();
      assert.ok(!(ed.form.threshold_db > 0), `a typed minus must never become positive (${text})`);
    }
    assert.equal(ed.form.threshold_db, -10);
    ed.commitThreshold();
    await settle();
    assert.equal(puts().length, 1);
    assert.deepEqual(puts()[0].body, { threshold_db: -10 });
  });

  test('a lone "-" is not committed and the stored value comes back', async () => {
    const ed = await editor(-20);
    ed.thresholdText = '-';
    ed.onThresholdInput();
    assert.equal(ed.form.threshold_db, -20);
    ed.commitThreshold();
    await settle();
    assert.equal(puts().length, 0);
    assert.equal(toasts.length, 1);
    assert.equal(ed.thresholdText, '-20');
  });

  test('the minus and plus steppers move 1 dB and stop at the range', async () => {
    const ed = await editor(-1);
    ed.stepThreshold(1);
    assert.equal(ed.thresholdText, '0');
    ed.stepThreshold(1);
    assert.equal(ed.form.threshold_db, 0);
    assert.equal(ed.canStep(1), false);
    ed.stepThreshold(-1);
    ed.stepThreshold(-1);
    assert.equal(ed.form.threshold_db, -2);
    await new Promise((resolve) => setTimeout(resolve, 5));
    await settle();
    assert.equal(puts().length, 1); // quick steps are sent once
    assert.deepEqual(puts()[0].body, { threshold_db: -2 });
  });

  test('a step made while the previous one is on its way is sent after it, not lost', async () => {
    const ed = await editor(-10);
    const wait = () => new Promise((resolve) => setTimeout(resolve, 5));
    ed.stepThreshold(-1);
    await wait();
    assert.deepEqual(puts()[0].body, { threshold_db: -11 });
    ed.stepThreshold(-1); // -12 while -11 is on its way
    await wait();
    assert.equal(puts().length, 1); // waits for the reply
    puts()[0].reply(200, { view: view(-11), sync: {} });
    await settle();
    assert.equal(ed.thresholdText, '-12'); // the reply doesn't undo the newer step
    await wait();
    await settle();
    assert.equal(puts().length, 2);
    assert.deepEqual(puts()[1].body, { threshold_db: -12 });
  });
});
