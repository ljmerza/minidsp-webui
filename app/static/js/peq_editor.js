// Filter editors (docs 05 §4-5): shared helpers (window.mdsp.filters), the PEQ editor and the server-computed response
// views (output Response tab, Dashboard widget). crossover.js builds on the helpers, so load this file first.
//
// Needs uPlot, dsp.js and graph.js. While a handle is dragged (or a field typed) the curve is a dsp.js preview; on
// release the band goes to the server, which compiles and checks it, and its coefficients replace the preview. A
// refused change puts the stored band back. Sync badges follow the writer's "sync" events, and "desired" events from
// other tabs reload the bank.
(() => {
  'use strict';

  // The status model's order (app/sync/status.py); "changed" (≠) and "waiting" (⏸) come from the page's status overlay.
  const ICON = { synced: '✓', pending: '⟳', failed: '⚠', unknown: '?', changed: '≠', waiting: '⏸' };
  const RANK = { synced: 0, unknown: 1, waiting: 2, pending: 3, changed: 4, failed: 5 };
  const BAND_COLORS = [
    '#4ea1ff', '#ff7a59', '#3ccf7a', '#f0b429', '#a78bfa',
    '#22c3c3', '#f472b6', '#facc15', '#94a3b8', '#fb923c',
  ];
  const GAIN_TYPES = new Set(['peaking', 'lowshelf', 'highshelf', 'gain']);
  const FREQ_TYPES = new Set(['peaking', 'lowshelf', 'highshelf', 'lp1', 'hp1', 'lp2', 'hp2', 'bandpass', 'notch', 'ap1', 'ap2']);
  const Q_TYPES = new Set(['peaking', 'lowshelf', 'highshelf', 'lp2', 'hp2', 'bandpass', 'notch', 'ap2']);
  const FREQ_MIN = 5;
  const FREQ_MAX = 24000;
  const Q_MIN = 0.1;
  const Q_MAX = 30;
  const GAIN_MIN = -30;
  const GAIN_MAX = 20;
  const WHEEL_COMMIT_MS = 400;
  const WHEEL_RADIUS_PX = 30;
  const RELOAD_DELAY_MS = 350;
  const KEYS = ['b0', 'b1', 'b2', 'a1', 'a2'];

  const mdsp = () => window.mdsp || {};
  const toast = (message, level) => mdsp().toast && mdsp().toast(message, level);
  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  const round = (v, decimals) => {
    const f = 10 ** decimals;
    return Math.round(v * f) / f;
  };
  // Units rule (docs 05): frequency to 0.1 Hz below 100 Hz, Q to 2 decimals, gain to 0.1 dB.
  const roundFreq = (f) => (f < 100 ? round(f, 1) : round(f, 0));
  const roundQ = (q) => round(q, 2);
  const roundGain = (g) => round(g, 1);
  const num = (v) => (v === null || v === undefined || v === '' ? null : Number(v));

  // A signed number typed into a text field (phase 9 P01). A number <input> reports a lone "-" as "" and Alpine's
  // x-model.number then wipes it, so "-4" typed key by key arrived as +4. Signed fields are therefore text fields
  // parsed here: a half-typed sign or point ("-", "-.", "+", ".") is "partial" and changes nothing, so it can't flip
  // the stored value, and a commit is refused until the text is a whole number. Accepts the UI's own minus sign
  // (U+2212, from app.js fmtNum) and a decimal comma. Returns { kind: 'empty' | 'partial' | 'number' | 'invalid',
  // value }.
  const SIGNED = /^[+-]?(\d+\.?\d*|\.\d+)$/;
  const SIGNED_PARTIAL = /^[+-]?\.?$/;
  function parseSigned(text) {
    if (typeof text === 'number') return Number.isFinite(text) ? { kind: 'number', value: text } : { kind: 'invalid', value: null };
    const s = String(text ?? '').trim().replace(/[\u2212\u2012\u2013\ufe63\uff0d]/g, '-').replace(',', '.');
    if (s === '') return { kind: 'empty', value: null };
    if (SIGNED_PARTIAL.test(s)) return { kind: 'partial', value: null };
    if (!SIGNED.test(s)) return { kind: 'invalid', value: null };
    const value = Number(s);
    return { kind: 'number', value: Object.is(value, -0) ? 0 : value };
  }

  // A number for a text field: plain ASCII, no trailing zeros ("-4", "4.5"), "" for none.
  const fieldText = (v) => (v === null || v === undefined || !Number.isFinite(Number(v)) ? '' : String(Number(v) + 0));

  // JSON request to the /ui routes; never throws. { ok, status, data }.
  async function request(url, { method = 'GET', body } = {}) {
    const headers = { Accept: 'application/json' };
    if (method !== 'GET') Object.assign(headers, { 'Content-Type': 'application/json', 'HX-Request': 'true' });
    let response;
    try {
      response = await fetch(url, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: 'same-origin',
      });
    } catch {
      return { ok: false, status: 0, data: { detail: 'the web UI server is unreachable' } };
    }
    const data = await response.json().catch(() => null);
    return { ok: response.ok, status: response.status, data };
  }

  // A JSON request that may need confirmation (HTTP 409 confirmation_required): asks, then repeats with confirm.
  async function send(url, body) {
    let res = await request(url, { method: 'POST', body });
    if (res.status === 409 && res.data && res.data.error === 'confirmation_required') {
      if (!window.confirm(`${res.data.detail}.\n\nApply it anyway?`)) return { ...res, cancelled: true };
      res = await request(url, { method: 'POST', body: { ...body, confirm: true } });
    }
    return res;
  }

  function errorText(res) {
    const d = res.data || {};
    return d.detail || (d.problems || []).join('; ') || `Change refused (HTTP ${res.status})`;
  }

  function parseEvent(e) {
    const raw = e && e.detail && e.detail.data;
    if (typeof raw !== 'string') return null;
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  }

  function worst(statuses) {
    let w = null;
    for (const st of statuses) if (w === null || RANK[st] > RANK[w]) w = st;
    return w || 'unknown';
  }

  // Merge a "sync" event into a {path: status} map; true when a path under `prefix` changed.
  function applySync(sync, data, prefix) {
    let touched = false;
    for (const [path, st] of Object.entries(data.changes || {})) {
      if (path.startsWith(prefix)) {
        sync[path] = st;
        touched = true;
      }
    }
    for (const path of data.removed || []) {
      if (path.startsWith(prefix) && path in sync) {
        delete sync[path];
        touched = true;
      }
    }
    return touched;
  }

  // Remembered per component kind (graph view, mode, contribution toggle).
  function remember(key, value) {
    try {
      if (value === undefined) return window.localStorage.getItem(`mdsp.${key}`);
      window.localStorage.setItem(`mdsp.${key}`, String(value));
    } catch {
      /* storage disabled */
    }
    return null;
  }

  function biquadFromForm(form) {
    const out = {};
    for (const k of KEYS) {
      const text = String(form[k] ?? '').trim();
      const v = text === '' ? NaN : Number(text);
      if (!Number.isFinite(v)) return null;
      out[k] = v;
    }
    return out;
  }

  // Stability text for a raw-coefficient row: { ok, text }.
  function stability(biquad) {
    const dsp = mdsp().dsp;
    if (!biquad) return { ok: false, text: 'every coefficient must be a number' };
    const problem = dsp.biquadProblem(biquad);
    if (problem) return { ok: false, text: problem };
    return { ok: true, text: `stable · pole radius ${dsp.poleRadius(biquad.a1, biquad.a2).toFixed(4)}` };
  }

  const fullPrecision = (bq) => Object.fromEntries(KEYS.map((k) => [k, String(bq[k])]));

  function createPlot(el, opts) {
    const graph = mdsp().graph;
    if (!graph || !window.uPlot || !el) return null;
    try {
      return graph.create(el, opts);
    } catch (e) {
      toast(`Graph unavailable: ${e.message}`);
      return null;
    }
  }

  // The plotting area's pointer position, clamped inside it, as {freq, value}.
  function pointerData(plot, clientX, clientY) {
    const u = plot.plot;
    const rect = u.over.getBoundingClientRect();
    const x = clamp(clientX - rect.left, 0, rect.width);
    const y = clamp(clientY - rect.top, 0, rect.height);
    return { freq: u.posToVal(x, 'x'), value: u.posToVal(y, 'y') };
  }

  // The view a graph opens in when none was remembered for it: Settings' default range, else the page's own.
  const pageView = (fallback) => (mdsp().graph ? mdsp().graph.defaultView(fallback) : fallback);

  const helpers = {
    ICON, RANK, BAND_COLORS, request, send, errorText, parseEvent, worst, applySync, remember, biquadFromForm,
    stability, fullPrecision, createPlot, pageView, pointerData, roundFreq, roundQ, roundGain, clamp, parseSigned,
    fieldText,
  };
  window.mdsp = Object.assign(window.mdsp || {}, { filters: helpers });

  // --- PEQ editor ------------------------------------------------------------------------------------------------

  // The gain is shown as Cut / Boost plus a magnitude (phase 9 P01/P07): no minus key needed. `gain_db` stays the
  // signed value the preview, the graph and the server use; `cut` and `mag` (text) follow it. 0 dB counts as Cut, so
  // a magnitude typed into a fresh band lowers rather than raises.
  function setGainFields(form) {
    const g = num(form.gain_db);
    if (g === null || !Number.isFinite(g)) {
      form.mag = '';
      return;
    }
    form.cut = g < 0 || (g === 0 && form.cut !== false);
    form.mag = fieldText(Math.abs(g));
  }

  const signedGain = (cut, magnitude) => (magnitude === 0 ? 0 : cut ? -magnitude : magnitude);

  function formFromDesign(design) {
    const lt = design.lt || null;
    const form = {
      type: design.type,
      freq: design.freq ?? null,
      q: design.q ?? null,
      gain_db: design.gain_db ?? null,
      cut: true,
      mag: '',
      lt: lt ? { ...lt } : { f0: 30, q0: 0.7, fp: 15, qp: 0.5 },
    };
    setGainFields(form);
    return form;
  }

  function designFromForm(form) {
    const type = form.type;
    const d = { type };
    if (FREQ_TYPES.has(type)) d.freq = num(form.freq);
    if (Q_TYPES.has(type)) d.q = num(form.q);
    if (GAIN_TYPES.has(type)) d.gain_db = num(form.gain_db);
    if (type === 'lt') d.lt = Object.fromEntries(Object.entries(form.lt).map(([k, v]) => [k, num(v)]));
    return d;
  }

  function peqEditor(cfg) {
    // Kept outside Alpine's reactive proxy: the uPlot instance and pointer state.
    const ctl = {
      plot: null,
      grid: null,
      frame: null,
      drag: null,
      pointers: new Map(), // touch pointers, for pinch
      pinch: null,
      wheel: null,
      wheelTimer: null,
      reloadTimer: null,
    };

    return {
      cfg,
      bands: [],
      sync: {},
      loaded: false,
      error: null,
      warnings: [],
      selected: null,
      drawer: null,
      raw: { b0: '', b1: '', b2: '', a1: '', a2: '' },
      view: remember(`peq.view.${cfg.side}`) || pageView(cfg.view || 'full'),
      prefix: `${cfg.side}.${cfg.channel}.peq`,

      init() {
        this.$el.addEventListener('sse:sync', (e) => this.onSync(e));
        this.$el.addEventListener('sse:desired', (e) => this.onDesired(e));
        this.$nextTick(() => {
          ctl.plot = createPlot(this.$refs.graph, { view: this.view, mode: 'magnitude', height: cfg.height || 280 });
          this.bindGraph();
          this.load();
        });
      },

      destroy() {
        if (ctl.plot) ctl.plot.destroy();
      },

      // --- state -------------------------------------------------------------------------------------------------

      async load() {
        const res = await request(cfg.stateUrl);
        if (!res.ok) {
          this.error = errorText(res);
          return;
        }
        this.error = null;
        this.sync = res.data.sync || {};
        const busy = new Set(this.bands.filter((b) => this.isBusy(b)).map((b) => b.index));
        this.bands = res.data.bands.map((b) => (busy.has(b.index) ? this.bands[b.index] : this.fromServer(b)));
        this.loaded = true;
        this.render();
      },

      fromServer(b, previous = null) {
        return {
          ...b,
          server: b,
          form: formFromDesign(b.design),
          preview: null,
          invalid: null,
          dirty: false, // edited in the form or on the graph, not committed yet
          inflight: false,
          queued: previous ? previous.queued : null,
        };
      },

      replaceBand(view) {
        const k = view.index;
        const previous = this.bands[k];
        const next = this.fromServer(view, previous);
        next.inflight = previous ? previous.inflight : false;
        this.bands.splice(k, 1, next);
        if (this.drawer === k && view.design.type !== 'lt') this.raw = fullPrecision(view.compiled);
      },

      onSync(e) {
        const data = parseEvent(e);
        if (!data || data.preset !== cfg.preset) return;
        applySync(this.sync, data, `${this.prefix}.`);
      },

      onDesired(e) {
        const data = parseEvent(e);
        if (!data || data.preset !== cfg.preset || data.event !== 'edit') return;
        if (!(data.paths || []).some((p) => p.startsWith(`${this.prefix}.`))) return;
        clearTimeout(ctl.reloadTimer);
        ctl.reloadTimer = setTimeout(() => this.load(), RELOAD_DELAY_MS);
      },

      // --- table helpers -----------------------------------------------------------------------------------------

      color(k) {
        return BAND_COLORS[k % BAND_COLORS.length];
      },
      uses(band, field) {
        const t = band.form.type;
        if (field === 'freq') return FREQ_TYPES.has(t);
        if (field === 'q') return Q_TYPES.has(t);
        return GAIN_TYPES.has(t);
      },
      locked(band) {
        return band.external;
      },
      status(band) {
        return worst([this.sync[`${band.path}.compiled`] || 'unknown', this.sync[`${band.path}.bypass`] || 'unknown']);
      },
      statusIcon(band) {
        return ICON[this.status(band)];
      },
      statusTitle(band) {
        const st = this.status(band);
        return `${st.charAt(0).toUpperCase()}${st.slice(1)}`;
      },
      sourceText(band) {
        if (band.preview || band.inflight) return 'editing';
        return band.source;
      },
      isActive(band) {
        return !band.bypass && band.form.type !== 'passthrough';
      },
      // Unused (flat) or switched-off bands say "off" instead of a sync glyph (phase 9 P07): a ✓ on a band that is
      // off made edits look applied.
      isOff(band) {
        return band.bypass || band.design.type === 'passthrough';
      },
      statusText(band) {
        return this.isOff(band) ? 'off' : this.status(band);
      },
      isDragging(band) {
        return ctl.drag !== null && ctl.drag.index === band.index;
      },
      isEditing(band) {
        const k = band.index;
        return Boolean(
          band.dirty || this.isDragging(band) || (ctl.wheel && ctl.wheel.index === k) || (ctl.pinch && ctl.pinch.index === k),
        );
      },
      // A band a reload must not replace: a request is on its way, or the user has an uncommitted edit (a drag, a
      // pinch, wheel steps, a typed value not yet committed).
      isBusy(band) {
        return Boolean(band.inflight || band.queued || this.isEditing(band));
      },
      select(k) {
        this.selected = k;
        this.render();
      },

      // --- preview -----------------------------------------------------------------------------------------------

      // The biquad the graph shows for a band: the preview while editing, else the stored coefficients.
      biquad(band) {
        return band.preview || band.compiled;
      },

      updatePreview(band) {
        const dsp = mdsp().dsp;
        if (!dsp) return false;
        try {
          band.preview = dsp.designFilter(designFromForm(band.form), cfg.fs || dsp.FS);
          band.invalid = null;
          return true;
        } catch (e) {
          band.invalid = e.message;
          return false;
        }
      },

      grid() {
        const dsp = mdsp().dsp;
        if (!ctl.grid || ctl.grid.view !== this.view) ctl.grid = { view: this.view, freqs: dsp.viewGrid(this.view) };
        return ctl.grid.freqs;
      },

      render() {
        const dsp = mdsp().dsp;
        if (!ctl.plot || !dsp) return;
        const freqs = this.grid();
        const active = this.bands.filter((b) => this.isActive(b));
        const traces = active.map((b) => ({
          id: `b${b.index}`,
          label: `Band ${b.index + 1}`,
          color: this.color(b.index),
          width: this.selected === b.index ? 2 : 1.1,
          response: dsp.filterResponse([this.biquad(b)], freqs),
        }));
        const composite = dsp.filterResponse(active.map((b) => this.biquad(b)), freqs, { label: 'PEQ' });
        traces.push({ id: 'sum', label: `${cfg.name} PEQ`, color: cfg.color, bold: true, response: composite });
        ctl.plot.setTraces(traces);
        this.warnings = composite.warnings;
        ctl.plot.setHandles(
          active
            .filter((b) => FREQ_TYPES.has(b.form.type) && num(b.form.freq) > 0)
            .map((b) => ({
              id: b.index,
              freq: num(b.form.freq),
              value: GAIN_TYPES.has(b.form.type) ? num(b.form.gain_db) || 0 : 0,
              color: this.color(b.index),
              active: this.selected === b.index,
              disabled: b.external,
            })),
        );
      },

      setView(view) {
        this.view = view;
        remember(`peq.view.${cfg.side}`, view);
        if (ctl.plot) ctl.plot.setView(view);
        this.render();
      },
      autoFit() {
        if (ctl.plot) ctl.plot.autoFit();
      },
      resetY() {
        if (ctl.plot) ctl.plot.resetY();
      },

      // --- graph interaction -------------------------------------------------------------------------------------

      bindGraph() {
        const el = this.$refs.graph;
        if (!el || !ctl.plot) return;
        el.addEventListener('pointerdown', (e) => this.onPointerDown(e));
        el.addEventListener('pointermove', (e) => this.onPointerMove(e));
        el.addEventListener('pointerup', (e) => this.onPointerUp(e));
        el.addEventListener('pointercancel', (e) => this.onPointerUp(e));
        el.addEventListener('wheel', (e) => this.onWheel(e), { passive: false });
      },

      bandAt(clientX, clientY) {
        const h = ctl.plot && ctl.plot.handleAt(clientX, clientY);
        if (!h || h.disabled) return null;
        return this.bands[h.id] || null;
      },

      onPointerDown(e) {
        if (e.pointerType === 'touch') ctl.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
        if (ctl.pointers.size === 2 && this.selected !== null) {
          this.startPinch();
          e.preventDefault();
          return;
        }
        const band = this.bandAt(e.clientX, e.clientY);
        if (!band) return;
        e.preventDefault();
        this.$refs.graph.setPointerCapture(e.pointerId);
        this.selected = band.index;
        // Unrounded position, so slow (Shift) drags still move although the fields show rounded values.
        ctl.drag = {
          index: band.index,
          pointerId: e.pointerId,
          last: pointerData(ctl.plot, e.clientX, e.clientY),
          freq: num(band.form.freq),
          gain: num(band.form.gain_db) || 0,
          moved: false,
        };
        this.render();
      },

      onPointerMove(e) {
        if (ctl.pointers.has(e.pointerId)) ctl.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
        if (ctl.pinch) {
          this.movePinch(e.shiftKey);
          return;
        }
        const drag = ctl.drag;
        if (!drag || drag.pointerId !== e.pointerId) return;
        const band = this.bands[drag.index];
        const cur = pointerData(ctl.plot, e.clientX, e.clientY);
        const fine = e.shiftKey ? 0.1 : 1;
        drag.freq = clamp(drag.freq * (cur.freq / drag.last.freq) ** fine, FREQ_MIN, Math.min(FREQ_MAX, (cfg.fs || 96000) * 0.45));
        band.form.freq = roundFreq(drag.freq);
        if (GAIN_TYPES.has(band.form.type)) {
          drag.gain = clamp(drag.gain + (cur.value - drag.last.value) * fine, GAIN_MIN, GAIN_MAX);
          band.form.gain_db = roundGain(drag.gain);
          setGainFields(band.form);
        }
        drag.last = cur;
        drag.moved = true;
        band.dirty = true;
        this.updatePreview(band);
        this.scheduleRender();
      },

      onPointerUp(e) {
        ctl.pointers.delete(e.pointerId);
        if (ctl.pinch) {
          if (ctl.pointers.size < 2) this.endPinch();
          return;
        }
        const drag = ctl.drag;
        if (!drag || drag.pointerId !== e.pointerId) return;
        ctl.drag = null;
        if (drag.moved) this.commitForm(this.bands[drag.index]);
      },

      startPinch() {
        const band = this.bands[this.selected];
        if (!band || band.external || !Q_TYPES.has(band.form.type)) return;
        const [a, b] = [...ctl.pointers.values()];
        ctl.drag = null;
        ctl.pinch = { index: band.index, dist: Math.hypot(a.x - b.x, a.y - b.y) || 1, q: num(band.form.q) || 0.707 };
      },

      movePinch(fine) {
        const p = ctl.pinch;
        const [a, b] = [...ctl.pointers.values()];
        if (!a || !b) return;
        const band = this.bands[p.index];
        const ratio = (Math.hypot(a.x - b.x, a.y - b.y) || 1) / p.dist;
        band.form.q = roundQ(clamp(p.q * ratio ** (fine ? 0.1 : 1), Q_MIN, Q_MAX));
        band.dirty = true;
        this.updatePreview(band);
        this.scheduleRender();
      },

      endPinch() {
        const p = ctl.pinch;
        ctl.pinch = null;
        if (p) this.commitForm(this.bands[p.index]);
      },

      // Wheel over a handle = Q (the page scrolls normally elsewhere).
      onWheel(e) {
        const h = ctl.plot && ctl.plot.handleAt(e.clientX, e.clientY, WHEEL_RADIUS_PX);
        const band = h && !h.disabled ? this.bands[h.id] : null;
        if (!band || !Q_TYPES.has(band.form.type)) return;
        e.preventDefault();
        if (this.selected !== band.index || !ctl.wheel || ctl.wheel.index !== band.index) {
          ctl.wheel = { index: band.index, q: num(band.form.q) || 0.707 };
        }
        this.selected = band.index;
        const delta = e.deltaY || e.deltaX; // Shift+wheel scrolls sideways in some browsers
        const k = e.shiftKey ? 0.00015 : 0.0015;
        ctl.wheel.q = clamp(ctl.wheel.q * Math.exp(-delta * k), Q_MIN, Q_MAX);
        band.form.q = roundQ(ctl.wheel.q);
        band.dirty = true;
        this.updatePreview(band);
        this.scheduleRender();
        clearTimeout(ctl.wheelTimer);
        const index = band.index;
        ctl.wheelTimer = setTimeout(() => {
          ctl.wheel = null;
          this.commitForm(this.bands[index]); // re-read: the band object may have been replaced meanwhile
        }, WHEEL_COMMIT_MS);
      },

      scheduleRender() {
        if (ctl.frame) return;
        ctl.frame = requestAnimationFrame(() => {
          ctl.frame = null;
          this.render();
        });
      },

      // --- table edits -------------------------------------------------------------------------------------------

      onFieldInput(band) {
        this.selected = band.index;
        band.dirty = true;
        this.updatePreview(band);
        this.scheduleRender();
      },

      // The magnitude field while typing: a whole number updates the preview (a typed "-" also picks Cut, a "+"
      // Boost, a bare number keeps the current choice); a half-typed "-" or "." changes nothing.
      onGainInput(band) {
        const parsed = parseSigned(band.form.mag);
        band.dirty = true;
        this.selected = band.index;
        if (parsed.kind !== 'number') return;
        const text = String(band.form.mag).trim();
        if (parsed.value < 0) band.form.cut = true;
        else if (/^\+/.test(text)) band.form.cut = false;
        band.form.gain_db = signedGain(band.form.cut, Math.abs(parsed.value));
        this.onFieldInput(band);
      },

      // Change/blur on the magnitude field: refused (and put back) unless the text is a whole number.
      commitGain(band) {
        if (!band || band.external) return;
        const parsed = parseSigned(band.form.mag);
        if (parsed.kind !== 'number') {
          toast(`Band ${band.index + 1}: type how many dB to cut or boost, for example 4`);
          band.dirty = false;
          this.revert(band);
          return;
        }
        this.onGainInput(band);
        this.commitForm(band);
      },

      // The Cut / Boost choice: flips the sign of the current magnitude and sends it when that changes the gain.
      setCut(band, cut) {
        if (!band || band.external) return;
        band.form.cut = cut;
        const g = num(band.form.gain_db);
        if (g === null || g === 0) return;
        band.form.gain_db = signedGain(cut, Math.abs(g));
        this.selected = band.index;
        this.commitForm(band);
      },

      onTypeChange(band) {
        const t = band.form.type;
        const fallback = this.view === 'sub' ? 60 : 1000;
        if (FREQ_TYPES.has(t) && !(num(band.form.freq) > 0)) band.form.freq = fallback;
        if (Q_TYPES.has(t) && !(num(band.form.q) > 0)) band.form.q = 0.71;
        if (GAIN_TYPES.has(t) && num(band.form.gain_db) === null) band.form.gain_db = 0;
        if (!FREQ_TYPES.has(t)) band.form.freq = null;
        if (!Q_TYPES.has(t)) band.form.q = null;
        if (!GAIN_TYPES.has(t)) band.form.gain_db = null;
        setGainFields(band.form);
        this.selected = band.index;
        if (t === 'raw' || t === 'lt') {
          this.openDrawer(band); // applied from the drawer
          return;
        }
        this.commitForm(band);
      },

      setEnabled(band, on) {
        this.queue(band, { bypass: !on });
      },

      resetBand(band) {
        band.form = formFromDesign({ type: 'passthrough' });
        band.preview = null;
        this.queue(band, { design: { type: 'passthrough' }, bypass: true });
      },

      // Typed values are clamped to the fields' ranges (the inputs' min/max don't stop a typed value).
      clampForm(form) {
        const fMax = Math.min(FREQ_MAX, (cfg.fs || 96000) * 0.45);
        const fit = (key, lo, hi, rounder) => {
          const v = num(form[key]);
          if (v !== null && Number.isFinite(v)) form[key] = rounder(clamp(v, lo, hi));
        };
        if (FREQ_TYPES.has(form.type)) fit('freq', FREQ_MIN, fMax, roundFreq);
        if (Q_TYPES.has(form.type)) fit('q', Q_MIN, Q_MAX, roundQ);
        if (GAIN_TYPES.has(form.type)) fit('gain_db', GAIN_MIN, GAIN_MAX, roundGain);
        if (GAIN_TYPES.has(form.type)) setGainFields(form);
      },

      // Choosing a filter for an unused band turns it on in the same request (phase 9 P07): before, a band picked
      // from "— unused" stayed bypassed, so its edits seemed to do nothing. An all-zero raw slot is unused too: a
      // factory device and a readback store zeros there, and the table shows it as "Raw".
      turnOn(band, type) {
        const d = band.server && band.server.design;
        const zeros = d && d.type === 'raw' && d.raw && ['b0', 'b1', 'b2'].every((k) => Number(d.raw[k]) === 0);
        const unused = d && (d.type === 'passthrough' || zeros);
        return unused && band.bypass && type !== 'passthrough' ? { bypass: false } : {};
      },

      commitForm(band) {
        if (!band || band.external) return;
        band.dirty = false;
        this.clampForm(band.form);
        if (!this.updatePreview(band)) {
          toast(`Band ${band.index + 1}: ${band.invalid}`);
          this.revert(band);
          return;
        }
        this.queue(band, { design: designFromForm(band.form), ...this.turnOn(band, band.form.type) });
      },

      // --- raw drawer --------------------------------------------------------------------------------------------

      openDrawer(band) {
        if (this.drawer === band.index && band.form.type !== 'raw' && band.form.type !== 'lt') {
          this.drawer = null;
          return;
        }
        this.drawer = band.index;
        this.raw = fullPrecision(band.compiled);
      },
      closeDrawer() {
        const band = this.drawer === null ? null : this.bands[this.drawer];
        this.drawer = null;
        if (!band || band.inflight || band.queued) return;
        const unapplied = (band.form.type === 'raw' || band.form.type === 'lt') && band.form.type !== band.design.type;
        if (unapplied || band.dirty) this.revert(band); // a type picked or a value previewed but never applied
      },
      rawStability() {
        return stability(biquadFromForm(this.raw));
      },
      applyRaw(band) {
        const bq = biquadFromForm(this.raw);
        const check = stability(bq);
        if (!check.ok) {
          toast(`Band ${band.index + 1}: ${check.text}`);
          return;
        }
        band.preview = bq;
        this.render();
        this.queue(band, { raw: bq, ...this.turnOn(band, 'raw') });
      },
      applyLt(band) {
        band.form.type = 'lt';
        this.commitForm(band);
      },
      previewLt(band) {
        if (band.form.type !== 'lt') return;
        this.onFieldInput(band);
      },

      // --- bank actions ------------------------------------------------------------------------------------------

      async bankAction(action) {
        if (action === 'reset_all' && !window.confirm(`Reset every ${cfg.name} PEQ band to flat and bypassed?`)) return;
        const res = await send(cfg.bankUrl, { action });
        if (!res.ok) {
          if (!res.cancelled) toast(errorText(res));
          return;
        }
        this.sync = res.data.sync || {};
        this.bands = res.data.bands.map((b) => this.fromServer(b));
        this.render();
      },

      // --- sending -----------------------------------------------------------------------------------------------

      // One request per band at a time. Waiting changes are merged (the newest design or raw replaces an older one,
      // a bypass change is kept), so a toggle made while a design is on its way is not lost.
      queue(band, body) {
        const merged = { ...(band.queued || {}) };
        if ('design' in body || 'raw' in body) {
          delete merged.design;
          delete merged.raw;
        }
        band.queued = { ...merged, ...body };
        for (const p of [`${band.path}.compiled`, `${band.path}.bypass`]) if (p in this.sync) this.sync[p] = 'pending';
        if (!band.inflight) this.pump(band.index);
      },

      async pump(k) {
        let band = this.bands[k];
        band.inflight = true;
        try {
          while (band.queued) {
            const body = { ...band.queued, commit: true };
            band.queued = null;
            const res = await send(cfg.bandUrl.replace('{band}', String(k)), body);
            band = this.bands[k];
            if (res.ok) {
              Object.assign(this.sync, res.data.sync || {});
              if (!band.queued && !this.isEditing(band)) this.replaceBand(res.data.band);
              else Object.assign(band, res.data.band, { server: res.data.band }); // keeps the form being edited
            } else {
              if (!res.cancelled) toast(`Band ${k + 1}: ${errorText(res)}`);
              band.queued = null;
              if (res.data && res.data.band) this.replaceBand(res.data.band);
              else this.revert(band);
            }
            band = this.bands[k];
          }
        } finally {
          this.bands[k].inflight = false;
          this.render();
        }
      },

      revert(band) {
        const k = band.index;
        const next = this.fromServer(band.server);
        next.inflight = band.inflight;
        this.bands.splice(k, 1, next);
        this.render();
      },
    };
  }

  // --- server-computed responses (Response tab, Dashboard widget) ---------------------------------------------------

  // cfg: { url, key, view, mode, contributions (bool: offer the toggle), inputs (bool: offer the input select),
  //        preset (null = follow the device's active preset), refresh: ['desired' | 'sync' | 'master'] }
  function responseView(cfg) {
    const ctl = { plot: null, timer: null, seq: 0, traces: [] };
    const stored = (name, fallback) => remember(`${cfg.key}.${name}`) ?? fallback;

    return {
      title: cfg.title || '',
      view: stored('view', pageView(cfg.view || 'full')),
      mode: stored('mode', 'magnitude'),
      unwrap: stored('unwrap', 'false') === 'true',
      parts: stored('parts', 'false') === 'true',
      inputs: cfg.defaultInputs || '0',
      warnings: [],
      reading: '', // the one-line text equivalent (app/web/graph_reading.py)
      hasSum: false,
      showSum: true, // off by default when a plotted output is Shakers (tactile), until the user picks
      error: null,
      loading: false,
      empty: false,
      preset: cfg.preset,

      init() {
        for (const topic of cfg.refresh || []) this.$el.addEventListener(`sse:${topic}`, (e) => this.onEvent(topic, e));
        this.$nextTick(() => {
          ctl.plot = createPlot(this.$refs.graph, {
            view: this.view,
            mode: this.mode,
            unwrap: this.unwrap,
            height: cfg.height || 280,
            label: cfg.label,
          });
          this.load();
        });
      },

      destroy() {
        if (ctl.plot) ctl.plot.destroy();
      },

      url() {
        const u = new URL(cfg.url, window.location.href);
        u.searchParams.set('range', this.view);
        if (cfg.inputs) u.searchParams.set('inputs', this.inputs);
        return u.pathname + u.search;
      },

      async load() {
        const seq = ++ctl.seq;
        this.loading = true;
        const res = await request(this.url());
        if (seq !== ctl.seq) return; // a newer request is on its way
        this.loading = false;
        if (!res.ok) {
          this.error = errorText(res);
          return;
        }
        this.error = null;
        if (res.data.title) this.title = res.data.title;
        if (typeof res.data.preset === 'number') this.preset = res.data.preset;
        ctl.traces = res.data.traces || [];
        this.empty = ctl.traces.length === 0;
        this.warnings = res.data.warnings || [];
        this.reading = res.data.reading || '';
        this.hasSum = ctl.traces.some((t) => t.kind === 'sum');
        const sum = remember(`${cfg.key}.sum`);
        this.showSum = sum === null ? !(res.data.tactile || []).length : sum === 'true';
        this.draw();
      },

      draw() {
        if (!ctl.plot) return;
        const visible = (t) => (t.kind === 'part' ? this.parts : t.kind === 'sum' ? this.showSum : true);
        ctl.plot.setTraces(ctl.traces.map((t) => ({ ...t, width: t.kind === 'part' ? 1.2 : undefined, visible: visible(t) })));
      },

      onEvent(topic, e) {
        const data = parseEvent(e);
        if (!data) return;
        if (topic === 'master') {
          const p = data.master && data.master.preset;
          if (cfg.preset !== null || typeof p !== 'number' || p === this.preset) return;
        } else if (data.preset !== undefined && data.preset !== this.preset) {
          return;
        }
        clearTimeout(ctl.timer);
        ctl.timer = setTimeout(() => this.load(), topic === 'desired' ? RELOAD_DELAY_MS : 1000);
      },

      setMode(mode) {
        this.mode = mode;
        remember(`${cfg.key}.mode`, mode);
        if (ctl.plot) ctl.plot.setMode(mode, { unwrap: this.unwrap });
      },
      setUnwrap() {
        remember(`${cfg.key}.unwrap`, this.unwrap);
        if (ctl.plot) ctl.plot.setUnwrap(this.unwrap);
      },
      setParts() {
        remember(`${cfg.key}.parts`, this.parts);
        this.draw();
      },
      setSum() {
        remember(`${cfg.key}.sum`, this.showSum);
        this.draw();
      },
      setView(view) {
        this.view = view;
        remember(`${cfg.key}.view`, view);
        if (ctl.plot) ctl.plot.setView(view);
        this.load();
      },
      autoFit() {
        if (ctl.plot) ctl.plot.autoFit();
      },
      resetY() {
        if (ctl.plot) ctl.plot.resetY();
      },
    };
  }

  document.addEventListener('alpine:init', () => {
    window.Alpine.data('peqEditor', peqEditor);
    window.Alpine.data('responseView', responseView);
  });
})();
