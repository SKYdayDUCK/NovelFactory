"""Deferred client work cannot turn invalid responses into successful capacity."""
import json
from uuid import uuid4

from benchmarks.p3_open_arrival import rate_from_times, validate_responses
from novel_factory.domain.errors import DomainError


def test_deferred_invalid_200_stays_failure_and_raw_response_is_removed():
    rows = [{'status': 200, 'valid': False, '_content': b'{}'},
            {'status': 'transport_error', 'valid': False}]
    fixture = {'world': {'world_ref': {}}, 'ids': {'smith': str(uuid4())}}
    elapsed = validate_responses(rows, fixture)
    assert elapsed >= 0
    assert not any(row['valid'] for row in rows)
    assert rows[0]['correctness_error'] == 'DomainError'
    assert 'response_sha256' in rows[0]
    assert all('_content' not in row for row in rows)


def test_explicit_overload_rejection_is_not_counted_as_a_success():
    error = DomainError('BACKPRESSURE', 'Request capacity is full', True, 'WAIT')
    rows = [{'status': 429, 'valid': False,
             '_content': json.dumps(error.as_dict(str(uuid4()))).encode()}]
    validate_responses(rows, {'world': {'world_ref': {}}, 'ids': {}})
    assert rows[0]['code'] == 'BACKPRESSURE'
    assert not rows[0]['valid']
    assert 'correctness_error' not in rows[0]


def test_no_wire_samples_or_one_wire_sample_cannot_establish_an_offered_rate():
    assert rate_from_times([]) is None
    assert rate_from_times([3]) is None
    assert rate_from_times([3, 3]) is None
    assert rate_from_times([2, 0, 1]) == 1
