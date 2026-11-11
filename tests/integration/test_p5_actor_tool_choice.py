"""Finite model-directed actor read with two separately fenced provider sends."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from test_p5_role_container import binaries

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.codex_host import CodexRoleLookupAdapter
from novel_factory.infrastructure.model_gateway import ModelResponse, OpenAICompatibleAdapter
from novel_factory.interfaces.role_context_host import read_context

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


class ToolFixture(OpenAICompatibleAdapter):
    def __init__(self, mode, subject):
        super().__init__(endpoint="https://example.test/v1/chat/completions", model="role-tool-fixture", api_key="fixture")
        self.mode, self.subject, self.calls = mode, subject, []
        self.sent, self.release = Event(), Event()

    @property
    def configuration(self):
        return {**super().configuration, "execution_kind": "TEST_FIXTURE"}

    def generate(self, request):
        packet = json.loads(request.messages[1]["content"])
        self.calls.append(packet)
        ctx = packet["context"]
        if len(self.calls) == 1:
            if self.mode == "concurrent-reentry":
                self.sent.set()
                assert self.release.wait(timeout=15)
            tool = "nf_resume_project" if self.mode == "wrong-tool" else "nf_query_batch"
            value = {"tool": tool, "arguments": {"lookups": [{"subject_id": self.subject, "predicate": "owner"}]}}
            if self.mode == "invalid-uuid":
                value["arguments"]["lookups"][0]["subject_id"] = "invalid"
            if self.mode == "oversized-choice":
                value["arguments"]["lookups"][0]["predicate"] = "p" * 32769
            if self.mode == "skip":
                value = {"tool": None}
        else:
            payload = {"schema_version": "2.0", "proposal_id": str(uuid4()), "intent": "Wait",
                "targets": [], "resource_requests": [], "preconditions": [], "evidence_refs": [],
                "story_interval": {"start": point(1), "end": point(2), "bounds": "[)"}, "idempotency_key": "wait",
                **{k:ctx[k] for k in ("task_id","generation","context_bundle_id","actor_id","world_ref","dependency_fingerprint")}}
            value = {"schema_version":"2.1","status":"SUCCEEDED","unresolved_questions":[],"payload":payload,
                     **{k:ctx[k] for k in ("task_id","generation","context_bundle_id","actor_id","world_ref","role_id")}}
        amount = None if (self.mode, len(self.calls)) in (("unknown-first",1), ("unknown-final",2)) else Decimal("0.05")
        return ModelResponse(json.dumps(value), provider_receipt=f"fixture-{len(self.calls)}", input_tokens=22,
                             output_tokens=257 if self.mode=="over-token-cap" else 7, actual_amount=amount, currency="USD" if amount else None,
                             raw_response={"fixture": True}, request_sha256=request.request_sha256)


@pytest.mark.parametrize("mode,states", [
    ("read", ["SUCCEEDED","SUCCEEDED"]), ("resume-after-choice", ["SUCCEEDED","SUCCEEDED"]),
    ("out-of-scope", ["SUCCEEDED"]), ("concurrent-reentry", ["SUCCEEDED","SUCCEEDED"]),
    ("revoke-observation", ["SUCCEEDED"]), ("budget-prevents-final", ["SUCCEEDED"]),
    ("invalid-uuid", ["FAILED"]), ("oversized-choice", ["FAILED"]), ("over-token-cap", ["FAILED"]), ("skip", ["SUCCEEDED","SUCCEEDED"]),
    ("unknown-first", ["UNKNOWN"]), ("unknown-final", ["SUCCEEDED","UNKNOWN"]), ("wrong-tool", ["FAILED"]),
])
def test_model_selects_scoped_lookup_before_final_candidate(kernel_env, tmp_path, monkeypatch, mode, states):
    codex, relay = binaries(tmp_path)
    kernel = kernel_env["kernel"]
    world = load_fixture(kernel)
    wr, admin = world["world_ref"], world["principal"]
    actor, sword = world["ids"]["protagonist"], world["ids"]["sword"]
    provider = ToolFixture(mode, world["ids"]["rival"] if mode == "out-of-scope" else sword)
    route = CodexRoleLookupAdapter(provider, HostJournal(kernel), binary=codex, context_reader=read_context, relay=relay)
    gateway = AgentGateway(kernel, {"codex-role-tools":route})
    scheduler = RuntimeSupervisor(kernel,gateway)
    job = RuntimeTaskService(kernel,gateway).prepare(admin, wr, role_id="character_actor", task_kind="character_action",
        actor_id=actor, story_time=point(1), subject_ids=[sword], predicates=["owner"], assignment="Decide",
        adapter_id="codex-role-tools", queue_state="READY")
    scheduler.enroll(admin, job.task["task_id"], manifest_ref=job.manifest_ref,
        input_ref=job.agent_input["input_ref"], knowledge_cut={"plane":"BELIEF","actor_id":actor,"known_at":point(1)},
        budget_id=kernel.tasks.create_budget(wr["work_id"],"0.51" if mode == "budget-prevents-final" else "2"), reserve_amount="1", context_tokens=20000,
        token_reservation=100000)
    if mode == "revoke-observation":
        store = gateway._audit_artifact
        def revoke_after_read(binding, identity, label, payload, kind):
            ref = store(binding, identity, label, payload, kind)
            if label == "observation":
                kernel.revoke_grant(admin, job.principal.grant_id)
            return ref
        monkeypatch.setattr(gateway,"_audit_artifact",revoke_after_read)

    def dispatch_args():
        with kernel.db.uow(write=False) as repo:
            task = repo.one("tasks", {"task_id": job.task["task_id"]})
            cfg = repo.one("supervisor_jobs", {"task_id": task["task_id"]})["config"]
        return {"schema_version":"2.1", "task_id":task["task_id"], "generation":task["generation"],
                "worker_id":task["lease_owner"], "call_key":"supervised-candidate", **cfg["dispatch"]}

    if mode == "concurrent-reentry":
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(scheduler.run_once, admin, wr["work_id"], wr["branch_id"])
            assert provider.sent.wait(timeout=15)
            try:
                with pytest.raises(DomainError) as blocked:
                    gateway.execute(job.principal, dispatch_args())
                assert blocked.value.code == "CALL_RECONCILIATION_REQUIRED"
                assert len(provider.calls) == 1
            finally:
                provider.release.set()
            result = first.result(timeout=20)
    elif mode == "resume-after-choice":
        class WorkerStopped(BaseException):
            pass
        lookup = route.actor_lookup
        def interrupted(*args):
            raise WorkerStopped
        route.actor_lookup = interrupted
        with pytest.raises(WorkerStopped):
            scheduler.run_once(admin, wr["work_id"], wr["branch_id"])
        route.actor_lookup = lookup
        # Re-enter via a fresh gateway under the still-live original worker lease.
        # A generation/lease that has already expired remains fenced.
        recovered = AgentGateway(kernel, {"codex-role-tools": route})
        result = recovered.execute(job.principal, dispatch_args())
    else:
        result = scheduler.run_once(admin, wr["work_id"], wr["branch_id"])
    assert result["status"] == ("RESULT_STORED" if len(states)==2 and all(s=="SUCCEEDED" for s in states) else "BLOCKED"), result
    with kernel.db.uow(write=False) as repo:
        calls = sorted(repo.many("call_attempts", {"task_id":job.task["task_id"]}),key=lambda c:c["call_key"])
        assert [c["state"] for c in calls] == states
        assert [c["call_key"] for c in calls] == ["actor-lookup-choice-v1", "supervised-candidate"][:len(states)]
        assert sum(c["reserved_amount"] for c in calls) <= 1
        assert sum(c["reserved_tokens"] for c in calls) <= 100000
        assert len(calls) == len(provider.calls)
        turns = [repo.one("host_turns", {"attempt_id":c["attempt_id"]}) for c in calls]
        assert all(t["phase"] == "COMPLETED" for t in turns)
        assert len({t["turn_id"] for t in turns}) == len(calls)
        obs = repo.many("artifacts", {"work_id":wr["work_id"],"kind":"AGENT_ROLE_TOOL_RESULT"})
        if len(calls) == 2:
            lookup = provider.calls[1]["role_lookup"]
            if mode == "skip":
                assert lookup == {"tool":None} and not obs
            else:
                assert lookup["tool"] == "nf_query_batch" and lookup["result"]["all_items_succeeded"]
                answer = lookup["result"]["items"][0]["payload"]["state_items"][0]["answer"]
                assert answer["plane"] == "BELIEF" and answer["knowledge_status"] == "UNKNOWN" and answer["data"] is None
                assert len(obs)==1 and obs[0]["payload"]["result"] == lookup["result"]
                if mode == "read" and (evidence := os.environ.get("NF_P5_ROLE_TOOL_CHOICE_EVIDENCE")):
                    trace = {"model": "deterministic selection and final inference fixtures; no external bill",
                        "postgres":repo.conn.exec_driver_sql("SHOW server_version").scalar_one(),
                        "selected_tool":lookup["tool"],"response_sha256":fingerprint(lookup["result"]),
                        "lookup_thread":obs[0]["payload"]["thread_id"],"turns":[t["turn_id"] for t in turns],
                        "call_keys":[c["call_key"] for c in calls],"accounting":states,"provider_calls":len(provider.calls),
                        "candidate":result["validation_status"]}
                    Path(evidence).write_text(json.dumps(trace,indent=2)+"\n")
        elif mode in ("budget-prevents-final", "revoke-observation"):
            assert len(obs) == 1
            if mode == "budget-prevents-final":
                assert result["reason"] == "BUDGET_EXCEEDED"
            else:
                assert result["reason"] == "VIEW_DENIED"
        else:
            assert not obs
    assert scheduler.run_once(admin, wr["work_id"], wr["branch_id"])["status"] == "IDLE"
    assert len(provider.calls) == len(states)

    if mode == "read":
        from novel_factory.application.statistics import StatisticsService
        summary = StatisticsService(kernel).rebuild(admin,wr["work_id"],wr["branch_id"])
        assert summary["metrics"]["model.fixture.calls"] == 2
        assert summary["metrics"]["model.fixture.total_tokens"] == 58
        assert Decimal(summary["metrics"]["model.fixture.cost.USD"]) == Decimal("0.10")
