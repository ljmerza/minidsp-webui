// Page-wide glue: number formatting, toasts, response handling, SSE connection state, keyboard shortcuts.
(() => {
  'use strict';

  const MINUS = '−';
  const DASH = '—';

  function fmtNum(value, decimals = 1) {
    if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
    const text = Math.abs(value).toFixed(decimals);
    return (value < 0 && Number(text) !== 0 ? MINUS : '') + text;
  }

  function fmtDb(value, decimals = 1, unit = 'dB') {
    const text = fmtNum(value, decimals);
    return text === DASH ? text : `${text} ${unit}`;
  }

  function toast(message, level = 'error') {
    const region = document.getElementById('toasts');
    if (!region) return;
    const item = document.createElement('div');
    item.className = `toast toast-${level}`;
    item.setAttribute('role', level === 'error' ? 'alert' : 'status');
    const text = document.createElement('span');
    text.textContent = message;
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast-close';
    close.setAttribute('aria-label', 'Dismiss');
    close.textContent = '×';
    close.addEventListener('click', () => item.remove());
    item.append(text, close);
    region.append(item);
    while (region.children.length > 4) region.firstElementChild.remove();
    setTimeout(() => item.remove(), 8000);
  }

  window.mdsp = Object.assign(window.mdsp || {}, { fmtNum, fmtDb, toast });

  // Error responses from our partial routes carry X-Mdsp-Error and a fragment worth swapping (the current state).
  // Anything else that failed (a crash page, a proxy error) must not end up inside the header.
  document.addEventListener('htmx:beforeSwap', (e) => {
    const xhr = e.detail.xhr;
    if (xhr && xhr.status >= 400 && !xhr.getResponseHeader('X-Mdsp-Error')) e.detail.shouldSwap = false;
  });

  document.addEventListener('htmx:afterRequest', (e) => {
    const xhr = e.detail.xhr;
    if (!xhr || xhr.status === 0) return;
    const message = xhr.getResponseHeader('X-Mdsp-Error');
    if (message) toast(decodeURIComponent(message));
    else if (xhr.status >= 400) toast(`Request failed (HTTP ${xhr.status})`);
  });

  document.addEventListener('htmx:sendError', () => toast('Request failed: the web UI server is unreachable'));

  // A device showed up (or changed) after this page was rendered without it: reload so the page matches.
  document.addEventListener('htmx:afterSwap', (e) => {
    if (!e.detail.target || e.detail.target.id !== 'app-header') return;
    const hdr = e.detail.target.querySelector('.hdr');
    const serial = hdr ? hdr.dataset.serial || '' : '';
    if (serial && serial !== (document.body.dataset.serial || '')) window.location.reload();
  });

  // Close the live feed when leaving the page, so navigating away isn't reported as a connection error. A page
  // restored from the back/forward cache has a dead feed and stale values, so it is reloaded.
  if (window.htmx && htmx.createEventSource) {
    const createEventSource = htmx.createEventSource;
    const sources = new Set();
    htmx.createEventSource = (url) => {
      const source = createEventSource(url);
      sources.add(source);
      return source;
    };
    window.addEventListener('pagehide', () => {
      for (const source of sources) source.close();
      sources.clear();
    });
    window.addEventListener('pageshow', (e) => { if (e.persisted) window.location.reload(); });
  }

  // Live-update connection state. EventSource reconnects by itself; only show the notice if it stays down.
  let offlineTimer = null;
  function setOffline(offline) {
    const notice = document.querySelector('[data-sse-offline]');
    if (notice) notice.hidden = !offline;
    document.body.classList.toggle('sse-down', offline);
  }
  document.addEventListener('htmx:sseError', () => {
    if (offlineTimer === null) offlineTimer = setTimeout(() => setOffline(true), 2000);
  });
  document.addEventListener('htmx:sseOpen', () => {
    clearTimeout(offlineTimer);
    offlineTimer = null;
    setOffline(false);
  });

  // Keyboard: "m" toggles master mute, 1-4 pick a preset (each preset button asks for confirmation).
  const NON_TEXT_INPUTS = new Set(['range', 'checkbox', 'radio', 'button', 'submit', 'reset']);
  function isTextEntry(el) {
    if (!el || !el.tagName) return false;
    if (el.isContentEditable || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') return true;
    return el.tagName === 'INPUT' && !NON_TEXT_INPUTS.has(el.type);
  }
  document.addEventListener('keydown', (e) => {
    if (e.defaultPrevented || e.ctrlKey || e.metaKey || e.altKey || e.repeat || isTextEntry(e.target)) return;
    let button = null;
    if (e.key === 'm' || e.key === 'M') button = document.getElementById('master-mute');
    else if (/^[1-9]$/.test(e.key)) button = document.querySelector(`[data-preset-key="${e.key}"]`);
    if (button && !button.disabled) {
      e.preventDefault();
      button.click();
    }
  });
})();
