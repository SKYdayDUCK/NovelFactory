"""R1-A04/H05: true asynchronous materialization and abrupt worker recovery."""
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

import pytest
from sqlalchemy import func, text

from novel_factory.application.derived_worker import CONSUMER, DerivedWorldWorker
from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import (
    create_bound_task,
    load_fixture,
    point,
    prepare_command,
    run_command,
)
from novel_factory.infrastructure.database import Database, tables
from novel_factory.infrastructure.objects import ObjectStore

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def _crash_worker(url, object_root, stage):
    kernel = Kernel(Database(url), ObjectStore(object_root))
    def fail(current):
        if current == stage:
            os._exit(74)
    DerivedWorldWorker(kernel).run_once("crash-worker", fault=fail)


def _expire(kernel, message_id):
    outbox = tables["outbox"]
    with kernel.db.engine.begin() as conn:
        conn.execute(outbox.update().where(outbox.c.outbox_id == message_id).values(
            lease_expiry=func.clock_timestamp() - text("interval '1 second'")))


def _drain(kernel):
    worker = DerivedWorldWorker(kernel)
    for _ in range(100):
        if worker.run_once("clean-fixture") is None:
            return
    raise AssertionError("Fixture created more than 100 pending world messages")


def _transfer(kernel, fixture, *, at=30, amount="30"):
    result = run_command(kernel, fixture["principal"], fixture["world_ref"], "RESOURCE_TRANSFER", {
        "from_account": fixture["ids"]["warehouse"], "to_account": fixture["ids"]["relief"],
        "resource_id": fixture["ids"]["grain"], "unit": "bag", "quantity": amount, "at": point(at)})
    return {**fixture, "world_ref": result["world_ref"]}


@pytest.mark.fault
@pytest.mark.parametrize("stage,committed", [
    ("after_object_before_consume", False),
    ("after_artifact_before_watermark", False),
    ("after_watermark_before_commit", False),
    ("after_consume_before_ack", True),
])
def test_abrupt_worker_exit_recovers_atomic_inbox_artifact_and_watermarks(kernel_env, stage, committed):
    kernel = kernel_env["kernel"]
    _drain(kernel)
    fixture = _transfer(kernel, load_fixture(kernel, "white_bridge"))
    wr = fixture["world_ref"]
    with kernel.db.uow(write=False) as repo:
        before = kernel.revision(repo, wr)
        message = repo.one("outbox", {"dedupe_key": "world:" + wr["world_revision_id"]})
    process = multiprocessing.get_context("spawn").Process(target=_crash_worker,
        args=(kernel_env["url"], str(kernel_env["store"].root), stage))
    process.start(); process.join(timeout=15)
    assert process.exitcode == 74
    with kernel.db.uow(write=False) as repo:
        inbox = repo.one("inbox", {"consumer": CONSUMER, "message_id": message["outbox_id"]})
        watermarks = repo.many("index_watermarks", {"branch_id": wr["branch_id"]})
        artifacts = repo.many("artifacts", {"work_id": wr["work_id"], "kind": "DERIVED_WORLD_INDEX"})
        assert bool(inbox) is committed
        assert len(watermarks) == (2 if committed else 0)
        assert len(artifacts) == (1 if committed else 0)
        assert repo.one("outbox", {"outbox_id": message["outbox_id"]})["state"] == "DELIVERING"
    _expire(kernel, message["outbox_id"])
    result = DerivedWorldWorker(kernel).run_once("replacement-worker")
    assert result["consumed"] is not committed
    with kernel.db.uow(write=False) as repo:
        assert repo.one("outbox", {"outbox_id": message["outbox_id"]})["state"] == "DELIVERED"
        assert len(repo.many("artifacts", {"work_id": wr["work_id"], "kind": "DERIVED_WORLD_INDEX"})) == 1
        assert all(row["world_revision_id"] == wr["world_revision_id"] and row["generation"] == 1
                   for row in repo.many("index_watermarks", {"branch_id": wr["branch_id"]}))
        assert kernel.revision(repo, wr) == before
        assert kernel.head(repo, wr["work_id"]) == wr
        assert len(repo.many("events", {"work_id": wr["work_id"]})) == 1


