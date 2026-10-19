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

// --- signed numbers (phase 9 P01) ---------------------------------------------------------------------------------

describe('parseSigned', () => {
  const { parseSigned } = window.mdsp.filters;

  test('a half-typed sign or point is kept as partial, never as a number', () => {
    for (const text of ['-', '-.', '+', '+.', '.', '−', ' - ']) {
      assert.deepEqual(parseSigned(text), { kind: 'partial', value: null }, JSON.stringify(text));
    }
  });

  test('signed numbers keep their sign', () => {
    assert.deepEqual(parseSigned('-4'), { kind: 'number', value: -4 });
    assert.deepEqual(parseSigned('4'), { kind: 'number', value: 4 });
    assert.deepEqual(parseSigned('+4'), { kind: 'number', value: 4 });
    assert.deepEqual(parseSigned('−4.5'), { kind: 'number', value: -4.5 }); // the UI's own minus sign, pasted
    assert.deepEqual(parseSigned('-.5'), { kind: 'number', value: -0.5 });
    assert.deepEqual(parseSigned(' 4. '), { kind: 'number', value: 4 });
    assert.deepEqual(parseSigned('-4,5'), { kind: 'number', value: -4.5 });
    assert.deepEqual(parseSigned(-3), { kind: 'number', value: -3 });
  });

  test('empty and invalid text', () => {
    assert.deepEqual(parseSigned(''), { kind: 'empty', value: null });
    assert.deepEqual(parseSigned(null), { kind: 'empty', value: null });
    for (const text of ['abc', '4-', '--4', '1e3', '4.5.1', '- 4']) assert.equal(parseSigned(text).kind, 'invalid', text);
  });
});

