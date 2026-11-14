"""Published continuity -> task-bound projection -> a recovered next-volume planning worker."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest
from test_continuity import entry, qualify, request, reviewer, volume
from test_p5_process_resume import _https
from test_p5_supervisor import backend

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.continuity import ContinuityService
from novel_factory.application.planning import PlanningService
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import point
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.private_files import create_private_file

pytestmark = [pytest.mark.postgres, pytest.mark.integration]
ROOT = Path(__file__).resolve().parents[2]


def test_published_volume_to_recovered_planner(kernel_env, tmp_path):
    kernel = kernel_env["kernel"]
    f, first, _ = volume(kernel)
    controller, wr = f["principal"], f["world_ref"]
    wid, branch = wr["work_id"], wr["branch_id"]
    continuity = ContinuityService(kernel)
    promise = entry(f, first, 1, kind="FORESHADOW")
    promise.update(status="OPEN", planned_payoff_at=point(40),
                   interpretation="第一卷的雨声线索在文本中留下未决问题。")
    body = request(f, [first], [promise])
    approved, _, _ = qualify(continuity, controller, reviewer(kernel, f), wid, body)
    snapshot = continuity.publish(controller, wid, approved)["snapshot_ref"]
    original = continuity.inspect(controller, wid, snapshot)["snapshot"]
    plans = PlanningService(kernel)
    with kernel.db.uow(write=False) as repo:
        source = kernel.artifact(repo, f["plan"]["volume_contract_ref"], wid, kind="VOLUME_CONTRACT")
        prefix_key = {"work_id": wid, "selection_kind": "SERIAL_PREFIX:" + f["volume_id"]}
        prefix_before = repo.one("selections", prefix_key)
        events_before = len(repo.many("events", {"work_id": wid}))
    next_contract = dict(source["payload"], volume_id=str(uuid4()), contract_revision_id=str(uuid4()),
                         creative_brief="第二卷延续第一卷留下的线索。", upstream_literary_refs=[snapshot],
                         dependency_fingerprint=fingerprint(snapshot))
    contract_ref = plans.save_volume_contract(controller, next_contract, "volume-two-contract")
    access = tmp_path / "supervisor.json"
    with os.fdopen(create_private_file(access), "w", encoding="utf-8") as out:
        json.dump({"token": f["token"], "work_id": wid, "branch_id": branch}, out)

    with backend() as (_, calls, http):
        cert = _https(http, tmp_path)
        endpoint = f"https://127.0.0.1:{http.server_port}/v1/chat/completions"
        model = "offline-window-fixture"
        route = OpenAICompatibleAdapter(endpoint=endpoint, model=model, api_key="test", timeout_seconds=60.0)
        gateway = AgentGateway(kernel, {"external": route})
        supervisor = RuntimeSupervisor(kernel, gateway)
        runtime = RuntimeTaskService(kernel, gateway)
        shared = {"story_time": point(12), "subject_ids": [f["ids"]["sword"]], "predicates": ["owner"],
                  "adapter_id": "external"}
        # Even a deliberately assigned snapshot cannot grant editorial memory to a character.
        actor = runtime.prepare(controller, wr, role_id="character_actor", task_kind="character_action",
            actor_id=f["ids"]["protagonist"], assignment="当时知道什么？", material_refs=[snapshot], **shared)
        with pytest.raises(DomainError) as denied:
            continuity.read_for_task(actor.principal, actor.task["task_id"], actor.task["generation"], snapshot)
        assert denied.value.code == "VIEW_DENIED"

        job = runtime.prepare(controller, wr, role_id="volume_director", task_kind="window_plan",
            assignment="为第二卷提出下一场的候选计划；保留线索的不确定性。", queue_state="READY",
            material_refs=[snapshot], input_artifacts=[contract_ref], **shared)
        supervisor.enroll(controller, job.task["task_id"], input_ref=job.agent_input["input_ref"],
            manifest_ref=job.manifest_ref, knowledge_cut={"plane":"WORLD"}, context_tokens=20000,
            budget_id=kernel.tasks.create_budget(wid, "1"), reserve_amount="0.1", token_reservation=100000)

        def next_window(ctx):
            materials = json.loads(calls[-1]["messages"][1]["content"])["authorized_materials"]
            memory = json.loads(materials[0]["content"])
            return {"schema_version": "2.0", "window_plan_id": str(uuid4()),
                "volume_contract_revision_id": next_contract["contract_revision_id"], "input_world_ref": wr,
                "story_interval": {"start": point(13), "end": point(14), "bounds": "[)"},
                "scope_ids": [f["ids"]["sword"]], "process_ids": [], "focus_actor_ids": [f["ids"]["protagonist"]],
                "resolution_strategy": "MIXED", "candidate_goals": [memory["entries"][0]["interpretation"]],
                "next_decision_reason": "角色可选择求证，也可暂缓。", "assumptions": [], "adopted_event_refs": [],
                "expected_outputs": ["REPLAN_REPORT"], "dependency_fingerprint": ctx["dependency_fingerprint"],
                "status": "DRAFT"}

        http.candidate = next_window
        env = {**os.environ, "NF_DATABASE_URL": kernel_env["url"], "NF_OBJECT_ROOT": str(kernel_env["store"].root),
               "NF_MODEL_ENDPOINT": endpoint, "NF_MODEL_NAME": model, "NF_MODEL_API_KEY": "test",
               "NF_MODEL_MAX_OUTPUT_TOKENS": "2048", "NF_MODEL_TIMEOUT_SECONDS": "60",
               "NO_PROXY": "127.0.0.1", "SSL_CERT_FILE": str(cert)}
        def fresh_worker():
            run = subprocess.run([sys.executable, "scripts/run_supervisor.py", "--access-file", str(access),
                                  "--once"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=40, check=False)
            assert run.returncode == 0, run.stderr
            return json.loads(run.stdout)

        started = time.monotonic()
        receipt = fresh_worker()
        assert receipt["status"] == "RESULT_STORED" and receipt["task_id"] == job.task["task_id"], receipt
        assert fresh_worker()["status"] == "IDLE" and len(calls) == 1
        elapsed = time.monotonic() - started
        packet = json.loads(calls[0]["messages"][1]["content"])
        material, = packet["authorized_materials"]
        scoped = json.loads(material["content"])
        assert material["artifact_ref"] == snapshot and scoped["snapshot_ref"] == snapshot
        assert scoped["entries"] == [{k: v for k, v in promise.items() if k != "planned_payoff_at"}]
        assert not any(s in calls[0]["messages"][1]["content"] for s in ("planned_payoff_at", "candidate_sha256"))
        assert scoped["authority"] == "DERIVED_EDITORIAL_MEMORY_NOT_WORLD_FACT_OR_ACTOR_BELIEF"
        assert scoped["world_effects_applied"] == 0

    with kernel.db.uow(write=False) as repo:
        result = repo.one("task_results", {"task_id": job.task["task_id"]})["payload"]
        assert result["status"] == "SUCCEEDED" and result["generation"] == receipt["generation"]
        candidate_ref = result["artifacts"][1]
        candidate = kernel.artifact(repo, candidate_ref, wid, kind="AGENT_CANDIDATE")["payload"]
        assert candidate["candidate_goals"] == [promise["interpretation"]]
    new_window = plans.save_window_plan(controller, candidate, contract_ref, "volume-two-window")
    assert plans.save_window_plan(controller, candidate, contract_ref, "volume-two-window") == new_window
    assert continuity.inspect(controller, wid, snapshot)["snapshot"] == original
    with kernel.db.uow(write=False) as repo:
        assert kernel.head(repo, wid, branch) == wr
        assert repo.one("selections", prefix_key) == prefix_before
        assert len(repo.many("events", {"work_id": wid})) == events_before
        if path := os.environ.get("NF_P5_CROSS_VOLUME_EVIDENCE"):
            evidence = {"postgres": repo.conn.exec_driver_sql("SHOW server_version").scalar_one(),
                        "prior_volume_release": first["release_ref"], "published_snapshot": snapshot,
                        "next_volume_contract": contract_ref, "next_volume_window": new_window,
                        "next_volume_id": next_contract["volume_id"], "task_id": job.task["task_id"],
                        "generation": receipt["generation"], "process_starts": 2,
                        "fixture": "Loopback HTTPS deterministic WindowPlan; no external provider",
                        "provider_requests": 1, "provider_requests_after_restart_replay": 0,
                        "fixture_reported_tokens": {"input": 11, "output": 7}, "elapsed_seconds": round(elapsed, 4),
                        "approved_continuity_reviews": 1, "manual_actions_during_dispatch": 0,
                        "actor_denial": denied.value.code, "future_payoff_delivered": False,
                        "world_events_added": 0, "serial_prefix_changed": False}
            Path(path).write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
