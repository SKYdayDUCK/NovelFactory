"""A warm pure-coordinate cache must have exactly the schema's semantics."""
from copy import deepcopy

import pytest
from hypothesis import given
from hypothesis import strategies as st

from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.errors import DomainError
from novel_factory.domain.temporal import _validated_point_key, point_key


def outcome(call):
    try:
        return ('value', call())
    except DomainError as error:
        return ('error', error.code, error.safe_message)


@given(st.integers(min_value=-(2**64), max_value=2**64),
       st.integers(min_value=-(2**40), max_value=2**40))
def test_cached_coordinates_match_authoritative_schema(tick, phase):
    value = {'tick': tick, 'phase': phase}
    expected = outcome(lambda: tuple(validate_contract('StoryPoint', value)[k]
                                     for k in ('tick', 'phase')))
    assert outcome(lambda: point_key(value)) == expected
    assert outcome(lambda: point_key(value)) == expected


@pytest.mark.parametrize('value', [
    {'tick': True, 'phase': 0}, {'tick': 1, 'phase': False},
    {'tick': 1.0, 'phase': 0}, {'tick': 1, 'phase': 0.0},
    {'tick': '1', 'phase': 0}, {'tick': 1},
    {'tick': 1, 'phase': 0, 'authority': 'secret'}, None, [],
])
def test_warm_cache_never_accepts_extra_fields_or_skips_original_type_rules(value):
    point_key({'tick': 1, 'phase': 0})
    expected = outcome(lambda: tuple(validate_contract('StoryPoint', value)[k]
                                     for k in ('tick', 'phase')))
    assert outcome(lambda: point_key(value)) == expected


def test_input_mutation_cannot_poison_cached_coordinate_and_cache_is_bounded():
    value = {'tick': 1, 'phase': 0}
    original = deepcopy(value)
    assert point_key(value) == (1, 0)
    value['tick'] = 9
    assert point_key(value) == (9, 0)
    value['extra'] = 1
    with pytest.raises(DomainError):
        point_key(value)
    assert point_key(original) == (1, 0)
    for tick in range(5000):
        point_key({'tick': tick, 'phase': 0})
    assert _validated_point_key.cache_info().currsize <= 4096
