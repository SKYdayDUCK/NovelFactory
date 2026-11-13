"""A valid fused read must still see a revocation committed after its start audit."""
import pytest
from sqlalchemy import text

from novel_factory.application.audit_log import LogService
from novel_factory.application.bound_tools import invoke_bound_tool
from novel_factory.application.mcp_runtime import McpRuntime
from novel_factory.application.task_operations import TaskOperationsBinding
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture
from test_http_fenced_task_status import ready_task


@pytest.mark.parametrize("fenced", [False, True])
def test_live_status_authorization_with_audit(kernel_env, monkeypatch, fenced):
    kernel = kernel_env["kernel"]
    book = load_fixture(kernel)
    wr = book["world_ref"]
    owner = kernel.issue_grant(book["principal"], wr["work_id"], role_id="writer")["principal"]
    task_id = ready_task(kernel, book, owner)
    runtime = McpRuntime(kernel, owner, TaskOperationsBinding(wr["work_id"], wr["branch_id"]),
                         fenced_task_status=fenced, fuse_status_auth=True)
    arguments = {"task_id": task_id}
    name = "nf_task_status"
    assert invoke_bound_tool(runtime, name, arguments, transport="mcp")["state"] == "READY"
    with pytest.raises(DomainError, match="unavailable") as ceiling:
        invoke_bound_tool(runtime, name, arguments, ceiling=set(), transport="mcp")
    assert ceiling.value.code == "VIEW_DENIED"

    begin = LogService.begin_tool

    def revoke_after_start(*args, **kwargs):
        receipt = begin(*args, **kwargs)
        kernel.revoke_grant(book["principal"], owner.grant_id)
        return receipt

    with monkeypatch.context() as patch:
        patch.setattr(LogService, "begin_tool", staticmethod(revoke_after_start))
        with pytest.raises(DomainError) as denied:
            invoke_bound_tool(runtime, name, arguments, transport="mcp")
        assert denied.value.code == "VIEW_DENIED"
    with pytest.raises(DomainError) as malformed:
        invoke_bound_tool(runtime, name, {}, transport="mcp")
    assert malformed.value.code == "VIEW_DENIED"
    with kernel.db.uow(write=False) as repo:
        rows = repo.conn.execute(text("""
            SELECT a.event_type, a.error_code, o.topic
            FROM nf_audit_events a LEFT JOIN nf_outbox o ON o.dedupe_key='audit:' || a.event_id::text
            WHERE a.principal_id=:p AND a.tool_name=:tool ORDER BY a.event_time
        """), {"p": owner.principal_id, "tool": name}).all()
    assert [row.event_type for row in rows] == ["mcp.started", "mcp.finished"] * 4
    assert [row.error_code for row in rows if row.event_type == "mcp.finished"] == [
        None, "VIEW_DENIED", "VIEW_DENIED", "VIEW_DENIED"]
    assert all(row.topic == "audit.recorded" for row in rows)
