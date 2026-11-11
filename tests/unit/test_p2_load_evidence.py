"""Regression tests for false performance/acceptance claims, no database needed."""
import json

from benchmarks.p2_acceptance_matrix import CHECKS, REQUIRED_VARIANTS, build_matrix, gate_exit_code
from benchmarks.p2_behavioral_load import summarize


def synthetic_junit(tmp_path, omitted=()):
    """Parser fixture only: no synthetic execution is exported as evidence."""
    path = tmp_path/'synthetic-parser-fixture.xml'
    instances = []
    for name in sorted({name for names in CHECKS.values() for name in names}):
        if name in REQUIRED_VARIANTS:
            instances.extend(f'{name}[{variant}]' for variant in REQUIRED_VARIANTS[name])
        else:
            instances.append(name)
    path.write_text('<testsuite>' + ''.join(
        f'<testcase classname="TEST_FIXTURE" name="{name}"/>'
        for name in instances if name not in omitted) + '</testsuite>')
    return path


def synthetic_load(tmp_path, agents, profile='mixed', result='PASS'):
    path = tmp_path/f'synthetic-{agents}-{profile}-{result}.json'
    path.write_text(json.dumps({'logical_agent_task_count': agents, 'engineer_correctness': result,
        'profile': profile, 'hot_subject_fraction': .8 if profile == 'overload' else .2,
        'load': {'scheduled_phase_arrival_rps': 10,
                 'overload_arrival_rps': 1000 if profile == 'overload' else None,
                 'overload_rounds': 5 if profile == 'overload' else None},
        'correctness': {'new_world_revisions': 0},
        'environment': {'git_sha': '0'*40, 'git_dirty': False}}))
    return path


def test_load_accounting_excludes_rejections_duplicate_receipts_and_unsent_requests():
    base = {'world_revision_id': 'base'}
    world = {'world_revision_id': 'new'}
    parent = {'world_revision_id': 'intervening'}
    receipt = {'world_ref': world, 'original_base_world_ref': base, 'parent_world_ref': parent}
    raw = []
    for index, (status, retry, code) in enumerate([
        (200, False, None), (200, True, None), (409, False, 'DEPENDENCY_CONFLICT'),
        (429, False, 'BACKPRESSURE'), ('transport_error', False, None),
    ]):
        raw.append({'label': 'accept', 'status': status, 'duplicate_retry': retry, 'code': code,
                    'latency_ms': 10+index, 'arrival_delay_ms': 0, 'agent': index})
    measured = summarize({'raw': raw, 'duration_seconds': 2, 'receipts': [receipt, receipt],
        'peak_inflight_http': 5, 'planned_requests': 6, 'correctness_errors': []})
    assert measured['api_success_rps'] == 1
    assert measured['effective_call_rps_excluding_duplicates'] == .5
    assert measured['effective_world_commits_per_second'] == .5
    assert measured['client_requests_never_emitted'] == 1
    assert measured['completed_requests'] == 5
    assert measured['conflict_count'] == 1
    assert measured['latency_all_responses']['count'] == 5
    assert measured['latency_successes']['count'] == 2
    assert measured['latency_rejections_and_failures']['count'] == 3
    assert measured['accepted_original_input_after_unrelated_head_progress'] == 1


def test_acceptance_mapping_never_counts_absent_or_failed_execution_as_pass(tmp_path):
    junit = tmp_path/'only-one-failed.xml'
    junit.write_text('<testsuite><testcase classname="tests.integration.test_world_kernel" '
        'name="test_r1_sword_history_owner_holder_destroyed_and_belief"><failure>boom</failure>'
        '</testcase></testsuite>')
    matrix = build_matrix([junit], [])
    checks = {check['test_id']: check for case in matrix['cases'] for check in case['checks']}
    assert checks['R1-A-01']['status'] != 'PASS'
    assert checks['R1-F-01']['status'] == 'NOT_EXECUTED'
    assert checks['R1-G-03']['status'] == 'NOT_IN_SCOPE_P3_02'
    assert matrix['independent_gates']['overall_p2'] == 'NOT_FULLY_ACCEPTED'


