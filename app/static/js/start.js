// The Start page setting (phase 9 design §12.2): Home or Meters & graph, per browser (localStorage), so a phone and a
// desktop can differ.
//
// - On Home (loaded in its <head> with data-start-page, so it runs before the page draws) it sends a browser whose
//   choice is Meters & graph on to /meters, but only when the app was opened from outside: a bookmark, a typed
//   address or a home-screen icon. Links inside the app (the menu's Home, the device name) and reloads stay on Home.
// - On Settings, the startPage component reads and saves the choice.
(() => {
  'use strict';

  const KEY = 'mdsp.startPage';
  const CHOICES = ['home', 'meters'];

  function read() {
    try {
      const value = window.localStorage.getItem(KEY);
      return CHOICES.includes(value) ? value : 'home';
    } catch {
      return 'home'; // storage blocked: the default
    }
  }

  function write(value) {
    try {
      if (value === 'home') window.localStorage.removeItem(KEY);
      else window.localStorage.setItem(KEY, value);
      return true;
    } catch {
      return false;
    }
  }

  // True when this page load came from outside the app (no same-origin referrer, and not a reload or Back).
  function openedFromOutside() {
    let type = 'navigate';
    try {
      const entry = performance.getEntriesByType('navigation')[0];
      if (entry && entry.type) type = entry.type;
    } catch {
      // older browsers: treat it as a fresh navigation
    }
    if (type !== 'navigate') return false;
    if (!document.referrer) return true;
    try {
      return new URL(document.referrer).origin !== window.location.origin;
    } catch {
      return true;
    }
  }

  const script = document.currentScript;
  if (script && script.dataset && 'startPage' in script.dataset && read() === 'meters' && openedFromOutside()) {
    const target = script.dataset.meters || '/meters';
    window.location.replace(target + window.location.search + window.location.hash);
  }

  function startPage() {
    return {
      value: 'home',
      saved: '',
      init() {
        this.value = read();
      },
      choose(value) {
        if (!CHOICES.includes(value)) return;
        this.value = value;
        this.saved = write(value) ? 'Saved for this browser.' : "This browser doesn't allow saving it.";
      },
    };
  }

  window.mdsp = Object.assign(window.mdsp || {}, { start: { read, write, openedFromOutside } });

  document.addEventListener('alpine:init', () => {
    window.Alpine.data('startPage', startPage);
  });
})();
