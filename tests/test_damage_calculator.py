"""Regressions for actual Showdown calculator use, not just an approximate fallback."""
from itertools import product
import math
import pytest
from agent.strategy.calculator import DamageCalculator, DamagePrediction, BACKEND, joint_damage_metrics
from agent.strategy.dex import Dex
from agent.strategy.policy import Policy
from agent.strategy.offline import battle_state, pokemon


@pytest.fixture(scope='module')
def calc():
    return DamageCalculator(Dex())


def test_known_neutral_gen9_damage_rolls(calc):
    p = calc.predict({'species': 'Mew', 'level': 50}, {'species': 'Mew', 'level': 50}, 'Tackle')
    assert p.backend == BACKEND
    # Lv50 neutral Mew: 120 Atk, 120 Def, 175 HP. Tackle's 16 rolls are 16..19 HP.
    assert p.min_damage * 175 == pytest.approx(16)
    assert p.max_damage * 175 == pytest.approx(19)
    assert sum(prob for _, prob in p.distribution) == pytest.approx(1)
    assert len(p.distribution) == 4


def test_damage_range_is_conditional_on_hit_and_ko_uses_accuracy(calc):
    defender = {'species': 'Magikarp', 'level': 50, 'current_hp_fraction': .01}
    p = calc.predict({'species': 'Mew', 'level': 50}, defender, 'Hydro Pump')
    assert p.hit_chance == .8
    assert p.ko_chance(.01) == pytest.approx(.8)
    assert p.expected_fraction == pytest.approx(sum(d * prob for d, prob in p.distribution) * .8)


def test_immune_target_has_zero_damage(calc):
    p = calc.predict({'species': 'Mew', 'level': 50}, {'species': 'Gengar', 'level': 50}, 'Tackle')
    assert p.min_damage == p.max_damage == p.expected_fraction == 0
    assert p.ko_chance(.1) == 0


def test_rain_and_burn_change_damage(calc):
    a, d = {'species': 'Mew', 'level': 50}, {'species': 'Snorlax', 'level': 50}
    water = calc.predict(a, d, 'Surf')
    rain = calc.predict(a, d, 'Surf', {'weather': 'rain'})
    assert rain.expected_fraction > water.expected_fraction * 1.4
    tackle = calc.predict(a, d, 'Tackle')
    burned = calc.predict({**a, 'status': 'brn'}, d, 'Tackle')
    assert burned.expected_fraction < tackle.expected_fraction * .6


def test_spread_and_screens_are_independent(calc):
    a, d = {'species': 'Mew', 'level': 50, '_calc_side': 'own'}, {'species': 'Snorlax', 'level': 50, '_calc_side': 'opponent'}
    normal = calc.predict(a, d, 'Surf', spread=False)
    spread = calc.predict(a, d, 'Surf', spread=True)
    screen = calc.predict(a, d, 'Surf', {'opponent_side_conditions': {'lightscreen': 4}}, spread=False)
    assert .65 < spread.expected_fraction / normal.expected_fraction < .85
    # Single-target doubles attacks use the doubles screen reduction, about 2/3.
    assert .58 < screen.expected_fraction / normal.expected_fraction < .75


def test_screens_do_not_reduce_friendly_fire(calc):
    a, d = {'species': 'Mew', 'level': 50, '_calc_side': 'own'}, {'species': 'Snorlax', 'level': 50, '_calc_side': 'own'}
    normal = calc.predict(a, d, 'Surf', spread=True)
    screened = calc.predict(a, d, 'Surf', {'side_conditions': {'lightscreen': 4}}, spread=True)
    assert screened.expected_fraction == normal.expected_fraction


def test_three_hit_surging_strikes_distribution(calc):
    p = calc.predict({'species': 'Urshifu-Rapid-Strike', 'level': 50}, {'species': 'Mew', 'level': 50}, 'Surging Strikes')
    assert sum(prob for _, prob in p.distribution) == pytest.approx(1)
    assert len(p.distribution) > 4  # Convolved multi-hit rolls, not one-hit damage.
    assert p.min_damage > .3
    assert p.backend == BACKEND


