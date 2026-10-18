// Routing guard for the routing page (UI only: the edit API and HA/MQTT clients are not affected). Routing enables
// can't be read back from the device, so a route switched off by accident silences an output with nothing to show for
// it. Before a route is turned off, params.js asks for confirmation when that leaves the output without any enabled
// route while the output is un-muted or its meter shows signal above the no-signal floor (phase9 design §5.3, P21:
// the mock's flat -60 dBFS is "signal" by that rule too, so the question names the reading). Pure functions, so node
// can test them.
(() => {
  'use strict';

  // Keep in sync with app/web/params.py (ROUTE_LIVE_DBFS, would_silence_output).
  const LIVE_DBFS = -90;

  // routes: whether each input's route to the output is enabled right now (before the change).
  function wouldSilence(routes, input) {
    return !routes.some((on, i) => i !== input && !!on);
  }

  function isLive(level) {
    return typeof level === 'number' && Number.isFinite(level) && level > LIVE_DBFS;
  }

  const dbfs = (level) => `${level < 0 ? '−' : ''}${Math.abs(level).toFixed(1)} dBFS`;

  // The confirmation text for turning off route `input` -> the output, or null when no question is needed.
  // name: the output's name ("Sub"); inputName: the input's ("In 1"); muted: the output's desired mute; level: its
  // latest meter reading in dBFS (null if unknown).
  function disableWarning({ routes, input, name, inputName, muted, level }) {
    if (!wouldSilence(routes, input)) return null;
    const ask = `Turn off ${inputName || `In ${input + 1}`} → ${name}?`;
    const why = 'no other input is routed to it';
    if (isLive(level)) return `${ask} ${name} has signal right now (${dbfs(level)}) and will go silent: ${why}.`;
    if (!muted) return `${ask} ${name} isn't muted and will go silent: ${why}.`;
    return null;
  }

  // Mono sum (phase9 design §10.1, P22): what sending every input to output `o` at `gain` changes. routes: per input
  // { name, enabled, gain }. Returns { values, changes: [text], text } where values is the batch to send.
  function monoPlan({ output, routes, gain, paths }) {
    const fmt = (v) => `${v < 0 ? '−' : v > 0 ? '+' : ''}${Math.abs(v).toFixed(1)} dB`;
    const values = {};
    const changes = [];
    routes.forEach((r, i) => {
      values[paths[i].enabled] = true;
      values[paths[i].gain] = gain;
      if (!r.enabled) changes.push(`${r.name} off → on at ${fmt(gain)}`);
      else if (Math.abs(r.gain - gain) > 1e-9) changes.push(`${r.name} ${fmt(r.gain)} → ${fmt(gain)}`);
    });
    const text = changes.length
      ? `${output}: ${changes.join(', ')}. Other outputs don't change.`
      : `Every input already goes to ${output} at ${fmt(gain)}. Nothing to change.`;
    return { values, changes, text };
  }

  // "#in1-out2" (the flow diagram's route links, phase9 design §5.4): the id of a route cell's checkbox, or null.
  function cellFromHash(hash) {
    const m = /^#in(\d+)-out(\d+)$/.exec(hash || '');
    return m ? `in${Number(m[1])}-out${Number(m[2])}` : null;
  }

  window.mdsp = Object.assign(window.mdsp || {}, {
    routing: { LIVE_DBFS, wouldSilence, isLive, disableWarning, monoPlan, cellFromHash },
  });

  // Arriving from a route link focuses that cell (the browser only scrolls to it).
  if (typeof document !== 'undefined' && typeof location !== 'undefined') {
    const focusCell = () => {
      const id = cellFromHash(location.hash);
      const cell = id && document.getElementById(id);
      if (cell) cell.focus({ preventScroll: false });
    };
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', focusCell);
    else focusCell();
    window.addEventListener('hashchange', focusCell);
  }
})();
