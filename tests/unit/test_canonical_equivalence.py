"""Differential NF-CANON-1 checks against the immutable pre-optimization source."""
import importlib.util
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from novel_factory.domain import canonical
from novel_factory.domain.errors import DomainError

REFERENCE_SHA256 = '3fc335ad00e3973b200683e953e5066a64ce6f0040035df000c3dfc350fa1571'
# Exact canonical.py bytes from commit 0a0af490c7768fe4b7d2da16dbaa74f0d9585f0d.
REFERENCE_PATH = Path(__file__).parents[1] / 'fixtures/canonical_nf_canon_1_reference.py'
assert sha256(REFERENCE_PATH.read_bytes()).hexdigest() == REFERENCE_SHA256
spec = importlib.util.spec_from_file_location('novel_factory.domain._canonical_reference', REFERENCE_PATH)
reference = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reference)


def outcome(function, value):
    try:
        return ('value', function(value))
    except DomainError as exc:
        return ('error', exc.code, exc.safe_message, exc.retryable, exc.recovery)


text = st.text(alphabet=st.characters(blacklist_categories=()), max_size=30)
scalar = st.one_of(st.none(), st.booleans(), st.integers(min_value=-(2**65), max_value=2**65),
                   st.floats(allow_nan=True, allow_infinity=True), text)
values = st.recursive(scalar, lambda child: st.one_of(
    st.lists(child, max_size=6),
    st.dictionaries(st.one_of(text, st.integers(-2, 2)), child, max_size=6)), max_leaves=25)


@settings(max_examples=500, derandomize=True, deadline=None)
@given(values)
def test_full_canonical_bytes_and_error_envelopes_match_frozen_reference(value):
    assert outcome(canonical.canonical_bytes, value) == outcome(reference.canonical_bytes, value)
    assert outcome(canonical.fingerprint, value) == outcome(reference.fingerprint, value)


class IntegerSubclass(int):
    pass


class StringSubclass(str):
    pass


class ListSubclass(list):
    pass


class DictSubclass(dict):
    pass


@pytest.mark.parametrize('value', [
    IntegerSubclass(1), StringSubclass('native-looking'), ListSubclass([1]), DictSubclass(a=1),
    {'nested': IntegerSubclass(1)}, {StringSubclass('key'): 1},
    {'nested': [Decimal(1)]}, {'nested': object()}, b'bytes', ('tuple',),
    {'first': 1.0, '\ud800': None}, {'\ud800': None, 'later': 1.0},
    {'first': {'\udfff': 1}, 'later': object()}, {1: 'bad key', 'later': '\ud800'},
    [2**53, '\ud800'], ['\ud800', 2**53],
    {'n': 2**53 - 1}, {'n': -(2**53 - 1)}, {'n': 2**53}, {'n': -(2**53)},
    {'text': '\x00\x7f中文😀'}, {'\x00\x7f中文😀': 'value'},
])
def test_native_types_unicode_safe_integer_bounds_and_first_error_order(value):
    assert outcome(canonical.canonical_bytes, value) == outcome(reference.canonical_bytes, value)


@pytest.mark.parametrize('content', [
    '{"a":1,"a":2}', '{"a":{"b":1,"b":2}}', '{"n":NaN}', '{"n":Infinity}',
    '{"n":1.0}', '{"n":1e3}', '{"n":9007199254740992}',
    '{"x":"\\ud800"}', '{"\\ud800":1}', '{"x":"\\udfff"}',
    '{"x":"中文😀"}', b'{"x":"\xff"}', '{"a":true,"b":null,"n":-3}',
])
def test_strict_loader_keeps_duplicate_number_and_unicode_rejections(content):
    assert outcome(canonical.strict_json_loads, content) == outcome(reference.strict_json_loads, content)
