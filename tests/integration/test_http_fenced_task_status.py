"""Verify the singleton work-lock join under an actual concurrent grant revocation."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture


def ready_task(kernel, book, principal):
    task_id = str(uuid4())
    spec = {
        "schema_version": "2.0", "task_id": task_id, "generation": 0,
        "role_id": "writer", "task_kind": "kernel_acceptance", "state": "READY",
        "world_ref": book["world_ref"], "story_time": {"tick": 0, "phase": 0},
        "context_bundle_id": None, "policy_version": "nf-policy-1",
        "dependency_fingerprint": None, "runtime_contract_version": "a" * 64,
        "capability_grant_id": principal.grant_id, "input_artifacts": [],
        "budget": {"currency": "USD", "max_amount": "1", "token_budget": 1000},
        "wall_deadline": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
    }
    kernel.create_task(principal, spec, "status-test:" + task_id)
    return task_id


def test_singleton_status_holds_work_lock_and_rechecks_grant(kernel_env):
    base = kernel_env['kernel']
    a, b = (load_fixture(base) for _ in range(2))
    owner = base.issue_grant(a['principal'], a['world_ref']['work_id'], role_id='writer')['principal']
    task = ready_task(base, a, owner)
    assert base.get_task_with_locked_work(owner, task.upper()) == base.get_task(owner, task)
    for principal, task_id in ((b['principal'], task), (owner, str(uuid4()))):
        with pytest.raises(DomainError) as error:
            base.get_task_with_locked_work(principal, task_id)
        assert error.value.code == 'VIEW_DENIED'
    with ThreadPoolExecutor(max_workers=1) as pool:
        with base.db.engine.begin() as holder:
            holder.execute(text('UPDATE nf_grants SET active=false WHERE grant_id=:id'), {'id': owner.grant_id})
            holder_pid = holder.scalar(text('SELECT pg_backend_pid()'))
            pending = pool.submit(base.get_task_with_locked_work, owner, task)
            deadline = monotonic() + 3
            while True:
                holder.execute(text('SELECT pg_stat_clear_snapshot()'))
                if holder.scalar(text('SELECT EXISTS (SELECT FROM pg_stat_activity '
                                         'WHERE :p=ANY(pg_blocking_pids(pid)))'), {'p': holder_pid}):
                    break
                if pending.done():
                    pending.result()
                    raise AssertionError('Revocation did not block the read')
                if monotonic() >= deadline:
                    raise AssertionError('Reader never waited on the grant update')
                sleep(.01)
            # The reader is blocked on the grant and already holds the owning
            # work row. The separate probe cannot take an exclusive work lock.
            with pytest.raises(OperationalError) as conflict:
                with base.db.engine.begin() as probe:
                    probe.execute(text('SELECT work_id FROM nf_works WHERE work_id=:w FOR UPDATE NOWAIT'),
                                  {'w': a['world_ref']['work_id']})
            assert conflict.value.orig.sqlstate == '55P03'
        with pytest.raises(DomainError) as denied:
            pending.result(timeout=5)
        assert denied.value.code == 'VIEW_DENIED'


def test_complete_locked_work_and_generation_are_separate_from_task(kernel_env):
    kernel = kernel_env['kernel']
    book = load_fixture(kernel)
    grant = kernel.issue_grant(book['principal'], book['world_ref']['work_id'], role_id='writer')['principal']
    task_id = ready_task(kernel, book, grant)
    claim = kernel.claim_task(grant, 'trusted-worker', task_id=task_id)
    with kernel.db.uow(write=False) as repo:
        task, work, world_ref = repo.task_with_locked_work(task_id)
        assert task == repo.one('tasks', {'task_id': task_id})
        assert work == repo.one('works', {'work_id': task['work_id']})
        assert work['policy_version'] and work['interpretation_ref'] and work['policy_ref']
        assert task['generation'] == claim['generation']
        assert world_ref == repo.one('task_inputs', {'task_id': task_id, 'generation': task['generation']})['spec']['world_ref']


@pytest.mark.parametrize('setting,expected_sql', [(None, 5), ('0', 5), ('1', 3)])
def test_environment_setting_selects_only_authenticated_http_status(kernel_env, monkeypatch, tmp_path,
                                                                    setting, expected_sql):
    from fastapi.testclient import TestClient
    from fastapi.encoders import jsonable_encoder
    from sqlalchemy import event
    from novel_factory.interfaces.http import from_environment
    from novel_factory.contracts.registry import validate_contract

    kernel = kernel_env['kernel']
    book = load_fixture(kernel)
    own = kernel.issue_grant(book['principal'], book['world_ref']['work_id'], role_id='writer')
    peer = kernel.issue_grant(book['principal'], book['world_ref']['work_id'], role_id='writer')
    task_id = ready_task(kernel, book, own['principal'])
    expected = kernel.get_task(own['principal'], task_id)
    monkeypatch.setenv('NF_DATABASE_URL', kernel_env['url'])
    monkeypatch.setenv('NF_OBJECT_ROOT', str(tmp_path / 'http-settings'))
    monkeypatch.setenv('NF_POOL_SIZE', '4')
    if setting is None:
        monkeypatch.delenv('NF_HTTP_FENCED_TASK_STATUS', raising=False)
    else:
        monkeypatch.setenv('NF_HTTP_FENCED_TASK_STATUS', setting)
    app = from_environment()
    engine = app.state.kernel.db.engine
    statements = []

    def record(conn, cursor, sql, *_):
        statements.append(sql)

    def headers(actor):
        return {'Authorization': 'Bearer ' + actor['token']}

    try:
        with TestClient(app) as client:
            path = '/api/v1/tasks/' + task_id.upper()
            event.listen(engine, 'before_cursor_execute', record)
            try:
                response = client.get(path, headers=headers(own))
                assert response.status_code == 200 and response.json() == jsonable_encoder(expected)
                assert len(statements) == expected_sql
                assert any('LATERAL' in sql for sql in statements) == (setting == '1')
                statements.clear()
                # A bound agent in the HTTP process still gets the old path.
                assert app.state.kernel.get_task(own['principal'], task_id) == expected
                assert len(statements) == 4 and all('LATERAL' not in sql for sql in statements)
            finally:
                event.remove(engine, 'before_cursor_execute', record)

            for url, kwargs, status, code in (
                (path, {}, 401, 'UNAUTHENTICATED'),
                (path, {'headers': headers(peer)}, 403, 'VIEW_DENIED'),
                ('/api/v1/tasks/' + str(uuid4()), {'headers': headers(own)}, 403, 'VIEW_DENIED'),
                ('/api/v1/tasks/invalid', {'headers': headers(own)}, 422, 'INVALID_ARGUMENT'),
            ):
                error = client.get(url, **kwargs)
                assert error.status_code == status and error.json()['code'] == code
                validate_contract('ErrorEnvelope', error.json())
                assert error.headers['cache-control'] == 'no-store'
            kernel.revoke_grant(book['principal'], own['principal'].grant_id)
            revoked = client.get(path, headers=headers(own))
            assert revoked.status_code == 401 and revoked.json()['code'] == 'UNAUTHENTICATED'
    finally:
        app.state.kernel.db.close()