describe('peqEditor signed gain (Cut / Boost + magnitude)', () => {
  const UNUSED = { type: 'passthrough' };
  const BOOST = { ...PEQ, gain_db: 2 };

  test('the form shows Cut / Boost and a magnitude', async () => {
    const ed = await peqEditor([bandView(0), bandView(1, { design: BOOST }), bandView(2, { design: { ...PEQ, gain_db: 0 } })]);
    assert.deepEqual([ed.bands[0].form.cut, ed.bands[0].form.mag], [true, '3']);
    assert.deepEqual([ed.bands[1].form.cut, ed.bands[1].form.mag], [false, '2']);
    assert.deepEqual([ed.bands[2].form.cut, ed.bands[2].form.mag], [true, '0']); // 0 dB defaults to Cut
  });

  test('typing "-4" key by key never previews or commits a +4 boost', async () => {
    const ed = await peqEditor([bandView(0, { design: BOOST })]);
    const band = ed.bands[0];
    for (const text of ['-', '-4']) {
      band.form.mag = text;
      ed.onGainInput(band);
      assert.ok(!(band.form.gain_db > 0 && text === '-4'), 'a typed minus must never become a boost');
    }
    assert.equal(posts().length, 0); // typing alone sends nothing
    assert.equal(band.form.gain_db, -4);
    assert.equal(band.form.cut, true);
    ed.commitGain(band);
    await settle();
    assert.equal(posts().length, 1);
    assert.equal(posts()[0].body.design.gain_db, -4);
    assert.equal(band.form.mag, '4'); // the sign moved into the Cut / Boost choice
  });

  test('a lone "-" leaves the stored gain alone and is not committed', async () => {
    const ed = await peqEditor([bandView(0, { design: BOOST })]);
    const band = ed.bands[0];
    band.form.mag = '-';
    ed.onGainInput(band);
    assert.equal(band.form.gain_db, 2);
    ed.commitGain(band);
    await settle();
    assert.equal(posts().length, 0);
    assert.equal(toasts.length, 1);
    assert.match(toasts[0], /Band 1/);
    assert.deepEqual([ed.bands[0].form.gain_db, ed.bands[0].form.mag], [2, '2']); // put back
  });

  test('Cut + 4 stores -4, and an unsigned magnitude keeps the chosen sign', async () => {
    const ed = await peqEditor([bandView(0, { design: { ...PEQ, gain_db: 0 } }), bandView(1)]);
    const band = ed.bands[0];
    ed.setCut(band, true);
    band.form.mag = '4';
    ed.onGainInput(band);
    ed.commitGain(band);
    await settle();
    assert.equal(posts()[0].body.design.gain_db, -4);

    ed.bands[1].form.mag = '6'; // band 2 is a 3 dB cut
    ed.onGainInput(ed.bands[1]);
    ed.commitGain(ed.bands[1]);
    await settle();
    assert.equal(posts()[1].body.design.gain_db, -6);
  });

  test('switching Cut to Boost flips the stored gain at once', async () => {
    const ed = await peqEditor([bandView(0)]);
    ed.setCut(ed.bands[0], false);
    await settle();
    assert.equal(posts()[0].body.design.gain_db, 3);
    assert.equal(ed.bands[0].form.cut, false);
  });

  test('a clamped gain updates the magnitude and the sign', async () => {
    const ed = await peqEditor([bandView(0)]);
    ed.bands[0].form.gain_db = 200;
    ed.commitForm(ed.bands[0]);
    assert.deepEqual([ed.bands[0].form.cut, ed.bands[0].form.mag], [false, '20']);
  });

  test('picking a type on an unused band turns it on in the same request', async () => {
    const ed = await peqEditor([bandView(0, { design: UNUSED }), bandView(1, { design: UNUSED })]);
    const band = ed.bands[1];
    band.form.type = 'peaking';
    ed.onTypeChange(band);
    await settle();
    assert.equal(posts().length, 1);
    assert.equal(posts()[0].body.bypass, false);
    assert.equal(posts()[0].body.design.type, 'peaking');
  });

  test('an empty (all-zero) raw slot counts as unused, so picking a type turns it on', async () => {
    // A factory device and a readback store zeros in unused slots; the table shows them as "Raw".
    const ZEROS = { b0: 0, b1: 0, b2: 0, a1: 0, a2: 0 };
    const ed = await peqEditor([bandView(0, { design: { type: 'raw', raw: ZEROS } })]);
    ed.bands[0].form.type = 'peaking';
    ed.onTypeChange(ed.bands[0]);
    await settle();
    assert.equal(posts()[0].body.bypass, false);
  });

  test('a bypassed raw filter that is not empty keeps its on/off switch', async () => {
    const ed = await peqEditor([bandView(0, { design: { type: 'raw', raw: { ...FLAT, b0: 0.5 } } })]);
    ed.bands[0].form.type = 'peaking';
    ed.onTypeChange(ed.bands[0]);
    await settle();
    assert.equal('bypass' in posts()[0].body, false);
  });

  test('a type change on a band already in use leaves its on/off switch alone', async () => {
    const ed = await peqEditor([bandView(0, { bypass: true })]);
    ed.bands[0].form.type = 'lowshelf';
    ed.onTypeChange(ed.bands[0]);
    await settle();
    assert.equal('bypass' in posts()[0].body, false);
  });

  test('unused and switched-off bands say "off"', async () => {
    const ed = await peqEditor([
      bandView(0, { design: UNUSED }),
      bandView(1, { bypass: true }),
      bandView(2, { bypass: false }),
    ]);
    assert.equal(ed.statusText(ed.bands[0]), 'off');
    assert.equal(ed.statusText(ed.bands[1]), 'off');
    assert.notEqual(ed.statusText(ed.bands[2]), 'off');
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
    assert.deepEqual(toasts, ['Filter 1: nope']);
  });
});

// --- the EQ width hint (phase 9 design §8.2) ----------------------------------------------------------------------

describe('widthHint', () => {
  const { widthRange, widthHint } = window.mdsp.filters;

  test("matches the design's and the q help entry's examples", () => {
    assert.equal(widthHint(40, 4), 'Width (Q 4) at 40 Hz: affects about 35–45 Hz');
    assert.equal(widthHint(40, 1.4), 'Width (Q 1.4) at 40 Hz: affects about 28–57 Hz');
    assert.equal(widthHint(40, 8), 'Width (Q 8) at 40 Hz: affects about 38–43 Hz');
    const [lo, hi] = widthRange(40, 0.7);
    assert.ok(Math.abs(lo - 20.6) < 0.1 && Math.abs(hi - 77.7) < 0.1, `${lo}–${hi}`);
  });

  test('the edges sit symmetrically on a log scale, and high frequencies round to 10 Hz', () => {
    const [lo, hi] = widthRange(1000, 2);
    assert.ok(Math.abs(lo * hi - 1000 * 1000) < 1e-6);
    assert.equal(widthHint(3000, 2), 'Width (Q 2) at 3000 Hz: affects about 2340–3840 Hz');
  });

  test('says nothing for a missing or non-positive frequency or Q', () => {
    for (const [f, q] of [[null, 4], [40, 0], [40, ''], ['', 4], [-40, 4]]) assert.equal(widthHint(f, q), '');
  });
});

