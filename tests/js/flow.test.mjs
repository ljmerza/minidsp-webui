// app/static/js/flow.js: the signal-flow diagram's pure helpers (roving focus, meter readings).
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { loadScripts } from './load.mjs';

const { flow, routing } = loadScripts('app/static/js/flow.js', 'app/static/js/routing.js');

describe('flow diagram', () => {
  test('arrow keys move between a row\'s blocks and wrap; Home and End jump', () => {
    assert.equal(flow.nextIndex('ArrowRight', 0, 6), 1);
    assert.equal(flow.nextIndex('ArrowRight', 5, 6), 0);
    assert.equal(flow.nextIndex('ArrowLeft', 0, 6), 5);
    assert.equal(flow.nextIndex('ArrowLeft', 3, 6), 2);
    assert.equal(flow.nextIndex('Home', 4, 6), 0);
    assert.equal(flow.nextIndex('End', 1, 6), 5);
    assert.equal(flow.nextIndex('Tab', 1, 6), null);
    assert.equal(flow.nextIndex('ArrowRight', 0, 0), null);
  });

  test('a meter reads "no signal" below the floor and a dash without data', () => {
    assert.equal(flow.meterText(-95, false, -90), 'no signal');
    assert.equal(flow.meterText(-18, false, -90), '-18.0 dBFS'); // no app.js here: the plain fallback
    assert.equal(flow.meterText(-18, true, -90), '—');
    assert.equal(flow.meterText(null, false, -90), '—');
    assert.equal(flow.meterText(Number.NaN, false, -90), '—');
  });

  test('route links name a routing cell', () => {
    assert.equal(routing.cellFromHash('#in1-out2'), 'in1-out2');
    assert.equal(routing.cellFromHash('#in01-out4'), 'in1-out4');
    for (const hash of ['', '#', '#mono-title', '#in1-out', 'in1-out2', '#in1-out2"><x']) {
      assert.equal(routing.cellFromHash(hash), null);
    }
  });
});
