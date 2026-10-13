import copy
import time
from itertools import product
import pytest
from agent.strategy.dex import Dex, effectiveness, ident
from agent.strategy.policy import Policy
from agent.strategy.offline import pokemon, battle_state, sample_states


@pytest.fixture(scope='module')
def dex():
    return Dex()


def policy(dex):
    return Policy('me', dex)


def is_legal(action, state):
    slots = state['legal_actions']['actions'][0]['input']['action']['slots']
    for s in slots:
        choice = action[f"slot_{s['slot']}"]
        assert any(choice['type'] == o['type'] and (
            choice['type'] == 'pass' or
            choice['type'] == 'switch' and choice['species'] == o['species'] or
            choice['type'] == 'move' and choice['move_id'] == o['move_id'] and choice['target'] in (o.get('targets') or [0])
        ) for o in s['options'])
    assert Policy.valid_pair(action['slot_0'], action['slot_1'])


def test_static_data(dex):
    assert len(dex.species) > 1000 and len(dex.moves) > 700
    assert dex.pokemon('Incineroar')['types'] == ['Fire', 'Dark']
    assert dex.move('Moonblast')['base_power'] == 95


@pytest.mark.parametrize('attack,defenders,expected', [('electric', ['Water', 'Flying'], 4), ('ground', ['Flying'], 0), ('fairy', ['Dragon', 'Steel'], 1), ('ghost', ['Normal'], 0), ('fire', ['Water', 'Dragon'], .25)])
def test_types(attack, defenders, expected):
    assert effectiveness(attack, defenders) == expected


def test_draft_only_legal_and_synergy(dex):
    st = next(sample_states())
    st['legal_actions']['actions'] = [a for a in st['legal_actions']['actions'] if a['input']['action']['card_id'] == 'vgc-tornadus']
    assert policy(dex).decide(st)['card_id'] == 'vgc-tornadus'


def test_preview_four_unique_and_leads_subset(dex):
    st = list(sample_states())[1]
    a = policy(dex).decide(st)
    assert len(set(a['bring'])) == 4
    assert len(set(a['leads'])) == 2 and set(a['leads']) <= set(a['bring'])
    assert set(a['bring']) <= {c['species'] for c in st['observation']['your_roster']}


def test_default_battle_legal_and_fast(dex):
    st = battle_state()
    p = policy(dex)
    started = time.perf_counter()
    a = p.decide(st)
    assert time.perf_counter() - started < 1
    is_legal(a, st)
    assert any(c.get('move_id') == 'moonblast' for c in [a['slot_0'], a['slot_1']])


def test_single_reserve_both_fainted(dex):
    opts = [{'type': 'switch', 'species': 'rillaboom'}, {'type': 'pass'}]
    st = battle_state(active=[{'species': 'incineroar', 'fainted': True}, {'species': 'fluttermane', 'fainted': True}], options=[opts, opts])
    a = policy(dex).decide(st)
    is_legal(a, st)
    assert sorted([a['slot_0']['type'], a['slot_1']['type']]) == ['pass', 'switch']


def test_two_reserves_no_duplicate_switch(dex):
    opts = [{'type': 'switch', 'species': 'rillaboom'}, {'type': 'switch', 'species': 'amoonguss'}]
    st = battle_state(options=[opts, opts])
    a = policy(dex).decide(st)
    is_legal(a, st)
    assert a['slot_0']['species'] != a['slot_1']['species']


def test_one_forced_switch_other_pass(dex):
    st = battle_state(options=[[{'type': 'pass'}], [{'type': 'switch', 'species': 'rillaboom'}]])
    is_legal(policy(dex).decide(st), st)


def test_only_legal_targets_used(dex):
    st = battle_state(options=[[{'type': 'move', 'move_id': 'pollenpuff', 'targets': [-2]}], [{'type': 'move', 'move_id': 'protect', 'targets': [0]}]])
    a = policy(dex).decide(st)
    is_legal(a, st)
    assert a['slot_0']['target'] == -2