def test_invalid_200_duplicate_does_not_reduce_valid_success_throughput():
    raw = [
        {'label': 'query', 'status': 200, 'duplicate_retry': False, 'code': None,
         'latency_ms': 10, 'arrival_delay_ms': 100, 'agent': 0},
        {'label': 'accept', 'status': 200, 'duplicate_retry': True, 'code': None,
         'latency_ms': 10, 'arrival_delay_ms': 200, 'agent': 1, 'error_type': 'ValidationError'},
    ]
    result = summarize({'raw': raw, 'duration_seconds': 1, 'receipts': [],
                        'peak_inflight_http': 2, 'planned_requests': 2, 'correctness_errors': []})
    assert result['valid_successful_calls'] == 1
    assert result['successful_duplicate_receipts'] == 0
    assert result['effective_call_rps_excluding_duplicates'] == 1
    assert result['scheduled_to_completion_latency']['p95_ms'] == 210


def test_failed_load_blocks_overall_gate_even_when_all_mapped_test_instances_pass(tmp_path):
    # Synthetic parser input is local to this unit test, never production evidence.
    junit = synthetic_junit(tmp_path)
    load = synthetic_load(tmp_path, 200, result='FAIL')
    result = build_matrix([junit], [load])
    assert result['mapped_test_execution_gate'] == 'PASS'
    assert result['correctness_check_gate'] == 'PARTIAL_WITH_STATED_BOUNDARIES'
    assert result['load_gate'] == 'NOT_MET'
    assert result['overall_engineering_gate'] == 'NOT_MET'
    assert gate_exit_code(result, 'full') == 1
    assert gate_exit_code(result, 'executed-tests') == 1


def test_one_passing_population_cannot_satisfy_full_load_gate(tmp_path):
    result = build_matrix([synthetic_junit(tmp_path)], [synthetic_load(tmp_path, 50)])
    assert result['provided_load_run_gate'] == 'PASS_FOR_PROVIDED_RUNS'
    assert result['load_gate'] == 'NOT_EXECUTED'
    missing = {(row['logical_agents'], row['scenario']) for row in result['required_load_scenarios']
               if row['status'] == 'NOT_EXECUTED'}
    assert missing == {(50, 'hotspot_overload'), (200, 'mixed'), (200, 'hotspot_overload')}
    assert gate_exit_code(result, 'full') == 1
    assert gate_exit_code(result, 'executed-tests') == 0
    assert result['overall_engineering_gate'] == 'NOT_MET'


def test_partial_parameter_population_does_not_claim_all_permutations_or_promotion_races(tmp_path):
    permutation = 'test_all_six_actor_completion_orders_preserve_selected_joint_world'
    promotion = 'test_r1_b_promotion_compares_both_canon_and_candidate_heads'
    omitted = [f'{permutation}[return_order{index}]' for index in range(1, 6)]
    omitted.append(f'{promotion}[candidate]')
    result = build_matrix([synthetic_junit(tmp_path, omitted)], [])
    checks = {check['test_id']: check for case in result['cases'] for check in case['checks']}
    assert checks['R1-C-01']['status'] == 'NOT_EXECUTED'
    assert len(checks['R1-C-01']['missing_test_variants']) == 5
    assert checks['R1-B-04']['status'] == 'NOT_EXECUTED'
    assert checks['R1-B-04']['missing_test_variants'] == [f'{promotion}[candidate]']
    assert gate_exit_code(result, 'executed-tests') == 1


def test_all_load_scenarios_do_not_promote_partial_dependency_worker_coverage(tmp_path):
    paths = [synthetic_load(tmp_path, agents, profile)
             for agents in (50, 200) for profile in ('mixed', 'overload')]
    result = build_matrix([synthetic_junit(tmp_path)], paths)
    checks = {check['test_id']: check for case in result['cases'] for check in case['checks']}
    assert result['load_gate'] == 'PASS_FOR_REQUIRED_SCENARIOS'
    assert checks['R1-A-04']['status'] == 'PARTIAL_SYNC_FENCE_ONLY'
    assert 'not actually paused' in checks['R1-A-04']['qualification']
    assert 'full production search-index worker' in checks['R1-H-05']['qualification']
    assert result['correctness_check_gate'] == 'PARTIAL_WITH_STATED_BOUNDARIES'
    assert result['overall_engineering_gate'] == 'NOT_MET'
    assert gate_exit_code(result, 'full') == 1
    assert gate_exit_code(result, 'executed-tests') == 0


