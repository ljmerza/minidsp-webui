// Behaviour of app/static/js/dsp.js beyond the shared vectors: validation, stability, crossover structure and the
// channel-chain helpers.
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { loadScripts } from './load.mjs';

const { dsp } = loadScripts('app/static/js/dsp.js');

const maxAbs = (values) => Math.max(...Array.from(values, Math.abs));
const isDesignError = (pattern) => (e) => e instanceof dsp.DesignError && pattern.test(e.message);

describe('filters', () => {
  test('PEQ +6 dB @ 1 kHz Q 1 is +6.00 dB at 1 kHz (docs 04 §8.2)', () => {
    const r = dsp.filterResponse([dsp.peaking(1000, 1, 6)], [1000]);
    assert.ok(Math.abs(r.magnitude_db[0] - 6) < 0.01);
  });

  test('pass-through is flat with zero phase and group delay', () => {
    const r = dsp.filterResponse([dsp.passthrough()]);
    assert.equal(r.freqs.length, 512);
    assert.equal(maxAbs(r.magnitude_db), 0);
    assert.equal(maxAbs(r.phase_deg), 0);
    assert.equal(maxAbs(r.group_delay_ms), 0);
    assert.deepEqual(r.warnings, []);
    assert.ok(dsp.isPassthrough([1, 0, 0, 0, 0]));
    assert.ok(!dsp.isPassthrough(dsp.gain(-6)));
  });

  test('designFilter enforces the required parameters and Nyquist', () => {
    assert.throws(() => dsp.designFilter({ type: 'peaking', freq: 100, q: 1 }), isDesignError(/requires gain_db/));
    assert.throws(() => dsp.designFilter({ type: 'lp2', freq: 48000, q: 0.7 }), isDesignError(/Nyquist/));
    assert.throws(() => dsp.designFilter({ type: 'hp2', freq: 50, q: 0 }), isDesignError(/Q must be a positive/));
    assert.throws(() => dsp.designFilter({ type: 'peaking', freq: 50, q: 1, gain_db: NaN }), isDesignError(/gain/));
    assert.throws(() => dsp.designFilter({ type: 'bogus' }), isDesignError(/unknown filter type/));
    assert.deepEqual(dsp.designFilter({ type: 'passthrough' }), { ...dsp.PASSTHROUGH });
    assert.deepEqual(dsp.designFilter({}), { ...dsp.PASSTHROUGH });
  });

  test('every filter type compiles to a stable biquad', () => {
    const designs = {
      peaking: { freq: 42, q: 4, gain_db: -3.5 },
      lowshelf: { freq: 35, q: 0.7, gain_db: 6 },
      highshelf: { freq: 3000, q: 0.7, gain_db: -4 },
      lp1: { freq: 120 },
      hp1: { freq: 30 },
      lp2: { freq: 80, q: 0.707 },
      hp2: { freq: 20, q: 0.707 },
      bandpass: { freq: 500, q: 2 },
      notch: { freq: 60, q: 10 },
      ap1: { freq: 200 },
      ap2: { freq: 100, q: 0.7 },
      lt: { lt: { f0: 40, q0: 0.9, fp: 20, qp: 0.6 } },
      gain: { gain_db: -6 },
      passthrough: {},
      raw: { raw: { b0: 0.5, b1: 0, b2: 0, a1: 0.5, a2: 0 } },
    };
    assert.deepEqual(Object.keys(designs).sort(), [...dsp.FILTER_TYPES].sort());
    for (const [type, params] of Object.entries(designs)) {
      assert.ok(dsp.isStable(dsp.designFilter({ type, ...params })), type);
    }
  });

  test('shelf slope maps to Q like the server', () => {
    assert.ok(Math.abs(dsp.shelfQFromSlope(1, 6) - Math.SQRT1_2) < 1e-12);
    assert.throws(() => dsp.shelfQFromSlope(0, 6), isDesignError(/slope must be positive/));
    assert.throws(() => dsp.shelfQFromSlope(10, 24), isDesignError(/too steep/));
  });

  test('Linkwitz Transform: DC gain (f0/fp)², unity at high frequencies', () => {
    const r = dsp.filterResponse([dsp.linkwitzTransform(40, 0.9, 20, 0.6)], [0.05, 20000], { float32: false });
    assert.ok(Math.abs(r.magnitude_db[0] - 20 * Math.log10(4)) < 0.01);
    assert.ok(Math.abs(r.magnitude_db[1]) < 0.05);
  });
});

