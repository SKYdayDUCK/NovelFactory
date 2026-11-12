"""A real, locally blocked creative dispatch carried across a named physical recovery point."""

import json

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.creative_context_store import load_snapshot
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter


def stage_packet(kernel):
    world = load_fixture(kernel)
    wr, owner = world["world_ref"], world["principal"]
    adapter = OpenAICompatibleAdapter(
        endpoint="https://127.0.0.1:9/v1/chat/completions",
        model="p57-transport-drill",
        api_key=None,
    )
    gateway = AgentGateway(
        kernel, {"offline": adapter}, creative_harness=CreativeHarnessConfig(enabled=True)
    )
    assignment = RuntimeTaskService(kernel, gateway).prepare(
        owner,
        wr,
        role_id="writer",
        task_kind="scene_draft",
        story_time=point(1),
        subject_ids=[world["ids"]["sword"]],
        predicates=["owner"],
        assignment="写人物在渡口停步的那一刻。",
        adapter_id="offline",
    )
    request = {
        "schema_version": "2.1",
        "task_id": assignment.task["task_id"],
        "generation": assignment.task["generation"],
        "worker_id": assignment.worker_id,
        "input_ref": assignment.agent_input["input_ref"],
        "budget_id": kernel.tasks.create_budget(wr["work_id"], "1"),
        "call_key": "p57-recovery-preflight",
        "reserve_amount": "0.1",
        "token_reservation": 120000,
    }
    blocked = gateway.execute(assignment.principal, request)
    if (blocked["status"], blocked["reason"], blocked["cost_status"]) != (
        "BLOCKED",
        "MODEL_UNAVAILABLE",
        "NOT_SENT",
    ):
        raise RuntimeError(
            "Creative transport drill did not stop at the local authorization boundary"
        )
    with kernel.db.uow(write=False) as repo:
        task = repo.one("tasks", {"task_id": request["task_id"]})
        ref, snapshot = load_snapshot(kernel, repo, task, request["input_ref"], required=True)
        if repo.many("call_attempts", {"task_id": task["task_id"]}):
            raise RuntimeError("An unavailable transport created a model attempt")
    return (
        {
            "world_ref": wr,
            "packet_ref": ref,
            "decision_ref": blocked["evidence_ref"],
            "input_ref": request["input_ref"],
            "generation": request["generation"],
            "task_id": request["task_id"],
            "compiled_sha256": fingerprint(snapshot["model_input"]),
        },
        owner,
        assignment.principal,
    )


def verify_packet(kernel, witness, owner, worker, later_ref):
    work = witness["world_ref"]["work_id"]
    packet = json.loads(kernel.get_artifact(owner, work, witness["packet_ref"]))
    blocked = json.loads(kernel.get_artifact(owner, work, witness["decision_ref"]))
    verified = validate_contract("ContextPacket", packet["packet"], "harness")
    if (
        fingerprint(packet) != witness["packet_ref"]["sha256"]
        or packet["input_ref"] != witness["input_ref"]
        or verified["scope"]["world_ref"] != witness["world_ref"]
        or verified["scope"]["generation"] != witness["generation"]
        or fingerprint(packet["model_input"]) != witness["compiled_sha256"]
        or blocked["task_id"] != witness["task_id"]
        or blocked["provider_call_sent"]
        or blocked["cost_status"] != "NOT_SENT"
    ):
        raise RuntimeError("The restored packet or original provider status differs")
    with kernel.db.uow(write=False) as repo:
        if repo.one(
            "artifacts", {"artifact_revision_id": later_ref["artifact_revision_id"]}
        ) or repo.many("call_attempts", {"task_id": witness["task_id"]}):
            raise RuntimeError("Restored a post-target write or a phantom model attempt")
    try:
        kernel.get_artifact(worker, work, witness["packet_ref"])
    except DomainError as denied:
        if denied.code != "VIEW_DENIED":
            raise
    else:
        raise RuntimeError("Raw pre-crash context became visible on the restored role grant")
    return {
        **witness,
        "preserved_source_count": len(verified["sources"]),
        "provider_cost_status": "NOT_SENT",
        "model_attempts": 0,
        "post_target_artifact_absent": True,
        "role_raw_read_denied": True,
    }
