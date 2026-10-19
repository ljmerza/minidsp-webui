// Master volume: an Alpine component keeps the slider and the numeric field in step; htmx sends the value after
// an 80 ms pause (hx-trigger "input changed delay:80ms"). Header swaps are held back while the user is still
// adjusting the volume, so a refresh never yanks the slider out from under a drag or drops a pending change.
(() => {
  'use strict';

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

  function revert(el, value) {
    const root = el.closest('[x-data]');
    if (root && window.Alpine) window.Alpine.$data(root).v = value;
    else el.value = value;
  }

  // Raising the volume by more than the configured step in one go (a tap on the track, a typo) needs a confirmation,
  // and so does lowering it by more than the drop step (a stray tap near the bottom of the track). A drag sends
  // small steps, so it never asks.
  function guardedChange(el, next, ref) {
    const raise = Number(el.dataset.confirmRaiseDb);
    const drop = Number(el.dataset.confirmDropDb);
    if (raise > 0 && next - ref > raise) return 'Raise';
    if (drop > 0 && ref - next > drop) return 'Lower';
    return null;
  }

  document.addEventListener('htmx:confirm', (e) => {
    const el = e.detail.elt;
    if (!isVolume(el)) return;
    const next = Number(el.value);
    const current = el.dataset.current === '' ? NaN : Number(el.dataset.current);
    const ref = state.lastSent !== null ? state.lastSent : current;
    const verb = Number.isFinite(next) && Number.isFinite(ref) ? guardedChange(el, next, ref) : null;
    if (!verb) {
      if (Number.isFinite(next)) state.lastSent = next;
      return;
    }
    e.preventDefault();
    const { fmtNum, fmtDb } = window.mdsp;
    // Plain wording (phase 9 design §1, kept by the user's decision): say where the Volume goes, from and to.
    const change = `${fmtNum(Math.abs(next - ref))} dB ${verb === 'Raise' ? 'louder' : 'quieter'}`;
    if (window.confirm(`${verb === 'Raise' ? 'Raise' : 'Drop'} the volume from ${fmtNum(ref)} to ${fmtDb(next)}? That's ${change}.`)) {
      state.lastSent = next;
      state.confirmed = el;
      e.detail.issueRequest(true);
    } else {
      revert(el, ref);
    }
  });

  // The server enforces the same guards (409 without confirm=true), so a confirmed change carries the flag.
  document.addEventListener('htmx:configRequest', (e) => {
    if (state.confirmed && state.confirmed === e.detail.elt) {
      e.detail.parameters.confirm = 'true';
      state.confirmed = null;
    }
  });

  document.addEventListener('alpine:init', () => {
    window.Alpine.data('masterVolume', (initial) => ({
      v: initial,
      fmt(value) {
        return window.mdsp.fmtDb(value);
      },
      typed(event) {
        const value = parseFloat(event.target.value);
        if (Number.isFinite(value)) this.v = value;
      },
    }));
  });
})();
