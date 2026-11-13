"""Exercise both actor gateway routes with a real BELIEF read and a fixture inference."""
import json
from decimal import Decimal

import pytest

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.mcp_runtime import McpRuntime, TaskBinding
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.codex_host import CodexRoleContextAdapter, CodexRoleLookupAdapter
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig
from novel_factory.infrastructure.model_gateway import ModelResponse
from test_p5_actor_tool_choice import ToolFixture


@pytest.mark.postgres
@pytest.mark.integration
@pytest.mark.parametrize("mode", ["host-read", "model-selected-lookup"])
def test_harness_actor_uses_frozen_prose_and_control_identity(kernel_env, mode, monkeypatch):
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    wr, admin = world["world_ref"], world["principal"]
    actor, subject = world["ids"]["protagonist"], world["ids"]["sword"]
    model_inputs, mcp_reads = [], []

    def read(k, owner, task, binary, relay, *, tool="nf_context_get", arguments=None):
        runtime = McpRuntime(k, owner, TaskBinding(task["task_id"], task["generation"], task["lease_owner"]))
        mcp_reads.append(tool)
        result = runtime.context_get() if tool == "nf_context_get" else runtime.query_batch(arguments["lookups"])
        return result, "fixture-thread"

    def generate(request):
        system, user = request.messages[0]["content"], json.loads(request.messages[1]["content"])
        model_inputs.append(user)
        if mode == "model-selected-lookup" and len(model_inputs) == 1:
            assert "dependency_fingerprint" not in user["context"]
            response = {"tool": "nf_query_batch", "arguments": {"lookups": [{"subject_id": subject, "predicate": "owner"}]}}
        else:
            assert "context" not in user and "creative_materials" in user
            assert "dependency_fingerprint" not in json.dumps(user["creative_materials"])
            identity = json.loads(system.split("\nIdentity: ", 1)[1].splitlines()[0])
            fixed = user["proposal_protocol"]["copy_into_payload"]
            assert fixed["actor_id"] == actor and fixed["story_interval"]["start"] == point(1)
            assert fixed["dependency_fingerprint"] == identity["dependency_fingerprint"]
            payload = {**fixed, "intent": "Wait", "targets": [], "resource_requests": [],
                       "preconditions": [], "evidence_refs": []}
            response = {k: v for k, v in identity.items() if k != "dependency_fingerprint"}
            response.update(status="SUCCEEDED", unresolved_questions=[], payload=payload)
        return ModelResponse(json.dumps(response), provider_receipt=f"fixture-{len(model_inputs)}", input_tokens=12,
                             output_tokens=7, actual_amount=Decimal("0.05"), currency="USD",
                             raw_response={"fixture": True}, request_sha256=request.request_sha256)

    cls = CodexRoleContextAdapter if mode == "host-read" else CodexRoleLookupAdapter
    route = cls(ToolFixture("read", subject), HostJournal(kernel), context_reader=read, relay="fixture")
    monkeypatch.setattr(route, "check_available", lambda: None)
    monkeypatch.setattr(route, "generate", generate)
    gateway = AgentGateway(kernel, {"fixture": route}, creative_harness=CreativeHarnessConfig(enabled=True))
    supervisor = RuntimeSupervisor(kernel, gateway)
    job = RuntimeTaskService(kernel, gateway).prepare(admin, wr, role_id="character_actor", task_kind="character_action",
        story_time=point(1), subject_ids=[subject], predicates=["owner"], actor_id=actor, assignment="Decide",
        adapter_id="fixture", queue_state="READY")
    supervisor.enroll(admin, job.task["task_id"], manifest_ref=job.manifest_ref, input_ref=job.agent_input["input_ref"],
        knowledge_cut={"plane": "BELIEF", "actor_id": actor, "known_at": point(1)},
        budget_id=kernel.tasks.create_budget(wr["work_id"], "2"), reserve_amount="1",
        context_tokens=20000, token_reservation=100000)
    receipt = supervisor.run_once(admin, wr["work_id"], wr["branch_id"])
    assert receipt.get("validation_status") == "VALIDATED_CANDIDATE", receipt
    if mode == "model-selected-lookup":
        assert len(model_inputs) == 2 and mcp_reads == ["nf_query_batch"]
        answer = model_inputs[1]["role_lookup"]["result"]["items"][0]["payload"]["state_items"][0]["answer"]
        assert answer["plane"] == "BELIEF" and answer["knowledge_status"] == "UNKNOWN" and answer["data"] is None
    else:
        assert len(model_inputs) == 1 and mcp_reads == ["nf_context_get"]
    with kernel.db.uow(write=False) as repo:
        audit = kernel.artifact(repo, receipt["evidence_ref"], wr["work_id"])["payload"]
        ref = audit["creative_context_ref"]
    assert json.loads(kernel.get_artifact(admin, wr["work_id"], ref))["packet"]["context"]["actor_id"] == actor
    other = kernel.fork(admin, wr, "CANDIDATE", "other-actor-review-branch")
    other_admin = kernel.issue_grant(admin, wr["work_id"], role_id="control_operator",
                                     capabilities=["ADMIN", "READ"], branch_ids=[other["branch_id"]])["principal"]
    for viewer in (job.principal, other_admin):
        with pytest.raises(DomainError) as error:
            kernel.get_artifact(viewer, wr["work_id"], ref)
        assert error.value.code == "VIEW_DENIED"
