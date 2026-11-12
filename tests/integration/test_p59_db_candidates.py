"""Real PG cases for set-wise reads, queue cancellation and read-only failure recovery."""
import asyncio
from dataclasses import replace
from threading import Event
from concurrent.futures import ThreadPoolExecutor
import time
from uuid import uuid4

import pytest
from sqlalchemy import event, text

from benchmarks.p59_db_candidates import task_fixture
from experiments.db_execution import coordinator as module
from experiments.db_execution.coordinator import Coordinator
from experiments.db_execution.operations import LeaseRenewal, TaskStatus, execute
from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import Kernel
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture


def setup_pair(kernel):
    books = [load_fixture(kernel) for _ in range(2)]
    tasks = []
    for book in books:
        identity = kernel.issue_grant(book["principal"], book["world_ref"]["work_id"], role_id="writer")["principal"]
        tasks.append(task_fixture(kernel, book, identity, "writer", 0))
    return books, tasks


@pytest.mark.parametrize("joined", [False, True])
def test_status_result_equivalence_and_partial_denials(kernel_env, joined):
    kernel = kernel_env["kernel"]
    books, (first, second) = setup_pair(kernel)
    unknown = str(uuid4())
    foreign = TaskStatus(first.principal, second.task_id)
    impostor = replace(first, principal=Principal(first.principal.grant_id, str(uuid4())))
    wrong_grant = replace(first, principal=Principal(str(uuid4()), first.principal.principal_id))
    peer = kernel.issue_grant(books[0]["principal"], books[0]["world_ref"]["work_id"], role_id="writer")["principal"]
    requests = [first, second, foreign, impostor, wrong_grant, TaskStatus(peer, first.task_id),
                replace(first, task_id=unknown), replace(first, task_id=first.task_id.upper()),
                replace(first, task_id="{" + first.task_id.replace("-", "") + "}")]
    results = execute(kernel, requests, join_input=joined, grouped=True)
    singleton = Kernel(kernel.db, kernel.objects, joined_task_status=joined)
    for i, operation in enumerate(requests):
        operation.validate()
        try:
            expected = kernel.get_task(operation.principal, operation.task_id)
        except DomainError as err:
            assert isinstance(results[i], DomainError)
            assert results[i].code == err.code == "VIEW_DENIED"
            with pytest.raises(DomainError) as failure:
                singleton.get_task(operation.principal, operation.task_id)
            assert failure.value.code == err.code
        else:
            assert results[i] == expected == singleton.get_task(operation.principal, operation.task_id)
            assert {"grant_id", "principal_id", "lease_owner"}.isdisjoint(results[i])
    results[0]["world_ref"]["work_id"] = "mutated"
    assert results[-1]["world_ref"] == books[0]["world_ref"]

    # A new generation samples the corresponding immutable TaskSpec, including
    # after the incumbent cancellation command. Status reads are side-effect free.
    claim = kernel.claim_task(first.principal, "fixture-worker", task_id=first.task_id)
    kernel.cancel_task(first.principal, first.task_id, claim["generation"], "end-generation")
    current = execute(kernel, [first], join_input=joined, grouped=True)[0]
    assert current == kernel.get_task(first.principal, first.task_id)
    assert current["state"] == "CANCELLED" and current["generation"] > 0


