"""Measured bounded open-arrival stability probe for one persistent service.

Uses real persisted logical Agents and fixed historical query inputs. The same
API workers stay alive throughout the probe. This read-only soak complements
(the mixed write/conflict load and process-death receipt test); it does not
claim multi-hour durability, real Host interoperability or model concurrency.
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


async def steady(services, fixture, *, seconds, rate, max_inflight=256):
    population = fixture['population']
    records, samples, active = [], [], set()
    started = time.monotonic()
    scheduled_count = int(seconds * rate)
    async with httpx.AsyncClient(base_url=services.base_url, timeout=30, trust_env=False,
            limits=httpx.Limits(max_connections=max_inflight,
                               max_keepalive_connections=max_inflight)) as client:
        async def one(index, scheduled):
            actor = population[index % len(population)]
            kind = 'history' if index % 20 == 0 else 'graph' if index % 20 == 1 else 'state'
            before = time.monotonic()
            record = {'index': index, 'agent': actor['index'], 'kind': kind,
                'started_seconds': before - started, 'arrival_delay_ms': max(0, before-scheduled)*1000,
                'valid': False, 'status': 'client_error'}
            try:
                response = await client.post('/api/v1/queries/timeline', json=actor[kind],
                    headers={'Authorization': 'Bearer ' + actor['token']})
                body = response.json()
                record['status'] = response.status_code
                record['code'] = body.get('code')
                validate_contract('UnifiedQueryResult' if response.status_code == 200 else 'ErrorEnvelope',
                                  body, '2.1' if response.status_code == 200 else '2.0')
                if response.status_code == 200:
                    assert body['read_cut']['world_ref'] == fixture['world']['world_ref']
                    if kind == 'state':
                        expected = {'kind': 'ENTITY', 'entity_id': fixture['ids']['smith']}
                        assert len(body['payload']['state_items']) == 2
                        assert all(row['answer']['data'] == expected for row in body['payload']['state_items'])
                    record['valid'] = True
                elif response.status_code not in (429, 503):
                    record['correctness_error'] = 'unexpected_status'
            except httpx.TransportError as error:
                record.update(status='transport_error', error_type=type(error).__name__)
            except Exception as error:  # noqa: BLE001 - retained failure evidence
                record['correctness_error'] = type(error).__name__
            record['latency_ms'] = (time.monotonic() - before) * 1000
            records.append(record)

        next_sample = started
        for index in range(scheduled_count):
            scheduled = started + index/rate
            await asyncio.sleep(max(0, scheduled-time.monotonic()))
            if time.monotonic() >= next_sample:
                snapshots = await asyncio.to_thread(services.snapshot, max(1, time.monotonic()-started))
                samples.append({'seconds': time.monotonic()-started, **snapshots})
                next_sample += 10
            if len(active) >= max_inflight:
                records.append({'index': index, 'agent': index % len(population),
                    'status': 'client_drop', 'valid': False, 'latency_ms': None,
                    'arrival_delay_ms': (time.monotonic()-scheduled)*1000})
                continue
            task = asyncio.create_task(one(index, scheduled))
            active.add(task)
            task.add_done_callback(active.discard)
        if active:
            await asyncio.gather(*active)
    duration = time.monotonic()-started
    return {'records': records, 'samples': samples, 'duration_seconds': duration,
            'scheduled_count': scheduled_count}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agents', type=int, choices=[10, 50, 200, 500, 1000], required=True)
    parser.add_argument('--seconds', type=float, default=600)
    parser.add_argument('--read-rps', type=float, default=20)
    parser.add_argument('--api-processes', type=int, choices=[1, 2, 4], default=2)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 10 <= args.seconds <= 86400 or not 0 < args.read_rps <= 2000:
        parser.error('bounded stability duration/rate invalid')
    url = os.environ.get('NF_TEST_DATABASE_URL')
    if not url:
        parser.error('NF_TEST_DATABASE_URL is required')
    with (tempfile.TemporaryDirectory(prefix='nf-p3-soak-') as objects,
          isolated_kernel(url, Path(objects)) as (kernel, target)):
        fixture = make_population(kernel, args.agents, .8)
        kernel.db.engine.dispose()
        with ServiceProcesses(target, objects, processes=args.api_processes) as services:
            with DatabaseObserver(target) as observer:
                result = asyncio.run(steady(services, fixture, seconds=args.seconds, rate=args.read_rps))
            final = services.snapshot(result['duration_seconds'])
        with kernel.db.uow(write=False) as repo:
            head = kernel.head(repo, fixture['world']['world_ref']['work_id'])
        assert head == fixture['world']['world_ref'], 'Read-only soak changed authoritative history'
    rows = result['records']
    successful = [r for r in rows if r['valid']]
    attempts = [r for r in rows if r['status'] != 'client_drop']
    errors = [r for r in rows if r.get('correctness_error')]
    span = max((r['started_seconds'] for r in attempts), default=0) - min((r['started_seconds'] for r in attempts), default=0)
    offered = (len(attempts)-1)/span if span else 0
    measured_pass = (not errors and len(successful) == result['scheduled_count']
                     and offered >= .95 * args.read_rps
                     and final['database']['checked_out_at_end'] == 0)
    result.update(evidence_kind='REAL_HTTP_POSTGRESQL_READ_ONLY_STABILITY_PROBE',
        environment={**hardware(), 'api_processes': args.api_processes, 'aggregate_api_pool': 8,
            'load_client_location': 'separate API processes, shared host'},
        configuration={**vars(args), 'output': str(args.output)},
        schema_version='p3-stability-1', source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        actual_call_start_rps=offered, successful_query_rps=len(successful)/result['duration_seconds'],
        latency_all_attempts=percentiles([r['latency_ms']/1000 for r in attempts]),
        status_counts=dict(Counter(str(r['status']) for r in rows)), correctness_errors=errors,
        world_unchanged=True, new_world_commits=0, real_model_calls=0,
        probe_gate='PASS' if measured_pass else 'NOT_MET',
        long_duration_gate='NOT_EXECUTED' if args.seconds < 3600 else ('PARTIAL_READ_ONLY' if measured_pass else 'NOT_MET'),
        database_observer=observer.result(), service_final=final,
        limitations=['Synthetic small world; fixed historical reads, no model requests',
            'Process crash/receipt recovery is a separate integration test',
            'No claim of hosted Agent products being integrated',
            'One-hour minimum long-duration label is an explicit probe convention, not a production SLO'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({key: result[key] for key in ('probe_gate','long_duration_gate',
        'duration_seconds','actual_call_start_rps','successful_query_rps','status_counts')}, indent=2))
    return int(not measured_pass)


if __name__ == '__main__':
    raise SystemExit(main())
