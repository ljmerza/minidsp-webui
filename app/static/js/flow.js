// The signal-flow diagram (phase 9 design §5): partials/flow_card.html + partials/flow.html, drawn on the server.
//
// This component sits inside the page's levelMeters component (meters.js) and reads its level / peak / clip / stale
// through the template expressions, so the diagram's meters and CLIP chips are the page's meters. It adds:
// - horizontal meter bars with a "no signal" reading below the Settings floor (5.3);
// - ←/→ (and Home/End) between the setting blocks of a row, one tab stop per row (roving tabindex, 5.5);
// - the inline fixes ("Route In 1 → Sub", "Unmute In 1"): one edit through the same endpoint as the editors, then an
//   Undo toast;
// - one polite announcement per clip latch ("Shakers clipped");
// - a re-fetch when the playing preset or Mute all changes (the "master" stream carries every volume tick too), and
//   focus kept on the same control across re-fetches;
// - window.mdsp.flow.highlight(): wizards outline the blocks they will change (5.2 "Wizard target").
(() => {
  'use strict';

  const mdsp = () => (typeof window !== 'undefined' && window.mdsp) || {};
  const toast = (message, level, options) => mdsp().toast && mdsp().toast(message, level, options);
  const FLOOR_DB = -120; // the meter scale's bottom (meters.js)

  let targets = []; // [{ output, blocks: [...] }] highlighted for a wizard

  function parseEvent(event) {
    if (!event || typeof event.data !== 'string') return null;
    try {
      return JSON.parse(event.data);
    } catch {
      return null;
    }
  }

  function fraction(db) {
    const f = mdsp().meterFraction;
    return typeof f === 'function' ? f(db) : 0;
  }

  // The meter's reading: "—" without data, "no signal" below the floor, else "−18.0 dBFS".
  function meterText(db, stale, floor) {
    if (stale || db === null || db === undefined || !Number.isFinite(db)) return '—';
    if (db < floor) return 'no signal';
    const fmt = mdsp().fmtDb;
    return fmt ? fmt(db, 1, 'dBFS') : `${db.toFixed(1)} dBFS`;
  }

  // The next block for a key press inside a row's toolbar, or null.
  function nextIndex(key, index, count) {
    if (count <= 0) return null;
    if (key === 'ArrowRight') return (index + 1) % count;
    if (key === 'ArrowLeft') return (index - 1 + count) % count;
    if (key === 'Home') return 0;
    if (key === 'End') return count - 1;
    return null;
  }

  function rove(toolbar, target) {
    for (const block of toolbar.querySelectorAll('[data-flow-block]')) {
      block.tabIndex = block === target ? 0 : -1;
    }
  }

  function applyHighlight(root) {
    const scope = root || document;
    for (const el of scope.querySelectorAll('[data-flow-block].is-target')) el.classList.remove('is-target');
    for (const t of targets) {
      for (const block of t.blocks || []) {
        const el = scope.querySelector(`[data-flow-block][data-output="${Number(t.output)}"][data-block="${block}"]`);
        if (el) el.classList.add('is-target');
      }
    }
  }

  function flowDiagram(cfg = {}) {
    const floor = Number.isFinite(cfg.floor) ? cfg.floor : -90;
    const latched = new Set();
    let focusKey = null;
    let rootEl = null; // the card: `this.$el` is the element of whichever directive calls a method

    return {
      details: false,
      showUnused: false,
      clipMessage: '',

      init() {
        rootEl = this.$el;
        rootEl.addEventListener('click', (e) => this.onClick(e));
        rootEl.addEventListener('keydown', (e) => this.onKeydown(e));
        rootEl.addEventListener('sse:master', (e) => this.onMaster(e.detail));
        const box = this.box();
        if (!box) return;
        box.addEventListener('htmx:beforeSwap', () => {
          const active = document.activeElement;
          focusKey = active && box.contains(active) && active.dataset ? active.dataset.flowKey || null : null;
        });
        box.addEventListener('htmx:afterSwap', () => {
          applyHighlight(box);
          if (!focusKey) return;
          const target = box.querySelector(`[data-flow-key="${CSS.escape(focusKey)}"]`);
          focusKey = null;
          if (!target) return;
          const toolbar = target.closest('[role="toolbar"]');
          if (toolbar) rove(toolbar, target);
          target.focus();
        });
      },

      box() {
        return rootEl ? rootEl.querySelector('[data-flow-box]') : null;
      },

      flow() {
        return rootEl ? rootEl.querySelector('[data-flow]') : null;
      },

      refresh() {
        const box = this.box();
        if (box && window.htmx) window.htmx.trigger(box, 'refresh');
      },

      // --- meters (the values come from the enclosing levelMeters component) ---------------------------------------

      meterNow(db, stale) {
        if (stale || db === null || db === undefined || !Number.isFinite(db)) return FLOOR_DB;
        return Math.round(Math.max(FLOOR_DB, Math.min(0, db)) * 10) / 10;
      },

      meterText(db, stale) {
        return meterText(db, stale, floor);
      },

      meterBar(db, stale) {
        const f = stale ? 0 : fraction(db);
        return `clip-path: inset(0 ${((1 - f) * 100).toFixed(2)}% 0 0)`;
      },

      meterPeak(db, stale) {
        if (stale || db === null || db === undefined) return 'display: none';
        return `left: ${(fraction(db) * 100).toFixed(2)}%`;
      },

      hasSignal(db, stale) {
        return !stale && typeof db === 'number' && Number.isFinite(db) && db >= floor;
      },

      // Called from x-effect with the clip latches: announce each new latch once.
      announceClips(clip) {
        if (!clip) return;
        const flow = this.flow();
        let names = {};
        try {
          names = flow ? JSON.parse(flow.dataset.names || '{}') : {};
        } catch {
          names = {};
        }
        const fresh = [];
        for (const [key, on] of Object.entries(clip)) {
          if (on && !latched.has(key)) {
            latched.add(key);
            fresh.push(names[key] || key);
          } else if (!on) {
            latched.delete(key);
          }
        }
        if (fresh.length) this.clipMessage = `${fresh.join(', ')} clipped`;
      },

      // --- keyboard ------------------------------------------------------------------------------------------------

      onKeydown(e) {
        const block = e.target.closest && e.target.closest('[data-flow-block]');
        if (!block || e.altKey || e.ctrlKey || e.metaKey) return;
        const toolbar = block.closest('[role="toolbar"]');
        if (!toolbar) return;
        if (e.key === ' ') {
          // Space activates the link, as Enter does (design 5.5).
          e.preventDefault();
          block.click();
          return;
        }
        const blocks = Array.from(toolbar.querySelectorAll('[data-flow-block]'));
        const next = nextIndex(e.key, blocks.indexOf(block), blocks.length);
        if (next === null) return;
        e.preventDefault();
        rove(toolbar, blocks[next]);
        blocks[next].focus();
      },

      // --- the preset or Mute all changed ----------------------------------------------------------------------------

      onMaster(event) {
        const data = parseEvent(event);
        const master = data && data.master;
        const flow = this.flow();
        if (!master || !flow) return;
        const presetChanged = typeof master.preset === 'number' && flow.dataset.preset !== String(master.preset);
        const known = flow.dataset.masterMute;
        const muteChanged = typeof master.mute === 'boolean' && !!known && known !== String(master.mute);
        if (presetChanged || muteChanged) this.refresh();
      },

      // --- inline fixes ----------------------------------------------------------------------------------------------

      onClick(e) {
        const button = e.target.closest && e.target.closest('[data-flow-fix]');
        if (!button || button.disabled) return;
        const flow = this.flow();
        if (!flow || !flow.dataset.editUrl) return;
        let values;
        let undo;
        try {
          values = JSON.parse(button.dataset.values || '{}');
          undo = JSON.parse(button.dataset.undo || '{}');
        } catch {
          return;
        }
        this.fix(flow.dataset.editUrl, flow.dataset.preset, values, undo, button.dataset.done || 'Done.', button);
      },

      async fix(url, preset, values, undo, done, button) {
        if (button) button.disabled = true;
        const ok = await this.send(url, values);
        if (button && button.isConnected) button.disabled = false;
        if (!ok) return;
        this.refresh();
        const opener = rootEl ? rootEl.querySelector('#flow-title') : null;
        toast(done, 'info', {
          action: {
            label: 'Undo',
            onClick: async () => {
              const flow = this.flow();
              if (!flow || flow.dataset.preset !== preset) {
                toast('Left as it is: the device has switched presets since.', 'info');
                return;
              }
              if (await this.send(url, undo)) this.refresh();
            },
            returnFocus: opener,
          },
        });
      },

      // True when the server took the values. A refused change says why; a guarded one asks first.
      async send(url, values, confirm = false) {
        let response;
        let data = null;
        try {
          response = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'HX-Request': 'true', Accept: 'application/json' },
            body: JSON.stringify({ values, commit: true, confirm }),
            credentials: 'same-origin',
          });
          data = await response.json().catch(() => null);
        } catch {
          toast('Request failed: the web UI server is unreachable');
          return false;
        }
        if (response.ok) return true;
        if (response.status === 409 && data && data.error === 'confirmation_required' && !confirm) {
          if (window.confirm(`${data.detail}.\n\nApply it anyway?`)) return this.send(url, values, true);
          return false;
        }
        const detail = data && (data.detail || (data.problems || []).join('; '));
        toast(detail || `Change refused (HTTP ${response.status})`);
        return false;
      },
    };
  }

  // Wizards: outline the blocks they will change, e.g. highlight([{ output: 1, blocks: ['eq'] }]); clear() removes it.
  const api = {
    highlight(list) {
      targets = Array.isArray(list) ? list : [];
      applyHighlight(document);
    },
    clear() {
      targets = [];
      applyHighlight(document);
    },
  };

  window.mdsp = Object.assign(window.mdsp || {}, { flow: { ...api, flowDiagram, meterText, nextIndex } });

  if (typeof document !== 'undefined') {
    document.addEventListener('alpine:init', () => {
      window.Alpine.data('flowDiagram', flowDiagram);
    });
  }
})();
