"""Existing invocation gates, live grants, per-request audit and same-binding reads."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from threading import Barrier, Event

import pytest
from sqlalchemy import text

from benchmarks import p59_role_read_group
from novel_factory.application.bound_tools import invoke_bound_tool
from novel_factory.domain.errors import DomainError
from test_p59_mcp_authority import actor_read  # noqa: F401

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_mixed_results_validator_and_audits(actor_read):
    kernel, _, _, runtime = actor_read
    task = runtime.task_get()
    context = runtime.context_get()
    reader = p59_role_read_group.StaticReadGroup(runtime, window=1)
    rendezvous = Barrier(2)

    def read(name):
        rendezvous.wait(timeout=5)
        return reader.read(name)

    runtime.task_get = lambda: read("nf_task_get")
    runtime.context_get = lambda: read("nf_context_get")
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(invoke_bound_tool, runtime, "nf_task_get", {}, request_id=10)
        b = pool.submit(invoke_bound_tool, runtime, "nf_context_get", {}, request_id=11)
        assert a.result(timeout=10) == task and b.result(timeout=10) == context
    # An invalid item's payload must never enter the shared read.
    with pytest.raises(DomainError) as bad:
        invoke_bound_tool(runtime, "nf_context_get", {"generation": 100}, request_id=12)
    assert bad.value.code == "INVALID_ARGUMENT" and reader.shapes == {2: 1}
    with kernel.db.uow(write=False) as repo:
        pairs = repo.conn.execute(text("""
            SELECT a.request_id,f.outcome,f.error_code FROM nf_audit_events a JOIN nf_audit_events f
            ON f.parent_span_id=a.event_id AND f.causation_id=a.event_id
            WHERE a.event_type='local_tool.started' AND f.event_type='local_tool.finished'
            AND a.task_id=:task ORDER BY a.event_time
        """), {"task": task["task_id"]}).all()
    assert sorted(pairs) == sorted((sha256(str(i).encode()).hexdigest(), status, code)
                                   for i, status, code in [(10, "OK", None), (11, "OK", None),
                                                           (12, "ERROR", "INVALID_ARGUMENT")])


def test_queued_member_rechecks_grant_before_batch(actor_read):
    kernel, world, role, runtime = actor_read
    reader = p59_role_read_group.StaticReadGroup(runtime, window=1)
    captured = Event()
    release = Event()
    original = reader._execute

    def held(group):
        assert len(group) == 2
        captured.set()
        assert release.wait(5)
        original(group)

    reader._execute = held
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(reader.read, "nf_task_get")
        b = pool.submit(reader.read, "nf_context_get")
        assert captured.wait(5)
        kernel.revoke_grant(world["principal"], role.principal.grant_id)
        release.set()
        for result in (a, b):
            with pytest.raises(DomainError) as error:
                result.result(timeout=5)
            assert error.value.code == "VIEW_DENIED"


def test_dynamic_binding_falls_back_to_individual_methods(actor_read, monkeypatch):
    _, _, _, runtime = actor_read
    calls = []
    runtime.task_get = lambda: calls.append("task") or {"kind": "task"}
    runtime.context_get = lambda: calls.append("context") or {"kind": "context"}
    reader = p59_role_read_group.StaticReadGroup(runtime, window=1)
    monkeypatch.setattr(p59_role_read_group, "assignment_from_inputs", lambda inputs: "remote-witness")
    with ThreadPoolExecutor(2) as pool:
        a = pool.submit(reader.read, "nf_task_get")
        b = pool.submit(reader.read, "nf_context_get")
        assert a.result(timeout=5) == {"kind": "task"}
        assert b.result(timeout=5) == {"kind": "context"}
    assert sorted(calls) == ["context", "task"] and reader.shapes == {2: 1}
