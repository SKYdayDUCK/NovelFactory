"""Real fixed-cut query delivery, including a cache hit after the sampling transaction."""

from contextlib import contextmanager

import pytest
from sqlalchemy import event, text

from novel_factory.application.semantic_dependencies import check_task_dependencies
from novel_factory.domain.errors import DomainError

from .conftest import p, request, seed, start

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@contextmanager
def query_sql(engine):
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(" ".join(statement.lower().split()))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


@pytest.mark.parametrize("alternate_branch", [False, True], ids=["same-branch", "cross-branch"])
def test_repeated_delivery_verifies_both_scopes_and_persists_one_read(query_env, alternate_branch):
    kernel, service = query_env["kernel"], query_env["query"]
    world = seed(query_env)
    task = start(query_env, world)
    principal = task["principal"]
    query = request(task)
    if alternate_branch:
        other = kernel.fork(principal, world["world_ref"], "CANDIDATE", "alternate-read")
        query["read_contract"]["revision"]["world_ref"] = other

    first = service.query(principal, query)
    hits = service.cache_hits
    with query_sql(kernel.db.engine) as statements:
        again = service.query(principal, query)
    assert service.cache_hits == hits + 1
    assert first["payload"] == again["payload"]
    assert first["dependency_handle"] == again["dependency_handle"]

    # The fixed-cut sample is read-only. Its separate delivery transaction begins
    # with the maintenance fence and ends after recording the immutable observation.
    (gate,) = [i for i, sql in enumerate(statements) if "pg_advisory_xact_lock_shared" in sql]
    delivery = statements[gate:]
    for table in ("nf_works", "nf_grants"):
        scope_locks = [sql for sql in delivery if f"from {table} " in sql and "for share" in sql]
        assert len(scope_locks) == (2 if alternate_branch else 1)
    assert any("from nf_branches " in sql and "for share" in sql for sql in delivery)
    assert any("from nf_tasks " in sql and "for share" in sql for sql in delivery)
    assert sum(sql.startswith("insert into nf_semantic_reads") for sql in delivery) == 1
    if not alternate_branch:
        # The original same-branch delivery issued 11 statements, including a
        # second work/grant authorization and a third read of the same work.
        assert len(delivery) <= 8

    with kernel.db.uow(write=False) as repo:
        keys = {"task": query["task_id"], "gen": query["generation"]}
        assert (
            repo.conn.scalar(
                text("""
            SELECT count(*) FROM nf_semantic_reads WHERE task_id=:task AND generation=:gen
                AND details->>'origin'='QUERY'
        """),
                keys,
            )
            == 1
        )
        if alternate_branch:
            assert (
                repo.conn.scalar(
                    text("""
                SELECT count(*) FROM nf_conflict_cases WHERE task_id=:task AND generation=:gen
                    AND conflict_type='BRANCH_DIVERGENCE'
            """),
                    keys,
                )
                == 1
            )
            with pytest.raises(DomainError) as stale:
                check_task_dependencies(repo, keys["task"], keys["gen"])
            assert stale.value.code == "DEPENDENCY_CONFLICT"


@pytest.mark.parametrize(
    "change,code", [("revoke", "VIEW_DENIED"), ("advance", "STALE_GENERATION")]
)
def test_cache_hit_rechecks_changes_committed_after_sampling(query_env, monkeypatch, change, code):
    kernel, service = query_env["kernel"], query_env["query"]
    world = seed(query_env)
    wr = world["world_ref"]
    principal = kernel.issue_grant(
        world["principal"],
        wr["work_id"],
        role_id="evidence_maintainer",
        capabilities=["READ", "TASK_CREATE", "TASK_CLAIM"],
        branch_ids=[wr["branch_id"]],
        max_story_time=p(42),
        planes=["WORLD"],
    )["principal"]
    task = start(query_env, world, principal=principal, role="evidence_maintainer", at=p(42))
    query = request(task)
    service.query(principal, query)
    hits = service.cache_hits
    deliver = service._delivery_check

    def after_sample(*args):
        if change == "revoke":
            kernel.revoke_grant(world["principal"], principal.grant_id)
        else:
            kernel.tasks.fail(query["task_id"], query["generation"], "query-worker", retryable=True)
            reclaimed = kernel.claim_task(principal, "second-worker", task_id=query["task_id"])
            assert reclaimed["generation"] > query["generation"]
        return deliver(*args)

    monkeypatch.setattr(service, "_delivery_check", after_sample)
    with pytest.raises(DomainError) as blocked:
        service.query(principal, query)
    assert blocked.value.code == code
    assert service.cache_hits == hits + 1