def test_out_of_order_materialization_never_regresses_watermark(kernel_env):
    kernel = kernel_env["kernel"]
    _drain(kernel)
    first = _transfer(kernel, load_fixture(kernel, "white_bridge"))
    second = _transfer(kernel, first, at=31, amount="10")
    worker = DerivedWorldWorker(kernel)
    older = worker.outbox.claim("older", topic="world.accepted")
    newer = worker.outbox.claim("newer", topic="world.accepted")
    assert worker.process_claimed(newer, "newer")["activated"]
    assert not worker.process_claimed(older, "older")["activated"]
    with kernel.db.uow(write=False) as repo:
        marks = repo.many("index_watermarks", {"branch_id": first["world_ref"]["branch_id"]})
        assert all(row["world_revision_id"] == second["world_ref"]["world_revision_id"] for row in marks)
        assert len(repo.many("artifacts", {"work_id": first["world_ref"]["work_id"], "kind": "DERIVED_WORLD_INDEX"})) == 2
        assert kernel.head(repo, first["world_ref"]["work_id"]) == second["world_ref"]


def test_duplicate_consumers_and_stale_lease_cannot_double_materialize(kernel_env):
    kernel = kernel_env["kernel"]
    _drain(kernel)
    fixture = _transfer(kernel, load_fixture(kernel, "white_bridge"))
    worker = DerivedWorldWorker(kernel)
    expired = worker.outbox.claim("expired", topic="world.accepted")
    _expire(kernel, expired["outbox_id"])
    lease = worker.outbox.claim("current", topic="world.accepted")
    with pytest.raises(DomainError) as error:
        worker.process_claimed(expired, "expired")
    assert error.value.code == "LEASE_EXPIRED"
    with ThreadPoolExecutor(max_workers=2) as threads:
        results = list(threads.map(lambda _: worker.process_claimed(lease, "current"), range(2)))
    assert sorted(result["consumed"] for result in results) == [False, True]
    assert results[0]["artifact_ref"] == results[1]["artifact_ref"]
    with kernel.db.uow(write=False) as repo:
        assert len(repo.many("artifacts", {"work_id": fixture["world_ref"]["work_id"], "kind": "DERIVED_WORLD_INDEX"})) == 1
        assert all(row["generation"] == 1 for row in repo.many("index_watermarks", {"branch_id": fixture["world_ref"]["branch_id"]}))


def test_candidate_branch_index_cannot_replace_canon_or_other_branch_index(kernel_env):
    kernel = kernel_env["kernel"]
    _drain(kernel)
    main = _transfer(kernel, load_fixture(kernel, "white_bridge"))
    candidate_wr = kernel.fork(main["principal"], main["world_ref"], "CANDIDATE", "index-branch")
    candidate = _transfer(kernel, {**main, "world_ref": candidate_wr}, at=31, amount="10")
    worker = DerivedWorldWorker(kernel)
    first = worker.outbox.claim("main", topic="world.accepted")
    second = worker.outbox.claim("candidate", topic="world.accepted")
    worker.process_claimed(second, "candidate")
    worker.process_claimed(first, "main")
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, main["world_ref"]["work_id"]) == main["world_ref"]
        for fixture in (main, candidate):
            assert all(row["world_revision_id"] == fixture["world_ref"]["world_revision_id"]
                       for row in repo.many("index_watermarks", {"branch_id": fixture["world_ref"]["branch_id"]}))


def test_later_artifact_invalidation_cannot_change_fixed_world_index_identity(kernel_env):
    kernel = kernel_env["kernel"]
    _drain(kernel)
    fixture = _transfer(kernel, load_fixture(kernel, "white_bridge"))
    worker = DerivedWorldWorker(kernel)
    lease = worker.outbox.claim("artifact-race", topic="world.accepted")
    original = worker.process_claimed(lease, "artifact-race")
    wr = fixture["world_ref"]
    # Plan/release publication is allowed to add a newer scope invalidation at
    # the same WorldRef. It must not change an immutable world-index payload.
    with kernel.db.uow() as repo:
        scope = "ARTIFACT_SELECTION:fixture-new-release"
        bumped = repo.bump(wr["work_id"], wr["branch_id"], [scope])
        repo.add("invalidations", {"invalidation_id": str(uuid4()), **wr,
                                    "scope_key": scope, "version": bumped[scope]})
    duplicate = worker.process_claimed(lease, "artifact-race")
    assert not duplicate["consumed"]
    assert duplicate["artifact_ref"] == original["artifact_ref"]


