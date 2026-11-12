"""Branch permissions on a gateway's own frozen records and historical audit stages."""
from uuid import uuid4

import pytest

from novel_factory.application.audit_log import LogService
from novel_factory.domain.errors import DomainError
from novel_factory.domain.canonical import canonical_bytes
from test_agent_gateway import setup_gateway
from test_harness_context import EnvelopeFixture, configured


pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def branch_operator(item, branch):
    world = item["world"]
    return item["kernel"].issue_grant(
        world["principal"], world["world_ref"]["work_id"], role_id="operator",
        branch_ids=[branch], capabilities=["READ", "ADMIN"],
    )["principal"]


def operators(item):
    world = item["world"]
    wr = world["world_ref"]
    sibling = item["kernel"].fork(world["principal"], wr, "CANDIDATE", "audit-policy-sibling")
    return branch_operator(item, wr["branch_id"]), branch_operator(item, sibling["branch_id"])


def read_policy(kernel, work, ref, operator, sibling):
    assert kernel.get_artifact(operator, work, ref)
    try:
        kernel.get_artifact(sibling, work, ref)
    except DomainError as exc:
        assert exc.code == "VIEW_DENIED"
        return "DENY"
    return "ALLOW"


def test_s1_raw_audit_policy_at_preparation_and_delivery(kernel_env, monkeypatch):
    configured(monkeypatch)
    item = setup_gateway(kernel_env, EnvelopeFixture())
    kernel, wr = item["kernel"], item["world"]["world_ref"]
    owner, sibling = operators(item)
    request = item["request"]
    prepared = item["gateway"].prepare_input(
        item["world"]["principal"], wr, role_id="writer", task_kind="scene_draft",
        assignment="Workshop sample for the permitted branch.", adapter_id="fixture",
    )
    rights = {"prepared_input": read_policy(kernel, wr["work_id"], prepared["input_ref"], owner, sibling)}
    result = item["gateway"].execute(item["principal"], request)
    assert result["validation_status"] == "VALIDATED_CANDIDATE"
    with kernel.db.uow(write=False) as repo:
        audit = kernel.artifact(repo, result["evidence_ref"], wr["work_id"])["payload"]
    for field in ("input_ref", "prompt_ref", "response_ref", "output_ref", "creative_context_ref"):
        rights[field] = read_policy(kernel, wr["work_id"], audit[field], owner, sibling)
    rights["evidence_ref"] = read_policy(kernel, wr["work_id"], result["evidence_ref"], owner, sibling)
    assert rights == dict.fromkeys(rights, "DENY")


def test_unmarked_historical_gateway_stages_keep_their_task_branch(kernel_env, monkeypatch):
    configured(monkeypatch)
    item = setup_gateway(kernel_env, EnvelopeFixture())
    kernel, world = item["kernel"], item["world"]
    owner, sibling = operators(item)
    result = item["gateway"].execute(item["principal"], item["request"])
    assert result["validation_status"] == "VALIDATED_CANDIDATE"
    work = world["world_ref"]["work_id"]
    with kernel.objects.lock(), kernel.db.uow() as repo:
        audit = kernel.artifact(repo, result["evidence_ref"], work)["payload"]
        task = repo.one("tasks", {"task_id": audit["task_id"]})
        material = {"text": "Historical fixture record."}
        refs = {}
        # Import the immutable, unmarked format using the historical stage ledger.
        for kind in ("AGENT_RESPONSE", "AGENT_CANDIDATE", "AGENT_ROLE_TOOL_PROMPT",
                     "AGENT_ROLE_TOOL_RESPONSE", "AGENT_ROLE_TOOL_CHOICE", "AGENT_ROLE_TOOL_RESULT"):
            ref = kernel._blob(material)
            kernel._register_artifact(repo, ref, work, material, kind, task["principal_id"])
            LogService.model_stage(repo.conn, task, audit["attempt_id"], kind, ref["artifact_revision_id"])
            refs[kind] = ref
    rights = {kind: read_policy(kernel, work, ref, owner, sibling) for kind, ref in refs.items()}
    assert rights == dict.fromkeys(rights, "DENY")


def test_static_agent_artifact_read_policy_stays_compatible(kernel_env, monkeypatch):
    configured(monkeypatch, False)
    item = setup_gateway(kernel_env)
    owner, sibling = operators(item)
    kernel, work = item["kernel"], item["world"]["world_ref"]["work_id"]
    result = item["gateway"].execute(item["principal"], item["request"])
    with kernel.db.uow(write=False) as repo:
        audit = kernel.artifact(repo, result["evidence_ref"], work)["payload"]
    assert "creative_context_ref" not in audit
    assert kernel.get_artifact(owner, work, result["evidence_ref"]) == canonical_bytes(audit)
    assert kernel.get_artifact(sibling, work, result["evidence_ref"]) == canonical_bytes(audit)


def test_legacy_input_without_preparation_event_uses_existing_task_branch(kernel_env, monkeypatch):
    configured(monkeypatch)
    item = setup_gateway(kernel_env, EnvelopeFixture())
    kernel, work = item["kernel"], item["world"]["world_ref"]["work_id"]
    owner, sibling = operators(item)
    with kernel.objects.lock(), kernel.db.uow() as repo:
        original = kernel.artifact(repo, item["request"]["input_ref"], work)
        data = dict(original["payload"], assignment="Archive sample prepared before branch recording.")
        legacy = kernel._blob(data)
        kernel._register_artifact(repo, legacy, work, data, "AGENT_INPUT", original["created_by"])
    assert kernel.get_artifact(item["world"]["principal"], work, legacy) == canonical_bytes(data)
    for operator in (owner, sibling):
        with pytest.raises(DomainError) as error:
            kernel.get_artifact(operator, work, legacy)
        assert error.value.code == "VIEW_DENIED"
    # Restore the original input attachment, as a pre-existing archived TaskSpec.
    spec = dict(item["spec"], task_id=str(uuid4()), input_artifacts=[legacy])
    kernel.tasks.create(spec, principal_id=item["principal"].principal_id, idempotency_key=spec["task_id"])
    assert read_policy(kernel, work, legacy, owner, sibling) == "DENY"


def test_new_task_accepts_only_its_branch_prepared_input(kernel_env, monkeypatch):
    configured(monkeypatch)
    item = setup_gateway(kernel_env, EnvelopeFixture())
    kernel, world = item["kernel"], item["world"]
    wr = kernel.fork(world["principal"], world["world_ref"], "CANDIDATE", "input-policy-sibling")
    writer = kernel.issue_grant(world["principal"], wr["work_id"], role_id="writer",
                                branch_ids=[wr["branch_id"]], planes=["WORLD"],
                                capabilities=["READ", "TASK_CREATE"], max_story_time=item["spec"]["story_time"])
    spec = dict(item["spec"], task_id=str(uuid4()), world_ref=wr,
                capability_grant_id=writer["principal"].grant_id, input_artifacts=[item["request"]["input_ref"]])
    with pytest.raises(DomainError) as error:
        kernel.create_task(writer["principal"], spec, spec["task_id"])
    assert error.value.code == "VIEW_DENIED"
    prepared = item["gateway"].prepare_input(world["principal"], wr, role_id="writer", task_kind="scene_draft",
                                            assignment="Sample for the new branch.", adapter_id="fixture")
    spec["input_artifacts"] = [prepared["input_ref"]]
    kernel.create_task(writer["principal"], spec, spec["task_id"])
