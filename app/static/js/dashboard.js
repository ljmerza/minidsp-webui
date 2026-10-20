// Dashboard output cards (docs 05 §1): a quick mute toggle and a ±0.5 dB gain nudge per output.
//
// The cards are server-rendered (partials/dashboard_outputs.html) and re-fetched by HTMX on SSE "sync" / "desired"
// events, so every tab converges. This component also re-fetches them when the active preset changes, and handles
// the buttons: a click updates the card at once (⟳ pending), then goes to the same edit endpoint as the output pages,
// so the server applies the same guards (gain ceilings, the big-step confirmation). Un-muting an output whose gain or
// mute was never confirmed on the device asks first, as on the output page. Per output at most one request is in
// flight and only the newest values wait, so values never arrive out of order. A refused change re-fetches the cards,
// which puts the stored values back. Mute acts at once and leaves an Undo toast (phase9 design §4.3, P20).
//
// Home and /outputs use the same component for their cards (partials/home_card.html) with one more control: the big
// −/+ around "Level, total" (design §4.3). `levelStep` is the rule, mirrored from app/web/home.py level_step: + raises
// the output level up to its limit, then every enabled route into the output (routes share the output's limit);
// − only lowers the output level. Holding a button repeats it. The card's total, breakdown and at-limit notes update
// at once; the re-fetch after the change lands brings the server's wording back.
(() => {
  'use strict';

  const ICON = { synced: '✓', pending: '⟳', failed: '⚠', unknown: '?', changed: '≠', waiting: '⏸' };

  const mdsp = () => window.mdsp || {};
  const toast = (message, level, options) => mdsp().toast && mdsp().toast(message, level, options);
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

  const MINUS = '\u2212';
  const STEP_DB = 0.5;
  const EPS = 1e-9;
  const HOLD_DELAY_MS = 450;
  const HOLD_EVERY_MS = 150;

  // Half-up on the 0.1 dB grid, as home.py's _r1 (Math.round rounds halves up).
  const grid = (value) => Math.floor(value * 10 + 0.5) / 10;

  // One press of a card's − (direction < 0) or + (direction > 0). `state`: { index, output, ceiling, gainMin,
  // routes: [{ input, route }] }. Returns { values: {path: dB}, target: 'output' | 'routes' | 'none' }; no values
  // means the press can't change anything (the button is disabled).
  function levelStep(state, direction, step = STEP_DB) {
    const { index, output, ceiling, gainMin } = state;
    const routes = state.routes || [];
    if (direction < 0) {
      if (output > gainMin + EPS) {
        return { values: { [`outputs.${index}.gain_db`]: Math.max(gainMin, grid(output - step)) }, target: 'output' };
      }
      return { values: {}, target: 'none' };
    }
    if (output < ceiling - EPS) {
      return { values: { [`outputs.${index}.gain_db`]: Math.min(ceiling, grid(output + step)) }, target: 'output' };
    }
    const values = {};
    for (const r of routes) {
      if (r.route < ceiling - EPS) values[`inputs.${r.input}.routing.${index}.gain_db`] = Math.min(ceiling, grid(r.route + step));
    }
    return Object.keys(values).length ? { values, target: 'routes' } : { values: {}, target: 'none' };
  }

  // Number words as app/sync/summary.py: signed() "+4", "−2.5", "0"; signed1() "+16.0".
  function clean(value, digits) {
    const v = Number(value.toFixed(digits));
    return v === 0 ? 0 : v;
  }
  function signed(value, digits = 2) {
    const v = clean(value, digits);
    if (v > 0) return `+${v}`;
    if (v < 0) return `${MINUS}${-v}`;
    return '0';
  }
  function signed1(value) {
    const v = clean(value, 1);
    if (v > 0) return `+${v.toFixed(1)}`;
    if (v < 0) return `${MINUS}${(-v).toFixed(1)}`;
    return '0.0';
  }

  // "Level, total" and its breakdown, as summary.py _total: { text: "+16.0 dB", breakdown: "route +4 + output +12" }.
  // `state.routes`: [{ name, in, route }].
  function totalView(state) {
    const output = state.output;
    const routes = state.routes || [];
    if (!routes.length) return { text: `${signed1(output)} dB`, breakdown: `output ${signed(output)}` };
    const totals = routes.map((r) => r.in + r.route + output);
    const lo = Math.min(...totals);
    const hi = Math.max(...totals);
    const text = clean(hi - lo, 2) === 0 ? `${signed1(hi)} dB` : `${signed(lo, 1)} to ${signed(hi, 1)} dB`;
    const chain = (r, labelled) => {
      const level = clean(r.in, 2) ? `level ${signed(r.in)} + ` : '';
      const body = `${level}route ${signed(r.route)} + output ${signed(output)}`;
      if (labelled) return `${r.name}: ${body}`;
      return level ? `${r.name} ${body}` : body;
    };
    const breakdown = routes.length === 1 ? chain(routes[0], false) : routes.map((r) => chain(r, true)).join(' · ');
    return { text, breakdown };
  }

  function cardState(card) {
    let routes = [];
    try {
      routes = JSON.parse(card.dataset.routes || '[]');
    } catch {
      routes = [];
    }
    return {
      index: Number(card.dataset.output),
      output: Number(card.dataset.gain),
      ceiling: Number(card.dataset.ceiling),
      gainMin: Number(card.dataset.gainMin),
      routes,
    };
  }

  function focusKey(el) {
    const card = el && el.closest && el.closest('[data-output]');
    if (!card || !el.dataset || !el.dataset.action) return null;
    return `${card.dataset.output}|${el.dataset.action}|${el.dataset.step || ''}`;
  }

  function outputCards() {
    const queues = new Map(); // "<edit url>|<output>" -> { inflight, queued: values | null }
    let focus = null; // the quick control that had focus before a re-fetch replaced it
    let hold = null; // { button, timer, repeated, missed, ended } while a −/+ is held down
    let deferred = false; // a re-fetch was skipped while an Adjust ▾ menu was open
    let writes = 0; // changes sent so far
    let fetchedAfter = null; // `writes` when the cards' newest re-fetch went out

    return {
      showUnused: false, // Home: the cards folded into the "Not in use" line are shown

      init() {
        this.$el.addEventListener('click', (e) => this.onClick(e));
        this.$el.addEventListener('sse:master', (e) => this.onMaster(e.detail));
        this.$el.addEventListener('pointerdown', (e) => this.onPointerDown(e));
        // A hold ends with the release (wherever it happens), or early: the pointer leaves the button, Escape, or the
        // window loses focus (no release may follow then).
        for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) {
          document.addEventListener(type, () => this.stopHold(true), true);
        }
        // `pointerleave` doesn't bubble, but this capturing listener sees it for every element in the card: only the
        // held button's own counts (the pointer left it), not its −/+ glyph's (the pointer moved onto its padding).
        this.$el.addEventListener(
          'pointerleave',
          (e) => {
            if (hold && e.target === hold.button) this.stopHold(false);
          },
          true,
        );
        document.addEventListener('keydown', (e) => {
          if (e.key === 'Escape') this.stopHold(false);
        }, true);
        if (typeof window.addEventListener === 'function') window.addEventListener('blur', () => this.stopHold(false));
        // An open Adjust ▾ menu holds re-fetches back (they would close it); closing it catches up.
        this.$el.addEventListener(
          'toggle',
          (e) => {
            if (e.target.dataset && 'adjust' in e.target.dataset && !e.target.open && deferred) {
              deferred = false;
              this.refresh();
            }
          },
          true,
        );
        const box = this.box();
        if (!box) return;
        box.addEventListener('htmx:beforeRequest', (e) => {
          if (e.target === box) fetchedAfter = writes;
        });
        box.addEventListener('htmx:beforeSwap', (e) => {
          // A re-fetch landing while a change is on its way would flash the old value; the drain re-fetches anyway.
          // One landing while −/+ is held would replace the button under the pointer; the end of the hold catches up.
          if (this.busy() || (hold && !hold.ended)) {
            e.detail.shouldSwap = false;
            if (hold) hold.missed = true;
          } else if (fetchedAfter !== null && fetchedAfter < writes) {
            // Asked for before the newest change went out, so it shows the old values (and an Undo would then find
            // nothing to undo). The drain's own re-fetch was dropped while this one was in flight: fetch again.
            e.detail.shouldSwap = false;
            setTimeout(() => this.refresh(), 0);
          } else if (Array.from(box.querySelectorAll('[data-adjust]')).some((menu) => menu.open)) {
            e.detail.shouldSwap = false;
            deferred = true;
          } else focus = box.contains(document.activeElement) ? focusKey(document.activeElement) : null;
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

      // "master" arrives for every volume or mute change too; only a different active preset or Mute all (every
      // card's health badge then says Silent) changes the cards.
      onMaster(event) {
        const data = parseEvent(event);
        const preset = data && data.master ? data.master.preset : null;
        const mute = data && data.master ? data.master.mute : null;
        const list = this.list();
        if (!list) return;
        const presetChanged = typeof preset === 'number' && list.dataset.preset !== String(preset);
        const known = list.dataset.masterMute;
        const muteChanged = typeof mute === 'boolean' && !!known && known !== String(mute);
        if (presetChanged || muteChanged) this.refresh();
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
          if (this.setMute(card, mute)) this.muteToast(o, card.dataset.name || `Out ${o + 1}`, mute);
        } else if (button.dataset.action === 'level') {
          if (hold && hold.repeated && hold.button === button) return; // it already stepped; this is its release
          this.levelPress(card, Number(button.dataset.step));
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

      // One press of a card's −/+ (design §4.3): the rule picks the output level or the routes; nothing happens when
      // it can't change anything. False when nothing was sent.
      levelPress(card, step) {
        const list = this.list();
        if (!list || !list.dataset.editUrl) return false;
        const state = cardState(card);
        const plan = levelStep(state, step, Math.abs(step) || STEP_DB);
        if (!Object.keys(plan.values).length) return false;
        for (const [path, value] of Object.entries(plan.values)) {
          if (path.startsWith('outputs.')) state.output = value;
          const route = /^inputs\.(\d+)\.routing\./.exec(path);
          if (route) {
            const r = state.routes.find((x) => x.input === Number(route[1]));
            if (r) r.route = value;
          }
        }
        card.dataset.gain = String(state.output);
        card.dataset.routes = JSON.stringify(state.routes);
        this.showLevel(card, state);
        this.show(card, {});
        this.enqueue(list.dataset.editUrl, state.index, plan.values);
        return true;
      },

      // The card's total, breakdown, button states and at-limit notes for its current values.
      showLevel(card, state) {
        const view = totalView(state);
        const text = card.querySelector('[data-total-text]');
        if (text) text.textContent = view.text;
        const breakdown = card.querySelector('[data-breakdown]');
        if (breakdown) breakdown.textContent = view.breakdown;
        const plus = levelStep(state, 1);
        const minus = levelStep(state, -1);
        for (const b of card.querySelectorAll('[data-action="level"]')) {
          const plan = Number(b.dataset.step) > 0 ? plus : minus;
          b.disabled = !Object.keys(plan.values).length;
        }
        const show = {
          routes: plus.target === 'routes',
          max: plus.target === 'none',
          min: minus.target === 'none',
        };
        for (const note of card.querySelectorAll('[data-note]')) note.hidden = !show[note.dataset.note];
      },

      // Holding −/+ repeats it (after a short pause, then every 150 ms) until released.
      onPointerDown(e) {
        if (e.button !== undefined && e.button !== 0) return;
        const button = e.target.closest && e.target.closest('[data-action="level"]');
        if (!button || button.disabled || !this.$el.contains(button)) return;
        this.stopHold();
        const card = button.closest('[data-output]');
        if (!card) return;
        const current = { button, timer: null, repeated: false, missed: false, ended: false };
        hold = current;
        current.timer = setTimeout(function repeat() {
          if (hold !== current || button.disabled || !button.isConnected) return;
          current.repeated = true;
          this.levelPress(card, Number(button.dataset.step));
          current.timer = setTimeout(repeat.bind(this), HOLD_EVERY_MS);
        }.bind(this), HOLD_DELAY_MS);
      },

      // The hold is over (`released`: the pointer went up or was cancelled) or stops early (left the button, Escape,
      // the window lost focus). The repeat stops, and a re-fetch skipped during the hold is fetched again, unless a
      // change is on its way (its own re-fetch follows): a release off the button sends no click, so nothing else
      // would. The click that follows the release of a repeated hold must not step once more, so a repeated hold
      // that stopped early is kept (it no longer holds re-fetches) until the release.
      stopHold(released = true) {
        const ended = hold;
        if (!ended) return;
        clearTimeout(ended.timer);
        const first = !ended.ended;
        ended.ended = true;
        setTimeout(() => {
          if (hold === ended && (released || !ended.repeated)) hold = null;
          if (first && (ended.repeated || ended.missed) && !this.busy()) this.refresh();
        }, 0);
      },

      // Mute or un-mute one card's output (un-muting a never-synced output asks first). False when cancelled.
      setMute(card, mute) {
        const list = this.list();
        if (!list || !list.dataset.editUrl) return false;
        if (!mute && card.dataset.neverSynced === 'true' && !this.confirmUnmute(card)) return false;
        const o = Number(card.dataset.output);
        this.show(card, { mute });
        this.enqueue(list.dataset.editUrl, o, { [`outputs.${o}.mute`]: mute });
        return true;
      },

      card(o) {
        const list = this.list();
        return list ? list.querySelector(`[data-output="${o}"]`) : null;
      },

      // "Shakers muted. [Undo]": Undo puts the previous state back. The cards may have been re-fetched meanwhile, so
      // the card is looked up again; if they now show another preset (the playing preset changed), Undo would hit
      // the same output number on that preset instead, so it changes nothing and says so.
      muteToast(o, name, mute) {
        const before = this.list();
        const preset = before ? before.dataset.preset : undefined;
        toast(`${name} ${mute ? 'muted' : 'un-muted'}.`, 'info', {
          action: {
            label: 'Undo',
            onClick: () => {
              const list = this.list();
              if (!list || list.dataset.preset !== preset) {
                toast(`${name} was left as it is: the device has switched presets since.`, 'info');
                return;
              }
              const card = this.card(o);
              if (card && card.dataset.mute === String(mute)) this.setMute(card, !mute);
              const again = this.card(o);
              const button = again && again.querySelector('[data-action="mute"]');
              if (button) button.focus();
            },
          },
        });
      },

      confirmUnmute(card) {
        const name = card.dataset.name || 'This output';
        const why = 'has never been sent, so its level on the device is unknown';
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
          const word = muteButton.querySelector('[data-mute-word]') || muteButton;
          word.textContent = muted ? 'Muted' : 'Mute';
        }
        for (const nudge of card.querySelectorAll('[data-action="nudge"]')) {
          const up = Number(nudge.dataset.step) > 0;
          nudge.disabled = up ? value >= Number(card.dataset.ceiling) : value <= Number(card.dataset.gainMin);
        }
        const badge = card.querySelector('[data-sync-badge]');
        if (badge) {
          badge.className = badge.className.replace(/\bsync-(synced|pending|failed|unknown|changed|waiting)\b/, 'sync-pending');
          const icon = badge.querySelector('.sync-icon');
          const label = badge.querySelector('.sync-text');
          if (icon) icon.textContent = ICON.pending;
          if (label) label.textContent = 'sending';
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
        writes += 1;
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

  window.mdsp = Object.assign(window.mdsp || {}, { dashboard: { outputCards, nudgedGain, levelStep, totalView } });

  document.addEventListener('alpine:init', () => {
    window.Alpine.data('outputCards', outputCards);
  });
})();
