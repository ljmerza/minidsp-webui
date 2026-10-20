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

  // Toasts appear next to the control that caused them (phase 9 design §3.6): `options.near`, else the control the
  // user last pressed (within a few seconds). On phones, and when there is no such control, they sit at the bottom,
  // above the phone's bottom bar. They are always kept inside the screen.
  const ANCHOR_MS = 4000;
  const EDGE = 12;
  let lastControl = null;
  let lastControlAt = 0;
  function rememberControl(e) {
    const el = e.target instanceof Element ? e.target.closest('button, a, input, select, label, [role="button"]') : null;
    if (!el || el.closest('#toasts')) return;
    lastControl = el;
    lastControlAt = Date.now();
  }
  document.addEventListener('pointerdown', rememberControl, true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') rememberControl(e); }, true);
  document.addEventListener('change', rememberControl, true);

  function placeToasts(region, near) {
    const phone = window.matchMedia && window.matchMedia('(max-width: 599px)').matches;
    let anchor = near;
    if (!anchor && lastControl && Date.now() - lastControlAt < ANCHOR_MS) anchor = lastControl;
    const rect = anchor && anchor.isConnected ? anchor.getBoundingClientRect() : null;
    if (phone || !rect || (rect.width === 0 && rect.height === 0)) {
      region.removeAttribute('style');
      region.classList.remove('is-anchored');
      return;
    }
    const vw = document.documentElement.clientWidth;
    const vh = document.documentElement.clientHeight;
    const width = Math.min(512, vw - 2 * EDGE);
    const left = Math.max(EDGE, Math.min(rect.left, vw - width - EDGE));
    region.classList.add('is-anchored');
    region.style.left = `${Math.round(left)}px`;
    region.style.right = 'auto';
    region.style.width = `${Math.round(width)}px`;
    if (rect.bottom + 8 + 120 <= vh) {
      region.style.top = `${Math.round(Math.max(EDGE, rect.bottom + 8))}px`;
      region.style.bottom = 'auto';
    } else {
      region.style.top = 'auto';
      region.style.bottom = `${Math.round(Math.max(EDGE, vh - rect.top + 8))}px`;
    }
  }

  // `options.timeout` (ms, default 8 s) and `options.action` ({label, url, values, target, swap}): a button that POSTs
  // `values` to `url` with htmx and swaps the reply into `target` (e.g. Undo). Instead of a url, `action.onClick` may
  // be a function (its promise is awaited). The toast goes away when it is used, and its timer pauses while the
  // pointer is over it or focus is inside it (phase9 design §3.6). A toast takes one tab stop: its action, else its
  // close button; Esc closes it. `options.near`: the element to show it next to.
  function toast(message, level = 'error', options = {}) {
    const region = document.getElementById('toasts');
    if (!region) return null;
    placeToasts(region, options.near || null);
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
    if (runs) close.tabIndex = -1; // one tab stop: the action
    item.append(close);
    item.addEventListener('keydown', (e) => {
      if (e.key !== 'Escape') return;
      e.stopPropagation();
      const returnTo = action && action.returnFocus;
      item.remove();
      if (returnTo && returnTo.isConnected) returnTo.focus();
    });
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

  // A refresh never replaces a button or link while it is being pressed: a click needs the same element under the
  // press and the release, so a swap in between lost it (Mute all and the preset buttons in the header, the Home cards'
  // Mute, "Switch to preset N"). Such a swap is dropped instead, and once the press is over the part is fetched again
  // with its own hx-get, unless the click sent a request whose reply goes into that part anyway. Only a refresh is held
  // (a GET of the part's own hx-get, which can be fetched again): a reply to a write or to another GET (Duplicate's
  // "Created …", "Problems only") carries what no refresh does, so it is swapped as before, as is a part without an
  // hx-get of its own.
  const PRESS_MAX_MS = 10000; // a release that never arrives (e.g. the window lost focus) doesn't hold swaps for good
  let press = null; // { el, at }: the button or link under a primary pointer that is down
  const heldParts = new Map(); // a part whose refresh was dropped during the press -> whether a reply will refresh it
  const pressing = () => !!press && press.el.isConnected && performance.now() - press.at < PRESS_MAX_MS;
  const isRefresh = (part, config) =>
    !!config && String(config.verb).toLowerCase() === 'get' && config.path === part.getAttribute('hx-get');
  document.addEventListener('pointerdown', (e) => {
    const el = e.button === 0 && e.target instanceof Element ? e.target.closest('button, a[href]') : null;
    press = el ? { el, at: performance.now() } : null;
  }, true);
  function catchUp() {
    if (pressing()) return; // pressed again meanwhile: that release catches up
    for (const [part, answered] of heldParts) {
      if (!answered && part.isConnected && window.htmx) {
        window.htmx.ajax('GET', part.getAttribute('hx-get'), { source: part, target: part });
      }
    }
    heldParts.clear();
  }
  const endPress = () => {
    if (!press) return;
    press = null;
    if (heldParts.size) setTimeout(catchUp, 0); // after the click that follows the release
  };
  document.addEventListener('pointerup', endPress, true);
  document.addEventListener('pointercancel', endPress, true);
  document.addEventListener('htmx:beforeSwap', (e) => {
    const part = e.detail.target;
    if (!e.detail.shouldSwap || !pressing() || !(part instanceof Element) || !part.contains(press.el)) return;
    if (!part.hasAttribute('hx-get') || !isRefresh(part, e.detail.requestConfig)) return;
    e.detail.shouldSwap = false;
    heldParts.set(part, false); // even if a reply was expected: this may have been it
  });
  // Only a request sent once the press is over (the click's) answers a held refresh; one sent during the press is
  // another refresh, and its reply is dropped too.
  document.addEventListener('htmx:beforeRequest', (e) => {
    const { elt, target } = e.detail;
    if (pressing() || !heldParts.has(target) || !(elt instanceof Element) || !target.contains(elt)) return;
    heldParts.set(target, true);
  });

  // A part that a noisy event re-fetches (a Volume drag sends a "master" event per step) is fetched at most once per
  // window, and its other triggers keep working. htmx's own "throttle:" can't do this: it throttles the element, so for
  // the whole window the part ignored every trigger (its "refresh", sse:sync …), and it drops the last event, so the
  // part could stay stale. Instead a hidden receiver (hx-trigger="sse:…", no hx-get) with data-throttle-refresh="<ms>"
  // triggers "refresh" on data-refresh-target (a selector) at the first event, then once more at the end of every
  // window that saw more events, so the last state always shows. data-refresh-now="master.preset …": an event that
  // changes one of these fields of its payload is fetched at once. State is kept per target, so a receiver inside the
  // part it refreshes may be swapped out with it.
  const throttled = new Map(); // target selector -> { timer, pending, seen: Map(field -> value) }
  function refreshPart(selector) {
    const part = document.querySelector(selector);
    if (part && window.htmx) window.htmx.trigger(part, 'refresh');
  }
  function fieldChanged(receiver, message, seen) {
    const fields = (receiver.getAttribute('data-refresh-now') || '').split(/\s+/).filter(Boolean);
    if (!fields.length) return false;
    let data = null;
    try {
      data = JSON.parse(message && message.data);
    } catch {
      return false;
    }
    let changed = false;
    for (const field of fields) {
      const value = field.split('.').reduce((v, key) => (v && typeof v === 'object' ? v[key] : undefined), data);
      if (value === undefined) continue;
      if (seen.has(field) && seen.get(field) !== value) changed = true;
      seen.set(field, value);
    }
    return changed;
  }
  document.addEventListener('htmx:sseMessage', (e) => {
    const receiver = e.target;
    if (!(receiver instanceof Element) || !receiver.hasAttribute('data-throttle-refresh')) return;
    const selector = receiver.getAttribute('data-refresh-target');
    if (!selector) return;
    const every = Number(receiver.getAttribute('data-throttle-refresh')) || 1000;
    let state = throttled.get(selector);
    if (!state) {
      state = { timer: null, pending: false, seen: new Map() };
      throttled.set(selector, state);
    }
    const now = fieldChanged(receiver, e.detail, state.seen);
    if (state.timer !== null && !now) {
      state.pending = true;
      return;
    }
    clearTimeout(state.timer);
    state.pending = false;
    refreshPart(selector);
    const tick = () => {
      if (!state.pending) {
        state.timer = null;
        return;
      }
      state.pending = false;
      refreshPart(selector);
      state.timer = setTimeout(tick, every);
    };
    state.timer = setTimeout(tick, every);
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
    // Parts rendered while settings were still being sent look again once the feed is open: the "sync" event saying
    // the writes finished may have gone out before this page connected, which left "Sending" on screen for good.
    for (const el of document.querySelectorAll('[data-resync-on-open]')) {
      if (window.htmx) window.htmx.trigger(el, 'mdsp-resync');
    }
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
