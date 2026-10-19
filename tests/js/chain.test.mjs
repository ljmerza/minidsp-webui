// Phase 9 chunk 14 (design §8): the plain editors' words and the Band tab's one Steepness select, driven without a
// DOM. A stub fetch plays the server and the Alpine factories are called directly (no graph, so render() is a no-op).
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
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
globalThis.fetch = (url, opts = {}) =>
  new Promise((resolve) => {
    calls.push({
      url,
      method: opts.method || 'GET',
      body: opts.body === undefined ? undefined : JSON.parse(opts.body),
      reply: (status, data) => resolve({ ok: status < 300, status, json: async () => data }),
    });
  });

for (const path of ['app/static/js/dsp.js', 'app/static/js/peq_editor.js', 'app/static/js/crossover.js', 'app/static/js/compressor.js']) {
  vm.runInThisContext(readFileSync(new URL(path, ROOT), 'utf8'), { filename: path });
}
window.mdsp.toast = (message) => toasts.push(message);
const factories = {};
window.Alpine = { data: (name, factory) => (factories[name] = factory) };
for (const [name, fn] of listeners) if (name === 'alpine:init') fn();

const { bandSentence, steepKey, steepOptions } = window.mdsp.crossover;
const { inWords, presetText } = window.mdsp.compressor;

const settle = () => new Promise((resolve) => setImmediate(resolve));
const FLAT = { b0: 1, b1: 0, b2: 0, a1: 0, a2: 0 };
const posts = () => calls.filter((c) => c.method === 'POST');

beforeEach(() => {
  calls = [];
  toasts.length = 0;
});

const design = (kind, freq, order = 4, family = 'lr', extra = {}) => ({ kind, family, order, freq, freq_hi: null, bessel_norm: 'phase', ...extra });
const group = (k, d, { bypass = false, biquads = 2 } = {}) => ({
  index: k, path: `outputs.0.crossover.${k}`, design: d, compiled: [FLAT, FLAT, FLAT, FLAT], bypass, label: 'X', biquads_used: biquads,
});

describe('band sentence', () => {
  test('a band between a high-pass and a low-pass', () => {
    assert.equal(
      bandSentence([group(0, design('lp', 80)), group(1, design('hp', 20))]),
      'This output plays 20–80 Hz. Below 20 Hz and above 80 Hz it fades by 24 dB per octave.',
    );
  });

  test('different slopes on each side', () => {
    assert.equal(
      bandSentence([group(0, design('lp', 80, 8)), group(1, design('hp', 20, 2))]),
      'This output plays 20–80 Hz. Below 20 Hz it fades by 12 dB per octave, above 80 Hz by 48.',
    );
  });

  test('a low-pass only, and a filter that is off counts for nothing', () => {
    assert.equal(
      bandSentence([group(0, design('lp', 80)), group(1, design('hp', 20), { bypass: true })]),
      'This output plays everything below 80 Hz. Above it, sound fades by 24 dB per octave.',
    );
    assert.equal(
      bandSentence([group(0, design('hp', 2500, 2)), group(1, null, { bypass: true, biquads: 0 })]),
      'This output plays everything above 2.5 kHz. Below it, sound fades by 12 dB per octave.',
    );
  });

  test('full range, custom coefficients and a band that passes nothing', () => {
    assert.equal(bandSentence([group(0, null, { biquads: 0 }), group(1, design('lp', 80), { bypass: true })]),
      'This output plays the full range: nothing is filtered out.');
    assert.match(bandSentence([group(0, null, { biquads: 2 })]), /custom filter/);
    assert.equal(
      bandSentence([group(0, design('lp', 40)), group(1, design('hp', 60))]),
      'This output plays nothing: its high-pass (60 Hz) is above its low-pass (40 Hz).',
    );
  });

  test('a band-pass is a high-pass and a low-pass', () => {
    assert.equal(
      bandSentence([group(0, design('bp', 30, 2, 'lr', { freq_hi: 120 }))]),
      'This output plays 30–120 Hz. Below 30 Hz and above 120 Hz it fades by 12 dB per octave.',
    );
  });
});

