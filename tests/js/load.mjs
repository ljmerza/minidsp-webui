// Loads app/static/js files the way the browser does (classic scripts that attach to `window`), without a DOM.
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

export const ROOT = new URL('../../', import.meta.url);

export function loadScripts(...paths) {
  const saved = globalThis.window;
  globalThis.window = {};
  try {
    for (const path of paths) {
      vm.runInThisContext(readFileSync(new URL(path, ROOT), 'utf8'), { filename: path });
    }
    return globalThis.window.mdsp;
  } finally {
    if (saved === undefined) delete globalThis.window;
    else globalThis.window = saved;
  }
}

export const readJson = (path) => JSON.parse(readFileSync(path instanceof URL ? path : new URL(path, ROOT), 'utf8'));
