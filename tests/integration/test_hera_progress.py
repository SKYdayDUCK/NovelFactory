"""Backfill, rollback, state transitions, authorization and fallback on actual PG."""
import pytest
from sqlalchemy import text

from novel_factory.application.mcp_runtime import McpRuntime, ProjectBinding
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture
from novel_factory.orchestration.progress import configure_progress
from test_hera_durable_admission import spec as task_spec
from datetime import UTC, datetime, timedelta


def test_optional_projection_matches_existing_and_future_tasks(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    book = load_fixture(kernel)
    owner, wr = book["principal"], book["world_ref"]
    w, b = wr["work_id"], wr["branch_id"]
    writer = kernel.issue_grant(owner, w, role_id="writer")["principal"]
    resume = kernel.issue_grant(owner, w, role_id="resume_operator", capabilities=["PROJECT_RESUME"],
                                branch_ids=[b], expires_at=datetime.now(UTC) + timedelta(hours=1))["principal"]
    reader = McpRuntime(kernel, resume, ProjectBinding(w, b))
    spec = task_spec(book, principal=writer, role="writer")
    kernel.create_task(writer, spec, "existing")
    baseline = reader.project()
    with kernel.db.uow() as r:
        configure_progress(r.conn, w, b, enabled=True)
    monkeypatch.setenv("NF_HERA_PROJECT_PROGRESS", "1")
    assert reader.project() == baseline
    second = task_spec(book, principal=writer, role="writer")
    kernel.create_task(writer, second, "after-backfill")
    kernel.cancel_task(writer, spec["task_id"], 0, "one-cancel")
    expected = reader.project()
    assert expected["counts"]["READY"] == expected["counts"]["CANCELLED"] == 1
    with pytest.raises(RuntimeError), kernel.db.uow() as r:
        r.conn.execute(text("UPDATE nf_tasks SET state='FAILED' WHERE task_id=:id"), {"id": second["task_id"]})
        assert r.conn.scalar(text("SELECT counts->>'FAILED' FROM nf_hera_progress WHERE branch_id=:b"), {"b":b}) == "1"
        raise RuntimeError("rollback")
    assert reader.project() == expected
    with kernel.db.uow() as r:
        configure_progress(r.conn, w, b, enabled=False)
    assert reader.project() == expected
    kernel.revoke_grant(owner, resume.grant_id)
    with pytest.raises(DomainError, match="unavailable"):
        reader.project()
