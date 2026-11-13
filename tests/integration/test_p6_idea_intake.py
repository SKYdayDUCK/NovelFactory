"""Use the CLI's native service path with a disclosed fixture upstream on real PostgreSQL."""

import importlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from test_p5_supervisor import backend
from test_p6_rolling_production import answer

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.production_pipeline import (
    pipeline_status,
    production_material_reader,
)
from novel_factory.application.rolling_production import rolling_status
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.interfaces.bindings import read_private_config, runtime_from_config

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.mark.parametrize("outcome", ["approved", "rejected", "unknown_cost", "continue"])
def test_model_design_delivery_recovery_and_scope(kernel_env, tmp_path, monkeypatch, outcome):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    cli = importlib.import_module("p6_idea")
    kernel = kernel_env["kernel"]
    idea = {
        "idea": "一位无名画师接到委托。",
        "length": {"preset": "short", "unit": "han_characters", "min": 6000, "max": 10000},
        "genre": "现实",
    }
    if outcome == "continue":
        idea["length"] = {"preset": "medium_long", "unit": "chapters", "min": 3, "max": 3}
    allowance = {
        "adapter_id": "test",
        "reserve_amount": "0.1",
        "total_amount": "5",
        "currency": "USD",
        "total_tokens": 2000000,
        "token_reservation": 100000,
    }
    with backend() as (adapter, requests, server):
        generate = adapter.generate

        def settled_fixture(request):
            return replace(generate(request), actual_amount=Decimal(0), currency="USD")

        if outcome != "unknown_cost":
            monkeypatch.setattr(adapter, "generate", settled_fixture)
        gateway = AgentGateway(
            kernel, {"test": adapter}, material_reader=production_material_reader(kernel)
        )
        expected = {
            "title": "测试计划",
            "protagonist": "人物甲",
            "place": "地点乙",
            "storyline": "验" * 4000,
            "world_rules": ["公开的定价有效"],
            "chapter_goals": ["选择", "解决"],
        }

        def candidate(packet):
            if packet["context"]["role_id"] == "world_designer":
                return expected
            if packet["context"]["role_id"] == "volume_director":
                assert "北窗的雨声很轻" in packet["assignment"]
                return {"volume_brief": "试验第二卷", "chapter_goals": ["试验结局"]}
            if "candidate_ref" not in packet["assignment"]:
                return answer(packet)
            ref = json.loads(packet["assignment"])["candidate_ref"]
            issues = (
                []
                if outcome in {"approved", "continue"}
                else [
                    {
                        "issue_id": str(uuid4()),
                        "category": "CONTINUITY",
                        "severity": "BLOCKER",
                        "subject_ref": ref,
                        "description": "Synthetic inconsistency",
                        "evidence_refs": [],
                        "suggested_change": "Repair the inconsistency",
                        "disposition": "OPEN",
                        "issue_kind": "HARD_CONFLICT",
                    }
                ]
            )
            return {
                "summary": "fixture checked design",
                "issues": issues,
                "counterinterpretations": [],
                "evidence_refs": [],
            }

        server.packet_candidate = candidate
        private = tmp_path / "private-project"

        def run(action):
            return cli.run_intake(action, kernel, gateway, private, idea, allowance)

        enrolled = run("enroll")
        assert run("enroll")["task_id"] == enrolled["task_id"]
        with pytest.raises(DomainError, match="different idea"):
            cli.run_intake("enroll", kernel, gateway, private, {**idea, "idea": "变更"}, allowance)
        assert run("status")["state"] == "READY" and not requests
        if outcome == "unknown_cost":
            assert run("advance")["cost_status"] == "UNKNOWN_COST"
            for _ in range(2):
                with pytest.raises(DomainError) as pending:
                    run("advance")
                assert pending.value.code == "CALL_RECONCILIATION_REQUIRED"
            assert len(requests) == 1 and not (private / "first-window.json").exists()
            return
        for _ in range(9):
            receipt = run("advance")
            if receipt.get("status") in {"INITIAL_PLAN_MATERIALIZED", "DESIGN_REWORK_REQUIRED"}:
                break
        if outcome == "rejected":
            assert receipt["status"] == "DESIGN_REWORK_REQUIRED"
            assert len(requests) == 2 and not (private / "first-window.json").exists()
            assert run("advance") == receipt and len(requests) == 2
            return
        assert receipt.get("status") == "INITIAL_PLAN_MATERIALIZED", receipt
        assert (
            receipt["execution_kind"] == "TEST_FIXTURE"
            and receipt["source_input_ref"] == enrolled["input_ref"]
        )
        assert receipt == run("advance") and len(requests) == 2
        assert (
            "画师" in requests[0]["messages"][1]["content"]
            and "人物甲" not in requests[0]["messages"][1]["content"]
        )
        assert receipt["world_ref"]["work_id"] != enrolled["world_ref"]["work_id"]
        assert receipt["design_review"]["task_id"] != receipt["task_id"]
        assert receipt["design_review"]["attempt_id"] != receipt["attempt_id"]
        with kernel.db.uow(write=False) as repo:
            work = receipt["world_ref"]["work_id"]
            assert kernel.artifact(repo, receipt["design_ref"], work)["payload"] == expected
            assert len(kernel.artifact(repo, receipt["plan_ref"], work)["payload"]["nodes"]) == 2
            dag = kernel.artifact(repo, receipt["dag_ref"], work)["payload"]
            assert dag["profile_ref"] == receipt["profile_ref"]
            assert (
                repo.conn.scalar(
                    text("SELECT COUNT(*) FROM nf_call_attempts WHERE task_id=:t"),
                    {"t": receipt["task_id"]},
                )
                == 1
            )
            assert repo.one("tasks", {"task_id": receipt["task_id"]})["state"] == "ACCEPTED"
        registered = run("register-window")
        assert (
            registered == run("register-window")
            and registered["budget_id"] != enrolled["budget_id"]
        )
        for name, expected_wr in (
            ("preparation-control.json", enrolled["world_ref"]),
            ("control.json", receipt["world_ref"]),
        ):
            config = read_private_config(private / name)
            assert config["binding"] == {key: expected_wr[key] for key in ("work_id", "branch_id")}
            runtime = runtime_from_config(config)
            try:
                assert runtime.binding.work_id == expected_wr["work_id"]
            finally:
                runtime.kernel.db.close()
        prep = cli._load_private(private / "preparation.json")
        with kernel.db.uow() as repo, pytest.raises(DomainError) as denied:
            kernel.authorize(
                repo, kernel.authenticate(prep["token"]), receipt["world_ref"]["work_id"], "ADMIN"
            )
        assert denied.value.code == "VIEW_DENIED"

        if outcome == "continue":
            canon = cli._load_private(private / "supervisor.json")
            owner, wr = kernel.authenticate(canon["token"]), receipt["world_ref"]
            for _ in range(65):
                step = RuntimeSupervisor(kernel, gateway).run_once(
                    owner, wr["work_id"], wr["branch_id"]
                )
                if step["status"] in {"NEXT_PLAN_BLOCKED", "NEXT_PLAN_ACCEPTED"}:
                    break
            assert step["status"] == "NEXT_PLAN_ACCEPTED", step
            cursor = rolling_status(kernel, owner, wr["work_id"], wr["branch_id"])
            assert cursor["chapter_count"] == 3 and cursor["window_count"] == 2
            assert cursor["frozen_chapters"] == 2
            board = pipeline_status(
                kernel, owner, wr["work_id"], wr["branch_id"], include_outputs=True
            )
            facts = [key for key, node in board["nodes"].items() if node["stage"] == "facts"]
            assert len(facts) == 2 and all(board["outputs"][key]["acceptance_ref"] for key in facts)
            assert step["plan_task"] != step["review_task"]
