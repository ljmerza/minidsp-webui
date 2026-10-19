// Browser mirror of app/dsp (docs 04 §7): RBJ filters and Butterworth / Linkwitz-Riley crossovers in the device
// convention, stability checks and response evaluation. Preview only: the server stays the authority for every
// coefficient that reaches the device, and its coefficients replace the preview once it answers. Bessel crossovers,
// coefficient recognition and FIR responses are server-only.
//
// Device biquad: y[n] = b0·x[n] + b1·x[n-1] + b2·x[n-2] + a1·y[n-1] + a2·y[n-2], i.e. a1/a2 are negated relative to the
// RBJ denominator, and H(z) = (b0 + b1·z⁻¹ + b2·z⁻²) / (1 − a1·z⁻¹ − a2·z⁻²). The arithmetic below follows
// app/dsp/design.py term for term; tests/js/parity.test.mjs checks it against tests/vectors/*.json.
//
// Biquads are plain objects {b0, b1, b2, a1, a2} (the server's JSON); every function that takes one also accepts a
// [b0, b1, b2, a1, a2] array. Responses use the same keys as the server's Response.to_dict(), so graph.js plots either.
//
// Loaded as a classic script it sets window.mdsp.dsp; under CommonJS it is module.exports.
(() => {
  'use strict';

  const FS = 96000;
  const N_POINTS = 512;
  const F_MIN = 5;
  const F_MAX = 24000;
  const VIEWS = { sub: [5, 300], full: [10, 20000] };
  const DB_FLOOR = -200;
  const FLOAT32_WARN_DB = 0.1;
  const FLOAT32_RELEVANT_DB = -100;
  const CHECK_F_MIN = 0.1; // lowest non-zero frequency of the full-band check grid (DC is always included)
  const KEYS = ['b0', 'b1', 'b2', 'a1', 'a2'];
  const PASSTHROUGH = Object.freeze({ b0: 1, b1: 0, b2: 0, a1: 0, a2: 0 });

  class DesignError extends Error {
    constructor(message, { serverOnly = false } = {}) {
      super(message);
      this.name = 'DesignError';
      // True when the design is valid but can only be compiled by the server (Bessel): ask it instead of failing.
      this.serverOnly = serverOnly;
    }
  }

  // --- helpers --------------------------------------------------------------------------------------------------

  const fmt = (x) => (Number.isFinite(x) ? String(Number(x.toPrecision(6))) : String(x));

  function fromRbj(b0, b1, b2, a0, a1, a2) {
    return { b0: b0 / a0, b1: b1 / a0, b2: b2 / a0, a1: -a1 / a0, a2: -a2 / a0 };
  }

  function coeffs(biquad) {
    if (Array.isArray(biquad) || ArrayBuffer.isView(biquad)) {
      if (biquad.length !== 5) {
        throw new DesignError(`a biquad has 5 coefficients (b0 b1 b2 a1 a2), got ${biquad.length}`);
      }
      return Array.from(biquad, Number);
    }
    if (biquad && typeof biquad === 'object') return KEYS.map((k) => Number(biquad[k] ?? (k === 'b0' ? 1 : 0)));
    throw new DesignError('a biquad is {b0, b1, b2, a1, a2} or a 5-element array');
  }

  function toBiquad(biquad) {
    const c = coeffs(biquad);
    return { b0: c[0], b1: c[1], b2: c[2], a1: c[3], a2: c[4] };
  }

  const isPassthrough = (biquad) => {
    const c = coeffs(biquad);
    return c[0] === 1 && c[1] === 0 && c[2] === 0 && c[3] === 0 && c[4] === 0;
  };

  const toFloat32 = (biquad) => toBiquad(coeffs(biquad).map(Math.fround));

  function checkFreq(freq, fs, name = 'frequency') {
    if (!(Number.isFinite(freq) && freq > 0 && freq < fs / 2)) {
      throw new DesignError(`${name} ${fmt(freq)} Hz must be between 0 and ${fmt(fs / 2)} Hz (Nyquist)`);
    }
  }

  function checkQ(q, name = 'Q') {
    if (!(Number.isFinite(q) && q > 0)) throw new DesignError(`${name} must be a positive number, got ${fmt(q)}`);
  }

  function checkGain(gainDb) {
    if (!Number.isFinite(gainDb)) throw new DesignError(`gain must be a finite number of dB, got ${fmt(gainDb)}`);
  }

  function terms(freq, q, fs) {
    checkFreq(freq, fs);
    checkQ(q);
    const w0 = (2 * Math.PI * freq) / fs;
    const sw = Math.sin(w0);
    return [Math.cos(w0), sw, sw / (2 * q)];
  }

  // --- stability (app/dsp/stability.py) ---------------------------------------------------------------------------

  const triangle = (a1, a2) => Math.abs(a2) < 1 && Math.abs(a1) < 1 - a2;

  // Poles of 1 − a1·z⁻¹ − a2·z⁻² strictly inside the unit circle, in float64 and after float32 rounding.
  function polesStable(a1, a2) {
    if (!(Number.isFinite(a1) && Number.isFinite(a2))) return false;
    return triangle(a1, a2) && triangle(Math.fround(a1), Math.fround(a2));
  }

  // Largest pole magnitude (< 1 means stable): roots of z² − a1·z − a2.
  function poleRadius(a1, a2) {
    const disc = a1 * a1 + 4 * a2;
    if (disc < 0) return Math.sqrt(-a2);
    const s = Math.sqrt(disc);
    return Math.max(Math.abs((a1 + s) / 2), Math.abs((a1 - s) / 2));
  }

  // Why the biquad can't be sent to the device, or null when it's fine.
  function biquadProblem(biquad) {
    let c;
    try {
      c = coeffs(biquad);
    } catch (e) {
      return e.message;
    }
    if (!c.every(Number.isFinite)) return 'biquad has a non-finite coefficient';
    if (!c.every((x) => Number.isFinite(Math.fround(x)))) return 'biquad coefficient overflows float32';
    const [, , , a1, a2] = c;
    if (!polesStable(a1, a2)) {
      if (triangle(a1, a2)) {
        // Stable as designed; only the device's float32 rounding pushes the poles onto/over the unit circle.
        const radius32 = Number(poleRadius(Math.fround(a1), Math.fround(a2)).toPrecision(6));
        return (
          `unstable after float32 rounding (device precision): pole radius ${radius32}; ` +
          'raise the frequency or lower the gain or Q'
        );
      }
      const radius = Number(poleRadius(a1, a2).toPrecision(6));
      return `unstable biquad: pole radius ${radius} (needs |a2| < 1 and |a1| < 1 - a2)`;
    }
    return null;
  }

  const isStable = (biquad) => biquadProblem(biquad) === null;

  function checked(biquad) {
    const problem = biquadProblem(biquad);
    if (problem !== null) throw new DesignError(problem);
    return biquad;
  }

  // --- second-order RBJ recipes ---------------------------------------------------------------------------------

  function peaking(freq, q, gainDb, fs = FS) {
    checkGain(gainDb);
    const [cw, , alpha] = terms(freq, q, fs);
    const a = 10 ** (gainDb / 40);
    return fromRbj(1 + alpha * a, -2 * cw, 1 - alpha * a, 1 + alpha / a, -2 * cw, 1 - alpha / a);
  }

  function lowShelf(freq, q, gainDb, fs = FS) {
    checkGain(gainDb);
    const [cw, , alpha] = terms(freq, q, fs);
    const a = 10 ** (gainDb / 40);
    const k = 2 * Math.sqrt(a) * alpha;
    return fromRbj(
      a * ((a + 1) - (a - 1) * cw + k),
      2 * a * ((a - 1) - (a + 1) * cw),
      a * ((a + 1) - (a - 1) * cw - k),
      (a + 1) + (a - 1) * cw + k,
      -2 * ((a - 1) + (a + 1) * cw),
      (a + 1) + (a - 1) * cw - k,
    );
  }

  function highShelf(freq, q, gainDb, fs = FS) {
    checkGain(gainDb);
    const [cw, , alpha] = terms(freq, q, fs);
    const a = 10 ** (gainDb / 40);
    const k = 2 * Math.sqrt(a) * alpha;
    return fromRbj(
      a * ((a + 1) + (a - 1) * cw + k),
      -2 * a * ((a - 1) + (a + 1) * cw),
      a * ((a + 1) + (a - 1) * cw - k),
      (a + 1) - (a - 1) * cw + k,
      2 * ((a - 1) - (a + 1) * cw),
      (a + 1) - (a - 1) * cw - k,
    );
  }

  // RBJ shelf slope S (1 = steepest monotonic) → the equivalent Q.
  function shelfQFromSlope(slope, gainDb) {
    if (!(Number.isFinite(slope) && slope > 0)) {
      throw new DesignError(`shelf slope must be positive, got ${fmt(slope)}`);
    }
    checkGain(gainDb);
    const a = 10 ** (gainDb / 40);
    const invQSq = (a + 1 / a) * (1 / slope - 1) + 2;
    if (invQSq <= 0) throw new DesignError(`shelf slope ${fmt(slope)} is too steep for ${fmt(gainDb)} dB`);
    return 1 / Math.sqrt(invQSq);
  }

  function lowpass2(freq, q, fs = FS) {
    const [cw, , alpha] = terms(freq, q, fs);
    return fromRbj((1 - cw) / 2, 1 - cw, (1 - cw) / 2, 1 + alpha, -2 * cw, 1 - alpha);
  }

  function highpass2(freq, q, fs = FS) {
    const [cw, , alpha] = terms(freq, q, fs);
    return fromRbj((1 + cw) / 2, -(1 + cw), (1 + cw) / 2, 1 + alpha, -2 * cw, 1 - alpha);
  }

  // Constant 0 dB peak gain band-pass.
  function bandpass(freq, q, fs = FS) {
    const [cw, , alpha] = terms(freq, q, fs);
    return fromRbj(alpha, 0, -alpha, 1 + alpha, -2 * cw, 1 - alpha);
  }

  function notch(freq, q, fs = FS) {
    const [cw, , alpha] = terms(freq, q, fs);
    return fromRbj(1, -2 * cw, 1, 1 + alpha, -2 * cw, 1 - alpha);
  }

  function allpass2(freq, q, fs = FS) {
    const [cw, , alpha] = terms(freq, q, fs);
    return fromRbj(1 - alpha, -2 * cw, 1 + alpha, 1 + alpha, -2 * cw, 1 - alpha);
  }

  // --- first-order sections (one biquad slot, b2 = a2 = 0) ------------------------------------------------------

  function tanK(freq, fs) {
    checkFreq(freq, fs);
    return Math.tan((Math.PI * freq) / fs);
  }

  function lowpass1(freq, fs = FS) {
    const k = tanK(freq, fs);
    return fromRbj(k, k, 0, 1 + k, k - 1, 0);
  }

  function highpass1(freq, fs = FS) {
    const k = tanK(freq, fs);
    return fromRbj(1, -1, 0, 1 + k, k - 1, 0);
  }

  // +1 at DC, −90° at freq, −180° towards Nyquist (same phase sense as allpass2).
  function allpass1(freq, fs = FS) {
    const k = tanK(freq, fs);
    return fromRbj(k - 1, 1 + k, 0, 1 + k, k - 1, 0);
  }

  // --- bilinear transform / Linkwitz Transform ------------------------------------------------------------------

  // Analog (n2·s² + n1·s + n0) / (d2·s² + d1·s + d0) → biquad, exact at prewarpHz.
  function bilinear(num, den, prewarpHz, fs = FS) {
    checkFreq(prewarpHz, fs, 'pre-warp frequency');
    const wc = 2 * Math.PI * prewarpHz;
    const g = wc / Math.tan(wc / (2 * fs));
    const g2 = g * g;
    const [n2, n1, n0] = num;
    const [d2, d1, d0] = den;
    return fromRbj(
      n2 * g2 + n1 * g + n0,
      2 * (n0 - n2 * g2),
      n2 * g2 - n1 * g + n0,
      d2 * g2 + d1 * g + d0,
      2 * (d0 - d2 * g2),
      d2 * g2 - d1 * g + d0,
    );
  }

  // Moves a sealed driver's (f0, Q0) to the target (fp, Qp); pre-warped at sqrt(f0·fp) like the server.
  function linkwitzTransform(f0, q0, fp, qp, fs = FS) {
    checkFreq(f0, fs, 'f0');
    checkFreq(fp, fs, 'fp');
    checkQ(q0, 'Q0');
    checkQ(qp, 'Qp');
    const w0 = 2 * Math.PI * f0;
    const wp = 2 * Math.PI * fp;
    return bilinear([1, w0 / q0, w0 * w0], [1, wp / qp, wp * wp], Math.sqrt(f0 * fp), fs);
  }

  // --- trivial sections -----------------------------------------------------------------------------------------

  function gain(gainDb) {
    checkGain(gainDb);
    return { b0: 10 ** (gainDb / 20), b1: 0, b2: 0, a1: 0, a2: 0 };
  }

  const passthrough = () => ({ ...PASSTHROUGH });

  // A user-entered biquad, accepted only when finite and stable (also after float32 rounding).
  const raw = (biquad) => checked(toBiquad(biquad));

  // --- single-slot dispatch -------------------------------------------------------------------------------------

  const FILTER_TYPES = {
    peaking: ['freq', 'q', 'gain_db'],
    lowshelf: ['freq', 'q', 'gain_db'],
    highshelf: ['freq', 'q', 'gain_db'],
    lp1: ['freq'],
    hp1: ['freq'],
    lp2: ['freq', 'q'],
    hp2: ['freq', 'q'],
    bandpass: ['freq', 'q'],
    notch: ['freq', 'q'],
    ap1: ['freq'],
    ap2: ['freq', 'q'],
    lt: ['lt'],
    gain: ['gain_db'],
    passthrough: [],
    raw: ['raw'],
  };

  // Compile one PEQ band design ({type, freq, q, gain_db, lt: {f0, q0, fp, qp}, raw}) into a biquad.
  // Throws DesignError for impossible or unstable designs.
  function designFilter(design, fs = FS) {
    const type = (design && design.type) || 'passthrough';
    const required = FILTER_TYPES[type];
    if (!required) throw new DesignError(`unknown filter type '${type}'`);
    const missing = required.filter((name) => design[name] === null || design[name] === undefined);
    if (missing.length) throw new DesignError(`filter type '${type}' requires ${missing.join(', ')}`);
    const { freq: f, q, gain_db: g } = design;
    let bq;
    switch (type) {
      case 'peaking': bq = peaking(f, q, g, fs); break;
      case 'lowshelf': bq = lowShelf(f, q, g, fs); break;
      case 'highshelf': bq = highShelf(f, q, g, fs); break;
      case 'lp1': bq = lowpass1(f, fs); break;
      case 'hp1': bq = highpass1(f, fs); break;
      case 'lp2': bq = lowpass2(f, q, fs); break;
      case 'hp2': bq = highpass2(f, q, fs); break;
      case 'bandpass': bq = bandpass(f, q, fs); break;
      case 'notch': bq = notch(f, q, fs); break;
      case 'ap1': bq = allpass1(f, fs); break;
      case 'ap2': bq = allpass2(f, q, fs); break;
      case 'lt': {
        const { f0, q0, fp, qp } = design.lt;
        bq = linkwitzTransform(f0, q0, fp, qp, fs);
        break;
      }
      case 'gain': bq = gain(g); break;
      case 'passthrough': return passthrough();
      default: return raw(design.raw);
    }
    return checked(bq);
  }

  // --- crossovers -----------------------------------------------------------------------------------------------

  // Second-order Qs of a Butterworth filter, ascending: Q_k = 1 / (2 sin((2k−1)π / 2n)). Odd orders add one
  // first-order section, which is not listed.
  function butterworthQs(order) {
    if (!(Number.isInteger(order) && order >= 1 && order <= 8)) {
      throw new DesignError(`Butterworth order must be 1..8, got ${order}`);
    }
    const qs = [];
    for (let k = 1; k <= Math.floor(order / 2); k++) qs.push(1 / (2 * Math.sin(((2 * k - 1) * Math.PI) / (2 * order))));
    return qs.sort((x, y) => x - y);
  }

  // LR2n = BWn cascaded twice; the two first-order sections of an odd BWn merge into one Q = 0.5 biquad.
  function linkwitzRileyQs(order) {
    if (![2, 4, 6, 8].includes(order)) throw new DesignError(`Linkwitz-Riley order must be 2, 4, 6 or 8, got ${order}`);
    const half = order / 2;
    const bw = butterworthQs(half);
    const qs = [...bw, ...bw];
    if (half % 2) qs.push(0.5);
    return qs.sort((x, y) => x - y);
  }

  function sectionBiquad(s, fs) {
    switch (s.kind) {
      case 'lp1': return lowpass1(s.freq, fs);
      case 'hp1': return highpass1(s.freq, fs);
      case 'lp2': return lowpass2(s.freq, s.q, fs);
      default: return highpass2(s.freq, s.q, fs);
    }
  }

  function prototypeSections(kind, family, order, freq) {
    const first = kind === 'lp' ? 'lp1' : 'hp1';
    const second = kind === 'lp' ? 'lp2' : 'hp2';
    if (family === 'bw') {
      const sections = order % 2 ? [{ kind: first, freq, q: null }] : [];
      return sections.concat(butterworthQs(order).map((q) => ({ kind: second, freq, q })));
    }
    if (family === 'lr') return linkwitzRileyQs(order).map((q) => ({ kind: second, freq, q }));
    if (family === 'bessel') {
      throw new DesignError('Bessel crossovers are designed by the server', { serverOnly: true });
    }
    throw new DesignError(`unknown crossover family '${family}'`);
  }

  // Same rules as the server's CrossoverDesign model.
  function validateCrossover(design) {
    if (!design || typeof design !== 'object') throw new DesignError('a crossover design is required');
    const { kind, family, order, freq, freq_hi: freqHi } = design;
    if (!['lp', 'hp', 'bp'].includes(kind)) throw new DesignError(`unknown crossover kind '${kind}'`);
    if (!['bw', 'lr', 'bessel'].includes(family)) throw new DesignError(`unknown crossover family '${family}'`);
    if (!(Number.isInteger(order) && order >= 1 && order <= 8)) {
      throw new DesignError(`crossover order must be 1..8, got ${order}`);
    }
    if (!(Number.isFinite(freq) && freq > 0)) {
      throw new DesignError(`crossover frequency must be positive, got ${fmt(freq)}`);
    }
    if (family === 'lr' && order % 2) throw new DesignError('Linkwitz-Riley crossovers need an even order');
    if (family === 'bessel' && order < 2) throw new DesignError('Bessel crossovers need order 2..8');
    if (kind === 'bp') {
      if (freqHi === null || freqHi === undefined) throw new DesignError('band-pass needs freq_hi');
      if (!(freqHi > freq)) throw new DesignError('freq_hi must be above freq');
    }
  }

  // Analog sections of a crossover group, in the order they are written (band-pass: high-pass first).
  function crossoverSections(design) {
    validateCrossover(design);
    const { kind, family, order, freq } = design;
    if (kind === 'bp') {
      const highPass = prototypeSections('hp', family, order, freq);
      return highPass.concat(prototypeSections('lp', family, order, design.freq_hi));
    }
    return prototypeSections(kind, family, order, freq);
  }

  const biquadsNeeded = (design) => crossoverSections(design).length;

  // Compile a crossover group ({kind: lp|hp|bp, family: bw|lr, order, freq, freq_hi}) into exactly nBiquads
  // biquads, padding unused slots with pass-through.
  function designCrossover(design, nBiquads = 4, fs = FS) {
    validateCrossover(design);
    for (const f of [design.freq, design.freq_hi]) if (f !== null && f !== undefined) checkFreq(f, fs);
    const sections = crossoverSections(design);
    if (sections.length > nBiquads) {
      const { family, order, kind } = design;
      const needs = `${family} order ${order} ${kind} needs ${sections.length} biquads`;
      throw new DesignError(`${needs}, the group has ${nBiquads}`);
    }
    const biquads = sections.map((s) => checked(sectionBiquad(s, fs)));
    while (biquads.length < nBiquads) biquads.push(passthrough());
    return biquads;
  }

  // --- frequency grid -------------------------------------------------------------------------------------------

  // n log-spaced frequencies from fMin to fMax (Hz), both included.
  function freqGrid(fMin = F_MIN, fMax = F_MAX, n = N_POINTS) {
    if (!(fMin > 0 && fMin < fMax) || n < 2) {
      throw new RangeError(`bad frequency grid ${fMin}..${fMax} Hz with ${n} points`);
    }
    const out = new Float64Array(n);
    const step = Math.log(fMax / fMin) / (n - 1);
    for (let i = 0; i < n; i++) out[i] = fMin * Math.exp(step * i);
    out[0] = fMin;
    out[n - 1] = fMax;
    return out;
  }

  // DC plus n log-spaced frequencies from 0.1 Hz to Nyquist: where float32 rounding can matter outside the plot.
  function fullBandGrid(fs = FS, n = N_POINTS) {
    const out = new Float64Array(n + 1);
    out.set(freqGrid(CHECK_F_MIN, fs / 2, n), 1);
    return out;
  }

  const viewGrid = (view, n = N_POINTS) => freqGrid(...VIEWS[view], n);

  // Delay rounded to whole samples like the device, with Python's round-half-to-even so both sides agree on ties.
  function delaySamples(delayMs, fs = FS) {
    const x = (delayMs * fs) / 1000;
    const r = Math.round(x);
    return r - x === 0.5 && r % 2 !== 0 ? r - 1 : r;
  }

  // --- transfer functions -------------------------------------------------------------------------------------------
  //
  // A transfer function is {re, im, dre, dim}: H and dH/dω (ω in rad/sample) sampled on one grid, as Float64Arrays.
  // The derivative is propagated exactly, so group delay is −Im(H'/H) (same as the server).

  function tfNew(n) {
    return { re: new Float64Array(n), im: new Float64Array(n), dre: new Float64Array(n), dim: new Float64Array(n) };
  }

  function tfUnity(n) {
    const t = tfNew(n);
    t.re.fill(1);
    return t;
  }

  const tfZero = (n) => tfNew(n);

  // In-place product: a ← a·b (derivative by the product rule). Returns a.
  function tfMulInPlace(a, b) {
    const n = a.re.length;
    for (let i = 0; i < n; i++) {
      const ar = a.re[i], ai = a.im[i], adr = a.dre[i], adi = a.dim[i];
      const br = b.re[i], bi = b.im[i], bdr = b.dre[i], bdi = b.dim[i];
      a.re[i] = ar * br - ai * bi;
      a.im[i] = ar * bi + ai * br;
      a.dre[i] = adr * br - adi * bi + (ar * bdr - ai * bdi);
      a.dim[i] = adr * bi + adi * br + (ar * bdi + ai * bdr);
    }
    return a;
  }

  function tfCopy(a) {
    const copy = (x) => Float64Array.from(x);
    return { re: copy(a.re), im: copy(a.im), dre: copy(a.dre), dim: copy(a.dim) };
  }

  const tfMul = (a, b) => tfMulInPlace(tfCopy(a), b);

  function tfAdd(a, b) {
    const t = tfCopy(a);
    for (let i = 0; i < t.re.length; i++) {
      t.re[i] += b.re[i];
      t.im[i] += b.im[i];
      t.dre[i] += b.dre[i];
      t.dim[i] += b.dim[i];
    }
    return t;
  }

  function tfScale(a, k) {
    const t = tfCopy(a);
    for (let i = 0; i < t.re.length; i++) {
      t.re[i] *= k;
      t.im[i] *= k;
      t.dre[i] *= k;
      t.dim[i] *= k;
    }
    return t;
  }

  const omega = (f, fs) => (2 * Math.PI * f) / fs;

  // Cascade of biquads; float32 (default) rounds each coefficient like the device does.
  function cascadeTf(biquads, freqs, { fs = FS, float32 = true } = {}) {
    const n = freqs.length;
    const out = tfUnity(n);
    const list = Array.from(biquads, (bq) => {
      const c = coeffs(bq);
      return float32 ? c.map(Math.fround) : c;
    });
    if (!list.length) return out;
    for (let i = 0; i < n; i++) {
      const w = omega(freqs[i], fs);
      // z⁻¹ = e^{−jω}, z⁻² = (z⁻¹)²
      const c1 = Math.cos(w), s1 = -Math.sin(w);
      const c2 = c1 * c1 - s1 * s1, s2 = 2 * c1 * s1;
      let hr = 1, hi = 0, dr = 0, di = 0;
      for (const [b0, b1, b2, a1, a2] of list) {
        const nr = b0 + b1 * c1 + b2 * c2, ni = b1 * s1 + b2 * s2;
        const er = 1 - a1 * c1 - a2 * c2, ei = -(a1 * s1 + a2 * s2);
        // dN = −j(b1·z⁻¹ + 2·b2·z⁻²), dD = j(a1·z⁻¹ + 2·a2·z⁻²); −j·(x + jy) = y − jx
        const pr = b1 * c1 + 2 * b2 * c2, pi = b1 * s1 + 2 * b2 * s2;
        const dnr = pi, dni = -pr;
        const qr = a1 * c1 + 2 * a2 * c2, qi = a1 * s1 + 2 * a2 * s2;
        const ddr = -qi, ddi = qr;
        const mag = er * er + ei * ei;
        // H = N / D, H' = (N'·D − N·D') / D²  =  (N' − H·D') / D
        const kr = (nr * er + ni * ei) / mag, ki = (ni * er - nr * ei) / mag;
        const tr = dnr - (kr * ddr - ki * ddi), ti = dni - (kr * ddi + ki * ddr);
        const kdr = (tr * er + ti * ei) / mag, kdi = (ti * er - tr * ei) / mag;
        const nhr = hr * kr - hi * ki, nhi = hr * ki + hi * kr;
        const ndr = dr * kr - di * ki + (hr * kdr - hi * kdi);
        const ndi = dr * ki + di * kr + (hr * kdi + hi * kdr);
        hr = nhr; hi = nhi; dr = ndr; di = ndi;
      }
      out.re[i] = hr;
      out.im[i] = hi;
      out.dre[i] = dr;
      out.dim[i] = di;
    }
    return out;
  }

  // e^{−jω·samples}
  function delayTf(samples, freqs, fs = FS) {
    const t = tfNew(freqs.length);
    for (let i = 0; i < freqs.length; i++) {
      const p = -omega(freqs[i], fs) * samples;
      const hr = Math.cos(p), hi = Math.sin(p);
      t.re[i] = hr;
      t.im[i] = hi;
      // dH = −j·samples·H
      t.dre[i] = samples * hi;
      t.dim[i] = -samples * hr;
    }
    return t;
  }

  // --- responses ----------------------------------------------------------------------------------------------------

  // {freqs, magnitude_db, phase_deg, phase_unwrapped_deg, group_delay_ms} from a transfer function: the same keys and
  // conventions as the server's Response.to_dict() (dB floored at −200; phase 0 where the response is exactly zero).
  function toResponse(tf, freqs, fs = FS) {
    const n = freqs.length;
    const f = Float64Array.from(freqs);
    const db = new Float64Array(n);
    const ph = new Float64Array(n);
    const unwrapped = new Float64Array(n);
    const gd = new Float64Array(n);
    const dphase = new Float64Array(n);
    const wrapped = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      const hr = tf.re[i], hi = tf.im[i];
      const m = Math.hypot(hr, hi);
      db[i] = m === 0 ? DB_FLOOR : Math.max(20 * Math.log10(m), DB_FLOOR);
      if (m === 0) continue;
      wrapped[i] = Math.atan2(hi, hr);
      // dφ/dω = Im(H'/H)
      dphase[i] = (tf.dim[i] * hr - tf.dre[i] * hi) / (hr * hr + hi * hi);
    }
    for (let i = 0; i < n; i++) {
      ph[i] = (wrapped[i] * 180) / Math.PI;
      gd[i] = ((-dphase[i] / fs) * 1000) || 0;
      if (i === 0) {
        unwrapped[0] = wrapped[0];
        continue;
      }
      // Integrate dφ/dω from the previous point, then snap to the nearest 2π branch of the wrapped phase.
      const predicted = unwrapped[i - 1] + 0.5 * (dphase[i - 1] + dphase[i]) * (omega(f[i], fs) - omega(f[i - 1], fs));
      unwrapped[i] = wrapped[i] + 2 * Math.PI * Math.round((predicted - wrapped[i]) / (2 * Math.PI));
    }
    for (let i = 0; i < n; i++) unwrapped[i] = (unwrapped[i] * 180) / Math.PI;
    return { freqs: f, magnitude_db: db, phase_deg: ph, phase_unwrapped_deg: unwrapped, group_delay_ms: gd };
  }

  // Largest |dB difference| between two transfer functions where the reference is above floorDb.
  function maxDbDifference(reference, other, floorDb = FLOAT32_RELEVANT_DB) {
    let worst = 0;
    for (let i = 0; i < reference.re.length; i++) {
      const ref = 20 * Math.log10(Math.hypot(reference.re[i], reference.im[i]));
      if (!(ref >= floorDb)) continue;
      const oth = 20 * Math.log10(Math.max(Math.hypot(other.re[i], other.im[i]), 1e-300));
      worst = Math.max(worst, Math.abs(oth - ref));
    }
    return worst;
  }

  // build(float32, grid) -> transfer function. The deviation is judged on the plotted grid and on the full band, so
  // rounding that only shows below 5 Hz (a Linkwitz Transform's DC gain) or above 24 kHz still warns.
  function finish(build, freqs, fs, float32, label) {
    const tf = build(float32, freqs);
    const check = fullBandGrid(fs);
    const deviation = Math.max(
      maxDbDifference(build(false, freqs), float32 ? tf : build(true, freqs)),
      maxDbDifference(build(false, check), build(true, check)),
    );
    const out = toResponse(tf, freqs, fs);
    out.float32_deviation_db = deviation;
    out.warnings = [];
    if (deviation > FLOAT32_WARN_DB) {
      out.warnings.push(
        `${label}: rounding note: the device stores filters as float32 numbers, which moves the response by up ` +
          `to ${deviation.toFixed(2)} dB`,
      );
    }
    return out;
  }

  const gridOf = (freqs) => (freqs ? Float64Array.from(freqs) : freqGrid());

  // Response of a biquad cascade (one band, one crossover group, or any list).
  function filterResponse(biquads, freqs, { fs = FS, float32 = true, label = 'filter' } = {}) {
    const grid = gridOf(freqs);
    const list = Array.from(biquads);
    return finish((f32, g) => cascadeTf(list, g, { fs, float32: f32 }), grid, fs, float32, label);
  }

  // --- channel chains (ConfigDoc JSON, docs 04 §4) ------------------------------------------------------------------

  const gainFactor = (gainDb) => 10 ** (gainDb / 20);

  const peqTf = (bands, freqs, opts) =>
    cascadeTf((bands || []).filter((b) => !b.bypass).map((b) => b.compiled), freqs, opts);

  const crossoverTf = (groups, freqs, opts) =>
    cascadeTf((groups || []).filter((g) => !g.bypass).flatMap((g) => g.compiled || []), freqs, opts);

  function inputTf(doc, i, freqs, opts) {
    const ch = doc.inputs[i];
    if (ch.mute) return tfZero(freqs.length);
    return tfScale(peqTf(ch.peq, freqs, opts), gainFactor(ch.gain_db));
  }

  // Each stage of output n's chain plus `total`, in processing order: input (routed sum), peq, crossover, fir,
  // polarity, delay (whole samples), gain (output gain, mute, optional master). `inputs` picks which inputs feed the
  // sum (default Input 1; null = all), summed coherently. The FIR is server-only and contributes 1 here.
  function outputStages(doc, n, freqs, { fs = FS, float32 = true, inputs = [0], masterDb = null } = {}) {
    const m = freqs.length;
    const out = doc.outputs[n];
    const opts = { fs, float32 };
    const selected = inputs === null ? doc.inputs.map((_, i) => i) : inputs;
    let routed = tfZero(m);
    for (const i of selected) {
      const cell = doc.inputs[i].routing[n];
      if (cell && cell.enabled) routed = tfAdd(routed, tfScale(inputTf(doc, i, freqs, opts), gainFactor(cell.gain_db)));
    }
    const stages = {
      input: routed,
      peq: peqTf(out.peq, freqs, opts),
      crossover: crossoverTf(out.crossover, freqs, opts),
      fir: tfUnity(m),
      polarity: tfScale(tfUnity(m), out.invert ? -1 : 1),
      delay: delayTf(delaySamples(out.delay_ms || 0, fs), freqs, fs),
      gain: tfScale(tfUnity(m), out.mute ? 0 : gainFactor((out.gain_db || 0) + (masterDb || 0))),
    };
    const total = tfUnity(m);
    for (const t of Object.values(stages)) tfMulInPlace(total, t);
    stages.total = total;
    return stages;
  }

  const firWarnings = (doc, outputs) =>
    outputs
      .filter((n) => doc.outputs[n].fir && !doc.outputs[n].fir.bypass)
      .map((n) => `output ${n + 1}: the FIR is active but its taps are unknown, so it is plotted as flat`);

  // Electrical sum of the selected outputs (complex, so delay and polarity matter). Ignores drivers and the room.
  function systemResponse(doc, outputs, freqs, { fs = FS, float32 = true, inputs = [0], masterDb = null } = {}) {
    const grid = gridOf(freqs);
    const build = (f32, g) => {
      let total = tfZero(g.length);
      for (const n of outputs) {
        total = tfAdd(total, outputStages(doc, n, g, { fs, float32: f32, inputs, masterDb }).total);
      }
      return total;
    };
    const label = outputs.map((n) => `output ${n + 1}`).join(', ') || 'sum';
    const resp = finish(build, grid, fs, float32, label);
    resp.warnings.push(...firWarnings(doc, outputs));
    return resp;
  }

  const outputResponse = (doc, n, freqs, opts) => systemResponse(doc, [n], freqs, opts);

  // Input i's PEQ only (gain and mute excluded), as the Input page plots it.
  function inputResponse(doc, i, freqs, { fs = FS, float32 = true } = {}) {
    const grid = gridOf(freqs);
    const build = (f32, g) => peqTf(doc.inputs[i].peq, g, { fs, float32: f32 });
    return finish(build, grid, fs, float32, `input ${i + 1} PEQ`);
  }

  const api = Object.freeze({
    FS,
    N_POINTS,
    F_MIN,
    F_MAX,
    VIEWS,
    DB_FLOOR,
    FLOAT32_WARN_DB,
    PASSTHROUGH,
    FILTER_TYPES: Object.keys(FILTER_TYPES),
    DesignError,
    // coefficients
    fromRbj,
    coeffs,
    toBiquad,
    toFloat32,
    isPassthrough,
    // stability
    polesStable,
    poleRadius,
    biquadProblem,
    isStable,
    // filters
    peaking,
    lowShelf,
    highShelf,
    shelfQFromSlope,
    lowpass2,
    highpass2,
    bandpass,
    notch,
    allpass2,
    lowpass1,
    highpass1,
    allpass1,
    bilinear,
    linkwitzTransform,
    gain,
    passthrough,
    raw,
    designFilter,
    // crossovers
    butterworthQs,
    linkwitzRileyQs,
    crossoverSections,
    biquadsNeeded,
    designCrossover,
    // grid and transfer functions
    freqGrid,
    fullBandGrid,
    viewGrid,
    delaySamples,
    gainFactor,
    tfUnity,
    tfZero,
    tfMul,
    tfAdd,
    tfScale,
    cascadeTf,
    delayTf,
    toResponse,
    maxDbDifference,
    // responses
    filterResponse,
    outputStages,
    outputResponse,
    systemResponse,
    inputResponse,
  });

  if (typeof module === 'object' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.mdsp = Object.assign(window.mdsp || {}, { dsp: api });
})();
