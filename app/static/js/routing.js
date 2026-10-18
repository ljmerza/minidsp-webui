// Routing guard for the routing page (UI only: the edit API and HA/MQTT clients are not affected). Routing enables
// can't be read back from the device, so a route switched off by accident silences an output with nothing to show for
// it. Before a route is turned off, params.js asks for confirmation when that leaves the output without any enabled
// route while the output is un-muted or its meter shows signal. Pure functions, so node can test them.
(() => {
  'use strict';

  // Keep in sync with app/web/params.py (ROUTE_LIVE_DBFS, would_silence_output).
  const LIVE_DBFS = -100;

  // routes: whether each input's route to the output is enabled right now (before the change).
  function wouldSilence(routes, input) {
    return !routes.some((on, i) => i !== input && !!on);
  }

  function isLive(level) {
    return typeof level === 'number' && Number.isFinite(level) && level > LIVE_DBFS;
  }

  // The confirmation text for turning off route `input` -> the output, or null when no question is needed.
  // name: "Out 2 (Sub)"; muted: the output's desired mute; level: its latest meter reading in dBFS (null if unknown).
  function disableWarning({ routes, input, name, muted, level }) {
    if (!wouldSilence(routes, input)) return null;
    if (isLive(level)) return `${name} currently has signal; disabling its only route will silence it. Continue?`;
    if (!muted) return `${name} is un-muted; disabling its only route will silence it. Continue?`;
    return null;
  }

  window.mdsp = Object.assign(window.mdsp || {}, { routing: { LIVE_DBFS, wouldSilence, isLive, disableWarning } });
})();