def test_worker_takes_work_before_branch_to_coexist_with_runtime_upgrade(kernel_env):
    kernel = kernel_env["kernel"]
    _drain(kernel)
    fixture = _transfer(kernel, load_fixture(kernel, "white_bridge"))
    worker = DerivedWorldWorker(kernel)
    lease = worker.outbox.claim("lock-order", topic="world.accepted")
    arrived, has_branch = Event(), Event()

    def observe(stage):
        if stage == "before_work_lock":
            arrived.set()
        elif stage == "after_branch_lock":
            has_branch.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with kernel.db.uow() as repo:
            repo.one("works", {"work_id": fixture["world_ref"]["work_id"]}, lock="update")
            pending = pool.submit(worker.process_claimed, lease, "lock-order", fault=observe)
            assert arrived.wait(timeout=5)
            # A canon/runtime administrator already owns the work update lock.
            # The worker must wait before locking the branch, so the admin can
            # take the branch next without a work/branch lock cycle.
            assert not has_branch.wait(timeout=0.25)
            repo.one("branches", {"branch_id": fixture["world_ref"]["branch_id"]}, lock="update")
        assert pending.result(timeout=10)["consumed"]
    assert has_branch.is_set()


def test_delayed_index_is_explicitly_stale_and_dependency_guard_does_not_wait_for_worker(kernel_env):
    kernel = kernel_env["kernel"]
    _drain(kernel)
    initial = load_fixture(kernel, "white_bridge")
    task = create_bound_task(kernel, initial["principal"], initial["world_ref"], point(30),
                             [initial["ids"]["warehouse"]], ["lifecycle"])
    pending = prepare_command(kernel, initial["principal"], initial["world_ref"], "RESOURCE_TRANSFER", {
        "from_account": initial["ids"]["warehouse"], "to_account": initial["ids"]["relief"],
        "resource_id": initial["ids"]["grain"], "unit": "bag", "quantity": "5", "at": point(30)},
        task=task, mode="DEPENDENCY_GUARDED")
    first = _transfer(kernel, initial)
    worker = DerivedWorldWorker(kernel)
    assert worker.dependency_impacts(initial["principal"], first["world_ref"])["status"] == "INDEX_PENDING"
    with pytest.raises(DomainError) as error:
        kernel.accept(initial["principal"], pending["request"])
    assert error.value.code == "DEPENDENCY_CONFLICT"
    worker.run_once("first-index")
    impact = worker.dependency_impacts(initial["principal"], first["world_ref"])
    assert impact["status"] == "READY" and impact["complete"]
    assert any(item["owner_type"] == "context" for item in impact["impacts"])
    second = _transfer(kernel, first, at=31, amount="10")
    query_task = create_bound_task(kernel, initial["principal"], second["world_ref"], point(32),
                                   [initial["ids"]["warehouse"]], ["lifecycle"])
    request = {"schema_version": "2.1", "task_id": query_task["task_id"], "generation": query_task["generation"],
        "read_contract": {"revision": {"mode": "FIXED", "world_ref": second["world_ref"]},
            "process_policy": "ALLOW_PARTIAL", "index_policy": "ALLOW_STALE_SEARCH", "max_wait_ms": 0},
        "knowledge_cut": {"plane": "WORLD"}, "subject_time": {"kind": "POINT", "at": point(31)},
        "query": {"kind": "TIMELINE", "subject_ids": [initial["ids"]["warehouse"]],
            "entry_kinds": ["FACT"], "time_axis": "SOURCE", "include_inactive": False}, "page_size": 100, "cursor": None}
    service = WorldQueryService(kernel)
    stale = service.query(initial["principal"], request)
    assert stale["freshness"] == "STALE" and stale["payload"]["coverage"] == "PARTIAL"
    worker.run_once("second-index")
    assert service.query(initial["principal"], request)["freshness"] == "CURRENT"
    actor = kernel.issue_grant(initial["principal"], initial["world_ref"]["work_id"], role_id="character_actor",
        actor_id=initial["ids"]["friend"], planes=["BELIEF"], capabilities=["READ"], max_story_time=point(100))["principal"]
    with pytest.raises(DomainError) as error:
        worker.dependency_impacts(actor, second["world_ref"])
    assert error.value.code == "VIEW_DENIED"
    for actor_id, horizon in [(initial["ids"]["friend"], point(100)), (None, point(0))]:
        restricted = kernel.issue_grant(initial["principal"], initial["world_ref"]["work_id"],
            role_id="control_operator", actor_id=actor_id, planes=["WORLD"],
            capabilities=["READ", "BRANCH", "ADMIN"], max_story_time=horizon)["principal"]
        with pytest.raises(DomainError) as denied:
            worker.dependency_impacts(restricted, second["world_ref"])
        assert denied.value.code == "VIEW_DENIED"
