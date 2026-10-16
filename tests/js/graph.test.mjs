// app/static/js/graph.js: the pure helpers, plus ResponsePlot driven against a fake uPlot and a minimal fake DOM.
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { loadScripts } from './load.mjs';

const { graph, dsp } = loadScripts('app/static/js/dsp.js', 'app/static/js/graph.js');

describe('helpers', () => {
  test('1-2-5 log ticks and labels', () => {
    assert.deepEqual(graph.logSplits(5, 300), [5, 10, 20, 50, 100, 200]);
    assert.deepEqual(graph.logSplits(10, 20000).map(graph.tickLabel), [
      '10', '20', '50', '100', '200', '500', '1k', '2k', '5k', '10k', '20k',
    ]);
    assert.deepEqual(graph.logSplits(0, 10), []);
    assert.deepEqual(graph.minorSplits(5, 20), [6, 7, 8, 9]);
  });

  test('number formats use the real minus sign and units', () => {
    assert.equal(graph.fmtFreq(42), '42.0 Hz');
    assert.equal(graph.fmtFreq(120.4), '120 Hz');
    assert.equal(graph.fmtFreq(1250), '1.25 kHz');
    assert.equal(graph.fmtFreq(16000), '16.0 kHz');
    assert.equal(graph.fmtDb(-6.1), '\u22126.1 dB');
    assert.equal(graph.fmtDb(-0.04), '0.0 dB');
    assert.equal(graph.fmtDb(3.26), '3.3 dB');
    assert.equal(graph.fmtPhase(-90), '−90°');
    assert.equal(graph.fmtMs(9), '9.00 ms');
    assert.equal(graph.fmtDb(null), '—');
    assert.equal(graph.fmtFreq(NaN), '—');
  });

  test('channel colours: Settings overrides, then the defaults, then the fallback', () => {
    assert.equal(graph.channelColor('output', 1), '#f0b429');
    assert.equal(graph.channelColor('input', 0), '#4ea1ff');
    assert.equal(graph.channelColor('output', 9), graph.COLORS.fallback);
    assert.equal(graph.channelColor('output', 1, { 'output.1': '#123456' }), '#123456');
    assert.equal(graph.channelColor('output', 1, { output: [null, '#abcdef'] }), '#abcdef');
  });

  test('nearest grid index on a log axis', () => {
    const f = [10, 100, 1000];
    assert.equal(graph.nearestIndex(f, 5), 0);
    assert.equal(graph.nearestIndex(f, 31), 0);
    assert.equal(graph.nearestIndex(f, 32), 1);
    assert.equal(graph.nearestIndex(f, 5000), 2);
    assert.equal(graph.nearestIndex([], 5), -1);
  });

  test('resampling interpolates in log frequency and leaves gaps outside the source', () => {
    const out = graph.resample([10, 1000], [0, 20], [5, 10, 100, 1000, 2000]);
    assert.deepEqual(out.map((v) => (v === null ? null : Number(v.toFixed(9)))), [null, 0, 10, 20, null]);
  });

  test('trace values per mode, including unwrapped phase', () => {
    const r = dsp.filterResponse([dsp.lowpass2(80, 0.7071)], dsp.viewGrid('sub', 64));
    const t = { response: r };
    assert.deepEqual(graph.traceValues(t, 'magnitude'), Array.from(r.magnitude_db));
    assert.deepEqual(graph.traceValues(t, 'phase'), Array.from(r.phase_deg));
    assert.deepEqual(graph.traceValues(t, 'phase', { unwrap: true }), Array.from(r.phase_unwrapped_deg));
    assert.deepEqual(graph.traceValues(t, 'group_delay'), Array.from(r.group_delay_ms));
    const coarse = graph.traceValues(t, 'magnitude', { grid: [10, 80, 200] });
    assert.equal(coarse.length, 3);
  });

  test('readout rows give f, dB, phase and group delay for every visible trace', () => {
    const grid = dsp.viewGrid('sub', 32);
    const lp = dsp.filterResponse([dsp.lowpass2(80, 0.7071)], grid);
    const hp = dsp.filterResponse([dsp.highpass2(20, 0.7071)], grid);
    const traces = [
      { id: 'a', label: 'Sub', color: '#111111', response: lp },
      { id: 'b', label: 'Shakers', color: '#222222', response: hp },
      { id: 'c', label: 'Hidden', color: '#333333', response: hp, visible: false },
    ];
    const out = graph.readoutRows(traces, grid, 10);
    assert.equal(out.freq, grid[10]);
    assert.deepEqual(out.rows.map((r) => r.label), ['Sub', 'Shakers']);
    assert.equal(out.rows[0].db, lp.magnitude_db[10]);
    assert.equal(out.rows[1].phase, hp.phase_deg[10]);
    assert.equal(out.rows[1].group_delay, hp.group_delay_ms[10]);
    assert.equal(graph.readoutRows(traces, grid, null), null);
  });

  test('auto-fit ignores the −200 dB floor and points outside the view', () => {
    const grid = dsp.freqGrid();
    const lp = dsp.filterResponse(dsp.designCrossover({ kind: 'lp', family: 'lr', order: 4, freq: 80 }), grid);
    const [lo, hi] = graph.autoRange([{ response: lp }], 'magnitude', grid, [5, 300]);
    assert.ok(hi > 0 && hi <= 6, `hi ${hi}`);
    assert.ok(lo < -40 && lo >= -60 && lo % 6 === 0, `lo ${lo}`);
    const muted = { response: dsp.toResponse(dsp.tfZero(grid.length), grid) };
    assert.deepEqual(graph.autoRange([muted], 'magnitude', grid, [5, 300]), [-24, 24]);
    assert.deepEqual(graph.defaultRange('phase'), [-180, 180]);
    assert.equal(graph.defaultRange('phase', true), null);
    assert.equal(graph.defaultRange('group_delay'), null);
  });

  test('y ticks', () => {
    assert.deepEqual(graph.ySplits('phase', -180, 180), [-180, -135, -90, -45, 0, 45, 90, 135, 180]);
    assert.deepEqual(graph.ySplits('magnitude', -24, 24), [-24, -18, -12, -6, 0, 6, 12, 18, 24]);
    assert.ok(graph.ySplits('group_delay', 0, 10).length >= 4);
  });
});

