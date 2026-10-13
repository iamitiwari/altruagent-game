"""Validate the custom policy through the official starter's types and runner."""
import copy
import json
import pytest
from altruagent import DecisionContext, GameState, WithReasoning
from altruagent.runner import run_game
from agent.agent import PokemonAgent, create_agent
from agent.strategy.offline import sample_states, battle_state, pokemon

CTX = DecisionContext(session_id='sample', tournament_id=None, game_type='pokemon_vgc_doubles_draft', agent_id='me')


def sdk_state(raw):
    return GameState.from_mcp_state({'session_id': CTX.session_id, 'game_type': CTX.game_type, 'status': 'in_progress', 'is_current_actor': True, 'is_terminal': False, **raw})


def test_factory_returns_custom_fresh_policy():
    a, b = create_agent(), create_agent()
    assert isinstance(a, PokemonAgent) and a.policy is not b.policy
    a.policy.cards['test'] = {}
    assert 'test' not in b.policy.cards


@pytest.mark.parametrize('index', [0, 1, 2])
def test_actual_sdk_observation_and_structured_decision(index):
    raw = list(sample_states())[index]
    state = sdk_state(raw)
    assert isinstance(state.observation, str)  # SDK stringifies; policy must use raw.
    agent = PokemonAgent(audit_dir=None)
    decision = agent.choose_action(state, CTX)
    assert isinstance(decision, WithReasoning)
    assert decision.action['type'] == ['draft_pick', 'select_lineup', 'doubles_turn'][index]
    assert decision.reasoning_summary


def test_accepted_hook_only_commits_successful_actions():
    agent = PokemonAgent(audit_dir=None)
    state = sdk_state(battle_state())
    agent.choose_action(state, CTX)
    agent.on_action_result({'error': 'STALE_STATE'}, CTX)
    assert not agent.policy.last_moves
    agent.choose_action(state, CTX)
    agent.on_action_result({'accepted': True}, CTX)
    assert agent.policy.last_moves


def test_preview_uses_template_ids():
    raw = list(sample_states())[1]
    template = raw['legal_actions']['actions'][0]['input']['action']
    template['roster'] = ['incineroar', 'fluttermane', 'urshifurapidstrike', 'rillaboom', 'tornadus', 'amoonguss']
    d = PokemonAgent(audit_dir=None).choose_action(sdk_state(raw), CTX).action
    assert set(d['bring']) <= set(template['roster'])
    assert set(d['leads']) <= set(d['bring'])


def test_no_target_move_omits_target():
    raw = battle_state(options=[[{'type': 'move', 'move_id': 'protect', 'targets': []}], [{'type': 'move', 'move_id': 'moonblast', 'targets': [1]}]])
    d = PokemonAgent(audit_dir=None).choose_action(sdk_state(raw), CTX).action
    assert d['slot_0'] == {'type': 'move', 'move_id': 'protect'}


def test_null_arrays_and_slot_active_fallback():
    raw = battle_state()
    obs = raw['observation']
    slots = raw['legal_actions']['actions'][0]['input']['action']['slots']
    for slot, active in zip(slots, obs['active_pokemon']):
        slot['active'] = active
    obs['active_pokemon'] = None
    obs['opponent_active_pokemon'] = None
    obs['available_moves'] = None
    obs['team'] = None
    d = PokemonAgent(audit_dir=None).choose_action(sdk_state(raw), CTX).action
    assert d['type'] == 'doubles_turn'


def test_both_pass_when_server_only_offers_pass():
    raw = battle_state(options=[[{'type': 'pass'}], [{'type': 'pass'}]])
    d = PokemonAgent(audit_dir=None).choose_action(sdk_state(raw), CTX).action
    assert d['slot_0'] == d['slot_1'] == {'type': 'pass'}


def test_audit_log_redacts_credentials_and_records_decisions(tmp_path):
    agent = PokemonAgent(audit_dir=tmp_path)
    raw = battle_state()
    raw['observation']['unexpected_token'] = 'seat-secret'
    agent.choose_action(sdk_state(raw), CTX)
    agent.on_action_result({'accepted': True, 'access_token': 'secret-token'}, CTX)
    path = next(tmp_path.glob('*.jsonl'))
    text = path.read_text()
    assert 'seat-secret' not in text and 'secret-token' not in text
    events = [json.loads(line) for line in text.splitlines()]
    assert [e['event'] for e in events] == ['start', 'decision', 'action_result']
    assert events[0]['source_sha256'] and events[1]['action']['type'] == 'doubles_turn'
    assert path.stat().st_mode & 0o777 == 0o600


def test_other_games_rejected_without_actions():
    ctx = DecisionContext(session_id='sample', tournament_id=None, game_type='werewolf', agent_id='me')
    with pytest.raises(ValueError, match='supports only'):
        PokemonAgent(audit_dir=None).choose_action(sdk_state(battle_state()), ctx)


def test_match_instance_cannot_leak_history_to_another_match():
    agent = PokemonAgent(audit_dir=None)
    agent.choose_action(sdk_state(battle_state()), CTX)
    other = DecisionContext(session_id='other', tournament_id=None, game_type=CTX.game_type, agent_id='me')
    with pytest.raises(ValueError, match='reused across matches'):
        agent.choose_action(sdk_state(battle_state()), other)


def test_validator_rejects_invented_target_and_duplicate_reserve():
    raw = battle_state()
    action = {'type': 'doubles_turn', 'slot_0': {'type': 'move', 'move_id': 'fakeout', 'target': 99}, 'slot_1': {'type': 'move', 'move_id': 'moonblast', 'target': 1}}
    with pytest.raises(ValueError, match='absent from legal'):
        PokemonAgent.validate(action, raw)
    reserve = [{'type': 'switch', 'species': 'rillaboom'}]
    raw = battle_state(options=[reserve, reserve])
    action = {'type': 'doubles_turn', 'slot_0': reserve[0], 'slot_1': reserve[0]}
    with pytest.raises(ValueError, match='Incompatible'):
        PokemonAgent.validate(action, raw)


@pytest.mark.parametrize('stale', [False, True])
def test_full_draft_preview_battle_via_official_runner(stale):
    from altruagent.mcp_transport import MCPToolError
    raws = list(sample_states())
    for i, raw in enumerate(raws):
        raw['state_version'] = i
        raw.setdefault('legal_actions', {})['state_version'] = i
    terminal = sdk_state({'is_terminal': True, 'is_current_actor': False, 'status': 'completed', 'state_version': 3, 'observation': {}})
    class Game:
        session_id = CTX.session_id
        def __init__(self):
            self.index = 0
            self.faulted = False
            self.actions = []
        def get_state(self):
            return sdk_state(raws[self.index])
        def play_action(self, *, action_id, action, state_version, reasoning_summary):
            assert state_version == self.index and reasoning_summary
            PokemonAgent.validate(action, raws[self.index])
            if stale and not self.faulted:
                self.faulted = True
                raise MCPToolError('simulated concurrent update', error_code='STALE_STATE', status_code=409)
            self.actions.append(action)
            self.index += 1
            next_state = sdk_state(raws[self.index]) if self.index < len(raws) else terminal
            return {'accepted': True, 'state': next_state.raw}
        def get_result(self):
            return {'is_terminal': True, 'status': 'completed', 'your_return': 1, 'returns': {'me': 1}}
    agent = PokemonAgent(audit_dir=None)
    game = Game()
    result = run_game(game, CTX, agent, log=lambda _: None)
    assert result.is_terminal
    assert [a['type'] for a in game.actions] == ['draft_pick', 'select_lineup', 'doubles_turn']
    assert agent.policy.last_moves
