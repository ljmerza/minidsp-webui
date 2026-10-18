// app/static/js/routing.js: the routing page's "this would silence an output" guard (pure helpers).
import assert from 'node:assert/strict';
import { describe, test } from 'node:test';

import { loadScripts } from './load.mjs';

const { routing } = loadScripts('app/static/js/routing.js');

describe('routing guard', () => {
  test('an output keeps sound while another input still routes to it', () => {
    assert.equal(routing.wouldSilence([true, true], 0), false);
    assert.equal(routing.wouldSilence([true, false], 0), true);
    assert.equal(routing.wouldSilence([false, true], 1), true);
    assert.equal(routing.wouldSilence([true], 0), true);
    assert.equal(routing.wouldSilence([], 0), true);
  });

  test('live signal is above the -90 dBFS no-signal floor', () => {
    assert.equal(routing.LIVE_DBFS, -90);
    assert.equal(routing.isLive(-20), true);
    assert.equal(routing.isLive(-60), true); // the mock's flat reading
    assert.equal(routing.isLive(-89.9), true);
    assert.equal(routing.isLive(-90), false);
    assert.equal(routing.isLive(-95), false);
    assert.equal(routing.isLive(-120), false);
    for (const v of [null, undefined, Number.NaN, -Infinity, '-20']) assert.equal(routing.isLive(v), false);
  });

  test('the last route of an output with signal asks, naming the route and the reading', () => {
    const message = routing.disableWarning({ routes: [true, false], input: 0, name: 'Sub', inputName: 'In 1', muted: false, level: -30 });
    assert.equal(
      message,
      'Turn off In 1 → Sub? Sub has signal right now (−30.0 dBFS) and will go silent: no other input is routed to it.',
    );
    // a muted output with signal on its meter still asks
    assert.match(
      routing.disableWarning({ routes: [true, false], input: 0, name: 'Out 2', inputName: 'In 1', muted: true, level: -30 }),
      /^Turn off In 1 → Out 2\? Out 2 has signal right now/,
    );
  });

  test('the last route of a quiet un-muted output asks too; below the floor is quiet', () => {
    const message = routing.disableWarning({ routes: [false, true], input: 1, name: 'Shakers', inputName: 'In 2', muted: false, level: null });
    assert.equal(message, "Turn off In 2 → Shakers? Shakers isn't muted and will go silent: no other input is routed to it.");
    assert.match(routing.disableWarning({ routes: [true], input: 0, name: 'Out 1', muted: false, level: -95 }), /^Turn off In 1 → Out 1\? Out 1 isn't muted/);
  });

  test('no question when another route remains or the output is muted and quiet', () => {
    assert.equal(routing.disableWarning({ routes: [true, true], input: 0, name: 'Out 2', muted: false, level: -10 }), null);
    assert.equal(routing.disableWarning({ routes: [true, false], input: 0, name: 'Out 2', muted: true, level: null }), null);
    assert.equal(routing.disableWarning({ routes: [true, false], input: 0, name: 'Out 2', muted: true, level: -95 }), null);
  });
});

describe('mono sum plan', () => {
  const paths = [
    { enabled: 'inputs.0.routing.1.enabled', gain: 'inputs.0.routing.1.gain_db' },
    { enabled: 'inputs.1.routing.1.enabled', gain: 'inputs.1.routing.1.gain_db' },
  ];

  test('previews each route that changes and sends every route on at the gain', () => {
    const plan = routing.monoPlan({
      output: 'Sub',
      gain: -6,
      paths,
      routes: [
        { name: 'In 1', enabled: false, gain: 0 },
        { name: 'In 2', enabled: true, gain: 0 },
      ],
    });
    assert.equal(plan.text, "Sub: In 1 off → on at −6.0 dB, In 2 0.0 dB → −6.0 dB. Other outputs don't change.");
    assert.deepEqual(plan.values, {
      'inputs.0.routing.1.enabled': true,
      'inputs.0.routing.1.gain_db': -6,
      'inputs.1.routing.1.enabled': true,
      'inputs.1.routing.1.gain_db': -6,
    });
    assert.equal(plan.changes.length, 2);
  });

  test('nothing to change when every input already plays there at the gain', () => {
    const plan = routing.monoPlan({
      output: 'Sub',
      gain: -6,
      paths,
      routes: [
        { name: 'In 1', enabled: true, gain: -6 },
        { name: 'In 2', enabled: true, gain: -6 },
      ],
    });
    assert.deepEqual(plan.changes, []);
    assert.equal(plan.text, 'Every input already goes to Sub at −6.0 dB. Nothing to change.');
  });
});
