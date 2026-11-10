"""P2 real TCP/PostgreSQL behavioral load; no provider calls or story-quality claim.

Run under scripts/with_postgres.py. Each run creates an isolated database, real
grants and persistent tasks. Queries and the proposal/certificate/accept pipeline
use HTTP. Setup is measured separately. TEST_FIXTURE controls deliberately stale
resource commands; their expected rejection is not a fictional winner selection.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import resource
import subprocess
import sys
import threading
import time
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from benchmarks.http_load import SqlMeasurements, loopback_server, percentiles
from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.world import resource_at
from novel_factory.fixtures import fixture_definition, point, run_command
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.interfaces.http import HttpSettings, create_app

ROOT = Path(__file__).resolve().parents[1]
SEED = 20261110
SOURCE_TEXT = Path(__file__).read_bytes()
SOURCE_SHA256 = hashlib.sha256(SOURCE_TEXT).hexdigest()


def hardware() -> dict:
    memory = {}
    cpu_models = []
    if Path('/proc/meminfo').exists():
        memory = {line.split(':')[0]: line.split(':', 1)[1].strip()
                  for line in Path('/proc/meminfo').read_text().splitlines()
                  if line.startswith(('MemTotal:', 'MemAvailable:', 'SwapTotal:'))}
    if Path('/proc/cpuinfo').exists():
        cpu_models = sorted({line.split(':', 1)[1].strip()
                             for line in Path('/proc/cpuinfo').read_text().splitlines()
                             if line.startswith('model name')})
    cgroup = {}
    for name in ('cpu.max', 'memory.max', 'memory.peak'):
        path = Path('/sys/fs/cgroup') / name
        if path.exists():
            cgroup[name] = path.read_text().strip()
    return {'platform': platform.platform(), 'python': sys.version,
            'logical_cpu_count': os.cpu_count(), 'cpu_models': cpu_models,
            'memory': memory, 'cgroup': cgroup,
            'versions': {name: importlib.metadata.version(name) for name in
                         ('SQLAlchemy', 'psycopg', 'uvicorn', 'httpx')},
            'git_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT,
                                               text=True).strip(),
            'git_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'],
                                                       cwd=ROOT, text=True)),
            'benchmark_source_sha256': SOURCE_SHA256,
            'load_client_location': 'same process, separate asyncio loop, real loopback TCP',
            'api_processes': 1, 'api_server_threads': 1}


class DatabaseObserver:
    """Separate one-connection observer; not included in application SQL counts."""
    def __init__(self, url):
        self.engine = create_engine(url, pool_size=1, max_overflow=0)
        self.stop = threading.Event()
        self.samples = []
        self.error_types = []

    def snapshot(self):
        with self.engine.connect() as conn:
            return {'database_bytes': conn.scalar(text('SELECT pg_database_size(current_database())')),
                    'wal_lsn': conn.scalar(text('SELECT pg_current_wal_lsn()::text')),
                    'stats': dict(conn.execute(text('SELECT xact_commit, xact_rollback, blks_read, '
                        'blks_hit, temp_files, temp_bytes, deadlocks FROM pg_stat_database '
                        'WHERE datname=current_database()')).mappings().one())}

    def _run(self):
        while not self.stop.is_set():
            try:
                with self.engine.connect() as conn:
                    rows = conn.execute(text("SELECT state, wait_event_type, count(*) AS count "
                        "FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() "
                        "GROUP BY state,wait_event_type")).mappings().all()
                self.samples.append({'elapsed_seconds': round(time.monotonic()-self.started, 6),
                                     'connections': [dict(row) for row in rows]})
            except Exception as exc:  # noqa: BLE001 - observer failures must be recorded, not hidden
                self.error_types.append(type(exc).__name__)
            self.stop.wait(.2)

    def __enter__(self):
        self.before = self.snapshot()
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(5)
        self.after = self.snapshot()
        with self.engine.connect() as conn:
            self.wal_bytes = conn.scalar(text('SELECT pg_wal_lsn_diff(:after,:before)'),
                {'after': self.after['wal_lsn'], 'before': self.before['wal_lsn']})
        self.engine.dispose()

    def result(self):
        return {'sample_interval_seconds': .2, 'observer_connection_budget': 1,
                'before': self.before, 'after': self.after, 'wal_bytes': int(self.wal_bytes),
                'samples': self.samples, 'observer_errors': self.error_types,
                'stats_note': 'pg_stat_database cumulative counters may lag; application SQL '
                              'and pool counters are separately measured by SQLAlchemy listeners'}


@contextmanager
def isolated_kernel(base_url: str, objects: Path):
    name = 'nf_p2_load_' + uuid4().hex[:16]
    admin = create_engine(make_url(base_url).set(database='postgres'), isolation_level='AUTOCOMMIT')
    with admin.connect() as conn:
        assert int(conn.scalar(text('SHOW server_version_num'))) // 10000 == 17
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(base_url).set(database=name).render_as_string(hide_password=False)
    db = None
    try:
        subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=ROOT,
                       env={**os.environ, 'NF_DATABASE_URL': url, 'DATABASE_URL': url}, check=True)
        db = Database(url, pool_size=8, max_overflow=0)
        yield Kernel(db, ObjectStore(objects)), url
    finally:
        if db is not None:
            db.close()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def make_population(kernel, agents: int, hot_fraction: float, *, seed=SEED):
    """Populate authoritative bootstrap + accepted seed events; no direct table writes."""
    definition = fixture_definition('white_bridge')
    genesis, ids = deepcopy(definition['genesis']), definition['ids']
    namespace = uuid5(NAMESPACE_URL, f'nf-p2-load:{seed}:{agents}')
    independent = max(1, agents // 10)
    conflicted = max(1, agents // 10)
    accounts = []
    item_ids = []
    for index in range(agents):
        item = str(uuid5(namespace, f'item:{index}'))
        item_ids.append(item)
        genesis['entities'][item] = {'entity_id': item, 'entity_kind': 'ITEM',
            'name': f'TEST_FIXTURE_封存物_{index}', 'created_at': point(1)}
        for predicate in ('legal_owner', 'possessed_by', 'located_in'):
            genesis['relations'].append({'subject_id': item, 'predicate': predicate,
                'object_value': {'kind': 'ENTITY', 'entity_id': ids['smith']},
                'valid_time': {'start': point(1), 'end': None, 'bounds': '[)'}})
    for index in range(independent):
        pair = []
        for suffix, quantity in [('source', '100'), ('target', '0')]:
            account = str(uuid5(namespace, f'account:{index}:{suffix}'))
            genesis['entities'][account] = {'entity_id': account, 'entity_kind': 'ACCOUNT',
                'name': f'TEST_FIXTURE_仓储_{index}_{suffix}', 'created_at': point(1)}
            genesis['resources'].append({'account_id': account, 'resource_id': ids['grain'],
                                        'quantity': quantity, 'unit': 'bag'})
            pair.append(account)
        accounts.append(pair)
    world = kernel.bootstrap_work('TEST_FIXTURE:P2 behavioral load', genesis,
        frontiers={entity: point(1, 1) for entity in genesis['entities']})
    # These are real accepted events, not inserted history rows or fake snapshots.
    for day in (2, 3, 4, 5):
        accepted = run_command(kernel, world['principal'], world['world_ref'], 'RESOURCE_TRANSFER',
            {'from_account': ids['reserve'], 'to_account': ids['recipient'],
             'resource_id': ids['grain'], 'quantity': '1', 'unit': 'bag', 'at': point(day)})
        world['world_ref'] = accepted['world_ref']
    population = []
    for index in range(agents):
        writer = index < independent + conflicted
        shared = index < int(agents * hot_fraction)
        subject = item_ids[0] if shared else item_ids[index]
        if index < independent:
            pair = accounts[index]
        elif writer:
            pair = [ids['warehouse'], ids['relief']]
        else:
            pair = None
        subjects = pair or [subject]
        predicates = [f'resource:{ids["grain"]}:bag'] if writer else ['owner', 'active_holder']
        grant = kernel.issue_grant(world['principal'], world['world_ref']['work_id'],
            role_id='control_operator' if writer else 'adjudication_advisor',
            capabilities=['READ', 'GRAPH_READ', 'COMPILE', 'TASK_CREATE', 'TASK_CLAIM',
                          'ARTIFACT_WRITE'] + (['PROPOSE', 'APPROVE', 'ACCEPT', 'ADMIN'] if writer else []),
            planes=['WORLD'], max_story_time=point(30))
        manifest = kernel.put_artifact(grant['principal'], world['world_ref']['work_id'],
            {'subject_ids': subjects, 'predicates': predicates}, 'INPUT_MANIFEST')
        task_id = str(uuid4())
        spec = {'schema_version': '2.0', 'task_id': task_id, 'generation': 0,
            'role_id': 'control_operator' if writer else 'adjudication_advisor',
            'task_kind': 'p2_load_fixture', 'state': 'READY', 'world_ref': world['world_ref'],
            'story_time': point(10), 'context_bundle_id': None, 'policy_version': 'nf-policy-1',
            'dependency_fingerprint': None, 'runtime_contract_version': fingerprint({'fixture': 'P2_LOAD'}),
            'capability_grant_id': grant['principal'].grant_id, 'input_artifacts': [manifest],
            'budget': {'currency': 'USD', 'max_amount': '0', 'token_budget': 50000},
            'wall_deadline': (datetime.now(UTC) + timedelta(hours=2)).isoformat()}
        kernel.create_task(grant['principal'], spec, 'create:' + task_id)
        claim = kernel.claim_task(grant['principal'], 'load:' + task_id,
                                  task_id=task_id, lease_seconds=3600)
        common = {'schema_version': '2.1', 'task_id': task_id, 'generation': claim['generation'],
            'read_contract': {'revision': {'mode': 'FIXED', 'world_ref': world['world_ref']},
                'process_policy': 'ALLOW_PARTIAL', 'index_policy': 'AUTHORITATIVE', 'max_wait_ms': 0},
            'knowledge_cut': {'plane': 'WORLD'}, 'subject_time': {'kind': 'POINT', 'at': point(10)}}
        compile_request = {**common, 'recipe_id': 'ADJUDICATOR', 'input_manifest_ref': manifest,
            'token_budget': 50000, 'idempotency_key': 'compile:' + task_id}
        state = {**common, 'subject_time': {'kind': 'POINT', 'at': point(1)},
            'query': {'kind': 'STATE', 'subject_ids': [subject],
            'predicates': ['owner', 'active_holder']}, 'page_size': 50, 'cursor': None}
        graph = {**common, 'subject_time': {'kind': 'POINT', 'at': point(1)},
            'query': {'kind': 'NEIGHBORHOOD', 'seed_ids': [subject],
            'predicates': ['possessed_by', 'located_in'], 'max_hops': 3, 'max_edges': 20},
            'page_size': 50, 'cursor': None}
        history = {**common, 'subject_time': {'kind': 'INTERVAL',
            'interval': {'start': point(1), 'end': point(10), 'bounds': '[)'}},
            'query': {'kind': 'TIMELINE', 'subject_ids': [ids['reserve']],
                'entry_kinds': ['FACT'], 'time_axis': 'SOURCE', 'include_inactive': False},
            'page_size': 50, 'cursor': None}
        population.append({'index': index, 'token': grant['token'], 'principal': grant['principal'],
            'task_id': task_id, 'generation': claim['generation'], 'compile': compile_request,
            'state': state, 'graph': graph, 'history': history, 'writer': writer,
            'conflicted': writer and index >= independent, 'pair': pair})
    return {'world': world, 'population': population, 'ids': ids,
            'independent': independent, 'conflicted': conflicted,
            'genesis_entities': len(genesis['entities']), 'genesis_relations': len(genesis['relations']),
            'seed_accepted_events': 4, 'accounts': accounts}


async def retry_same_request(send_once, payload, *, max_attempts=1,
                             retry_budget_seconds=30, sleeper=asyncio.sleep, clock=time.monotonic):
    """Retry only an explicitly retryable transport/admission outcome.

    Serialize once: every attempt retains exactly the same payload, idempotency
    key, generation and certificate. The caller validates all responses and marks
    retryability. Authority/semantic failures, including expired certificates,
    never cause this driver to prepare a new command or silently change its base.
    """
    if not 1 <= max_attempts <= 8 or not 0 < retry_budget_seconds <= 120:
        raise ValueError('retry budget must be 1..8 attempts and 0..120 seconds')
    request_bytes = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
    deadline = clock() + retry_budget_seconds
    record = None
    for attempt in range(1, max_attempts + 1):
        if attempt > 1 and clock() >= deadline:
            record['retry_exhausted'] = True
            return None
        value, record = await send_once(request_bytes, attempt)
        if value is not None or not record.get('retryable'):
            return value
        # Respect Retry-After, add bounded exponential delay, and never retain
        # a client request or database connection during this sleep.
        delay = max(record.get('retry_after_seconds', 0), min(2**(attempt-1), 5))
        if attempt == max_attempts or clock() + delay >= deadline:
            record['retry_exhausted'] = True
            return None
        record['retry_wait_seconds'] = delay
        await sleeper(delay)


def retryable_failure(status, body):
    """Do not infer safe recovery merely from a status code or error string."""
    return (status in {429, 503}
            and body.get('code') in {'BACKPRESSURE', 'RATE_LIMITED', 'SERVICE_UNAVAILABLE', 'POOL_TIMEOUT'}
            and body.get('retryable') is True
            and body.get('recovery') in {'WAIT', 'RETRY_SAME_COMMAND'})


async def exercise(base_url, fixture, *, profile='mixed', arrival_rps=10,
                   open_loop_rps=1000, rounds=5, observer=None, max_attempts=1,
                   retry_budget_seconds=30):
    population = fixture['population']
    raw, expected_errors, correctness_errors, operational_errors = [], [], [], []
    logical_sequence = 0
    receipts = []
    active = peak_active = 0
    origin = time.monotonic()
    client_limit = 256
    expected_calls = 5*len(population) + 7*fixture['independent'] + 1
    if profile == 'overload':
        expected_calls += len(population)*rounds
    async with httpx.AsyncClient(base_url=base_url, timeout=30, trust_env=False,
            limits=httpx.Limits(max_connections=client_limit,
                               max_keepalive_connections=client_limit, keepalive_expiry=2)) as http:
        async def call(client, label, endpoint, payload, *, scheduled=None, duplicate=False):
            nonlocal logical_sequence
            logical_sequence += 1
            operation_id = str(logical_sequence)
            scheduled_at = scheduled if scheduled is not None else time.monotonic()
            last_record = None

            async def send_once(request_bytes, attempt):
                nonlocal active, peak_active, last_record
                begin = time.monotonic()
                active += 1
                peak_active = max(peak_active, active)
                record = {'agent': client['index'], 'task_id': client['task_id'], 'label': label,
                    'operation_id': operation_id, 'attempt': attempt,
                    'infrastructure_retry': attempt > 1,
                    'endpoint': endpoint, 'started_seconds': round(begin-origin, 6),
                    'request_sha256': fingerprint(json.loads(request_bytes)),
                    'request_bytes_sha256': hashlib.sha256(request_bytes).hexdigest(),
                    'duplicate_retry': duplicate, 'retryable': False,
                    'arrival_delay_ms': round(max(0, begin-scheduled_at)*1000, 4)}
                body = None
                try:
                    response = await http.post('/api/v1/' + endpoint,
                        headers={'Authorization': 'Bearer ' + client['token'],
                                 'Content-Type': 'application/json'}, content=request_bytes)
                    body = response.json()
                    record.update(status=response.status_code, code=body.get('code'),
                        response_sha256=fingerprint(body), response_bytes=len(response.content))
                    if response.status_code == 200:
                        if endpoint == 'queries/timeline':
                            validate_contract('UnifiedQueryResult', body, '2.1')
                            if body['read_cut']['world_ref'] != fixture['world']['world_ref']:
                                raise AssertionError('read did not preserve fixed WorldRef')
                            if label in {'state_cold', 'state_warm', 'state_mixed', 'overload'}:
                                items = body['payload']['state_items']
                                expected = {'kind': 'ENTITY', 'entity_id': fixture['ids']['smith']}
                                if len(items) != 2 or any(x['answer']['data'] != expected for x in items):
                                    raise AssertionError('fixed state payload changed under concurrent writes')
                        elif endpoint == 'contexts/compile':
                            validate_contract('ContextCompileResult', body, '2.1')
                            if body['status'] != 'READY':
                                raise AssertionError('context compilation incomplete')
                        elif endpoint == 'world/accept-guarded':
                            validate_contract('GuardedCommitReceipt', body, '2.1')
                            receipts.append(body)
                            if label == 'accept_conflict':
                                raise AssertionError('stale shared candidate unexpectedly accepted')
                    else:
                        validate_contract('ErrorEnvelope', body)
                        record['retryable'] = retryable_failure(response.status_code, body)
                        if record['retryable']:
                            try:
                                retry_after = float(response.headers.get('Retry-After', '1'))
                                record['retry_after_seconds'] = retry_after if 0 <= retry_after <= 120 else 120
                            except ValueError:
                                record['retry_after_seconds'] = 1
                        elif (response.status_code == 409 and client['conflicted']
                                and label == 'accept_conflict' and body['code'] == 'DEPENDENCY_CONFLICT'):
                            record['expected_conflict'] = True
                            expected_errors.append(record)
                        else:
                            correctness_errors.append({**record, 'reason': 'unexpected response'})
                except httpx.TransportError as exc:
                    # Unknown delivery can only replay these already-idempotent
                    # operations. No token, request body or exception URL is logged.
                    record.update(status='transport_error', error_type=type(exc).__name__, retryable=True)
                except Exception as exc:  # noqa: BLE001 - retain validation/correctness failures
                    record.update(status=record.get('status', 'client_error'), error_type=type(exc).__name__)
                    record['retryable'] = False
                    correctness_errors.append({**record, 'reason': type(exc).__name__})
                finally:
                    active -= 1
                    record['latency_ms'] = round((time.monotonic()-begin)*1000, 4)
                    raw.append(record)
                    last_record = record
                value = body if record.get('status') == 200 and 'error_type' not in record else None
                return value, record

            value = await retry_same_request(send_once, payload, max_attempts=max_attempts,
                                              retry_budget_seconds=retry_budget_seconds)
            if value is None and last_record.get('retry_exhausted'):
                # Overload rejection remains visible but is not a world-state
                # correctness failure. Required business calls must all recover.
                if label != 'overload':
                    operational_errors.append({**last_record, 'reason': 'retry budget exhausted'})
                else:
                    expected_errors.append(last_record)
            return value

        async def phase(label, fn, *, rate=arrival_rps, clients=population):
            start = time.monotonic()
            calls = []
            for number, client in enumerate(clients):
                scheduled = start + number/rate
                await asyncio.sleep(max(0, scheduled-time.monotonic()))
                calls.append(asyncio.create_task(fn(client, scheduled)))
            result = await asyncio.gather(*calls)
            if observer:
                observer(label)
            return result

        compiled = await phase('compile', lambda c, s: call(c, 'compile', 'contexts/compile', c['compile'], scheduled=s),
                               rate=min(arrival_rps, 10))
        if not all(compiled):
            return {'raw': raw, 'expected_errors': expected_errors,
                'correctness_errors': correctness_errors, 'operational_errors': operational_errors, 'receipts': receipts,
                'peak_inflight_http': peak_active, 'planned_requests': expected_calls,
                'duration_seconds': time.monotonic()-origin}
        await phase('state_cold', lambda c, s: call(c, 'state_cold', 'queries/timeline', c['state'], scheduled=s))
        await phase('state_warm', lambda c, s: call(c, 'state_warm', 'queries/timeline', c['state'], scheduled=s))
        writers = [c for c in population if c['writer']]

        async def prepare(client, scheduled):
            command = {'template_id': 'RESOURCE_TRANSFER', 'args': {
                'from_account': client['pair'][0], 'to_account': client['pair'][1],
                'resource_id': fixture['ids']['grain'], 'quantity': '1', 'unit': 'bag', 'at': point(10)}}
            proposal = await call(client, 'propose', 'proposals', {'task_id': client['task_id'],
                'generation': client['generation'], 'command': command,
                'idempotency_key': 'propose:' + client['task_id']}, scheduled=scheduled)
            if not proposal:
                return
            request = {'schema_version': '2.1', 'task_id': client['task_id'],
                'generation': client['generation'], 'context_bundle_id': proposal['context_bundle_id'],
                'concurrency_mode': 'DEPENDENCY_GUARDED', 'base_world_ref': fixture['world']['world_ref'],
                'proposal_ref': proposal['proposal_ref'], 'approval_refs': [proposal['approval_id']],
                'dependency_handle': proposal['dependency_handle'],
                'idempotency_key': 'prepare:' + client['task_id']}
            certificate = await call(client, 'certificate', 'world/acceptance-certificates', request)
            if certificate:
                command = {key: value for key, value in request.items() if key != 'dependency_handle'}
                command.update(acceptance_certificate_ref=certificate,
                    dependency_fingerprint=proposal['dependency_fingerprint'],
                    idempotency_key='accept:' + client['task_id'])
                client['accept_request'] = command

        # Read traffic continues while write candidates/certificates are built.
        # Rates are per class, and schema/authorization/database work remains real.
        preparation = asyncio.create_task(phase('prepare', prepare, clients=writers,
                                                rate=min(arrival_rps, 1)))
        await phase('graph', lambda c, s: call(c, 'graph' if c['index'] % 10 == 0 else 'state_mixed',
            'queries/timeline', c['graph'] if c['index'] % 10 == 0 else c['state'], scheduled=s))
        await phase('history', lambda c, s: call(c, 'history', 'queries/timeline', c['history'], scheduled=s))
        await preparation
        if observer:
            observer('mixed_join')
        # A declared control-plane fixture decision invalidates the shared
        # resource candidates before concurrent delivery. Arrival order does not
        # select a fictional winner. Actual joint intent arbitration is tested
        # separately by the P2 scheduler and the A-H matrix.
        hot = [c for c in writers if c['conflicted']]
        chosen = hot[0]
        selected_receipt = None
        if 'accept_request' in chosen:
            selected_receipt = await call(chosen, 'accept_selected_fixture', 'world/accept-guarded', chosen['accept_request'])

        async def accept(client, scheduled):
            if 'accept_request' not in client:
                return
            if client['conflicted'] and selected_receipt is None:
                return
            if client is chosen:
                return await call(client, 'accept_retry', 'world/accept-guarded',
                                  client['accept_request'], scheduled=scheduled, duplicate=True)
            label = 'accept_conflict' if client['conflicted'] else 'accept_independent'
            response = await call(client, label, 'world/accept-guarded', client['accept_request'], scheduled=scheduled)
            if response and not client['conflicted']:
                await call(client, 'accept_retry', 'world/accept-guarded', client['accept_request'], duplicate=True)
        await phase('accept', accept, clients=writers, rate=min(arrival_rps, 1))
        if profile == 'overload':
            # Independent scheduled arrival stream, bounded to configured request
            # count. Driver delay is reported; no coordinated-omission claim.
            await phase('overload', lambda c, s: call(c, 'overload', 'queries/timeline', c['state'], scheduled=s),
                        rate=open_loop_rps, clients=population*rounds)
    return {'raw': raw, 'expected_errors': expected_errors,
            'correctness_errors': correctness_errors, 'operational_errors': operational_errors, 'receipts': receipts,
            'peak_inflight_http': peak_active, 'planned_requests': expected_calls,
            'duration_seconds': time.monotonic()-origin}


def summarize(result):
    raw, duration = result['raw'], result['duration_seconds']
    first_attempts = [row for row in raw if row.get('attempt', 1) == 1]
    infrastructure_retries = len(raw) - len(first_attempts)
    first_failures = [row for row in first_attempts if (row['status'] != 200 or 'error_type' in row)
                      and not row.get('expected_conflict', row.get('code') == 'DEPENDENCY_CONFLICT'
                                      and row.get('label') == 'accept_conflict')]
    last_attempts = {}
    for index, row in enumerate(raw):
        key = row.get('operation_id', str(index))
        if key not in last_attempts or row.get('attempt', 1) > last_attempts[key].get('attempt', 1):
            last_attempts[key] = row
    labels = sorted({r['label'] for r in raw})
    counts = Counter(str(r['status']) for r in raw)
    successes = [r for r in raw if r['status'] == 200 and 'error_type' not in r]
    rejections = [r for r in raw if r['status'] != 200 or 'error_type' in r]
    unique_revisions = {r['world_ref']['world_revision_id'] for r in result['receipts']}
    cross_head_revisions = {r['world_ref']['world_revision_id'] for r in result['receipts']
                            if r['original_base_world_ref'] != r['parent_world_ref']}
    duplicate_receipts = sum(r['duplicate_retry'] for r in successes)
    real_conflicts = [r for r in raw if r.get('code') in {'DEPENDENCY_CONFLICT', 'RESOURCE_CONFLICT'}]
    return {'duration_seconds': round(duration, 6), 'planned_requests': result['planned_requests'],
        'emitted_requests': len(raw), 'completed_requests': len(raw),
        'client_requests_never_emitted': result['planned_requests']-len(first_attempts),
        'emitted_logical_operations': len(first_attempts),
        'completed_logical_operations': len(last_attempts),
        'first_attempt_failure_count': len(first_failures),
        'first_attempt_status_counts': dict(Counter(str(row['status']) for row in first_attempts)),
        'first_attempt_latency': percentiles([row['latency_ms']/1000 for row in first_attempts]),
        'recovered_logical_operations': sum(row.get('attempt', 1) > 1 and row['status'] == 200
            and 'error_type' not in row for row in last_attempts.values()),
        'logical_operation_completion_latency': percentiles([
            (row['arrival_delay_ms']+row['latency_ms'])/1000 for row in last_attempts.values()]),
        'api_tool_calls': len(raw), 'mcp_tool_calls': 0, 'status_counts': dict(counts),
        'valid_successful_calls': len(successes), 'api_success_rps': round(len(successes)/duration, 4),
        'successful_duplicate_receipts': duplicate_receipts,
        'effective_call_rps_excluding_duplicates': round((len(successes)-duplicate_receipts)/duration, 4),
        'new_world_commit_receipts': len(unique_revisions),
        'accepted_original_input_after_unrelated_head_progress': len(cross_head_revisions),
        'cross_head_accept_rate_among_unique_commits': len(cross_head_revisions)/max(1,len(unique_revisions)),
        'semantic_replan_requests_in_load': 0,
        'effective_world_commits_per_second': round(len(unique_revisions)/duration, 4),
        'conflict_count': len(real_conflicts), 'conflict_rate_all_requests': len(real_conflicts)/len(raw),
        'failure_rate_all_requests': (len(raw)-len(successes))/len(raw),
        'idempotent_retry_rate_all_requests': sum(r['duplicate_retry'] for r in raw)/len(raw),
        'infrastructure_retries': infrastructure_retries, 'model_retries': 0,
        'peak_outstanding_client_requests': result['peak_inflight_http'],
        'client_connection_limit': 256,
        'outstanding_definition': 'Includes HTTPX connection-pool wait; server ingress is measured separately',
        'latency_all_responses': percentiles([r['latency_ms']/1000 for r in raw]),
        'latency_successes': percentiles([r['latency_ms']/1000 for r in successes]),
        'latency_rejections_and_failures': percentiles([r['latency_ms']/1000 for r in rejections]),
        'scheduled_to_completion_latency': percentiles([
            (r['arrival_delay_ms']+r['latency_ms'])/1000 for r in raw]),
        'arrival_schedule_delay': percentiles([r['arrival_delay_ms']/1000 for r in raw]),
        'per_operation': {label: {'calls': len(rows := [r for r in raw if r['label'] == label]),
            'successes': sum(r['status'] == 200 and 'error_type' not in r for r in rows),
            'latency_all_responses': percentiles([r['latency_ms']/1000 for r in rows]),
            'scheduled_to_completion_latency': percentiles([
                (r['arrival_delay_ms']+r['latency_ms'])/1000 for r in rows])}
            for label in labels},
        'successful_calls_by_agent': dict(Counter(str(r['agent']) for r in successes)),
        'latency_warning': 'P99 for individual write categories has few samples; descriptive only',
        'correctness_errors': result['correctness_errors'],
        'unresolved_operational_errors': result.get('operational_errors', [])}


def observed_open_loop(raw, target):
    """Measure actual first attempt starts; retries never improve offered rate."""
    rows = [row for row in raw if row['label'] == 'overload' and row.get('attempt', 1) == 1]
    if not rows:
        return {'gate': 'NOT_APPLICABLE', 'target_rps': None, 'first_attempts': 0}
    span = max(row['started_seconds'] for row in rows) - min(row['started_seconds'] for row in rows)
    actual = (len(rows)-1)/max(span, 1e-9)
    return {'gate': 'MET' if actual >= target * .95 else 'NOT_MET',
            'target_rps': target, 'first_attempts': len(rows),
            'actual_call_start_rps': actual, 'start_window_seconds': span,
            'max_schedule_delay_ms': max(row['arrival_delay_ms'] for row in rows),
            'criterion': 'Actual first call starts >=95% of frozen target; HTTPX waits still included',
            'scope': 'Client call-start rate only, not a claim of network wire rate or sustained capacity'}


def load_exit_code(result, scope):
    if scope == 'recovered-completion':
        return int(result['bounded_recovery_business_gate'] != 'PASS')
    if scope == 'capacity':
        return int(result['capacity_gate'] != 'PASS')
    raise ValueError('unknown load gate scope')


def run_benchmark(kernel, url, *, agents, hot_fraction=.2, profile='mixed', arrival_rps=10,
                  open_loop_rps=1000, rounds=5, max_attempts=1, retry_budget_seconds=30):
    started_at_utc = datetime.now(UTC).isoformat()
    before_setup = time.monotonic()
    fixture = make_population(kernel, agents, hot_fraction)
    setup_seconds = time.monotonic()-before_setup
    service = WorldQueryService(kernel)
    app = create_app(kernel, query_service=service, settings=HttpSettings())
    phases = []
    process_before = resource.getrusage(resource.RUSAGE_SELF)
    def phase_observer(label):
        phases.append({'phase': label, 'checked_out_connections': kernel.db.engine.pool.checkedout(),
                       'cache_hits': service.cache_hits, 'cache_misses': service.cache_misses})
    with loopback_server(app) as base_url, SqlMeasurements(kernel.db.engine) as measured:
        with DatabaseObserver(url) as observer:
            result = asyncio.run(exercise(base_url, fixture, profile=profile, arrival_rps=arrival_rps,
                open_loop_rps=open_loop_rps, rounds=rounds, observer=phase_observer,
                max_attempts=max_attempts, retry_budget_seconds=retry_budget_seconds))
        sql = measured.result(result['duration_seconds'])
    process_after = resource.getrusage(resource.RUSAGE_SELF)
    with kernel.db.uow(write=False) as repo:
        wr = kernel.head(repo, fixture['world']['world_ref']['work_id'])
        final = kernel.revision(repo, wr)
        initial = kernel.revision(repo, fixture['world']['world_ref'])
        settings = {name: repo.conn.scalar(text('SHOW ' + name)) for name in
            ('server_version', 'max_connections', 'shared_buffers', 'fsync', 'synchronous_commit')}
        table_counts = {name: repo.conn.scalar(text('SELECT count(*) FROM nf_' + name))
                       for name in ('tasks', 'contexts', 'revisions', 'events', 'receipts', 'scopes')}
    summary = summarize(result)
    expected_new = fixture['independent'] + 1
    actual_new = final['branch_seq'] - initial['branch_seq']
    correctness = {'new_world_revisions': actual_new, 'expected_new_world_revisions': expected_new,
        'new_selected_effect_groups': len(final['selection'])-len(initial['selection']),
        'all_grants_have_real_persisted_tasks': table_counts['tasks'] >= agents,
        'connections_released_between_phases': all(p['checked_out_connections'] == 0 for p in phases
                                                   if p['phase'] not in {'prepare', 'graph', 'history'}),
        'shared_resource_final_quantity': resource_at(final['snapshot'], fixture['ids']['warehouse'],
            fixture['ids']['grain'], 'bag', point(11))['quantity'],
        'independent_resource_final_quantities': [resource_at(final['snapshot'], pair[0],
            fixture['ids']['grain'], 'bag', point(11))['quantity'] for pair in fixture['accounts']]}
    passed = (not summary['correctness_errors'] and not summary['unresolved_operational_errors']
        and summary['client_requests_never_emitted'] == 0
        and actual_new == expected_new
        and correctness['new_selected_effect_groups'] == expected_new
        and correctness['connections_released_between_phases']
        and correctness['shared_resource_final_quantity'] == '99'
        and all(q == '99' for q in correctness['independent_resource_final_quantities'])
        and sql['peak_checked_out_connections'] <= 8 and sql['checked_out_at_end'] == 0)
    open_loop = observed_open_loop(result['raw'], open_loop_rps)
    first_passed = passed and not summary['first_attempt_failure_count']
    capacity_passed = first_passed and open_loop['gate'] in {'MET', 'NOT_APPLICABLE'}
    return {'schema_version': 'p2-load-2', 'evidence_kind': 'REAL_HTTP_REAL_POSTGRESQL_TEST_FIXTURE',
        'started_at_utc': started_at_utc,
        'engineer_correctness': 'PASS' if passed else 'FAIL',
        'first_attempt_capacity_gate': 'PASS' if first_passed else 'NOT_MET',
        'open_loop_offered_rate': open_loop,
        'capacity_gate': 'PASS' if capacity_passed else 'NOT_MET',
        'bounded_recovery_business_gate': 'PASS' if passed else 'NOT_MET',
        'recovery_scope': 'Same original commands only; a recovery PASS does not promote first-attempt capacity.',
        'logical_agent_task_count': agents, 'hot_subject_fraction': hot_fraction, 'profile': profile,
        'seed': SEED, 'setup_seconds': round(setup_seconds, 6), 'environment': hardware(),
        'database_settings': settings, 'data': {'entities': fixture['genesis_entities'],
            'initial_relations': fixture['genesis_relations'], 'initial_accepted_events': 4,
            'final_table_counts': table_counts, 'scale': 'bounded synthetic small world; not 100k-event medium design'},
        'load': {'scheduled_phase_arrival_rps': arrival_rps,
            'retry_policy': {'max_attempts': max_attempts, 'retry_budget_seconds': retry_budget_seconds,
                'same_payload_and_key': True, 'semantic_reprepare': False,
                'deadline_scope': 'No new retry starts after budget; in-flight attempt may finish later within HTTP timeout',
                'individual_http_timeout_seconds': 30,
                'retryable': ['validated 429/503 WAIT or RETRY_SAME_COMMAND', 'TransportError']},
            'context_arrival_rps': min(arrival_rps, 10), 'write_arrival_rps': min(arrival_rps, 1),
            'mixed_read_distribution': {'state_fraction': .9, 'graph_fraction': .1,
                'history': 'one additional FACT timeline per task'},
            'overload_arrival_rps': open_loop_rps if profile == 'overload' else None,
            'overload_rounds': rounds if profile == 'overload' else None,
            'independent_world_commands': fixture['independent'],
            'shared_candidates': fixture['conflicted'], 'http_settings': HttpSettings().__dict__,
            'hot_fraction_meaning': 'Fraction of read subjects mapped to one shared item; '
                'shared writer candidates remain agents/10 in every profile',
            'cache_size': service.cache_size, 'cache_boundary': 'process query-result cache cold/warm; '
                'PostgreSQL and OS caches are not flushed',
            'shared_conflict_policy': 'one predetermined TEST_FIXTURE control command before stale deliveries; '
                'not resource arbitration by database arrival order'},
        'metrics': summary, 'database_application': sql, 'database_observer': observer.result(),
        'phase_boundaries': phases,
        'resources': {'process_user_cpu_seconds': process_after.ru_utime-process_before.ru_utime,
            'process_system_cpu_seconds': process_after.ru_stime-process_before.ru_stime,
            'process_peak_rss_kib': process_after.ru_maxrss,
            'minor_page_faults': process_after.ru_minflt-process_before.ru_minflt,
            'major_page_faults': process_after.ru_majflt-process_before.ru_majflt},
        'admission': app.state.admission.snapshot(),
        'authentication_admission': app.state.authentication_admission.snapshot(),
        'ingress': app.state.ingress.snapshot(), 'correctness': correctness,
        'external_model': {'actual_calls': 0, 'peak_inflight_calls': 0, 'tokens': 0,
            'billed_cost_usd': None, 'status': 'NOT_DISPATCHED',
            'note': 'No real or fixture model dispatch; this measures service behavior, not model concurrency'},
        'raw_requests': result['raw'], 'world_commit_receipts': result['receipts'],
        'limitations': ['One API process with eight pooled DB connections',
            'Host is shared with other development tasks; wall-clock observations are not exclusive-host SLA',
            'No sustained capacity or multi-hour soak claim', 'No 200-model novel creation claim',
            'HTTP pipeline represents control commands; joint scheduler arbitration is validated separately']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agents', type=int, choices=[10, 50, 200], required=True)
    parser.add_argument('--profile', choices=['mixed', 'hotspot', 'overload'], default='mixed')
    parser.add_argument('--hot-fraction', type=float, default=.2)
    parser.add_argument('--arrival-rps', type=float, default=10)
    parser.add_argument('--open-loop-rps', type=float, default=1000)
    parser.add_argument('--rounds', type=int, default=5)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-attempts', type=int, default=1)
    parser.add_argument('--retry-budget-seconds', type=float, default=30)
    parser.add_argument('--gate-scope', choices=['capacity', 'recovered-completion'], default='capacity')
    args = parser.parse_args()
    if not 0 <= args.hot_fraction <= 1 or min(args.arrival_rps, args.open_loop_rps) <= 0 or not 1 <= args.rounds <= 30:
        parser.error('invalid workload bounds')
    if not 1 <= args.max_attempts <= 8 or not 0 < args.retry_budget_seconds <= 120:
        parser.error('invalid retry bounds')
    url = os.environ.get('NF_TEST_DATABASE_URL')
    if not url:
        parser.error('run under scripts/with_postgres.py or set NF_TEST_DATABASE_URL')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    source_path = args.output.parent/'source_snapshots'/f'{SOURCE_SHA256}.py'
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(SOURCE_TEXT)
    import tempfile
    with (tempfile.TemporaryDirectory(prefix='nf-p2-load-objects-') as directory,
          isolated_kernel(url, Path(directory)) as (kernel, database_url)):
        result = run_benchmark(kernel, database_url, agents=args.agents,
            hot_fraction=args.hot_fraction, profile=args.profile, arrival_rps=args.arrival_rps,
            open_loop_rps=args.open_loop_rps, rounds=args.rounds,
            max_attempts=args.max_attempts, retry_budget_seconds=args.retry_budget_seconds)
    result['reproduction_argv'] = sys.argv
    result['source_snapshot'] = str(source_path)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'file': str(args.output), 'correctness': result['engineer_correctness'],
        'logical_agents': args.agents, 'first_attempt_capacity_gate': result['first_attempt_capacity_gate'],
        'bounded_recovery_business_gate': result['bounded_recovery_business_gate'],
        'capacity_gate': result['capacity_gate'], 'open_loop_offered_rate': result['open_loop_offered_rate'],
        'gate_scope': args.gate_scope, 'metrics': result['metrics']}, ensure_ascii=False, indent=2))
    return load_exit_code(result, args.gate_scope)


if __name__ == '__main__':
    raise SystemExit(main())
