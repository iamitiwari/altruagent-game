"""Deterministic draft/preview policy and bounded joint-action search for VGC."""
from itertools import combinations, product
from dataclasses import replace
from .dex import Dex, ident, effectiveness
from .calculator import DamageCalculator, DamagePrediction, joint_damage_metrics

# Frozen priors supplement general stat/coverage scoring; fixed sets still matter.
TIERS = {
    'calyrexshadow': 48, 'calyrexice': 44, 'miraidon': 46, 'koraidon': 45,
    'kyogre': 40, 'groudon': 38, 'zacian': 36, 'zaciancrowned': 44,
    'lunala': 36, 'zamazentacrowned': 40, 'terapagos': 36,
    'incineroar': 27, 'rillaboom': 25, 'fluttermane': 30,
    'urshifurapidstrike': 29, 'urshifu': 27, 'tornadus': 25,
    'amoonguss': 23, 'whimsicott': 23, 'landorustherian': 22,
    'gholdengo': 24, 'ironhands': 23, 'chienpao': 24, 'ogerponwellspring': 27,
    'ogerponhearthflame': 25, 'farigiraf': 21, 'indeedeef': 20,
}
PROTECT = {'protect', 'detect', 'spikyshield', 'banefulbunker', 'silktrap', 'burningbulwark'}
REDIRECT = {'followme', 'ragepowder'}
WEIGHTS = {'damage': 42., 'knockout': 24., 'chip': 5., 'switch_cost': 13., 'friendly_fire': 65., 'setup_cover': 8.}
BOOST = {'swordsdance', 'nastyplot', 'calmmind', 'bulkup', 'dragondance', 'quiverdance'}


def hp(p):
    if not p or p.get('fainted'):
        return 0.0
    if p.get('current_hp_fraction') is not None:
        return max(0, min(1, float(p['current_hp_fraction'])))
    if p.get('current_hp') is not None and p.get('max_hp'):
        return max(0, min(1, p['current_hp'] / p['max_hp']))
    return 1.0


def alive(p):
    return bool(p) and hp(p) > 0


def condition(container, name):
    if isinstance(container, dict):
        return any(ident(k) == name and bool(v) for k, v in container.items())
    return any(ident(x) == name for x in (container or []))


