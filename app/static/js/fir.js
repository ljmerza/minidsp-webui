// Output FIR tab (docs 05 §4): upload a 96 kHz mono WAV, preview its impulse and magnitude/phase, load it to the
// device (confirmed: the device mutes while it loads), enable/bypass and clear. Talks to app/api/v1/compressor_fir.py.
//
// An upload is only stored and previewed; nothing reaches the device until "Load to device" is confirmed.
(() => {
  'use strict';

  const ICON = { synced: '✓', pending: '⟳', failed: '⚠', unknown: '?' };
  const RANK = { synced: 0, unknown: 1, pending: 2, failed: 3 };
  const RELOAD_DELAY_MS = 350;
  const PHASE_FLOOR_DB = -60;
  const MODE_TITLES = { magnitude: 'Magnitude (dB)', phase: 'Phase (°)', excess: 'Phase with the latency removed (°)' };
  const loadWarning = (preview) => {
    const db = preview && preview.peak_gain_db;
    const gain = typeof db === 'number' ? ` (peak gain ${db >= 0 ? '+' : ''}${db.toFixed(1)} dB)` : '';
    return `Loading a FIR filter${gain} mutes the device while the taps load: audio drops out briefly.\n\nLoad it now?`;
  };
  const CLEAR_WARNING = 'Clearing the FIR reloads it on the device: audio drops out briefly.\n\nClear it now?';

  const toast = (message, level) => window.mdsp && window.mdsp.toast && window.mdsp.toast(message, level);

  function worst(statuses) {
    let w = null;
    for (const st of statuses) if (w === null || RANK[st] > RANK[w]) w = st;
    return w || 'unknown';
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

  async function request(url, { method = 'GET', body, form } = {}) {
    const headers = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let response;
    try {
      response = await fetch(url, {
        method,
        headers,
        body: form !== undefined ? form : body === undefined ? undefined : JSON.stringify(body),
        credentials: 'same-origin',
      });
    } catch {
      return { ok: false, status: 0, data: { detail: 'the web UI server is unreachable' } };
    }
    const data = await response.json().catch(() => null);
    return { ok: response.ok, status: response.status, data };
  }

  function errorText(res) {
    const d = res.data || {};
    const detail = typeof d.detail === 'string' ? d.detail : null; // already lists the problems
    return detail || (d.problems || []).join('; ') || `Request refused (HTTP ${res.status})`;
  }

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  const LOG_TICKS = [10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000];
  const DECADE_TICKS = [10, 100, 1000, 10000];
  const tickLabel = (v) => (v >= 1000 ? `${v / 1000}k` : String(v));

  // A small uPlot line chart: { setData(xs, ys, label), destroy() }.
  function linePlot(el, { height, logX, xLabel, yLabel, color }) {
    if (!window.uPlot || !el) return null;
    const axis = cssVar('--text-muted', '#8c96a3');
    const grid = cssVar('--border', '#2a313b');
    const font = `12px ${cssVar('--font', 'system-ui, sans-serif')}`;
    const common = { stroke: axis, font, grid: { stroke: grid, width: 1 }, ticks: { stroke: grid, width: 1, size: 4 } };
    const width = () => Math.max(240, el.clientWidth || 480);
    const xAxis = { ...common, label: xLabel, labelFont: font, labelSize: 18 };
    if (logX) {
      // Narrow plots (phones) keep the decades only, so the labels don't run into each other.
      xAxis.splits = (u, i, min, max) =>
        (u.width < 520 ? DECADE_TICKS : LOG_TICKS).filter((t) => t >= min && t <= max);
      xAxis.filter = (u, splits) => splits;
      xAxis.values = (u, splits) => splits.map((v) => (v === null ? '' : tickLabel(v)));
    }
    const opts = {
      width: width(),
      height,
      legend: { show: false },
      cursor: { drag: { x: true, y: false, setScale: true } },
      scales: { x: logX ? { distr: 3, log: 10, time: false } : { time: false }, y: { auto: true } },
      axes: [xAxis, { ...common, size: 56, label: yLabel || undefined, labelFont: font, labelSize: 18 }],
      series: [{ label: xLabel }, { label: yLabel, stroke: color, width: 1.5, points: { show: false } }],
    };
    const plot = new window.uPlot(opts, [[], []], el);
    let observer = null;
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(() => plot.setSize({ width: width(), height }));
      observer.observe(el);
    }
    return {
      setData(xs, ys) {
        plot.setData([xs, ys]);
      },
      destroy() {
        if (observer) observer.disconnect();
        plot.destroy();
      },
    };
  }

  function firEditor(cfg) {
    const ctl = { impulse: null, response: null, reloadTimer: null };

    return {
      cfg,
      loaded: false,
      supported: true,
      state: null,
      sync: {},
      upload: null, // { blob, preview } of a file uploaded but not loaded yet
      shown: 'loaded', // which preview the plots show: 'loaded' | 'upload'
      responseMode: 'magnitude', // magnitude | phase | excess
      busy: false,
      error: null,
      prefix: `outputs.${cfg.channel}.fir`,

      init() {
        this.$el.addEventListener('sse:sync', (e) => this.onSync(e));
        this.$el.addEventListener('sse:desired', (e) => this.onDesired(e));
        this.$nextTick(() => {
          const accent = cssVar('--accent', '#4ea1ff');
          ctl.impulse = linePlot(this.$refs.impulse, {
            height: 200, logX: false, xLabel: 'Time (ms)', yLabel: 'Amplitude', color: accent,
          });
          ctl.response = linePlot(this.$refs.response, {
            height: 220, logX: true, xLabel: 'Frequency (Hz)', yLabel: '', color: cssVar('--ok', '#3ccf7a'),
          });
          this.load();
        });
      },

      destroy() {
        if (ctl.impulse) ctl.impulse.destroy();
        if (ctl.response) ctl.response.destroy();
      },

      async load() {
        const res = await request(cfg.url);
        if (!res.ok) {
          this.error = errorText(res);
          return;
        }
        this.error = null;
        this.apply(res.data);
      },

      apply(state) {
        const previewKept = this.state && this.state.blob_id === state.blob_id && !state.preview;
        this.state = previewKept ? { ...state, preview: this.state.preview } : state;
        this.supported = state.supported;
        this.sync = { ...state.sync_status };
        this.loaded = true;
        if (this.upload && this.upload.blob.id === state.blob_id) this.upload = null; // it's the loaded one now
        if (!this.upload) this.shown = 'loaded';
        this.render();
      },

      onSync(e) {
        const data = parseEvent(e);
        if (!data || data.preset !== cfg.preset) return;
        const next = { ...this.sync };
        for (const [path, st] of Object.entries(data.changes || {})) if (path.startsWith(`${this.prefix}.`)) next[path] = st;
        for (const path of data.removed || []) delete next[path];
        this.sync = next;
      },

      onDesired(e) {
        const data = parseEvent(e);
        if (!data || data.preset !== cfg.preset || this.busy) return;
        if (!(data.paths || []).some((p) => p.startsWith(`${this.prefix}.`))) return;
        clearTimeout(ctl.reloadTimer);
        ctl.reloadTimer = setTimeout(() => this.load(), RELOAD_DELAY_MS);
      },

      // --- view helpers ----------------------------------------------------------------------------------------

      status() {
        return worst(Object.values(this.sync));
      },
      icon() {
        return ICON[this.status()];
      },
      hasFilter() {
        return Boolean(this.state && this.state.blob_id);
      },
      preview() {
        if (this.shown === 'upload' && this.upload) return this.upload.preview;
        return this.state ? this.state.preview : null;
      },
      summary(p, blob) {
        if (!p) return '';
        const name = blob && blob.name ? `${blob.name} — ` : '';
        const basis = p.latency_basis === 'linear-phase' ? 'linear-phase' : 'peak position';
        return `${name}${p.n_taps} taps — latency ≈ ${p.latency_ms.toFixed(1)} ms (${basis}) — peak gain ${p.peak_gain_db.toFixed(1)} dB`;
      },

      render() {
        const p = this.preview();
        if (!ctl.impulse || !ctl.response) return;
        if (!p) {
          ctl.impulse.setData([], []);
          ctl.response.setData([], []);
          return;
        }
        ctl.impulse.setData(p.impulse.t_ms, p.impulse.values);
        const r = p.response;
        let ys = r.magnitude_db;
        if (this.responseMode !== 'magnitude') {
          // Phase is noise where the filter passes nothing (stop band): leave those points out.
          const phase = this.responseMode === 'phase' ? r.phase_deg : r.excess_phase_deg;
          ys = phase.map((v, i) => (r.magnitude_db[i] > PHASE_FLOOR_DB ? v : null));
        }
        ctl.response.setData(r.freqs, ys);
      },

      modeTitle() {
        return MODE_TITLES[this.responseMode];
      },

      setMode(mode) {
        this.responseMode = mode;
        this.render();
      },

      show(which) {
        this.shown = which;
        this.render();
      },

      // --- actions ---------------------------------------------------------------------------------------------

      async run(call) {
        this.busy = true;
        const res = await call();
        this.busy = false;
        if (!res.ok) {
          this.error = errorText(res);
          toast(this.error);
          return null;
        }
        this.error = null;
        return res.data;
      },

      async pick(event) {
        const file = event.target.files && event.target.files[0];
        event.target.value = '';
        if (!file) return;
        const form = new FormData();
        form.append('file', file, file.name);
        const data = await this.run(() => request(cfg.url, { method: 'POST', form }));
        if (!data) return;
        this.upload = data;
        this.show('upload');
      },

      async loadUpload() {
        if (!this.upload || !window.confirm(loadWarning(this.upload.preview))) return;
        const body = { blob_id: this.upload.blob.id };
        const data = await this.run(() => request(`${cfg.url}/load?confirm=true`, { method: 'POST', body }));
        if (!data) return;
        const uploaded = this.upload;
        this.upload = null;
        this.apply({ ...data.fir, preview: uploaded.preview });
      },

      discardUpload() {
        this.upload = null;
        this.show('loaded');
      },

      async setEnabled(on) {
        const put = (confirm) =>
          request(confirm ? `${cfg.url}?confirm=true` : cfg.url, { method: 'PUT', body: { bypass: !on } });
        const data = await this.run(async () => {
          const res = await put(false);
          // Enabling a FIR that raises the level more than the big-step limit needs confirmation.
          if (res.status === 409 && res.data && res.data.error === 'confirmation_required') {
            if (!window.confirm(`${res.data.detail}.\n\nEnable it anyway?`)) return { ok: true, data: null };
            return put(true);
          }
          return res;
        });
        if (data) this.apply(data.fir);
        else this.load();
      },

      async clear() {
        if (!window.confirm(CLEAR_WARNING)) return;
        const data = await this.run(() => request(`${cfg.url}/clear?confirm=true`, { method: 'POST' }));
        if (data) this.apply(data.fir);
      },
    };
  }

  if (typeof window === 'undefined') return;
  document.addEventListener('alpine:init', () => {
    window.Alpine.data('firEditor', firEditor);
  });
})();