describe('stability', () => {
  test('the stability triangle, in float64 and float32', () => {
    assert.equal(dsp.biquadProblem([1, 0, 0, 0.5, 0.4]), null);
    assert.match(dsp.biquadProblem([1, 0, 0, 0, 1]), /unstable biquad: pole radius 1 /);
    assert.match(dsp.biquadProblem([1, 0, 0, 1.5, -0.4]), /unstable/);
    // Stable in float64, but a2 rounds to exactly −1 in float32.
    assert.ok(dsp.polesStable(0, -(1 - 1e-12)) === false);
    assert.match(dsp.biquadProblem([NaN, 0, 0, 0, 0]), /non-finite/);
    assert.match(dsp.biquadProblem([1e39, 0, 0, 0, 0]), /overflows float32/);
    assert.match(dsp.biquadProblem([1, 2, 3]), /5 coefficients/);
    assert.equal(dsp.biquadProblem({ b0: 1, b1: 0, b2: 0, a1: 0, a2: 0 }), null);
  });

  test('pole radius', () => {
    assert.ok(Math.abs(dsp.poleRadius(0, -0.25) - 0.5) < 1e-12); // complex pair, |p|² = −a2
    assert.ok(Math.abs(dsp.poleRadius(1.5, -0.56) - 0.8) < 1e-12); // real poles 0.8, 0.7
  });

  test('raw rejects an unstable biquad with a clear message', () => {
    assert.throws(() => dsp.raw([1, 0, 0, 1.2, 0]), isDesignError(/unstable biquad/));
    assert.throws(() => dsp.designFilter({ type: 'raw', raw: [1, 0, 0, 0, 1.01] }), isDesignError(/unstable/));
  });
});

