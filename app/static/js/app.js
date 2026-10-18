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

  // `options.timeout` (ms, default 8 s) and `options.action` ({label, url, values, target, swap}): a button that POSTs
  // `values` to `url` with htmx and swaps the reply into `target` (e.g. Undo). Instead of a url, `action.onClick` may
  // be a function (its promise is awaited). The toast goes away when it is used, and its timer pauses while the
  // pointer is over it or focus is inside it (phase9 design §3.6).
  function toast(message, level = 'error', options = {}) {
    const region = document.getElementById('toasts');
    if (!region) return null;
    const item = document.createElement('div');
    item.className = `toast toast-${level}`;
    item.setAttribute('role', level === 'error' ? 'alert' : 'status');
    const text = document.createElement('span');
    text.className = 'toast-text';
    text.textContent = message;
    item.append(text);
    const action = options.action;
    const runs = action && action.label && (typeof action.onClick === 'function' || (action.url && window.htmx));
    if (runs) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'toast-action';
      button.textContent = action.label;
      let busy = false; // not `disabled`: a disabled button drops focus, and focus should follow the result
      button.addEventListener('click', () => {
        if (busy) return;
        busy = true;
        button.setAttribute('aria-disabled', 'true');
        const done =
          typeof action.onClick === 'function'
            ? Promise.resolve().then(() => action.onClick())
            : htmx.ajax('POST', action.url, {
                source: button,
                target: action.target || 'body',
                swap: action.swap || 'none',
                values: action.values || {},
              });
        done.catch(() => {}).finally(() => {
          const hadFocus = item.contains(document.activeElement);
          item.remove();
          const scope = hadFocus && action.target ? document.querySelector(action.target) : null;
          const next = scope ? scope.querySelector('[data-autofocus]') : null;
          if (next) next.focus();
          else if (hadFocus && action.returnFocus && action.returnFocus.isConnected) action.returnFocus.focus();
        });
      });
      item.append(button);
    }
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast-close';
    close.setAttribute('aria-label', 'Dismiss');
    close.textContent = '×';
    close.addEventListener('click', () => item.remove());
    item.append(close);
    region.append(item);
    while (region.children.length > 4) region.firstElementChild.remove();
    let left = options.timeout || 8000;
    let since = Date.now();
    let timer = setTimeout(() => item.remove(), left);
    let hovered = false;
    const pause = () => {
      if (timer === null) return;
      clearTimeout(timer);
      timer = null;
      left = Math.max(1500, left - (Date.now() - since));
    };
    const resume = () => {
      if (timer !== null || hovered || item.contains(document.activeElement)) return;
      since = Date.now();
      timer = setTimeout(() => item.remove(), left);
    };
    item.addEventListener('pointerenter', () => { hovered = true; pause(); });
    item.addEventListener('pointerleave', () => { hovered = false; resume(); });
    item.addEventListener('focusin', pause);
    item.addEventListener('focusout', (e) => {
      if (!item.contains(e.relatedTarget)) setTimeout(resume, 0);
    });
    return item;
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

  // A server reply asked for a toast (HX-Trigger "mdsp-toast": {message, level, timeout_ms, action}).
  document.addEventListener('mdsp-toast', (e) => {
    const d = e.detail || {};
    if (d.message) toast(d.message, d.level || 'info', { timeout: d.timeout_ms, action: d.action });
  });

  // Server-rendered confirm dialogs (<dialog data-modal>): opened as a modal when swapped in, so focus is trapped
  // and Esc cancels. Closing removes the dialog and returns focus to the control that opened it. If the dialog's own
  // request replaced the page part holding it, focus moves to that part's [data-autofocus] element instead.
  let lastOpener = null;
  document.addEventListener('htmx:beforeRequest', (e) => {
    if (!(e.target instanceof Element) || !e.target.closest('dialog')) lastOpener = document.activeElement;
  });
  function openDialogs(root) {
    if (!(root instanceof Element)) return;
    const found = root.matches('dialog[data-modal]') ? [root] : root.querySelectorAll('dialog[data-modal]');
    for (const dialog of found) {
      if (dialog.open || typeof dialog.showModal !== 'function') continue;
      const opener = lastOpener;
      dialog.addEventListener('close', () => {
        dialog.remove();
        if (opener && opener.isConnected) opener.focus();
      });
      dialog.addEventListener('click', (ev) => {
        if (ev.target instanceof Element && ev.target.closest('[data-dialog-close]')) dialog.close();
      });
      dialog.showModal();
    }
  }
  // Only a swap that replaced the focused element (or the one that sent the request, if focus already dropped) hands
  // focus on; a background refresh never takes it. Keyed by the target's id, as other swaps settle in between.
  const focusWasInside = new Set();
  document.addEventListener('htmx:beforeSwap', (e) => {
    const target = e.detail.target;
    if (!(target instanceof Element) || !target.id) return;
    const active = document.activeElement;
    const source = e.detail.requestConfig && e.detail.requestConfig.elt;
    const dropped = !active || active === document.body;
    if (target.contains(active) || (dropped && source instanceof Element && source !== target && target.contains(source))) {
      focusWasInside.add(target.id);
    }
  });
  document.addEventListener('htmx:afterSettle', (e) => {
    const target = e.detail.target;
    if (!(target instanceof Element)) return;
    // After an outerHTML swap the target is the replaced element; its successor carries the same id.
    const scope = target.isConnected ? target : target.id ? document.getElementById(target.id) : null;
    if (!scope) return;
    openDialogs(scope);
    const handOn = focusWasInside.delete(scope.id);
    const lost = !document.activeElement || document.activeElement === document.body;
    const next = lost && handOn ? scope.querySelector('[data-autofocus]') : null;
    if (next) next.focus();
  });

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