// --- ResponsePlot against fakes ---------------------------------------------------------------------------------

class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.nodeType = 1;
    this.children = [];
    this.attributes = {};
    this.style = {};
    this.className = '';
    this.textContent = '';
    this.clientWidth = 640;
    const classes = new Set();
    this.classList = { add: (c) => classes.add(c), contains: (c) => classes.has(c) };
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  append(...children) {
    this.children.push(...children);
  }

  prepend(child) {
    this.children.unshift(child);
  }

  replaceChildren(...children) {
    this.children = children;
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }

  get text() {
    return this.textContent + this.children.map((c) => c.text).join(' ');
  }

  find(className) {
    if (this.className === className) return this;
    for (const c of this.children) {
      const hit = c.find(className);
      if (hit) return hit;
    }
    return null;
  }
}

class FakeUPlot {
  static instances = [];
  static pxRatio = 1;

  constructor(opts, data, target) {
    this.opts = opts;
    this.data = data;
    this.target = target;
    this.width = opts.width;
    this.height = opts.height;
    this.cursor = { idx: null };
    this.scales = { x: {}, y: {} };
    this.bbox = { left: 0, top: 0, width: 600, height: 200 };
    this.over = { getBoundingClientRect: () => ({ left: 100, top: 50, width: 600, height: 200 }) };
    this.setDataCalls = 0;
    this.destroyed = false;
    this.ctx = { save() {}, restore() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, arc() {}, fill() {} };
    const [xmin, xmax] = opts.scales.x.range();
    this.scales.x = { min: xmin, max: xmax };
    const [ymin, ymax] = opts.scales.y.range();
    this.scales.y = { min: ymin, max: ymax };
    FakeUPlot.instances.push(this);
  }

  setData(data) {
    this.data = data;
    this.setDataCalls++;
  }

  setScale(key, { min, max }) {
    this.scales[key] = { min, max };
  }

  setSize({ width, height }) {
    this.width = width;
    this.height = height;
  }

  redraw() {
    for (const h of this.opts.hooks.draw) h(this);
  }

  destroy() {
    this.destroyed = true;
  }

  valToPos(v, key) {
    const { min, max } = this.scales[key];
    if (key === 'x') return ((Math.log10(v) - Math.log10(min)) / (Math.log10(max) - Math.log10(min))) * 600;
    return (1 - (v - min) / (max - min)) * 200;
  }

  posToVal(p, key) {
    const { min, max } = this.scales[key];
    if (key === 'x') return 10 ** (Math.log10(min) + (p / 600) * (Math.log10(max) - Math.log10(min)));
    return max - (p / 200) * (max - min);
  }

  hover(idx) {
    this.cursor.idx = idx;
    for (const h of this.opts.hooks.setCursor) h(this);
  }
}

function withFakeDom(fn) {
  const head = new FakeElement('head');
  const ids = new Map();
  const saved = { window: globalThis.window, document: globalThis.document };
  globalThis.window = { uPlot: FakeUPlot };
  globalThis.document = {
    head,
    createElement: (tag) => new FakeElement(tag),
    getElementById: (id) => ids.get(id) || head.children.find((c) => c.id === id) || null,
  };
  try {
    return fn();
  } finally {
    globalThis.window = saved.window;
    globalThis.document = saved.document;
    FakeUPlot.instances.length = 0;
  }
}

