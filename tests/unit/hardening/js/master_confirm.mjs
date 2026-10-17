// Drives app/static/js/master.js's htmx:confirm / htmx:configRequest handlers with a stub DOM.
// Usage: node master_confirm.mjs <path to master.js>; prints one JSON line per case.
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const handlers = {};
const dialogs = [];
let answer = true;
const context = {
  document: { addEventListener: (type, fn) => (handlers[type] ||= []).push(fn), getElementById: () => null },
  window: {
    addEventListener() {},
    confirm: (text) => { dialogs.push(text); return answer; },
    mdsp: { fmtNum: (v) => v.toFixed(1), fmtDb: (v) => `${v.toFixed(1)} dB` },
  },
  performance: { now: () => 0 },
  setTimeout,
  htmx: { trigger() {} },
};
vm.createContext(context);
vm.runInContext(readFileSync(process.argv[2], 'utf8'), context);

function element(value, current) {
  return {
    value: String(value),
    dataset: { current: String(current), confirmRaiseDb: '6', confirmDropDb: '20' },
    matches: (sel) => sel === '[data-volume-input]',
    closest: () => null,
  };
}

function run(name, value, current, accept) {
  answer = accept;
  dialogs.length = 0;
  const el = element(value, current);
  let prevented = false;
  let issued = false;
  const confirmEvent = { detail: { elt: el, issueRequest: () => { issued = true; } }, preventDefault: () => { prevented = true; } };
  for (const fn of handlers['htmx:confirm']) fn(confirmEvent);
  const parameters = {};
  for (const fn of handlers['htmx:configRequest']) fn({ detail: { elt: el, parameters } });
  for (const fn of handlers['htmx:afterSwap']) fn({ detail: { target: { id: 'app-header' } } }); // reset lastSent
  console.log(JSON.stringify({ name, prevented, issued, dialog: dialogs[0] || null, value: el.value, confirm: parameters.confirm || null }));
}

run('big drop accepted', -120, -20, true);
run('big drop cancelled', -120, -20, false);
run('drop at the threshold', -40, -20, true);
run('big raise', -10, -20, true);
run('small step', -20.5, -20, true);
