"""Compact delivery still uses the task owner's current grant and frozen role ceiling."""
import pytest
from sqlalchemy import text

from benchmarks.bound_stdio_throughput import provision
from novel_factory.application.audit_log import LogService
from novel_factory.application.bound_tools import invoke_bound_tool
from novel_factory.application.mcp_runtime import McpRuntime
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture
from novel_factory.interfaces.bindings import parse_binding, read_private_config


@pytest.mark.parametrize("name", ["nf_task_get", "nf_context_get"])
def test_compact_delivery_keeps_live_authority_and_evidence(kernel_env, tmp_path, monkeypatch, name):
    kernel = kernel_env["kernel"]
    book = load_fixture(kernel)
    role = provision(kernel, tmp_path, 0, world=book)
    config = read_private_config(role.path)
    owner = kernel.authenticate(config["token"])
    binding = parse_binding(config["binding"])
    runtime = McpRuntime(kernel, owner, binding, compact_role_reads=True)
    assert invoke_bound_tool(runtime, name, {}, transport="mcp") == role.expected[name]
    with pytest.raises(DomainError) as denied:
        invoke_bound_tool(runtime, name, {}, ceiling=set(), transport="mcp")
    assert denied.value.code == "VIEW_DENIED"

    original = LogService.begin_tool

    def revoke_after_start(*args, **kwargs):
        receipt = original(*args, **kwargs)
        kernel.revoke_grant(book["principal"], owner.grant_id)
        return receipt

    with monkeypatch.context() as patch:
        patch.setattr(LogService, "begin_tool", staticmethod(revoke_after_start))
        with pytest.raises(DomainError) as revoked:
            invoke_bound_tool(runtime, name, {}, transport="mcp")
    assert revoked.value.code == "VIEW_DENIED"
    with pytest.raises(DomainError) as malformed:
        invoke_bound_tool(runtime, name, {"unexpected": 1}, transport="mcp")
    assert malformed.value.code == "VIEW_DENIED"

    with kernel.db.uow(write=False) as repo:
        rows = repo.conn.execute(text("""
            SELECT a.event_type,a.error_code,a.evidence_id,o.topic FROM nf_audit_events a
            LEFT JOIN nf_outbox o ON o.dedupe_key='audit:' || a.event_id::text
            WHERE a.principal_id=:p AND a.tool_name=:tool ORDER BY a.event_time
        """), {"p": owner.principal_id, "tool": name}).all()
    assert [row.event_type for row in rows] == ["mcp.started", "mcp.finished"] * 4
    assert [row.error_code for row in rows[1::2]] == [None] + ["VIEW_DENIED"] * 3
    assert str(rows[1].evidence_id) == role.expected["nf_task_get"]["context_bundle_id"]
    assert all(row.topic == "audit.recorded" for row in rows)


def test_compact_audit_rejects_conflicting_outbox_and_rolls_back(kernel_env):
    from uuid import uuid4

    from novel_factory.application.audit_log import record

    kernel = kernel_env["kernel"]
    book = load_fixture(kernel)
    work = book["world_ref"]["work_id"]
    event_id = str(uuid4())
    with kernel.db.uow() as repo:
        repo.outbox("different.topic", "audit", event_id, {}, "audit:" + event_id)
    with pytest.raises(DomainError) as conflict:
        with kernel.db.uow() as repo:
            record(repo.conn, "mcp.started", event_id=event_id, work_id=work, compact_outbox=True)
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"
    with kernel.db.uow(write=False) as repo:
        assert repo.conn.scalar(text("SELECT count(*) FROM nf_audit_events WHERE event_id=:id"),
                                {"id": event_id}) == 0
