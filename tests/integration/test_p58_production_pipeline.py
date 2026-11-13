"""Real PG supervisor, HTTP fixture, immutable drafts and independent continuity publication."""
import json
import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from test_continuity import qualify, request, reviewer, volume
from test_p5_supervisor import backend
from test_planning import make_plan

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.continuity import ContinuityService
from novel_factory.application.planning import PlanningService
from novel_factory.application.production_pipeline import (
    pipeline_status,
    production_material_reader,
    register_pipeline,
    revise_window,
    transfer_window,
)
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import entity_value, point, run_command

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def uid():
    return str(uuid4())


def test_native_parallel_drafts_conditional_plans_and_early_review(kernel_env):
    kernel = kernel_env["kernel"]
    f, release, _ = volume(kernel)
    original, owner = f["world_ref"], f["principal"]
    work, branch = original["work_id"], original["branch_id"]
    target, _ = make_plan(PlanningService(kernel), f)
    ids = f["ids"]
    with kernel.db.uow(write=False) as repo:
        contract = kernel.artifact(repo, target["volume_contract_ref"], work)["payload"]
    series = uid()
    chapters = [{"chapter_id": uid(), "scenes": [{"scene_ref": scene, "handoff_refs": []}]} for scene in f["scene_refs"]]
    candidate_ids = [uid(), uid()]
    futures = [{"candidate_id": ident, "distance": 1, "volume_contract_ref": target["volume_contract_ref"],
                "assumptions": [dep]} for ident, dep in zip(candidate_ids, [
                    {"kind": "WORLD_FACT", "condition": {"kind": "STATE_EQUALS", "subject_id": ids["protagonist"],
                     "predicate": "located_in", "expected_value": entity_value(ids["north"])}},
                    {"kind": "LITERARY_COMMITMENT", "node_id": f["plan"]["nodes"][0]["node_id"]}], strict=True)]
    options = {"world_ref": original, "series_id": series, "volume_id": f["volume_id"], "chapters": chapters, "future": futures,
                   "story_time": point(12), "subject_ids": [ids["protagonist"], ids["sword"], ids["north"], ids["south"]],
                   "predicates": ["possessed_by", "located_in"], "routes": {"draft": "fixture", "review": "fixture", "plan": "fixture"},
                   "budget_id": kernel.tasks.create_budget(work, "2"), "reserve_amount": "0.1", "token_reservation": 100000,
                   "limits": {"draft": 1, "review": 1, "plan": 2, "lookahead": 1, "review_backlog": 1, "window_goals": 2},
                   "deadline_seconds": 3600, "idempotency_key": "p58-production-test"}
    with backend(concurrent=True) as (http, calls, server):
        gateway = AgentGateway(kernel, {"fixture": http}, material_reader=production_material_reader(kernel))
        def model(packet):
            ctx = packet["context"]
            if ctx["role_id"] == "writer":
                scene = json.loads(packet["authorized_materials"][0]["content"])
                assert "scene_contract" in scene and "assumptions" not in packet["assignment"]
                return {"text": "北窗的雨声很轻。", "event_refs": [], "important_deltas": [], "evidence_refs": []}
            if ctx["role_id"] == "reviewer":
                assert "北窗的雨声很轻。" in packet["authorized_materials"][0]["content"]
                return {"summary": "合成测试中的章节阅读意见。", "issues": [], "counterinterpretations": [], "evidence_refs": []}
            assert ctx["role_id"] == "volume_director" and packet["read_cut"]["knowledge_cut"]["plane"] == "PLAN"
            fixed = packet["proposal_protocol"]["copy_into_payload"]
            assert fixed["input_world_ref"] == ctx["world_ref"] and fixed["dependency_fingerprint"] == ctx["dependency_fingerprint"]
            assert ids["south"] in packet["assignment"] and fixed["status"] == "DRAFT"
            return {**fixed, "volume_contract_revision_id": contract["contract_revision_id"],
                    "story_interval": {"start": point(13), "end": point(14), "bounds": "[)"},
                    "scope_ids": [ids["south"]], "focus_actor_ids": [ids["protagonist"]], "process_ids": [],
                    "resolution_strategy": "MIXED", "candidate_goals": ["前提成立时检查渡口"],
                    "next_decision_reason": "复核前卷结局", "assumptions": [], "adopted_event_refs": [],
                    "expected_outputs": ["REPLAN_REPORT"]}
        server.packet_candidate = model
        invalid = deepcopy(options)
        invalid["future"][0]["distance"] = 2
        with pytest.raises(DomainError):
            register_pipeline(kernel, gateway, owner, **invalid)
        registered = register_pipeline(kernel, gateway, owner, **options)
        assert register_pipeline(kernel, gateway, owner, **options) == registered and not calls
        def tick():
            return RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)
        with ThreadPoolExecutor(3) as pool:
            initial = list(pool.map(lambda _: tick(), range(3)))
        assert sorted(x["node_id"].split(":")[0] for x in initial) == ["draft", "plan", "plan"], initial
        assert all(x["state"] == "ENROLLED" for x in initial), initial
        status = lambda: pipeline_status(kernel, owner, work, branch)["nodes"]
        with pytest.raises(DomainError):
            pipeline_status(kernel, reviewer(kernel, f), work, branch)
        saw_early_review = False
        outcomes = []
        for _ in range(26):
            step = tick()
            outcomes.append(step)
            if step["status"] == "IDLE":
                break
            if step.get("state") == "ENROLLED" and step["node_id"].startswith("review:"):
                saw_early_review |= any(n["stage"] == "draft" and n["state"] == "PENDING" for n in status().values())
        assert saw_early_review, outcomes
        nodes = status()
        assert len(calls) == 6 and all(n["state"] == {"draft": "DRAFT_STORED", "review": "ADVISORY_REVIEW",
                "plan": "PROVISIONAL_WITH_ASSUMPTIONS"}[n["stage"]] for n in nodes.values()), outcomes
        with kernel.db.uow(write=False) as repo:
            assert kernel.head(repo, work, branch) == original
            assert not repo.many("events", {"work_id": work})
            assert not repo.one("selections", {"work_id": work, "selection_kind": "PLAN:" + target["volume_id"]})
        with pytest.raises(DomainError, match="continuity"):
            transfer_window(kernel, owner, world_ref=original, candidate_id=candidate_ids[1], idempotency_key="unsafe")
        live = {**f, "world_ref": run_command(kernel, owner, original, "SET_RELATION", {
            "subject_id": ids["protagonist"], "predicate": "located_in", "object_value": entity_value(ids["south"]),
            "at": point(9)})["world_ref"]}
        cont = ContinuityService(kernel)
        approval, _, _ = qualify(cont, owner, reviewer(kernel, live), work, request(live, [release], series_id=series))
        cont.publish(owner, work, approval)
        for _ in range(4):
            if tick()["status"] == "IDLE":
                break
        now = status()
        assert now["plan:" + candidate_ids[0]]["state"] == "REPLAN_REQUIRED"
        assert now["plan:" + candidate_ids[1]]["state"] == "READY_FOR_ADOPTION"
        assert all(now[k] == n for k, n in nodes.items() if n["stage"] != "plan")
        sent = transfer_window(kernel, owner, world_ref=live["world_ref"], candidate_id=candidate_ids[1], idempotency_key="transfer")
        assert transfer_window(kernel, owner, world_ref=live["world_ref"], candidate_id=candidate_ids[1], idempotency_key="transfer") == sent
        with kernel.db.uow(write=False) as repo:
            assert kernel.artifact(repo, sent["window_ref"], work, kind="WINDOW_PLAN")["payload"]["status"] == "DRAFT"
            assert kernel.head(repo, work, branch) == live["world_ref"]
        assert len(calls) == 6
        exported = pipeline_status(kernel, owner, work, branch, include_outputs=True)
        assert all(n["attempt_ids"] for n in exported["nodes"].values())
        assert exported["literary_quality"] == "NOT_EVALUATED"
        assert all(v["text"] == "北窗的雨声很轻。" for k, v in exported["outputs"].items() if k.startswith("draft:"))
        affected = now["plan:" + candidate_ids[0]]
        revised = revise_window(kernel, owner, world_ref=live["world_ref"], candidate_id=candidate_ids[0],
            expected_candidate_ref=affected["output_ref"], assumptions=[{"kind": "WORLD_FACT", "condition": {
                "kind": "ENTITY_ACTIVE", "subject_id": ids["protagonist"]}}], idempotency_key="one-local-revision")
        assert revised["prior_candidate_ref"] == affected["output_ref"]
        for _ in range(8):
            if tick()["status"] == "IDLE":
                break
        updated = status()
        assert updated["plan:" + candidate_ids[0]]["state"] == "READY_FOR_ADOPTION", updated["plan:" + candidate_ids[0]]
        current = updated["plan:" + candidate_ids[0]]
        prior = current["history"][0]
        assert prior["task_id"] == affected["task_id"]
        assert prior["attempt_ids"] == affected["attempt_ids"]
        assert current["attempt_ids"] and set(current["attempt_ids"]).isdisjoint(prior["attempt_ids"])
        with kernel.db.uow(write=False) as repo:
            for record in (prior, current):
                attempts = repo.many("call_attempts", {"task_id": record["task_id"]})
                assert set(record["attempt_ids"]) == {row["attempt_id"] for row in attempts}
        assert len(calls) == 7 and all(updated[k] == n for k, n in now.items() if n["stage"] != "plan")
        # An expired READY candidate must relinquish the branch slot, even with a published predecessor.
        from datetime import timedelta
        from unittest.mock import patch

        from novel_factory.infrastructure.repository import KernelRepository
        with kernel.db.uow(write=False) as repo:
            later = repo.now() + timedelta(days=2)
        with patch.object(KernelRepository, "now", return_value=later):
            tick()
            assert status()["plan:" + candidate_ids[0]]["state"] == "STOPPED"
            with pytest.raises(DomainError):
                transfer_window(kernel, owner, world_ref=live["world_ref"], candidate_id=candidate_ids[0],
                                idempotency_key="expired")
        assert len(calls) == 7
        if output_path := os.environ.get("NF_P58_PIPELINE_TEST_EVIDENCE"):
            with kernel.db.uow(write=False) as repo:
                tasks = [n["task_id"] for n in updated.values()] + [affected["task_id"]]
                attempts = repo.many("call_attempts", {"task_id": tasks})
                ledger = [{k: str(c[k]) if c[k] is not None else None for k in
                          ("task_id", "generation", "attempt_id", "request_hash", "state", "budget_id",
                           "actual_amount")} for c in attempts]
            Path(output_path).write_text(json.dumps({"kind": "DETERMINISTIC_LOOPBACK_HTTP", "real_model_calls": 0,
                "settled_provider_expense": None, "literary_quality": "NOT_EVALUATED", "initial_world_ref": original,
                "registered": registered, "first_ready_nodes": initial, "production_handoffs": outcomes,
                "export_before_local_revision": exported, "after_local_revision": updated, "nf_attempts": ledger,
                "fixture_http_request_count": len(calls)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_book_bootstrap_cli_creates_two_isolated_unenrolled_books(kernel_env, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    script = root / "scripts/p58_bootstrap_book.py"
    template = root / "reports/p6_readiness/templates/book.json"
    env = {**os.environ, "NF_DATABASE_URL": kernel_env["url"], "NF_OBJECT_ROOT": str(kernel_env["store"].root)}
    def create(name):
        return json.loads(subprocess.check_output([sys.executable, str(script), "--book", str(template),
                          "--private-dir", str(tmp_path / name)], env=env))
    a, b = create("a"), create("b")
    assert create("a")["status"] == "EXISTS" and a["world_ref"]["work_id"] != b["world_ref"]["work_id"]
    assert a["model_requests"] == b["model_requests"] == 0 and a["budget_id"] != b["budget_id"]
    k = kernel_env["kernel"]
    for name, receipt in (("a", a), ("b", b)):
        request_json = json.loads((tmp_path / name / "pipeline.json").read_text())
        access = json.loads((tmp_path / name / "supervisor.json").read_text())
        assert (tmp_path / name / "supervisor.json").stat().st_mode & 0o077 == 0
        assert (tmp_path / name / "control.json").stat().st_mode & 0o077 == 0
        budget = json.loads(subprocess.check_output([sys.executable, str(root / "scripts/p6_budget.py"),
                       "--binding", str(tmp_path / name / "control.json"), "show",
                       "--budget-id", receipt["budget_id"]], env=env))
        assert budget["work_id"] == receipt["world_ref"]["work_id"] and float(budget["limit_amount"]) == 0
        gateway = AgentGateway(k, {})
        receipt2 = register_pipeline(k, gateway, k.authenticate(access["token"]), **request_json)
        assert receipt2["nodes"] == 5
        with k.db.uow(write=False) as repo:
            assert not repo.many("tasks", {"work_id": receipt["world_ref"]["work_id"]})


def test_unresolved_writer_cannot_restart_under_replacement_board(kernel_env):
    from test_release import setup_batch

    k = kernel_env["kernel"]
    f = setup_batch(k, count=1)
    wr, owner = f["world_ref"], f["principal"]
    work, branch = wr["work_id"], wr["branch_id"]
    with backend() as (adapter, calls, server):
        gateway = AgentGateway(k, {"fixture": adapter}, material_reader=production_material_reader(k))
        supervisor = RuntimeSupervisor(k, gateway)
        params = {"world_ref": wr, "volume_id": f["volume_id"], "series_id": uid(), "chapters": [
            {"chapter_id": uid(), "scenes": [{"scene_ref": f["scene_refs"][0], "handoff_refs": []}]}],
            "future": [], "story_time": point(2), "subject_ids": [f["ids"]["sword"]],
            "predicates": ["possessed_by"], "routes": {k: "fixture" for k in ("plan", "review", "draft")},
            "budget_id": k.tasks.create_budget(work, "1"), "reserve_amount": "0.1", "token_reservation": 100000,
            "limits": {"draft": 1, "review": 1, "plan": 1, "review_backlog": 1, "window_goals": 1, "lookahead": 1},
            "deadline_seconds": 3600, "idempotency_key": "unresolved"}
        register_pipeline(k, gateway, owner, **params)
        server.lose_response = True
        for _ in range(7):
            if supervisor.run_once(owner, work, branch)["status"] == "IDLE":
                break
        state = pipeline_status(k, owner, work, branch)
        assert all(n["state"] == "STOPPED" for n in state["nodes"].values()), state
        with pytest.raises(DomainError) as exc:
            register_pipeline(k, gateway, owner, **{**params, "idempotency_key": "different-key"})
        assert exc.value.code == "CALL_RECONCILIATION_REQUIRED" and len(calls) == 1
