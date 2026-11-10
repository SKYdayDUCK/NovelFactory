"""Profile the existing world-version path against real PostgreSQL 17.

This is a sequential attribution experiment, not an HTTP capacity test. Worlds,
tasks, contexts, proposals, certificates and accepted events use public Kernel
services. No event/projection rows are fabricated. Each read repeat pins exactly
one WorldRef; source hashes and all per-operation records accompany the result.

Run with scripts/with_postgres.py (no --migrate is necessary: this driver creates
and migrates its own disposable database). JSON codec bytes measure JSON values
delivered to/from psycopg, not network traffic or physical PostgreSQL I/O.
"""
from __future__ import annotations

import argparse
import cProfile
import importlib
import json
import os
import pstats
import re
import resource
import statistics
import tempfile
import time
import weakref
from collections import Counter, defaultdict
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from functools import wraps
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

from psycopg.types.json import set_json_dumps, set_json_loads
from sqlalchemy import event, text

from benchmarks.context_profile import make_world
from benchmarks.http_load import percentiles
from benchmarks.p2_behavioral_load import (
    DatabaseObserver,
    hardware,
    isolated_kernel,
    make_population,
)
from novel_factory.application.queries import WorldQueryService
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain import reducer
from novel_factory.domain import world as world_module
from novel_factory.domain.canonical import canonical_bytes, fingerprint
from novel_factory.fixtures import entity_value, point, run_command

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATHS = (
    'benchmarks/scion_profile.py', 'benchmarks/p2_behavioral_load.py',
    'benchmarks/context_profile.py', 'src/novel_factory/application/kernel.py',
    'src/novel_factory/application/queries.py', 'src/novel_factory/application/templates.py',
    'src/novel_factory/domain/world.py', 'src/novel_factory/domain/temporal.py',
    'src/novel_factory/domain/canonical.py', 'src/novel_factory/infrastructure/repository.py',
    'src/novel_factory/infrastructure/database.py',
)


def current_rss_kib():
    try:
        row = next(line for line in Path('/proc/self/status').read_text().splitlines()
                   if line.startswith('VmRSS:'))
        return int(row.split()[1])
    except (OSError, StopIteration, ValueError):
        return None


class JSONCodec:
    """One stable callable per codec/experiment; no per-checkout closures.

    Psycopg caches customized adapter classes by callable identity. The weak
    meter reference prevents that cache from retaining a completed experiment.
    """
    def __init__(self, meter, function, kind):
        self.meter = weakref.ref(meter)
        self.function, self.kind = function, kind

    def __call__(self, value):
        meter = self.meter()
        if meter is None or not meter.active:
            return self.function(value)
        start, cpu = time.perf_counter(), time.thread_time()
        result = self.function(value)
        elapsed, used = time.perf_counter() - start, time.thread_time() - cpu
        raw = value if self.kind == 'decode' else result
        size = len(raw.encode('utf-8')) if isinstance(raw, str) else len(raw)
        row = meter.json_stats[self.kind]
        row['calls'] += 1
        row['bytes'] += size
        row['wall_seconds'] += elapsed
        row['thread_cpu_seconds'] += used
        return result


