"""Paired P3 CPU/lineage experiment against the immutable 206eaa5 baseline.

Both paths use the same actual PostgreSQL WorldRef, facts, source evidence and
permission/guard material. Only the two changed pure-coordinate/lineage methods
are restored for the baseline arm. No baseline implementation is imported by
production code. Setup, task identity and observation SQL are outside timings.
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
from contextlib import ExitStack, contextmanager
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from benchmarks.context_paired_profile import capture_persisted_material, world_identity
from benchmarks.context_profile import SQLMeter, make_world, measure, prepare
from benchmarks.p2_behavioral_load import isolated_kernel
from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.domain import temporal
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.repository import KernelRepository

ROOT = Path(__file__).resolve().parents[1]
BASELINE = '206eaa5b655419d29bd6b15d618ae6eaf9048354'


def baseline_module(path, name, directory):
    content = subprocess.check_output(['git', 'show', f'{BASELINE}:{path}'], cwd=ROOT)
    target = Path(directory) / (name.rsplit('.', 1)[-1] + '.py')
    target.write_bytes(content)
    spec = importlib.util.spec_from_file_location(name, target)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module, sha256(content).hexdigest()


@contextmanager
def baseline_methods(point_function, ancestry_method):
    original_point = temporal.point_key
    originals = []
    # Imports bind functions by value. Restore every package import of this
    # one function while sequentially executing the baseline arm; no threads.
    for name, module in list(sys.modules.items()):
        if name.startswith('novel_factory.') and module is not None:
            for key, value in list(vars(module).items()):
                if value is original_point:
                    originals.append((module, key, value))
                    setattr(module, key, point_function)
    original_ancestry = KernelRepository.ancestor
    KernelRepository.ancestor = ancestry_method
    try:
        yield
    finally:
        for module, key, value in originals:
            setattr(module, key, value)
        KernelRepository.ancestor = original_ancestry


@contextmanager
def paired_inputs(directory, entities, repeats):
    # Freeze each request once. Task generation and the resulting observation
    # handle belong to the input. A PostgreSQL clone lets both arms run normal
    # service transactions, including authorization, binding and real commits.
    objects = Path(directory) / 'objects'
    with isolated_kernel(os.environ['NF_DATABASE_URL'], objects) as (seed, url), ExitStack() as stack:
        world, items, _ = make_world(seed, entities)
        requests = {(count, repeat): prepare(seed, world, items[:count])
                    for count in (1, 20, 100) for repeat in range(repeats + 1)}
        seed.db.close()  # PostgreSQL requires our private template to be idle.
        source = make_url(url)
        clone = 'nf_p3_pair_' + uuid4().hex
        admin = create_engine(source.set(database='postgres'), isolation_level='AUTOCOMMIT')
        stack.callback(admin.dispose)
        connection = stack.enter_context(admin.connect())
        template = connection.dialect.identifier_preparer.quote_identifier(source.database)
        connection.exec_driver_sql(f'CREATE DATABASE "{clone}" TEMPLATE {template}')
        stack.callback(connection.exec_driver_sql, f'DROP DATABASE "{clone}" WITH (FORCE)')
        arms = {}
        for arm, name in [('baseline', source.database), ('candidate', clone)]:
            db = Database(source.set(database=name).render_as_string(hide_password=False), pool_size=4)
            stack.callback(db.close)
            arms[arm] = (Kernel(db, seed.objects), SQLMeter(db.engine))
        yield arms, world, requests


def run(output, *, entities=300, repeats=5):
    records, pairs = [], []
    with tempfile.TemporaryDirectory(prefix='nf-p3-paired-') as directory:
        old_temporal, temporal_hash = baseline_module('src/novel_factory/domain/temporal.py',
            'novel_factory.domain._p3_baseline_temporal', directory)
        old_repo, repo_hash = baseline_module('src/novel_factory/infrastructure/repository.py',
            'novel_factory.infrastructure._p3_baseline_repository', directory)
        with paired_inputs(directory, entities, repeats) as (arms, world, requests):
            identity_before = world_identity(arms['baseline'][0], world)
            assert world_identity(arms['candidate'][0], world) == identity_before
            for count in (1, 20, 100):
                for repeat in range(repeats):
                    request = requests[count, repeat]
                    materials = {}
                    # Alternate arm order to avoid consistently warming one arm.
                    for arm in (('baseline', 'candidate') if repeat % 2 == 0 else ('candidate', 'baseline')):
                        kernel, meter = arms[arm]
                        service = WorldQueryService(kernel)
                        callback = lambda service=service, request=request: service.context_compile(world['principal'], request)
                        if arm == 'baseline':
                            with baseline_methods(old_temporal.point_key, old_repo.KernelRepository.ancestor):
                                compiled, record = measure(meter, arm, callback)
                        else:
                            compiled, record = measure(meter, arm, callback)
                        record.update(subjects=count, repeat=repeat)
                        records.append(record)
                        materials[arm] = capture_persisted_material(kernel, world, request, compiled)
                    assert materials['baseline']['task_id'] == materials['candidate']['task_id'] == request['task_id']
                    assert materials['baseline']['context_bundle_id'] != materials['candidate']['context_bundle_id']
                    for key in ('full_facts', 'full_fact_sha256', 'guard_material',
                                'guard_material_sha256', 'scope_row_count'):
                        assert materials['baseline'][key] == materials['candidate'][key], key
                    pairs.append({'subjects': count, 'repeat': repeat, 'complete_material_equal': True,
                                  'baseline': materials['baseline'], 'candidate': materials['candidate']})
                # Profiling is a separate observation, never a paired latency sample.
                for arm in ('baseline', 'candidate'):
                    kernel, meter = arms[arm]
                    request = requests[count, repeats]
                    callback = lambda request=request: WorldQueryService(kernel).context_compile(world['principal'], request)
                    if arm == 'baseline':
                        with baseline_methods(old_temporal.point_key, old_repo.KernelRepository.ancestor):
                            _, record = measure(meter, 'profile-' + arm, callback, profile=True)
                    else:
                        _, record = measure(meter, 'profile-' + arm, callback, profile=True)
                    record['subjects'] = count
                    records.append(record)
            identity_after = world_identity(arms['baseline'][0], world)
            assert identity_before == identity_after == world_identity(arms['candidate'][0], world)
            summary = []
            for count in (1, 20, 100):
                row = {'subjects': count}
                for arm in ('baseline', 'candidate'):
                    values = [r for r in records if r['subjects'] == count and r['operation'] == arm]
                    row[arm] = {'median_seconds': statistics.median(r['elapsed_seconds'] for r in values),
                        'sql_counts': sorted({r['sql']['statements'] for r in values}),
                        'median_cursor_execution_seconds': statistics.median(r['sql']['cursor_execution_seconds'] for r in values)}
                summary.append(row)
            result = {'evidence_kind': 'REAL_POSTGRESQL_SAME_WORLD_PAIRED_ENGINEERING',
                'baseline_commit': BASELINE, 'baseline_source_hashes': {'temporal.py': temporal_hash,
                    'repository.py': repo_hash},
                'candidate_source_hashes': {path: sha256((ROOT/path).read_bytes()).hexdigest() for path in (
                    'src/novel_factory/domain/temporal.py', 'src/novel_factory/infrastructure/repository.py')},
                'environment': {'python': sys.version, 'cpus': os.cpu_count(), 'shared_host': True},
                'entities': entities, 'repeats': repeats, 'summary': summary, 'records': records,
                'input_isolation': 'Same frozen task and generation in independently committed PostgreSQL clones',
                'paired_material': pairs, 'world_before': identity_before, 'world_after': identity_after,
                'real_model_calls': 0,
                'limitation': 'Small deterministic world, warm PostgreSQL/OS buffers; not a capacity or literary quality claim'}
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
            print(json.dumps({'paired_equal': len(pairs), 'summary': summary}, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--entities', type=int, default=300)
    parser.add_argument('--repeats', type=int, default=5)
    args = parser.parse_args()
    if not 100 <= args.entities <= 10000 or not 1 <= args.repeats <= 30:
        parser.error('invalid bounded fixture size')
    run(args.output, entities=args.entities, repeats=args.repeats)


if __name__ == '__main__':
    main()
