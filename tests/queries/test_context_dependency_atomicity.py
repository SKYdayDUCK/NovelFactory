"""P3-01 candidate: complete dependency scope and atomic compiler persistence.

These tests run unchanged before and after batching dependency inserts. They
exercise persisted safety properties, including a real transaction rollback
after PostgreSQL has executed a dependency insert.
"""
from copy import deepcopy

import pytest
from sqlalchemy import event

from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError

from .conftest import compile_request, p, seed, start, uid

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_context_preserves_positive_negative_alias_and_coverage_dependencies(query_env):
    world = seed(query_env)
    kernel, service = query_env["kernel"], query_env["query"]
    absent = uid()
    subjects = [world["ids"]["sword"], world["ids"]["P"], absent]
    task = start(query_env, world, at=p(35), subjects=subjects,
                 predicates=["active_holder", "legal_owner"])
    result = service.context_compile(task["principal"], compile_request(task))
    context_id = result["compiled"]["bundle"]["context_bundle_id"]
    expected = {"RULESET:world", "SCOPE_REGISTRY:world", "PREDICATE_RANGE:possessed_by",
                "PREDICATE_RANGE:legal_owner"}
    expected |= {prefix + subject for prefix in ("ENTITY:", "PROCESS_COVERAGE:") for subject in subjects}
    with kernel.db.uow(write=False) as repo:
        context = repo.one("contexts", {"context_bundle_id": context_id})
        rows = repo.many("dependencies", {"owner_type": "context", "owner_id": context_id})
        revision = kernel.revision(repo, world["world_ref"])
    assert len(rows) == len(expected)
    assert {row["scope_key"] for row in rows} == expected
    assert all(row["work_id"] == world["world_ref"]["work_id"] and
               row["branch_id"] == world["world_ref"]["branch_id"] and
               row["dependency_kind"] == "COMPUTED" and
               row["observed_version"] == revision["scope_versions"].get(row["scope_key"], 0) for row in rows)
    manifest = context["dependency_manifest"]
    assert context["dependency_fingerprint"] == fingerprint(manifest)
    assert {entry["scope_key"] for entry in manifest["scope_versions"]} == expected
    queries = {entry["scope_key"]: entry for entry in manifest["queries"]}
    assert queries["ENTITY:" + absent]["kind"] == "NEGATIVE"
    assert all(queries["ENTITY:" + subject]["kind"] == "POSITIVE" for subject in subjects[:2])
    assert len({entry["result_sha256"] for entry in queries.values()}) == 1
    # The real impact service must still discover even negative knowledge and
    # process coverage owners; dropped dependency rows could silently miss it.
    affected = kernel.impact(world["principal"], world["world_ref"],
                             ["ENTITY:" + absent, "PROCESS_COVERAGE:" + world["ids"]["sword"]])
    assert {(row["owner_id"], row["scope_key"]) for row in affected} == {
        (context_id, "ENTITY:" + absent), (context_id, "PROCESS_COVERAGE:" + world["ids"]["sword"])}
    assert all(not row["stale"] for row in affected)
    assert service.context_compile(task["principal"], compile_request(task)) == result


def test_failed_dependency_write_rolls_back_context_binding_receipt_and_outbox(query_env):
    world = seed(query_env)
    kernel, service = query_env["kernel"], query_env["query"]
    task = start(query_env, world, at=p(35), subjects=[world["ids"]["sword"], world["ids"]["P"]])
    request = compile_request(task)
    task_id = task["task"]["task_id"]
    where = {"work_id": world["world_ref"]["work_id"]}
    with kernel.db.uow(write=False) as repo:
        artifacts_before = {row["artifact_revision_id"] for row in repo.many("artifacts", where)}
    injected = []

    def fail_after_postgresql_insert(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().startswith("INSERT INTO nf_dependencies"):
            injected.append(True)
            raise RuntimeError("P3_CONTEXT_DEPENDENCY_WRITE_FAILURE")

    event.listen(kernel.db.engine, "after_cursor_execute", fail_after_postgresql_insert)
    try:
        with pytest.raises(RuntimeError, match="P3_CONTEXT_DEPENDENCY_WRITE_FAILURE"):
            service.context_compile(task["principal"], request)
    finally:
        event.remove(kernel.db.engine, "after_cursor_execute", fail_after_postgresql_insert)
    assert injected == [True]
    with kernel.db.uow(write=False) as repo:
        assert repo.one("tasks", {"task_id": task_id})["context_bundle_id"] is None
        assert repo.many("contexts", {"task_id": task_id}) == []
        assert repo.many("dependencies", where) == []
        assert repo.many("receipts", {**where, "command_type": "CONTEXT_COMPILE"}) == []
        assert repo.many("outbox", {"aggregate_id": task_id, "topic": "context.compiled"}) == []
        assert {row["artifact_revision_id"] for row in repo.many("artifacts", where)} == artifacts_before
    result = service.context_compile(task["principal"], request)
    assert service.context_compile(task["principal"], request) == result
    with kernel.db.uow(write=False) as repo:
        context_id = result["compiled"]["bundle"]["context_bundle_id"]
        assert repo.one("tasks", {"task_id": task_id})["context_bundle_id"] == context_id
        assert len(repo.many("contexts", {"task_id": task_id})) == 1
        assert len(repo.many("receipts", {**where, "command_type": "CONTEXT_COMPILE"})) == 1
        assert len(repo.many("outbox", {"aggregate_id": task_id, "topic": "context.compiled"})) == 1
    changed = deepcopy(request)
    changed["token_budget"] -= 1
    with pytest.raises(DomainError) as conflict:
        service.context_compile(task["principal"], changed)
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


def test_revocation_after_sampling_prevents_any_dependency_binding(query_env, monkeypatch):
    world = seed(query_env)
    kernel, service = query_env["kernel"], query_env["query"]
    actor = kernel.issue_grant(world["principal"], world["world_ref"]["work_id"], role_id="character_actor",
                               actor_id=world["ids"]["P"], max_story_time=p(35))["principal"]
    task = start(query_env, world, principal=actor, role="character_actor", at=p(35))
    original = service._execute

    def revoke_after_computing(packet, request):
        value = original(packet, request)
        kernel.revoke_grant(world["principal"], actor.grant_id)
        return value

    monkeypatch.setattr(service, "_execute", revoke_after_computing)
    with pytest.raises(DomainError) as revoked:
        service.context_compile(actor, compile_request(task, plane="BELIEF"))
    assert revoked.value.code == "VIEW_DENIED"
    with kernel.db.uow(write=False) as repo:
        assert repo.one("tasks", {"task_id": task["task"]["task_id"]})["context_bundle_id"] is None
        assert repo.many("contexts", {"task_id": task["task"]["task_id"]}) == []
        assert repo.many("dependencies", {"work_id": world["world_ref"]["work_id"]}) == []
