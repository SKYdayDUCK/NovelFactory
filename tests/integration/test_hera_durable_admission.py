"""Persistent capacity and expiry over actual PostgreSQL transactions and workers."""
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import json
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from novel_factory.application.kernel import Kernel
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.database import Database
from novel_factory.orchestration.workflow import WorkflowService

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def spec(world, *, principal=None, role="control_operator", kind="application", state="READY"):
    principal = principal or world["principal"]
    return {"schema_version": "2.0", "task_id": str(uuid4()), "generation": 0,
            "role_id": role, "task_kind": kind, "state": state, "world_ref": world["world_ref"],
            "story_time": point(1), "context_bundle_id": None, "policy_version": "nf-policy-1",
            "dependency_fingerprint": None, "runtime_contract_version": "0" * 64,
            "capability_grant_id": principal.grant_id, "input_artifacts": [],
            "budget": {"currency": "USD", "max_amount": "1", "token_budget": 100},
            "wall_deadline": (datetime.now(UTC) + timedelta(hours=1)).isoformat()}


@pytest.fixture(params=[False, True], ids=["scan", "exact"])
def policy(kernel_env, request):
    db = kernel_env["db"]

    with db.uow() as repo:
        repo.conn.execute(text("SELECT nf_hera_set_capacity_mode(:on)"), {"on": request.param})

    def configure(total=1000, work=1000, principal=1000, resource=1000):
        with db.uow() as repo:
            repo.conn.execute(text("""
                UPDATE nf_durable_admission_policy SET enabled=true,
                    total_limit=:total, work_limit=:work, principal_limit=:principal, resource_limit=:resource,
                    role_resources='{"writer":"CPU_COMPUTE"}'
            """), dict(total=total, work=work, principal=principal, resource=resource))

    yield configure
    with db.uow() as repo:
        repo.conn.execute(text("UPDATE nf_durable_admission_policy SET enabled=false"))
        repo.conn.execute(text("SELECT nf_hera_set_capacity_mode(false)"))


def test_racing_clients_retries_and_capacity_share_one_commit(kernel_env, policy):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    owner = world["principal"]
    tasks = [spec(world) for _ in range(8)]
    policy(total=2)
    start = Barrier(8)
    db2 = Database(kernel_env["url"], pool_size=4)
    other = Kernel(db2, kernel_env["store"])

    def submit(i):
        caller = (kernel, other)[i % 2]
        start.wait(8)
        try:
            return caller.create_task(owner, tasks[i], tasks[i]["task_id"])
        except DomainError as error:
            assert (error.code, error.retryable, error.recovery) == ("BACKPRESSURE", True, "WAIT")
            return None

    try:
        with ThreadPoolExecutor(8) as pool:
            results = list(pool.map(submit, range(8)))
        accepted = [r for r in results if r]
        assert len(accepted) == 2
        assert all(r["resource_class"] == "MODEL" and r["queue_deadline"] <= r["wall_deadline"]
                   for r in accepted)
        index = results.index(accepted[0])
        with ThreadPoolExecutor(8) as pool:
            replay = list(pool.map(lambda i: (kernel, other)[i % 2].create_task(
                owner, tasks[index], tasks[index]["task_id"]), range(8)))
        assert replay == [accepted[0]] * 8
        with pytest.raises(DomainError, match="different content"):
            other.create_task(owner, {**tasks[index], "state": "BLOCKED"}, tasks[index]["task_id"])
        for item in accepted:
            kernel.cancel_task(owner, item["task_id"], 0, "cancel-" + item["task_id"])
        rejected = tasks[results.index(None)]
        resumed = other.create_task(owner, rejected, rejected["task_id"])
        assert kernel.get_task(owner, resumed["task_id"])["state"] == "READY"
        other.cancel_task(owner, resumed["task_id"], 0, "release-" + resumed["task_id"])
        with kernel.db.uow(write=False) as repo:
            params = {"ids": [s["task_id"] for s in tasks]}
            assert repo.conn.scalar(text("SELECT count(*) FROM nf_durable_admissions WHERE task_id=ANY(CAST(:ids AS uuid[]))"),
                                    params) == 3
            assert repo.conn.scalar(text("SELECT count(*) FROM nf_outbox WHERE topic='task.changed'"
                                         " AND payload->>'state'='READY' AND aggregate_id=ANY(:ids)"), params) == 3
    finally:
        db2.close()


