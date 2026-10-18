// app/static/js/profiles.js: a profile load asks once from the preview and sends back exactly what that question
// named (`confirmed`); when the server answers 409 because the state changed meanwhile, it asks again about the new
// amounts before anything is loaded.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { beforeEach, describe, test } from 'node:test';
import vm from 'node:vm';

import { ROOT } from './load.mjs';

let calls = [];
let replies = [];
let confirms = [];
let answers = [];
let toasts = [];
let triggers = [];

globalThis.window = {
  confirm: (message) => {
    confirms.push(message);
    return answers.length ? answers.shift() : true;
  },
  mdsp: { toast: (message, level) => toasts.push([message, level]) },
  htmx: { trigger: (el, name) => triggers.push(name) },
};
globalThis.htmx = window.htmx;
globalThis.document = { addEventListener: () => {}, querySelectorAll: () => [], body: {} };
globalThis.fetch = async (url, opts = {}) => {
  const body = JSON.parse(opts.body);
  calls.push({ url, body });
  const [status, data] = replies.shift();
  return { ok: status < 300, status, json: async () => data };
};

vm.runInThisContext(readFileSync(new URL('app/static/js/profiles.js', ROOT), 'utf8'), { filename: 'profiles.js' });

const page = () => window.profilesPage({ loadUrl: '/ui/profiles/{id}/load', serial: null, reloadEvent: 'reload' });
const RAISE_8 = { raises: [{ path: 'outputs.0.gain_db', delta_db: 8, kind: 'gain' }], fir_loads: [] };
const RAISE_38 = { raises: [{ path: 'outputs.0.gain_db', delta_db: 38, kind: 'gain' }], fir_loads: [] };
const preview = (extra = {}) => [200, { problems: [], needs_confirm: true, question: 'Load “Loud”?', confirms: RAISE_8, ...extra }];
const loaded = [200, { ok: true, message: 'Loaded “Loud” into preset 1.', level: 'info' }];
const grew = [409, { error: 'confirmation_required', detail: 'This raises Out 1 output level by 38 dB at once', confirms: RAISE_38 }];

beforeEach(() => {
  calls = [];
  replies = [];
  confirms = [];
  answers = [];
  toasts = [];
  triggers = [];
});

describe('profile load', () => {
  test('asks once and sends back what the preview named, never a blanket confirm', async () => {
    replies = [preview(), loaded];
    await page().load(1, 0, false);
    assert.equal(confirms.length, 1);
    assert.equal(calls[0].body.preview, true);
    assert.deepEqual(calls[1].body, { preset: 0, activate: false, serial: null, confirmed: RAISE_8 });
    assert.ok(!('confirm' in calls[1].body));
    assert.deepEqual(triggers, ['reload']);
  });

  test('a load that needs no confirmation sends neither confirm nor confirmed', async () => {
    replies = [preview({ needs_confirm: false, confirms: { raises: [], fir_loads: [] } }), loaded];
    await page().load(1, 0, false);
    assert.deepEqual(calls[1].body, { preset: 0, activate: false, serial: null });
  });

  test('a raise that grew after the preview asks again with the new amounts', async () => {
    replies = [preview(), grew, loaded];
    await page().load(1, 0, false);
    assert.equal(confirms.length, 2);
    assert.match(confirms[1], /by 38 dB at once\.\n\nApply it anyway\?/);
    assert.deepEqual(calls[2].body.confirmed, RAISE_38);
    assert.deepEqual(toasts, [['Loaded “Loud” into preset 1.', 'info']]);
  });

  test('declining the second question loads nothing', async () => {
    replies = [preview(), grew];
    answers = [true, false];
    await page().load(1, 0, false);
    assert.equal(calls.length, 2);
    assert.deepEqual(triggers, []);
  });

  test('a state that keeps changing ends in an error toast, not a loop', async () => {
    replies = [preview(), grew, grew, grew, grew];
    await page().load(1, 0, false);
    assert.equal(calls.length, 5);
    assert.equal(confirms.length, 4);
    assert.deepEqual(toasts, [['This raises Out 1 output level by 38 dB at once', undefined]]);
  });
});
