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
