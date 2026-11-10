"""Independent open-arrival probe with HTTP wire starts and deferred validation.

Only the read-only overload probe defers expensive schema validation until all
network requests complete. Every response is still fully validated before it
can count as a successful call. Client validation time is reported separately.
The original P2 mixed/retry/business gates are unchanged. No tokens are written.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import tempfile
import time
from collections import Counter
from pathlib import Path

import httpx

from benchmarks.http_load import percentiles
from benchmarks.p2_behavioral_load import (
    DatabaseObserver,
    hardware,
    isolated_kernel,
    make_population,
)
from benchmarks.process_server import ServiceProcesses
from novel_factory.contracts.registry import validate_contract


def rate_from_times(values):
    values = sorted(values)
    if len(values) < 2 or values[-1] == values[0]:
        return None
    return (len(values)-1)/(values[-1]-values[0])


async def offer(base_url, fixture, *, target_rps, rounds, client_connections=256):
    population = fixture['population']
    encoded = [json.dumps(client['state'], separators=(',', ':'), ensure_ascii=False).encode()
               for client in population]
    records = []
    started = time.monotonic()
    async with httpx.AsyncClient(base_url=base_url, timeout=30, trust_env=False,
            limits=httpx.Limits(max_connections=client_connections,
                               max_keepalive_connections=client_connections)) as http:
        async def one(index, scheduled):
            position = index % len(population)
            client = population[position]
            before = time.monotonic()
            row = {'index': index, 'agent': client['index'], 'status': 'client_error',
                'scheduled_seconds': scheduled-started, 'dispatch_seconds': before-started,
                'arrival_delay_ms': max(0, before-scheduled)*1000, 'valid': False}
            async def trace(event, info):
                if event == 'http11.send_request_headers.started':
                    row.setdefault('wire_start_seconds', time.monotonic()-started)
            try:
                response = await http.post('/api/v1/queries/timeline', content=encoded[position],
                    headers={'Authorization': 'Bearer '+client['token'], 'Content-Type': 'application/json'},
                    extensions={'trace': trace})
                row.update(status=response.status_code, response_bytes=len(response.content),
                           _content=response.content)
            except httpx.TransportError as error:
                row.update(status='transport_error', error_type=type(error).__name__)
            except Exception as error:  # noqa: BLE001 - retain unexpected failures
                row['correctness_error'] = type(error).__name__
            row['completed_seconds'] = time.monotonic()-started
            row['latency_ms'] = (time.monotonic()-before)*1000
            records.append(row)
        pending = []
        for index in range(len(population)*rounds):
            scheduled = started + index/target_rps
            await asyncio.sleep(max(0, scheduled-time.monotonic()))
            pending.append(asyncio.create_task(one(index, scheduled)))
        await asyncio.gather(*pending)
    return records, time.monotonic()-started


def validate_responses(rows, fixture):
    """Only validated responses may enter successful throughput; retain errors."""
    started = time.monotonic()
    for row in rows:
        content = row.pop('_content', None)
        if content is None:
            continue
        row['response_sha256'] = hashlib.sha256(content).hexdigest()
        try:
            body = json.loads(content)
            row['code'] = body.get('code')
            if row['status'] == 200:
                validate_contract('UnifiedQueryResult', body, '2.1')
                assert body['read_cut']['world_ref'] == fixture['world']['world_ref']
                items = body['payload']['state_items']
                expected = {'kind': 'ENTITY', 'entity_id': fixture['ids']['smith']}
                assert len(items) == 2 and all(item['answer']['data'] == expected for item in items)
                row['valid'] = True
            else:
                validate_contract('ErrorEnvelope', body)
                if row['status'] not in {429, 503}:
                    row['correctness_error'] = 'unexpected_status'
        except Exception as error:  # noqa: BLE001 - keep invalid success as failure
            row['correctness_error'] = type(error).__name__
    return time.monotonic()-started


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agents', type=int, choices=[10, 50, 200, 500, 1000], required=True)
    parser.add_argument('--rounds', type=int, default=5)
    parser.add_argument('--arrival-rps', type=float, default=1000)
    parser.add_argument('--hot-fraction', type=float, default=.8)
    parser.add_argument('--api-processes', type=int, choices=[1, 2, 4], default=2)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.rounds <= 30 or not 0 < args.arrival_rps <= 5000 or not 0 <= args.hot_fraction <= 1:
        parser.error('invalid bounded load parameters')
    url = os.environ.get('NF_TEST_DATABASE_URL')
    if not url:
        parser.error('NF_TEST_DATABASE_URL is required')
    with (tempfile.TemporaryDirectory(prefix='nf-p3-open-') as objects,
          isolated_kernel(url, Path(objects)) as (kernel, target)):
        fixture = make_population(kernel, args.agents, args.hot_fraction)
        kernel.db.engine.dispose()
        with ServiceProcesses(target, objects, processes=args.api_processes) as services:
            with DatabaseObserver(target) as observer:
                rows, duration = asyncio.run(offer(services.base_url, fixture,
                    target_rps=args.arrival_rps, rounds=args.rounds))
            metrics = services.snapshot(duration)
        validation_seconds = validate_responses(rows, fixture)
        with kernel.db.uow(write=False) as repo:
            head = kernel.head(repo, fixture['world']['world_ref']['work_id'])
        assert head == fixture['world']['world_ref'], 'Read overload changed authoritative history'
    dispatch_rate = rate_from_times([row['dispatch_seconds'] for row in rows])
    wire_rate = rate_from_times([row['wire_start_seconds'] for row in rows if 'wire_start_seconds' in row])
    successful = sum(row['valid'] for row in rows)
    errors = [row for row in rows if row.get('correctness_error')]
    complete = len(rows) == args.agents*args.rounds
    wire_complete = sum('wire_start_seconds' in row for row in rows) == len(rows)
    met = complete and wire_complete and wire_rate is not None and wire_rate >= args.arrival_rps*.95
    result = {'schema_version': 'p3-open-arrival-1', 'evidence_kind': 'REAL_HTTP_POSTGRESQL_OPEN_ARRIVAL',
        'environment': {**hardware(), 'api_processes': args.api_processes,
            'load_client_location': 'separate spawned service processes; read validation after network window'},
        'configuration': {**vars(args), 'output': str(args.output)},
        'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'network_window_seconds': duration, 'client_validation_seconds': validation_seconds,
        'scheduled_requests': args.agents*args.rounds, 'completed_requests': len(rows),
        'dispatch_start_rps': dispatch_rate, 'observed_wire_start_rps': wire_rate,
        'wire_started_requests': sum('wire_start_seconds' in row for row in rows),
        'client_connections_limit': 256, 'first_attempts_only': True,
        'validated_successes': successful, 'successful_query_rps': successful/duration,
        'latency_all_attempts': percentiles([row['latency_ms']/1000 for row in rows]),
        'scheduled_to_network_completion': percentiles([
            row['completed_seconds']-row['scheduled_seconds'] for row in rows]),
        'status_counts': dict(Counter(str(row['status']) for row in rows)),
        'offered_wire_rate_gate': 'PASS' if met else 'NOT_MET',
        'capacity_gate': 'PASS' if met and not errors and successful == len(rows) else 'NOT_MET',
        'correctness_errors': errors, 'world_unchanged': True, 'new_world_commits': 0,
        'service_metrics': metrics, 'database_observer': observer.result(), 'raw_requests': rows,
        'real_model_calls': 0,
        'limitations': ['Read-only overload; mixed business commits are measured by p2_behavioral_load',
            'Wire-start trace observes HTTP header send start, not packet capture or server acceptance',
            'Schema validation runs after network timing but every success requires it',
            'Shared host; short window does not establish sustained production SLO']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({key: result[key] for key in ['capacity_gate','offered_wire_rate_gate',
        'dispatch_start_rps','observed_wire_start_rps','successful_query_rps','status_counts']}, indent=2))
    return int(result['capacity_gate'] != 'PASS')


if __name__ == '__main__':
    raise SystemExit(main())
