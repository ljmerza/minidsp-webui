// Device & sync page (phase 9 design §11). The settings table and the last check reload themselves over the event
// stream; this keeps the folds (<details data-keep-open id=…>) and switches (<input data-keep-state id=…>) the way the
// user left them across those reloads. Only what the user changed is kept, so a channel the server opens because it
// has a new problem still opens.
(function () {
  "use strict";

  const opened = new Map(); // details id -> open
  const checked = new Map(); // input id -> checked

  document.addEventListener("click", (event) => {
    const summary = event.target instanceof Element ? event.target.closest("summary") : null;
    const details = summary && summary.parentElement;
    if (!(details instanceof HTMLDetailsElement) || !details.id || !details.hasAttribute("data-keep-open")) return;
    // A control inside the summary (a "?" button) doesn't toggle the fold.
    const control = event.target.closest("button, a, input, select, textarea");
    if (control && summary.contains(control)) return;
    // The fold toggles after the click; read its state then.
    setTimeout(() => opened.set(details.id, details.open), 0);
  });

  document.addEventListener("change", (event) => {
    const input = event.target;
    if (input instanceof HTMLInputElement && input.id && input.hasAttribute("data-keep-state")) {
      checked.set(input.id, input.checked);
    }
  });

  function restore(root) {
    if (!(root instanceof Element)) return;
    const scope = [root, ...root.querySelectorAll("details[data-keep-open][id], input[data-keep-state][id]")];
    for (const el of scope) {
      if (el instanceof HTMLDetailsElement && el.hasAttribute("data-keep-open") && opened.has(el.id)) {
        el.open = opened.get(el.id);
      } else if (el instanceof HTMLInputElement && el.hasAttribute("data-keep-state") && checked.has(el.id)) {
        el.checked = checked.get(el.id);
      }
    }
  }

  document.addEventListener("htmx:load", (event) => restore(event.detail && event.detail.elt));

  // A check that finished while the "Use the device's values" confirm was open wasn't shown (device.html skips that
  // refresh and marks the box data-missed): once the confirm closes, load it.
  document.addEventListener(
    "close",
    (event) => {
      const dialog = event.target;
      if (!(dialog instanceof Element) || !dialog.closest("#verify-dialog-slot")) return;
      setTimeout(() => {
        const box = document.getElementById("verify-box");
        if (!box || !box.dataset.missed || !window.htmx) return;
        if (document.querySelector("#verify-dialog-slot dialog, #verify-panel .htmx-request")) return;
        delete box.dataset.missed;
        window.htmx.trigger(box, "sse:verify");
      }, 0);
    },
    true,
  );
})();
