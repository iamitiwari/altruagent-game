# Frozen official Showdown calculator

Package: `@smogon/calc` **0.12.0**.

Upstream: https://github.com/smogon/damage-calc

Downloaded during development from:
https://registry.npmjs.org/@smogon/calc/-/calc-0.12.0.tgz

The following package files are vendored without modifications:

- `dist/data/production.min.js` → `data.min.js`
- `dist/production.min.js` → `calc.min.js`
- `package.json` → `package.json`

The MIT license is retained in `LICENSE`, fetched from the upstream repository.
The Python adapter and `calc_bridge.js` are this agent's integration code.

SHA-256:

- `data.min.js`: `b6037ccba7bfdabc332041e237cf19cfddee8d6037f9ee3ccba6fda55208e873`
- `calc.min.js`: `ee755aaac6819fe1fd81277ca327fdda161d17f9ff8897f03296a50bbc060471`
- `LICENSE`: `c09470bbbd54fe28e8ca5d8a16b55f234150a0d82bc9b46e4b28c5475e6f0309`

These bundles contain mechanics and static generation data. They do not contain
live match observations or default competitive set feeds. They are loaded from
disk into pinned `quickjs==1.19.4`, once per match context, with a bounded
execution time, memory limit, no browser DOM, no Python callbacks, and no network
or file access from the embedded engine.