describe('steepness', () => {
  test('the three Linkwitz-Riley choices, a custom design and Other…', () => {
    assert.equal(steepKey({ family: 'lr', order: 4 }), 'lr4');
    assert.equal(steepKey({ family: 'bw', order: 4 }), 'custom');
    const keys = steepOptions({ kind: 'lp', family: 'bw', order: 3 }, 4).map((o) => o.key);
    assert.deepEqual(keys, ['lr2', 'lr4', 'lr8', 'custom', 'other']);
    const custom = steepOptions({ kind: 'lp', family: 'bw', order: 3 }, 4).find((o) => o.key === 'custom');
    assert.equal(custom.label, 'Custom: Butterworth 18 dB/oct');
    assert.deepEqual(steepOptions({ kind: 'lp', family: 'lr', order: 4 }, 4).map((o) => o.key), ['lr2', 'lr4', 'lr8', 'other']);
  });

  test('48 dB/oct is too steep for a band-pass in 4 filter stages', () => {
    const lr8 = steepOptions({ kind: 'bp', family: 'lr', order: 4 }, 4).find((o) => o.key === 'lr8');
    assert.equal(lr8.disabled, true);
  });
});

async function crossoverEditor(groups) {
  const ed = factories.crossoverEditor({ channel: 0, preset: 0, name: 'Out 1', nBiquads: 4, fs: 96000, stateUrl: '/x', groupUrl: '/g/{group}', presetUrl: '/x' });
  const loading = ed.load();
  await settle();
  calls.pop().reply(200, { sync: {}, groups, presets: [] });
  await settle();
  await loading;
  return ed;
}

describe('crossoverEditor steepness select', () => {
  test('"Steep, 24 dB/oct (LR4)" writes the same design as Linkwitz-Riley + 24 dB/oct under Advanced', async () => {
    const start = design('lp', 80, 3, 'bw');
    const viaSteepness = await crossoverEditor([group(0, start), group(1, null, { bypass: true, biquads: 0 })]);
    viaSteepness.setSteepness(viaSteepness.groups[0], 'lr4');
    await settle();
    const fromSelect = posts()[0].body.design;

    calls = [];
    const viaAdvanced = await crossoverEditor([group(0, start), group(1, null, { bypass: true, biquads: 0 })]);
    viaAdvanced.groups[0].form.family = 'lr'; // today's selects: x-model, then commit on change
    viaAdvanced.groups[0].form.order = 4;
    viaAdvanced.commit(viaAdvanced.groups[0]);
    await settle();
    assert.deepEqual(fromSelect, { kind: 'lp', family: 'lr', order: 4, freq: 80 });
    assert.deepEqual(posts().at(-1).body.design, fromSelect);
  });

  test('Other… opens Advanced and sends nothing; a custom design opens it from the start', async () => {
    const ed = await crossoverEditor([group(0, design('lp', 80)), group(1, design('hp', 20, 3, 'bw'))]);
    assert.equal(ed.isAdvanced(ed.groups[0]), false);
    assert.equal(ed.isAdvanced(ed.groups[1]), true);
    const select = { value: 'other' };
    ed.setSteepness(ed.groups[0], 'other', select);
    await settle();
    assert.equal(ed.isAdvanced(ed.groups[0]), true);
    assert.equal(select.value, 'lr4');
    assert.equal(posts().length, 0);
    assert.equal(ed.kindName(ed.groups[0]), 'Low-pass');
    assert.equal(ed.sentence(), 'This output plays 20–80 Hz. Below 20 Hz it fades by 18 dB per octave, above 80 Hz by 24.');
  });

  test('a choice that needs too many filter stages is refused with a message', async () => {
    const ed = await crossoverEditor([group(0, design('bp', 30, 2, 'lr', { freq_hi: 120 })), group(1, null, { bypass: true, biquads: 0 })]);
    ed.setSteepness(ed.groups[0], 'lr8', { value: 'lr8' });
    await settle();
    assert.equal(posts().length, 0);
    assert.match(toasts[0], /filter stages/);
  });
});

describe('compressor in words', () => {
  test('threshold and ratio as a sentence', () => {
    assert.equal(inWords(-36, 4, false), 'Above −36 dBFS, every 4 dB louder comes out only 1 dB louder.');
    assert.equal(
      inWords(0, 50, false),
      'Above 0 dBFS, every 50 dB louder comes out only 1 dB louder. At this ratio it acts as a limiter.',
    );
    assert.equal(inWords(-20, 1, false), 'Ratio 1:1 changes nothing, even above −20 dBFS.');
    assert.equal(inWords(-36, 4, true), 'Off: this output passes through unchanged.');
  });

  test('template buttons carry their values', () => {
    const p = { label: 'Shaker protection', values: { threshold_db: -36, ratio: 50, attack_ms: 15, release_ms: 30 } };
    assert.equal(presetText(p), 'Shaker protection (−36, 50:1, 15, 30)');
  });
});