class Attribution:
    """Scoped observations, with no SQL parameters or credentials retained.

    SQL cursor timing includes failed statements and excludes result decoding.
    Stage and transaction scopes are inclusive and must never be summed. Codec
    adapters are connection-local; the observer's connection stays unmeasured.
    """
    def __init__(self, kernel):
        self.kernel = kernel
        self.engine = kernel.db.engine
        self.active = False
        self.originals = []
        self.loads = JSONCodec(self, json.loads, 'decode')
        self.dumps = JSONCodec(self, json.dumps, 'encode')
        self.reset()

    def reset(self):
        self.sql = Counter()
        self.sql_tables = Counter()
        self.sql_seconds = self.sql_max_seconds = 0.0
        self.sql_errors = self.selected_rows = 0
        self.json_stats = defaultdict(lambda: {'calls': 0, 'bytes': 0,
                                             'wall_seconds': 0.0, 'thread_cpu_seconds': 0.0})
        self.stages = defaultdict(lambda: {'calls': 0, 'errors': 0, 'wall_seconds': 0.0,
                                         'thread_cpu_seconds': 0.0, 'max_wall_seconds': 0.0})
        self.replay_event_counts = []
        self.transactions = []

    def _connect(self, dbapi_connection, connection_record):
        # Register exactly once per physical connection. Repeating
        # set_json_dumps on checkout subclasses the already customized dumper,
        # even when the same stable callable is reused, and distorts timings.
        set_json_loads(self.loads, dbapi_connection)
        set_json_dumps(self.dumps, dbapi_connection)

    def _before(self, conn, cursor, statement, parameters, context, executemany):
        context.nf_scion_profile_started = time.perf_counter() if self.active else None
        if self.active:
            self.sql[statement.split()[0].upper()] += 1
            self.sql_tables.update(set(re.findall(r'\bnf_[a-z_]+\b', statement)))

    def _finish(self, context, cursor=None, failed=False):
        started = getattr(context, 'nf_scion_profile_started', None)
        if started is None:
            return
        context.nf_scion_profile_started = None
        elapsed = time.perf_counter() - started
        self.sql_seconds += elapsed
        self.sql_max_seconds = max(self.sql_max_seconds, elapsed)
        self.sql_errors += int(failed)
        if cursor is not None and cursor.description is not None:
            self.selected_rows += max(0, cursor.rowcount)

    def _after(self, conn, cursor, statement, parameters, context, executemany):
        self._finish(context, cursor)

    def _error(self, exception_context):
        self._finish(exception_context.execution_context, failed=True)

    def _wrap(self, function, name):
        @wraps(function)
        def measured(*args, **kwargs):
            if not self.active:
                return function(*args, **kwargs)
            start, cpu, failed = time.perf_counter(), time.thread_time(), True
            if name == 'world.replay':
                events = args[1] if len(args) > 1 else kwargs['events']
                self.replay_event_counts.append(len(events))
            try:
                result = function(*args, **kwargs)
                failed = False
                return result
            finally:
                elapsed = time.perf_counter() - start
                row = self.stages[name]
                row['calls'] += 1
                row['errors'] += int(failed)
                row['wall_seconds'] += elapsed
                row['thread_cpu_seconds'] += time.thread_time() - cpu
                row['max_wall_seconds'] = max(row['max_wall_seconds'], elapsed)
        return measured

    def _replace(self, owner, name, replacement):
        self.originals.append((owner, name, getattr(owner, name)))
        setattr(owner, name, replacement)

    def __enter__(self):
        # The experiment owns this idle Engine. Ensure any earlier setup
        # connection is replaced so all measured connections get one adapter.
        self.engine.dispose()
        event.listen(self.engine, 'connect', self._connect)
        event.listen(self.engine, 'before_cursor_execute', self._before)
        event.listen(self.engine, 'after_cursor_execute', self._after)
        event.listen(self.engine, 'handle_error', self._error)
        for owner, names in (
            (type(self.kernel), ('revision', 'propose', 'prepare', 'accept', '_accept_transaction',
                                '_tokens', '_verify_tokens', '_event_sources')),
            (WorldQueryService, ('_sample', '_delivery_check', '_execute', 'context_compile', 'query')),
        ):
            for name in names:
                self._replace(owner, name, self._wrap(getattr(owner, name), owner.__name__ + '.' + name))
        original_replay = world_module.replay
        wrapped_replay = self._wrap(original_replay, 'world.replay')
        self._replace(world_module, 'replay', wrapped_replay)
        self._replace(reducer, 'replay', wrapped_replay)
        original_uow = self.kernel.db.uow

        @contextmanager
        def measured_uow(*args, **kwargs):
            observed = self.active
            start, cpu, failed = time.perf_counter(), time.thread_time(), True
            try:
                with original_uow(*args, **kwargs) as repo:
                    yield repo
                failed = False
            finally:
                if observed:
                    self.transactions.append({'write': kwargs.get('write', True),
                        'repeatable_read': kwargs.get('repeatable_read', False), 'failed': failed,
                        'wall_seconds_including_acquire_and_commit': time.perf_counter() - start,
                        'thread_cpu_seconds': time.thread_time() - cpu})
        self._replace(self.kernel.db, 'uow', measured_uow)
        return self

    def __exit__(self, *args):
        self.active = False
        for owner, name, original in reversed(self.originals):
            setattr(owner, name, original)
        event.remove(self.engine, 'connect', self._connect)
        event.remove(self.engine, 'before_cursor_execute', self._before)
        event.remove(self.engine, 'after_cursor_execute', self._after)
        event.remove(self.engine, 'handle_error', self._error)
        # Dispose only this driver's idle pool so instrumented connection-local
        # adapters cannot outlive the profile scope. No active Session is shared.
        self.engine.dispose()

    def snapshot(self):
        return {'sql': {'statements': sum(self.sql.values()), 'statement_kinds': dict(self.sql),
                'referenced_tables': dict(self.sql_tables), 'cursor_seconds': self.sql_seconds,
                'max_cursor_seconds': self.sql_max_seconds, 'failed_statements': self.sql_errors,
                'selected_rows_reported_by_cursor': self.selected_rows,
                'parameters_retained': False, 'excludes_result_decoding_and_pool_pre_ping': True},
            'database_json_codec': dict(self.json_stats),
            'json_bytes_note': 'Exact JSON codec input/output byte lengths, excluding scalar columns, '
                               'PostgreSQL protocol overhead, compression and physical disk I/O.',
            'replay_event_counts': self.replay_event_counts[:],
            'replayed_events_total': sum(self.replay_event_counts),
            'inclusive_stages': dict(self.stages), 'unit_of_work_scopes': self.transactions[:],
            'timing_note': 'Nested stage, codec, SQL and UOW timers overlap. UOW includes connection '
                           'acquisition, application work and final commit/rollback wait; it is not lock time.'}


