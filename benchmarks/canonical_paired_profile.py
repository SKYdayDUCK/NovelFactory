"""Same-world hot-query comparison of strict NF-CANON-1 checking.

Only canonical._check is replaced for the baseline arm, using exact source from
0a0af490. Authentication, SQL, response cache, Schema validation, task fences and
delivery checks remain current and identical in both arms. This sequential
fixed-input experiment does not establish an HTTP capacity or production SLO.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID

from benchmarks.context_paired_profile import world_identity
from benchmarks.p2_behavioral_load import hardware, make_population
from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain import canonical
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore

ROOT = Path(__file__).resolve().parents[1]
BASELINE = '0a0af490c7768fe4b7d2da16dbaa74f0d9585f0d'
SOURCE = 'src/novel_factory/domain/canonical.py'
BASELINE_SOURCE_SHA256 = '3fc335ad00e3973b200683e953e5066a64ce6f0040035df000c3dfc350fa1571'


def baseline_module(directory):
    content = subprocess.check_output(['git', 'show', f'{BASELINE}:{SOURCE}'], cwd=ROOT)
    assert sha256(content).hexdigest() == BASELINE_SOURCE_SHA256
    path = Path(directory) / 'canonical_baseline.py'
    path.write_bytes(content)
    name = 'novel_factory.domain._canonical_paired_baseline'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@contextmanager
def checker(function):
    old = canonical._check
    canonical._check = function
    try:
        yield
    finally:
        canonical._check = old


def stable_response(response):
    """Keep every fact/guard/cut; validate the three generated per-call fields."""
    validate_contract('UnifiedQueryResult', response, '2.1')
    assert UUID(response['request_id']).version == 4
    assert UUID(response['read_cut']['read_cut_id']).version == 4
    assert datetime.fromisoformat(response['read_cut']['resolved_at']).utcoffset() is not None
    result = deepcopy(response)
    del result['request_id']
    del result['read_cut']['read_cut_id']
    del result['read_cut']['resolved_at']
    return result


def run(output, *, agents=200, repeats=8):
    result = {'kind': 'REAL_POSTGRESQL_SAME_QUERY_PAIRED_CANONICAL_CHECK', 'status': 'INCOMPLETE',
              'baseline_commit': BASELINE, 'agents': agents, 'repeats': repeats, 'pairs': [],
              'volatile_response_fields': ['request_id', 'read_cut.read_cut_id', 'read_cut.resolved_at'],
              'real_model_calls': 0,
              'limitation': 'Sequential warm application queries in one generated world; '
                            'no TCP/admission contention, no capacity or production SLO conclusion.'}
    db = Database(pool_size=4, max_overflow=0)
    try:
        with tempfile.TemporaryDirectory(prefix='nf-canonical-paired-') as directory:
            baseline = baseline_module(directory)
            candidate_check = canonical._check
            result['source_sha256'] = {'baseline_canonical.py': BASELINE_SOURCE_SHA256,
                **{path: sha256((ROOT / path).read_bytes()).hexdigest() for path in (
                    SOURCE, 'src/novel_factory/application/kernel.py',
                    'src/novel_factory/application/queries.py',
                    'src/novel_factory/contracts/registry.py',
                    'benchmarks/canonical_paired_profile.py')}}
            result['environment'] = hardware()
            result['environment'].update(load_client_location='direct application calls; no HTTP transport',
                                         api_processes=0, api_server_threads=0)
            result['environment']['cpu_affinity'] = sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None
            kernel = Kernel(db, ObjectStore(Path(directory) / 'objects'))
            fixture = make_population(kernel, agents, .8)
            actors = [actor for actor in fixture['population'] if not actor['writer']][:5]
            service = WorldQueryService(kernel)
            result['world_before'] = world_identity(kernel, fixture['world'])
            reference = [stable_response(service.query(actor['principal'], actor['state'])) for actor in actors]
            result['queries'] = [actor['state'] for actor in actors]
            result['reference_responses'] = reference
            result['fixture'] = {'entities': fixture['genesis_entities'], 'relations': fixture['genesis_relations'],
                                  'read_actors': len(actors), 'queries_per_arm_per_pair': len(actors) * 2}
            for repeat in range(repeats):
                pair = {'repeat': repeat, 'order': ['baseline', 'candidate'] if repeat % 2 == 0 else ['candidate', 'baseline']}
                for arm in pair['order']:
                    start, cpu_start = time.perf_counter(), time.process_time()
                    with checker(baseline._check if arm == 'baseline' else candidate_check):
                        responses = [service.query(actor['principal'], actor['state']) for _ in range(2) for actor in actors]
                    wall, cpu = time.perf_counter() - start, time.process_time() - cpu_start
                    normalized = [stable_response(response) for response in responses]
                    pair[arm] = {'wall_seconds_per_query': wall / len(responses),
                                 'cpu_seconds_per_query': cpu / len(responses),
                                 'query_count': len(responses),
                                 'full_payload_evidence_and_cut_equal': normalized == reference * 2,
                                 'response_sha256': [canonical.fingerprint(response) for response in normalized]}
                    assert pair[arm]['full_payload_evidence_and_cut_equal'], 'Complete response material differs'
                result['pairs'].append(pair)
            result['world_after'] = world_identity(kernel, fixture['world'])
            assert result['world_before'] == result['world_after'], 'Read comparison changed the authoritative world'
            result['summary'] = {arm: {metric: statistics.median(pair[arm][metric] for pair in result['pairs'])
                                       for metric in ['wall_seconds_per_query', 'cpu_seconds_per_query']}
                                 for arm in ['baseline', 'candidate']}
            result['status'] = 'SEMANTICS_AND_EVIDENCE_PASS'
    finally:
        db.close()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: result[key] for key in ['status', 'summary', 'fixture']}, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agents', type=int, default=200)
    parser.add_argument('--repeats', type=int, default=8)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 10 <= args.agents <= 1000 or not 1 <= args.repeats <= 30:
        parser.error('agents must be 10..1000 and repeats 1..30')
    run(args.output, agents=args.agents, repeats=args.repeats)


if __name__ == '__main__':
    main()
