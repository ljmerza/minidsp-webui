// Profiles page (docs 05 §7): load a profile into a preset, revert a preset to a revision, point the diff at
// something, and show stored UTC times in local time. Loads and reverts ask first (the server phrases the question
// from a preview), and again when the big-step guard or a FIR load needs it (409 confirmation_required).
(() => {
  'use strict';

  const toast = (message, level) => window.mdsp && window.mdsp.toast(message, level);

  function localTimes(root) {
    for (const el of (root || document).querySelectorAll('time[datetime]')) {
      const when = new Date(el.getAttribute('datetime'));
      if (Number.isNaN(when.getTime())) continue;
      el.textContent = when.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
      el.title = el.getAttribute('datetime');
    }
  }

  async function post(url, body) {
    let response;
    try {
      response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'HX-Request': 'true', Accept: 'application/json' },
        body: JSON.stringify(body),
        credentials: 'same-origin',
      });
    } catch {
      return { ok: false, status: 0, data: { detail: 'the web UI server is unreachable' } };
    }
    const data = await response.json().catch(() => null);
    return { ok: response.ok, status: response.status, data };
  }

  function refusal(result) {
    const data = result.data || {};
    const problems = Array.isArray(data.problems) && data.problems.length ? data.problems.join('; ') : '';
    return problems || data.detail || `Request failed (HTTP ${result.status})`;
  }

  // preview -> confirm -> apply -> (409 -> confirm -> apply with confirm)
  async function guarded(url, body) {
    const preview = await post(url, { ...body, preview: true });
    if (!preview.ok) {
      toast(refusal(preview));
      return false;
    }
    if (!window.confirm(preview.data.question)) return false;
    let result = await post(url, { ...body, confirm: false });
    if (result.status === 409 && result.data && result.data.error === 'confirmation_required') {
      if (!window.confirm(`${result.data.detail}.\n\nApply it anyway?`)) return false;
      result = await post(url, { ...body, confirm: true });
    }
    if (!result.ok) {
      toast(refusal(result));
      return false;
    }
    toast(result.data.message, result.data.level === 'warning' ? 'warning' : 'info');
    return true;
  }

  // Keep the diff selects' profile options in step with the list after save / rename / duplicate / delete.
  function syncDiffOptions() {
    const items = [...document.querySelectorAll('#profile-list [data-profile-id]')];
    const profiles = items.map((el) => ({ value: `profile:${el.dataset.profileId}`, label: `Profile “${el.dataset.profileName}”` }));
    for (const select of document.querySelectorAll('.diff-form select')) {
      const chosen = select.value;
      for (const option of [...select.options]) {
        if (option.value.startsWith('profile:')) option.remove();
      }
      const first = select.options[0] || null;
      for (const p of profiles) {
        const option = new Option(p.label, p.value);
        select.insertBefore(option, first);
      }
      if ([...select.options].some((o) => o.value === chosen)) select.value = chosen;
    }
  }

  document.addEventListener('htmx:afterSwap', (e) => {
    localTimes(e.detail.target);
    if (e.detail.target && e.detail.target.id === 'profile-list') syncDiffOptions();
  });
  document.addEventListener('DOMContentLoaded', () => localTimes(document));

  function profilesPage(cfg) {
    return {
      busy: null,

      reload() {
        if (window.htmx) htmx.trigger(document.body, cfg.reloadEvent);
      },

      async load(id, preset, activate) {
        if (this.busy !== null) return;
        this.busy = id;
        try {
          const url = cfg.loadUrl.replace('{id}', encodeURIComponent(id));
          if (await guarded(url, { preset, activate, serial: cfg.serial })) this.reload();
        } finally {
          this.busy = null;
        }
      },

      async revert(serial, preset, revision) {
        if (this.busy !== null) return;
        this.busy = `r${revision}`;
        try {
          const url = cfg.revertUrl
            .replace('{serial}', encodeURIComponent(serial))
            .replace('{preset}', encodeURIComponent(preset))
            .replace('{revision}', encodeURIComponent(revision));
          if (await guarded(url, {})) this.reload();
        } finally {
          this.busy = null;
        }
      },

      diffWith(a, b) {
        const form = this.$refs.diffForm;
        if (!form) return;
        const pick = (select, value) => {
          if (!select || !value) return;
          if (![...select.options].some((o) => o.value === value)) select.add(new Option(value, value), 0);
          select.value = value;
        };
        pick(this.$refs.diffA, a);
        pick(this.$refs.diffB, b);
        if (!b && this.$refs.diffB && this.$refs.diffB.value === a) {
          const other = [...this.$refs.diffB.options].find((o) => o.value.startsWith('preset:') && o.value !== a);
          if (other) this.$refs.diffB.value = other.value;
        }
        htmx.trigger(form, 'submit');
        document.getElementById('diff').scrollIntoView({ behavior: 'smooth', block: 'start' });
      },
    };
  }

  window.profilesPage = profilesPage;
  if (typeof module !== 'undefined' && module.exports) module.exports = { profilesPage, refusal };
})();
