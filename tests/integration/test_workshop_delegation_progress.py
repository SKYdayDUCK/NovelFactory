"""The NF binding reports current-generation S9 progress without disclosing case material."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_s9_dependencies import query
from test_workshop_delegation import site  # noqa: F401

from novel_factory.application.conflict_resolution import ConflictResolutionService
from novel_factory.application.delegation import provision_slot
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.query_policy import profiles
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.semantic_dependencies import add_assumption
from novel_factory.domain.propositions import encode_proposition
from novel_factory.domain.semantic_keys import slot
from novel_factory.fixtures import fixture_definition, point, run_command

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_only_current_delegation_generation_reports_dependency_conflict(site):  # noqa: F811
    bridge, submit, get, _, supervisor, owner, model_calls, original = site
    kernel = supervisor.kernel
    sword = fixture_definition("r1_sword")["ids"]["sword"]
    base = original.spec["world_ref"]
    assignment = "Summarize the privately observed lifecycle."
    observed_at = point(1)
    frozen = RuntimeTaskService(kernel, supervisor.gateway).define(
        owner,
        base,
        role_id=submit["role_id"],
        task_kind="summary",
        story_time=observed_at,
        subject_ids=[sword],
        predicates=["lifecycle"],
        assignment=assignment,
        adapter_id="fixture",
        max_amount=submit["max_amount"],
        token_budget=submit["token_limit"],
    )
    scope = "scene.summary.lifecycle"
    provision_slot(
        supervisor,
        owner,
        bridge.installation_id,
        scope_ref=scope,
        task_template_id=submit["task_template_id"],
        frozen=frozen,
        budget_id=kernel.tasks.create_budget(base["work_id"], "2"),
        reserve_amount=submit["max_amount"],
        token_reservation=submit["token_limit"],
        accept_summary=True,
    )
    other = bridge.call("nf_delegation_submit", submit)
    scoped_get = dict(get, operation_id=str(uuid4()))
    receipt = bridge.call(
        "nf_delegation_submit",
        dict(
            submit,
            operation_id=scoped_get["operation_id"],
            scope_ref=scope,
            budget_reservation_ref=str(uuid4()),
        ),
    )
    task_id = receipt["task_id"]
    assert task_id == frozen.spec["task_id"] and task_id != other["task_id"]
    claim = kernel.claim_task(
        frozen.principal, "lifecycle-reader", task_id=task_id, lease_seconds=600
    )
    generation = claim["generation"]
    compiled = WorldQueryService(kernel).context_compile(
        frozen.principal,
        {
            "schema_version": "2.1",
            "task_id": task_id,
            "generation": generation,
            "recipe_id": profiles()[submit["role_id"]]["allowed_recipes"][0],
            "input_manifest_ref": frozen.manifest_ref,
            "knowledge_cut": frozen.cut,
            "read_contract": {
                "revision": {"mode": "FIXED", "world_ref": base},
                "process_policy": "ALLOW_PARTIAL",
                "index_policy": "AUTHORITATIVE",
                "max_wait_ms": 0,
            },
            "subject_time": {"kind": "POINT", "at": frozen.spec["story_time"]},
            "token_budget": 20000,
            "idempotency_key": "compile:" + task_id,
        },
    )
    assert compiled["status"] == "READY"
    # A summary's fixed historical observation stays true. Explicitly bind the
    # continuing requirement to this generation's own authorized query receipt.
    observed = query(kernel, {"principal": frozen.principal}, base, claim, sword, "lifecycle", 1)
    assumption = add_assumption(
        kernel,
        frozen.principal,
        {
            "task_id": task_id,
            "generation": generation,
            "dependency_handle": observed["dependency_handle"],
            "dependency_kind": "REQUIRES",
            "statement": encode_proposition(
                sword, "lifecycle", {"kind": "STRING", "value": "ACTIVE"}
            ),
        },
    )
    assert not assumption["uncertain"]
    before = bridge.call("nf_delegation_get", scoped_get)
    assert before["task_state"] == "RUNNING" and before["generation"] == generation
    assert before["wait_reason"] is None and before["result_ref"] is None

    change = run_command(kernel, owner, base, "DESTROY_ITEM", {"item_id": sword, "at": point(40)})
    expected = dict(before, wait_reason="DEPENDENCY_CONFLICT")
    assert bridge.call("nf_delegation_get", scoped_get) == expected
    resolution = ConflictResolutionService(kernel)
    while resolution.scan_one(owner, base["work_id"], base["branch_id"]) is not None:
        pass
    with kernel.db.uow(write=False) as repo:
        case_id = repo.conn.execute(
            text("""
            SELECT c.case_id::text FROM nf_conflict_cases c
            JOIN nf_semantic_reads r USING(task_id, generation, work_id, branch_id)
            WHERE c.task_id=:task AND c.generation=:gen AND c.source_task_id=:writer
            AND c.world_revision_id=:revision AND r.details->>'origin'='ASSUMPTION'
            AND :scope=ANY(r.keys)
        """),
            {
                "task": task_id,
                "gen": generation,
                "writer": change["task"]["task_id"],
                "revision": change["world_ref"]["world_revision_id"],
                "scope": slot(sword, "lifecycle"),
            },
        ).scalar_one()
    current = bridge.call("nf_delegation_get", scoped_get)
    assert current == expected and bridge.call("nf_delegation_get", get) == other
    encoded = json.dumps(current)
    for private in (
        case_id,
        sword,
        assignment,
        other["task_id"],
        other["operation_id"],
        change["task"]["task_id"],
        change["world_ref"]["world_revision_id"],
        compiled["compiled"]["bundle"]["context_bundle_id"],
        compiled["compiled_artifact_ref"]["sha256"],
        observed["dependency_handle"],
    ):
        assert private not in encoded

    # Recovery fences the failed worker. The old case stays in the database.
    kernel.tasks.fail(task_id, generation, "lifecycle-reader", retryable=True)
    next_view = bridge.call("nf_delegation_get", scoped_get)
    assert next_view["task_id"] == task_id and next_view["generation"] == generation + 1
    assert next_view["task_state"] == "STALE" and next_view["wait_reason"] is None
    assert next_view["result_ref"] is None and not model_calls
