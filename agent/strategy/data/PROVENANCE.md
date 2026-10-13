# Frozen Pokémon reference data

`pokedex.json` and `moves.json` are the Gen 9 static tables extracted from the
PyPI wheel `poke_env-0.16.1-py3-none-any.whl` during development on 2026-10-13.
They are public species/move reference data, not live match observations.
The packaged source license is retained in `POKE_ENV_LICENSE`.

Upstream: https://github.com/hsahovic/poke-env
PyPI release: https://pypi.org/project/poke-env/0.16.1/
Showdown upstream: https://github.com/smogon/pokemon-showdown

SHA-256 of the vendored bytes:

- pokedex.json: `05f15c92e2d223f89ad911da5ac958e50a0ae64964ceab0bd139cf0c28b1d7b0`
- moves.json: `4b25b4df1768255652d0a1f1e314961e9573b63f4177a9e10a1123eeaad7e291`

These files are not refreshed during matches. The hand-maintained generation
6+ type-effectiveness table is in `agent/strategy/dex.py`.