def test_revoke_while_pending_backpressure_cancel_and_recovery(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    books, (first, second) = setup_pair(kernel)
    started, release = Event(), Event()
    original = module.execute
    dispatched = []

    def hold_first(*args, **kwargs):
        dispatched.extend(args[1])
        if not started.is_set():
            started.set()
            if not release.wait(10):
                raise RuntimeError("Test dispatch gate failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "execute", hold_first)

    async def run():
        async with Coordinator(kernel, workers=1, capacity=4, batch=8, split_queues=True) as channel:
            try:
                blocker = asyncio.create_task(channel.submit(second))
                assert await asyncio.to_thread(started.wait, 5)
                revoked = asyncio.create_task(channel.submit(first))
                abandoned = asyncio.create_task(channel.submit(first))
                expired = asyncio.create_task(channel.submit(second, timeout=.01))
                good = asyncio.create_task(channel.submit(second))
                await asyncio.sleep(0)
                with pytest.raises(DomainError, match="queue is full"):
                    await channel.submit(second)
                with pytest.raises(TypeError):
                    await channel.submit(("SQL", "arbitrary"))
                with pytest.raises(DomainError, match="Invalid bound identity"):
                    await channel.submit(replace(first, task_id="invalid"))
                abandoned.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await abandoned
                with pytest.raises(TimeoutError):
                    await expired
                kernel.revoke_grant(books[0]["principal"], first.principal.grant_id)
            finally:
                release.set()
            with pytest.raises(DomainError) as failure:
                await revoked
            assert failure.value.code == "VIEW_DENIED"
            assert await good == await blocker == kernel.get_task(second.principal, second.task_id)
            assert dispatched.count(first) == 1 and dispatched.count(second) == 2
            assert channel.counts["peak_queued"] <= 4 and channel.counts["peak_executing"] == 1
        with pytest.raises(DomainError, match="closed"):
            await channel.submit(second)
        async with Coordinator(kernel, workers=2) as restarted:
            assert await restarted.submit(second) == kernel.get_task(second.principal, second.task_id)

    asyncio.run(run())


def test_lease_write_unknown_is_never_retried(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    _, (status, _) = setup_pair(kernel)
    worker = "trusted-supervisor-" + "w" * 300
    claim = kernel.claim_task(status.principal, worker, task_id=status.task_id)
    write = LeaseRenewal(status.task_id, claim["generation"], worker, lease_seconds=3600)
    started, release = Event(), Event()
    calls = []

    def delay_write(*args, **kwargs):
        calls.append(type(args[1][0]).__name__)
        if type(args[1][0]) is LeaseRenewal:
            started.set()
            assert release.wait(5)
        return execute(*args, **kwargs)

    monkeypatch.setattr(module, "execute", delay_write)

    async def run():
        async with Coordinator(kernel, workers=1, batch=8, split_queues=True) as channel:
            try:
                renewal = asyncio.create_task(channel.submit(write, timeout=1.0))
                assert await asyncio.to_thread(started.wait, 2)
                with pytest.raises(DomainError) as failure:
                    await renewal
                assert failure.value.code == "OUTCOME_UNKNOWN"
            finally:
                release.set()
            status_after = await channel.submit(status)
            assert status_after["lease_expiry"] > claim["lease_expiry"]
            assert status_after["generation"] == claim["generation"]
            assert calls.count("LeaseRenewal") == 1
        with pytest.raises(ValueError, match="share a transaction"):
            execute(kernel, [write, status], join_input=True, grouped=True)

    asyncio.run(run())


def test_database_error_rolls_back_all_and_later_request_succeeds(kernel_env):
    kernel = kernel_env["kernel"]
    _, operations = setup_pair(kernel)
    seen = []

    def fail_once(conn, cursor, sql, params, *_):
        if not seen and "nf_grants" in sql and "FOR SHARE" in sql:
            seen.append(True)
            # A real error aborts this backend transaction. This uses only the
            # test-owned database connection, without interrupting another client.
            return "SELECT 1/0", ()
        return sql, params

    async def run():
        async with Coordinator(kernel, workers=1, batch=8, join_input=True) as channel:
            event.listen(kernel.db.engine, "before_cursor_execute", fail_once, retval=True)
            try:
                result = await asyncio.gather(*(channel.submit(op) for op in operations), return_exceptions=True)
                assert seen and all(isinstance(e, DomainError) and e.code == "UNAVAILABLE" for e in result)
            finally:
                event.remove(kernel.db.engine, "before_cursor_execute", fail_once)
            assert await channel.submit(operations[0]) == kernel.get_task(operations[0].principal, operations[0].task_id)
            assert channel.counts["db_errors"] == 1

    asyncio.run(run())

def test_concurrent_grant_lock_rechecks_after_wait_in_both_orders(kernel_env):
    kernel = kernel_env["kernel"]
    books, (first, second) = setup_pair(kernel)
    changed, release = Event(), Event()

    def hold_update(conn, cursor, statement, parameters, context, many):
        if statement.startswith("UPDATE nf_grants"):
            changed.set()
            if not release.wait(12):
                raise TimeoutError("Grant-update test gate timed out")

    event.listen(kernel.db.engine, "after_cursor_execute", hold_update)
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            revoke = pool.submit(kernel.revoke_grant, books[0]["principal"], first.principal.grant_id)
            try:
                assert changed.wait(3)
                batches = [[first, second], [second, first]]
                futures = [pool.submit(execute, kernel, batch, join_input=True, grouped=True) for batch in batches]
                # Observe both real readers waiting on the uncommitted grant.
                deadline = time.monotonic() + 7
                while time.monotonic() < deadline:
                    with kernel.db.uow(write=False) as repo:
                        waiting = repo.conn.scalar(text("""SELECT count(*) FROM pg_stat_activity
                            WHERE datname=current_database() AND pid<>pg_backend_pid()
                            AND wait_event_type='Lock' AND query LIKE '%nf_grants%FOR SHARE%'"""))
                    if waiting == 2:
                        break
                    time.sleep(.01)
                assert waiting == 2
            finally:
                release.set()
            revoke.result(timeout=4)
            for batch, future in zip(batches, futures, strict=True):
                for op, value in zip(batch, future.result(timeout=4), strict=True):
                    if op == first:
                        assert isinstance(value, DomainError) and value.code == "VIEW_DENIED"
                    else:
                        assert value == kernel.get_task(second.principal, second.task_id)
    finally:
        event.remove(kernel.db.engine, "after_cursor_execute", hold_update)

def test_hybrid_size_one_keeps_singleton_join(kernel_env):
    kernel = kernel_env["kernel"]
    _, (op, _) = setup_pair(kernel)
    reference = kernel.get_task(op.principal, op.task_id)
    statements = []

    def record(conn, cursor, statement, *_):
        statements.append(statement)

    async def run():
        async with Coordinator(kernel, workers=1, batch=1, join_input=True) as channel:
            event.listen(kernel.db.engine, "before_cursor_execute", record)
            try:
                assert await channel.submit(op) == reference
            finally:
                event.remove(kernel.db.engine, "before_cursor_execute", record)

    asyncio.run(run())
    assert len(statements) == 3
    assert "JOIN nf_task_inputs" in statements[0]
    assert all("FOR SHARE" in sql for sql in statements[1:])
