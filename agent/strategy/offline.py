"""Synthetic player-view fixtures for an offline smoke test; not real match results."""
from .dex import Dex, ident


def pokemon(species, moves=None, **kw):
    return Dex().pokemon({'species': species, 'moves': moves or [], 'level': 50, 'current_hp_fraction': 1, **kw})


def battle_state(active=None, foes=None, options=None, turn=1):
    active = active or [pokemon('Incineroar', ['Fake Out', 'Flare Blitz', 'Protect']), pokemon('Flutter Mane', ['Moonblast', 'Shadow Ball', 'Protect'])]
    foes = foes or [pokemon('Urshifu-Rapid-Strike', ['Surging Strikes', 'Close Combat']), pokemon('Amoonguss', ['Spore', 'Pollen Puff'])]
    options = options or [
        [{'type': 'move', 'move_id': 'fakeout', 'targets': [1, 2]}, {'type': 'move', 'move_id': 'flareblitz', 'targets': [1, 2]}, {'type': 'move', 'move_id': 'protect', 'targets': [0]}],
        [{'type': 'move', 'move_id': 'moonblast', 'targets': [1, 2]}, {'type': 'move', 'move_id': 'shadowball', 'targets': [1, 2]}, {'type': 'move', 'move_id': 'protect', 'targets': [0]}]]
    return {'session_id': 'sample', 'state_version': 7, 'game_type': 'pokemon_vgc_doubles_draft', 'phase': 'moving', 'is_current_actor': True, 'is_terminal': False, 'status': 'in_progress', 'observation': {'turn': turn, 'is_doubles': True, 'active_pokemon': active, 'opponent_active_pokemon': foes, 'team': {str(i): p for i, p in enumerate(active)}, 'available_moves': [[], []]}, 'legal_actions': {'state_version': 7, 'actions': [{'action_id': 'doubles_turn', 'input': {'action': {'type': 'doubles_turn', 'slots': [{'slot': i, 'options': options[i]} for i in range(2)]}}}]}, 'next_actions': [{'tool': 'play_action'}]}


def sample_states():
    cards = [{'card_id': 'vgc-' + ident(p['species']), **p} for p in [pokemon('Incineroar', ['Fake Out', 'Flare Blitz', 'Parting Shot', 'Protect'], ability='Intimidate'), pokemon('Flutter Mane', ['Moonblast', 'Shadow Ball', 'Dazzling Gleam', 'Protect']), pokemon('Urshifu-Rapid-Strike', ['Surging Strikes', 'Close Combat', 'Aqua Jet', 'Protect']), pokemon('Rillaboom', ['Fake Out', 'Grassy Glide', 'Wood Hammer', 'U-turn']), pokemon('Tornadus', ['Tailwind', 'Bleakwind Storm', 'Taunt', 'Protect']), pokemon('Amoonguss', ['Spore', 'Rage Powder', 'Pollen Puff', 'Protect'])]]
    yield {'phase': 'draft', 'state_version': 0, 'observation': {'available_cards': cards, 'current_seat': 'me', 'rosters': {'me': [], 'foe': []}}, 'legal_actions': {'actions': [{'action_id': 'draft_pick:' + c['card_id'], 'input': {'action': {'type': 'draft_pick', 'card_id': c['card_id']}}} for c in cards]}}
    yield {'phase': 'team_preview', 'state_version': 12, 'observation': {'your_roster': cards, 'opponent_roster': [pokemon('Kyogre', ['Water Spout', 'Origin Pulse', 'Ice Beam']), pokemon('Rillaboom', ['Wood Hammer', 'Fake Out'])]}, 'legal_actions': {'actions': [{'action_id': 'select_lineup', 'input': {'action': {'type': 'select_lineup', 'roster': [p['species'] for p in cards]}}}]}}
    yield battle_state()


def main():
    import json
    import time
    from altruagent import DecisionContext, GameState
    from agent.agent import PokemonAgent
    context = DecisionContext(session_id='offline', tournament_id=None, game_type='pokemon_vgc_doubles_draft', agent_id='me')
    agent = PokemonAgent(audit_dir=None)
    for raw in sample_states():
        raw = {'session_id': 'offline', 'game_type': context.game_type, 'is_current_actor': True, 'status': 'in_progress', **raw}
        started = time.perf_counter()
        decision = agent.choose_action(GameState.from_mcp_state(raw), context)
        output = {'phase': raw['phase'], 'action': decision.action, 'reason': decision.reasoning_summary, 'duration_ms': round((time.perf_counter() - started) * 1000, 2)}
        if raw['phase'] == 'moving':
            chosen = set()
            for slot in (0, 1):
                choice = decision.action[f'slot_{slot}']
                if choice['type'] == 'move':
                    species = raw['observation']['active_pokemon'][slot]['species']
                    chosen.add((ident(species), ident(choice['move_id'])))
            output['damage_forecasts'] = [f for f in agent.policy.damage_forecasts.values()
                if (ident(f['attacker']), ident(f['move_id'])) in chosen]
        print(json.dumps(output))
        agent.on_action_result({'accepted': True}, context)
    print('Synthetic strategy check complete; no server connection or live match.')


if __name__ == '__main__':
    main()