def normalized_query(result):
    validate_contract('UnifiedQueryResult', result, '2.1')
    assert UUID(result['request_id']).version == 4
    assert UUID(result['read_cut']['read_cut_id']).version == 4
    assert datetime.fromisoformat(result['read_cut']['resolved_at']).utcoffset() is not None
    stable = deepcopy(result)
    del stable['request_id']
    del stable['read_cut']['read_cut_id']
    del stable['read_cut']['resolved_at']
    return stable


def setup_task(kernel, world, subjects, story_time, predicates):
    """Create an actual task/input; leave compilation to the measured phase."""
    principal, wr = world['principal'], world['world_ref']
    manifest = kernel.put_artifact(principal, wr['work_id'],
        {'subject_ids': subjects, 'predicates': predicates}, 'INPUT_MANIFEST')
    tid = str(uuid4())
    spec = {'schema_version': '2.0', 'task_id': tid, 'generation': 0,
        'role_id': 'control_operator', 'task_kind': 'scion_attribution', 'state': 'READY',
        'world_ref': wr, 'story_time': story_time, 'context_bundle_id': None,
        'policy_version': 'nf-policy-1', 'dependency_fingerprint': None,
        'runtime_contract_version': fingerprint({'fixture': 'scion_profile_v1'}),
        'capability_grant_id': principal.grant_id, 'input_artifacts': [manifest],
        'budget': {'currency': 'USD', 'max_amount': '0', 'token_budget': 200000},
        'wall_deadline': (datetime.now(UTC) + timedelta(hours=2)).isoformat()}
    kernel.create_task(principal, spec, 'scion-task:' + tid)
    claim = kernel.claim_task(principal, 'scion-worker:' + tid, lease_seconds=3600, task_id=tid)
    request = {'schema_version': '2.1', 'task_id': tid, 'generation': claim['generation'],
        'recipe_id': 'ADJUDICATOR', 'input_manifest_ref': manifest,
        'read_contract': {'revision': {'mode': 'FIXED', 'world_ref': wr},
            'process_policy': 'ALLOW_PARTIAL', 'index_policy': 'AUTHORITATIVE', 'max_wait_ms': 0},
        'knowledge_cut': {'plane': 'WORLD'}, 'subject_time': {'kind': 'POINT', 'at': story_time},
        'token_budget': 200000, 'idempotency_key': 'scion-context:' + tid}
    return request


