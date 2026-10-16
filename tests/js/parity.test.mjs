// Parity of the browser DSP mirror (app/static/js/dsp.js) with the Python authority (app/dsp), via the shared
// tests/vectors/*.json written by tests/unit/dsp/vectors_gen.py. Plain node, no dependencies:
//   node --test tests/js/*.test.mjs
// MDSP_EXTRA_VECTORS may name a JSON file of whole-document responses computed by app/dsp/response.py
// (tests/unit/test_js_parity.py writes one), which checks the channel-chain composition too.
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { describe, test } from 'node:test';

import { loadScripts, readJson } from './load.mjs';

const { dsp } = loadScripts('app/static/js/dsp.js');

const COEFF_TOL = 1e-12; // docs 08 Phase 3: coefficients to 1e-12
const DB_TOL = 1e-5; // expected values are rounded to 1e-6 dB
const DEEP_DB = -100; // below this, a dB value is a ratio of two tiny numbers: compare loosely
const DEEP_DB_TOL = 1e-3;
const PHASE_TOL = 1e-5;
const DOC_DB_TOL = 0.05 + 1e-9; // docs 04 §8.1 values are printed to 0.1 dB

const FILES = ['golden', 'filters', 'crossovers', 'rew', 'delay'];

function assertDb(actual, expected, label) {
  assert.equal(actual.length, expected.length, `${label}: length`);
  expected.forEach((e, i) => {
    const tol = e < DEEP_DB ? DEEP_DB_TOL : DB_TOL;
    assert.ok(Math.abs(actual[i] - e) <= tol, `${label}[${i}]: ${actual[i]} vs ${e}`);
  });
}

function assertClose(actual, expected, tol, label) {
  assert.equal(actual.length, expected.length, `${label}: length`);
  expected.forEach((e, i) => {
    assert.ok(Math.abs(actual[i] - e) <= tol * Math.max(1, Math.abs(e)), `${label}[${i}]: ${actual[i]} vs ${e}`);
  });
}

// Wrapped phases can legitimately land on either side of ±180°.
function assertPhase(actual, expected, label) {
  expected.forEach((e, i) => {
    const d = Math.abs(((actual[i] - e + 540) % 360) - 180);
    assert.ok(d <= PHASE_TOL, `${label}[${i}]: ${actual[i]} vs ${e}`);
  });
}

function assertCoeffs(biquads, rows, label) {
  assert.equal(biquads.length, rows.length, `${label}: number of biquads`);
  biquads.forEach((bq, i) => {
    dsp.coeffs(bq).forEach((c, k) => {
      assert.ok(Math.abs(c - rows[i][k]) <= COEFF_TOL, `${label}: biquad ${i} coefficient ${k}: ${c} vs ${rows[i][k]}`);
    });
  });
}

// What the JS mirror designs for a vector, or null when the vector has no design (raw, delay).
function design(v) {
  if (v.kind === 'crossover') return dsp.designCrossover(v.params);
  if (v.kind === 'chain') return v.params.crossovers.flatMap((x) => dsp.designCrossover(x));
  if (v.kind === 'raw' || v.kind === 'delay') return null;
  return [dsp.designFilter({ type: v.kind, ...v.params })];
}

const isBessel = (v) =>
  (v.kind === 'crossover' && v.params.family === 'bessel') ||
  (v.kind === 'chain' && v.params.crossovers.some((x) => x.family === 'bessel'));

for (const file of FILES) {
  const vectors = readJson(`tests/vectors/${file}.json`);

  describe(`${file}.json`, () => {
    test('is not empty', () => assert.ok(vectors.length > 0));

    for (const v of vectors) {
      test(v.name, () => {
        if (isBessel(v)) {
          // Bessel is server-only (docs 04 §7): the mirror must say so rather than guess.
          assert.throws(() => design(v), (e) => e instanceof dsp.DesignError && e.serverOnly === true);
        } else {
          const biquads = design(v);
          if (biquads) assertCoeffs(biquads, v.coeffs, 'design');
        }

        if (v.kind === 'raw') {
          v.coeffs.forEach((row) => assert.equal(dsp.biquadProblem(row), null));
          assertCoeffs(v.coeffs.map((row) => dsp.raw(row)), v.coeffs, 'raw');
        }

        if (v.kind === 'delay') {
          assert.equal(dsp.delaySamples(v.params.delay_ms), v.params.samples);
          const resp = dsp.toResponse(dsp.delayTf(v.params.samples, v.freqs), v.freqs);
          assertDb(resp.magnitude_db, v.expected_db, 'delay magnitude');
          assertClose(resp.group_delay_ms, v.expected_group_delay_ms, 1e-9, 'delay group delay');
          return;
        }

        const exact = dsp.filterResponse(v.coeffs, v.freqs, { float32: false });
        assertDb(exact.magnitude_db, v.expected_db, 'float64 dB');
        if (v.expected_db_float32) {
          assertDb(dsp.filterResponse(v.coeffs, v.freqs).magnitude_db, v.expected_db_float32, 'float32 dB');
        }
        if (v.expected_phase_deg) assertPhase(exact.phase_deg, v.expected_phase_deg, 'phase');
        if (v.doc_db) assertClose(exact.magnitude_db, v.doc_db, DOC_DB_TOL, 'docs 04 §8.1 table');
      });
    }
  });
}

const extra = process.env.MDSP_EXTRA_VECTORS;
if (extra) {
  describe('whole-document responses from app/dsp/response.py', () => {
    test('extra vectors file exists', () => assert.ok(existsSync(extra), extra));
    const vectors = existsSync(extra) ? readJson(new URL(`file://${extra}`)) : [];
    for (const v of vectors) {
      test(v.name, () => {
        const opts = { inputs: v.inputs, masterDb: v.master_db ?? null };
        let resp;
        if (v.kind === 'system') resp = dsp.systemResponse(v.doc, v.outputs, v.freqs, opts);
        else if (v.kind === 'input') resp = dsp.inputResponse(v.doc, v.input, v.freqs);
        else if (v.kind === 'filter') resp = dsp.filterResponse(v.coeffs, v.freqs);
        else assert.fail(`unknown extra vector kind ${v.kind}`);
        const e = v.expected;
        assertDb(resp.magnitude_db, e.magnitude_db, 'magnitude');
        assertPhase(resp.phase_deg, e.phase_deg, 'phase');
        assertClose(resp.phase_unwrapped_deg, e.phase_unwrapped_deg, 1e-6, 'unwrapped phase');
        assertClose(resp.group_delay_ms, e.group_delay_ms, 1e-6, 'group delay');
        assert.ok(Math.abs(resp.float32_deviation_db - e.float32_deviation_db) <= 1e-6, 'float32 deviation');
        assert.deepEqual(resp.warnings.length, e.warnings.length, `warnings: ${resp.warnings} vs ${e.warnings}`);
      });
    }
  });
}