describe('crossovers', () => {
  test('Butterworth Q table (docs 04 §3)', () => {
    const table = {
      2: [0.7071],
      3: [1.0],
      4: [0.5412, 1.3066],
      5: [0.618, 1.618],
      6: [0.5176, 0.7071, 1.9319],
      7: [0.555, 0.8019, 2.247],
      8: [0.5098, 0.6013, 0.9, 2.5629],
    };
    for (const [n, qs] of Object.entries(table)) {
      const got = dsp.butterworthQs(Number(n));
      assert.equal(got.length, qs.length);
      got.forEach((q, i) => assert.ok(Math.abs(q - qs[i]) < 5e-5, `BW${n} Q${i}: ${q}`));
    }
    assert.deepEqual(dsp.butterworthQs(1), []);
  });

  test('Linkwitz-Riley sections', () => {
    assert.deepEqual(dsp.linkwitzRileyQs(2), [0.5]);
    assert.equal(dsp.linkwitzRileyQs(4).length, 2);
    assert.deepEqual(dsp.linkwitzRileyQs(6).map((q) => Number(q.toFixed(4))), [0.5, 1, 1]);
    assert.equal(dsp.linkwitzRileyQs(8).length, 4);
    assert.throws(() => dsp.linkwitzRileyQs(3), isDesignError(/2, 4, 6 or 8/));
  });

  test('groups are padded with pass-through and band-pass writes the high-pass first', () => {
    const lr4 = dsp.designCrossover({ kind: 'lp', family: 'lr', order: 4, freq: 80 });
    assert.equal(lr4.length, 4);
    assert.ok(dsp.isPassthrough(lr4[2]) && dsp.isPassthrough(lr4[3]));
    const bp = dsp.crossoverSections({ kind: 'bp', family: 'lr', order: 4, freq: 20, freq_hi: 80 });
    assert.deepEqual(bp.map((s) => [s.kind, s.freq]), [['hp2', 20], ['hp2', 20], ['lp2', 80], ['lp2', 80]]);
    assert.equal(dsp.biquadsNeeded({ kind: 'lp', family: 'bw', order: 5, freq: 100 }), 3);
    assert.equal(dsp.designCrossover({ kind: 'lp', family: 'lr', order: 2, freq: 80 }, 2).length, 2);
  });

  test('invalid crossover designs are refused', () => {
    const bad = [
      [{ kind: 'lp', family: 'lr', order: 3, freq: 80 }, /even order/],
      [{ kind: 'bp', family: 'lr', order: 4, freq: 80 }, /needs freq_hi/],
      [{ kind: 'bp', family: 'lr', order: 4, freq: 80, freq_hi: 40 }, /above freq/],
      [{ kind: 'bp', family: 'bw', order: 8, freq: 20, freq_hi: 80 }, /needs 8 biquads, the group has 4/],
      [{ kind: 'lp', family: 'bw', order: 9, freq: 80 }, /order must be 1..8/],
      [{ kind: 'lp', family: 'bw', order: 2, freq: 50000 }, /Nyquist/],
      [{ kind: 'xx', family: 'bw', order: 2, freq: 80 }, /unknown crossover kind/],
      [{ kind: 'lp', family: 'cheby', order: 2, freq: 80 }, /unknown crossover family/],
    ];
    for (const [design, pattern] of bad) assert.throws(() => dsp.designCrossover(design), isDesignError(pattern));
  });

  test('Bessel is server-only', () => {
    assert.throws(
      () => dsp.designCrossover({ kind: 'lp', family: 'bessel', order: 4, freq: 80 }),
      (e) => e instanceof dsp.DesignError && e.serverOnly,
    );
  });

  test('LR4 low-pass + high-pass at the same corner sums flat (docs 04 §8.2)', () => {
    const f = dsp.freqGrid();
    for (const fc of [80, 1000]) {
      const design = (kind) => dsp.designCrossover({ kind, family: 'lr', order: 4, freq: fc });
      const tf = (kind) => dsp.cascadeTf(design(kind), f, { float32: false });
      const [lp, hp] = [tf('lp'), tf('hp')];
      assert.ok(maxAbs(dsp.toResponse(dsp.tfAdd(lp, hp), f).magnitude_db) < 0.01, `fc ${fc}`);
    }
  });

  test('the shaker LR4 80/20 Hz crossover triggers the float32 warning (same as the server)', () => {
    const lp = dsp.designCrossover({ kind: 'lp', family: 'lr', order: 4, freq: 80 });
    const hp = dsp.designCrossover({ kind: 'hp', family: 'lr', order: 4, freq: 20 });
    assert.ok(dsp.filterResponse(lp).float32_deviation_db < 0.1);
    const r = dsp.filterResponse([...lp, ...hp]);
    assert.ok(r.float32_deviation_db > 0.1 && r.float32_deviation_db < 0.3, r.float32_deviation_db);
    assert.match(r.warnings[0], /float32 coefficient rounding moves the response by up to 0\.\d\d dB/);
  });
});

