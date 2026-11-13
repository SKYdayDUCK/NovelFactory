"""Derive first-attempt validated read goodput without changing the historical AT-30 gate."""
import argparse
import gzip
import json
from pathlib import Path


def completion(result):
    offered = result['logical_agent_task_count'] * result['load']['overload_rounds']
    rows = [row for row in result['raw_requests'] if row['label'] == 'overload' and row['attempt'] == 1]
    if len(rows) != offered or len({row['operation_id'] for row in rows}) != offered:
        raise ValueError('Missing or duplicated first attempts')
    first_scheduled = min(row['started_seconds'] - row['arrival_delay_ms'] / 1000 for row in rows)
    last_response = max(row['started_seconds'] + row['latency_ms'] / 1000 for row in rows)
    window = last_response - first_scheduled
    valid = sum(row['status'] == 200 and 'error_type' not in row for row in rows)
    goodput = valid / window
    return {'first_attempts': len(rows), 'validated_successes': valid, 'window_seconds': window,
            'validated_successes_per_second': goodput, 'historical_capacity_gate': result['capacity_gate'],
            'additional_completion_gate': 'PASS' if (goodput >= 950 and valid == offered and
                result['engineer_correctness'] == 'PASS') else 'NOT_MET'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('results', nargs='+', type=Path)
    for path in parser.parse_args().results:
        opener = gzip.open if path.suffix == '.gz' else open
        with opener(path, 'rt') as stream:
            print(json.dumps({'path': str(path), **completion(json.load(stream))}))
