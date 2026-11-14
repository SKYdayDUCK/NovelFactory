"""Accepted historical sources retain authority, not a live execution lease."""
from datetime import timedelta

import pytest
from test_production_verification import coverage_fixture, drive_coverage

from novel_factory.application.authorization import Principal
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.rolling_facts import revalidate_fact_reading
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import point, run_command
from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_expired_accepted_reader_can_revalidate_but_cannot_query_or_rebind(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, source, calls, _, _ = coverage_fixture(kernel, monkeypatch)
    board, cursor = drive_coverage(kernel, gateway, owner, source)
    assert cursor["state"] == "FROZEN"
    facts = next(node for node in board["nodes"].values() if node.get("state") == "FACTS_ACCEPTED")
    with kernel.db.uow(write=False) as repo:
        old = kernel.artifact(repo, facts["output_ref"], source["work_id"], kind="ROLLING_FACT_REPORT")["payload"]
        extraction = kernel.artifact(repo, old["extraction_ref"], source["work_id"])["payload"]
        draft = kernel.artifact(repo, extraction["draft_ref"], source["work_id"])["payload"]
        reader = repo.one("tasks", {"task_id": extraction["task_id"]})
        assert reader["state"] == "ACCEPTED"
        head = kernel.head(repo, source["work_id"], source["branch_id"])
    target = run_command(kernel, owner, head, "SCHEDULE_ADVANCE", {
        "scope_ids": board["subject_ids"], "target": point(3, 1)})["world_ref"]
    count = len(calls)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("HISTORICAL_SOURCE_MUST_NOT_CALL_MODEL")

    monkeypatch.setattr(RuntimeModelAdapter, "generate", forbidden)
    principal = Principal(reader["grant_id"], reader["principal_id"])
    service = NarrativeService(kernel)
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": source["work_id"]}, lock="update")
        repo.change("tasks", {"task_id": reader["task_id"]}, {"deadline": repo.now() - timedelta(seconds=1)})
        assert service._extraction(repo, old["extraction_ref"], extraction["draft_ref"], draft,
                                   allow_accepted=True)[0] == extraction
        # This is the unchanged qualification used by public query/context APIs.
        with pytest.raises(DomainError) as error:
            WorldQueryService(kernel)._task(repo, principal, reader["task_id"], reader["generation"])
        assert error.value.code == "DEADLINE_EXCEEDED"
        for table, changes, code in [
            ("grants", {"epoch": repo.one("grants", {"grant_id": reader["grant_id"]})["epoch"] + 1}, "VIEW_DENIED"),
            ("grants", {"active": False}, "VIEW_DENIED"),
            ("works", {"policy_version": "historical-source-test-policy"}, "TASK_REBIND_REQUIRED"),
            ("tasks", {"generation": reader["generation"] + 1}, "STALE_GENERATION"),
        ]:
            with repo.conn.begin_nested() as savepoint:
                keys = {"grant_id": reader["grant_id"]} if table == "grants" else (
                    {"work_id": source["work_id"]} if table == "works" else {"task_id": reader["task_id"]})
                repo.change(table, keys, changes)
                with pytest.raises(DomainError) as error:
                    service._extraction(repo, old["extraction_ref"], extraction["draft_ref"], draft, allow_accepted=True)
                assert error.value.code == code
                savepoint.rollback()
        renewed_ref, renewed = revalidate_fact_reading(kernel, repo, owner, facts["output_ref"], target)
        assert renewed_ref != facts["output_ref"] and renewed["extraction_ref"] == old["extraction_ref"]
        assert renewed["adopted_world_ref"] == target
        assert repo.one("tasks", {"task_id": reader["task_id"]})["deadline"] < repo.now()
    assert len(calls) == count
    with pytest.raises(DomainError) as error:
        WorldQueryService(kernel).query(principal, {
            "schema_version": "2.1", "task_id": reader["task_id"], "generation": reader["generation"],
            "read_contract": {"revision": {"mode": "FIXED", "world_ref": head},
                              "process_policy": "ALLOW_PARTIAL", "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
            "knowledge_cut": {"plane": "WORLD"}, "subject_time": {"kind": "POINT", "at": point(1)},
            "query": {"kind": "STATE", "subject_ids": board["subject_ids"], "predicates": ["lifecycle"]},
            "page_size": 30, "cursor": None})
    assert error.value.code == "DEADLINE_EXCEEDED"