def test_focus_fire_can_secure_ko(dex):
    opts = [{'type': 'move', 'move_id': 'tackle', 'targets': [1, 2]}]
    st = battle_state(active=[pokemon('Tauros', ['Tackle']), pokemon('Tauros', ['Tackle'])], foes=[pokemon('Blissey', current_hp_fraction=.4), pokemon('Snorlax')], options=[opts, opts])
    a = policy(dex).decide(st)
    assert a['slot_0']['target'] == a['slot_1']['target'] == 1


@pytest.mark.parametrize('ability,typ', [('Levitate', 'ground'), ('Storm Drain', 'water'), ('Lightning Rod', 'electric'), ('Flash Fire', 'fire'), ('Sap Sipper', 'grass')])
def test_ability_immunities(dex, ability, typ):
    p = policy(dex)
    f = pokemon('Gengar', ability=ability, types=['Normal'])
    assert p.damage(pokemon('Mew'), f, {'base_power': 100, 'type': typ, 'category': 'Special'}) == 0


def test_psychic_terrain_blocks_priority(dex):
    p = policy(dex)
    assert p.damage(pokemon('Incineroar'), pokemon('Rillaboom'), 'Fake Out', {'fields': ['psychicterrain']}) == 0


def test_spore_avoids_grass(dex):
    st = battle_state(active=[pokemon('Amoonguss'), pokemon('Incineroar')], foes=[pokemon('Rillaboom'), pokemon('Urshifu-Rapid-Strike')], options=[[{'type': 'move', 'move_id': 'spore', 'targets': [1, 2]}], [{'type': 'move', 'move_id': 'protect', 'targets': [0]}]])
    a = policy(dex).decide(st)
    assert a['slot_0']['target'] == 2


def test_fakeout_not_used_consecutively(dex):
    opts = [{'type': 'move', 'move_id': 'fakeout', 'targets': [1]}, {'type': 'move', 'move_id': 'flareblitz', 'targets': [2]}]
    st = battle_state(options=[opts, [{'type': 'move', 'move_id': 'moonblast', 'targets': [1]}]])
    p = policy(dex)
    p.decide(st)
    st['observation']['turn'] = 2
    a = p.decide(st)
    assert a['slot_0']['move_id'] == 'flareblitz'


def test_policy_memory_isolated(dex):
    a, b = policy(dex), policy(dex)
    a.decide(next(sample_states()))
    assert a.cards and not b.cards


def test_fallback_also_legal(dex):
    st = battle_state(options=[[{'type': 'switch', 'species': 'rillaboom'}, {'type': 'pass'}], [{'type': 'switch', 'species': 'rillaboom'}, {'type': 'pass'}]])
    is_legal(policy(dex).decide(st, fallback=True), st)


def test_missing_options_fail_instead_of_inventing(dex):
    st = battle_state()
    st['legal_actions']['actions'][0]['input']['action']['slots'][0]['options'] = []
    with pytest.raises(ValueError, match='No compatible'):
        policy(dex).decide(st)


def test_action_discriminator_does_not_erase_move_type(dex):
    assert dex.move({'type': 'move', 'move_id': 'moonblast'})['type'] == 'Fairy'
    p = policy(dex)
    target = pokemon('Urshifu-Rapid-Strike')
    attacker = pokemon('Flutter Mane')
    assert p.damage(attacker, target, {'type': 'move', 'move_id': 'moonblast'}) == p.damage(attacker, target, 'Moonblast')


def test_pollen_puff_heals_ally(dex):
    st = battle_state(active=[pokemon('Amoonguss'), pokemon('Incineroar', current_hp_fraction=.2)], options=[[{'type': 'move', 'move_id': 'pollenpuff', 'targets': [-2, 1]}], [{'type': 'move', 'move_id': 'protect', 'targets': [0]}]])
    a = policy(dex).decide(st)
    assert a['slot_0']['target'] == -2


def test_mid_battle_restart_avoids_fakeout(dex):
    st = battle_state(turn=5, options=[[{'type': 'move', 'move_id': 'fakeout', 'targets': [1]}, {'type': 'move', 'move_id': 'flareblitz', 'targets': [2]}], [{'type': 'move', 'move_id': 'moonblast', 'targets': [1]}]])
    assert policy(dex).decide(st)['slot_0']['move_id'] == 'flareblitz'
