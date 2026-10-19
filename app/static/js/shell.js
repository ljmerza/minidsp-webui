// The global shell (phase 9 design §3): the header's sheets and pop-ups, the ≡ navigation sheet, the phone's bottom
// bar, the header CLIP light and the problems announcements.
//
// - hdrSheet: a disclosure (button + panel) in the header: the problems list, Help me…, the CLIP pop-up, and on phones
//   the preset and Volume sheets. Esc or a click outside closes it (Esc puts focus back on its button); one is open
//   at a time. While one is open the header isn't re-rendered under it: the refresh waits until it closes. The reply
//   to a request made from inside the open sheet (a preset button, Volume −/+) is shown at once: the Volume sheet
//   stays open across it, the others close.
// - navSheet: below 900 px the ≡ button opens the navigation as a sheet; the rest of the page is inert meanwhile, and
//   Esc, ✕ or the backdrop close it with focus back on ≡.
// - The bottom bar hides while a text or number field outside the header has focus (body.is-typing), so a phone's
//   keyboard doesn't push it over the field.
// - The CLIP light latches when any channel reaches −1 dBFS on any page (the page's "levels" feed), and stays latched
//   across pages (sessionStorage, per device) until Reset.
// - Problems: the pill's list is announced politely once per change (#shell-live), never on every refresh.
// - Inline fixes in the problems list ("Route In 1 → Sub"): one edit through the editors' endpoint, then Undo.
(() => {
  'use strict';

  const CLIP_DBFS = -1; // as meters.js
  const mdsp = () => window.mdsp || {};

  function say(text) {
    const live = document.getElementById('shell-live');
    if (!live || !text) return;
    live.textContent = '';
    setTimeout(() => { live.textContent = text; }, 50); // a fresh text node, so the same words are read again
  }

  // --- header sheets ---------------------------------------------------------------------------------------------------

  let openSheet = null;
  let refreshWaiting = false;

  function headerHeld() {
    return !!document.querySelector('#app-header .hdr-sheet.is-open');
  }

  // The open sheet's id (its panel's), when the reply to a request made from inside it is let through.
  let swapFromSheet = null;
  // Sheets whose buttons are pressed repeatedly (Volume −/+) stay open across that swap; the others (a preset
  // switch) close, and focus goes back to their toggle.
  const KEEP_OPEN = new Set(['hdr-volume-sheet']);

  function sheetPanelId(sheet) {
    const toggle = sheet && sheet.querySelector('.hdr-sheet-toggle');
    return toggle ? toggle.getAttribute('aria-controls') : null;
  }

  document.addEventListener('htmx:beforeSwap', (e) => {
    const target = e.detail.target;
    if (!target || target.id !== 'app-header' || !e.detail.shouldSwap) return;
    if (!headerHeld()) return;
    // A reply to something done inside the open sheet (a preset button, Volume −/+) shows its result at once;
    // only unrelated refreshes wait until the sheet closes.
    const elt = e.detail.requestConfig && e.detail.requestConfig.elt;
    const sheet = elt && elt.closest ? elt.closest('#app-header .hdr-sheet.is-open') : null;
    if (sheet) {
      swapFromSheet = sheetPanelId(sheet);
      refreshWaiting = false; // this reply is the fresh header
      return;
    }
    e.detail.shouldSwap = false;
    refreshWaiting = true;
  });

  document.addEventListener('htmx:afterSettle', (e) => {
    if (!swapFromSheet || !e.detail.target || e.detail.target.id !== 'app-header') return;
    const id = swapFromSheet;
    swapFromSheet = null;
    if (openSheet) openSheet = null; // the old component is gone with the old header
    const toggle = document.querySelector(`#app-header .hdr-sheet-toggle[aria-controls="${id}"]`);
    const sheet = toggle ? toggle.closest('.hdr-sheet') : null;
    if (!sheet) return;
    if (KEEP_OPEN.has(id) && window.Alpine) {
      window.Alpine.$data(sheet).show();
    } else {
      toggle.focus();
    }
  });

  function releaseHeader() {
    if (!refreshWaiting || headerHeld()) return;
    refreshWaiting = false;
    const header = document.getElementById('app-header');
    if (header && window.htmx) setTimeout(() => window.htmx.trigger(header, 'refresh'), 0);
  }

  function hdrSheet() {
    return {
      open: false,
      toggle() {
        if (this.open) this.close(true);
        else this.show();
      },
      show() {
        if (openSheet && openSheet !== this) openSheet.close(false);
        this.open = true;
        openSheet = this;
      },
      close(returnFocus) {
        if (!this.open) return;
        this.open = false;
        if (openSheet === this) openSheet = null;
        // The toggle may sit in a nested component (the Volume button is inside masterVolume), so look it up.
        const toggle = this.$root.querySelector('.hdr-sheet-toggle');
        if (returnFocus && toggle) toggle.focus();
        this.$nextTick(releaseHeader);
      },
      destroy() {
        if (openSheet === this) openSheet = null;
      },
    };
  }

  // --- navigation sheet ------------------------------------------------------------------------------------------------

  const SHEET_NAV = '(max-width: 899px)';

  function navSheet() {
    return {
      open: false,
      init() {
        const query = window.matchMedia(SHEET_NAV);
        const onChange = () => { if (!query.matches) this.close(false); };
        if (query.addEventListener) query.addEventListener('change', onChange);
      },
      toggle() {
        if (this.open) this.close(true);
        else this.show();
      },
      show() {
        if (!window.matchMedia(SHEET_NAV).matches) return;
        this.open = true;
        document.body.classList.add('nav-open');
        for (const el of document.querySelectorAll('#main, .app-header, .hdr-bottom')) el.inert = true;
        this.$nextTick(() => {
          const nav = this.$refs.nav;
          const first = nav && (nav.querySelector('[aria-current="page"]') || nav.querySelector('a[href], button'));
          if (first) first.focus();
        });
      },
      close(returnFocus) {
        if (!this.open) return;
        this.open = false;
        document.body.classList.remove('nav-open');
        for (const el of document.querySelectorAll('#main, .app-header, .hdr-bottom')) el.inert = false;
        if (returnFocus && this.$refs.navToggle) this.$refs.navToggle.focus();
      },
    };
  }

  // --- bottom bar: hidden while typing ---------------------------------------------------------------------------------

  const NON_TEXT_INPUTS = new Set(['range', 'checkbox', 'radio', 'button', 'submit', 'reset', 'color', 'file']);
  function isTextField(el) {
    if (!el || !el.tagName) return false;
    if (el.isContentEditable || el.tagName === 'TEXTAREA') return true;
    return el.tagName === 'INPUT' && !NON_TEXT_INPUTS.has(el.type);
  }
  document.addEventListener('focusin', (e) => {
    const typing = isTextField(e.target) && !e.target.closest('#app-header');
    document.body.classList.toggle('is-typing', typing);
  });
  document.addEventListener('focusout', (e) => {
    if (!e.relatedTarget || !isTextField(e.relatedTarget)) document.body.classList.remove('is-typing');
  });

  // --- header height (the sticky sidebar and anchors sit just under it) ------------------------------------------------

  function trackHeaderHeight() {
    const header = document.querySelector('.app-header');
    if (!header || typeof ResizeObserver === 'undefined') return;
    const set = () => document.documentElement.style.setProperty('--hdr-h', `${Math.round(header.getBoundingClientRect().height)}px`);
    new ResizeObserver(set).observe(header);
    set();
  }

  // --- CLIP light ------------------------------------------------------------------------------------------------------

  const clipStore = {
    on: false,
    keys: [],
    labels: { i: [], o: [] },
    serial: '',
    names() {
      return this.keys.map((key) => {
        const side = key[0];
        const index = Number(key.slice(1));
        const list = this.labels[side] || [];
        return list[index] || `${side === 'i' ? 'In' : 'Out'} ${index + 1}`;
      });
    },
    latch(keys) {
      const fresh = keys.filter((k) => !this.keys.includes(k));
      if (!fresh.length) return [];
      this.keys = [...this.keys, ...fresh];
      this.on = true;
      this.save();
      return fresh;
    },
    reset() {
      this.keys = [];
      this.on = false;
      this.save();
    },
    storageKey() {
      return `mdsp-clip-${this.serial}`;
    },
    save() {
      try {
        if (this.keys.length) sessionStorage.setItem(this.storageKey(), JSON.stringify(this.keys));
        else sessionStorage.removeItem(this.storageKey());
      } catch {
        /* private mode: the light still works on this page */
      }
    },
    load() {
      try {
        const saved = JSON.parse(sessionStorage.getItem(this.storageKey()) || '[]');
        if (Array.isArray(saved) && saved.length) {
          this.keys = saved.filter((k) => typeof k === 'string' && /^[io]\d+$/.test(k));
          this.on = this.keys.length > 0;
        }
      } catch {
        /* ignore a broken entry */
      }
    },
  };

  // The clipped channels in a "levels" frame ({"i": [...], "o": [...]} in dBFS).
  function clippedKeys(data) {
    const keys = [];
    for (const side of ['i', 'o']) {
      const values = data && Array.isArray(data[side]) ? data[side] : [];
      values.forEach((v, idx) => {
        if (typeof v === 'number' && v >= CLIP_DBFS) keys.push(`${side}${idx}`);
      });
    }
    return keys;
  }

  function onLevels(event) {
    const store = window.Alpine ? window.Alpine.store('clip') : null;
    if (!store || !event || typeof event.data !== 'string') return;
    let data;
    try {
      data = JSON.parse(event.data);
    } catch {
      return;
    }
    const fresh = store.latch(clippedKeys(data));
    // The diagram announces its own clip latches; elsewhere the header does.
    if (fresh.length && !document.querySelector('[data-flow]')) {
      const names = store.names().filter((_, i) => fresh.includes(store.keys[i]));
      say(`${names.join(', ')} clipped`);
    }
  }

  // --- problems: one polite announcement per change --------------------------------------------------------------------

  let lastProblems = null;
  function announceProblems() {
    const box = document.querySelector('#app-header [data-problems-announce]');
    const text = box ? box.dataset.problemsAnnounce : null;
    if (text === null || text === undefined) return;
    if (lastProblems !== null && text !== lastProblems) say(text);
    lastProblems = text;
  }

  // --- inline fixes in the problems list -------------------------------------------------------------------------------

  async function send(url, values, confirm = false) {
    const toast = mdsp().toast || (() => {});
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
      if (window.confirm(`${data.detail}.\n\nApply it anyway?`)) return send(url, values, true);
      return false;
    }
    const detail = data && (data.detail || (data.problems || []).join('; '));
    toast(detail || `Change refused (HTTP ${response.status})`);
    return false;
  }

  function refreshHeader() {
    const header = document.getElementById('app-header');
    if (header && window.htmx) window.htmx.trigger(header, 'refresh');
    document.body.dispatchEvent(new CustomEvent('mdsp-sync-changed', { bubbles: true }));
  }

  document.addEventListener('click', async (e) => {
    const button = e.target instanceof Element ? e.target.closest('[data-shell-fix]') : null;
    if (!button || button.getAttribute('aria-disabled') === 'true') return;
    let values;
    let undo;
    try {
      values = JSON.parse(button.dataset.values || '{}');
      undo = JSON.parse(button.dataset.undo || '{}');
    } catch {
      return;
    }
    const { url, preset } = button.dataset;
    // The same fixes sit on Home (its health line and cards, /inputs): focus then stays on the page, on the card or
    // the health line the button was in, since the button itself is about to be re-rendered.
    const onPage = !button.closest('#app-header');
    const scope = onPage ? button.closest('[data-fix-scope]') : null;
    const toggle = onPage
      ? button.closest('[data-output], [data-input]') || (scope && scope.querySelector('[tabindex="-1"]'))
      : document.querySelector('#app-header .hdr-problems .hdr-sheet-toggle');
    button.setAttribute('aria-disabled', 'true');
    const ok = await send(url, values);
    button.removeAttribute('aria-disabled');
    if (!ok) return;
    if (openSheet && !onPage) openSheet.close(false);
    if (toggle && toggle.isConnected) toggle.focus();
    refreshHeader();
    const toast = mdsp().toast;
    if (!toast) return;
    toast(button.dataset.done || 'Done.', 'info', {
      near: toggle,
      action: {
        label: 'Undo',
        onClick: async () => {
          const hdr = document.querySelector('#app-header [data-problems-preset]');
          if (hdr && hdr.dataset.problemsPreset !== preset) {
            toast('Left as it is: the device has switched presets since.', 'info');
            return;
          }
          if (await send(url, undo)) refreshHeader();
        },
        returnFocus: toggle,
      },
    });
  });

  // --- the other inline fixes (data-shell-action, partials/fix_macros.html) -------------------------------------------
  //
  // "master": Unmute all / Set −30 dB through the header's master endpoints (form fields, as its own buttons send);
  // a guarded change (a big volume step) asks first, then Undo puts the old value back. "post": Retry on the edited
  // preset. "clip": the CLIP lights' Reset.

  async function postForm(url, fields, confirm = false) {
    const toast = mdsp().toast || (() => {});
    const body = new URLSearchParams();
    for (const [k, v] of Object.entries(fields || {})) body.set(k, String(v));
    if (confirm) body.set('confirm', 'true');
    let response;
    try {
      response = await fetch(url, {
        method: 'POST',
        headers: { 'HX-Request': 'true', 'Content-Type': 'application/x-www-form-urlencoded' },
        body,
        credentials: 'same-origin',
      });
    } catch {
      toast('Request failed: the web UI server is unreachable');
      return false;
    }
    if (response.ok) return true;
    let detail = response.headers.get('X-Mdsp-Error') || '';
    try {
      detail = decodeURIComponent(detail);
    } catch {
      /* keep it as sent */
    }
    if (response.status === 409 && !confirm && 'volume' in (fields || {})) {
      if (window.confirm(`${detail || 'That is a big change of the volume'}.\n\nApply it anyway?`)) {
        return postForm(url, fields, true);
      }
      return false;
    }
    toast(detail || `Change refused (HTTP ${response.status})`);
    return false;
  }

  function masterUrl(endpoint) {
    const hdr = document.querySelector('#app-header [data-master-url]');
    return hdr && endpoint ? `${hdr.dataset.masterUrl}/${endpoint}` : null;
  }

  document.addEventListener('click', async (e) => {
    const button = e.target instanceof Element ? e.target.closest('[data-shell-action]') : null;
    if (!button || button.getAttribute('aria-disabled') === 'true') return;
    const kind = button.dataset.shellAction;
    const toast = mdsp().toast || (() => {});
    // As the preset-edit fixes above: focus goes to the card, the health line or the diagram's title on the page, or
    // back to the problems pill in the header, since the button is about to be re-rendered.
    const onPage = !button.closest('#app-header');
    const scope = onPage ? button.closest('[data-fix-scope]') : null;
    const back = onPage
      ? button.closest('[data-output], [data-input]') || (scope && scope.querySelector('[tabindex="-1"]'))
        || document.getElementById('flow-title')
      : document.querySelector('#app-header .hdr-problems .hdr-sheet-toggle');
    const focusBack = () => {
      if (openSheet && !onPage) openSheet.close(false);
      if (back && back.isConnected) back.focus();
    };
    if (kind === 'clip') {
      const store = window.Alpine ? window.Alpine.store('clip') : null;
      if (store) store.reset();
      document.dispatchEvent(new CustomEvent('mdsp-clip-reset'));
      focusBack();
      toast(button.dataset.done || 'CLIP lights reset.', 'info', { near: back });
      return;
    }
    let values;
    let undo;
    try {
      values = JSON.parse(button.dataset.values || '{}');
      undo = JSON.parse(button.dataset.undo || '{}');
    } catch {
      return;
    }
    const url = kind === 'master' ? masterUrl(button.dataset.endpoint) : button.dataset.url;
    if (!url) return;
    button.setAttribute('aria-disabled', 'true');
    const ok = await postForm(url, kind === 'master' ? values : {});
    button.removeAttribute('aria-disabled');
    if (!ok) return;
    focusBack();
    refreshHeader();
    const options = { near: back };
    if (kind === 'master' && Object.keys(undo).length) {
      options.action = {
        label: 'Undo',
        onClick: async () => {
          if (await postForm(url, undo)) refreshHeader();
        },
        returnFocus: back,
      };
    }
    toast(button.dataset.done || 'Done.', 'info', options);
  });

  // --- wiring ----------------------------------------------------------------------------------------------------------

  document.addEventListener('alpine:init', () => {
    window.Alpine.data('hdrSheet', hdrSheet);
    window.Alpine.data('navSheet', navSheet);
    const feed = document.querySelector('[data-clip-feed]');
    try {
      clipStore.labels = JSON.parse((feed && feed.dataset.clipNames) || '{}');
    } catch {
      clipStore.labels = { i: [], o: [] };
    }
    clipStore.serial = document.body.dataset.serial || '';
    clipStore.load();
    window.Alpine.store('clip', clipStore);
  });

  document.addEventListener('DOMContentLoaded', () => {
    const feed = document.querySelector('[data-clip-feed]');
    if (feed) feed.addEventListener('sse:levels', (e) => onLevels(e.detail));
    trackHeaderHeight();
    announceProblems();
  });

  document.addEventListener('htmx:afterSwap', (e) => {
    if (e.detail.target && e.detail.target.id === 'app-header') announceProblems();
  });

  window.mdsp = Object.assign(window.mdsp || {}, { shell: { clippedKeys, say, isTextField } });
})();
