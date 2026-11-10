"""Join the immutable v2.0-R1 A-H design checks to actual pytest JUnit outcomes.

This records coverage rather than changing the original planned matrix. A test
name in this mapping is not a PASS until its executed JUnit instance passed.
Engineering coverage, exact workload limitations, model operation and literary
acceptance remain separate fields.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHECKS = {
    'R1-A-01': ['test_actual_sword_acceptance_history_and_ryw',
                'test_r1_sword_history_owner_holder_destroyed_and_belief'],
    'R1-A-02': ['test_r1_sword_history_owner_holder_destroyed_and_belief',
                'test_real_query_world_belief_unknown_and_unadvanced_distinct'],
    'R1-A-03': ['test_destroyed_future_prerequisite_fences_immediately_then_replans_locally'],
    'R1-A-04': ['test_destroyed_future_prerequisite_fences_immediately_then_replans_locally',
                'test_outbox_failure_redelivery_and_inbox_atomicity'],
    'R1-B-01': ['test_r1_b_reviewed_loan_to_gift_preserves_history_and_promotes',
                'test_release_complete_exact_manifest_idempotency_and_immutable_prior'],
    'R1-B-02': ['test_r1_b_reviewed_loan_to_gift_preserves_history_and_promotes',
                'test_real_world_history_and_authorization_survive_isolated_restore'],
    'R1-B-03': ['test_r1_b_reviewed_loan_to_gift_preserves_history_and_promotes'],
    'R1-B-04': ['test_r1_b_promotion_compares_both_canon_and_candidate_heads',
                'test_promotion_cas_preserves_new_canon_and_repair_history'],
    'R1-B-05': ['test_cross_branch_text_requires_new_scene_task_and_source_qualification',
                'test_explicit_new_world_alignment_reuses_exact_text_but_requires_new_approval'],
    'R1-C-01': ['test_all_six_actor_completion_orders_preserve_selected_joint_world'],
    'R1-C-02': ['test_r1_c_unproven_inbound_blocks_related_scope_but_not_remote'],
    'R1-C-03': ['test_unrelated_region_can_advance_and_channel_exact_bound_is_respected'],
    'R1-C-04': ['test_r1_c_delayed_message_observation_and_duplicate_delivery_preserve_knowledge_time'],
    'R1-C-05': ['test_three_actor_joint_result_is_atomic_idempotent_and_sealed',
                'test_all_six_actor_completion_orders_preserve_selected_joint_world'],
    'R1-D-01': ['test_non_admin_artifact_must_match_live_task_generation_and_cut',
                'test_pagination_pins_world_and_rejects_changed_query',
                'test_fixed_cut_stale_search_is_not_unknown_world'],
    'R1-D-02': ['test_fork_inherits_selection_once_and_isolates_future',
                'test_r1_b_reviewed_loan_to_gift_preserves_history_and_promotes'],
    'R1-D-03': ['test_fixed_latest_batch_and_compiler_rebind',
                'test_cross_branch_text_requires_new_scene_task_and_source_qualification'],
    'R1-D-04': ['test_actor_context_rejects_world_plan_other_actor_and_future',
                'test_actor_material_errors_do_not_reveal_artifact_existence'],
    'R1-E-01': ['test_actor_context_rejects_world_plan_other_actor_and_future',
                'test_actor_receives_only_own_compiled_belief_and_frozen_runtime',
                'test_cache_hit_rechecks_revoked_grant',
                'test_reader_inputs_do_not_grant_text_or_hidden_material_access'],
    'R1-E-02': ['test_actor_hidden_and_nonexistent_have_identical_answer_shape',
                'test_actor_material_errors_do_not_reveal_artifact_existence'],
    'R1-E-03': ['test_character_host_without_verified_isolation_cannot_dispatch',
                'test_real_http_transport_shape_is_stateless_text_only'],
    'R1-E-04': ['test_valid_belief_cannot_cite_forged_or_other_actor_evidence',
                'test_formal_late_knowledge_preserves_target_time_and_supports_retrospection',
                'test_unreliable_reader_disclosure_is_accepted_text_without_world_truth'],
    'R1-F-01': ['test_r1_f_200_slots_20_independent_and_20_intents_require_explicit_joint_requalification'],
    'R1-F-02': ['test_write_skew_two_guards_cannot_both_leave',
                'test_negative_range_phantom_invalidates_clear_route',
                'test_scope_identity_cannot_be_renamed_or_reset_for_aba',
                'test_new_registered_influence_invalidates_existing_guarded_certificate',
                'test_interpretation_epoch_invalidates_prepared_acceptance'],
    'R1-F-03': ['test_all_six_actor_completion_orders_preserve_selected_joint_world',
                'test_r1_f_200_slots_20_independent_and_20_intents_require_explicit_joint_requalification'],
    'R1-F-04': ['test_after_sample_change_does_not_refresh_certificate_tokens',
                'test_expired_and_unissued_certificates_are_rejected',
                'test_current_grant_and_approval_revocation_block_accept'],
    'R1-F-05': ['test_r1_f_200_slots_20_independent_and_20_intents_require_explicit_joint_requalification'],
    'R1-F-06': ['test_same_key_retry_and_new_key_do_not_repeat_effects',
                'test_r1_f_200_slots_20_independent_and_20_intents_require_explicit_joint_requalification',
                'test_real_world_history_and_authorization_survive_isolated_restore'],
    'R1-G-01': ['test_r1_g_macro_transport_and_three_presentations_do_not_repeat_any_effect'],
    'R1-G-02': ['test_text_first_delta_must_use_formal_kernel_acceptance_then_exact_alignment',
                'test_forged_clear_report_and_wrong_span_never_receive_acceptance'],
    'R1-G-03': [],
    'R1-G-04': ['test_fork_inherits_selection_once_and_isolates_future',
                'test_cross_branch_text_requires_new_scene_task_and_source_qualification'],
    'R1-H-01': ['test_r1_h_real_tcp_lost_reply_retry_revocation_and_outbox_recovery'],
    'R1-H-02': ['test_old_generation_and_cancelled_task_cannot_commit',
                'test_lost_response_then_cancel_retrieves_original_receipt',
                'test_task_authorization_is_rechecked_inside_mutation_transaction'],
    'R1-H-03': ['test_outbox_failure_redelivery_and_inbox_atomicity',
                'test_real_world_history_and_authorization_survive_isolated_restore',
                'test_world_transaction_process_crash_and_retry'],
    'R1-H-04': ['test_r1_h_real_tcp_lost_reply_retry_revocation_and_outbox_recovery'],
    'R1-H-05': ['test_r1_h_real_tcp_lost_reply_retry_revocation_and_outbox_recovery',
                'test_world_transaction_process_crash_and_retry',
                'test_fixed_cut_stale_search_is_not_unknown_world'],
}

# These requirements are deliberately explicit. One passing pytest parameter
# must not stand in for all six permutations, both promotion races, or both
# crash boundaries. Update this reviewed list when the underlying tests change.
REQUIRED_VARIANTS = {
    'test_all_six_actor_completion_orders_preserve_selected_joint_world':
        [f'return_order{index}' for index in range(6)],
    'test_r1_b_promotion_compares_both_canon_and_candidate_heads': ['canon', 'candidate'],
    'test_world_transaction_process_crash_and_retry': ['after_events', 'after_commit'],
    'test_r1_c_unproven_inbound_blocks_related_scope_but_not_remote': ['True', 'False'],
}

QUALIFICATIONS = {
    'R1-A-04': 'Synchronous task fences work with no dependency consumer; explicit director replan '
               'and duplicate command replay are tested. A running dependency worker was not '
               'actually paused and resumed, so asynchronous catch-up is not established.',
    'R1-B-01': 'History retcon and immutable prior releases are composed from separate integration '
               'tests; old release text remains tied to its original WorldRef.',
    'R1-C-01': 'All six orders of three actor outputs, plus reversed/forward twenty actor input '
               'orders with fixed selected decision. This is deterministic fixture arbitration.',
    'R1-C-03': 'Exclusive incoming-bound semantics are tested at D31; this is the same rule as '
               'design example D42, not an assertion that the literal D42 dataset ran.',
    'R1-E-02': 'Visible payload/error shapes are checked. Statistical latency and response-size '
               'side-channel resistance under hostile traffic is not established.',
    'R1-E-03': 'Unsafe Host dispatch is rejected; the tested HTTP adapter uses independent stateless '
               'calls. A real model/Host end-to-end experiment remains BLOCKED_ENVIRONMENT.',
    'R1-F-01': 'Twenty independent commands retain W0 and guarded acceptance. The later joint '
               'interaction requires an explicit new common input cut after HEAD moves.',
    'R1-F-03': 'One fixed selected result is accepted after all twenty intents seal; no winner '
               'is selected by DB lock acquisition. The old joint proposal is rejected on HEAD change.',
    'R1-F-05': 'Workload JSON separates true resource conflicts, old-base accepted receipts, '
               'duplicate retries and backpressure. Strict joint requires explicit requalification.',
    'R1-G-03': 'Complex macro/micro equivalence replacement belongs to P3-02 and was explicitly '
               'excluded by the user. P2 only accepts presentation with zero new effects.',
    'R1-G-01': 'The exact food 100→88, health 100→97 and crate handoff fixture runs at D53. '
               'Health is an explicit conserved synthetic resource, not a general health simulation engine.',
    'R1-H-05': 'The real TCP failure and retry are retained, with an idempotent minimal '
               'index-watermark consumer. Separate stale-search tests preserve PARTIAL/STALE '
               'versus exact current state. A full production search-index worker was not '
               'stopped and recovered; watermark catch-up is not that broader claim.',
}


def load_coverage(loads: list[dict]) -> list[dict]:
    """Require both populations and normal plus hot/open-arrival observations."""
    requirements = []
    for agents in (50, 200):
        for scenario in ('mixed', 'hotspot_overload'):
            matches = [run for run in loads if run['logical_agents'] == agents and (
                run['profile'] == 'mixed' if scenario == 'mixed' else
                run['profile'] == 'overload' and (run['hot_subject_fraction'] or 0) >= .8
                and (run['overload_arrival_rps'] or 0) > 0
                and (run['overload_rounds'] or 0) > 0)]
            requirements.append({'logical_agents': agents, 'scenario': scenario,
                'status': ('NOT_EXECUTED' if not matches else
                           'NOT_MET' if any(run.get('capacity_result', run['result']) != 'PASS' for run in matches)
                           else 'PASS_FOR_PROVIDED_RUNS'),
                'evidence_paths': [run['path'] for run in matches]})
    return requirements


def build_matrix(junit_paths: list[Path], load_paths: list[Path]) -> dict:
    executions = {}
    inputs = []
    for path in junit_paths:
        root = ET.parse(path).getroot()
        inputs.append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        for testcase in root.iter('testcase'):
            name = testcase.attrib['name']
            identity = testcase.attrib.get('classname', '') + '::' + name
            executions[identity] = {'name': name.split('[', 1)[0], 'instance_name': name,
                'testcase': identity,
                'status': 'FAIL' if any(testcase.find(tag) is not None for tag in ('error', 'failure'))
                          else 'SKIPPED' if testcase.find('skipped') is not None else 'PASS',
                'seconds': float(testcase.attrib.get('time', 0)), 'junit': str(path)}
    source = json.loads((ROOT/'validation/r1_case_matrix.json').read_text())
    cases = []
    for source_case in source['cases']:
        checks = []
        for check in source_case['acceptance_checks']:
            test_id = check['test_id']
            required = CHECKS[test_id]
            matches = [record for record in executions.values() if record['name'] in required]
            missing = sorted(set(required)-{record['name'] for record in matches})
            variants = [f'{name}[{variant}]' for name in required
                        for variant in REQUIRED_VARIANTS.get(name, [])]
            missing_variants = sorted(set(variants)-{record['instance_name'] for record in matches})
            status = ('NOT_IN_SCOPE_P3_02' if test_id == 'R1-G-03' else
                      'NOT_EXECUTED' if missing or missing_variants or not matches else
                      'FAIL' if any(r['status'] == 'FAIL' for r in matches) else
                      'SKIPPED' if any(r['status'] == 'SKIPPED' for r in matches) else 'PASS')
            if test_id == 'R1-E-02' and status == 'PASS':
                status = 'PASS_VISIBLE_SHAPE_ONLY'
            if test_id == 'R1-A-04' and status == 'PASS':
                status = 'PARTIAL_SYNC_FENCE_ONLY'
            checks.append({'test_id': test_id, 'criterion': check['criterion'],
                'status': status, 'required_test_names': required, 'missing_tests': missing,
                'required_test_variants': variants, 'missing_test_variants': missing_variants,
                'execution_evidence': matches, 'qualification': QUALIFICATIONS.get(test_id)})
        failed = any(c['status'] in {'FAIL', 'NOT_EXECUTED', 'SKIPPED'} for c in checks)
        partial = any(c['status'].startswith('PARTIAL_') for c in checks)
        overall = ('INCOMPLETE' if failed else 'PARTIAL_WITH_STATED_BOUNDARIES' if partial
                   else 'PASS_WITH_STATED_BOUNDARIES')
        cases.append({'case_id': source_case['case_id'], 'title': source_case['title'],
            'engineering_coverage': overall, 'checks': checks,
            'exact_variant': ('NOT_SUPPORTED_STRICT_REBIND_REQUIRED' if source_case['case_id'] == 'R1-F'
                              else 'SEE_PER_CHECK_QUALIFICATIONS')})
    loads = []
    for path in load_paths:
        data = json.loads(path.read_text())
        loads.append({'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'logical_agents': data['logical_agent_task_count'],
            'result': data['engineer_correctness'], 'profile': data['profile'],
            'capacity_result': data.get('capacity_gate', data['engineer_correctness']),
            'first_attempt_capacity_gate': data.get('first_attempt_capacity_gate', 'LEGACY_NOT_SEPARATED'),
            'recovered_completion_gate': data.get('bounded_recovery_business_gate', 'LEGACY_NOT_SEPARATED'),
            'open_loop_offered_rate': data.get('open_loop_offered_rate'),
            'offered_query_rps': data['load']['scheduled_phase_arrival_rps'],
            'hot_subject_fraction': data.get('hot_subject_fraction'),
            'overload_arrival_rps': data['load'].get('overload_arrival_rps'),
            'overload_rounds': data['load'].get('overload_rounds'),
            'actual_world_commits': data['correctness']['new_world_revisions'],
            'code_sha': data['environment']['git_sha'],
            'working_tree_dirty': data['environment']['git_dirty']})
    all_checks = [check for case in cases for check in case['checks']]
    correctness_gate = ('NOT_MET' if any(c['engineering_coverage'] == 'INCOMPLETE' for c in cases)
                        else 'PARTIAL_WITH_STATED_BOUNDARIES' if any(
                            c['engineering_coverage'] == 'PARTIAL_WITH_STATED_BOUNDARIES' for c in cases)
                        else 'PASS_WITH_STATED_BOUNDARIES')
    mapped_test_gate = ('NOT_MET' if any(c['status'] in {'FAIL', 'NOT_EXECUTED', 'SKIPPED'}
                                       for c in all_checks) else 'PASS')
    requirements = load_coverage(loads)
    load_run_gate = ('NOT_EXECUTED' if not loads else 'NOT_MET' if any(
        load['result'] != 'PASS' for load in loads) else 'PASS_FOR_PROVIDED_RUNS')
    capacity_run_gate = ('NOT_EXECUTED' if not loads else 'NOT_MET' if any(
        load['capacity_result'] != 'PASS' for load in loads) else 'PASS_FOR_PROVIDED_RUNS')
    coverage_gate = ('NOT_EXECUTED' if any(item['status'] == 'NOT_EXECUTED' for item in requirements)
                     else 'EXECUTED')
    load_gate = ('NOT_MET' if capacity_run_gate == 'NOT_MET' else
                 'NOT_EXECUTED' if coverage_gate == 'NOT_EXECUTED' else 'PASS_FOR_REQUIRED_SCENARIOS')
    return {'schema_version': 'p2-acceptance-evidence-1',
        'source_matrix': 'validation/r1_case_matrix.json', 'source_matrix_modified': False,
        'evidence_inputs': inputs, 'cases': cases,
        'check_counts': dict(Counter(c['status'] for c in all_checks)),
        'real_service_load_runs': loads,
        'correctness_check_gate': correctness_gate,
        'mapped_test_execution_gate': mapped_test_gate,
        'load_gate': load_gate,
        'provided_load_run_gate': load_run_gate,
        'provided_capacity_run_gate': capacity_run_gate,
        'required_load_scenarios': requirements,
        'load_coverage_gate': coverage_gate,
        'load_population_coverage': sorted({run['logical_agents'] for run in loads}),
        'overall_engineering_gate': ('MET_WITH_STATED_BOUNDARIES' if
            correctness_gate == 'PASS_WITH_STATED_BOUNDARIES' and load_gate == 'PASS_FOR_REQUIRED_SCENARIOS'
            else 'NOT_MET'),
        'independent_gates': {'real_model_production': 'BLOCKED_ENVIRONMENT',
            'author_full_read': 'PENDING_AUTHOR', 'overall_p2': 'NOT_FULLY_ACCEPTED'},
        'coverage_policy': 'Mappings are reviewed composition of real tests, not automatic proof '
            'from test names. Exact dataset deviations and untested subclaims stay visible. '
            'Source JUnit path+hash and concrete testcases support every executed claim.'}


def gate_exit_code(result: dict, scope: str) -> int:
    if scope == 'executed-tests':
        # CI may run a small smoke population, but must never call it 50/200
        # acceptance. Every mapped test instance and every supplied run must
        # pass. Partial design coverage remains explicit in the output report.
        return int(result['mapped_test_execution_gate'] != 'PASS'
                   or result['provided_load_run_gate'] == 'NOT_MET')
    return int(result['overall_engineering_gate'] != 'MET_WITH_STATED_BOUNDARIES')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--junit', type=Path, nargs='+', required=True)
    parser.add_argument('--load', type=Path, nargs='*', default=[])
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--gate-scope', choices=('full', 'executed-tests'), default='full',
        help='full requires design checks and 50/200 scenarios; executed-tests is a CI '
             'regression gate only and never changes the report overall acceptance')
    args = parser.parse_args()
    result = build_matrix(args.junit, args.load)
    exit_code = gate_exit_code(result, args.gate_scope)
    result.update(cli_gate_scope=args.gate_scope, cli_exit_code=exit_code)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'checks': result['check_counts'],
        'cases': {c['case_id']: c['engineering_coverage'] for c in result['cases']},
        'correctness_check_gate': result['correctness_check_gate'],
        'mapped_test_execution_gate': result['mapped_test_execution_gate'],
        'load_gate': result['load_gate'],
        'load_coverage_gate': result['load_coverage_gate'],
        'overall_engineering_gate': result['overall_engineering_gate'],
        'cli_gate_scope': args.gate_scope, 'cli_exit_code': exit_code}, ensure_ascii=False))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
