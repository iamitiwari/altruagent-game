"""Offline adapter for the engine powering calc.pokemonshowdown.com.

Only explicit player-view inputs and frozen reference tables enter this engine.
Damage distributions are conditional on hit count, accuracy, and supplied sets.
"""
from __future__ import annotations

import json
import math
from bisect import bisect_left
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from functools import lru_cache
import quickjs
from .dex import ident

ROOT = Path(__file__).resolve().parent
BACKEND = '@smogon/calc@0.12.0'
STATUS = {'burn': 'brn', 'brn': 'brn', 'paralysis': 'par', 'par': 'par', 'poison': 'psn',
          'psn': 'psn', 'toxic': 'tox', 'tox': 'tox', 'sleep': 'slp', 'slp': 'slp',
          'freeze': 'frz', 'frz': 'frz'}
WEATHER = {'sun': 'Sun', 'sunnyday': 'Sun', 'harshsunshine': 'Harsh Sunshine',
           'desolateland': 'Harsh Sunshine', 'rain': 'Rain', 'raindance': 'Rain',
           'heavyrain': 'Heavy Rain', 'primordialsea': 'Heavy Rain', 'sand': 'Sand',
           'sandstorm': 'Sand', 'hail': 'Hail', 'snow': 'Snow', 'snowscape': 'Snow',
           'strongwinds': 'Strong Winds', 'deltastream': 'Strong Winds'}


@lru_cache(maxsize=3)
def _script_text(path):
    return path.read_text()


def present(container, name):
    if isinstance(container, dict):
        return any(ident(k) == name and bool(v) for k, v in container.items())
    return any(ident(x.get('name') or x.get('id')) == name if isinstance(x, dict) else ident(x) == name for x in (container or []))


def hp_fraction(p):
    if p.get('fainted'):
        return 0.0
    if p.get('current_hp_fraction') is not None:
        return max(0., min(1., float(p['current_hp_fraction'])))
    if p.get('current_hp') is not None and p.get('max_hp'):
        return max(0., min(1., p['current_hp'] / p['max_hp']))
    return 1.0


@dataclass(frozen=True)
class DamagePrediction:
    # Damage normalized to calculator HP under the disclosed input assumptions.
    distribution: tuple[tuple[float, float], ...]
    min_damage: float
    max_damage: float
    expected_fraction: float
    hit_chance: float
    assumptions: tuple[str, ...] = ()
    backend: str = BACKEND

    def outcomes(self, multiplier=1.):
        result = [(d * multiplier, p * self.hit_chance) for d, p in self.distribution]
        if self.hit_chance < 1:
            result.append((0., 1 - self.hit_chance))
        return result

    def ko_chance(self, remaining_fraction):
        if remaining_fraction <= 0:
            return 0.
        return min(1., sum(p for d, p in self.outcomes() if d + 1e-9 >= remaining_fraction))

    def public(self):
        return {'backend': self.backend, 'min_hp_fraction': self.min_damage,
                'max_hp_fraction': self.max_damage, 'expected_hp_fraction': self.expected_fraction,
                'hit_chance': self.hit_chance, 'assumptions': list(self.assumptions)}


def joint_damage_metrics(a, b, remaining):
    """Expected capped damage and KO chance for independent action outcomes.

    Prefix sums avoid multiplying the number of multi-hit roll outcomes.
    Damage fractions must be normalized to the same target max HP.
    """
    if remaining <= 0:
        return 0., 0.
    b = sorted(b)
    amounts = [d for d, _ in b]
    prefix_p, prefix_damage = [0.], [0.]
    for d, prob in b:
        prefix_p.append(prefix_p[-1] + prob)
        prefix_damage.append(prefix_damage[-1] + d * prob)
    expected, ko = 0., 0.
    for damage, prob in a:
        i = bisect_left(amounts, remaining - damage - 1e-9)
        above = prefix_p[-1] - prefix_p[i]
        expected += prob * (damage * prefix_p[i] + prefix_damage[i] + remaining * above)
        ko += prob * above
    return max(0., expected), max(0., min(1., ko))


