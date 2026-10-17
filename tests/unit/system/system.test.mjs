// Pure helpers of app/static/js/system.js (run by test_system_js.py, or `node --test tests/unit/system/`).
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadScripts } from '../../js/load.mjs';

const mdsp = loadScripts('app/static/js/graph.js', 'app/static/js/system.js');
const sys = mdsp.system;

test('wrapSegments splits the phase at ±180° jumps and at silent points', () => {
  const N = null;
  assert.deepEqual(sys.wrapSegments([170, 179, -179, -170, null, 10, NaN, 20]), [
    [170, 179, N, N, N, N, N, N],
    [N, N, -179, -170, N, N, N, N],
    [N, N, N, N, N, 10, N, N],
    [N, N, N, N, N, N, N, 20],
  ]);
  assert.deepEqual(sys.wrapSegments([0, 90, 180, 90]), [[0, 90, 180, 90]]);
  assert.deepEqual(sys.wrapSegments([null, NaN]), []);
});

test('integrationTraces plot Δφ with the level difference as their dB readout', () => {
  const integ = {
    label_a: 'Shakers',
    label_b: 'Sub',
    color_a: '#3ccf7a',
    freqs: [20, 35, 50, 100],
    phase_diff_deg: [100, 170, -175, -160],
    level_diff_db: [1, 3, 12, null],
  };
  const traces = sys.integrationTraces(integ);
  assert.deepEqual(
    traces.map((t) => t.id),
    ['dphi0', 'dphi1'],
  );
  const [t0, t1] = traces;
  assert.equal(t0.label, 'Δφ Shakers − Sub');
  assert.equal(t1.color, '#3ccf7a');
  assert.deepEqual(t0.response.phase_deg, [100, 170, null, null]);
  assert.deepEqual(t1.response.phase_deg, [null, null, -175, -160]);
  assert.deepEqual(t0.response.magnitude_db, [1, 3, 12, null]);
  assert.deepEqual(mdsp.graph.traceValues(t1, 'phase'), [null, null, -175, -160]);
});

test('regionText describes a cancellation region', () => {
  const r = { f_lo: 37.04, f_hi: 74.07, peak_freq: 55.6, peak_dphi_deg: -179.9, worst_deficit_db: -12.34 };
  assert.equal(sys.regionText(r), '37.0 Hz–74.1 Hz, |Δφ| up to 180° at 55.6 Hz, the sum is up to 12.3 dB below in-phase');
  assert.match(sys.regionText({ ...r, worst_deficit_db: -80 }), /the sum nearly cancels$/);
  assert.equal(sys.regionText({ ...r, worst_deficit_db: null }), '37.0 Hz–74.1 Hz, |Δφ| up to 180° at 55.6 Hz');
});

test('cursorText reads Δφ, level and summation loss at a grid index', () => {
  const integ = {
    label_a: 'Shakers',
    label_b: 'Sub',
    freqs: [40, 60],
    phase_diff_deg: [-130.4, null],
    level_diff_db: [-2.25, null],
    deficit_db: [-6.5, null],
  };
  assert.equal(sys.cursorText(integ, 0), '40.0 Hz: Δφ −130° · Shakers −2.3 dB vs Sub · sum 6.5 dB below in-phase');
  assert.equal(sys.cursorText(integ, 1), '60.0 Hz: no phase (an output is silent)');
  assert.equal(sys.cursorText(integ, null), '');
  assert.equal(sys.cursorText(null, 0), '');
});

test('queryFor drops muted outputs and refuses a pair of one output', () => {
  const q = (o) => Object.fromEntries(new URLSearchParams(sys.queryFor(o)));
  const base = { selected: [3, 0, 1], muted: [1], view: 'sub', inputs: '0', pairA: '0', pairB: '1' };
  assert.deepEqual(q(base), { outputs: '0,3', range: 'sub', inputs: '0', pair: '0,1' });
  assert.equal(q({ ...base, selected: [1] }).outputs, 'none');
  assert.equal(q({ ...base, pairB: '0' }).pair, 'none');
  assert.equal(q({ ...base, pairA: '' }).pair, 'none');
  assert.equal(q({ ...base, view: 'full', inputs: 'all' }).range, 'full');
});
