"""Default Pokémon-only strategy for the official starter's per-seat runtime.

Draft sets and accepted actions live in a fresh object for each match.
See STRATEGY.md for the scoring model, tuning points, and limitations.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from altruagent import DecisionContext, GameState, WithReasoning
from .strategy import Policy
from .strategy.dex import ident

GAME_TYPE = 'pokemon_vgc_doubles_draft'
STRATEGY_VERSION = 'vgc-showdown-calc-v2'


def _redact(value):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if any(word in k.lower() for word in ('token', 'authorization', 'secret', 'official_agent_key', 'execution_id')) else _redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r'eak_live_[A-Za-z0-9_]+', '[REDACTED]', value)
        return re.sub(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[REDACTED]', value)
    return value


def _source_hash():
    root = Path(__file__).resolve().parent
    paths = [root / 'agent.py', *sorted((root / 'strategy').rglob('*.py')), *sorted((root / 'strategy' / 'data').glob('*.json')), *sorted((root / 'strategy').rglob('*.js'))]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class PokemonAgent:
    def __init__(self, *, audit_dir='logs'):
        self.policy = Policy()
        self.audit_dir = Path(audit_dir) if audit_dir is not None else None
        self.pending = None
        self.session_id = None
        self.started = False
        self.source_hash = _source_hash()
        try:
            self.commit = subprocess.check_output(['git', '-C', str(Path(__file__).resolve().parent), 'rev-parse', 'HEAD'], stderr=subprocess.DEVNULL, text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            self.commit = None

    def _audit(self, event, context, **data):
        if self.audit_dir is None:
            return
        self.audit_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Hash identifiers rather than putting arbitrary server strings in file paths.
        name = hashlib.sha256((context.session_id + ':' + context.agent_id).encode()).hexdigest()[:24]
        path = self.audit_dir / f'pokemon-{name}.jsonl'
        record = {'time': datetime.now(timezone.utc).isoformat(), 'event': event,
                  'session_id': context.session_id, 'agent_id': context.agent_id, **_redact(data)}
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), 'a') as stream:
            stream.write(json.dumps(record, separators=(',', ':')) + '\n')

    def choose_action(self, state: GameState, context: DecisionContext):
        game_type = context.game_type or state.game_name
        if game_type != GAME_TYPE:
            raise ValueError(f'This strategy supports only {GAME_TYPE}; got {game_type!r}')
        if self.session_id is not None and self.session_id != context.session_id:
            raise ValueError('A Pokémon strategy instance must not be reused across matches')
        self.session_id = context.session_id
        self.policy.agent_id = context.agent_id
        observation = state.raw.get('observation', {})
        if not isinstance(observation, dict):
            raise ValueError('Expected a structured Pokémon player observation')
        if not state.legal_actions:
            raise ValueError('Strategy called without legal actions')
        envelope = {**state.raw, 'phase': state.phase, 'state_version': state.state_version,
                    'observation': observation,
                    'legal_actions': {'state_version': state.state_version, 'actions': [
                        {'action_id': a.action_id, 'label': a.label, 'input': a.input} for a in state.legal_actions
                    ]}}
        if not self.started:
            self._audit('start', context, version=STRATEGY_VERSION, source_sha256=self.source_hash, commit=self.commit,
                        model_services=[], damage_engine='@smogon/calc@0.12.0', game_type=game_type)
            self.started = True
        started = time.perf_counter()
        action = self.policy.decide(envelope)
        self.validate(action, envelope)
        self.pending = (envelope, action)
        self._audit('decision', context, version=STRATEGY_VERSION, state_version=state.state_version,
                    phase=state.phase, observation=observation, legal_actions=envelope['legal_actions'],
                    action=action, reasoning=self.policy.last_reason, damage_forecasts=list(self.policy.damage_forecasts.values()),
                    duration_ms=round((time.perf_counter() - started) * 1000, 3))
        return WithReasoning(action, self.policy.last_reason)

    def on_action_result(self, result: dict, context: DecisionContext):
        self._audit('action_result', context, result=result)
        if self.pending is not None and result.get('accepted') is True and not result.get('error'):
            state, action = self.pending
            self.policy.accepted(state, action)
        self.pending = None

    @staticmethod
    def validate(action, state):
        """Enforce the offered template; strategy never invents an option or target."""
        entries = state['legal_actions']['actions']
        kind = action.get('type')
        if kind == 'draft_pick':
            ids = {a['input'].get('action', {}).get('card_id') or a['action_id'].removeprefix('draft_pick:') for a in entries}
            if action['card_id'] not in ids:
                raise ValueError('Draft pick is absent from the legal list')
        elif kind == 'select_lineup':
            template = entries[0]['input']['action']
            roster = {ident(s) for s in template['roster']}
            bring, leads = [ident(s) for s in action['bring']], [ident(s) for s in action['leads']]
            if len(bring) != 4 or len(set(bring)) != 4 or not set(bring) <= roster:
                raise ValueError('Invalid bring-four selection')
            if len(leads) != 2 or len(set(leads)) != 2 or not set(leads) <= set(bring):
                raise ValueError('Invalid lead pair')
        elif kind == 'doubles_turn':
            slots = entries[0]['input']['action']['slots']
            for slot in slots:
                choice = action[f"slot_{slot['slot']}"]
                def matches(option):
                    if choice['type'] != option['type']:
                        return False
                    if choice['type'] == 'pass':
                        return True
                    if choice['type'] == 'switch':
                        return ident(choice['species']) == ident(option['species'])
                    if choice['type'] == 'move':
                        return ident(choice['move_id']) == ident(option['move_id']) and (
                            choice.get('target') in option['targets'] if option.get('targets') else 'target' not in choice)
                    return False
                if not any(matches(o) for o in slot['options']):
                    raise ValueError(f"Choice for slot {slot['slot']} is absent from legal options")
            a, b = action['slot_0'], action['slot_1']
            pass_only = all(all(o['type'] == 'pass' for o in s['options']) for s in slots)
            if not Policy.valid_pair(a, b) and not (pass_only and a['type'] == b['type'] == 'pass'):
                raise ValueError('Incompatible slot choices')
        else:
            raise ValueError(f'Unsupported action type {kind!r}')


def create_agent():
    return PokemonAgent()
