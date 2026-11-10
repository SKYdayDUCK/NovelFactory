"""Compare complete replay against fixed e0b0f2b on one real stored input.

Both arms use the same PostgreSQL genesis, exact selected event order and
current common Schema/temporal helpers. Only the reducer implementation differs.
Complete snapshots and source evidence must match; setup and CPU profiles are
separate from paired timing. This proves a bounded computation improvement, not
an HTTP capacity, production SLO, or literary-quality result.
"""
from __future__ import annotations

import argparse
import cProfile
import importlib.util
import json
import os
import pstats
import statistics
import subprocess
import sys
import tempfile
import time
from hashlib import sha256
from pathlib import Path

from sqlalchemy import text

from benchmarks.context_paired_profile import world_identity
from benchmarks.p2_behavioral_load import hardware, make_population
from novel_factory.application.kernel import Kernel
from novel_factory.domain import world as candidate
from novel_factory.domain.canonical import fingerprint
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore

ROOT = Path(__file__).resolve().parents[1]
BASELINE = 'e0b0f2b080f3225d8be8e202e13258bc480f5b84'
SOURCE = 'src/novel_factory/domain/world.py'


def baseline_module(directory):
    content = subprocess.check_output(['git', 'show', f'{BASELINE}:{SOURCE}'], cwd=ROOT)
    path = Path(directory) / 'genesis_baseline_e0.py'
    path.write_bytes(content)
    name = 'novel_factory.domain._genesis_baseline_e0'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module, sha256(content).hexdigest()


def measure(callback, *, profile=False):
    profiler = cProfile.Profile() if profile else None
    started, cpu_started = time.perf_counter(), time.process_time()
    if profiler:
        profiler.enable()
    try:
        result = callback()
    finally:
        if profiler:
            profiler.disable()
    row = {'wall_seconds': time.perf_counter() - started,
           'process_cpu_seconds': time.process_time() - cpu_started}
    if profiler:
        row['profile'] = [{'file': Path(key[0]).name, 'line': key[1], 'function': key[2],
                           'calls': value[1], 'self_seconds': value[2], 'cumulative_seconds': value[3]}
                          for key, value in sorted(pstats.Stats(profiler).stats.items(),
                                                   key=lambda item: item[1][3], reverse=True)[:60]]
    return result, row