class DamageCalculator:
    def __init__(self, dex):
        self.dex = dex
        self.context = quickjs.Context()
        self.context.set_memory_limit(128 * 1024 * 1024)
        self.context.set_max_stack_size(2 * 1024 * 1024)
        self.context.eval('var window = globalThis; var console = {log:function(){}, warn:function(){}, error:function(){}};')
        for name in ('data.min.js', 'calc.min.js'):
            self.context.eval(_script_text(ROOT / 'vendor' / 'smogon' / name))
        self.context.eval(_script_text(ROOT / 'calc_bridge.js'))
        self.calculate_js = self.context.get('altruCalculate')
        self.context.set_time_limit(.25)
        self.cache = OrderedDict()
        self.calls = 0
        self.cache_hits = 0

    def _pokemon(self, p):
        key = ident(p.get('species') or p.get('name'))
        raw = self.dex.species.get(key)
        if not raw:
            raise ValueError('Species missing from fixed reference data')
        assumptions = []
        opts = {'level': p.get('level') or 50, 'nature': p.get('nature') or 'Serious',
                'ability': p.get('ability') or 'No Ability', 'item': p.get('item') or '',
                'evs': p.get('evs') or {}, 'ivs': p.get('ivs') or {},
                'boosts': p.get('boosts') or {}, 'status': STATUS.get(ident(p.get('status')), '')}
        for field, description in [('nature', 'neutral nature'), ('evs', 'zero EVs'), ('ivs', '31 IVs'), ('ability', 'no active ability modifier'), ('item', 'no held-item modifier')]:
            if field not in p or p[field] is None:
                assumptions.append(f'{raw["name"]}: unknown {field}; assume {description}')
        overrides = {}
        if p.get('types'):
            overrides['types'] = [str(t).capitalize() for t in p['types']]
        if p.get('base_stats'):
            overrides['baseStats'] = p['base_stats']
        if overrides:
            opts['overrides'] = overrides
        # No hypothetical Tera: the event has no Tera action.
        return {'name': raw['name'], 'options': opts, 'hp_fraction': hp_fraction(p)}, assumptions

    def _own(self, p, obs):
        if p.get('_calc_side') in {'own', 'opponent'}:
            return p['_calc_side'] == 'own'
        key = ident(p.get('species'))
        team = obs.get('team') or {}
        own = list(team.values()) if isinstance(team, dict) else list(team)
        own.extend(obs.get('active_pokemon') or [])
        foes = obs.get('opponent_active_pokemon') or []
        if any(x and ident(x.get('species')) == key for x in own if isinstance(x, dict)):
            return True
        if any(x and ident(x.get('species')) == key for x in foes if isinstance(x, dict)):
            return False
        return None

    def _field(self, a, d, obs, spread):
        field = {'gameType': 'Doubles'}
        weather = WEATHER.get(ident(obs.get('weather')))
        if weather:
            field['weather'] = weather
        for name in ('electric', 'grassy', 'misty', 'psychic'):
            if present(obs.get('fields'), name + 'terrain'):
                field['terrain'] = name.capitalize()
        for source, target in [('magicroom', 'isMagicRoom'), ('wonderroom', 'isWonderRoom'), ('gravity', 'isGravity')]:
            field[target] = present(obs.get('fields'), source)
        flags = {'reflect': 'isReflect', 'lightscreen': 'isLightScreen', 'auroraveil': 'isAuroraVeil', 'tailwind': 'isTailwind'}
        for p, side in [(a, 'attackerSide'), (d, 'defenderSide')]:
            own = self._own(p, obs)
            conditions = obs.get('side_conditions' if own else 'opponent_side_conditions', {}) if own is not None else {}
            field[side] = {flag: present(conditions, condition) for condition, flag in flags.items()}
        if self._own(a, obs) is not None and self._own(a, obs) == self._own(d, obs):
            # Reflect/screens do not reduce attacks from an ally.
            for flag in ('isReflect', 'isLightScreen', 'isAuroraVeil'):
                field['defenderSide'][flag] = False
        # Include only modifiers supported by observed active abilities.
        all_active = [*(obs.get('active_pokemon') or []), *(obs.get('opponent_active_pokemon') or [])]
        abilities = {ident(p.get('ability')) for p in all_active if isinstance(p, dict)}
        for ability, flag in [('beadsofruin', 'isBeadsOfRuin'), ('swordofruin', 'isSwordOfRuin'), ('tabletsofruin', 'isTabletsOfRuin'), ('vesselofruin', 'isVesselOfRuin')]:
            field[flag] = ability in abilities
        return field

    def _accuracy(self, move, a, d, obs):
        raw = move.get('accuracy', 100)
        if raw is True or raw is None:
            chance = 1.
        else:
            chance = float(raw) / 100 if raw > 1 else float(raw)
        weather = WEATHER.get(ident(obs.get('weather')))
        key = ident(move.get('move_id'))
        if key in {'thunder', 'hurricane'} and weather in {'Rain', 'Heavy Rain'} or key == 'blizzard' and weather in {'Hail', 'Snow'}:
            chance = 1.
        if key in {'thunder', 'hurricane'} and weather in {'Rain', 'Heavy Rain'} or key == 'blizzard' and weather in {'Hail', 'Snow'}:
            return 1.
        if ident(a.get('ability')) == 'noguard' or ident(d.get('ability')) == 'noguard':
            return 1.
        if raw is not True:
            stage = max(-6, min(6, a.get('boosts', {}).get('accuracy', 0) - d.get('boosts', {}).get('evasion', 0)))
            chance *= (3 + max(0, stage)) / (3 - min(0, stage))
        return max(0., min(1., chance))

    def predict(self, a, d, move, obs=None, spread=False):
        obs = obs or {}
        m = self.dex.move(move)
        key = ident(m.get('move_id'))
        raw_move = self.dex.moves.get(key)
        if not raw_move:
            raise ValueError('Move missing from fixed reference data')
        attacker, a_assumptions = self._pokemon(a)
        defender, d_assumptions = self._pokemon(d)
        assumptions = a_assumptions + d_assumptions
        options = {'ability': attacker['options']['ability'], 'item': attacker['options']['item']}
        if not spread and raw_move.get('target') in {'allAdjacent', 'allAdjacentFoes'}:
            options['overrides'] = {'target': 'normal'}
        if key in {'surgingstrikes', 'wickedblow', 'flowertrick'}:
            options['isCrit'] = True
        if isinstance(raw_move.get('multihit'), list) and options['ability'] != 'Skill Link':
            assumptions.append(f'{raw_move["name"]}: calculator default hit count; variable hits not fully modeled')
        if raw_move.get('multiaccuracy'):
            assumptions.append(f'{raw_move["name"]}: full hit sequence assumed before overall accuracy adjustment')
        packet = {'attacker': attacker, 'defender': defender,
                  'move': {'name': raw_move['name'], 'options': options},
                  'field': self._field(a, d, obs, spread)}
        encoded = json.dumps(packet, sort_keys=True, separators=(',', ':'))
        hit_chance = self._accuracy(m, a, d, obs)
        if encoded in self.cache:
            self.cache_hits += 1
            result = self.cache[encoded]
            self.cache.move_to_end(encoded)
        else:
            result = json.loads(self.calculate_js(encoded))
            self.calls += 1
            self.cache[encoded] = result
            if len(self.cache) > 4096:
                self.cache.popitem(last=False)
        max_hp = result['defender_max_hp']
        distribution = tuple((float(amount) / max_hp, float(prob)) for amount, prob in result['distribution'])
        ignores_ability = ident(a.get('ability')) in {'moldbreaker', 'teravolt', 'turboblaze'} or key in {'sunsteelstrike', 'moongeistbeam'}
        sturdy = ident(d.get('ability')) == 'sturdy' and (not ignores_ability or ident(d.get('item')) == 'abilityshield')
        sash = ident(d.get('item')) == 'focussash'
        if result.get('hits', 1) == 1 and hp_fraction(d) >= 1 - 1e-9 and (sash or sturdy):
            # Formula damage is not enough for KO scoring: one hit cannot break
            # full-health Sash/Sturdy. Two positive hits can still combine for a KO.
            floor = (max_hp - 1) / max_hp
            capped = {}
            for damage, prob in distribution:
                damage = min(damage, floor)
                capped[damage] = capped.get(damage, 0.) + prob
            distribution = tuple(sorted(capped.items()))
            assumptions.append('Full-health Focus Sash/Sturdy survival applied to single-hit damage')
        if not distribution or any(not math.isfinite(x) for row in distribution for x in row):
            raise ValueError('Calculator returned non-finite damage')
        return DamagePrediction(distribution=distribution, min_damage=distribution[0][0], max_damage=distribution[-1][0],
                                expected_fraction=sum(dmg * prob for dmg, prob in distribution) * hit_chance,
                                hit_chance=hit_chance, assumptions=tuple(assumptions))
