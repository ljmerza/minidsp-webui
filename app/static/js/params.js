// Preset-editing pages (routing, inputs, outputs): optimistic controls bound to desired-state paths.
//
// Every control with data-path belongs to the [data-params] root, which knows the serial, the edited preset and the
// edit URL. A change updates every control showing the same path at once, then goes to the server after an 80 ms
// pause (drags) or immediately (release, toggles, typed values; those also mark a commit boundary). Per path there
// is at most one request in flight and only the newest waiting value is sent next, so values never arrive out of
// order. The sync badges ([data-sync-for]) follow the writer's "sync" events; a value the server rejects (validation,
// a cancelled confirmation) is put back to the stored one, while a value that fails to *sync* is kept and shown as
// failed. "desired" events from other tabs move controls that aren't being edited here.
(() => {
  'use strict';

  const DEBOUNCE_MS = 80;
  const MAX_WAIT_MS = 250; // a continuous drag still sends about 4 values a second, so it can be heard as it moves
  const LOCAL_HOLD_MS = 1500; // ignore echoes of a path for this long after the last local change
  // The status model's order (app/sync/status.py): Failed > Changed on device > Sending > Waiting > Unknown > Sent.
  // "changed" (≠, found by the last check) and "waiting" (⏸, a preset that isn't playing) come from the server's
  // status overlay (status.badge_statuses) and from "verify" events.
  const RANK = { synced: 0, unknown: 1, waiting: 2, pending: 3, changed: 4, failed: 5 };
  const ICON = { synced: '✓', pending: '⟳', failed: '⚠', unknown: '?', changed: '≠', waiting: '⏸' };
  // On-screen words for the statuses (phase 9 design §2; app/web/ui_common.py STATUS_TEXT).
  const WORD = {
    synced: 'sent', pending: 'sending', failed: 'failed', unknown: 'unknown', changed: 'changed on device', waiting: 'waiting',
  };
  const KEY_STEP = 0.5;
  const KEY_STEP_FINE = 0.1;
  const LEVELS_STALE_MS = 2500; // routing page: output levels older than this don't count as signal

  const readJson = (root, selector, fallback) => {
    const el = root.querySelector(selector);
    if (!el) return fallback;
    try {
      return JSON.parse(el.textContent);
    } catch {
      return fallback;
    }
  };

  const readJsonAttr = (text, fallback) => {
    try {
      const value = JSON.parse(text);
      return Array.isArray(value) ? value : fallback;
    } catch {
      return fallback;
    }
  };

  const toast = (message, level, options) => window.mdsp && window.mdsp.toast(message, level, options);
  const round = (value, decimals) => {
    const f = 10 ** decimals;
    return Math.round(value * f) / f;
  };

  class ParamsPage {
    constructor(root) {
      this.root = root;
      this.preset = Number(root.dataset.preset);
      this.editUrl = root.dataset.editUrl;
      this.sampleRate = Number(root.dataset.sampleRate || 96000);
      this.status = readJson(root, '[data-sync-state]', {});
      this.desired = readJson(root, '[data-desired-state]', {});
      this.reasons = readJson(root, '[data-sync-reasons]', {});
      this.channels = new Map(); // path -> { timer, inflight, queued }
      this.localAt = new Map(); // path -> time of the last local change
      this.statusAt = new Map(); // path -> time of the last status from the server's event stream
      this.sentSince = new Set(); // paths sent again since the last device check (no longer "changed on device")
      this.checkedAt = null; // the last check's time, from "verify" events
      this.dragging = null;
      this.levels = readJsonAttr(root.dataset.outputLevels, []); // routing page only (sse:levels)
      this.levelsAt = this.levels.length ? performance.now() : -Infinity;

      root.addEventListener('input', (e) => this.onInput(e));
      root.addEventListener('change', (e) => this.onChange(e));
      root.addEventListener('keydown', (e) => this.onKey(e));
      root.addEventListener('pointerdown', (e) => {
        if (e.target.matches && e.target.matches('input[type="range"][data-path]')) this.dragging = e.target;
      });
      window.addEventListener('pointerup', () => { this.dragging = null; }, true);
      root.addEventListener('sse:sync', (e) => this.onSyncEvent(e.detail));
      root.addEventListener('sse:verify', (e) => this.onVerifyEvent(e.detail));
      root.addEventListener('sse:desired', (e) => this.onDesiredEvent(e.detail));
      root.addEventListener('sse:levels', (e) => this.onLevelsEvent(e.detail));
      root.addEventListener('click', (e) => this.onClick(e));
      this.renderBadges();
      this.updateRouting();
    }

    controls(path) {
      return Array.from(this.root.querySelectorAll('[data-path]')).filter((el) => el.dataset.path === path);
    }

    // --- reading and showing values ----------------------------------------------------------------------------

    readValue(el) {
      if (el.dataset.type === 'bool') return el.checked;
      const value = parseFloat(el.value);
      return Number.isFinite(value) ? value : null;
    }

    showValue(path, value, except = null) {
      for (const el of this.controls(path)) {
        if (el === except) continue;
        if (el.dataset.type === 'bool') el.checked = !!value;
        else if (el.type === 'number') el.value = el.hasAttribute('data-delay') ? round(value, 2).toFixed(2) : round(value, 1).toFixed(1);
        else el.value = value;
        if (el.type === 'range') el.setAttribute('aria-valuetext', window.mdsp.fmtDb(value));
      }
      for (const el of this.root.querySelectorAll('[data-samples-for]')) {
        if (el.dataset.samplesFor === path) el.textContent = String(Math.round((value * this.sampleRate) / 1000));
      }
      if (path.includes('.routing.')) this.updateRouting();
    }

    // Routing matrix: a cell's gain is editable only while the route is on; an output without any route is silent.
    updateRouting() {
      for (const el of this.root.querySelectorAll('[data-enabled-by]')) {
        const toggle = this.controls(el.dataset.enabledBy)[0];
        if (toggle) {
          el.disabled = !toggle.checked;
          const cell = el.closest('[data-route-cell]');
          if (cell) cell.classList.toggle('is-on', toggle.checked);
        }
      }
      for (const warn of this.root.querySelectorAll('[data-silent-output]')) {
        const o = warn.dataset.silentOutput;
        const routes = this.root.querySelectorAll(`[data-route-output="${o}"]`);
        warn.hidden = Array.from(routes).some((el) => el.checked);
      }
      this.updateMonoPreview();
    }

    // --- sync badges -------------------------------------------------------------------------------------------

    renderBadges() {
      for (const badge of this.root.querySelectorAll('[data-sync-for]')) {
        const paths = badge.dataset.syncFor.split(' ').filter(Boolean);
        let worst = 'synced';
        let reason = null;
        const waiting = this.waiting();
        for (const p of paths) {
          let st = this.status[p] || 'unknown';
          if (st === 'pending' && waiting) st = 'waiting';
          if (!(st in RANK)) st = 'unknown';
          if (RANK[st] > RANK[worst]) worst = st;
          if (!reason && this.reasons[p]) reason = this.reasons[p];
        }
        badge.className = badge.className.replace(/\bsync-(synced|pending|failed|unknown|changed|waiting)\b/, `sync-${worst}`);
        const icon = badge.querySelector('.sync-icon');
        const text = badge.querySelector('.sync-text');
        if (icon) icon.textContent = ICON[worst];
        if (text) text.textContent = WORD[worst];
        // The failure reason is read out after the status word (no hover-only title, phase 9 §13.1).
        let why = badge.querySelector('.sync-reason');
        if (reason && worst === 'failed') {
          if (!why) {
            why = document.createElement('span');
            why.className = 'sync-reason visually-hidden';
            badge.appendChild(why);
          }
          why.textContent = `: ${reason}`;
        } else if (why) {
          why.remove();
        }
      }
    }

    // The edited preset isn't playing (the sync bar shows the amber band), so its writes wait instead of being sent.
    waiting() {
      return !!document.querySelector('#syncbar [data-editing-band]');
    }

    onSyncEvent(event) {
      const data = parseEvent(event);
      if (!data || data.preset !== this.preset) return;
      const now = performance.now();
      for (const [path, st] of Object.entries(data.changes || {})) {
        this.status[path] = st;
        this.statusAt.set(path, now);
        if (st === 'pending') this.sentSince.add(path);
      }
      for (const path of data.removed || []) delete this.status[path];
      this.reasons = data.failed || {};
      this.renderBadges();
    }

    // A device check (or using / undoing the device's values) publishes the paths it found changed on the device: they
    // read ≠ until they are sent again, used or forgotten (design 11.1), as the header and the sync bar say.
    onVerifyEvent(event) {
      const data = parseEvent(event);
      if (!data || data.preset !== this.preset || data.status === 'error' || !Array.isArray(data.paths)) return;
      if (data.checked_at !== this.checkedAt) {
        this.checkedAt = data.checked_at;
        this.sentSince.clear();
      }
      const open = new Set(data.paths.filter((p) => !this.sentSince.has(p)));
      for (const [path, st] of Object.entries(this.status)) {
        if (st === 'changed' && !open.has(path)) this.status[path] = 'synced';
      }
      for (const path of open) {
        if (this.status[path] === 'synced') this.status[path] = 'changed';
      }
      this.renderBadges();
    }

    onDesiredEvent(event) {
      const data = parseEvent(event);
      if (!data || data.preset !== this.preset || data.event !== 'edit') return;
      const now = performance.now();
      for (const [path, value] of Object.entries(data.values || {})) {
        if (!(path in this.desired) || value === null || typeof value === 'object') continue;
        this.desired[path] = value;
        if (this.isBusy(path, now)) continue; // being edited here: don't yank the control
        this.showValue(path, value);
      }
    }

    onLevelsEvent(event) {
      const data = parseEvent(event);
      if (!data || !Array.isArray(data.o)) return;
      this.levels = data.o;
      this.levelsAt = performance.now();
    }

    outputLevel(o) {
      if (performance.now() - this.levelsAt > LEVELS_STALE_MS) return null;
      const v = this.levels[o];
      return typeof v === 'number' ? v : null;
    }

    isBusy(path, now = performance.now()) {
      const ch = this.channels.get(path);
      if (ch && (ch.timer || ch.inflight || ch.queued)) return true;
      if (this.dragging && this.dragging.dataset.path === path) return true;
      const focused = document.activeElement;
      if (focused && focused.dataset && focused.dataset.path === path && focused.type === 'number') return true;
      return now - (this.localAt.get(path) || -Infinity) < LOCAL_HOLD_MS;
    }

    // --- user input --------------------------------------------------------------------------------------------

    onInput(e) {
      const el = e.target;
      if (!el.dataset || !el.dataset.path || el.dataset.type === 'bool') return;
      const value = this.readValue(el);
      if (value === null) return;
      this.showValue(el.dataset.path, value, el);
      // Ranges stream while dragging; a typed number is sent when it is committed (change event).
      if (el.type === 'range') this.schedule(el.dataset.path, value, { commit: false });
    }

    onChange(e) {
      const el = e.target;
      if (el.matches && el.matches('[data-mono-target]')) {
        this.updateMonoPreview();
        return;
      }
      if (!el.dataset || !el.dataset.path) return;
      const path = el.dataset.path;
      const value = this.readValue(el);
      if (value === null) {
        this.showValue(path, this.desired[path]); // an empty or invalid number: put the stored value back
        return;
      }
      if (el.dataset.type === 'bool' && el.hasAttribute('data-confirm-unmute') && value === false && !this.confirmUnmute(el, path)) {
        el.checked = true;
        return;
      }
      if (el.dataset.type === 'bool' && el.hasAttribute('data-route-output') && value === false && !this.confirmRouteOff(el)) {
        el.checked = true; // cancelled: the cell stays on and nothing is sent
        return;
      }
      this.showValue(path, value, el);
      this.schedule(path, value, { commit: true, immediate: true });
      if (el.dataset.type === 'bool' && el.hasAttribute('data-undo-name')) this.undoToast(el, path, value);
    }

    // "Shakers muted. [Undo]" (phase9 design §3.6, P20): Undo puts the previous state back, through the same guards
    // as a click (un-muting a never-synced output still asks). Nothing happens if the switch moved on since.
    undoToast(el, path, value) {
      const name = el.dataset.undoName;
      const label = path.endsWith('.mute') ? (value ? 'muted' : 'un-muted') : `set to ${value ? 'on' : 'off'}`;
      toast(`${name} ${label}.`, 'info', {
        action: {
          label: 'Undo',
          returnFocus: el,
          onClick: () => {
            if (!el.isConnected || el.checked !== value) return;
            const back = !value;
            if (back === false && el.hasAttribute('data-confirm-unmute') && !this.confirmUnmute(el, path)) return;
            el.checked = back;
            this.showValue(path, back, el);
            this.schedule(path, back, { commit: true, immediate: true });
          },
        },
      });
    }

    // Turning off an output's last route silences it, and the device can't report routing back (see routing.js).
    confirmRouteOff(el) {
      const guard = window.mdsp && window.mdsp.routing;
      if (!guard) return true;
      const o = el.dataset.routeOutput;
      const routes = [];
      for (const cell of this.root.querySelectorAll(`[data-route-output="${o}"]`)) {
        routes[Number(cell.dataset.routeInput)] = cell === el ? true : cell.checked;
      }
      const message = guard.disableWarning({
        routes,
        input: Number(el.dataset.routeInput),
        name: el.dataset.routeName || `Out ${Number(o) + 1}`,
        inputName: el.dataset.routeInputName,
        muted: this.desired[`outputs.${o}.mute`] === true,
        level: this.outputLevel(Number(o)),
      });
      return !message || window.confirm(message);
    }

    // Un-muting an output whose gain or mute was never confirmed on the device plays whatever it holds.
    confirmUnmute(el, path) {
      const side = path.replace(/\.mute$/, '');
      const unknown = [`${side}.gain_db`, `${side}.mute`].some((p) => (this.status[p] || 'unknown') === 'unknown');
      if (!unknown) return true;
      const name = el.dataset.confirmUnmute || 'this output';
      return window.confirm(`${name} has never been sent, so its level on the device is unknown. Un-mute it anyway?`);
    }

    onKey(e) {
      const el = e.target;
      if (!el.matches || !el.matches('input[type="range"][data-path]')) return;
      const dir = { ArrowUp: 1, ArrowRight: 1, ArrowDown: -1, ArrowLeft: -1 }[e.key];
      if (!dir) return;
      e.preventDefault();
      const step = e.shiftKey ? KEY_STEP_FINE : KEY_STEP;
      const min = parseFloat(el.min);
      const max = parseFloat(el.max);
      const value = round(Math.min(max, Math.max(min, (parseFloat(el.value) || 0) + dir * step)), 1);
      el.value = value;
      this.showValue(el.dataset.path, value, el);
      this.schedule(el.dataset.path, value, { commit: true });
    }

    // --- mono sum (phase9 design §10.1, P22): no default output, a preview, Apply, then an Undo toast -------------

    monoPlan() {
      const select = this.root.querySelector('[data-mono-target]');
      const button = this.root.querySelector('[data-mono-sum]');
      const guard = window.mdsp && window.mdsp.routing;
      if (!select || !button || !guard || select.value === '') return null;
      const o = Number(select.value);
      const routes = [];
      const paths = [];
      for (const cell of this.root.querySelectorAll(`[data-route-output="${o}"]`)) {
        const gainPath = cell.dataset.path.replace(/\.enabled$/, '.gain_db');
        const gainEl = this.controls(gainPath)[0];
        const gain = gainEl ? parseFloat(gainEl.value) : this.desired[gainPath];
        routes.push({ name: cell.dataset.routeInputName || `In ${Number(cell.dataset.routeInput) + 1}`, enabled: cell.checked, gain });
        paths.push({ enabled: cell.dataset.path, gain: gainPath });
      }
      if (!routes.length) return null;
      const output = select.options[select.selectedIndex].textContent.trim();
      return { output, ...guard.monoPlan({ output, routes, gain: Number(button.dataset.gain), paths }) };
    }

    updateMonoPreview() {
      const button = this.root.querySelector('[data-mono-sum]');
      const preview = this.root.querySelector('[data-mono-preview]');
      if (!button || !preview) return;
      const plan = this.monoPlan();
      button.disabled = !plan || !plan.changes.length;
      const text = plan ? plan.text : preview.dataset.emptyText || '';
      if (preview.textContent !== text) preview.textContent = text;
    }

    // The − / + beside a signed field (routing's route levels, design §16 phone rule 4): one 0.5 dB step within the
    // field's range, sent like a typed value (quick presses are gathered into one write).
    stepField(button) {
      const path = button.dataset.stepFor;
      const field = this.controls(path).find((el) => el.type === 'number');
      if (!field || field.disabled) return;
      const current = parseFloat(field.value);
      const base = Number.isFinite(current) ? current : this.desired[path];
      if (typeof base !== 'number') return;
      const min = parseFloat(field.min);
      const max = parseFloat(field.max);
      const value = round(Math.min(Number.isFinite(max) ? max : Infinity, Math.max(Number.isFinite(min) ? min : -Infinity, base + Number(button.dataset.stepDb))), 1);
      if (value === round(base, 1)) return;
      this.showValue(path, value);
      this.schedule(path, value, { commit: true });
    }

    onClick(e) {
      const step = e.target.closest && e.target.closest('[data-step-for]');
      if (step && this.root.contains(step) && !step.disabled) {
        this.stepField(step);
        return;
      }
      const button = e.target.closest && e.target.closest('[data-mono-sum]');
      if (!button || !this.root.contains(button) || button.disabled) return;
      const plan = this.monoPlan();
      if (!plan || !plan.changes.length) return;
      const before = {};
      for (const path of Object.keys(plan.values)) {
        const el = this.controls(path)[0];
        before[path] = el ? this.readValue(el) : this.desired[path];
      }
      for (const [path, value] of Object.entries(plan.values)) this.showValue(path, value);
      this.sendBatch(plan.values);
      this.updateMonoPreview();
      toast(`${plan.output} now plays every input at ${window.mdsp.fmtDb(Number(button.dataset.gain))}.`, 'info', {
        action: {
          label: 'Undo',
          returnFocus: this.root.querySelector('[data-mono-target]'),
          onClick: () => {
            for (const [path, value] of Object.entries(before)) this.showValue(path, value);
            this.sendBatch(before);
            this.updateMonoPreview();
          },
        },
      });
    }

    // --- sending -----------------------------------------------------------------------------------------------

    schedule(path, value, { commit = false, immediate = false } = {}) {
      this.localAt.set(path, performance.now());
      this.status[path] = 'pending';
      this.renderBadges();
      let ch = this.channels.get(path);
      if (!ch) {
        ch = { timer: null, inflight: false, queued: null, since: null };
        this.channels.set(path, ch);
      }
      clearTimeout(ch.timer);
      ch.timer = null;
      const enqueue = () => {
        ch.timer = null;
        ch.since = null;
        ch.queued = { value, commit: commit || !!(ch.queued && ch.queued.commit) };
        if (!ch.inflight) this.pump(path, ch);
      };
      // Trailing debounce with a maximum wait: values settle for 80 ms, but a drag never goes 250 ms unsent.
      const now = performance.now();
      if (ch.since === null) ch.since = now;
      const wait = Math.min(DEBOUNCE_MS, Math.max(0, ch.since + MAX_WAIT_MS - now));
      if (immediate || wait === 0) enqueue();
      else ch.timer = setTimeout(enqueue, wait);
    }

    async pump(path, ch) {
      ch.inflight = true;
      try {
        while (ch.queued) {
          const job = ch.queued;
          ch.queued = null;
          if (!job.commit && job.value === ch.lastSent) continue; // a drag that paused on the same value
          ch.lastSent = job.value;
          const outcome = await this.send({ [path]: job.value }, job.commit);
          if (outcome === 'rejected') {
            ch.lastSent = undefined;
            ch.queued = null; // the user cancelled or the server refused: drop what the drag queued meanwhile
            clearTimeout(ch.timer);
            ch.timer = null;
          }
        }
      } finally {
        ch.inflight = false;
      }
    }

    async sendBatch(values) {
      const now = performance.now();
      for (const path of Object.keys(values)) {
        this.localAt.set(path, now);
        this.status[path] = 'pending';
      }
      this.renderBadges();
      return this.send(values, true); // 'ok' or 'rejected' (cancelled at the confirmation, or refused)
    }

    async send(values, commit, confirm = false) {
      const startedAt = performance.now();
      let response;
      let data = null;
      try {
        response = await fetch(this.editUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'HX-Request': 'true', Accept: 'application/json' },
          body: JSON.stringify({ values, commit, confirm }),
          credentials: 'same-origin',
        });
        data = await response.json().catch(() => null);
      } catch {
        toast('Request failed: the web UI server is unreachable');
        this.restore(Object.keys(values), null, null);
        return 'rejected';
      }
      if (response.ok && data) {
        this.accept(data, startedAt);
        return 'ok';
      }
      if (response.status === 409 && data && data.error === 'confirmation_required' && !confirm) {
        if (window.confirm(`${data.detail}.\n\nApply it anyway?`)) return this.send(values, commit, true);
        this.restore(Object.keys(values), data.values, data.sync);
        return 'rejected';
      }
      const detail = data && (data.detail || (data.problems || []).join('; '));
      toast(detail || `Change refused (HTTP ${response.status})`);
      this.restore(Object.keys(values), data && data.values, data && data.sync);
      return 'rejected';
    }

    accept(data, startedAt) {
      for (const [path, value] of Object.entries(data.values || {})) {
        this.desired[path] = value;
        // The server may have normalised it (delays are rounded to whole samples).
        const ch = this.channels.get(path);
        const typing = document.activeElement && document.activeElement.dataset && document.activeElement.dataset.path === path;
        if (!(ch && (ch.queued || ch.timer)) && !(this.dragging && this.dragging.dataset.path === path) && !typing) {
          this.showValue(path, value);
        }
      }
      for (const [path, st] of Object.entries(data.sync || {})) {
        // The event stream may already have reported a newer status (the write can finish before this reply).
        if ((this.statusAt.get(path) || -Infinity) < startedAt) this.status[path] = st;
      }
      this.renderBadges();
    }

    restore(paths, values, statuses) {
      for (const path of paths) {
        const value = values && path in values ? values[path] : this.desired[path];
        if (values && path in values) this.desired[path] = values[path];
        this.localAt.delete(path);
        if (value !== undefined && value !== null) this.showValue(path, value);
        if (statuses && statuses[path]) this.status[path] = statuses[path];
      }
      this.renderBadges();
    }

    // For helpers (delay calculator): set a path as if the user had typed it.
    set(path, value) {
      this.showValue(path, value);
      this.schedule(path, value, { commit: true, immediate: true });
    }

    value(path) {
      return this.desired[path];
    }
  }

  function parseEvent(event) {
    if (!event || typeof event.data !== 'string') return null;
    try {
      return JSON.parse(event.data);
    } catch {
      return null;
    }
  }

  function init() {
    const root = document.querySelector('[data-params]');
    if (!root || root.mdspParams) return;
    root.mdspParams = new ParamsPage(root);
    window.mdsp = Object.assign(window.mdsp || {}, { params: root.mdspParams });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  // Delay tab helpers: distance (m/ft at a speed of sound) and "N m closer/farther than <output>" -> ms.
  document.addEventListener('alpine:init', () => {
    const FEET_PER_METRE = 3.28084;
    window.Alpine.data('delayHelper', (cfg) => ({
      metres: cfg.distance,
      feet: round(cfg.distance * FEET_PER_METRE, 2),
      speed: cfg.speed,
      relative: 1,
      direction: 'closer',
      other: cfg.others.length ? cfg.others[0].index : null,

      fromMetres() {
        if (Number.isFinite(this.metres)) this.feet = round(this.metres * FEET_PER_METRE, 2);
      },
      fromFeet() {
        if (Number.isFinite(this.feet)) this.metres = round(this.feet / FEET_PER_METRE, 3);
      },
      toSamples(ms) {
        return Math.round((ms * cfg.sampleRate) / 1000);
      },
      distanceMs() {
        if (!(this.metres >= 0) || !(this.speed > 0)) return NaN;
        return (this.metres / this.speed) * 1000;
      },
      otherDelay() {
        const page = window.mdsp && window.mdsp.params;
        const value = page ? page.value(`outputs.${this.other}.delay_ms`) : null;
        return typeof value === 'number' ? value : 0;
      },
      relativeMs() {
        if (!(this.relative >= 0) || !(this.speed > 0) || this.other === null) return NaN;
        const offset = (this.relative / this.speed) * 1000;
        return this.otherDelay() + (this.direction === 'closer' ? offset : -offset);
      },
      relativeHint() {
        const ms = this.relativeMs();
        const name = (cfg.others.find((o) => o.index === this.other) || {}).name || 'the other output';
        if (Number.isFinite(ms) && ms < 0) return `That is below zero: delay ${name} instead.`;
        return `${name} is delayed ${window.mdsp.fmtNum(this.otherDelay(), 2)} ms; a closer speaker needs that plus the extra travel time.`;
      },
      valid(ms) {
        return Number.isFinite(ms) && ms >= 0 && this.toSamples(ms) <= Math.round((cfg.maxMs * cfg.sampleRate) / 1000);
      },
      fmtMs(ms) {
        if (!Number.isFinite(ms)) return '—';
        return `${window.mdsp.fmtNum(ms, 2)} ms (${this.toSamples(ms)} samples)`;
      },
      apply(ms) {
        if (!this.valid(ms)) return;
        const page = window.mdsp && window.mdsp.params;
        if (page) page.set(cfg.path, (this.toSamples(ms) * 1000) / cfg.sampleRate);
      },
    }));
  });
})();