describe('width range per band (phase 9 design §8.2, §8.3)', () => {
  const { widthRange, rangeText, widthHint } = window.mdsp.filters;

  test('rangeText is the hint without the sentence, and Nyquist caps the upper edge', () => {
    assert.equal(rangeText(40, 4), '35–45 Hz');
    assert.equal(rangeText(0, 4), '');
    const [, hi] = widthRange(20000, 0.5, 48000);
    assert.equal(hi, 48000);
    assert.equal(rangeText(20000, 0.5, 48000), '8280–48000 Hz');
    assert.equal(widthHint(40, 4, 48000), 'Width (Q 4) at 40 Hz: affects about 35–45 Hz');
  });

  test('each peaking, notch or band-pass band shows its range; shelves and pass filters do not', async () => {
    const ed = await peqEditor([
      bandView(0, { design: { type: 'peaking', freq: 40, q: 4, gain_db: -4 }, bypass: false }),
      bandView(1, { design: { type: 'notch', freq: 60, q: 8 }, bypass: false }),
      bandView(2, { design: { type: 'bandpass', freq: 1000, q: 2 }, bypass: false }),
      bandView(3, { design: { type: 'lowshelf', freq: 40, q: 0.71, gain_db: 3 }, bypass: false }),
      bandView(4, { design: { type: 'hp2', freq: 20, q: 0.71 }, bypass: false }),
    ]);
    assert.deepEqual(ed.bands.map((b) => ed.qRange(b)), ['about 35–45 Hz', 'about 56–64 Hz', 'about 781–1280 Hz', '', '']);
    ed.bands[0].form.q = 8; // typing: follows at once
    assert.equal(ed.qRange(ed.bands[0]), 'about 38–43 Hz');
    ed.select(1);
    assert.equal(ed.widthHint(), 'Width (Q 8) at 60 Hz: affects about 56–64 Hz');
    ed.select(3); // a shelf: the hint falls back to the first band with a range
    assert.equal(ed.widthHint(), 'Width (Q 8) at 40 Hz: affects about 38–43 Hz');
  });
});

