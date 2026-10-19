// Master volume: an Alpine component keeps the slider and the numeric field in step; htmx sends the value after
// an 80 ms pause (hx-trigger "input changed delay:80ms"). Header swaps are held back while the user is still
// adjusting the volume, so a refresh never yanks the slider out from under a drag or drops a pending change.
//
// The slider is dB-weighted (phase 9 design §10.3, P12): −127…−60 dB uses the first 15% of the track and −60…0 dB the
// rest, so the usable range isn't a thin strip at the top. The <input type=range> stays native; its value is a track
// position (0…1000) and `aria-valuetext` says the dB. The pure mapping below is mirrored by app/web/volume_scale.py
// (the server renders the first position) and tested by tests/js/master.test.mjs.
(() => {
  'use strict';

  const POS_MAX = 1000; // the range input's max (min is 0)
  const KNEE_DB = -60; // the bottom 15% of the track ends here
  const KNEE_AT = 0.15;
  const TICKS_DB = [-60, -40, -30, -20, -10, 0];

  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

  // Where `db` sits on the track (0…POS_MAX, unrounded). A range that doesn't cross the knee is linear.
  function toPos(db, min = -127, max = 0) {
    if (!(max > min) || !Number.isFinite(db)) return 0;
    const v = clamp(db, min, max);
    if (!(min < KNEE_DB && KNEE_DB < max)) return (POS_MAX * (v - min)) / (max - min);
    if (v <= KNEE_DB) return POS_MAX * KNEE_AT * ((v - min) / (KNEE_DB - min));
    return POS_MAX * (KNEE_AT + (1 - KNEE_AT) * ((v - KNEE_DB) / (max - KNEE_DB)));
  }

  // The dB of a track position, on the device's step grid (0.5 dB). Half steps round up, like Python's floor(x + 0.5).
  function fromPos(pos, min = -127, max = 0, step = 0.5) {
    if (!(max > min)) return min;
    const p = clamp(Number(pos) || 0, 0, POS_MAX) / POS_MAX;
    let db;
    if (!(min < KNEE_DB && KNEE_DB < max)) db = min + p * (max - min);
    else if (p <= KNEE_AT) db = min + (p / KNEE_AT) * (KNEE_DB - min);
    else db = KNEE_DB + ((p - KNEE_AT) / (1 - KNEE_AT)) * (max - KNEE_DB);
    if (step > 0) db = Math.floor(db / step + 0.5) * step;
    db = clamp(db, min, max);
    return db === 0 ? 0 : db; // no −0
  }

  // The one confirm rule (design §10.3) for the slider, the typed value and anything else that sets the Volume in one
  // go: raising it by more than `raiseDb` asks, and so does dropping it by more than `dropDb` (0 turns a check off).
  // Returns the question to ask, or null. `fmtNum` is app.js's formatter (U+2212 minus); whole numbers lose the ".0"
  // ("Drop the volume from −10 to −36.5 dB?").
  function confirmText({ from, to, raiseDb, dropDb, fmtNum }) {
    if (!Number.isFinite(from) || !Number.isFinite(to)) return null;
    const num = (v) => fmtNum(v, Math.abs(Math.round(v * 10) % 10) === 0 ? 0 : 1);
    const by = num(Math.abs(to - from));
    if (raiseDb > 0 && to - from > raiseDb) {
      return `Raise the volume from ${num(from)} to ${num(to)} dB? That's ${by} dB louder in one step. `
        + `The app asks before any raise of more than ${num(raiseDb)} dB.`;
    }
    if (dropDb > 0 && from - to > dropDb) {
      return `Drop the volume from ${num(from)} to ${num(to)} dB? That's ${by} dB quieter in one step. `
        + `The app asks before any drop of more than ${num(dropDb)} dB, so a slip of the slider can't silence `
        + 'everything without you noticing.';
    }
    return null;
  }

  window.mdsp = Object.assign(window.mdsp || {}, {
    volumeScale: { POS_MAX, KNEE_DB, KNEE_AT, TICKS_DB, toPos, fromPos, confirmText },
  });
  if (typeof document === 'undefined') return; // node tests load only the pure part

  const IDLE_MS = 120; // > the 80 ms debounce, so a pending send is never discarded by a swap
  const STUCK_REQUEST_MS = 10000;

  const state = {
    dragging: false,
    lastInput: 0,
    inflight: 0,
    lastRequestAt: 0,
    refreshPending: false,
    lastSent: null, // last volume sent (or confirmed) since the header was last rendered
    confirmed: null, // element whose next request the user just confirmed
  };

  const isVolume = (el) => !!(el && el.matches && el.matches('[data-volume-input]'));

  function busy(ownRequests = 0) {
    const now = performance.now();
    const inflight = state.inflight - ownRequests > 0 && now - state.lastRequestAt < STUCK_REQUEST_MS;
    return state.dragging || inflight || now - state.lastInput < IDLE_MS;
  }

  function scheduleRefresh() {
    if (state.refreshPending) return;
    state.refreshPending = true;
    const tick = () => {
      if (busy()) {
        setTimeout(tick, 100);
        return;
      }
      state.refreshPending = false;
      const header = document.getElementById('app-header');
      if (header) htmx.trigger(header, 'refresh');
    };
    setTimeout(tick, 100);
  }

  document.addEventListener('pointerdown', (e) => { if (isVolume(e.target)) state.dragging = true; }, true);
  const release = () => { state.dragging = false; };
  window.addEventListener('pointerup', release, true);
  window.addEventListener('pointercancel', release, true);
  window.addEventListener('blur', release);
  document.addEventListener('input', (e) => { if (isVolume(e.target)) state.lastInput = performance.now(); }, true);

  document.addEventListener('htmx:beforeRequest', (e) => {
    if (!isVolume(e.detail.elt)) return;
    state.inflight += 1;
    state.lastRequestAt = performance.now();
  });
  document.addEventListener('htmx:afterRequest', (e) => {
    if (isVolume(e.detail.elt)) state.inflight = Math.max(0, state.inflight - 1);
  });

  document.addEventListener('htmx:beforeSwap', (e) => {
    const target = e.detail.target;
    if (!target || target.id !== 'app-header' || !e.detail.shouldSwap) return;
    if (busy(isVolume(e.detail.elt) ? 1 : 0)) {
      e.detail.shouldSwap = false;
      scheduleRefresh();
    }
  });

  document.addEventListener('htmx:afterSwap', (e) => {
    if (e.detail.target && e.detail.target.id === 'app-header') state.lastSent = null;
  });

  // The slider's value is a track position (data-volume-scale="min max step"); the number field's is the dB itself.
  function scaleOf(el) {
    const parts = (el.dataset.volumeScale || '').split(' ').map(Number);
    return parts.length === 3 && parts.every(Number.isFinite) ? parts : null;
  }

  function volumeOf(el) {
    const scale = scaleOf(el);
    return scale ? fromPos(el.value, ...scale) : Number(el.value);
  }

  function revert(el, value) {
    const root = el.closest('[x-data]');
    if (root && window.Alpine) window.Alpine.$data(root).v = value;
    else {
      const scale = scaleOf(el);
      el.value = scale ? toPos(value, scale[0], scale[1]) : value;
    }
  }

  // One rule for every way of setting the Volume in one go (a tap on the track, a typed value, a key): a raise above
  // the big-step threshold asks, and so does a drop above the big-drop threshold (a stray tap near the bottom of the
  // track). A drag sends small steps, so it never asks; neither do the ±0.5 dB buttons. The server enforces the same.
  document.addEventListener('htmx:confirm', (e) => {
    const el = e.detail.elt;
    if (!isVolume(el)) return;
    const next = volumeOf(el);
    const current = el.dataset.current === '' ? NaN : Number(el.dataset.current);
    const ref = state.lastSent !== null ? state.lastSent : current;
    const question = confirmText({
      from: ref,
      to: next,
      raiseDb: Number(el.dataset.confirmRaiseDb),
      dropDb: Number(el.dataset.confirmDropDb),
      fmtNum: window.mdsp.fmtNum,
    });
    if (!question) {
      if (Number.isFinite(next)) state.lastSent = next;
      return;
    }
    e.preventDefault();
    if (window.confirm(question)) {
      state.lastSent = next;
      state.confirmed = el;
      e.detail.issueRequest(true);
    } else {
      revert(el, ref);
    }
  });

  // The server enforces the same guards (409 without confirm=true), so a confirmed change carries the flag. The
  // slider sends the dB of its position, never the position.
  document.addEventListener('htmx:configRequest', (e) => {
    const el = e.detail.elt;
    if (isVolume(el) && scaleOf(el)) e.detail.parameters.volume = String(volumeOf(el));
    if (state.confirmed && state.confirmed === el) {
      e.detail.parameters.confirm = 'true';
      state.confirmed = null;
    }
  });

  // Arrow keys move the dB-weighted slider by the device step (0.5 dB) wherever it is on the track, Page Up / Down by
  // 5 dB; Home and End stay native (the ends of the range). The change goes out like a drag step (an input event).
  const KEY_STEPS = { ArrowUp: 1, ArrowRight: 1, ArrowDown: -1, ArrowLeft: -1, PageUp: 10, PageDown: -10 };
  document.addEventListener('keydown', (e) => {
    const el = e.target;
    if (!isVolume(el) || !(e.key in KEY_STEPS)) return;
    const scale = scaleOf(el);
    if (!scale) return;
    e.preventDefault();
    const [min, max, step] = scale;
    const next = Math.min(max, Math.max(min, volumeOf(el) + KEY_STEPS[e.key] * step));
    el.value = String(toPos(next, min, max));
    el.dispatchEvent(new Event('input', { bubbles: true }));
  });

  document.addEventListener('alpine:init', () => {
    window.Alpine.data('masterVolume', (initial, min = -127, max = 0, step = 0.5) => ({
      v: initial,
      fmt(value) {
        return window.mdsp.fmtDb(value);
      },
      // The slider's track position for the current Volume, and the Volume of a position it was moved to.
      pos() {
        return toPos(this.v, min, max);
      },
      moved(event) {
        this.v = fromPos(event.target.value, min, max, step);
      },
      typed(event) {
        const value = parseFloat(event.target.value);
        if (Number.isFinite(value)) this.v = value;
      },
    }));
  });
})();
