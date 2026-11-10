"""Controller provisioning for one finite, least-privilege runtime task.

This composes existing services. It does not grant artifact access merely by
listing inputs: narrative reader/scene bindings remain explicit controller
operations. No model is called until a caller separately dispatches the task.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import require
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.query_policy import profiles
from novel_factory.contracts.registry import validate_contract


@dataclass(frozen=True)
class PreparedRuntimeTask:
    principal: Principal
    task: dict
    spec: dict
    agent_input: dict
    manifest_ref: dict
    context_bundle_id: str
    worker_id: str
    # Only a caller's private credential file may serialize this field.
    private_token: str = field(repr=False)


class RuntimeTaskService:
    def __init__(self, kernel, gateway):
        self.kernel, self.gateway = kernel, gateway

    def prepare(self, controller, world_ref, *, role_id, task_kind, story_time,
                subject_ids, predicates, assignment, adapter_id,
                knowledge_cut=None, actor_id=None, input_artifacts=(), material_refs=(),
                max_amount="1", token_budget=200_000, context_token_budget=20_000,
                currency="USD", lease_seconds=600, deadline_seconds=3600,
                skill_name=None):
        """Create, lease and compile a task using a fixed world and runtime.

        The controller must possess ADMIN already. Runtime grants receive only
        READ/COMPILE/TASK_CREATE/TASK_CLAIM/ARTIFACT_WRITE and, solely for a
        selected PLAN cut, PLAN_READ. They never receive world acceptance,
        approval, repair, promotion, branch administration or model tools.
        """
        validate_contract("WorldRef", world_ref)
        validate_contract("StoryPoint", story_time)
        require(type(deadline_seconds) is int and 1 <= deadline_seconds <= 86400,
                "INVALID_ARGUMENT", "Runtime deadline must be finite")
        require(type(lease_seconds) is int and 1 <= lease_seconds <= min(3600, deadline_seconds),
                "INVALID_ARGUMENT", "Runtime lease must fit the deadline")
        role = profiles().get(role_id)
        require(role is not None, "ROLE_MODE_DENIED", "Runtime role is not registered")
        cut = knowledge_cut or ({"plane": "BELIEF", "actor_id": actor_id, "known_at": story_time}
                                if role_id == "character_actor" else {"plane": "WORLD"})
        validate_contract("KnowledgeCut", cut, "2.1")
        require(cut["plane"] in role["allowed_planes"], "VIEW_DENIED", "Selected cut is outside the runtime role")
        if role_id == "character_actor":
            require(actor_id is not None and cut["plane"] == "BELIEF" and cut["actor_id"] == actor_id,
                    "VIEW_DENIED", "A character task requires its own knowledge cut")
        with self.kernel.db.uow(write=False) as repo:
            self.kernel.authorize(repo, controller, world_ref["work_id"], "ADMIN", world_ref["branch_id"])
            self.kernel.revision(repo, world_ref)
            policy_version = repo.one("works", {"work_id": world_ref["work_id"]})["policy_version"]
        prepared = self.gateway.prepare_input(controller, world_ref, role_id=role_id,
            task_kind=task_kind, assignment=assignment, adapter_id=adapter_id,
            skill_name=skill_name, material_refs=material_refs)
        manifest = self.kernel.put_artifact(controller, world_ref["work_id"],
            {"subject_ids": list(subject_ids), "predicates": list(predicates)}, "INPUT_MANIFEST")
        caps = ["READ", "COMPILE", "TASK_CREATE", "TASK_CLAIM", "ARTIFACT_WRITE"]
        if cut["plane"] == "PLAN":
            caps.append("PLAN_READ")
        granted = self.kernel.issue_grant(controller, world_ref["work_id"], role_id=role_id,
            actor_id=actor_id, capabilities=caps, planes=[cut["plane"]], max_story_time=story_time)
        principal = granted["principal"]
        inputs = []
        for ref in [manifest, prepared["input_ref"], *input_artifacts, *material_refs]:
            if ref not in inputs:
                inputs.append(ref)
        task_id = str(uuid4())
        spec = {"schema_version": "2.0", "task_id": task_id, "generation": 0,
            "role_id": role_id, "task_kind": task_kind, "state": "READY", "world_ref": world_ref,
            "story_time": story_time, "context_bundle_id": None, "policy_version": policy_version,
            "dependency_fingerprint": None, "runtime_contract_version": prepared["runtime_contract_version"],
            "capability_grant_id": principal.grant_id, "input_artifacts": inputs,
            "budget": {"currency": currency, "max_amount": max_amount, "token_budget": token_budget},
            "wall_deadline": (datetime.now(timezone.utc) + timedelta(seconds=deadline_seconds)).isoformat()}
        self.kernel.create_task(principal, spec, "runtime-create:" + task_id)
        worker = "runtime:" + task_id
        task = self.kernel.claim_task(principal, worker, task_id=task_id, lease_seconds=lease_seconds)
        context = WorldQueryService(self.kernel).context_compile(principal, {
            "schema_version": "2.1", "task_id": task_id, "generation": task["generation"],
            "recipe_id": role["allowed_recipes"][0], "input_manifest_ref": manifest,
            "read_contract": {"revision": {"mode": "FIXED", "world_ref": world_ref},
                              "process_policy": "ALLOW_PARTIAL", "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
            "knowledge_cut": cut, "subject_time": {"kind": "POINT", "at": story_time},
            "token_budget": context_token_budget, "idempotency_key": "runtime-compile:" + task_id})
        require(context["status"] == "READY", "CONTEXT_REQUIRED", "Runtime compilation is blocked")
        with self.kernel.db.uow(write=False) as repo:
            current = repo.one("tasks", {"task_id": task_id})
        return PreparedRuntimeTask(principal, task, spec, prepared, manifest,
                                   current["context_bundle_id"], worker, granted["token"])