describe('grid, delay and responses', () => {
  test('frequency grids', () => {
    const f = dsp.freqGrid();
    assert.equal(f.length, 512);
    assert.equal(f[0], 5);
    assert.equal(f[511], 24000);
    const ratio = f[1] / f[0];
    for (let i = 2; i < f.length; i++) assert.ok(Math.abs(f[i] / f[i - 1] - ratio) < 1e-12);
    assert.deepEqual([dsp.viewGrid('sub')[0], dsp.viewGrid('sub')[511]], [5, 300]);
    assert.deepEqual([dsp.viewGrid('full')[0], dsp.viewGrid('full')[511]], [10, 20000]);
    assert.throws(() => dsp.freqGrid(100, 10), RangeError);
  });

  test('delay rounds to whole samples, ties to even like the server', () => {
    assert.equal(dsp.delaySamples(9), 864);
    assert.equal(dsp.delaySamples(1.5), 144);
    assert.equal(dsp.delaySamples(10.41), 999);
    assert.equal(dsp.delaySamples(0.015625), 2); // 1.5 samples
    assert.equal(dsp.delaySamples(0.026041666666666668), 2); // 2.5 samples
  });

  test('a 9 ms delay has 9 ms of group delay and an unwrapped phase of −360°·f·9 ms', () => {
    const f = dsp.freqGrid();
    const r = dsp.toResponse(dsp.delayTf(864, f), f);
    assert.ok(maxAbs(r.group_delay_ms.map((g) => g - 9)) < 1e-9);
    const expected = Array.from(f, (x) => -360 * x * 0.009);
    assert.ok(maxAbs(r.phase_unwrapped_deg.map((p, i) => p - expected[i])) < 1e-6);
  });

  test('a muted chain reads −200 dB with zero phase and group delay', () => {
    const r = dsp.toResponse(dsp.tfZero(4), [10, 100, 1000, 10000]);
    assert.deepEqual(Array.from(r.magnitude_db), [-200, -200, -200, -200]);
    assert.equal(maxAbs(r.phase_deg), 0);
    assert.equal(maxAbs(r.group_delay_ms), 0);
  });

  test('a preview of 30 biquads on 512 points is fast enough to drag at 60 fps', () => {
    const biquads = Array.from({ length: 30 }, (_, k) => dsp.peaking(20 + 40 * k, 2, k % 2 ? 3 : -3));
    const f = dsp.freqGrid();
    dsp.filterResponse(biquads, f);
    const t0 = performance.now();
    for (let i = 0; i < 10; i++) dsp.filterResponse(biquads, f);
    const each = (performance.now() - t0) / 10;
    assert.ok(each < 16, `${each.toFixed(2)} ms per response`);
  });
});

