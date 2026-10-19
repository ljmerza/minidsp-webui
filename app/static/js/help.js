// "?" toggletips (phase 9 design §13.1). The markup comes from app/web/glossary.py help_button(): a disclosure button
// ([data-tt-btn], aria-expanded, aria-controls) followed by its popover ([data-tt-pop], role="dialog", hidden).
//
// Click, tap, Enter or Space toggle it (Enter and Space reach us as clicks on the native button); hover never opens it.
// Esc or a click outside closes it, and Esc returns focus to its button. One is open at a time. Where the browser has
// the Popover API the panel is shown in the top layer (popover="manual"), so a table cell or a scroll box can't clip
// it; elsewhere it is a fixed-position panel. Either way this script positions it next to its button.
//
// Listeners are delegated on the document, so toggletips in HTMX-swapped content (the header, the sync table) work
// without re-initialising anything.
(function () {
  'use strict';

  const MARGIN = 8; // px between the panel and the viewport edge / its button
  const hasPopover = typeof HTMLElement !== 'undefined' && Object.prototype.hasOwnProperty.call(HTMLElement.prototype, 'popover');
  let open = null; // { btn, pop }

  function panelOf(btn) {
    const id = btn.getAttribute('aria-controls');
    return id ? document.getElementById(id) : null;
  }

  function place(btn, pop) {
    const r = btn.getBoundingClientRect();
    const vw = document.documentElement.clientWidth;
    const vh = document.documentElement.clientHeight;
    pop.style.maxWidth = `${Math.min(320, vw - 2 * MARGIN)}px`;
    const w = pop.offsetWidth;
    const h = pop.offsetHeight;
    let left = r.left + r.width / 2 - w / 2;
    left = Math.max(MARGIN, Math.min(left, vw - w - MARGIN));
    // The glyph is 20 px inside a 44 px hit area: hang the panel just under the glyph, or above it if it won't fit.
    const glyph = btn.querySelector('.tt-glyph');
    const g = glyph ? glyph.getBoundingClientRect() : r;
    let top = g.bottom + MARGIN / 2;
    if (top + h > vh - MARGIN && g.top - MARGIN / 2 - h >= MARGIN) top = g.top - MARGIN / 2 - h;
    pop.style.left = `${Math.round(left)}px`;
    pop.style.top = `${Math.round(Math.max(MARGIN, top))}px`;
  }

  function show(btn, pop) {
    if (open) close(false);
    pop.hidden = false;
    if (hasPopover) {
      if (!pop.hasAttribute('popover')) pop.setAttribute('popover', 'manual');
      try {
        pop.showPopover();
      } catch (_) {
        /* already shown or detached: the fixed-position fallback still works */
      }
    }
    pop.classList.add('is-open');
    btn.setAttribute('aria-expanded', 'true');
    open = { btn, pop };
    place(btn, pop);
  }

  function close(returnFocus) {
    if (!open) return;
    const { btn, pop } = open;
    open = null;
    if (hasPopover && pop.hasAttribute('popover')) {
      try {
        pop.hidePopover();
      } catch (_) {
        /* not shown */
      }
    }
    pop.classList.remove('is-open');
    pop.hidden = true;
    btn.setAttribute('aria-expanded', 'false');
    if (returnFocus && btn.isConnected) btn.focus();
  }

  document.addEventListener('click', (event) => {
    const btn = event.target.closest && event.target.closest('[data-tt-btn]');
    if (btn) {
      event.preventDefault();
      event.stopPropagation(); // a toggletip inside a clickable row or summary must not trigger it
      const pop = panelOf(btn);
      if (!pop) return;
      if (open && open.btn === btn) close(false);
      else show(btn, pop);
      return;
    }
    if (open && !open.pop.contains(event.target)) close(false);
  }, true);

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && open) {
      event.preventDefault();
      event.stopPropagation(); // the toggletip closes, not an enclosing dialog
      close(true);
    }
  }, true);

  // Tabbing away from the button and its panel closes it (the panel isn't modal, so focus may leave).
  document.addEventListener('focusin', (event) => {
    if (open && !open.btn.contains(event.target) && !open.pop.contains(event.target)) close(false);
  });

  // A swap (HTMX) that removes an open toggletip's button closes it; scrolling or resizing moves it along.
  function follow() {
    if (!open) return;
    if (!open.btn.isConnected || !open.pop.isConnected) {
      open = null;
      return;
    }
    place(open.btn, open.pop);
  }
  window.addEventListener('scroll', follow, { passive: true, capture: true });
  window.addEventListener('resize', follow, { passive: true });
  document.addEventListener('htmx:afterSwap', follow);
})();