def test_ten_agent_ci_smoke_only_gates_executed_work(tmp_path):
    result = build_matrix([synthetic_junit(tmp_path)], [synthetic_load(tmp_path, 10)])
    assert all(item['status'] == 'NOT_EXECUTED' for item in result['required_load_scenarios'])
    assert result['load_population_coverage'] == [10]
    assert result['load_gate'] == 'NOT_EXECUTED'
    assert result['overall_engineering_gate'] == 'NOT_MET'
    assert gate_exit_code(result, 'executed-tests') == 0


def test_bounded_retry_freezes_bytes_and_key_and_accounts_budget():
    import asyncio
    from benchmarks.p2_behavioral_load import retry_same_request
    payload = {'idempotency_key': 'same-key', 'generation': 4, 'certificate': 'fixed'}
    sent, waits, outcomes = [], [], []

    async def send(data, attempt):
        sent.append(data)
        # Simulate unrelated caller state changing while the request is in flight.
        payload['generation'] += 1
        row = {'retryable': True, 'retry_after_seconds': 2}
        outcomes.append(row)
        return (None, row) if attempt < 3 else ({'receipt': 'original'}, row)

    async def wait(seconds):
        waits.append(seconds)

    result = asyncio.run(retry_same_request(send, payload, max_attempts=3, sleeper=wait))
    assert result == {'receipt': 'original'}
    assert sent[0] == sent[1] == sent[2]
    assert json.loads(sent[0]) == {'idempotency_key': 'same-key', 'generation': 4, 'certificate': 'fixed'}
    assert waits == [2, 2]
    assert not any(row.get('retry_exhausted') for row in outcomes)


def test_retry_exhaustion_is_bounded_and_does_not_turn_rejection_into_success():
    import asyncio
    from benchmarks.p2_behavioral_load import retry_same_request
    records = []

    async def send(data, attempt):
        row = {'retryable': True}
        records.append(row)
        return None, row

    async def wait(seconds):
        pass

    assert asyncio.run(retry_same_request(send, {}, max_attempts=2, sleeper=wait)) is None
    assert len(records) == 2 and records[-1]['retry_exhausted']
    # A Retry-After beyond the remaining wall-clock budget must terminate,
    # rather than ignoring the server or sleeping indefinitely.
    records.clear()

    async def long_wait(data, attempt):
        row = {'retryable': True, 'retry_after_seconds': 60}
        records.append(row)
        return None, row

    assert asyncio.run(retry_same_request(long_wait, {}, max_attempts=8,
                                         retry_budget_seconds=1, sleeper=wait)) is None
    assert len(records) == 1 and records[-1]['retry_exhausted']


def test_retry_policy_rejects_authority_generation_and_expired_certificate_failures():
    import asyncio
    from benchmarks.p2_behavioral_load import retry_same_request, retryable_failure
    assert retryable_failure(429, {'code': 'BACKPRESSURE', 'retryable': True, 'recovery': 'WAIT'})
    assert retryable_failure(503, {'code': 'SERVICE_UNAVAILABLE', 'retryable': True,
                                   'recovery': 'RETRY_SAME_COMMAND'})
    for code, status in [('GRANT_REVOKED', 403), ('STALE_CERTIFICATE', 409),
                         ('GENERATION_MISMATCH', 409), ('DEPENDENCY_CONFLICT', 409),
                         ('BACKPRESSURE', 500), ('UNAUTHORIZED', 429)]:
        assert not retryable_failure(status, {'code': code, 'retryable': True, 'recovery': 'WAIT'})
    assert not retryable_failure(429, {'code': 'BACKPRESSURE', 'retryable': False, 'recovery': 'WAIT'})
    calls = []

    async def denied(data, attempt):
        calls.append(attempt)
        return None, {'retryable': False, 'code': 'STALE_CERTIFICATE'}

    assert asyncio.run(retry_same_request(denied, {}, max_attempts=8)) is None
    assert calls == [1]


