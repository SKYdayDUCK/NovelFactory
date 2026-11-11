"""Exact story coordinates and conservative bounded-time consistency checks."""
from __future__ import annotations
from collections import defaultdict, deque
from functools import lru_cache
from typing import Any
from novel_factory.contracts.registry import plain, validate_contract
from .errors import DomainError

PHASE_RADIX = 2147483648


@lru_cache(maxsize=4096)
def _validated_point_key(tick: int, phase: int) -> tuple[int, int]:
    """Reuse schema validation for exact immutable coordinates, never worlds.

    The authoritative validator still checks each distinct pair. The bounded
    cache contains only two integers; no grant, revision, task or mutable input
    can be retained or substituted. Invalid pairs raise and are not cached.
    """
    validate_contract('StoryPoint', {'tick': tick, 'phase': phase})
    return tick, phase


def point_key(point: Any) -> tuple[int, int]:
    # Check shape before lookup: {tick, phase, extra}, bools, floats and adapter
    # objects must take the original validator path, even on a warm cache.
    if (type(point) is dict and point.keys() == {'tick', 'phase'}
            and type(point['tick']) is int and type(point['phase']) is int):
        return _validated_point_key(point['tick'], point['phase'])
    point = validate_contract('StoryPoint', point)
    return point['tick'], point['phase']


def point_ordinal(point: Any) -> int:
    tick, phase = point_key(point)
    return tick * PHASE_RADIX + phase


def contains(interval: dict, point: Any) -> bool:
    key = point_key(point)
    start = point_key(interval['start'])
    end = interval.get('end')
    return start <= key and (end is None or key < point_key(end))


def validate_interval(interval: Any, open_end: bool = False) -> dict:
    item = plain(interval)
    if open_end:
        validate_contract('ValidityInterval', item, '2.1')
    else:
        validate_contract('StoryInterval', item)
    if item.get('end') is not None and point_key(item['start']) >= point_key(item['end']):
        raise DomainError('HARD_RULE_VIOLATION', 'Story interval must have start before end')
    return item


def validate_temporal_constraints(events: list[dict]) -> list[str]:
    """Return a deterministic topological occurrence order after feasibility.

Strict causal edges require at least one phase step. Bounds are inclusive;
unknown bounds stay unknown. Propagating lower bounds through the DAG detects
acyclic-but-impossible before constraints (legacy T12), not just graph cycles.
"""
    by_id = {}
    low, high = {}, {}
    infinity = 10**40
    for item in events:
        event = plain(item)
        occurrence = event['occurrence_id']
        if occurrence in by_id:
            raise DomainError('DUPLICATE_EFFECT', 'An occurrence has multiple selected versions')
        by_id[occurrence] = event
        value = event['occurrence_time']
        validate_contract('OccurrenceTime', value)
        if value['kind'] == 'EXACT':
            low[occurrence] = high[occurrence] = point_ordinal(value['at'])
        elif value['kind'] == 'BOUNDED':
            low[occurrence] = point_ordinal(value['earliest'])
            high[occurrence] = point_ordinal(value['latest'])
            if low[occurrence] > high[occurrence]:
                raise DomainError('HARD_RULE_VIOLATION', 'Inconsistent occurrence bounds')
        else:
            low[occurrence], high[occurrence] = -infinity, infinity
    successors, degrees = defaultdict(list), {key: 0 for key in by_id}
    for key, event in by_id.items():
        seen = set()
        for parent in event.get('causal_parents', []):
            previous = parent['occurrence_id']
            if previous in seen:
                raise DomainError('INVALID_ARGUMENT', 'Duplicate causal parent')
            seen.add(previous)
            if previous not in by_id or by_id[previous]['event_version_id'] != parent['event_version_id']:
                raise DomainError('HARD_RULE_VIOLATION', 'Causal parent is not a selected event version')
            successors[previous].append(key)
            degrees[key] += 1
    ready = deque(sorted(key for key, degree in degrees.items() if degree == 0))
    order = []
    while ready:
        key = ready.popleft()
        if low[key] > high[key]:
            raise DomainError('HARD_RULE_VIOLATION', 'Causal constraints cannot satisfy occurrence bounds')
        order.append(key)
        for child in sorted(successors[key]):
            low[child] = max(low[child], low[key] + 1)
            degrees[child] -= 1
            if degrees[child] == 0:
                ready.append(child)
    if len(order) != len(events):
        raise DomainError('HARD_RULE_VIOLATION', 'Causal before graph contains a cycle')
    return order