def run(output, *, agents=200, repeats=5):
    result = {'kind': 'REAL_POSTGRESQL_SAME_INPUT_PAIRED_REPLAY', 'status': 'INCOMPLETE',
              'baseline_commit': BASELINE, 'agents': agents, 'repeats': repeats,
              'pairs': [], 'profiles': [], 'real_model_calls': 0,
              'limitation': 'Sequential pure replay from one stored synthetic world; '
                            'warm buffer/cache pairs and a separate cold observation. '
                            'No HTTP capacity or latency SLO gate is inferred.'}
    db = Database(pool_size=4, max_overflow=0)
    try:
        with tempfile.TemporaryDirectory(prefix='nf-genesis-paired-') as directory:
            baseline, baseline_sha = baseline_module(directory)
            result['source_sha256'] = {'baseline_world.py': baseline_sha,
                **{path: sha256((ROOT / path).read_bytes()).hexdigest() for path in (
                    SOURCE, 'src/novel_factory/domain/temporal.py',
                    'src/novel_factory/domain/canonical.py',
                    'src/novel_factory/contracts/registry.py',
                    'src/novel_factory/contracts/_schema_bundle.py',
                    'benchmarks/genesis_paired_profile.py')}}
            result['environment'] = hardware()
            result['environment']['cpu_affinity'] = sorted(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else None
            with db.engine.connect() as conn:
                result['postgres_version'] = conn.scalar(text('SELECT version()'))
                assert int(conn.scalar(text('SHOW server_version_num'))) // 10000 == 17
            kernel = Kernel(db, ObjectStore(Path(directory) / 'objects'))
            fixture = make_population(kernel, agents, .8)
            world = fixture['world']
            result['world_before'] = world_identity(kernel, world)
            with db.uow(write=False, repeatable_read=True) as repo:
                revision = kernel.revision(repo, world['world_ref'])
                genesis = repo.one('genesis', {'work_id': world['world_ref']['work_id']})['payload']
                rows = repo.many('events', {'work_id': world['world_ref']['work_id']})
            by_id = {row['event_version_id']: row['payload'] for row in rows}
            # JSONB object order is not a selected event sequence. Recover the
            # sequence used by this already-stored projection explicitly.
            event_ids = [item['event_ref']['event_version_id'] for item in revision['snapshot']['events']]
            assert len(event_ids) == len(set(event_ids)) == len(revision['selection'])
            assert set(event_ids) == set(revision['selection'].values())
            events = [by_id[event_id] for event_id in event_ids]
            result['input'] = {'world_ref': world['world_ref'], 'genesis': genesis, 'events': events,
                               'genesis_sha256': fingerprint(genesis), 'events_sha256': fingerprint(events)}
            result['reference_snapshot'] = revision['snapshot']
            result['fixture'] = {'entities': fixture['genesis_entities'], 'relations': fixture['genesis_relations'],
                                  'selected_events': len(events)}
            # Cache lifetime is local to this measurement process. Cold and warm
            # values are reported separately, never averaged together.
            candidate._initial_snapshot_memo = candidate._InitialSnapshotMemo()
            cold, cold_record = measure(lambda: candidate.replay(genesis, events))
            result['cold_candidate'] = cold_record
            assert cold == revision['snapshot'], 'Cold replay differs from stored full projection'
            for repeat in range(repeats):
                pair = {'repeat': repeat, 'order': ['baseline', 'candidate'] if repeat % 2 == 0 else ['candidate', 'baseline']}
                outputs = {}
                for arm in pair['order']:
                    reducer = baseline if arm == 'baseline' else candidate
                    outputs[arm], pair[arm] = measure(lambda reducer=reducer: reducer.replay(genesis, events))
                    pair[arm]['snapshot_sha256'] = fingerprint(outputs[arm])
                pair['complete_snapshot_equal'] = outputs['baseline'] == outputs['candidate'] == revision['snapshot']
                result['pairs'].append(pair)
                assert pair['complete_snapshot_equal'], 'Paired full snapshots or evidence differ'
            for arm, reducer in [('baseline', baseline), ('candidate', candidate)]:
                snapshot, record = measure(lambda reducer=reducer: reducer.replay(genesis, events), profile=True)
                record.update(arm=arm, complete_snapshot_equal=snapshot == revision['snapshot'])
                result['profiles'].append(record)
                assert record['complete_snapshot_equal']
            result['world_after'] = world_identity(kernel, world)
            assert result['world_before'] == result['world_after'], 'Measurement changed the authoritative world'
            result['summary'] = {arm: {
                'median_wall_seconds': statistics.median(pair[arm]['wall_seconds'] for pair in result['pairs']),
                'median_cpu_seconds': statistics.median(pair[arm]['process_cpu_seconds'] for pair in result['pairs'])}
                for arm in ['baseline', 'candidate']}
            result['memo'] = {'entries': len(candidate._initial_snapshot_memo._entries),
                               'payload_bytes': candidate._initial_snapshot_memo._bytes,
                               'entry_limit': candidate._initial_snapshot_memo.max_entries,
                               'payload_byte_limit': candidate._initial_snapshot_memo.max_bytes}
            result['status'] = 'SEMANTICS_AND_EVIDENCE_PASS'
    finally:
        db.close()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: result[key] for key in ['status', 'summary', 'fixture', 'memo']}, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agents', type=int, default=200)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 10 <= args.agents <= 1000 or not 1 <= args.repeats <= 30:
        parser.error('agents must be 10..1000 and repeats 1..30')
    run(args.output, agents=args.agents, repeats=args.repeats)


if __name__ == '__main__':
    main()
