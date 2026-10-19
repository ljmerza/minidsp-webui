// app/static/js/meters.js: the levelMeters component's readings, as the Routing page's input meters use them (the
// "no signal" floor, the peak-hold tick of a horizontal bar, the named reading for aria-valuetext). The Alpine factory
// is called directly, without a DOM.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { describe, test } from 'node:test';
import vm from 'node:vm';

import { ROOT } from './load.mjs';

const listeners = [];
globalThis.window = {};
globalThis.document = { addEventListener: (name, fn) => listeners.push([name, fn]) };
vm.runInThisContext(readFileSync(new URL('app/static/js/meters.js', ROOT), 'utf8'), { filename: 'meters.js' });
const factories = {};
window.Alpine = { data: (name, factory) => (factories[name] = factory) };
for (const [name, fn] of listeners) if (name === 'alpine:init') fn();

const { signalText } = window.mdsp;
const meters = (cfg) => factories.levelMeters({ inputs: ['Receiver In', 'In 2'], outputs: [], ...cfg });

describe('level meters', () => {
  test('a reading says "no signal" below the floor, a dash without data, and the level otherwise', () => {
    assert.equal(signalText(-95, false, -90), 'no signal');
    assert.equal(signalText(-90, false, -90), '-90.0 dBFS'); // at the floor: still a reading
    assert.equal(signalText(-18, false, -90), '-18.0 dBFS'); // no app.js here: the plain fallback
    assert.equal(signalText(-18, true, -90), '—');
    assert.equal(signalText(null, false, -90), '—');
    assert.equal(signalText(Number.NaN, false, -90), '—');
    assert.equal(signalText(-120, false, undefined), '-120.0 dBFS'); // no floor given: never "no signal"
  });

  test('the component reads each input against the configured floor and names it for screen readers', () => {
    const m = meters({ floor: -90 });
    m.apply({ i: [-18, -120], o: [] });
    assert.equal(m.signalText('i0'), '-18.0 dBFS');
    assert.equal(m.signalText('i1'), 'no signal');
    assert.equal(m.namedReading('i0'), 'Receiver In: -18.0 dBFS');
    assert.equal(m.namedReading('i1'), 'In 2: no signal');
    assert.equal(m.meterNow('i0'), -18);
  });

  test('before any frame the meters read a dash and hide the peak tick', () => {
    const m = meters({ floor: -90 });
    assert.equal(m.stale, true);
    assert.equal(m.signalText('i0'), '—');
    assert.equal(m.namedReading('i0'), 'Receiver In: —');
    assert.equal(m.hpeakStyle('i0'), 'display: none');
  });

  test('the peak tick holds the highest recent level on the horizontal scale, and CLIP latches at -1 dBFS', () => {
    const m = meters({ floor: -90 });
    m.apply({ i: [-6, -120] });
    m.apply({ i: [-30, -120] }); // within the 1.5 s hold: the tick stays at -6
    assert.equal(m.hpeakStyle('i0'), `left: ${(window.mdsp.meterFraction(-6) * 100).toFixed(2)}%`);
    assert.equal(m.signalText('i0'), '-30.0 dBFS');
    assert.equal(m.clip.i0, false);
    m.apply({ i: [-0.5, -120] });
    assert.equal(m.clip.i0, true);
    assert.equal(m.clipLabel('i0'), 'Receiver In clipped, press to reset');
    m.resetClip('i0');
    assert.equal(m.clip.i0, false);
  });
});