class Policy:
    def __init__(self, agent_id='', dex=None):
        self.agent_id = agent_id
        self.dex = dex or Dex()
        self.cards = {}                    # only this match's public draft sets
        self.seen_actives = {}             # (slot, species) -> first observed turn
        self.active_keys = [None, None]
        self.last_moves = {}              # updated only on accepted actions
        self.last_reason = ''
        self.calculator = DamageCalculator(self.dex)
        self.damage_forecasts = {}
        self._scored_predictions = {}

    def pokemon(self, p):
        p = self.dex.pokemon(p)
        card = self.cards.get(ident(p.get('species')), {})
        # Public draft fields remain useful when observations omit details.
        merged = {**card, **{k: v for k, v in p.items() if v is not None or k == 'status'}}
        return self.dex.pokemon(merged)

    def moves(self, p):
        return [self.dex.move(m) for m in p.get('moves', [])]

    def stat(self, p, stat):
        base = p.get('base_stats', {}).get(stat, 80)
        level = p.get('level', 50)
        iv = p.get('ivs', {}).get(stat, 31)
        ev = p.get('evs', {}).get(stat, 0)
        value = (2 * base + iv + ev / 4) * level / 100 + (level + 10 if stat == 'hp' else 5)
        natures = {'adamant': ('atk', 'spa'), 'jolly': ('spe', 'spa'), 'modest': ('spa', 'atk'),
                   'timid': ('spe', 'atk'), 'brave': ('atk', 'spe'), 'quiet': ('spa', 'spe'),
                   'bold': ('def', 'atk'), 'calm': ('spd', 'atk'), 'sassy': ('spd', 'spe'),
                   'relaxed': ('def', 'spe'), 'impish': ('def', 'spa'), 'careful': ('spd', 'spa')}
        up, down = natures.get(ident(p.get('nature')), ('', ''))
        value *= 1.1 if stat == up else 0.9 if stat == down else 1
        boost = p.get('boosts', {}).get(stat, 0)
        if stat != 'hp':
            value *= (2 + max(0, boost)) / (2 - min(0, boost))
        return value

    def speed(self, p, obs, own):
        speed = self.stat(p, 'spe')
        if ident(p.get('item')) == 'choicescarf':
            speed *= 1.5
        if ident(p.get('status')) in {'par', 'paralysis'}:
            speed *= 0.5
        side = obs.get('side_conditions' if own else 'opponent_side_conditions', {})
        if condition(side, 'tailwind'):
            speed *= 2
        weather = ident(obs.get('weather'))
        a = ident(p.get('ability'))
        if ((a == 'swiftswim' and 'rain' in weather) or
            (a == 'chlorophyll' and ('sun' in weather or 'sunny' in weather)) or
            (a == 'sandrush' and 'sand' in weather) or (a == 'slushrush' and 'snow' in weather)):
            speed *= 2
        return speed

    def grounded(self, p):
        return 'flying' not in {ident(t) for t in p.get('types', [])} and ident(p.get('ability')) != 'levitate'

    def predict_damage(self, attacker, defender, move, obs=None, spread=False):
        if not alive(attacker) or not alive(defender):
            return DamagePrediction(((0., 1.),), 0., 0., 0., 1.)
        m = self.dex.move(move)
        try:
            prediction = self.calculator.predict(attacker, defender, m, obs, spread)
        except Exception as exc:
            # A missing move/species or bounded engine error must not stall a turn.
            amount = self._approximate_damage(attacker, defender, m, obs, spread)
            prediction = DamagePrediction(((amount, 1.),), amount, amount, amount, 1.,
                (f'Calculator unavailable for this input: {type(exc).__name__}; use approximate damage',),
                backend='approximation')
        key = (ident(attacker.get('species')), ident(defender.get('species')), ident(m.get('move_id')), bool(spread))
        self.damage_forecasts[key] = {'attacker': attacker.get('species'), 'defender': defender.get('species'),
            'move_id': m.get('move_id'), 'spread': bool(spread), **prediction.public(),
            'ko_chance': prediction.ko_chance(hp(defender))}
        return prediction

    def damage(self, attacker, defender, move, obs=None, spread=False):
        return self.predict_damage(attacker, defender, move, obs, spread).expected_fraction

    def _approximate_damage(self, attacker, defender, move, obs=None, spread=False):
        """Approximate expected damage as a fraction of target max HP, not a simulator."""
        if not alive(attacker) or not alive(defender):
            return 0.0
        obs = obs or {}
        m = self.dex.move(move)
        mid = ident(m.get('move_id'))
        category = ident(m.get('category'))
        power = m.get('base_power', m.get('basePower', 0)) or 0
        if category == 'status':
            return 0.0
        if mid in {'seismictoss', 'nightshade'}:
            return attacker.get('level', 50) / self.stat(defender, 'hp') if effectiveness(m.get('move_type', m['type']), defender.get('types')) else 0
        if mid == 'superfang':
            return hp(defender) * 0.5
        power = {'surgingstrikes': 75, 'populationbomb': 150, 'tripleaxel': 105,
                 'lowkick': 100, 'grassknot': 100, 'electroball': 80, 'gyroball': 80,
                 'bodypress': 80, 'foulplay': 95, 'lastrespects': 50}.get(mid, power)
        if mid in {'eruption', 'waterspout', 'dragonenergy'}:
            power *= hp(attacker)
        if not power:
            return 0.0
        typ = ident(m.get('move_type') or m.get('type'))
        a, d = ident(attacker.get('ability')), ident(defender.get('ability'))
        item = ident(attacker.get('item'))
        eff = effectiveness(typ, defender.get('types', []))
        ignores = a in {'moldbreaker', 'teravolt', 'turboblaze'} or mid in {'sunsteelstrike', 'moongeistbeam'}
        if typ == 'ground' and d == 'levitate' and not ignores:
            return 0.0
        if not ignores and ((typ == 'water' and d in {'waterabsorb', 'stormdrain', 'dryskin'}) or
                (typ == 'electric' and d in {'voltabsorb', 'lightningrod', 'motordrive'}) or
                (typ == 'fire' and d in {'flashfire', 'wellbakedbody'}) or
                (typ == 'grass' and d == 'sapsipper')):
            return 0.0
        if d == 'wonderguard' and eff <= 1 and not ignores:
            return 0.0
        if mid == 'fakeout' and (d == 'innerfocus' or 'ghost' in {ident(t) for t in defender.get('types', [])}):
            return 0.0
        if m.get('priority', 0) > 0 and self.grounded(defender) and condition(obs.get('fields'), 'psychicterrain'):
            return 0.0
        attack_stat, defense_stat = ('atk', 'def') if category == 'physical' else ('spa', 'spd')
        attack = self.stat(attacker, 'def' if mid == 'bodypress' else attack_stat)
        if mid == 'foulplay':
            attack = self.stat(defender, 'atk')
        defense = self.stat(defender, defense_stat)
        mult = eff * (1.5 if typ in {ident(t) for t in attacker.get('types', [])} else 1)
        mult *= 0.75 if spread else 1
        if category == 'physical' and ident(attacker.get('status')) in {'brn', 'burn'} and a != 'guts' and mid != 'facade':
            mult *= 0.5
        if a in {'hugepower', 'purepower'} and category == 'physical':
            mult *= 2
        if a == 'technician' and power <= 60:
            mult *= 1.5
        if a == 'adaptability' and typ in {ident(t) for t in attacker.get('types', [])}:
            mult *= 4 / 3
        if a == 'waterbubble' and typ == 'water':
            mult *= 2
        if a == 'guts' and attacker.get('status') and category == 'physical':
            mult *= 1.5
        if item == 'choiceband' and category == 'physical' or item == 'choicespecs' and category == 'special':
            mult *= 1.5
        if item == 'lifeorb':
            mult *= 1.3
        if ident(defender.get('item')) == 'assaultvest' and category == 'special':
            mult /= 1.5
        if d == 'multiscale' and hp(defender) >= .99:
            mult *= .5
        if d == 'thickfat' and typ in {'fire', 'ice'}:
            mult *= .5
        weather = ident(obs.get('weather'))
        if 'rain' in weather:
            mult *= 1.5 if typ == 'water' else .5 if typ == 'fire' else 1
        if 'sun' in weather:
            mult *= 1.5 if typ == 'fire' else .5 if typ == 'water' else 1
        if mid == 'grassyglide' and condition(obs.get('fields'), 'grassyterrain'):
            mult *= 1.3
        accuracy = m.get('accuracy', 100)
        if accuracy is True or accuracy is None:
            accuracy = 1.0
        elif accuracy > 1:
            accuracy /= 100
        if mid in {'thunder', 'hurricane'} and 'rain' in weather:
            accuracy = 1
        if mid == 'blizzard' and ('snow' in weather or 'hail' in weather):
            accuracy = 1
        damage = ((2 * attacker.get('level', 50) / 5 + 2) * power * attack / defense / 50 + 2) * .925 * mult
        if mid in {'surgingstrikes', 'wickedblow', 'flowertrick'}:
            damage *= 1.5
        return max(0, damage / (defender.get('max_hp') or self.stat(defender, 'hp')) * accuracy)

    def quality(self, p):
        p = self.pokemon(p)
        stats = p.get('base_stats', {})
        bst = sum(stats.values()) if stats else 480
        moveids = {ident(m.get('move_id')) for m in self.moves(p)}
        support = 7 * bool(moveids & {'tailwind', 'trickroom'}) + 6 * ('fakeout' in moveids)
        support += 5 * bool(moveids & REDIRECT) + 6 * ('spore' in moveids)
        support += 6 * (ident(p.get('ability')) == 'intimidate')
        return bst / 20 + TIERS.get(ident(p.get('species')), 0) + support

    def matchup(self, p, foes):
        p = self.pokemon(p)
        attack = self.moves(p)
        score = 0.0
        for foe in foes:
            foe = self.pokemon(foe)
            best = max((self.damage(p, foe, m) for m in attack), default=.2)
            incoming = max((self.damage(foe, p, m) for m in self.moves(foe)), default=.25)
            score += min(1.2, best) * 15 - min(1.2, incoming) * 10
        return score / max(1, len(foes))

    def synergy(self, team):
        team = [self.pokemon(p) for p in team]
        score = 0.0
        ms = [{ident(m.get('move_id')) for m in self.moves(p)} for p in team]
        all_moves = set().union(*ms) if ms else set()
        score += 9 * bool(all_moves & {'tailwind', 'icywind', 'electroweb'})
        score += 7 * bool(all_moves & {'fakeout'})
        score += 6 * bool(all_moves & REDIRECT)
        score += 5 * any(ident(p.get('ability')) == 'intimidate' for p in team)
        if 'trickroom' in all_moves:
            slow = sum(p.get('base_stats', {}).get('spe', 80) <= 60 for p in team)
            score += 4 * slow - 3 * sum(p.get('base_stats', {}).get('spe', 80) >= 110 for p in team)
        types = {ident(t) for p in team for t in p.get('types', [])}
        score += len(types) * 1.5
        physical = sum(any(ident(m.get('category')) == 'physical' for m in self.moves(p)) for p in team)
        special = sum(any(ident(m.get('category')) == 'special' for m in self.moves(p)) for p in team)
        score += 4 * bool(physical and special)
        abilities = {ident(p.get('ability')) for p in team}
        if abilities & {'drizzle', 'primordialsea'}:
            score += 7 * sum(ident(p.get('ability')) == 'swiftswim' for p in team)
            score += 2 * sum('water' in {ident(t) for t in p.get('types', [])} for p in team)
        if abilities & {'drought', 'orichalcumpulse', 'desolateland'}:
            score += 7 * sum(ident(p.get('ability')) in {'chlorophyll', 'protosynthesis'} for p in team)
        for typ in ['water', 'fire', 'ground', 'ice', 'fairy', 'fighting', 'electric']:
            weak = sum(effectiveness(typ, p.get('types', [])) > 1 for p in team)
            score -= max(0, weak - 2) * 4
        return score

    def draft(self, state):
        obs = state['observation']
        for card in obs.get('available_cards', []):
            self.cards[ident(card['species'])] = card
        rosters = obs.get('rosters', {})
        for roster in rosters.values():
            for card in roster:
                key = ident(card.get('species'))
                self.cards[key] = {**self.cards.get(key, {}), **card}
        owner = self.agent_id or obs.get('current_seat')
        mine = rosters.get(owner, [])
        foes = [p for k, roster in rosters.items() if k != owner for p in roster]
        legal = state['legal_actions']['actions']
        available = {c['card_id']: c for c in obs.get('available_cards', [])}
        choices = []
        for entry in legal:
            action = entry.get('input', {}).get('action', {})
            card_id = action.get('card_id') or entry['action_id'].removeprefix('draft_pick:')
            p = available.get(card_id, {'species': card_id.removeprefix('vgc-')})
            score = self.quality(p) + self.matchup(p, foes)
            score += self.synergy([*mine, p]) - self.synergy(mine)
            # Small denial bonus, using only the public opposing draft.
            score += .15 * (self.synergy([*foes, p]) - self.synergy(foes))
            choices.append((score, card_id))
        if not choices:
            raise ValueError('No legal draft pick')
        score, card_id = max(choices)
        self.last_reason = f'Public draft: team strength, coverage, support; score {score:.1f}.'
        return {'type': 'draft_pick', 'card_id': card_id}

    def preview(self, state):
        obs = state['observation']
        template = state['legal_actions']['actions'][0]['input']['action']
        legal_roster = list(dict.fromkeys(template['roster']))
        details = {ident(p.get('species') or p.get('name')): p for p in obs.get('your_roster', [])}
        roster = [self.pokemon({**details.get(ident(species), {}), 'species': species}) for species in legal_roster]
        if not roster:
            template = state['legal_actions']['actions'][0]['input']['action']
            roster = [self.pokemon(s) for s in template['roster']]
        foes = [self.pokemon(p) for p in obs.get('opponent_roster', [])]
        if len(roster) < 4:
            raise ValueError('Preview requires at least four Pokémon')
        best = max(combinations(roster, 4), key=lambda team: sum(self.quality(p) + self.matchup(p, foes) for p in team) + self.synergy(team))
        def lead_score(pair):
            score = sum(self.matchup(p, foes) for p in pair) + self.synergy(pair)
            for p in pair:
                ids = {ident(m.get('move_id')) for m in self.moves(p)}
                score += 7 * bool(ids & {'fakeout', 'tailwind', 'trickroom'})
                score += 5 * (ident(p.get('ability')) == 'intimidate')
            return score
        leads = max(combinations(best, 2), key=lead_score)
        self.last_reason = 'Choose four for opposing public roster; lead support and complementary damage.'
        return {'type': 'select_lineup', 'bring': [p['species'] for p in best], 'leads': [p['species'] for p in leads]}

    def candidates(self, template):
        choices = []
        for opt in template.get('options', []):
            if opt['type'] == 'move':
                for target in opt.get('targets') or [0]:
                    choice = {'type': 'move', 'move_id': opt['move_id']}
                    if opt.get('targets'):
                        choice['target'] = target
                    choices.append((choice, opt))
            elif opt['type'] == 'switch':
                choices.append(({'type': 'switch', 'species': opt['species']}, opt))
            elif opt['type'] == 'pass':
                choices.append(({'type': 'pass'}, opt))
        return choices

    @staticmethod
    def valid_pair(a, b):
        if a['type'] == b['type'] == 'pass':
            return False
        return not (a['type'] == b['type'] == 'switch' and ident(a['species']) == ident(b['species']))

    def threat(self, p, foes, obs):
        value = 0
        for foe in foes:
            if not alive(foe):
                continue
            known = self.moves(foe)
            if known:
                value += max((self.damage(foe, p, m, obs) for m in known), default=0)
            else:
                # A generic STAB threat is an estimate from public typing, not hidden moves.
                value += max((self.damage(foe, p, {'base_power': 80, 'category': 'Physical' if self.stat(foe, 'atk') > self.stat(foe, 'spa') else 'Special', 'type': t}, obs) for t in foe.get('types', [])), default=.2)
        return value

    def score_choice(self, slot, choice, option, active, foes, team, obs):
        p = active[slot]
        kind = choice['type']
        dealt = [0., 0.]
        if kind == 'pass':
            return -2., dealt, 0., 0.
        if kind == 'switch':
            reserve = next((x for x in team if ident(x.get('species')) == ident(choice['species'])), self.pokemon(choice['species']))
            if not alive(p):
                return self.quality(reserve) * .15 + self.matchup(reserve, foes), dealt, 0., 0.
            before, after = self.threat(p, foes, obs), self.threat(reserve, foes, obs)
            score = 30 * (min(before, hp(p)) - min(after, hp(reserve))) - WEIGHTS['switch_cost']
            score += .3 * (self.matchup(reserve, foes) - self.matchup(p, foes))
            score += 5 * (ident(reserve.get('ability')) == 'intimidate')
            return score, dealt, 0., 0.
        m = self.dex.move(option)
        if m.get('current_pp') is not None and m['current_pp'] <= 0:
            return -100, dealt, 0., 0.
        # Some templates contain only move_id/targets; merge full available_moves.
        mid = ident(choice['move_id'])
        move_lists = list(obs.get('available_moves') or [[], []])
        slot_moves = (move_lists + [[], []])[slot] or []
        for candidate in slot_moves:
            if ident(candidate.get('id') or candidate.get('move_id')) == mid:
                m = self.dex.move({**m, **candidate})
                break
        incoming = self.threat(p, foes, obs)
        first = self.seen_actives.get((slot, ident(p.get('species')))) == obs.get('turn', 1)
        previous = self.last_moves.get(slot, {})
        last_protect = (previous.get('species') == ident(p.get('species')) and
                        previous.get('turn') == obs.get('turn', 1) - 1 and previous.get('move') in PROTECT)
        if mid in PROTECT:
            if last_protect:
                return -15, dealt, 0., 0.
            # Protection is useful when threatened; low HP alone is insufficient.
            score = min(incoming, hp(p)) * 28 - 9 + (8 if incoming >= hp(p) and incoming > .2 else 0)
            return score, dealt, 0., 1.
        if mid == 'fakeout' and not first:
            return -100, dealt, 0., 0.
        if mid == 'pollenpuff' and choice.get('target', 0) in (-1, -2):
            ally = active[-choice.get('target', 0) - 1]
            return min(.5, 1 - hp(ally)) * 45 - 4, dealt, 0., 0.
        if ident(m.get('category')) == 'status':
            score, support = self.support_score(mid, p, foes, choice.get('target', 0), active, slot, obs, incoming)
            return score, dealt, support, 0.
        target = choice.get('target', 0)
        spread = target == 0 and ident(m.get('target')) in {'alladjacent', 'alladjacentfoes', 'all'}
        indexes = [target - 1] if target in (1, 2) else [i for i, f in enumerate(foes) if alive(f)] if spread else []
        score = 0.
        for i in indexes:
            if i >= len(foes) or not alive(foes[i]):
                continue
            affected = sum(alive(f) for f in foes)
            if ident(m.get('target')) == 'alladjacent' and alive(active[1 - slot]):
                affected += 1
            prediction = self.predict_damage(p, foes[i], m, obs, spread and affected > 1)
            self._scored_predictions[(slot, mid, target, i)] = prediction
            dealt[i] = prediction.expected_fraction
            priority = m.get('priority', 0)
            if mid == 'grassyglide' and condition(obs.get('fields'), 'grassyterrain'):
                priority = 1
            speed_first = self.speed(p, obs, True) > self.speed(foes[i], obs, False)
            if condition(obs.get('fields'), 'trickroom'):
                speed_first = not speed_first
            if priority > 0 or speed_first:
                score += 3 * min(1, dealt[i])
            if mid == 'fakeout' and dealt[i] > 0 and ident(foes[i].get('ability')) not in {'innerfocus', 'shielddust'} and ident(foes[i].get('item')) != 'covertcloak':
                score += 17
        if target in (-1, -2):
            ally = active[-target - 1]
            score -= WEIGHTS['friendly_fire'] * self.damage(p, ally, m, obs)
        if spread and ident(m.get('target')) == 'alladjacent':
            ally = active[1 - slot]
            score -= WEIGHTS['friendly_fire'] * self.damage(p, ally, m, obs, True)
        if mid in {'flareblitz', 'bravebird', 'woodhammer', 'doubleedge', 'headsmash'}:
            score -= sum(dealt) * (9 if mid != 'headsmash' else 16)
        if mid in {'closecombat', 'headlongrush'}:
            score -= 2
        if mid in {'uturn', 'voltswitch', 'flipturn'}:
            score += 2
        if mid in {'icywind', 'electroweb'} and not condition(obs.get('fields'), 'trickroom'):
            score += 8 * sum(d > 0 for d in dealt)
        if mid == 'snarl':
            score += 5 * sum(d > 0 and self.stat(foes[i], 'spa') >= self.stat(foes[i], 'atk') for i, d in enumerate(dealt))
        if mid == 'knockoff':
            score += 4 * sum(d > 0 and bool(foes[i].get('item')) for i, d in enumerate(dealt))
        # Discount slow attacks by a fragile Pokémon under severe pressure.
        if incoming >= hp(p) and all(self.speed(p, obs, True) < self.speed(f, obs, False) for f in foes if alive(f)) and m.get('priority', 0) <= 0 and not condition(obs.get('fields'), 'trickroom'):
            dealt = [d * .7 for d in dealt]
            for i in indexes:
                k = (slot, mid, target, i)
                if k in self._scored_predictions:
                    prediction = self._scored_predictions[k]
                    self._scored_predictions[k] = replace(prediction, hit_chance=prediction.hit_chance * .7, expected_fraction=prediction.expected_fraction * .7)
        return score, dealt, 0., 0.

    def support_score(self, mid, p, foes, target, active, slot, obs, incoming):
        if mid == 'tailwind':
            if condition(obs.get('side_conditions'), 'tailwind'):
                return -15, 0
            gain = sum(self.speed(a, obs, True) < self.speed(f, obs, False) < 2 * self.speed(a, obs, True) for a in active if alive(a) for f in foes if alive(f))
            return 10 + 6 * gain - (15 if condition(obs.get('fields'), 'trickroom') else 0), 0
        if mid == 'trickroom':
            slow = sum(self.speed(a, obs, True) < self.speed(f, obs, False) for a in active if alive(a) for f in foes if alive(f))
            return (18 if slow >= 3 else -10) * (-1 if condition(obs.get('fields'), 'trickroom') else 1), 0
        if mid in REDIRECT:
            ally = active[1 - slot]
            return min(self.threat(ally, foes, obs), hp(ally)) * 24 - incoming * 8 - 3, 0
        if mid == 'helpinghand':
            return 1, .5
        if mid in {'wideguard', 'quickguard'}:
            move_ids = [self.dex.move(m) for f in foes for m in f.get('moves', [])]
            useful = any(ident(m.get('target')) in {'alladjacent', 'alladjacentfoes'} for m in move_ids) if mid == 'wideguard' else any(m.get('priority', 0) > 0 for m in move_ids)
            return 15 if useful else -8, 0
        if mid in {'spore', 'sleeppowder', 'thunderwave', 'willowisp', 'taunt', 'encore'}:
            if target not in (1, 2) or target > len(foes) or not alive(foes[target - 1]):
                return -50, 0
            f = foes[target - 1]
            types = {ident(t) for t in f.get('types', [])}
            ability = ident(f.get('ability'))
            if ability == 'goodasgold' or ident(p.get('ability')) == 'prankster' and 'dark' in types:
                return -50, 0
            if mid in {'spore', 'sleeppowder'}:
                if 'grass' in types or ident(f.get('item')) == 'safetygoggles' or ability in {'overcoat', 'insomnia', 'vitalspirit', 'sweetveil', 'purifyingsalt'} or f.get('status') or condition(obs.get('fields'), 'electricterrain') and self.grounded(f):
                    return -50, 0
                return 27, 0
            if mid == 'thunderwave':
                return (-50 if types & {'ground', 'electric'} or f.get('status') else 16), 0
            if mid == 'willowisp':
                return (-50 if 'fire' in types or f.get('status') or ability == 'guts' else 20 if self.stat(f, 'atk') > self.stat(f, 'spa') else 5), 0
            return 8, 0
        if mid in {'recover', 'roost', 'synthesis', 'slackoff', 'moonlight', 'morningsun'}:
            return min(.5, 1 - hp(p)) * 38 - min(incoming, hp(p)) * 12 - 5, 0
        if mid == 'pollenpuff' and target in (-1, -2):
            ally = active[-target - 1]
            return min(.5, 1 - hp(ally)) * 45 - 4, 0
        if mid in BOOST:
            return 12 - incoming * 30 - max(p.get('boosts', {}).values(), default=0) * 3, 0
        if mid == 'partingshot':
            return 16 if target in (1, 2) else -20, 0
        return -8, 0

    def battle(self, state, fallback=False):
        obs = state['observation']
        template = state['legal_actions']['actions'][0]['input']['action']
        slots = sorted(template['slots'], key=lambda s: s['slot'])
        if len(slots) != 2 or [s['slot'] for s in slots] != [0, 1]:
            raise ValueError('Expected exactly two doubles slots')
        choices = [self.candidates(s) for s in slots]
        raw_active = list(obs.get('active_pokemon') or [s.get('active') for s in slots])
        active = [{**self.pokemon(p), '_calc_side': 'own'} if p else {} for p in (raw_active + [None, None])[:2]]
        foes = [{**self.pokemon(p), '_calc_side': 'opponent'} if p else {} for p in (obs.get('opponent_active_pokemon') or [None, None])]
        while len(foes) < 2:
            foes.append({})
        raw_team = obs.get('team') or {}
        team = [{**self.pokemon(p), '_calc_side': 'own'} for p in (raw_team.values() if isinstance(raw_team, dict) else raw_team)]
        for slot, p in enumerate(active):
            key = ident(p.get('species'))
            if key != self.active_keys[slot]:
                # On a mid-battle restart, do not assume Fake Out is available.
                fresh = self.active_keys[slot] is not None or obs.get('turn', 1) == 1
                self.seen_actives[(slot, key)] = obs.get('turn', 1) if fresh else -1
                self.active_keys[slot] = key
        self._scored_predictions = {}
        scored = [[(c, self.score_choice(i, c, o, active, foes, team, obs)) for c, o in cs] for i, cs in enumerate(choices)]
        best = None
        for (a, sa), (b, sb) in product(*scored):
            both_pass_only = all(len(cs) == 1 and cs[0][0]['type'] == 'pass' for cs in choices)
            if not self.valid_pair(a, b) and not both_pass_only:
                continue
            score = sa[0] + sb[0]
            for i, f in enumerate(foes):
                remaining = hp(f)
                pa = self._scored_predictions.get((0, ident(a.get('move_id')), a.get('target', 0), i))
                pb = self._scored_predictions.get((1, ident(b.get('move_id')), b.get('target', 0), i))
                outcomes_a = pa.outcomes(1 + sb[2]) if pa else [(0., 1.)]
                outcomes_b = pb.outcomes(1 + sa[2]) if pb else [(0., 1.)]
                capped_damage, ko_chance = joint_damage_metrics(outcomes_a, outcomes_b, remaining)
                score += WEIGHTS['damage'] * capped_damage + WEIGHTS['knockout'] * ko_chance
                if remaining > 0:
                    score += WEIGHTS['chip'] * capped_damage / remaining
            for slot, c, other, own_score, other_score in [(0, a, b, sa, sb), (1, b, a, sb, sa)]:
                if c['type'] != 'move':
                    continue
                mid = ident(c['move_id'])
                if mid == 'fakeout' and any(own_score[1]) and other['type'] == 'move' and ident(other['move_id']) in {'tailwind', 'trickroom'} | BOOST:
                    score += WEIGHTS['setup_cover']
                move = self.dex.move(c['move_id'])
                if c.get('target', 0) == 0 and ident(move.get('target')) == 'alladjacent':
                    old_hit = self.damage(active[slot], active[1 - slot], move, obs, True)
                    if other_score[3]:
                        score += WEIGHTS['friendly_fire'] * old_hit
                    elif other['type'] == 'switch':
                        reserve = next((p for p in team if ident(p.get('species')) == ident(other['species'])), self.pokemon(other['species']))
                        new_hit = self.damage(active[slot], reserve, move, obs, True)
                        score += WEIGHTS['friendly_fire'] * (old_hit - new_hit)
            # Helping Hand without an ally attacking provides no benefit.
            if sa[2] and not any(sb[1]) or sb[2] and not any(sa[1]):
                score -= 20
            if sa[3] and sb[3]:
                score -= 12
            # Avoid spending both slots on the same status effect or Fake Out.
            if a['type'] == b['type'] == 'move' and a.get('target') == b.get('target') and ident(a['move_id']) == ident(b['move_id']):
                if ident(a['move_id']) in {'fakeout', 'spore', 'sleeppowder', 'thunderwave', 'willowisp', 'taunt', 'tailwind', 'trickroom'}:
                    score -= 30
            if fallback:
                score = 0
            if best is None or score > best[0]:
                best = (score, a, b)
        if best is None:
            raise ValueError('No compatible legal pair of slot choices')
        self.last_reason = f'Joint legal-action search with local Showdown damage rolls and KO probabilities; score {best[0]:.1f}.'
        return {'type': 'doubles_turn', 'slot_0': best[1], 'slot_1': best[2]}

    def decide(self, state, fallback=False):
        self.damage_forecasts = {}
        phase = state['phase']
        if phase == 'draft':
            return self.draft(state)
        if phase == 'team_preview':
            return self.preview(state)
        if phase == 'moving':
            return self.battle(state, fallback)
        raise ValueError(f'Unsupported Pokémon phase: {phase}')

    def accepted(self, state, action):
        if action['type'] != 'doubles_turn':
            return
        for slot in (0, 1):
            c = action[f'slot_{slot}']
            actives = list(state['observation'].get('active_pokemon') or [None, None])
            p = self.dex.pokemon((actives + [None, None])[slot])
            self.last_moves[slot] = {'species': ident(p.get('species')), 'turn': state['observation'].get('turn', 1), 'move': ident(c.get('move_id'))}
