"""A returned model envelope cannot outlive its Workshop employee's Skill authorization."""

import pytest
from test_dynamic_skill_dispatch import call

from novel_factory.application.mcp_runtime import McpRuntime, TaskBinding
from novel_factory.domain.errors import DomainError

pytest_plugins = ["test_dynamic_skill_dispatch"]


def test_real_workshop_revocation_after_gateway_result_prevents_supervisor_acceptance(dynamic_site):
    site = dynamic_site
    source = site.delegate("late-source")
    skill = "Distinguish the lantern's shadow from the hand that moves it."
    binding = site.share("for-late-delivery", skill)
    revision = site.activate(source, binding, skill, requested_tools=["context_get"])
    target = site.delegate("late-summary", revision["instance_ref"], accept_summary=True)
    task_port = McpRuntime(
        site.kernel,
        target.principal,
        TaskBinding(target.task_id, target.generation, target.request["worker_id"]),
    )
    assert task_port.available_tools() == ["nf_context_get"]
    assert task_port.context_get()["task_id"] == target.task_id
    with pytest.raises(DomainError):
        task_port.query_batch([{"subject_id": site.world["ids"]["sword"], "predicate": "owner"}])
    with pytest.raises(DomainError):
        task_port.artifact_put("Outside the frozen capability", "direct-writer")

    request = {**target.request, "call_key": "supervised-candidate"}
    receipt = site.gateway.execute(target.principal, request)
    assert (
        receipt["status"] == "RESULT_STORED"
        and receipt["validation_status"] == "VALIDATED_CANDIDATE"
    )
    assert skill in site.requests[0]["messages"][0]["content"]
    with site.kernel.db.uow(write=False) as repo:
        data = site.kernel.artifact(
            repo, target.frozen.agent_input["input_ref"], site.wr["work_id"]
        )["payload"]
        audit = site.kernel.artifact(repo, receipt["evidence_ref"], site.wr["work_id"])["payload"]
        prompt = site.kernel.artifact(repo, audit["prompt_ref"], site.wr["work_id"])["payload"]
        snapshot = site.kernel.artifact(repo, audit["creative_context_ref"], site.wr["work_id"])[
            "payload"
        ]
        assert (
            data["dynamic_assignment_ref"]
            == audit["dynamic_assignment_ref"]
            == prompt["dynamic_assignment_ref"]
        )
        assert snapshot["dynamic_assignment_ref"] == data["dynamic_assignment_ref"]
        assert snapshot["packet"]["runtime"]["skill_name"] != binding["skill_id"]

    sibling = site.kernel.fork(site.owner, site.wr, "CANDIDATE", "isolated-review-branch")
    other = site.kernel.issue_grant(
        site.owner,
        site.wr["work_id"],
        role_id="control_operator",
        branch_ids=[sibling["branch_id"]],
        capabilities=["READ", "ADMIN"],
    )
    for ref in (
        audit["prompt_ref"],
        audit["response_ref"],
        receipt["output_ref"],
        receipt["evidence_ref"],
    ):
        assert site.kernel.get_artifact(site.owner, site.wr["work_id"], ref)
        with pytest.raises(DomainError):
            site.kernel.get_artifact(other["principal"], site.wr["work_id"], ref)

    call(
        site.ports["manager"],
        "aw_skill_revoke",
        operation_id="after-result",
        binding_id=binding["binding_id"],
    )
    with pytest.raises(DomainError):
        site.gateway.execute(target.principal, request)
    site.supervisor._deliver_candidates(site.owner, site.wr["work_id"], site.wr["branch_id"])
    assert site.kernel.tasks.get(target.task_id)["state"] == "WAITING_REVIEW"
    assert len(site.requests) == 1
    with site.kernel.db.uow(write=False) as repo:
        assert (
            repo.one(
                "outbox", {"dedupe_key": f"task:{target.task_id}:{target.generation}:ACCEPTED"}
            )
            is None
        )
        assert not repo.many(
            "artifacts", {"kind": "MCP_TASK_CANDIDATE", "work_id": site.wr["work_id"]}
        )


