// Output crossover designer (docs 05 §4): two group cards (type, family, slope, frequency, enabled), the biquads each
// design needs, presets, and a raw-coefficient drawer per group with stability indicators and "flatten".
//
// Needs peq_editor.js (window.mdsp.filters), dsp.js and graph.js. Typing a frequency previews the group with dsp.js;
// a committed change goes to the server, whose coefficients replace the preview. Bessel designs are compiled by the
// server only, so they show once it answers.
(() => {
  'use strict';

  const ORDERS = [1, 2, 3, 4, 5, 6, 7, 8];
  const GROUP_COLORS = ['#f472b6', '#facc15'];
  const DEFAULT_DESIGN = { kind: 'lp', family: 'lr', order: 4, freq: 80, freq_hi: null, bessel_norm: 'phase' };
  const RELOAD_DELAY_MS = 350;

  const F = () => window.mdsp.filters;
  const dsp = () => window.mdsp.dsp;
  const toast = (message, level) => window.mdsp.toast && window.mdsp.toast(message, level);
  const num = (v) => (v === null || v === undefined || v === '' ? null : Number(v));

  // Biquads a design needs: BW n and Bessel n use ceil(n/2), LR n uses n/2; a band-pass needs both sides.
  function needed(form) {
    const order = Number(form.order);
    let per;
    if (form.family === 'lr') per = order / 2;
    else per = Math.ceil(order / 2);
    return form.kind === 'bp' ? 2 * per : per;
  }

  // Why an order can't be chosen for this family/type (null when it can).
  function orderProblem(form, order, nBiquads) {
    if (form.family === 'lr' && order % 2) return 'Linkwitz-Riley needs an even order';
    if (form.family === 'bessel' && order < 2) return 'Bessel needs order 2 or more';
    const n = needed({ ...form, order });
    if (n > nBiquads) return `needs ${n} biquads, a group has ${nBiquads}`;
    return null;
  }

  function formFrom(group) {
    const d = group.design || DEFAULT_DESIGN;
    return {
      kind: d.kind,
      family: d.family,
      order: d.order,
      freq: d.freq,
      freq_hi: d.freq_hi ?? null,
      bessel_norm: d.bessel_norm || 'phase',
    };
  }

  function designFrom(form) {
    const d = { kind: form.kind, family: form.family, order: Number(form.order), freq: num(form.freq) };
    if (form.kind === 'bp') d.freq_hi = num(form.freq_hi);
    if (form.family === 'bessel') d.bessel_norm = form.bessel_norm || 'phase';
    return d;
  }

  const rawRows = (compiled) => compiled.map((bq) => F().fullPrecision(bq));
  const sameRows = (a, b) => JSON.stringify(a) === JSON.stringify(b);

  function crossoverEditor(cfg) {
    const ctl = { plot: null, grid: null, reloadTimer: null, rowsFrom: null }; // rowsFrom: the drawer rows as loaded

    return {
      cfg,
      groups: [],
      sync: {},
      presets: [],
      loaded: false,
      error: null,
      warnings: [],
      drawer: null,
      rows: [],
      orders: ORDERS,
      view: F().remember('xover.view') || F().pageView('sub'),
      prefix: `outputs.${cfg.channel}.crossover`,

      init() {
        this.$el.addEventListener('sse:sync', (e) => this.onSync(e));
        this.$el.addEventListener('sse:desired', (e) => this.onDesired(e));
        this.$nextTick(() => {
          ctl.plot = F().createPlot(this.$refs.graph, { view: this.view, mode: 'magnitude', height: cfg.height || 280 });
          this.load();
        });
      },

      destroy() {
        if (ctl.plot) ctl.plot.destroy();
      },

      async load() {
        const res = await F().request(cfg.stateUrl);
        if (!res.ok) {
          this.error = F().errorText(res);
          return;
        }
        this.error = null;
        this.apply(res.data);
      },

      apply(data) {
        this.sync = data.sync || {};
        this.presets = data.presets || this.presets;
        // Groups with a request on its way or an uncommitted edit keep their state.
        const busy = new Set(this.groups.filter((g) => g.inflight || g.queued || g.dirty).map((g) => g.index));
        this.groups = data.groups.map((g) => (busy.has(g.index) ? this.groups[g.index] : this.fromServer(g)));
        this.loaded = true;
        // The open drawer follows the stored coefficients unless the user has typed into it.
        if (this.drawer !== null && sameRows(this.rows, ctl.rowsFrom)) this.setRows(this.groups[this.drawer].compiled);
        this.render();
      },

      setRows(compiled) {
        this.rows = rawRows(compiled);
        ctl.rowsFrom = rawRows(compiled);
      },

      fromServer(g, previous = null) {
        return {
          ...g,
          server: g,
          form: formFrom(g),
          preview: null,
          invalid: null,
          dirty: false, // a field typed but not committed yet
          inflight: false,
          queued: previous ? previous.queued : null,
        };
      },

      onSync(e) {
        const data = F().parseEvent(e);
        if (!data || data.preset !== cfg.preset) return;
        F().applySync(this.sync, data, `${this.prefix}.`);
      },

      onDesired(e) {
        const data = F().parseEvent(e);
        if (!data || data.preset !== cfg.preset || data.event !== 'edit') return;
        if (!(data.paths || []).some((p) => p.startsWith(`${this.prefix}.`))) return;
        clearTimeout(ctl.reloadTimer);
        ctl.reloadTimer = setTimeout(() => this.load(), RELOAD_DELAY_MS);
      },

      // --- view helpers ----------------------------------------------------------------------------------------

      color(k) {
        return GROUP_COLORS[k % GROUP_COLORS.length];
      },
      status(group) {
        const paths = Object.keys(this.sync).filter((p) => p.startsWith(`${group.path}.`));
        return paths.length ? F().worst(paths.map((p) => this.sync[p])) : 'unknown';
      },
      statusIcon(group) {
        return F().ICON[this.status(group)];
      },
      needed(group) {
        return needed(group.form);
      },
      tooBig(group) {
        return needed(group.form) > cfg.nBiquads;
      },
      used(group) {
        if (group.preview) return group.preview.filter((bq) => !dsp().isPassthrough(bq)).length;
        return group.biquads_used;
      },
      orderLabel(group, order) {
        const problem = orderProblem(group.form, order, cfg.nBiquads);
        return `${order * 6} dB/oct${problem ? ` (${problem})` : ''}`;
      },
      orderDisabled(group, order) {
        return orderProblem(group.form, order, cfg.nBiquads) !== null;
      },
      label(group) {
        if (group.preview || group.inflight) return 'editing…';
        return group.label;
      },

      // --- graph -----------------------------------------------------------------------------------------------

      grid() {
        if (!ctl.grid || ctl.grid.view !== this.view) ctl.grid = { view: this.view, freqs: dsp().viewGrid(this.view) };
        return ctl.grid.freqs;
      },

      biquads(group) {
        return group.preview || group.compiled;
      },

      render() {
        if (!ctl.plot || !dsp()) return;
        const freqs = this.grid();
        const traces = this.groups.map((g) => ({
          id: `g${g.index}`,
          label: `Group ${g.index + 1}${g.bypass ? ' (bypassed)' : ''}`,
          color: this.color(g.index),
          dash: g.bypass ? [5, 5] : undefined,
          width: 1.4,
          response: dsp().filterResponse(this.biquads(g), freqs),
        }));
        const enabled = this.groups.filter((g) => !g.bypass).flatMap((g) => this.biquads(g));
        const combined = dsp().filterResponse(enabled, freqs, { label: 'crossover' });
        traces.push({ id: 'sum', label: `${cfg.name} crossover`, color: cfg.color, bold: true, response: combined });
        ctl.plot.setTraces(traces);
        this.warnings = combined.warnings;
      },

      setView(view) {
        this.view = view;
        F().remember('xover.view', view);
        if (ctl.plot) ctl.plot.setView(view);
        this.render();
      },
      autoFit() {
        if (ctl.plot) ctl.plot.autoFit();
      },
      resetY() {
        if (ctl.plot) ctl.plot.resetY();
      },

      // --- edits -----------------------------------------------------------------------------------------------

      preview(group) {
        const form = group.form;
        try {
          if (form.family === 'bessel') {
            group.preview = null; // server-only design
          } else {
            group.preview = dsp().designCrossover(designFrom(form), cfg.nBiquads, cfg.fs || dsp().FS);
          }
          group.invalid = null;
          return true;
        } catch (e) {
          group.preview = null;
          group.invalid = e.message;
          return false;
        }
      },

      onInput(group) {
        group.dirty = true;
        this.preview(group);
        this.render();
      },

      // Family / type changes keep the slope when it still fits, else take the nearest one that does.
      onShapeChange(group) {
        const form = group.form;
        if (form.kind === 'bp' && !(num(form.freq_hi) > num(form.freq))) form.freq_hi = Math.round(num(form.freq) * 4) || 80;
        if (orderProblem(form, Number(form.order), cfg.nBiquads)) {
          const ok = ORDERS.filter((o) => !orderProblem(form, o, cfg.nBiquads));
          if (ok.length) form.order = ok.reduce((a, b) => (Math.abs(b - form.order) < Math.abs(a - form.order) ? b : a));
        }
        this.commit(group);
      },

      commit(group) {
        group.dirty = false;
        if (!group.form.freq || this.tooBig(group)) {
          toast(`Group ${group.index + 1}: ${this.tooBig(group) ? orderProblem(group.form, Number(group.form.order), cfg.nBiquads) : 'enter a frequency'}`);
          this.revert(group);
          return;
        }
        if (!this.preview(group) && group.form.family !== 'bessel') {
          toast(`Group ${group.index + 1}: ${group.invalid}`);
          this.revert(group);
          return;
        }
        this.render();
        this.queue(group, { design: designFrom(group.form) });
      },

      setEnabled(group, on) {
        this.queue(group, { bypass: !on });
      },

      // --- raw drawer ------------------------------------------------------------------------------------------

      toggleDrawer(group) {
        if (this.drawer === group.index) {
          this.drawer = null;
          return;
        }
        this.drawer = group.index;
        this.setRows(group.compiled);
      },
      rowStability(k) {
        return F().stability(F().biquadFromForm(this.rows[k]));
      },
      applyRaw(group) {
        const biquads = this.rows.map((r) => F().biquadFromForm(r));
        const bad = biquads.findIndex((bq) => !F().stability(bq).ok);
        if (bad >= 0) {
          toast(`Group ${group.index + 1}, biquad ${bad + 1}: ${F().stability(biquads[bad]).text}`);
          return;
        }
        group.preview = biquads;
        this.render();
        this.queue(group, { biquads });
      },
      flatten(group) {
        if (!window.confirm(`Flatten group ${group.index + 1} of ${cfg.name}? Every biquad becomes pass-through.`)) return;
        this.queue(group, { biquads: [] });
      },

      async applyPreset(preset) {
        if (!window.confirm(`Apply "${preset.label}" to ${cfg.name}? Both crossover groups change.`)) return;
        const res = await F().send(cfg.presetUrl, { preset: preset.key });
        if (!res.ok) {
          if (!res.cancelled) toast(F().errorText(res));
          return;
        }
        this.apply(res.data);
      },

      // --- sending ---------------------------------------------------------------------------------------------

      // Waiting changes are merged: a new design or biquad list replaces an older one, a bypass change is kept.
      queue(group, body) {
        const merged = { ...(group.queued || {}) };
        if ('design' in body || 'biquads' in body) {
          delete merged.design;
          delete merged.biquads;
        }
        group.queued = { ...merged, ...body };
        for (const p of Object.keys(this.sync)) if (p.startsWith(`${group.path}.`)) this.sync[p] = 'pending';
        if (!group.inflight) this.pump(group.index);
      },

      async pump(k) {
        let group = this.groups[k];
        group.inflight = true;
        try {
          while (group.queued) {
            const body = { ...group.queued, commit: true };
            group.queued = null;
            const res = await F().send(cfg.groupUrl.replace('{group}', String(k)), body);
            group = this.groups[k];
            if (res.ok) {
              Object.assign(this.sync, res.data.sync || {});
              if (!group.queued && !group.dirty) this.replace(res.data.group, 'biquads' in body);
              else Object.assign(group, res.data.group, { server: res.data.group }); // keeps the form being edited
            } else {
              if (!res.cancelled) toast(`Group ${k + 1}: ${F().errorText(res)}`);
              group.queued = null;
              if (res.data && res.data.group) this.replace(res.data.group);
              else this.revert(group);
            }
            group = this.groups[k];
          }
        } finally {
          this.groups[k].inflight = false;
          this.render();
        }
      },

      // ``fromRaw``: the reply to the drawer's own "Apply raw" / "Flatten", whose rows it replaces in any case.
      replace(view, fromRaw = false) {
        const k = view.index;
        const previous = this.groups[k];
        const next = this.fromServer(view, previous);
        next.inflight = previous ? previous.inflight : false;
        this.groups.splice(k, 1, next);
        if (this.drawer === k && (fromRaw || sameRows(this.rows, ctl.rowsFrom))) this.setRows(view.compiled);
      },

      revert(group) {
        const next = this.fromServer(group.server);
        next.inflight = group.inflight;
        this.groups.splice(group.index, 1, next);
        this.render();
      },
    };
  }

  document.addEventListener('alpine:init', () => {
    window.Alpine.data('crossoverEditor', crossoverEditor);
  });
})();
