"""Verify the singleton work-lock join under an actual concurrent grant revocation."""
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from benchmarks.p59_db_candidates import task_fixture
from experiments.db_execution.fenced_status import FencedStatusKernel
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture


def test_singleton_status_holds_work_lock_and_rechecks_grant(kernel_env):
    base = kernel_env['kernel']
    candidate = FencedStatusKernel(base.db, base.objects)
    a, b = (load_fixture(base) for _ in range(2))
    owner = base.issue_grant(a['principal'], a['world_ref']['work_id'], role_id='writer')['principal']
    task = task_fixture(base, a, owner, 'writer', 0).task_id
    assert candidate.get_task(owner, task.upper()) == base.get_task(owner, task)
    for principal, task_id in ((b['principal'], task), (owner, str(uuid4()))):
        with pytest.raises(DomainError) as error:
            candidate.get_task(principal, task_id)
        assert error.value.code == 'VIEW_DENIED'
    with ThreadPoolExecutor(max_workers=1) as pool:
        with base.db.engine.begin() as holder:
            holder.execute(text('UPDATE nf_grants SET active=false WHERE grant_id=:id'), {'id': owner.grant_id})
            holder_pid = holder.scalar(text('SELECT pg_backend_pid()'))
            pending = pool.submit(candidate.get_task, owner, task)
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