def test_revocation_after_nf_reserves_but_before_send_releases_only_the_unsent_attempt(
    dynamic_site, monkeypatch
):
    site = dynamic_site
    source = site.delegate("reserve-source")
    skill = "Trace the difference between the river and its reflection."
    binding = site.share("before-send", skill)
    selection = site.activate(source, binding, skill)
    target = site.delegate("reserve-target", selection["instance_ref"])
    persist = site.gateway._audit_artifact

    def revoke_after_recording(*args):
        ref = persist(*args)
        if args[-1] == "AGENT_PROMPT":
            call(
                site.ports["manager"],
                "aw_skill_revoke",
                operation_id="before-send",
                binding_id=binding["binding_id"],
            )
        return ref

    monkeypatch.setattr(site.gateway, "_audit_artifact", revoke_after_recording)
    with pytest.raises(DomainError):
        site.gateway.execute(target.principal, target.request)
    assert site.requests == []
    with site.kernel.db.uow(write=False) as repo:
        attempts = repo.many("call_attempts", {"task_id": target.task_id})
        assert len(attempts) == 1
        assert attempts[0]["state"] == "FAILED" and attempts[0]["actual_amount"] == 0
        assert attempts[0]["actual_tokens"] == 0
        budget = repo.one("budgets", {"budget_id": site.budget_id})
        assert budget["spent_amount"] == budget["reserved_amount"] == 0


def test_compact_mcp_reads_recheck_workshop_tool_ceiling_and_revocation(dynamic_site):
    from sqlalchemy import text

    from novel_factory.application.bound_tools import invoke_bound_tool

    site = dynamic_site
    source = site.delegate("compact-source")
    instructions = "Record only the facts visible to the assigned reader."
    binding = site.share("compact-read", instructions)
    revision = site.activate(source, binding, instructions, requested_tools=["context_get"])
    target = site.delegate("compact-target", revision["instance_ref"])
    runtime = McpRuntime(site.kernel, target.principal,
                         TaskBinding(target.task_id, target.generation, target.request["worker_id"]),
                         compact_role_reads=True)
    assert invoke_bound_tool(runtime, "nf_context_get", {}, transport="mcp")["task_id"] == target.task_id
    with pytest.raises(DomainError) as excluded:
        invoke_bound_tool(runtime, "nf_task_get", {}, transport="mcp")
    assert excluded.value.code == "VIEW_DENIED"
    with pytest.raises(DomainError) as malformed:
        invoke_bound_tool(runtime, "nf_context_get", {"extra": True}, transport="mcp")
    assert malformed.value.code == "INVALID_ARGUMENT"
    call(site.ports["manager"], "aw_skill_revoke", operation_id="before-compact-read",
         binding_id=binding["binding_id"])
    with pytest.raises(DomainError) as revoked:
        invoke_bound_tool(runtime, "nf_context_get", {}, transport="mcp")
    with pytest.raises(DomainError) as malformed_revoked:
        invoke_bound_tool(runtime, "nf_context_get", {"extra": True}, transport="mcp")
    assert malformed_revoked.value.code == revoked.value.code == "DYNAMIC_SKILL_DENIED"
    with site.kernel.db.uow(write=False) as repo:
        records = repo.conn.execute(text("""
            SELECT s.tool_name,f.error_code,s.work_id,s.branch_id,s.generation,
                   so.payload AS started,fo.payload AS finished
            FROM nf_audit_events s JOIN nf_audit_events f ON f.causation_id=s.event_id
            JOIN nf_outbox so ON so.dedupe_key='audit:'||s.event_id::text
            JOIN nf_outbox fo ON fo.dedupe_key='audit:'||f.event_id::text
            WHERE s.task_id=:task AND s.event_type='mcp.started' AND f.event_type='mcp.finished'
            ORDER BY s.event_time
        """), {"task": target.task_id}).all()
    assert [row.error_code for row in records] == [
        None, "VIEW_DENIED", "INVALID_ARGUMENT", revoked.value.code, revoked.value.code,
    ]
    for row in records:
        assert str(row.work_id) == site.wr["work_id"] and str(row.branch_id) == site.wr["branch_id"]
        assert row.started == row.finished
        assert row.started["task_id"] == target.task_id and row.generation == target.generation