def test_unknown_details_are_disclosed_not_silently_inferred(calc):
    p = calc.predict({'species': 'Mew'}, {'species': 'Snorlax'}, 'Tackle')
    assert any('unknown ability' in assumption for assumption in p.assumptions)
    assert any('unknown evs' in assumption for assumption in p.assumptions)


def test_cache_separates_weather(calc):
    a, d = {'species': 'Mew', 'level': 50}, {'species': 'Blissey', 'level': 50}
    calc.predict(a, d, 'Flamethrower', {'weather': 'rain'})
    calls, hits = calc.calls, calc.cache_hits
    calc.predict(a, d, 'Flamethrower', {'weather': 'rain'})
    assert calc.calls == calls and calc.cache_hits == hits + 1
    calc.predict(a, d, 'Flamethrower', {'weather': 'sun'})
    assert calc.calls == calls + 1


@pytest.mark.parametrize('hp', [.1, .4, .8, 1.])
def test_joint_metrics_match_exhaustive_probability(hp):
    a, b = [(0., .2), (.3, .4), (.7, .4)], [(0., .1), (.2, .4), (.5, .5)]
    expected, ko = joint_damage_metrics(a, b, hp)
    slow_expected = sum(min(da + db, hp) * pa * pb for (da, pa), (db, pb) in product(a, b))
    slow_ko = sum(pa * pb for (da, pa), (db, pb) in product(a, b) if da + db + 1e-9 >= hp)
    assert expected == pytest.approx(slow_expected)
    assert ko == pytest.approx(slow_ko)


def test_possible_ko_is_not_guaranteed():
    p = DamagePrediction(((.8, .5), (1.2, .5)), .8, 1.2, 1., 1.)
    assert p.ko_chance(1) == .5
    _, ko = joint_damage_metrics(p.outcomes(), [(0., 1.)], 1)
    assert ko == .5


def test_real_policy_forecasts_use_official_engine():
    p = Policy()
    p.decide(battle_state())
    forecasts = list(p.damage_forecasts.values())
    assert any(f['move_id'] == 'moonblast' and f['backend'] == BACKEND for f in forecasts)
    assert all(f['backend'] == BACKEND for f in forecasts)


def test_unsupported_input_fallback_is_explicitly_labeled():
    p = Policy()
    result = p.predict_damage(pokemon('Mew'), pokemon('Snorlax'), {'base_power': 80, 'category': 'Special', 'type': 'Psychic'})
    assert result.backend == 'approximation'
    assert result.assumptions


@pytest.mark.parametrize('survival', [{'item': 'Focus Sash'}, {'ability': 'Sturdy'}])
def test_full_health_survival_prevents_one_hit_ko_but_not_focus_fire(calc, survival):
    a = {'species': 'Mew', 'level': 50}
    d = {'species': 'Magikarp', 'level': 50, **survival}
    p = calc.predict(a, d, 'Thunderbolt')
    assert p.ko_chance(1) == 0
    assert p.max_damage < 1
    _, ko = joint_damage_metrics(p.outcomes(), p.outcomes(), 1)
    assert ko == pytest.approx(1)
    damaged = calc.predict(a, {**d, 'current_hp_fraction': .9}, 'Thunderbolt')
    assert damaged.ko_chance(.9) == pytest.approx(1)


def test_all_adjacent_spread_counts_the_ally_with_one_foe_left():
    p = Policy()
    raw = battle_state(active=[pokemon('Garchomp'), pokemon('Mew')],
        foes=[pokemon('Incineroar'), None], options=[
            [{'type': 'move', 'move_id': 'earthquake', 'targets': [0]}],
            [{'type': 'move', 'move_id': 'protect', 'targets': [0]}]])
    p.decide(raw)
    quake = [f for f in p.damage_forecasts.values() if f['attacker'] == 'Garchomp' and f['defender'] == 'Incineroar' and f['move_id'] == 'earthquake']
    assert quake and quake[0]['spread'] is True
