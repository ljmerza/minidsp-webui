// The output and input pages' chain tab bar (phase 9 design §8, §9; P19): choosing a chip swaps the tab panel in place
// (the header block re-renders with it), the URL follows, and Back / Forward return to the previous tab. Desktop keeps
// the scroll position; on a phone the panel lands just under the sticky header. Without JavaScript the chips are plain
// links (?tab=<key>). Every editor's script is loaded once by the page, so a swapped-in tab brings no <script src>.
//
// Also here: the Level tab's − / + steppers and "Raise route …" button (through params.js, so the usual guards and
// the big-step confirmation apply), the Delay tab's "Line up with…" helper, and opening Rename for #channel-name links.
(() => {
  'use strict';

  const PHONE = '(max-width: 599px), (pointer: coarse)';
  const FEET_PER_METRE = 3.28084;
  const box = () => document.querySelector('[data-chain-box]');
  const params = () => window.mdsp && window.mdsp.params;
  const toast = (message, level, options) => window.mdsp && window.mdsp.toast && window.mdsp.toast(message, level, options);
  const fmt = (value, digits = 2) => (window.mdsp && window.mdsp.fmtNum ? window.mdsp.fmtNum(value, digits) : String(value));
  const round1 = (value) => Math.round(value * 10) / 10;

  // --- the chain tabs ------------------------------------------------------------------------------------------------

  let latest = null; // the last chip chosen wins when several swaps overlap

  // Keep the document tall enough that scrollTo(y) holds: a short tab after a long one would clamp it, and so would a
  // tab whose editor briefly renders short while it loads, so the box keeps at least its height as swapped in. The
  // layout's own minimum height can absorb part of any growth, so it is measured again.
  function roomFor(y) {
    const b = box();
    if (!b) return;
    for (let i = 0; i < 4; i += 1) {
      const missing = Math.ceil(y + window.innerHeight - document.documentElement.scrollHeight);
      b.style.minHeight = `${b.offsetHeight + Math.max(0, missing)}px`;
      if (missing <= 0) return;
    }
  }

  function settled(target) {
    return new Promise((resolve) => {
      const done = () => {
        target.removeEventListener('htmx:afterSettle', done);
        clearTimeout(timer);
        resolve();
      };
      const timer = setTimeout(done, 1500);
      target.addEventListener('htmx:afterSettle', done);
    });
  }

  async function openTab(href, { push = true, focus = true } = {}) {
    const b = box();
    if (!b || !window.htmx) {
      window.location.href = href;
      return;
    }
    const token = {};
    latest = token;
    const y = window.scrollY;
    b.style.minHeight = `${b.offsetHeight}px`; // the page keeps its height while the panel is replaced
    const swapped = settled(b);
    try {
      await window.htmx.ajax('GET', href, { target: b, swap: 'innerHTML', select: '#output-chain' });
    } catch {
      window.location.href = href;
      return;
    }
    await swapped;
    if (latest !== token) return;
    b.style.minHeight = '';
    if (push && window.location.href !== new URL(href, window.location.href).href) {
      window.history.pushState({ mdspChain: true }, '', href);
    }
    const page = params();
    if (page) page.renderBadges();
    syncSteppers();
    const panel = document.getElementById('tab-panel');
    let target = y;
    if (panel && window.matchMedia(PHONE).matches) {
      // Phone (design §8.3): the panel goes to just under the sticky header (its scroll-margin-top).
      const margin = parseFloat(getComputedStyle(panel).scrollMarginTop) || 0;
      target = Math.max(0, panel.getBoundingClientRect().top + window.scrollY - margin);
    }
    roomFor(target);
    window.scrollTo(0, target);
    if (focus) {
      const current = document.querySelector('[data-chain-tab][aria-current="page"]');
      if (current) current.focus({ preventScroll: true });
    }
  }

  document.addEventListener('click', (e) => {
    const link = e.target.closest && e.target.closest('a[data-chain-tab]');
    if (!link || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (!box()) return;
    e.preventDefault();
    openTab(link.href);
  });

  // Back / Forward between tabs of this page. htmx keeps no snapshot here (hx-history="false"), and its own popstate
  // handler ignores entries without its marker, so the two never both act.
  window.addEventListener('popstate', (e) => {
    if (box() && e.state && e.state.mdspChain) openTab(window.location.href, { push: false, focus: false });
  });

  // --- Rename ------------------------------------------------------------------------------------------------------

  function openRename() {
    if (window.location.hash !== '#channel-name') return;
    const fold = document.querySelector('[data-chain-rename]');
    if (!fold) return;
    fold.open = true;
    const input = fold.querySelector('input[name="name"]');
    if (input) input.focus();
  }

  // --- Level tab: − / + steppers and "Raise route …" ---------------------------------------------------------------

  function stepperValue(button) {
    const control = document.querySelector(`input[type="number"][data-path="${button.dataset.chainStep}"]`);
    const value = control ? parseFloat(control.value) : NaN;
    if (Number.isFinite(value)) return value;
    const page = params();
    return page ? Number(page.value(button.dataset.chainStep)) : NaN;
  }

  function syncSteppers() {
    for (const button of document.querySelectorAll('[data-chain-step]')) {
      const value = stepperValue(button);
      const step = Number(button.dataset.step);
      const limit = Number(step < 0 ? button.dataset.min : button.dataset.max);
      button.disabled = Number.isFinite(value) && (step < 0 ? value <= limit + 1e-9 : value >= limit - 1e-9);
    }
  }

  document.addEventListener('click', (e) => {
    const button = e.target.closest && e.target.closest('[data-chain-step]');
    if (!button || button.disabled) return;
    const page = params();
    const value = stepperValue(button);
    if (!page || !Number.isFinite(value)) return;
    const next = round1(Math.min(Number(button.dataset.max), Math.max(Number(button.dataset.min), value + Number(button.dataset.step))));
    if (next !== value) page.set(button.dataset.chainStep, next);
    syncSteppers();
  });

  document.addEventListener('click', async (e) => {
    const button = e.target.closest && e.target.closest('[data-chain-values]');
    if (!button || button.disabled) return;
    const page = params();
    let values;
    try {
      values = JSON.parse(button.dataset.chainValues);
    } catch {
      return;
    }
    if (!page || !values) return;
    const before = Object.fromEntries(Object.keys(values).map((path) => [path, page.value(path)]));
    button.disabled = true;
    // Only claim it happened once the server took it: a cancelled confirmation or a refusal gets no toast or Undo.
    if ((await page.sendBatch(values)) !== 'ok') {
      button.disabled = false;
      return;
    }
    toast(button.dataset.chainDone || 'Done.', 'info', {
      action: { label: 'Undo', onClick: () => page.sendBatch(before) },
    });
  });

  for (const name of ['input', 'change']) document.addEventListener(name, syncSteppers);
  document.addEventListener('sse:desired', () => setTimeout(syncSteppers, 0));

  // --- Delay tab: "Line up with…" (design §8.2, P13) --------------------------------------------------------------

  document.addEventListener('alpine:init', () => {
    window.Alpine.data('lineUpHelper', (cfg) => ({
      distance: cfg.distance,
      unit: 'm',
      direction: 'closer',
      other: cfg.others.length ? cfg.others[0].index : null,

      otherName() {
        return (cfg.others.find((o) => o.index === this.other) || {}).name || 'the other output';
      },
      otherDelay() {
        const page = params();
        const value = page ? page.value(`outputs.${this.other}.delay_ms`) : null;
        return typeof value === 'number' ? value : 0;
      },
      metres() {
        const d = Number(this.distance);
        if (this.distance === '' || !(d >= 0)) return NaN;
        return this.unit === 'ft' ? d / FEET_PER_METRE : d;
      },
      // This output's delay: the other output's delay plus (closer) or minus (farther) the travel time.
      ms() {
        const m = this.metres();
        if (!Number.isFinite(m) || this.other === null || !(cfg.speed > 0)) return NaN;
        const offset = (m / cfg.speed) * 1000;
        return this.otherDelay() + (this.direction === 'closer' ? offset : -offset);
      },
      samples(ms) {
        return Math.round((ms * cfg.sampleRate) / 1000);
      },
      rounded() {
        return (this.samples(this.ms()) * 1000) / cfg.sampleRate;
      },
      valid() {
        const ms = this.ms();
        return Number.isFinite(ms) && ms >= 0 && this.samples(ms) <= Math.round((cfg.maxMs * cfg.sampleRate) / 1000);
      },
      resultText() {
        const ms = this.ms();
        const other = this.otherName();
        if (!Number.isFinite(ms)) return 'Enter how much closer or farther it is, in metres or feet.';
        if (ms < 0) return `That is below zero: ${other} should wait ${fmt(-ms)} ms instead (on its Delay tab).`;
        if (!this.valid()) return `That is more than the ${fmt(cfg.maxMs, 1)} ms the device allows.`;
        const already = this.otherDelay() > 0 ? ` ${other} already waits ${fmt(this.otherDelay())} ms; that is included.` : '';
        return `→ ${cfg.name} will wait ${fmt(this.rounded())} ms, so both arrive together.${already}`;
      },
      apply() {
        const page = params();
        if (!page || !this.valid()) return;
        page.set(cfg.path, this.rounded());
      },
    }));
  });

  function init() {
    if (!box()) return;
    // Mark this entry, so Back to it from another tab swaps the panel instead of doing nothing.
    window.history.replaceState({ mdspChain: true }, '');
    syncSteppers();
    openRename();
  }

  window.addEventListener('hashchange', openRename);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
