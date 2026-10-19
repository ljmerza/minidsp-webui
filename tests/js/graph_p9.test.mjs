// app/static/js/graph.js, phase 9 (design §12.3, §16, P09): legend metadata, rounding-note severity, the placed cursor
// (click / tap / keyboard) and its readout, against a fake uPlot and a minimal fake DOM.
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { loadScripts } from './load.mjs';

const { graph, dsp } = loadScripts('app/static/js/dsp.js', 'app/static/js/graph.js');

const NOTE = (label, db) =>
  `${label}: rounding note: the device stores filters as float32 numbers, which moves the response by up to ${db} dB`;

describe('legend metadata', () => {
  test('one row per visible trace, with its line style', () => {
    const items = graph.legendItems([
      { id: 'o0', label: 'Shakers', color: '#f0b429' },
      { id: 'sum', label: 'Sum', color: '#e3e7ec', bold: true },
      { id: 'peq', label: 'Output PEQ', color: '#22c3c3', dash: [6, 4] },
      { id: 'part', label: 'Hidden part', color: '#111111', visible: false },
      { id: 'x', label: 'No legend', color: '#222222', legend: false },
    ]);
    assert.deepEqual(items, [
      { id: 'o0', label: 'Shakers', color: '#f0b429', style: 'solid' },
      { id: 'sum', label: 'Sum', color: '#e3e7ec', style: 'bold' },
      { id: 'peq', label: 'Output PEQ', color: '#22c3c3', style: 'dashed' },
    ]);
  });

  test('segments sharing a label and colour show once; a missing colour falls back', () => {
    const items = graph.legendItems([
      { id: 'dphi0', label: 'Δφ Shakers − Sub', color: '#f0b429' },
      { id: 'dphi1', label: 'Δφ Shakers − Sub', color: '#f0b429' },
      { id: 'a', label: 'Plain' },
    ]);
    assert.deepEqual(items.map((i) => i.id), ['dphi0', 'a']);
    assert.equal(items[1].color, graph.COLORS.fallback);
    assert.deepEqual(graph.legendItems([]), []);
  });
});

describe('rounding-note severity', () => {
  test('parses the server and dsp.js wording', () => {
    assert.deepEqual(graph.parseRoundingNote(NOTE('output 1', '2.29')), { label: 'output 1', db: 2.29 });
    assert.deepEqual(graph.parseRoundingNote(NOTE('output 1, output 2', '0.20')), { label: 'output 1, output 2', db: 0.2 });
    assert.equal(graph.parseRoundingNote('Input 1 is not routed to Sub, so it is plotted as silent'), null);
    assert.equal(graph.parseRoundingNote(undefined), null);
  });

  test('the 2.29 dB note gets the strong wording; under 1 dB is harmless', () => {
    const strong = graph.classifyNotes([NOTE('output 1', '2.29')]);
    assert.equal(strong.strong, true);
    assert.equal(strong.notes[0].text, "output 1: this filter's real curve is noticeably off (2.3 dB)");
    assert.equal(strong.summary, "1 rounding note: this filter's real curve is noticeably off (2.3 dB)");

    const mild = graph.classifyNotes([NOTE('output 1', '0.20')]);
    assert.equal(mild.strong, false);
    assert.equal(mild.summary, '1 rounding note (0.20 dB, harmless)');
    assert.equal(mild.notes[0].text, 'output 1: off by up to 0.20 dB, harmless');
  });

  test('the threshold is 1 dB, inclusive', () => {
    assert.equal(graph.ROUNDING_STRONG_DB, 1);
    assert.equal(graph.classifyNotes([NOTE('a', '1.00')]).strong, true);
    assert.equal(graph.classifyNotes([NOTE('a', '0.99')]).strong, false);
    assert.equal(graph.classifyNotes([NOTE('a', '0.99')], 0.5).strong, true);
  });

  test('real warnings stay as they are; several notes fold into one line', () => {
    const silent = 'Input 1 is not routed to Sub, so it is plotted as silent (this graph feeds Input 1)';
    const out = graph.classifyNotes([NOTE('output 1', '0.20'), silent, NOTE('output 2', '2.29'), NOTE('sum', '0.15')]);
    assert.deepEqual(out.others, [silent]);
    assert.equal(out.notes.length, 3);
    assert.equal(out.summary, "3 rounding notes: one filter's real curve is noticeably off (2.3 dB)");
    const two = graph.classifyNotes([NOTE('output 1', '2.80'), NOTE('sum', '1.5')]);
    assert.equal(two.summary, "2 rounding notes: 2 filters' real curves are noticeably off (up to 2.8 dB)");
    const mild = graph.classifyNotes([NOTE('output 1', '0.20'), NOTE('sum', '0.15')]);
    assert.equal(mild.summary, '2 rounding notes (up to 0.20 dB, harmless)');
    const none = graph.classifyNotes([silent]);
    assert.deepEqual(none, { others: [silent], notes: [], strong: false, summary: '' });
    assert.deepEqual(graph.classifyNotes(undefined).notes, []);
  });
});

