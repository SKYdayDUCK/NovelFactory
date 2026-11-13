"""Backfill, rollback, state transitions, authorization and fallback on actual PG."""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from test_hera_durable_admission import spec as task_spec

from novel_factory.application.mcp_runtime import McpRuntime, ProjectBinding
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point
from novel_factory.orchestration.progress import check_progress, configure_progress


def test_optional_projection_matches_existing_and_future_tasks(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    book = load_fixture(kernel)
    owner, wr = book["principal"], book["world_ref"]
    w, b = wr["work_id"], wr["branch_id"]
    writer = kernel.issue_grant(owner, w, role_id="writer", max_story_time=point(1))["principal"]
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
        assert check_progress(r.conn, w, b) == "OK"
        r.conn.execute(text("UPDATE nf_hera_progress SET counts='[]'::jsonb WHERE branch_id=:b"), {"b": b})
        assert check_progress(r.conn, w, b) == "MISMATCH"
    assert reader.project() == expected
    with kernel.db.uow() as r:
        r.conn.execute(text("UPDATE nf_hera_progress SET counts=CAST(:counts AS jsonb) WHERE branch_id=:b"),
                       {"b": b, "counts": '{"READY":10}'})
        assert check_progress(r.conn, w, b) == "MISMATCH"
        configure_progress(r.conn, w, b, enabled=True)
        assert check_progress(r.conn, w, b) == "OK"
    assert reader.project() == expected
    with kernel.db.uow() as r:
        configure_progress(r.conn, w, b, enabled=False)
        assert check_progress(r.conn, w, b) == "DISABLED"
    assert reader.project() == expected
    with kernel.db.uow() as r:
        configure_progress(r.conn, w, b, enabled=True)
        r.conn.execute(text("UPDATE nf_hera_progress SET counts=CAST(:counts AS jsonb) WHERE branch_id=:b"),
                       {"b": b, "counts": '{"READY":"invalid"}'})
    kernel.cancel_task(writer, second["task_id"], 0, "cancel-with-bad-counter")
    with kernel.db.uow() as r:
        assert check_progress(r.conn, w, b) == "DISABLED"
    assert reader.project()["counts"]["CANCELLED"] == 2
    kernel.revoke_grant(owner, resume.grant_id)
    with pytest.raises(DomainError, match="unavailable"):
        reader.project()


def test_operator_can_suspend_only_an_empty_projection_and_restore_from_tasks(kernel_env):
    from sqlalchemy.exc import IntegrityError

    from novel_factory.orchestration.progress import set_progress_maintenance

    kernel = kernel_env["kernel"]
    book = load_fixture(kernel)
    work, branch = book["world_ref"]["work_id"], book["world_ref"]["branch_id"]
    role = kernel.issue_grant(book["principal"], work, role_id="writer", max_story_time=point(1))["principal"]
    def enabled(conn):
        return conn.scalar(text("""SELECT tgenabled FROM pg_trigger
            WHERE tgrelid='nf_tasks'::regclass AND tgname='nf_hera_progress_delta'"""))

    with pytest.raises(RuntimeError), kernel.db.uow() as repo:
        set_progress_maintenance(repo.conn, suspended=True)
        assert enabled(repo.conn) == "D"
        raise RuntimeError("abort configuration")

    with kernel.db.uow() as repo:
        assert enabled(repo.conn) == "O"
        set_progress_maintenance(repo.conn, suspended=True)
        set_progress_maintenance(repo.conn, suspended=True)
    with pytest.raises(IntegrityError), kernel.db.uow() as repo:
        repo.conn.execute(text("INSERT INTO nf_hera_progress VALUES (:w,:b,'{}',0)"),
                          {"w":work,"b":branch})
    spec = task_spec(book, principal=role, role="writer")
    kernel.create_task(role,spec,"suspended-write")
    with kernel.db.uow() as repo:
        configure_progress(repo.conn,work,branch,enabled=True)
        assert enabled(repo.conn) == "O"
        assert check_progress(repo.conn,work,branch) == "OK"
    with pytest.raises(ValueError, match="Disable all"), kernel.db.uow() as repo:
        set_progress_maintenance(repo.conn,suspended=True)
    kernel.cancel_task(role,spec["task_id"],0,"resumed-write")
    with kernel.db.uow() as repo:
        assert check_progress(repo.conn,work,branch) == "OK"
        configure_progress(repo.conn,work,branch,enabled=False)
        set_progress_maintenance(repo.conn,suspended=True)
        set_progress_maintenance(repo.conn,suspended=False)
        assert enabled(repo.conn) == "O"
