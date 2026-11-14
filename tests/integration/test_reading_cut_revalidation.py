"""Real MCP readings survive a later empty prefix without another model turn."""
import pytest
from test_production_verification import coverage_fixture, drive_coverage

from novel_factory.application.production_pipeline import _artifact
from novel_factory.application.rolling_facts import (
    revalidate_fact_reading,
    validate_native_fact_source,
)
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import point, run_command
from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_original_mcp_cuts_are_preserved_in_same_text_revalidation(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, source, calls, _, _ = coverage_fixture(kernel, monkeypatch)
    board, cursor = drive_coverage(kernel, gateway, owner, source)
    assert cursor["state"] == "FROZEN"
    facts_node = next(node for node in board["nodes"].values() if node.get("state") == "FACTS_ACCEPTED")
    old_ref = facts_node["output_ref"]
    with kernel.db.uow(write=False) as repo:
        old = kernel.artifact(repo, old_ref, source["work_id"], kind="ROLLING_FACT_REPORT")["payload"]
        knowledge = kernel.artifact(repo, old["knowledge_ref"], source["work_id"], kind="EPISTEMIC_READING")["payload"]
        head = kernel.head(repo, source["work_id"], source["branch_id"])
        original_calls = len(repo.many("call_attempts", {}))
        checkpoint = kernel.artifact(repo, cursor["last_checkpoint"], source["work_id"])["payload"]
    target = run_command(kernel, owner, head, "SCHEDULE_ADVANCE", {
        "scope_ids": board["subject_ids"], "target": point(3, 1)})["world_ref"]
    count = len(calls)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("READING_REVALIDATION_MUST_NOT_CALL_MODEL")

    monkeypatch.setattr(RuntimeModelAdapter, "generate", forbidden)
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": source["work_id"]}, lock="update")
        renewed_ref, renewed = revalidate_fact_reading(kernel, repo, owner, old_ref, target)
        assert revalidate_fact_reading(kernel, repo, owner, old_ref, target) == (renewed_ref, renewed)
        assert renewed_ref != old_ref and renewed["knowledge_ref"] == old["knowledge_ref"]
        assert renewed["extraction_ref"] == old["extraction_ref"]
        assert renewed["source_report_ref"] == old_ref and renewed["adopted_world_ref"] == target
        assert renewed["acceptance_ref"] != old["acceptance_ref"]
        for ref in (old["acceptance_ref"], renewed["acceptance_ref"]):
            accepted = kernel.artifact(repo, ref, source["work_id"], kind="NARRATIVE_ACCEPTANCE")["payload"]
            assert accepted["draft_ref"] == knowledge["draft_ref"] and accepted["text_ref"] == knowledge["text_ref"]
            assert accepted["world_effects_applied"] == 0
        assert kernel.artifact(repo, old["knowledge_ref"], source["work_id"])["payload"] == knowledge
        assert knowledge["world_ref"] == head != target
        assert kernel.artifact(repo, cursor["last_checkpoint"], source["work_id"])["payload"] == checkpoint
        assert len(repo.many("call_attempts", {})) == original_calls
        extraction = kernel.artifact(repo, old["extraction_ref"], source["work_id"], kind="ASSERTION_EXTRACTION")["payload"]
        task = repo.one("tasks", {"task_id": extraction["task_id"]})
        draft = kernel.artifact(repo, knowledge["draft_ref"], source["work_id"], kind="NARRATIVE_DRAFT")["payload"]
        forged = {**extraction, "assertions": [{**item, "summary": "Forged assertion not returned by this reader"}
                                              for item in extraction["assertions"]]}
        with pytest.raises(DomainError) as substituted:
            validate_native_fact_source(kernel, repo, task, forged, draft)
        assert substituted.value.code == "INVALID_EVIDENCE"
        missing = dict(old)
        del missing["knowledge_ref"]
        missing_ref = _artifact(kernel, repo, source["work_id"], owner, "ROLLING_FACT_REPORT", missing)
        with pytest.raises(DomainError) as error:
            revalidate_fact_reading(kernel, repo, owner, missing_ref, target)
        assert error.value.code == "EPISTEMIC_REVIEW_REQUIRED"
    assert len(calls) == count
