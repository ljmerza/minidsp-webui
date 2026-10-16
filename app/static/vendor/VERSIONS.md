# Vendored front-end libraries

Copied unmodified from the npm registry tarballs (tarball integrity checked against `npm view <pkg> dist.integrity`).
Nothing is loaded from a CDN at runtime. To upgrade: download the new tarball, copy the same `dist/` files over,
and update the table below (the tests check every sha256 listed here).

| Library | Version | License | Package file | Vendored file | sha256 |
|---|---|---|---|---|---|
| htmx.org | 2.0.11 | 0BSD | `dist/htmx.min.js` | `htmx/htmx.min.js` | `d6fdc75f204e6bdefa99b69bf1e6d4ac69b8a364f77929f45c13476b4000f717` |
| htmx-ext-sse | 2.2.4 | 0BSD | `dist/sse.min.js` | `htmx/sse.min.js` | `98a46496de0c3605fbffdce9167ba427bdd9553184f83f149c261891a92c0136` |
| alpinejs | 3.17.4 | MIT | `dist/cdn.min.js` | `alpine/alpine.min.js` | `232519394c6c8fdba6f362b1d9da16106db513cdbf899011f00daab4051df31c` |
| uplot | 1.6.32 | MIT | `dist/uPlot.iife.min.js` | `uplot/uPlot.iife.min.js` | `19c8d4c6ad88929a79f4ae49d6f7161566dfd0ba3d15cc495e974f787eb78f1f` |
| uplot | 1.6.32 | MIT | `dist/uPlot.min.css` | `uplot/uPlot.min.css` | `df630c6a8d6f8eeaff264b50f73ce5b114f646ffd9a0bb74f049b0a00135fa04` |

License texts: `htmx/LICENSE`, `htmx/LICENSE-htmx-ext-sse`, `uplot/LICENSE`. The alpinejs tarball ships no license
file; its `package.json` declares MIT (full text: https://github.com/alpinejs/alpine/blob/main/LICENSE.md).

Tarballs:

- https://registry.npmjs.org/htmx.org/-/htmx.org-2.0.11.tgz
- https://registry.npmjs.org/htmx-ext-sse/-/htmx-ext-sse-2.2.4.tgz
- https://registry.npmjs.org/alpinejs/-/alpinejs-3.17.4.tgz
- https://registry.npmjs.org/uplot/-/uplot-1.6.32.tgz