describe('cursor helpers', () => {
  const grid = dsp.viewGrid('sub', 128);

  test('a key step moves about 1/12 octave, a big step half an octave, inside the bounds', () => {
    const mid = 60;
    const next = graph.stepIndex(grid, mid, 1);
    const ratio = grid[next] / grid[mid];
    assert.ok(next > mid && ratio > 2 ** (1 / 24) && ratio < 2 ** (1 / 8), `ratio ${ratio}`);
    const big = graph.stepIndex(grid, mid, -1, { big: true });
    assert.ok(Math.abs(Math.log2(grid[mid] / grid[big]) - 0.5) < 0.05);
    assert.equal(graph.stepIndex(grid, grid.length - 1, 1), grid.length - 1);
    assert.equal(graph.stepIndex(grid, 0, -1), 0);
    assert.equal(graph.stepIndex(grid, 10, -1, { lo: 10, hi: 20 }), 10);
    assert.equal(graph.stepIndex([10, 1000], 0, 1), 1); // always at least one point
    assert.equal(graph.stepIndex([], 0, 1), null);
  });

  test('the one-line reading of a readout', () => {
    const readout = {
      freq: 50,
      rows: [
        { label: 'Shakers', db: -0.06, phase: 12, group_delay: 1.5 },
        { label: 'Sub', db: -3.21, phase: -90, group_delay: 2 },
      ],
    };
    assert.equal(graph.readingText(readout), '50.0 Hz: Shakers −0.1 dB, Sub −3.2 dB');
    assert.equal(graph.readingText(readout, 'phase'), '50.0 Hz: Shakers 12°, Sub −90°');
    assert.equal(graph.readingText(readout, 'group_delay'), '50.0 Hz: Shakers 1.50 ms, Sub 2.00 ms');
    assert.equal(graph.readingText(null), '');
  });
});

// --- ResponsePlot against fakes ---------------------------------------------------------------------------------

class FakeElement {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.nodeType = 1;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.style = {};
    this.className = '';
    this.textContent = '';
    this.hidden = false;
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

  getAttribute(name) {
    return this.attributes[name] ?? null;
  }

  addEventListener(type, fn) {
    (this.listeners[type] ||= []).push(fn);
  }

  dispatch(type, init = {}) {
    let prevented = false;
    const event = { type, ...init, preventDefault: () => (prevented = true) };
    for (const fn of this.listeners[type] || []) fn(event);
    return prevented;
  }

  get text() {
    return this.textContent + this.children.map((c) => c.text).join(' ');
  }

  find(className) {
    if (this.className.split(' ').includes(className)) return this;
    for (const c of this.children) {
      const hit = c.find(className);
      if (hit) return hit;
    }
    return null;
  }

  findAll(className, out = []) {
    if (this.className.split(' ').includes(className)) out.push(this);
    for (const c of this.children) c.findAll(className, out);
    return out;
  }
}

class FakeUPlot {
  static pxRatio = 1;

  constructor(opts, data) {
    this.opts = opts;
    this.data = data;
    this.width = opts.width;
    this.cursor = { idx: null };
    this.bbox = { left: 0, top: 0, width: 600, height: 200 };
    this.over = { getBoundingClientRect: () => ({ left: 100, top: 50, width: 600, height: 200 }) };
    this.drawn = [];
    const self = this;
    this.ctx = {
      save() {},
      restore() {},
      beginPath() {},
      moveTo(x) {
        self.drawn.push(['moveTo', x]);
      },
      lineTo() {},
      stroke() {},
      arc(x, y) {
        self.drawn.push(['arc', x, y]);
      },
      fill() {},
      setLineDash() {},
    };
    const [xmin, xmax] = opts.scales.x.range();
    const [ymin, ymax] = opts.scales.y.range();
    this.scales = { x: { min: xmin, max: xmax }, y: { min: ymin, max: ymax } };
  }