def test_existing_tasks_limits_and_entrypoint_identity(kernel_env, policy):
    k = kernel_env["kernel"]
    one, two = load_fixture(k), load_fixture(k)
    owners = one["principal"], two["principal"]
    granted = k.issue_grant(owners[0], one["world_ref"]["work_id"], role_id="writer",
                            capabilities=["READ", "TASK_CREATE"], max_story_time=point(5))
    writer = granted["principal"]
    initial = spec(one)
    k.create_task(owners[0], initial, "legacy")
    policy(principal=1)
    with pytest.raises(DomainError, match="capacity"):
        k.create_task(owners[0], spec(one), "denied-principal")
    fresh = spec(one, principal=writer, role="writer", kind="caller-controlled-name")
    assert k.create_task(writer, fresh, "accepted-writer")["resource_class"] == "CPU_COMPUTE"
    policy(work=2)
    with pytest.raises(DomainError, match="capacity"):
        k.create_task(writer, spec(one, principal=writer, role="writer"), "denied-work")
    policy(resource=1)
    with pytest.raises(DomainError, match="capacity"):
        k.create_task(owners[1], spec(two, kind="CPU_COMPUTE"), "denied-model-capacity")
    k.revoke_grant(owners[0], writer.grant_id)
    with pytest.raises(DomainError) as denied:
        k.create_task(writer, fresh, "accepted-writer")
    assert denied.value.code == "VIEW_DENIED"
    # Terminate the work via the internal lifecycle to release the module's isolated capacity.
    k.tasks.cancel(fresh["task_id"], 0, principal_id=writer.principal_id, idempotency_key="release")
    k.cancel_task(owners[0], initial["task_id"], 0, "release")


def test_age_and_expired_unclaimed_wait_survive_restart_and_disable(kernel_env, policy):
    k = kernel_env["kernel"]
    world = load_fixture(k)
    owner, wr = world["principal"], world["world_ref"]
    policy()
    invalid = dict(spec(world), wall_deadline=(datetime.now(UTC) + timedelta(days=8)).isoformat())
    with pytest.raises(DomainError, match="maximum age"):
        k.create_task(owner, invalid, "age")
    ids = []
    for state in ("READY", "BLOCKED"):
        task = spec(world, state=state)
        k.create_task(owner, task, state)
        ids.append(task["task_id"])
    # Inject a missed deadline without wall-clock sleeps. The wall deadline remains live.
    with k.db.uow() as repo:
        repo.conn.execute(text("""UPDATE nf_durable_admissions SET queue_deadline=statement_timestamp()
            WHERE task_id=ANY(CAST(:ids AS uuid[]))"""), {"ids": ids})
        repo.conn.execute(text("UPDATE nf_durable_admission_policy SET enabled=false"))
    restart_db = Database(kernel_env["url"])
    other = Kernel(restart_db, kernel_env["store"])
    try:
        assert other.claim_task(owner, "late", task_id=ids[0]) is None
        workflow = WorkflowService(other.tasks)
        def authorize(conn):
            from novel_factory.infrastructure.database import tables
            from novel_factory.infrastructure.repository import KernelRepository
            other.authorize(KernelRepository(conn, tables), owner, wr["work_id"], "ADMIN", wr["branch_id"])
        for count in (2, 0):
            result = workflow.resume_project(wr["work_id"], wr["branch_id"], authorize_in_tx=authorize)
            assert result["expired_ready"] == count
        assert all(other.get_task(owner, task)["state"] == "FAILED" for task in ids)
        with other.db.uow(write=False) as repo:
            assert repo.conn.scalar(text("""SELECT count(*) FROM nf_outbox
                WHERE topic='supervisor.task.expired' AND aggregate_id=ANY(:ids)"""), {"ids": ids}) == 2
            assert not repo.conn.scalar(text("SELECT count(*) FROM nf_task_executions WHERE task_id=ANY(CAST(:ids AS uuid[]))"),
                                        {"ids": ids})
    finally:
        restart_db.close()


def test_database_connection_loss_rolls_back_admission_and_retry_completes(kernel_env, policy, monkeypatch):
    """Kill the writing PostgreSQL backend after it emitted the event, before COMMIT."""
    from sqlalchemy.exc import DBAPIError

    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    owner = world["principal"]
    task = spec(world)
    policy(total=1)
    original = kernel.tasks._emit

    def disconnect(conn, *args):
        result = original(conn, *args)
        pid = conn.scalar(text("SELECT pg_backend_pid()"))
        with kernel_env["admin"].connect() as admin:
            assert admin.scalar(text("SELECT pg_terminate_backend(:pid, 2000)"), {"pid": pid})
        return result

    with monkeypatch.context() as injected:
        injected.setattr(kernel.tasks, "_emit", disconnect)
        with pytest.raises(DBAPIError):
            kernel.create_task(owner, task, "lost-before-commit")
    with kernel.db.uow(write=False) as repo:
        assert repo.one("tasks", {"task_id": task["task_id"]}) is None
        assert repo.one("task_commands", {"principal_id": owner.principal_id, "idempotency_key": "lost-before-commit"}) is None
        assert not repo.conn.scalar(text("SELECT EXISTS (SELECT FROM nf_durable_admissions WHERE task_id=:id)"),
                                    {"id": task["task_id"]})
        assert not repo.one("outbox", {"aggregate_id": task["task_id"]})
    receipt = kernel.create_task(owner, task, "lost-before-commit")
    assert kernel.create_task(owner, task, "lost-before-commit") == receipt
    kernel.cancel_task(owner, task["task_id"], 0, "release")