class Experiment:
    def __init__(self, args, kernel, observer):
        self.args, self.kernel, self.observer = args, kernel, observer
        self.result = {'schema_version': 'scion-profile-1', 'status': 'RUNNING',
            'instrumentation_version': 'connection-local-codec-once-v2',
            'kind': 'REAL_POSTGRESQL_SEQUENTIAL_ATTRIBUTION',
            'configuration': vars(args) | {'output': str(args.output)},
            'started_at_utc': datetime.now(UTC).isoformat(), 'source_sha256': {
                path: sha256((ROOT / path).read_bytes()).hexdigest() for path in SOURCE_PATHS},
            'environment': hardware() | {'load_client_location': 'direct synchronous Kernel calls',
                'api_processes': 0, 'api_server_threads': 0, 'aggregate_application_pool': 8,
                'observer_pool': 1},
            'records': [], 'datasets': [], 'real_model_calls': 0,
            'capacity_gate': 'NOT_APPLICABLE',
            'limitations': ['No HTTP, admission-queue, client-dispatch or concurrent-lock throughput measured.',
                'Shared host; stage timings include the same lightweight instrumentation in every sample.',
                'cProfile samples are explicitly flagged and excluded from ordinary latency summaries.',
                'Query cache cold means a fresh application result cache; PostgreSQL/OS buffers are warm.',
                'One timed new acceptance per checkpoint is an observation, not a tail-latency estimate.',
                'Historical events are generated by real official accepts; the 2000 selected-event cap is unchanged.']}
        loaded = {}
        for name in ('novel_factory.application.kernel', 'novel_factory.application.queries',
                     'novel_factory.domain.world', 'novel_factory.infrastructure.repository'):
            path = Path(importlib.import_module(name).__file__).resolve()
            assert path.is_relative_to(ROOT / 'src'), 'Application imports must come from the measured worktree'
            loaded[name] = {'path': str(path), 'sha256': sha256(path.read_bytes()).hexdigest()}
        self.result['actual_loaded_modules'] = loaded
        with kernel.db.engine.connect() as conn:
            self.result['environment']['postgres'] = conn.scalar(text('SELECT version()'))
            self.result['environment']['postgres_settings'] = {key: conn.scalar(text('SHOW ' + key))
                for key in ('shared_buffers', 'fsync', 'synchronous_commit', 'max_connections')}

    def save(self):
        self.args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.args.output.with_suffix(self.args.output.suffix + '.tmp')
        temporary.write_text(json.dumps(self.result, ensure_ascii=False, indent=2) + '\n')
        temporary.replace(self.args.output)

    def measure(self, meter, label, callback, *, suite, checkpoint, profile=False, extra=None):
        meter.reset()
        profiler = cProfile.Profile() if profile else None
        rss = current_rss_kib()
        meter.active = True
        start, cpu = time.perf_counter(), time.process_time()
        failure = None
        try:
            if profiler:
                profiler.enable()
            value = callback()
        except Exception as exc:
            failure = {'type': type(exc).__name__, 'code': getattr(exc, 'code', None)}
            raise
        finally:
            if profiler:
                profiler.disable()
            elapsed, cpu_elapsed = time.perf_counter() - start, time.process_time() - cpu
            meter.active = False
            row = {'operation': label, 'suite': suite, 'selected_events_before': checkpoint,
                'cprofile_enabled': profile, 'wall_seconds': elapsed, 'process_cpu_seconds': cpu_elapsed,
                'rss_before_kib': rss, 'rss_after_kib': current_rss_kib(),
                'process_lifetime_max_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'error': failure, **meter.snapshot(), **(extra or {})}
            if profiler:
                stats = pstats.Stats(profiler)
                row['profile'] = [{'file': (str(Path(key[0]).relative_to(ROOT))
                    if Path(key[0]).is_relative_to(ROOT) else Path(key[0]).name),
                    'line': key[1], 'function': key[2], 'calls': values[1],
                    'self_seconds': values[2], 'cumulative_seconds': values[3]}
                    for key, values in sorted(stats.stats.items(), key=lambda pair: pair[1][3], reverse=True)[:40]]
            self.result['records'].append(row)
            self.save()
        return value, row

    def dataset(self, world, suite):
        wr = world['world_ref']
        with self.kernel.db.uow(write=False) as repo:
            rev = self.kernel.revision(repo, wr)
            row = {'suite': suite, 'world_ref': wr, 'selected_events': len(rev['selection']),
                'branch_seq': rev['branch_seq'], 'state_hash': rev['state_hash'],
                'manifest_hash': rev['manifest_hash'], 'entities': len(rev['snapshot']['entities']),
                'relations': len(rev['snapshot']['relations']), 'scope_versions': len(rev['scope_versions'])}
        with self.kernel.db.engine.connect() as conn:
            storage = conn.execute(text('SELECT octet_length(snapshot::text) AS snapshot_json_bytes, '
                'pg_column_size(snapshot) AS snapshot_stored_bytes, '
                'octet_length(selection::text) AS selection_json_bytes, '
                'octet_length(scope_versions::text) AS scope_json_bytes '
                'FROM nf_revisions WHERE work_id=:wid AND branch_id=:bid AND world_revision_id=:rid'),
                {'wid': wr['work_id'], 'bid': wr['branch_id'], 'rid': wr['world_revision_id']}).mappings().one()
            row.update(dict(storage))
            row['all_revision_event_rows'] = conn.scalar(text(
                'SELECT count(*) FROM nf_revision_events WHERE work_id=:wid'), {'wid': wr['work_id']})
            row['all_snapshot_stored_bytes'] = conn.scalar(text(
                'SELECT sum(pg_column_size(snapshot)) FROM nf_revisions WHERE work_id=:wid'), {'wid': wr['work_id']})
        self.result['datasets'].append(row)
        self.save()
        return row

    def reads(self, meter, world, query, *, suite, checkpoint):
        principal, wr = world['principal'], world['world_ref']
        service = WorldQueryService(self.kernel)
        reference = None
        for repeat in range(self.args.repeats + 1):
            value, row = self.measure(meter, 'state_query', lambda: service.query(principal, query),
                suite=suite, checkpoint=checkpoint,
                extra={'repeat': repeat, 'result_cache_expected': 'cold' if repeat == 0 else 'warm'})
            stable = normalized_query(value)
            if reference is None:
                reference = stable
            assert stable == reference, 'Complete fixed-cut query result changed across repeats'
            assert value['read_cut']['world_ref'] == wr
            row.update(stable_result_sha256=fingerprint(stable), response_bytes=len(canonical_bytes(value)),
                       result_cache_hits=service.cache_hits, result_cache_misses=service.cache_misses)
        if self.args.profiles:
            value, row = self.measure(meter, 'state_query', lambda: service.query(principal, query),
                suite=suite, checkpoint=checkpoint, profile=True, extra={'result_cache_expected': 'warm'})
            assert normalized_query(value) == reference
            row['stable_result_sha256'] = fingerprint(reference)
        self.save()

    def pipeline(self, meter, world, template, args, subjects, predicates, *, suite, checkpoint, profile=False):
        principal, wr = world['principal'], deepcopy(world['world_ref'])
        context_request = setup_task(self.kernel, world, subjects, args['at'], predicates)
        service = WorldQueryService(self.kernel)
        common = {'suite': suite, 'checkpoint': checkpoint, 'profile': profile}
        context, row = self.measure(meter, 'context_compile',
            lambda: service.context_compile(principal, context_request), **common)
        assert context['status'] == 'READY'
        row.update(compiled_response_bytes=len(canonical_bytes(context)),
                   compiled_facts=len(context['compiled']['bundle']['facts']))
        key = 'scion-pipeline:' + context_request['task_id']
        proposal, _ = self.measure(meter, 'propose', lambda: self.kernel.propose(principal,
            context_request['task_id'], context_request['generation'], template, args, key), **common)
        mode = 'DEPENDENCY_GUARDED' if template == 'RESOURCE_TRANSFER' else 'STRICT_HEAD'
        prepare = {'schema_version': '2.1', 'task_id': context_request['task_id'],
            'generation': context_request['generation'], 'context_bundle_id': proposal['context_bundle_id'],
            'concurrency_mode': mode, 'base_world_ref': wr, 'proposal_ref': proposal['proposal_ref'],
            'approval_refs': [proposal['approval_id']], 'dependency_handle': proposal['dependency_handle'],
            'idempotency_key': 'prepare:' + key}
        cert, _ = self.measure(meter, 'prepare', lambda: self.kernel.prepare(principal, prepare), **common)
        accept = {key: value for key, value in prepare.items() if key != 'dependency_handle'}
        accept.update(acceptance_certificate_ref=cert, dependency_fingerprint=proposal['dependency_fingerprint'],
                      idempotency_key='accept:' + key)
        receipt, row = self.measure(meter, 'accept', lambda: self.kernel.accept(principal, accept), **common)
        validate_contract('GuardedCommitReceipt', receipt, '2.1')
        assert receipt['original_base_world_ref'] == wr and receipt['parent_world_ref'] == wr
        assert len(receipt['accepted_event_refs']) == 1
        row.update(receipt_sha256=fingerprint(receipt), concurrency_mode=mode, new_world_commits=1)
        repeated, retry_row = self.measure(meter, 'accept_receipt_retry',
            lambda: self.kernel.accept(principal, accept), **common)
        assert repeated == receipt
        retry_row.update(new_world_commits=0, receipt_sha256=fingerprint(repeated))
        world['world_ref'] = receipt['world_ref']
        with self.kernel.db.uow(write=False) as repo:
            rev = self.kernel.revision(repo, world['world_ref'])
            assert len(rev['selection']) == checkpoint + 1
            assert rev['parent_revision_id'] == wr['world_revision_id']
            assert self.kernel.head(repo, wr['work_id'], wr['branch_id']) == world['world_ref']
        self.save()

    def summarize(self):
        groups = defaultdict(list)
        for row in self.result['records']:
            if not row['cprofile_enabled']:
                groups[(row['suite'], row['selected_events_before'], row['operation'],
                        row.get('result_cache_expected'))].append(row)
        self.result['summary'] = [{'suite': key[0], 'selected_events_before': key[1],
            'operation': key[2], 'result_cache': key[3], 'samples': len(rows),
            'latency': percentiles([row['wall_seconds'] for row in rows]) if len(rows) > 1 else None,
            'median_wall_seconds': statistics.median(row['wall_seconds'] for row in rows),
            'median_process_cpu_seconds': statistics.median(row['process_cpu_seconds'] for row in rows),
            'sql_statements': [row['sql']['statements'] for row in rows],
            'decoded_json_bytes': [row['database_json_codec'].get('decode', {}).get('bytes', 0) for row in rows],
            'replayed_events': [row['replayed_events_total'] for row in rows]}
            for key, rows in groups.items()]


def run(args):
    url = os.environ.get('NF_TEST_DATABASE_URL')
    if not url:
        raise ValueError('NF_TEST_DATABASE_URL is required; use scripts/with_postgres.py')
    result = None
    with (tempfile.TemporaryDirectory(prefix='nf-scion-profile-') as objects,
          isolated_kernel(url, Path(objects)) as (kernel, target)):
        with DatabaseObserver(target) as observer, Attribution(kernel) as meter:
            exp = Experiment(args, kernel, observer)
            result = exp.result
            try:
                if args.agents:
                    setup_start = time.perf_counter()
                    fixture = make_population(kernel, args.agents, .8)
                    exp.result['population_setup_seconds'] = time.perf_counter() - setup_start
                    world = fixture['world']
                    exp.dataset(world, 'population')
                    actor = next(actor for actor in fixture['population'] if not actor['writer'])
                    read_world = {**world, 'principal': actor['principal']}
                    exp.reads(meter, read_world, actor['state'], suite='population', checkpoint=4)
                    pipeline_args = {'from_account': fixture['ids']['reserve'],
                        'to_account': fixture['ids']['recipient'], 'resource_id': fixture['ids']['grain'],
                        'quantity': '1', 'unit': 'bag', 'at': point(6)}
                    subjects = [pipeline_args['from_account'], pipeline_args['to_account']]
                    predicates = [f'resource:{pipeline_args["resource_id"]}:bag']
                    exp.pipeline(meter, world, 'RESOURCE_TRANSFER', pipeline_args, subjects, predicates,
                        suite='population', checkpoint=4)
                    if args.profiles:
                        pipeline_args['at'] = point(7)
                        exp.pipeline(meter, world, 'RESOURCE_TRANSFER', pipeline_args, subjects, predicates,
                            suite='population', checkpoint=5, profile=True)
                if args.histories:
                    setup_start = time.perf_counter()
                    world, items, owner = make_world(kernel, args.entities)
                    accepted = 0
                    for checkpoint in args.histories:
                        while accepted < checkpoint:
                            step = run_command(kernel, world['principal'], world['world_ref'], 'SET_RELATION',
                                {'subject_id': items[accepted % len(items)], 'predicate': 'legal_owner',
                                 'object_value': entity_value(owner), 'at': point(3 + accepted)},
                                key=f'scion-history:{accepted}')
                            world['world_ref'] = step['world_ref']
                            accepted += 1
                        assert accepted == checkpoint
                        exp.dataset(world, 'history')
                        request = setup_task(kernel, world, items[:1], point(1), ['legal_owner'])
                        query = {key: request[key] for key in ('schema_version', 'task_id', 'generation',
                            'read_contract', 'knowledge_cut', 'subject_time')}
                        query.update(query={'kind': 'STATE', 'subject_ids': items[:1],
                            'predicates': ['legal_owner']}, page_size=50, cursor=None)
                        exp.reads(meter, world, query, suite='history', checkpoint=checkpoint)
                        pipeline_args = {'subject_id': items[accepted % len(items)], 'predicate': 'legal_owner',
                            'object_value': entity_value(owner), 'at': point(3 + accepted)}
                        exp.pipeline(meter, world, 'SET_RELATION', pipeline_args,
                            [pipeline_args['subject_id']], ['legal_owner'],
                            suite='history', checkpoint=accepted)
                        accepted += 1
                        if args.profiles:
                            pipeline_args.update(subject_id=items[accepted % len(items)], at=point(3 + accepted))
                            exp.pipeline(meter, world, 'SET_RELATION', pipeline_args,
                                [pipeline_args['subject_id']], ['legal_owner'], suite='history',
                                checkpoint=accepted, profile=True)
                            accepted += 1
                    exp.result['history_suite_wall_seconds_including_setup'] = time.perf_counter() - setup_start
                exp.summarize()
                exp.result['status'] = 'PROFILE_COMPLETE_WITH_INVARIANTS_CHECKED'
            except Exception as exc:
                exp.result.update(status='FAILED', error={'type': type(exc).__name__, 'code': getattr(exc, 'code', None)})
                raise
            finally:
                exp.result['finished_at_utc'] = datetime.now(UTC).isoformat()
                exp.save()
        result['database_observer'] = observer.result()
        exp.save()
    print(json.dumps({'status': result['status'], 'records': len(result['records']),
                      'datasets': len(result['datasets']), 'output': str(args.output)}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agents', type=int, default=200, choices=[0, 10, 50, 200, 500, 1000],
                        help='Existing make_population fixture size; 0 omits this suite.')
    parser.add_argument('--entities', type=int, default=200)
    parser.add_argument('--histories', type=int, nargs='*', default=[1, 20, 100])
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--profiles', action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument('--output', type=Path,
                        default=Path('evidence/scion_20261111/phase_a/profile.json'))
    args = parser.parse_args()
    if not 10 <= args.entities <= 10000 or not 1 <= args.repeats <= 30:
        parser.error('entities must be 10..10000; repeats 1..30')
    if args.histories != sorted(set(args.histories)) or any(h < 1 or h > 1998 for h in args.histories):
        parser.error('histories must be unique/increasing, between 1 and 1998')
    minimum_gap = 2 if args.profiles else 1
    if any(right - left < minimum_gap for left, right in zip(args.histories, args.histories[1:])):
        parser.error('checkpoint spacing must leave room for measured acceptance(s)')
    if not args.agents and not args.histories:
        parser.error('at least one suite is required')
    run(args)


if __name__ == '__main__':
    main()
