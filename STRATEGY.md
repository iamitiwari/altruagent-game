# Pokémon baseline to refine

The default strategy is a deterministic, local planner for
`pokemon_vgc_doubles_draft`. It uses the official starter's transport and
assignment runtime. No GPU, model API, or model-generated actions are required.

## Draft a coherent team

For each currently legal card, score:

```
strength + matchup value against the public opposing draft
+ improvement to our team's synergy
+ 0.15 × improvement it would give the opposing team's synergy
```

Strength combines base stats, fixed species priors, and support tools such as
Intimidate, Fake Out, Spore, redirection, and speed control. Synergy rewards
type diversity, physical/special coverage, and weather combinations, and
penalizes accumulating common weaknesses. Trick Room rewards slower teammates
and penalizes mixing in several fast attackers. The small final term values
denying an opponent a useful card, without outweighing our own team building.

Only offered draft picks are considered. The server's legal list handles
already-taken cards and Item Clause. Public fixed sets seen during this match
are retained as permitted match-specific memory.

## Bring four and choose leads

Enumerate all 15 four-of-six subsets. Rank each by team strength, matchup
estimates against the opponent's public roster, and synergy. Then evaluate the
six lead pairs within the winning subset. Fake Out, Intimidate, Tailwind and
Trick Room increase lead value when paired with useful partners.

The final species IDs come from the authoritative preview template. The
adapter checks four distinct choices and two distinct leads contained in them.

## Choose both doubles actions together

Expand each slot's offered moves into its legal targets, plus offered switches
and passes. Evaluate every compatible pair. Reject duplicate reserve switches
and inappropriate double passes before scoring.

The core offensive score is:

```
42 × expected damage capped at each opponent's remaining HP, over all rolls
+ 24 × probability of knocking out each opponent
+ 5 × proportional progress toward a knockout
+ support/speed/survival terms − recoil/switch/friendly-fire costs
```

Damage rolls come from a frozen local copy of `@smogon/calc` 0.12.0, the engine
powering https://calc.pokemonshowdown.com/. It runs inside `quickjs` 1.19.4,
without website requests, Node, npm, or browser automation during play. The
adapter supplies generation 9, level, public draft EVs/IVs/nature/item/ability,
current HP, boosts, status, weather, terrain and selected doubles side effects.
Damage distributions include multi-hit damage rolls. Both actions' outcomes
are combined to score capped damage and KO probability, including an approximate
accuracy/miss adjustment. This distinguishes a possible knockout from a guaranteed
one under the supplied inputs, while retaining the policy's support/switch terms.
Known full-health Focus Sash and Sturdy prevent single-hit knockout credit;
focus fire can still earn it. All-adjacent spread moves count the ally when
determining whether the spread penalty applies.

Unknown set details are explicitly labeled as assumptions. No active ability
modifier, no held item, neutral nature, zero EVs and 31 IVs are defaults only when
those details are unavailable. Variable hit counts and per-hit accuracy remain
approximations. Helping Hand and acting-before-being-KO'd probabilities are still
heuristic policy adjustments. Unsupported inputs or bounded calculator errors
fall back to the earlier estimate, with `backend: approximation` in the forecasts.

Other terms include:

- Fake Out on a freshly observed entry, with a bonus for covering Tailwind,
  Trick Room or a boosting move; conservative behavior after a mid-battle restart.
- Helping Hand paired with an attacking partner; penalized when the partner
  isn't dealing damage.
- Protect when incoming threats justify it, with a penalty for consecutive use.
- Redirection, selected status moves, recovery, speed drops, and Snarl.
- Switching toward safer matchups, while charging a cost for sacrificing a turn.
- Friendly-fire penalties for ally targets and all-adjacent spread attacks,
  adjusted when the partner protects or switches into a resistant/immune reserve.
- Public typing/ability immunity checks and selected priority/terrain interactions.

A final validator checks the returned choices against the exact current action
template. Stale states and authentication recovery remain the starter's job.
Accepted actions alone update last-move history via `on_action_result`.

## Run and verify

```bash
source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m agent.strategy.offline
python -m pytest -q
python -m agent --check-tournament
```

The offline smoke check uses synthetic player-view states. Tests cover strategy
choices, legality, reference data, forced switches, match isolation, accepted
versus rejected actions, and a mocked draft → preview → battle → result through
the official runner, including a stale-state retry. They do not establish
competitive performance or live server compatibility.

Next, keep one process running with `python -m agent --match`, create a Testing
match for Pokémon VGC doubles draft with **Mine (self-hosted)**, and verify a
real complete game. If an older runtime is still running, stop it before starting
the new one during preparation. Do not replace behavior during an official match.

## Refine the policy

Start with practice evidence rather than adding infrastructure:

1. Inspect which decisions lost tempo or missed knockouts. Adjust the named
   `WEIGHTS` dictionary and `TIERS` priors in `agent/strategy/policy.py`.
2. Improve lineup selection by scoring against plausible opposing lead pairs,
   instead of averaging over the whole roster.
3. Add opponent-response scenarios using only public drafted sets and revealed
   player observations. Compare expected and worst-case outcomes to avoid a
   policy that protects too often or assumes every opponent attacks one target.
4. Extend `damage()` and `support_score()` for interactions actually appearing
   in the event's card pools, with focused regression tests.
5. Improve switching and spread-move evaluation using action order, board changes,
   weather/terrain switches, and target-specific protection probabilities.

This baseline is not a full Showdown simulator. The damage engine computes rolls
under its supplied inputs; uncertain sets, action order, opponent choices,
redirection, variable hits, incidental critical hits, accuracy modifiers, and
many joint board interactions are not fully modeled. Pokémon support is limited
to the event's VGC draft mode. A process
restart preserves the runtime seat but loses this policy object's draft/turn
memory; it resumes from current authorized player observations.

## Disclosure and records

- Policy: `vgc-showdown-calc-v2`, programmed strategy with fixed numeric weights.
- External model services: none.
- Gameplay transport/dependencies: those of the official starter (`httpx`,
  `python-dotenv`, and MCP 1.x), plus pinned `quickjs==1.19.4`, listed in
  `pyproject.toml`. The frozen calculator bundles, package metadata, upstream
  license and provenance are under `agent/strategy/vendor/smogon/`.
- Reference data: frozen Gen 9 public tables from `poke-env` 0.16.1, vendored
  under `agent/strategy/data/` with source details, hashes, and license. No
  downloads, searches, spectator views, or remote reference feeds during play.
- Match memory: fresh object per seat/match; public draft history and own accepted
  actions only. No shared opposing-seat memory or cross-match learning.
- Credentials: excluded from source and never passed to the policy.

Player-view observations, offered actions, damage forecasts with ranges and
assumptions, submitted choices, result callbacks,
timestamps, source hash and starting commit are logged to private
`logs/pokemon-<hashed-seat>.jsonl` files. Credential-like fields are redacted;
logs are excluded from Git. The official runtime handles the remaining connection
and lease operations. A starting commit identifies the base checkout; the source
hash identifies the actual strategy bytes, including uncommitted development
changes. Before submission, commit and deploy the exact tested source/configuration,
record the full commit, and preserve the required records under the official rules.