describe('steppers (phase 9 design §8.3, §16 phone rule 4)', () => {
  const { stepFreq, stepQ, stepMagnitude } = window.mdsp.filters;

  test('frequency moves 1/6 octave, and back again', () => {
    assert.equal(stepFreq(40, 1), 44.9);
    assert.equal(stepFreq(44.9, -1), 40);
    assert.equal(stepFreq(1000, 1), 1122);
    let f = 40;
    for (let k = 0; k < 6; k += 1) f = stepFreq(f, 1);
    assert.ok(Math.abs(f - 80) <= 0.2, `six steps are an octave: ${f}`);
  });

  test('Q walks a ladder of round values, joining it from any value', () => {
    assert.equal(stepQ(4, 1), 4.8);
    assert.equal(stepQ(4, -1), 3.4);
    assert.equal(stepQ(0.71, 1), 0.8);
    assert.equal(stepQ(4.3, 1), 4.8);
    assert.equal(stepQ(4.3, -1), 4);
    assert.equal(stepQ(30, 1), 30);
    assert.equal(stepQ(0.1, -1), 0.1);
    for (const q of [1.4, 4, 8]) assert.equal(stepQ(stepQ(q, 1), -1), q, `${q} is on the ladder`);
  });

  test('the magnitude moves 0.5 dB on a 0.5 dB grid and never below 0', () => {
    assert.equal(stepMagnitude(4, 1), 4.5);
    assert.equal(stepMagnitude(4, -1), 3.5);
    assert.equal(stepMagnitude(4.3, 1), 4.5);
    assert.equal(stepMagnitude(4.3, -1), 4);
    assert.equal(stepMagnitude(0, -1), 0);
    assert.equal(stepMagnitude(0.2, -1), 0);
  });

  async function stepEditor(bands) {
    const ed = factories.peqEditor({ side: 'outputs', channel: 0, preset: 0, name: 'Out 1', fs: 96000, stateUrl: '/s', bandUrl: '/b/{band}', bankUrl: '/bank', stepDelayMs: 5 });
    const loading = ed.load();
    await settle();
    await answer(calls.pop(), 200, { sync: {}, bands });
    await loading;
    return ed;
  }
  const pause = (ms = 20) => new Promise((resolve) => setTimeout(resolve, ms));
  const CUT4 = { type: 'peaking', freq: 40, q: 4, gain_db: -4 };

  test('quick presses preview at once and are sent once, like a typed value', async () => {
    const ed = await stepEditor([bandView(0, { design: CUT4, bypass: false })]);
    const band = ed.bands[0];
    ed.step(band, 'q', 1);
    ed.step(band, 'q', 1);
    ed.step(band, 'freq', 1);
    assert.deepEqual([band.form.q, band.form.freq, band.dirty], [5.6, 44.9, true]);
    assert.ok(band.preview, 'the curve previews the step');
    assert.equal(posts().length, 0);
    await pause();
    assert.equal(posts().length, 1);
    assert.deepEqual(posts()[0].body.design, { type: 'peaking', freq: 44.9, q: 5.6, gain_db: -4 });
    assert.equal(ed.bands[0].dirty, false);
  });

  test('gain steps move the magnitude and keep Cut or Boost: a cut never becomes a boost', async () => {
    const ed = await stepEditor([bandView(0, { design: { ...CUT4, gain_db: -0.5 }, bypass: false }), bandView(1, { design: { ...CUT4, gain_db: 1 }, bypass: false })]);
    const [cut, boost] = ed.bands;
    ed.step(cut, 'gain', -1);
    assert.deepEqual([cut.form.gain_db, cut.form.cut, cut.form.mag], [0, true, '0']);
    assert.equal(ed.canStep(cut, 'gain', -1), false);
    ed.step(cut, 'gain', -1); // ignored at 0
    ed.step(cut, 'gain', 1);
    assert.deepEqual([cut.form.gain_db, cut.form.cut, cut.form.mag], [-0.5, true, '0.5']);
    ed.step(boost, 'gain', 1);
    assert.deepEqual([boost.form.gain_db, boost.form.cut, boost.form.mag], [1.5, false, '1.5']);
    ed.step(boost, 'gain', -1);
    ed.step(boost, 'gain', -1);
    ed.step(boost, 'gain', -1);
    assert.deepEqual([boost.form.gain_db, boost.form.cut], [0, false]); // Boost stays chosen at 0
    await pause();
    const sent = Object.fromEntries(posts().map((c) => [c.url, c.body.design.gain_db]));
    assert.deepEqual(sent, { '/b/0': -0.5, '/b/1': 0 });
  });

  test('steps stop at the field limits and on fields the type does not use', async () => {
    const ed = await stepEditor([
      bandView(0, { design: { type: 'peaking', freq: 43000, q: 30, gain_db: 20 }, bypass: false }),
      bandView(1, { design: { type: 'peaking', freq: 5, q: 0.1, gain_db: -30 }, bypass: false }),
      bandView(2, { design: { type: 'hp1', freq: 20 }, bypass: false }),
      { ...bandView(3, { design: CUT4, bypass: false }), external: true },
    ]);
    const [high, low, hp1, locked] = ed.bands;
    assert.deepEqual(['freq', 'q', 'gain'].map((f) => ed.canStep(high, f, 1)), [false, false, false]);
    assert.deepEqual(['freq', 'q', 'gain'].map((f) => ed.canStep(high, f, -1)), [true, true, true]);
    assert.deepEqual(['freq', 'q', 'gain'].map((f) => ed.canStep(low, f, -1)), [false, false, true]); // a 30 dB cut can shrink
    assert.deepEqual(['freq', 'q', 'gain'].map((f) => ed.canStep(low, f, 1)), [true, true, false]);
    assert.deepEqual(['freq', 'q', 'gain'].map((f) => ed.canStep(hp1, f, 1)), [true, false, false]);
    assert.equal(ed.canStep(locked, 'freq', 1), false);
    ed.bands[0].form.freq = 22000;
    ed.step(ed.bands[0], 'freq', 1); // 22000 · 2^(1/6) is past 24 kHz: clamped
    assert.equal(ed.bands[0].form.freq, 24000);
  });
});