@pytest.mark.parametrize("invalid", [None, 1, [], ["MODEL"], {}, "INVALID"])
def test_reject_invalid_server_resource_classes(kernel_env, invalid):
    with pytest.raises(IntegrityError), kernel_env["db"].uow() as repo:
        repo.conn.execute(text("""
            UPDATE nf_durable_admission_policy SET role_resources=CAST(:mapping AS jsonb)
        """), {"mapping": json.dumps({"writer": invalid})})


def test_exact_backfill_remap_and_restart_use_existing_receipts(kernel_env):
    k = kernel_env["kernel"]
    world = load_fixture(k)
    owner = world["principal"]
    legacy = spec(world)
    legacy_id = legacy["task_id"]
    k.create_task(owner, legacy, "before-policy")
    frozen = spec(world)
    with k.db.uow() as repo:
        repo.conn.execute(text("UPDATE nf_durable_admission_policy SET enabled=true, resource_limit=5, total_limit=10, work_limit=10, principal_limit=10"))
    k.create_task(owner, frozen, "old-scan-receipt")
    with k.db.uow() as repo:
        repo.conn.execute(text("SELECT nf_hera_set_capacity_mode(true)"))
        repo.conn.execute(text("""UPDATE nf_durable_admission_policy
            SET role_resources=jsonb_build_object('control_operator','DB_WRITE'),resource_limit=1"""))
        scoped = dict(repo.conn.execute(text("""
            SELECT subject,used FROM nf_hera_capacity WHERE scope='resource' AND used>0
        """)).all())
        assert scoped == {"MODEL": 1, "DB_WRITE": 1}
    try:
        with pytest.raises(DomainError, match="capacity"):
            k.create_task(owner, spec(world), "denied-after-rebuild")
        k.cancel_task(owner, legacy_id, 0, "release-legacy")
        new = spec(world)
        k.create_task(owner, new, "reused-counter")
        k.cancel_task(owner, new["task_id"], 0, "release-new")
        k.cancel_task(owner, frozen["task_id"], 0, "release-old-receipt")
        with k.db.uow() as repo:
            assert repo.conn.scalar(text("SELECT sum(used) FROM nf_hera_capacity")) == 0
    finally:
        with k.db.uow() as repo:
            repo.conn.execute(text("SELECT nf_hera_set_capacity_mode(false)"))
            repo.conn.execute(text("UPDATE nf_durable_admission_policy SET enabled=false, role_resources='{}'"))


def test_saturated_restore_preserves_stale_generation_and_fences_live_task(kernel_env):
    from novel_factory.infrastructure.recovery import fence_recovered_tasks

    k = kernel_env["kernel"]
    world = load_fixture(k)
    owner = world["principal"]
    stale, live = spec(world), spec(world)
    k.create_task(owner, stale, "restore-old")
    with k.db.uow() as r:
        r.conn.execute(text("UPDATE nf_tasks SET state='STALE' WHERE task_id=:id"), {"id": stale["task_id"]})
        r.conn.execute(text("UPDATE nf_durable_admission_policy SET enabled=true, total_limit=1"))
        r.conn.execute(text("SELECT nf_hera_set_capacity_mode(true)"))
    try:
        k.create_task(owner, live, "restore-live")
        fenced = fence_recovered_tasks(k.db.engine, k.objects)
        assert [(item["task_id"], item["state"], item["generation"]) for item in fenced] == [
            (live["task_id"], "PAUSED", 1)]
        with pytest.raises(DomainError) as denied, k.db.uow() as r:
            # A separate, authorized repair that later needs a slot must retry.
            r.conn.execute(text("UPDATE nf_tasks SET state='PAUSED' WHERE task_id=:id"), {"id": stale["task_id"]})
        assert (denied.value.code, denied.value.retryable, denied.value.recovery) == ("BACKPRESSURE", True, "WAIT")
        with k.db.uow(write=False) as r:
            task = r.one("tasks", {"task_id": stale["task_id"]})
            assert (task["state"], task["generation"]) == ("STALE", 0)
            assert r.conn.scalar(text("SELECT used FROM nf_hera_capacity WHERE scope='total'")) == 1
    finally:
        with k.db.uow() as r:
            r.conn.execute(text("SELECT nf_hera_set_capacity_mode(false)"))
            r.conn.execute(text("UPDATE nf_durable_admission_policy SET enabled=false"))