describe('ResponsePlot', () => {
  const grid = dsp.viewGrid('sub', 64);
  const lp = dsp.filterResponse(dsp.designCrossover({ kind: 'lp', family: 'lr', order: 4, freq: 80 }), grid);
  const hp = dsp.filterResponse(dsp.designCrossover({ kind: 'hp', family: 'lr', order: 4, freq: 20 }), grid);
  const traces = () => [
    { id: 'o0', label: 'Sub', color: '#3ccf7a', response: lp },
    { id: 'o1', label: '<b>Shakers</b>', color: '#f0b429', response: hp, bold: true },
  ];

  test('builds a log-x plot with the sub view and ±24 dB default', () =>
    withFakeDom(() => {
      const el = new FakeElement('div');
      const plot = graph.create(el, {});
      plot.setTraces(traces());
      const u = plot.plot;
      assert.deepEqual(u.opts.scales.x.range(), [5, 300]);
      assert.equal(u.opts.scales.x.distr, 3);
      assert.deepEqual(u.opts.scales.y.range(), [-24, 24]);
      assert.equal(u.opts.series.length, 3);
      assert.equal(u.opts.series[2].stroke, '#f0b429');
      assert.ok(u.opts.series[2].width > u.opts.series[1].width);
      assert.equal(u.data[0].length, 64);
      assert.deepEqual(u.data[1], Array.from(lp.magnitude_db));
      assert.deepEqual(u.opts.axes[0].values(u, [10, 1000]), ['10', '1k']);
      assert.ok(globalThis.document.head.children.some((c) => c.id === 'mdsp-graph-css'));
    }));

  test('same series → setData only; changed series → rebuild', () =>
    withFakeDom(() => {
      const plot = graph.create(new FakeElement('div'), {});
      plot.setTraces(traces());
      const first = plot.plot;
      plot.setTraces(traces());
      assert.equal(plot.plot, first);
      assert.equal(first.setDataCalls, 1);
      plot.setTraces(traces().slice(0, 1));
      assert.notEqual(plot.plot, first);
      assert.ok(first.destroyed);
    }));

  test('hover readout lists f, dB and phase for every trace, as text', () =>
    withFakeDom(() => {
      const el = new FakeElement('div');
      const seen = [];
      const plot = graph.create(el, { onCursor: (r) => seen.push(r) });
      plot.setTraces(traces());
      plot.plot.hover(20);
      const readout = el.find('graph-readout');
      const text = readout.text;
      assert.ok(text.includes(graph.fmtFreq(grid[20])), text);
      assert.ok(text.includes(graph.fmtDb(lp.magnitude_db[20])), text);
      assert.ok(text.includes(graph.fmtPhase(hp.phase_deg[20])), text);
      assert.ok(text.includes('<b>Shakers</b>')); // labels are text nodes, never parsed as HTML
      assert.ok(!text.includes(' ms'));
      assert.equal(seen.at(-1).rows.length, 2);
      plot.setMode('group_delay');
      plot.plot.hover(20);
      assert.ok(el.find('graph-readout').text.includes(graph.fmtMs(hp.group_delay_ms[20])));
      plot.plot.hover(null);
      assert.ok(el.find('graph-readout-hint'));
    }));

  test('modes, views, auto-fit and explicit ranges', () =>
    withFakeDom(() => {
      const plot = graph.create(new FakeElement('div'), { view: 'full', mode: 'phase' });
      plot.setTraces(traces());
      assert.deepEqual(plot.plot.opts.scales.x.range(), [10, 20000]);
      assert.deepEqual(plot.plot.opts.scales.y.range(), [-180, 180]);
      assert.deepEqual(plot.plot.data[1], Array.from(lp.phase_deg));
      plot.setUnwrap(true);
      assert.deepEqual(plot.plot.data[1], Array.from(lp.phase_unwrapped_deg));
      plot.setMode('magnitude');
      plot.setView([20, 200]);
      assert.deepEqual(plot.plot.scales.x, { min: 20, max: 200 });
      const fitted = plot.autoFit();
      assert.deepEqual(plot.plot.scales.y, { min: fitted[0], max: fitted[1] });
      plot.setYRange([-12, 6]);
      assert.deepEqual(plot.plot.scales.y, { min: -12, max: 6 });
      plot.resetY();
      assert.deepEqual(plot.plot.scales.y, { min: -24, max: 24 });
      assert.throws(() => plot.setMode('nope'));
      plot.destroy();
      assert.equal(plot.plot, null);
    }));

  test('handles: hit-testing and pointer → (freq, dB)', () =>
    withFakeDom(() => {
      const plot = graph.create(new FakeElement('div'), {});
      plot.setTraces(traces());
      plot.setHandles([
        { id: 'b1', freq: 42, value: -3.5, color: '#fff' },
        { id: 'b2', freq: 200, value: 6, color: '#fff', active: true },
      ]);
      const pos = plot.toPos(42, -3.5);
      assert.equal(plot.handleAt(100 + pos.left + 3, 50 + pos.top - 2).id, 'b1');
      assert.equal(plot.handleAt(100 + pos.left + 40, 50 + pos.top), null);
      const d = plot.toData(100 + pos.left, 50 + pos.top);
      assert.ok(Math.abs(d.freq - 42) < 1e-9 && Math.abs(d.value + 3.5) < 1e-9);
      assert.equal(plot.toData(50, 50), null);
      plot.setMode('phase');
      assert.equal(plot.handleAt(100 + pos.left, 50 + pos.top), null);
    }));

  test('a missing uPlot is reported clearly', () =>
    withFakeDom(() => {
      globalThis.window.uPlot = undefined;
      assert.throws(() => graph.create(new FakeElement('div'), {}), /uPlot is not loaded/);
    }));
});
