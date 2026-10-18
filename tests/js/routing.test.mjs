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

  test('live signal is above -100 dBFS', () => {
    assert.equal(routing.LIVE_DBFS, -100);
    assert.equal(routing.isLive(-20), true);
    assert.equal(routing.isLive(-99.9), true);
    assert.equal(routing.isLive(-100), false);
    assert.equal(routing.isLive(-120), false);
    for (const v of [null, undefined, Number.NaN, -Infinity, '-20']) assert.equal(routing.isLive(v), false);
  });

  test('the last route of an output with signal asks, naming the output', () => {
    const message = routing.disableWarning({ routes: [true, false], input: 0, name: 'Out 2 (Sub)', muted: false, level: -30 });
    assert.equal(message, 'Out 2 (Sub) currently has signal; disabling its only route will silence it. Continue?');
    // a muted output with signal on its meter still asks
    assert.match(
      routing.disableWarning({ routes: [true, false], input: 0, name: 'Out 2', muted: true, level: -30 }),
      /^Out 2 currently has signal;/,
    );
  });

  test('the last route of a quiet un-muted output asks too', () => {
    const message = routing.disableWarning({ routes: [false, true], input: 1, name: 'Out 3 (Shakers)', muted: false, level: null });
    assert.equal(message, 'Out 3 (Shakers) is un-muted; disabling its only route will silence it. Continue?');
    assert.match(routing.disableWarning({ routes: [true], input: 0, name: 'Out 1', muted: false, level: -110 }), /un-muted/);
  });

  test('no question when another route remains or the output is muted and quiet', () => {
    assert.equal(routing.disableWarning({ routes: [true, true], input: 0, name: 'Out 2', muted: false, level: -10 }), null);
    assert.equal(routing.disableWarning({ routes: [true, false], input: 0, name: 'Out 2', muted: true, level: null }), null);
    assert.equal(routing.disableWarning({ routes: [true, false], input: 0, name: 'Out 2', muted: true, level: -120 }), null);
  });
});
