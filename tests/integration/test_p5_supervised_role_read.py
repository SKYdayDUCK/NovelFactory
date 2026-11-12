"""Actual autonomous dispatch via a character-scoped pre-inference MCP read."""
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from test_p5_role_container import binaries
from test_p5_supervisor import backend

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.domain.canonical import fingerprint
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.codex_host import CodexRoleContextAdapter
from novel_factory.interfaces.role_context_host import read_context

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.mark.parametrize("missing_relay", [False, True], ids=["single-send", "fail-before-reserving"])
def test_supervisor_uses_bounded_role_read(kernel_env, tmp_path, missing_relay):
    codex, relay = binaries(tmp_path)
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    wr, admin = world["world_ref"], world["principal"]
    actor = world["ids"]["protagonist"]
    with backend() as (http, requests, provider):
        route = CodexRoleContextAdapter(http, HostJournal(kernel), binary=codex, context_reader=read_context,
                                        relay=tmp_path / "absent" if missing_relay else relay)
        gateway = AgentGateway(kernel, {"codex-role-mcp": route})
        supervisor = RuntimeSupervisor(kernel, gateway)
        job = RuntimeTaskService(kernel, gateway).prepare(admin, wr, role_id="character_actor",
            task_kind="character_action", story_time=point(1), subject_ids=[actor], predicates=["location"],
            actor_id=actor, assignment="Choose whether to attend", adapter_id="codex-role-mcp", queue_state="READY")
        supervisor.enroll(admin, job.task["task_id"], manifest_ref=job.manifest_ref,
            input_ref=job.agent_input["input_ref"], knowledge_cut={"plane": "BELIEF", "actor_id": actor, "known_at": point(1)},
            budget_id=kernel.tasks.create_budget(wr["work_id"], "2"), reserve_amount="1",
            context_tokens=20000, token_reservation=100000)

        def choose(ctx):
            return {"schema_version": "2.0", "proposal_id": str(uuid4()), "task_id": ctx["task_id"],
                    "generation": ctx["generation"], "context_bundle_id": ctx["context_bundle_id"], "world_ref": wr,
                    "actor_id": ctx["actor_id"], "intent": "Wait", "targets": [], "resource_requests": [],
                    "story_interval": {"start": point(1), "end": point(2), "bounds": "[)"}, "preconditions": [],
                    "evidence_refs": [], "dependency_fingerprint": ctx["dependency_fingerprint"], "idempotency_key": "wait"}

        provider.candidate = choose
        result = supervisor.run_once(admin, wr["work_id"], wr["branch_id"])
        with kernel.db.uow(write=False) as repo:
            calls = repo.many("call_attempts", {"task_id": job.task["task_id"]})
            if missing_relay:
                assert result["status"] == "BLOCKED" and result["cost_status"] == "NOT_SENT"
                assert not requests and not calls
            else:
                assert result["status"] == "RESULT_STORED" and result["validation_status"] == "VALIDATED_CANDIDATE", result
                assert len(requests) == len(calls) == 1
                audit = kernel.artifact(repo, result["evidence_ref"], wr["work_id"])["payload"]
                raw = kernel.artifact(repo, audit["response_ref"], wr["work_id"])["payload"]
                metadata = json.loads(raw["provider_response_json"])
                ctx = json.loads(requests[0]["messages"][1]["content"])["context"]
                read = metadata["role_mcp_read"]
                assert ctx["actor_id"] == actor and read["context_sha256"] == fingerprint(ctx)
                assert read["model_turns"] == 0 and read["tool"] == "nf_context_get"
                assert read["thread_id"] != metadata["codex_thread_id"]
                if evidence := os.environ.get("NF_P5_SUPERVISED_ROLE_READ_EVIDENCE"):
                    trace = {"database": repo.conn.exec_driver_sql("SHOW server_version").scalar_one(),
                             "provider": "loopback deterministic HTTP fixture", "role_read": read,
                             "selection": "controller-fixed nf_context_get before one provider request",
                             "completion_thread": metadata["codex_thread_id"], "completion_turn": metadata["codex_turn_id"],
                             "provider_requests": len(requests), "ledger_attempts": len(calls),
                             "candidate_status": result["validation_status"], "paid_model": "NOT_RUN"}
                    Path(evidence).write_text(json.dumps(trace, indent=2) + "\n")
        assert supervisor.run_once(admin, wr["work_id"], wr["branch_id"])["status"] == "IDLE"
        assert len(requests) == (0 if missing_relay else 1)