def test_recovery_metrics_keep_original_logical_count_and_include_wait_in_completion():
    raw = [
        {'operation_id': 'one', 'attempt': 1, 'label': 'propose', 'status': 429,
         'duplicate_retry': False, 'code': 'BACKPRESSURE', 'latency_ms': 2000,
         'arrival_delay_ms': 0, 'agent': 0},
        {'operation_id': 'one', 'attempt': 2, 'label': 'propose', 'status': 200,
         'duplicate_retry': False, 'code': None, 'latency_ms': 100,
         'arrival_delay_ms': 3100, 'agent': 0},
    ]
    measured = summarize({'raw': raw, 'duration_seconds': 4, 'receipts': [],
        'peak_inflight_http': 1, 'planned_requests': 2, 'correctness_errors': [],
        'operational_errors': []})
    assert measured['emitted_requests'] == 2
    assert measured['emitted_logical_operations'] == 1
    assert measured['client_requests_never_emitted'] == 1
    assert measured['infrastructure_retries'] == 1
    assert measured['recovered_logical_operations'] == 1
    assert measured['first_attempt_failure_count'] == 1
    assert measured['effective_call_rps_excluding_duplicates'] == .25
    assert measured['logical_operation_completion_latency']['p95_ms'] == 3200


def test_recovery_pass_never_promotes_capacity_or_missing_open_arrival_rate(tmp_path):
    from benchmarks.p2_behavioral_load import load_exit_code, observed_open_loop
    path = synthetic_load(tmp_path, 200)
    data = json.loads(path.read_text())
    data.update(capacity_gate='NOT_MET', first_attempt_capacity_gate='NOT_MET',
                bounded_recovery_business_gate='PASS')
    path.write_text(json.dumps(data))
    matrix = build_matrix([synthetic_junit(tmp_path)], [path])
    assert matrix['provided_load_run_gate'] == 'PASS_FOR_PROVIDED_RUNS'
    assert matrix['provided_capacity_run_gate'] == 'NOT_MET'
    assert matrix['load_gate'] == 'NOT_MET'
    assert gate_exit_code(matrix, 'full') == 1
    assert load_exit_code(data, 'capacity') == 1
    assert load_exit_code(data, 'recovered-completion') == 0
    raw = [{'label': 'overload', 'attempt': 1, 'started_seconds': i,
            'arrival_delay_ms': i*999} for i in range(5)]
    # A very fast retry must not be counted as another planned arrival.
    raw.append({'label': 'overload', 'attempt': 2, 'started_seconds': .0001,
                'arrival_delay_ms': 10})
    rate = observed_open_loop(raw, 1000)
    assert rate['gate'] == 'NOT_MET' and rate['first_attempts'] == 5
    assert rate['actual_call_start_rps'] == 1


def test_retry_budget_rechecks_deadline_after_delayed_wakeup():
    import asyncio
    from benchmarks.p2_behavioral_load import retry_same_request
    clock, records = [0.0], []

    async def send(data, attempt):
        row = {'retryable': True}
        records.append(row)
        return None, row

    async def delayed_wakeup(seconds):
        # Simulate overloaded event loop resuming well after requested sleep.
        clock[0] += 31

    assert asyncio.run(retry_same_request(send, {}, max_attempts=5, retry_budget_seconds=30,
        sleeper=delayed_wakeup, clock=lambda: clock[0])) is None
    assert len(records) == 1 and records[0]['retry_exhausted']


def test_derived_open_loop_rate_excludes_retry_attempts(tmp_path):
    from benchmarks.p2_load_derived import derive
    raw = [{'operation_id': str(i), 'label': 'overload', 'attempt': 1, 'status': 200,
            'duplicate_retry': False, 'code': None, 'latency_ms': 20, 'arrival_delay_ms': 0,
            'agent': i, 'started_seconds': float(i)} for i in range(3)]
    raw.append({**raw[-1], 'attempt': 2, 'started_seconds': 100.0})
    original = {'raw_requests': raw, 'metrics': {'planned_requests': 3, 'duration_seconds': 101,
        'peak_outstanding_client_requests': 1, 'correctness_errors': []},
        'world_commit_receipts': [], 'load': {'overload_arrival_rps': 1000},
        'environment': {'benchmark_source_sha256': '0'*64}, 'engineer_correctness': 'PASS',
        'ingress': {}, 'database_application': {}, 'hot_subject_fraction': .8}
    path = tmp_path/'retry-parser-fixture.json'
    path.write_text(json.dumps(original))
    derived = derive(path)
    assert derived['overload_observation']['requests'] == 3
    assert derived['overload_observation']['actual_call_starts_per_second'] == 1
    assert derived['overload_observation']['infrastructure_retry_attempts'] == 1
    assert derived['metrics']['client_requests_never_emitted'] == 0
