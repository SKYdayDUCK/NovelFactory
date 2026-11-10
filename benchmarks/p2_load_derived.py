"""Recalculate accounting from retained raw requests without overwriting evidence.

Adds schedule-to-completion latency, separates failed/duplicate responses and
clarifies outstanding-client versus server-ingress counts. It never changes the
original scenario result or fabricates a missing source snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from benchmarks.p2_behavioral_load import summarize


def derive(path: Path) -> dict:
    original = json.loads(path.read_text())
    old_metrics = original['metrics']
    expected = old_metrics.get('planned_requests', old_metrics.get('planned_and_emitted_requests'))
    result = summarize({'raw': original['raw_requests'],
        'duration_seconds': old_metrics['duration_seconds'],
        'planned_requests': expected, 'receipts': original['world_commit_receipts'],
        'peak_inflight_http': old_metrics.get('peak_outstanding_client_requests', old_metrics.get('peak_inflight_http')),
        'correctness_errors': old_metrics['correctness_errors'],
        'operational_errors': old_metrics.get('unresolved_operational_errors', [])})
    overload = [row for row in original['raw_requests'] if row['label'] == 'overload' and row.get('attempt', 1) == 1]
    if overload:
        start = min(row['started_seconds'] for row in overload)
        last_start = max(row['started_seconds'] for row in overload)
        finish = max(row['started_seconds']+row['latency_ms']/1000 for row in overload)
        overload_summary = {'requests': len(overload),
            'infrastructure_retry_attempts': sum(row['label'] == 'overload' and row.get('attempt', 1) > 1
                for row in original['raw_requests']),
            'target_arrival_rps': original['load']['overload_arrival_rps'],
            'actual_call_start_window_seconds': last_start-start,
            'actual_call_starts_per_second': (len(overload)-1)/max(last_start-start, 1e-9),
            'completed_window_seconds': finish-start,
            'completion_rps': len(overload)/max(finish-start, 1e-9),
            'max_scheduling_delay_ms': max(row['arrival_delay_ms'] for row in overload),
            'note': 'First-attempt completion count includes rejected responses and is not effective business throughput. '
                    'Call starts include HTTPX connection wait; same-process load generator '
                    'shares CPU with server. This is not proof of achieved target wire RPS.'}
    else:
        overload_summary = None
    digest = original['environment']['benchmark_source_sha256']
    snapshot = path.parent/'source_snapshots'/f'{digest}.py'
    available = snapshot.exists() and hashlib.sha256(snapshot.read_bytes()).hexdigest() == digest
    return {'schema_version': 'p2-load-derived-1', 'original_path': str(path),
        'original_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'original_scenario_result_unchanged': original['engineer_correctness'],
        'original_capacity_gate_unchanged': original.get('capacity_gate', 'LEGACY_NOT_SEPARATED'),
        'original_recovery_gate_unchanged': original.get('bounded_recovery_business_gate', 'LEGACY_NOT_SEPARATED'),
        'metrics': result, 'overload_observation': overload_summary,
        'server_ingress': original['ingress'],
        'application_sql_and_pool': original['database_application'],
        'hot_fraction': original['hot_subject_fraction'],
        'hot_fraction_meaning': 'Read-subject concentration only; number of shared writer candidates '
                                'is always logical_agents/10, not hot_fraction*writers.',
        'source_snapshot': str(snapshot) if available else None,
        'source_snapshot_status': 'HASH_VERIFIED' if available else 'NOT_RETAINED_FOR_DEVELOPMENT_ATTEMPT',
        'derivation_source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'reproduction_command': f'python -m benchmarks.p2_load_derived --input {path} --output <new-path>',
        'limitations': ['Earlier planned counts are only as precise as originally recorded',
            'No rejected response is promoted to success by post-processing',
            'No performance re-run or model dispatch occurred during derivation']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error('derived output must not overwrite original evidence')
    result = derive(args.input)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({'source_snapshot_status': result['source_snapshot_status'],
                      'original_result': result['original_scenario_result_unchanged']}))


if __name__ == '__main__':
    main()
