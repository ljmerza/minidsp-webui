// Dashboard output cards (docs 05 §1): a quick mute toggle and a ±0.5 dB gain nudge per output.
//
// The cards are server-rendered (partials/dashboard_outputs.html) and re-fetched by HTMX on SSE "sync" / "desired"
// events, so every tab converges. This component also re-fetches them when the active preset changes, and handles
// the buttons: a click updates the card at once (⟳ pending), then goes to the same edit endpoint as the output pages,
// so the server applies the same guards (gain ceilings, the big-step confirmation). Un-muting an output whose gain or
// mute was never confirmed on the device asks first, as on the output page. Per output at most one request is in
// flight and only the newest values wait, so values never arrive out of order. A refused change re-fetches the cards,
// which puts the stored values back.
(() => {
  'use strict';

  const ICON = { synced: '✓', pending: '⟳', failed: '⚠', unknown: '?' };

  const mdsp = () => window.mdsp || {};
  const toast = (message, level) => mdsp().toast && mdsp().toast(message, level);
  const fmtDb = (value) => (mdsp().fmtDb ? mdsp().fmtDb(value) : `${value.toFixed(1)} dB`);
  const round1 = (value) => Math.round(value * 10) / 10;

  function parseEvent(event) {
    if (!event || typeof event.data !== 'string') return null;
    try {
      return JSON.parse(event.data);
    } catch {
      return null;
    }
  }

  // The next gain for a nudge: on the 0.1 dB grid, inside [gain_min, ceiling]; null when it wouldn't change.
  function nudgedGain(current, step, min, max) {
    const next = Math.min(max, Math.max(min, round1(current + step)));
    return Number.isFinite(next) && next !== current ? next : null;
  }

  function focusKey(el) {
    const card = el && el.closest && el.closest('[data-output]');
    if (!card || !el.dataset || !el.dataset.action) return null;
    return `${card.dataset.output}|${el.dataset.action}|${el.dataset.step || ''}`;
  }

  function outputCards() {
    const queues = new Map(); // "<edit url>|<output>" -> { inflight, queued: values | null }
    let focus = null; // the quick control that had focus before a re-fetch replaced it

    return {
      init() {
        this.$el.addEventListener('click', (e) => this.onClick(e));
        this.$el.addEventListener('sse:master', (e) => this.onMaster(e.detail));
        const box = this.box();
        if (!box) return;
        box.addEventListener('htmx:beforeSwap', (e) => {
          // A re-fetch landing while a change is on its way would flash the old value; the drain re-fetches anyway.
          if (this.busy()) e.detail.shouldSwap = false;
          else focus = box.contains(document.activeElement) ? focusKey(document.activeElement) : null;
        });
        box.addEventListener('htmx:afterSwap', () => {
          if (!focus) return;
          const target = Array.from(box.querySelectorAll('[data-action]')).find((el) => focusKey(el) === focus);
          focus = null;
          if (target && !target.disabled) target.focus();
        });
      },

      box() {
        return this.$el.querySelector('[data-output-cards]');
      },

      list() {
        return this.$el.querySelector('.output-cards');
      },

      refresh() {
        const box = this.box();
        if (box && window.htmx) window.htmx.trigger(box, 'refresh');
      },

      busy() {
        for (const q of queues.values()) if (q.inflight || q.queued) return true;
        return false;
      },

      // "master" arrives for every volume or mute change too; only a different active preset changes the cards.
      onMaster(event) {
        const data = parseEvent(event);
        const preset = data && data.master ? data.master.preset : null;
        const list = this.list();
        if (typeof preset === 'number' && list && list.dataset.preset !== String(preset)) this.refresh();
      },

      onClick(e) {
        const button = e.target.closest && e.target.closest('[data-action]');
        if (!button || button.disabled || !this.$el.contains(button)) return;
        const card = button.closest('[data-output]');
        const list = this.list();
        if (!card || !list || !list.dataset.editUrl) return;
        const o = Number(card.dataset.output);
        if (button.dataset.action === 'mute') {
          const mute = card.dataset.mute !== 'true';
          if (!mute && card.dataset.neverSynced === 'true' && !this.confirmUnmute(card)) return;
          this.show(card, { mute });
          this.enqueue(list.dataset.editUrl, o, { [`outputs.${o}.mute`]: mute });
        } else if (button.dataset.action === 'nudge') {
          const gain = nudgedGain(
            Number(card.dataset.gain),
            Number(button.dataset.step),
            Number(card.dataset.gainMin),
            Number(card.dataset.ceiling),
          );
          if (gain === null) return;
          this.show(card, { gain });
          this.enqueue(list.dataset.editUrl, o, { [`outputs.${o}.gain_db`]: gain });
        }
      },

      confirmUnmute(card) {
        const name = card.dataset.name || 'This output';
        const why = 'has never been synced, so its gain on the device is unknown';
        return window.confirm(`${name} ${why}. Un-mute it anyway?`);
      },

      // Optimistic update of one card: values, button states and a pending badge.
      show(card, { gain, mute }) {
        if (gain !== undefined) card.dataset.gain = String(gain);
        if (mute !== undefined) card.dataset.mute = String(mute);
        const muted = card.dataset.mute === 'true';
        const value = Number(card.dataset.gain);
        const text = card.querySelector('[data-gain-text]');
        if (text) text.textContent = fmtDb(value) + (muted ? ' (muted)' : '');
        const muteButton = card.querySelector('[data-action="mute"]');
        if (muteButton) {
          muteButton.classList.toggle('is-muted', muted);
          muteButton.setAttribute('aria-pressed', String(muted));
          muteButton.textContent = muted ? 'Muted' : 'Mute';
        }
        for (const nudge of card.querySelectorAll('[data-action="nudge"]')) {
          const up = Number(nudge.dataset.step) > 0;
          nudge.disabled = up ? value >= Number(card.dataset.ceiling) : value <= Number(card.dataset.gainMin);
        }
        const badge = card.querySelector('[data-sync-badge]');
        if (badge) {
          badge.className = badge.className.replace(/\bsync-(synced|pending|failed|unknown)\b/, 'sync-pending');
          const icon = badge.querySelector('.sync-icon');
          const label = badge.querySelector('.sync-text');
          if (icon) icon.textContent = ICON.pending;
          if (label) label.textContent = 'pending';
        }
      },

      enqueue(url, output, values) {
        const key = `${url}|${output}`;
        let q = queues.get(key);
        if (!q) {
          q = { inflight: false, queued: null };
          queues.set(key, q);
        }
        q.queued = { ...(q.queued || {}), ...values };
        if (!q.inflight) this.pump(url, q);
      },

      async pump(url, q) {
        q.inflight = true;
        try {
          while (q.queued) {
            const values = q.queued;
            q.queued = null;
            if (!(await this.send(url, values))) q.queued = null; // refused: drop what queued up meanwhile
          }
        } finally {
          q.inflight = false;
        }
        if (!this.busy()) this.refresh();
      },

      // True when the server took the values (whether they have synced yet is the badge's business).
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

  window.mdsp = Object.assign(window.mdsp || {}, { dashboard: { outputCards, nudgedGain } });

  document.addEventListener('alpine:init', () => {
    window.Alpine.data('outputCards', outputCards);
  });
})();