  setData(data) {
    this.data = data;
  }

  setScale(key, { min, max }) {
    this.scales[key] = { min, max };
  }

  setSize() {}

  redraw() {
    this.drawn = [];
    for (const h of this.opts.hooks.draw) h(this);
  }

  destroy() {}

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
  const saved = { window: globalThis.window, document: globalThis.document };
  globalThis.window = { uPlot: FakeUPlot };
  globalThis.document = {
    head,
    createElement: (tag) => new FakeElement(tag),
    getElementById: (id) => head.children.find((c) => c.id === id) || null,
  };
  try {
    return fn();
  } finally {
    globalThis.window = saved.window;
    globalThis.document = saved.document;
  }
}

describe('ResponsePlot: legend and placed cursor', () => {
  const grid = dsp.viewGrid('sub', 96);
  const lp = dsp.filterResponse(dsp.designCrossover({ kind: 'lp', family: 'lr', order: 4, freq: 80 }), grid);
  const hp = dsp.filterResponse(dsp.designCrossover({ kind: 'hp', family: 'lr', order: 4, freq: 20 }), grid);
  const traces = () => [
    { id: 'o0', label: 'Shakers', color: '#f0b429', response: hp },
    { id: 'o1', label: 'Sub', color: '#3ccf7a', response: lp },
    { id: 'sum', label: 'Sum', color: '#e3e7ec', response: lp, bold: true },
  ];
  const canvasOf = (el) => el.find('graph-canvas');

  test('the legend lists the visible traces as text, with their line styles', () =>
    withFakeDom(() => {
      const el = new FakeElement('div');
      const plot = graph.create(el, {});
      plot.setTraces(traces());
      const legend = el.find('graph-legend');
      assert.equal(legend.tagName, 'UL');
      assert.equal(legend.hidden, false);
      assert.deepEqual(legend.findAll('graph-legend-label').map((l) => l.textContent), ['Shakers', 'Sub', 'Sum']);
      assert.ok(legend.find('graph-legend-line').className.includes('is-solid'));
      assert.ok(legend.findAll('graph-legend-line')[2].className.includes('is-bold'));
      plot.setTraces(traces().map((t) => (t.id === 'sum' ? { ...t, visible: false } : t))); // "Show the sum" off
      assert.deepEqual(legend.findAll('graph-legend-label').map((l) => l.textContent), ['Shakers', 'Sub']);
      const none = new FakeElement('div');
      graph.create(none, { legend: false }).setTraces(traces());
      assert.equal(none.find('graph-legend'), null);
    }));

  test('the plot is a keyboard slider; the readout hint says tap or click', () =>
    withFakeDom(() => {
      const el = new FakeElement('div');
      const plot = graph.create(el, { label: 'Calculated response' });
      const c = canvasOf(el);
      assert.equal(c.getAttribute('tabindex'), '0');
      assert.equal(c.getAttribute('role'), 'slider');
      assert.match(c.getAttribute('aria-label'), /^Calculated response: cursor/);
      assert.equal(c.getAttribute('aria-valuemin'), '5');
      assert.equal(c.getAttribute('aria-valuemax'), '300');
      assert.equal(c.getAttribute('aria-valuenow'), '5');
      assert.equal(c.getAttribute('aria-valuetext'), 'no cursor placed');
      assert.equal(el.find('graph-readout-hint').textContent, 'Tap or click the graph for values');
      plot.destroy();
    }));

  test('a tap places the cursor: the readout keeps its values after the pointer leaves', () =>
    withFakeDom(() => {
      const el = new FakeElement('div');
      const seen = [];
      const plot = graph.create(el, { onCursor: (r) => seen.push(r) });
      plot.setTraces(traces());
      // The middle of the plotting area (over at left 100, width 600): about 38.7 Hz on 5–300 Hz.
      canvasOf(el).dispatch('click', { clientX: 400, clientY: 120 });
      const idx = plot.pinnedIndex();
      assert.ok(idx !== null && Math.abs(grid[idx] - Math.sqrt(5 * 300)) / grid[idx] < 0.05, `freq ${grid[idx]}`);
      let text = el.find('graph-readout').text;
      assert.ok(text.includes(graph.fmtFreq(grid[idx])), text);
      assert.ok(text.includes(graph.fmtDb(hp.magnitude_db[idx])), text);
      plot.plot.hover(10); // hovering shows the hovered point ...
      assert.ok(el.find('graph-readout').text.includes(graph.fmtFreq(grid[10])));
      plot.plot.hover(null); // ... and leaving falls back to the placed cursor, not the hint
      text = el.find('graph-readout').text;
      assert.ok(text.includes(graph.fmtFreq(grid[idx])), text);
      assert.equal(el.find('graph-readout-hint'), null);
      assert.equal(seen.at(-1).freq, grid[idx]);
      // It's drawn: a vertical line at its x and one dot per visible trace.
      plot.plot.redraw();
      assert.equal(plot.plot.drawn.filter((d) => d[0] === 'arc').length, 3);
      // A click outside the plotting area changes nothing.
      canvasOf(el).dispatch('click', { clientX: 20, clientY: 20 });
      assert.equal(plot.pinnedIndex(), idx);
    }));

  test('arrow keys move it, Home/End jump, Esc removes it; the spoken value is the reading', () =>
    withFakeDom(() => {
      const el = new FakeElement('div');
      const plot = graph.create(el, {});
      plot.setTraces(traces());
      const c = canvasOf(el);
      assert.equal(c.dispatch('keydown', { key: 'ArrowRight' }), true); // first press: the middle of the view
      const first = plot.pinnedIndex();
      assert.ok(Math.abs(Math.log(grid[first] / Math.sqrt(5 * 300))) < 0.05);
      c.dispatch('keydown', { key: 'ArrowRight' });
      assert.ok(plot.pinnedIndex() > first);
      c.dispatch('keydown', { key: 'ArrowLeft', shiftKey: true });
      assert.ok(plot.pinnedIndex() < first);
      c.dispatch('keydown', { key: 'End' });
      assert.equal(plot.pinnedIndex(), grid.length - 1);
      assert.equal(c.getAttribute('aria-valuenow'), String(Math.round(grid.at(-1))));
      c.dispatch('keydown', { key: 'Home' });
      assert.equal(plot.pinnedIndex(), 0);
      assert.equal(c.dispatch('keydown', { key: 'a' }), false); // other keys are left alone
      assert.equal(c.dispatch('keydown', { key: 'Escape' }), true);
      assert.equal(plot.pinnedIndex(), null);
      assert.equal(c.dispatch('keydown', { key: 'Escape' }), false); // nothing to remove: Esc goes on (dialogs)
      plot.destroy();
    }));

  test('the spoken value is throttled but the newest one always lands', async () => {
    const saved = { window: globalThis.window, document: globalThis.document };
    const head = new FakeElement('head');
    globalThis.window = { uPlot: FakeUPlot };
    globalThis.document = { head, createElement: (t) => new FakeElement(t), getElementById: () => null };
    try {
      const el = new FakeElement('div');
      const plot = graph.create(el, {});
      plot.setTraces(traces());
      const c = canvasOf(el);
      c.dispatch('keydown', { key: 'Home' });
      const firstText = c.getAttribute('aria-valuetext');
      assert.equal(firstText, graph.readingText(graph.readoutRows(plot.traces, plot.grid, 0)));
      c.dispatch('keydown', { key: 'ArrowRight' });
      c.dispatch('keydown', { key: 'ArrowRight' });
      assert.equal(c.getAttribute('aria-valuetext'), firstText); // held back for now
      await new Promise((resolve) => setTimeout(resolve, 350));
      const idx = plot.pinnedIndex();
      assert.equal(c.getAttribute('aria-valuetext'), graph.readingText(graph.readoutRows(plot.traces, plot.grid, idx)));
      plot.destroy();
    } finally {
      globalThis.window = saved.window;
      globalThis.document = saved.document;
    }
  });

  test('the placed cursor survives a rebuild, follows its frequency onto a new grid and hides outside the view', () =>
    withFakeDom(() => {
      const el = new FakeElement('div');
      const plot = graph.create(el, {});
      plot.setTraces(traces());
      plot.pin(40);
      const before = plot.grid[plot.pinnedIndex()];
      plot.setMode('phase');
      assert.equal(plot.grid[plot.pinnedIndex()], before);
      const fine = dsp.viewGrid('sub', 300);
      plot.setTraces([{ id: 'o0', label: 'Shakers', color: '#f0b429', response: dsp.filterResponse([], fine) }]);
      assert.ok(Math.abs(plot.grid[plot.pinnedIndex()] / 40 - 1) < 0.02);
      plot.setView([100, 300]);
      assert.equal(plot.pinnedIndex(), null);
      assert.equal(canvasOf(el).getAttribute('aria-valuemin'), '100');
    }));
});
