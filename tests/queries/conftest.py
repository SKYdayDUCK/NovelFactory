from copy import deepcopy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.domain.propositions import encode_proposition

ROOT = Path(__file__).resolve().parents[2]


def uid(): return str(uuid4())
def p(tick, phase=0): return {'tick': tick, 'phase': phase}


@pytest.fixture(scope='module')
def query_env(tmp_path_factory):
    url = os.environ.get('NF_TEST_DATABASE_URL')
    if not url:
        pytest.fail('Real PostgreSQL 17 required; use scripts/with_postgres.py')
    name = 'nf_queries_' + uuid4().hex[:16]
    admin = create_engine(make_url(url).set(database='postgres'), isolation_level='AUTOCOMMIT')
    with admin.connect() as conn:
        assert int(conn.scalar(text('SHOW server_version_num'))) // 10000 == 17
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    target = make_url(url).set(database=name).render_as_string(hide_password=False)
    subprocess.run([sys.executable, '-m', 'alembic', 'upgrade', 'head'], cwd=ROOT,
                   env={**os.environ, 'NF_DATABASE_URL': target}, check=True)
    db = Database(target, pool_size=8)
    kernel = Kernel(db, ObjectStore(tmp_path_factory.mktemp('query_objects')))
    yield {'db': db, 'kernel': kernel, 'query': WorldQueryService(kernel), 'url': target}
    db.close()
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()


def seed(env, *, covered=42):
    ids = {k: uid() for k in ('sword', 'P', 'F', 'V', 'north', 'south', 'grain', 'store', 'caravan')}
    kinds = {'sword': 'ITEM', 'P': 'CHARACTER', 'F': 'CHARACTER', 'V': 'CHARACTER',
             'north': 'REGION', 'south': 'REGION', 'grain': 'RESOURCE', 'store': 'ACCOUNT', 'caravan': 'ACCOUNT'}
    entities = {ids[k]: {'entity_id': ids[k], 'entity_kind': kind, 'name': k, 'created_at': p(0)} for k, kind in kinds.items()}
    def relation(subject, predicate, target):
        return {'subject_id': ids[subject], 'predicate': predicate, 'object_value': {'kind': 'ENTITY', 'entity_id': ids[target]},
                'valid_time': {'start': p(0), 'end': None, 'bounds': '[)'}}
    genesis = {'entities': entities, 'relations': [relation('sword', 'legal_owner', 'P'), relation('sword', 'possessed_by', 'V'),
                                                 relation('V', 'located_in', 'north'), relation('F', 'located_in', 'south')],
               'resources': [{'account_id': ids['caravan'], 'resource_id': ids['grain'], 'quantity': '100', 'unit': 'bag'}],
               'cognitions': [{'record_id': uid(), 'actor_id': ids['P'], 'record_kind': 'BELIEF',
                               'statement': encode_proposition(ids['sword'], 'possessed_by', {'kind': 'ENTITY', 'entity_id': ids['F']}),
                               'acquired_at': p(0), 'evidence_refs': []}], 'lifecycle': {}}
    created = env['kernel'].bootstrap_work('isolated query fixture', genesis, frontiers={eid: p(covered) for eid in entities})
    return {**created, 'ids': ids, 'genesis': genesis}


def start(env, world, *, principal=None, role='control_operator', at=None, subjects=None, predicates=None, wr=None):
    kernel = env['kernel']; principal = principal or world['principal']; wr = wr or world['world_ref']; at = at or p(60)
    subjects = subjects or [world['ids']['sword']]; predicates = predicates or ['legal_owner', 'possessed_by', 'lifecycle']
    # Input manifests contain selectors only, no privileged source text.
    manifest = kernel.put_artifact(world['principal'], wr['work_id'], {'subject_ids': subjects, 'predicates': predicates}, 'INPUT_MANIFEST')
    spec = {'schema_version': '2.0', 'task_id': uid(), 'generation': 0, 'role_id': role, 'task_kind': 'P1_QUERY', 'state': 'READY',
            'world_ref': wr, 'story_time': at, 'context_bundle_id': None, 'policy_version': 'nf-policy-1', 'dependency_fingerprint': None,
            'runtime_contract_version': 'a'*64, 'capability_grant_id': principal.grant_id, 'input_artifacts': [manifest],
            'budget': {'currency': 'CNY', 'max_amount': '0', 'token_budget': 20000},
            'wall_deadline': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    kernel.create_task(principal, spec, spec['task_id'])
    task = kernel.claim_task(principal, 'query-worker', lease_seconds=3600, task_id=spec['task_id'])
    return {'task': task, 'spec': spec, 'manifest': manifest, 'principal': principal, 'world': world}


def request(started, *, at=None, plane='WORLD', actor=None, operation=None, read_contract=None):
    wr = started['spec']['world_ref']; at = at or p(35)
    cut = {'plane': plane}
    if plane == 'BELIEF': cut.update(actor_id=actor or started['world']['ids']['P'], known_at=at)
    return {'schema_version': '2.1', 'task_id': started['task']['task_id'], 'generation': started['task']['generation'],
            'read_contract': read_contract or {'revision': {'mode': 'FIXED', 'world_ref': wr}, 'process_policy': 'ALLOW_PARTIAL',
                                               'index_policy': 'AUTHORITATIVE', 'max_wait_ms': 0},
            'knowledge_cut': cut, 'subject_time': {'kind': 'POINT', 'at': at},
            'query': operation or {'kind': 'STATE', 'subject_ids': [started['world']['ids']['sword']], 'predicates': ['legal_owner', 'possessed_by']},
            'page_size': 100, 'cursor': None}


def compile_request(started, *, plane='WORLD', actor=None, key=None):
    value = request(started, at=started['spec']['story_time'], plane=plane, actor=actor)
    value = {key: value[key] for key in ('schema_version','task_id','generation','read_contract','knowledge_cut','subject_time')}
    value.update(recipe_id='ACTOR' if plane == 'BELIEF' else 'DIRECTOR', input_manifest_ref=started['manifest'], token_budget=20000,
                 idempotency_key=key or started['spec']['task_id'] + ':compile')
    return value