describe('channel chains', () => {
  const bq = (c) => ({ b0: c[0], b1: c[1], b2: c[2], a1: c[3], a2: c[4] });
  const band = (biquad, bypass = false) => ({ bypass, compiled: biquad });
  const group = (biquads, bypass = false) => ({ bypass, compiled: biquads });
  const flat = () => ({ bypass: true, compiled: { ...dsp.PASSTHROUGH } });
  const route = (enabled) => ({ enabled, gain_db: 0 });

  function shakerDoc() {
    const lp = dsp.designCrossover({ kind: 'lp', family: 'lr', order: 4, freq: 80 });
    const hp = dsp.designCrossover({ kind: 'hp', family: 'lr', order: 4, freq: 20 });
    const output = (extra) => ({
      gain_db: 0,
      mute: false,
      invert: false,
      delay_ms: 0,
      peq: [flat(), flat()],
      crossover: [group(lp, true), group(hp, true)],
      fir: { bypass: true },
      ...extra,
    });
    return {
      inputs: [
        { gain_db: 0, mute: false, peq: [flat()], routing: [route(true), route(true)] },
        { gain_db: 0, mute: false, peq: [flat()], routing: [route(false), route(true)] },
      ],
      outputs: [output({}), output({ crossover: [group(lp), group(hp)], delay_ms: 9 })],
    };
  }

  test('the shaker output reproduces the docs 04 §8.1 table', () => {
    const freqs = [10, 20, 40, 60, 80, 120, 200];
    const table = [-24.6, -6.1, -1.1, -2.5, -6.1, -15.7, -32.1];
    const r = dsp.outputResponse(shakerDoc(), 1, freqs, { float32: false });
    r.magnitude_db.forEach((db, i) => assert.ok(Math.abs(db - table[i]) <= 0.05 + 1e-9, `${freqs[i]} Hz: ${db}`));
  });

  test('stages: routing, gain, mute, polarity, delay and all-inputs summing', () => {
    const doc = shakerDoc();
    const f = [100];
    const one = dsp.outputStages(doc, 0, f, { float32: false });
    assert.ok(Math.abs(one.total.re[0] - 1) < 1e-12);
    assert.deepEqual(Object.keys(one), ['input', 'peq', 'crossover', 'fir', 'polarity', 'delay', 'gain', 'total']);

    // Input 2 also feeds output 2: summing all inputs coherently doubles it (+6.02 dB).
    const both = dsp.outputResponse(doc, 0, f, { inputs: null, float32: false });
    assert.ok(Math.abs(both.magnitude_db[0]) < 1e-9); // input 2 is not routed to output 1
    const summed = dsp.systemResponse(doc, [1], f, { inputs: [0, 1], float32: false });
    const single = dsp.systemResponse(doc, [1], f, { float32: false });
    assert.ok(Math.abs(summed.magnitude_db[0] - single.magnitude_db[0] - 20 * Math.log10(2)) < 1e-9);

    doc.outputs[0].invert = true;
    doc.outputs[0].gain_db = -6;
    const inv = dsp.outputStages(doc, 0, f, { float32: false });
    assert.ok(Math.abs(inv.total.re[0] + 10 ** (-6 / 20)) < 1e-12);
    const mastered = dsp.outputResponse(doc, 0, f, { masterDb: -10, float32: false });
    assert.ok(Math.abs(mastered.magnitude_db[0] + 16) < 1e-9);

    doc.outputs[0].mute = true;
    assert.equal(dsp.outputResponse(doc, 0, f).magnitude_db[0], -200);
    doc.inputs[0].mute = true;
    assert.equal(dsp.outputResponse(doc, 1, f).magnitude_db[0], -200);
  });

  test('bypassed bands and groups contribute nothing; active ones do', () => {
    const doc = shakerDoc();
    doc.outputs[0].peq[0] = band(bq([2, 0, 0, 0, 0]), true);
    assert.ok(Math.abs(dsp.outputResponse(doc, 0, [100]).magnitude_db[0]) < 1e-9);
    doc.outputs[0].peq[0].bypass = false;
    assert.ok(Math.abs(dsp.outputResponse(doc, 0, [100]).magnitude_db[0] - 20 * Math.log10(2)) < 1e-6);
    doc.inputs[0].peq[0] = band(bq([0.5, 0, 0, 0, 0]));
    assert.ok(Math.abs(dsp.inputResponse(doc, 0, [100]).magnitude_db[0] + 20 * Math.log10(2)) < 1e-6);
  });

  test('an active FIR is plotted flat with a warning', () => {
    const doc = shakerDoc();
    doc.outputs[1].fir = { bypass: false, blob_id: 'x' };
    const r = dsp.systemResponse(doc, [0, 1]);
    assert.ok(r.warnings.some((w) => /output 2: the FIR is active but its taps are unknown/.test(w)));
  });

  test('the electrical sum shows the 9 ms shaker delay as phase rotation', () => {
    const doc = shakerDoc();
    doc.outputs[0].crossover[0].bypass = false;
    doc.outputs[0].crossover[1].bypass = false;
    const f = [40];
    const aligned = dsp.systemResponse(doc, [0, 1], f, { float32: false }).magnitude_db[0];
    doc.outputs[1].delay_ms = 0;
    const same = dsp.systemResponse(doc, [0, 1], f, { float32: false }).magnitude_db[0];
    // Identical chains add to +6.02 dB over one; the 9 ms delay (−130° at 40 Hz) partly cancels.
    const one = dsp.outputResponse(doc, 0, f, { float32: false }).magnitude_db[0];
    assert.ok(Math.abs(same - one - 20 * Math.log10(2)) < 1e-9);
    assert.ok(aligned < same - 3);
  });
});
