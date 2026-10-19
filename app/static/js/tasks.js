// Guided tasks (phase 9 design §14.1): the wizard dialog.
//
// - Any link to /tasks/<id> of a wizard listed on the dialog (data-tasks) opens that wizard in the dialog instead of
//   navigating: a modal <dialog> (focus trap, Esc), a full-screen sheet on phones. Its steps come from
//   /ui/tasks/<id> and are swapped in by htmx (the step forms carry hx-get / hx-post).
// - Opening pushes a history entry (the page URL plus ?task=<id>&…), so the browser's Back closes it; each step
//   replaces that entry's query, so a refresh reopens the wizard at the same step. ?task=<id> on any page opens it.
// - On every step change focus moves to the step's heading, and the step is announced politely (#task-live).
// - The flow diagram behind the dialog highlights the blocks the step will change (data-highlight).
// - After a write (data-applied) the page under the dialog refreshes; when the dialog closes, a toast offers Undo for
//   what remains of the 30 s.
// - On the /tasks/<id> page itself the same steps swap in place and the server pushes each step's URL.
(() => {
  'use strict';

  const dialog = () => document.getElementById('task-dialog');
  const host = () => {
    const d = dialog();
    return d ? d.querySelector('[data-task-host]') : null;
  };
  const ids = () => ((dialog() && dialog().dataset.tasks) || '').split(/\s+/).filter(Boolean);
  const root = () => (dialog() && dialog().dataset.root) || '';
  const mdsp = () => window.mdsp || {};

  let opener = null;
  let pendingToast = null; // {message, undo, until}
  const UNDO_URL = () => `${root()}/ui/tasks/undo`;

  function say(text) {
    const live = document.getElementById('task-live');
    if (!live || !text) return;
    live.textContent = '';
    setTimeout(() => { live.textContent = text; }, 50);
  }

  // A link's wizard: {id, params} when it points at /tasks/<id> of a wizard the dialog opens.
  function wizardOf(href) {
    let url;
    try {
      url = new URL(href, location.href);
    } catch {
      return null;
    }
    if (url.origin !== location.origin) return null;
    const prefix = `${root()}/tasks/`;
    if (!url.pathname.startsWith(prefix)) return null;
    const id = url.pathname.slice(prefix.length);
    return ids().includes(id) ? { id, params: url.searchParams } : null;
  }

  function clipKeys() {
    const store = window.Alpine && window.Alpine.store ? window.Alpine.store('clip') : null;
    if (!store) return null;
    return store.keys && store.keys.length ? store.keys.join(',') : 'none';
  }

  // The page's URL without the wizard's part (what it was before the dialog opened).
  function stripped() {
    const url = new URL(location.href);
    return url.pathname + (pageQuery ? `?${pageQuery}` : '') + url.hash;
  }

  // The page's own query (before the wizard), kept when the dialog's answers are written into the URL.
  let pageQuery = '';

  function pageUrlWith(id, query) {
    const url = new URL(location.href);
    const params = new URLSearchParams(pageQuery);
    params.set('task', id);
    for (const [k, v] of new URLSearchParams(query)) if (k !== 'serial' || !params.has('serial')) params.set(k, v);
    url.search = params.toString();
    return url.pathname + url.search + url.hash;
  }

  function open(id, params, { push = true, from = null } = {}) {
    const d = dialog();
    const h = host();
    if (!d || !h || !window.htmx) return false;
    const query = new URLSearchParams(params || '');
    query.delete('task');
    query.set('mode', 'dialog');
    if (id === 'diagnose' && !query.has('clip')) {
      const clip = clipKeys();
      if (clip) query.set('clip', clip);
    }
    const serial = document.body.dataset.serial;
    if (serial && !query.has('serial')) query.set('serial', serial);
    if (!d.open) {
      opener = from || document.activeElement;
      pageQuery = new URLSearchParams(location.search);
      pageQuery.delete('task');
      pageQuery = pageQuery.toString();
      d.showModal();
      if (push) history.pushState({ mdspTask: id }, '', pageUrlWith(id, ''));
      else history.replaceState({ mdspTask: id }, '', pageUrlWith(id, ''));
    }
    window.htmx.ajax('GET', `${root()}/ui/tasks/${id}?${query}`, { target: h, swap: 'innerHTML' });
    return true;
  }

  function highlight(keys) {
    for (const el of document.querySelectorAll('.is-wizard-target')) el.classList.remove('is-wizard-target');
    for (const key of keys || []) {
      for (const el of document.querySelectorAll(`[data-flow-key="${CSS.escape(key)}"]`)) el.classList.add('is-wizard-target');
    }
  }

  function refreshPage() {
    const header = document.getElementById('app-header');
    if (header && window.htmx) window.htmx.trigger(header, 'refresh');
    document.body.dispatchEvent(new CustomEvent('mdsp-sync-changed', { bubbles: true }));
    for (const el of document.querySelectorAll('[data-output-cards], .home-presets-box')) {
      if (window.htmx) window.htmx.trigger(el, 'refresh');
    }
  }

  function finish() {
    // The dialog has closed: clean up, give focus back, and offer Undo once more.
    const h = host();
    highlight([]);
    if (h) h.innerHTML = '';
    if (opener && opener.isConnected) opener.focus();
    opener = null;
    const pending = pendingToast;
    pendingToast = null;
    if (pending && Date.now() < pending.until) undoToast(pending);
  }

  function undoToast(pending) {
    const toast = mdsp().toast;
    if (!toast) return;
    const left = Math.max(1500, pending.until - Date.now());
    toast(pending.message, 'info', {
      timeout: Math.min(8000, left),
      action: {
        label: 'Undo',
        onClick: async () => {
          let message = 'Undo failed.';
          try {
            const response = await fetch(UNDO_URL() + (document.body.dataset.serial ? `?serial=${document.body.dataset.serial}` : ''), {
              method: 'POST',
              headers: { 'Content-Type': 'application/json', 'HX-Request': 'true', Accept: 'application/json' },
              body: JSON.stringify({ record: pending.undo }),
              credentials: 'same-origin',
            });
            const data = await response.json().catch(() => null);
            if (data && data.message) message = data.message;
          } catch {
            message = 'Request failed: the web UI server is unreachable';
          }
          refreshPage();
          toast(message, 'info');
        },
      },
    });
  }

  function requestClose() {
    const d = dialog();
    if (!d || !d.open) return;
    if (history.state && history.state.mdspTask) {
      history.back(); // popstate closes it, and the URL loses ?task
    } else {
      d.close();
      history.replaceState(null, '', stripped());
    }
  }

  // A link inside a menu (Adjust ▾, a header sheet, the phone's ≡ sheet): close it first. Returns what should get focus
  // back when the wizard closes (the menu's button), or null for the link itself.
  function closeMenus(link) {
    let back = null;
    const details = link.closest('details[open]');
    if (details) {
      details.open = false;
      back = details.querySelector('summary');
    }
    const sheet = link.closest('.hdr-sheet');
    if (sheet && window.Alpine) {
      const data = window.Alpine.$data(sheet);
      if (data && data.open) data.close(false);
      back = sheet.querySelector('.hdr-sheet-toggle') || back;
    }
    if (link.closest('#sidenav') && document.body.classList.contains('nav-open') && window.Alpine) {
      const shell = document.querySelector('.shell');
      const data = shell ? window.Alpine.$data(shell) : null;
      if (data && data.open) data.close(false);
      back = document.querySelector('.nav-toggle') || back;
    }
    return back;
  }

  // --- wiring ----------------------------------------------------------------------------------------------------------

  document.addEventListener('click', (e) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const target = e.target instanceof Element ? e.target : null;
    if (!target) return;
    if (target.closest('[data-task-close]')) {
      e.preventDefault();
      requestClose();
      return;
    }
    const link = target.closest('a[href]');
    if (!link || link.target === '_blank' || link.hasAttribute('download')) return;
    if (document.querySelector('[data-task-host][data-mode="page"]') && !dialog().open) {
      // On a wizard's own page its links stay page links (another wizard still opens in the dialog).
      const found = wizardOf(link.href);
      const here = document.querySelector('[data-task-host][data-mode="page"] [data-task-step]');
      if (!found || (here && here.dataset.task === found.id)) return;
    }
    const found = wizardOf(link.href);
    if (!found) return;
    e.preventDefault();
    open(found.id, found.params, { push: !dialog().open, from: closeMenus(link) });
  });

  document.addEventListener('DOMContentLoaded', () => {
    const d = dialog();
    if (!d) return;
    d.addEventListener('cancel', (e) => {
      e.preventDefault(); // Esc: close the same way as ✕ (so the history entry goes too)
      requestClose();
    });
    d.addEventListener('close', finish);
    // ?task=<id> on any page opens it (design §14.1), with Back closing it: the page's URL without the wizard's
    // answers goes under it in the history (only ``serial`` belongs to the page; the rest are the wizard's).
    const params = new URLSearchParams(location.search);
    const id = params.get('task');
    if (id && ids().includes(id)) {
      params.delete('task');
      const url = new URL(location.href);
      const page = new URLSearchParams();
      if (params.has('serial')) page.set('serial', params.get('serial'));
      history.replaceState(null, '', url.pathname + (page.toString() ? `?${page}` : '') + url.hash);
      open(id, params);
    }
  });

  window.addEventListener('popstate', (e) => {
    const d = dialog();
    if (!d) return;
    const state = e.state || {};
    if (d.open && !state.mdspTask) {
      d.close();
      return;
    }
    if (!d.open && state.mdspTask) {
      const params = new URLSearchParams(location.search);
      const id = params.get('task');
      if (id && ids().includes(id)) open(id, params, { push: false });
    }
  });

  // A step arrived (in the dialog or on the page): focus, announce, URL, highlight, refresh, toast.
  document.addEventListener('htmx:afterSettle', (e) => {
    const target = e.detail.target;
    if (!(target instanceof Element) || !target.matches('[data-task-host]')) return;
    const step = target.querySelector('[data-task-step]');
    if (!step) return;
    const heading = step.querySelector('.task-heading');
    if (heading) heading.focus();
    say(step.dataset.announce);
    const inDialog = target.dataset.mode === 'dialog';
    if (inDialog) {
      history.replaceState({ mdspTask: step.dataset.task }, '', pageUrlWith(step.dataset.task, step.dataset.taskQuery || ''));
      try {
        highlight(JSON.parse(step.dataset.highlight || '[]'));
      } catch {
        highlight([]);
      }
    }
    if (step.dataset.applied === 'true') refreshPage();
    if (step.dataset.undo && Number(step.dataset.undoLeft) > 0) {
      pendingToast = {
        message: step.dataset.toast || 'Done.',
        undo: step.dataset.undo,
        until: Date.now() + Number(step.dataset.undoLeft) * 1000,
      };
    } else if (step.dataset.applied === 'true') {
      pendingToast = null;
    }
    // Undo buttons go away when their 30 s are up.
    for (const button of step.querySelectorAll('[data-undo-left]')) {
      const left = Number(button.dataset.undoLeft);
      if (button.tagName === 'BUTTON' && left > 0) {
        setTimeout(() => {
          const form = button.closest('form');
          if (form && form.isConnected) form.remove();
        }, left * 1000);
      }
    }
  });

  // A refused write comes back as a step too; a server error doesn't: say so in the dialog.
  document.addEventListener('htmx:responseError', (e) => {
    const target = e.detail.target;
    if (!(target instanceof Element) || !target.matches('[data-task-host]')) return;
    if (mdsp().toast && !dialog().open) mdsp().toast(`Request failed (HTTP ${e.detail.xhr.status})`);
  });

  window.mdsp = Object.assign(window.mdsp || {}, { tasks: { open, wizardOf } });
})();
