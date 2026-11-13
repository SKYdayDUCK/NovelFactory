"""Capture real frozen-load responses, then replay the timed P2 success checks offline."""
import argparse
import asyncio
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

import httpx

from benchmarks.p2_behavioral_load import SOURCE_SHA256, isolated_kernel, make_population
from benchmarks.process_server import ServiceProcesses
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint


async def capture(base, fixture):
    bodies = []
    # Acquisition is outside all reported CPU measurements. All 200 real task
    # grants must succeed, so capture at low concurrency without overload.
    async with httpx.AsyncClient(base_url=base, timeout=30, trust_env=False) as client:
        for actor in fixture['population']:
            response = await client.post('/api/v1/queries/timeline', json=actor['state'],
                                         headers={'Authorization': 'Bearer ' + actor['token']})
            response.raise_for_status()
            bodies.append(response.content.decode())
    return bodies


def verify(body_bytes, world_ref, smith):
    # Mirrors the synchronous success path of send_once in p2_behavioral_load.
    body = json.loads(body_bytes)
    fingerprint(body)
    validate_contract('UnifiedQueryResult', body, '2.1')
    if body['read_cut']['world_ref'] != world_ref:
        raise AssertionError('fixed revision mismatch')
    items = body['payload']['state_items']
    if len(items) != 2 or any(item['answer']['data'] != {'kind': 'ENTITY', 'entity_id': smith}
                              for item in items):
        raise AssertionError('incorrect state')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not args.corpus.exists():
        with tempfile.TemporaryDirectory() as objects, isolated_kernel(os.environ['NF_TEST_DATABASE_URL'],
                Path(objects)) as (kernel, url):
            fixture = make_population(kernel, 200, .8)
            kernel.db.engine.dispose()
            with ServiceProcesses(url, objects, processes=1) as service:
                bodies = asyncio.run(capture(service.base_url, fixture))
            corpus = {'world_ref': fixture['world']['world_ref'], 'smith': fixture['ids']['smith'],
                      'results': bodies, 'unique_grants': len({a['principal'].grant_id for a in fixture['population']}),
                      'p2_sha256': SOURCE_SHA256}
        with gzip.open(args.corpus, 'wt') as stream:
            json.dump(corpus, stream, separators=(',', ':'))
    with gzip.open(args.corpus, 'rt') as stream:
        corpus = json.load(stream)
    bodies = [body.encode() for body in corpus['results']]
    assert len(bodies) == corpus['unique_grants'] == 200
    assert len({json.loads(body)['request_id'] for body in bodies}) == 200
    assert len({json.loads(body)['dependency_handle'] for body in bodies}) == 200
    assert os.environ.get('NF_HERA_STATIC_CONTRACTS', '0') == '0', 'requires original client validator'
    for body in bodies:
        verify(body, corpus['world_ref'], corpus['smith'])
    repeats = []
    for _ in range(4):
        started = time.thread_time()
        for _ in range(5):
            for body in bodies:
                verify(body, corpus['world_ref'], corpus['smith'])
        repeats.append(time.thread_time() - started)
    result = {'client_cpu_seconds_per_1000': repeats, 'logical_grants': 200,
              'socket_or_database_in_timed_region': False,
              'body_sizes': sorted(set(map(len, bodies))),
              'input_sha256': hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
              'p2_driver_sha256': SOURCE_SHA256, 'cpus': sorted(os.sched_getaffinity(0))}
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(result, flush=True)


if __name__ == '__main__':
    main()
