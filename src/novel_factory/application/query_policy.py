"""Intersect original runtime role profiles with live database grants.

control_operator is the explicit local author/control-plane role added by the
P1 implementation ADR. It is not one of the ten original runtime agent roles.
"""
from functools import lru_cache
from importlib.resources import files
import json
from pathlib import Path
from novel_factory.domain.errors import DomainError


@lru_cache(maxsize=1)
def profiles():
    path = files('novel_factory').joinpath('data/runtime/query_profiles.json')
    if path.is_file():
        content = path.read_text(encoding='utf-8')
    else:
        # Editable source checkout, not a fallback permission policy.
        path = Path(__file__).resolve().parents[3] / 'runtime/query_profiles.json'
        content = path.read_text(encoding='utf-8')
    return {profile['role_id']: profile for profile in json.loads(content)['profiles']}


def check_profile(grant, plane, operation=None, recipe=None):
    if plane not in grant['planes']:
        raise DomainError('VIEW_DENIED', 'Requested view is outside this grant')
    if grant['role_id'] == 'control_operator' and 'ADMIN' in grant['capabilities']:
        if plane not in {'WORLD', 'BELIEF', 'PLAN', 'SIMULATION'}:
            raise DomainError('UNSUPPORTED_SCHEMA', 'This view is not implemented in P1')
    else:
        profile = profiles().get(grant['role_id'])
        if profile is None or plane not in profile['allowed_planes']:
            raise DomainError('VIEW_DENIED', 'Requested view is outside this role')
        if operation and operation not in profile['allowed_operations']:
            raise DomainError('VIEW_DENIED', 'Operation is outside this role')
        if recipe and recipe not in profile['allowed_recipes']:
            raise DomainError('VIEW_DENIED', 'Recipe is outside this role')
    if plane == 'PLAN' and 'PLAN_READ' not in grant['capabilities']:
        raise DomainError('VIEW_DENIED', 'Plan reading capability is required')
    if operation == 'NEIGHBORHOOD' and 'GRAPH_READ' not in grant['capabilities']:
        raise DomainError('VIEW_DENIED', 'Graph reading capability is required')
