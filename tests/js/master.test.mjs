// app/static/js/master.js: the dB-weighted Volume slider mapping and the one confirm rule (phase 9 design §10.3).
// app/web/volume_scale.py mirrors the mapping; tests/unit/test_volume_scale.py pins it to the same numbers.
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { loadScripts } from './load.mjs';

const { volumeScale: vs } = loadScripts('app/static/js/master.js');
const fmtNum = (v, d = 1) => `${v < 0 && Number(Math.abs(v).toFixed(d)) !== 0 ? '−' : ''}${Math.abs(v).toFixed(d)}`;

describe('dB-weighted volume slider', () => {
  test('−127…−60 dB takes the first 15% of the track and −60…0 dB the rest', () => {
    assert.equal(vs.POS_MAX, 1000);
    assert.equal(vs.toPos(-127), 0);
    assert.equal(vs.toPos(-60), 150);
    assert.equal(vs.toPos(0), 1000);
    assert.equal(vs.toPos(-30), 575);
    // −3.5 dB no longer looks like the top of the track's linear 97%, but −30 dB isn't at 75% either
    assert.ok(vs.toPos(-30) < 600 && vs.toPos(-30) > 500);
  });

  test('exact at the ticks, both ways', () => {
    assert.deepEqual(vs.TICKS_DB, [-60, -40, -30, -20, -10, 0]);
    const expected = { '-60': 150, '-40': 433.333, '-30': 575, '-20': 716.667, '-10': 858.333, 0: 1000 };
    for (const tick of vs.TICKS_DB) {
      const pos = vs.toPos(tick);
      assert.ok(Math.abs(pos - expected[tick]) < 1e-3, `${tick} dB at ${pos}`);
      assert.equal(vs.fromPos(pos), tick);
      assert.equal(vs.fromPos(Math.round(pos)), tick); // a whole-number position still lands on the tick
    }
    assert.equal(vs.fromPos(0), -127);
  });

  test('monotonic, and every 0.5 dB step survives the round trip', () => {
    let last = -Infinity;
    for (let p = 0; p <= 1000; p += 0.5) {
      const db = vs.fromPos(p);
      assert.ok(db >= last, `fromPos(${p}) = ${db} < ${last}`);
      last = db;
    }
    let lastPos = -Infinity;
    for (let db = -127; db <= 0; db += 0.5) {
      const pos = vs.toPos(db);
      assert.ok(pos > lastPos, `toPos(${db}) = ${pos} isn't above ${lastPos}`);
      lastPos = pos;
      assert.equal(vs.fromPos(pos), db);
    }
  });

  test('clamps, rounds to the step and never returns −0', () => {
    assert.equal(vs.toPos(-200), 0);
    assert.equal(vs.toPos(12), 1000);
    assert.equal(vs.toPos(Number.NaN), 0);
    assert.equal(vs.fromPos(-5), -127);
    assert.equal(vs.fromPos(5000), 0);
    assert.ok(Object.is(vs.fromPos(999.99), 0));
    assert.equal(vs.fromPos(vs.toPos(-40.2)), -40); // the device's 0.5 dB grid
    assert.equal(vs.fromPos(vs.toPos(-40.25)), -40); // half steps round up
    assert.equal(vs.fromPos(vs.toPos(-12.3), -127, 0, 0.1), -12.3);
  });

  test('a range that does not cross the knee is linear', () => {
    assert.equal(vs.toPos(-25, -50, 0), 500);
    assert.equal(vs.fromPos(500, -50, 0), -25);
    assert.equal(vs.toPos(-80, -100, -60), 500);
  });
});

describe('volume confirm rule', () => {
  const rule = (from, to) => vs.confirmText({ from, to, raiseDb: 6, dropDb: 20, fmtNum });

  test('a drop over the threshold asks with the from and to values', () => {
    assert.equal(
      rule(-10, -36.5),
      "Drop the volume from −10 to −36.5 dB? That's 26.5 dB quieter in one step. The app asks before any drop of "
        + "more than 20 dB, so a slip of the slider can't silence everything without you noticing.",
    );
  });

  test('a raise over the big-step threshold asks', () => {
    assert.equal(
      rule(-30, -10),
      "Raise the volume from −30 to −10 dB? That's 20 dB louder in one step. The app asks before any raise of more "
        + 'than 6 dB.',
    );
  });

  test('the typed −10 case (P12): a 6.5 dB drop from −3.5 does not ask; at or under the thresholds nothing asks', () => {
    assert.equal(rule(-3.5, -10), null);
    assert.equal(rule(-20, -40), null); // exactly 20 dB down
    assert.equal(rule(-20, -14), null); // exactly 6 dB up
    assert.equal(rule(-20, -20.5), null);
    assert.match(rule(-20, -40.5), /^Drop the volume from −20 to −40\.5 dB\?/);
  });

  test('0 turns a check off; unknown values never ask', () => {
    assert.equal(vs.confirmText({ from: -10, to: -100, raiseDb: 6, dropDb: 0, fmtNum }), null);
    assert.equal(vs.confirmText({ from: -100, to: 0, raiseDb: 0, dropDb: 20, fmtNum }), null);
    assert.equal(rule(Number.NaN, -10), null);
  });
});
