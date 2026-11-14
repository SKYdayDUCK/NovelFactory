"""Use the CLI's native service path with a disclosed fixture upstream on real PostgreSQL."""

import importlib
import json
import sys
from contextlib import ExitStack
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


@pytest.mark.parametrize("outcome", ["approved", "rejected", "unknown_cost", "continue", "quality_continue"])
def test_model_design_delivery_recovery_and_scope(kernel_env, tmp_path, monkeypatch, outcome):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    cli = importlib.import_module("p6_idea")
    kernel = kernel_env["kernel"]
    idea = {
        "idea": "一位无名画师接到委托。",
        "length": {"preset": "short", "unit": "han_characters", "min": 6000, "max": 10000},
        "genre": "现实",
    }
    continuation = outcome in {"continue", "quality_continue"}
    if continuation:
        idea["length"] = {"preset": "medium_long", "unit": "chapters", "min": 3, "max": 3}
    allowance = {
        "adapter_id": "test",
        "reserve_amount": "0.1",
        "total_amount": "5",
        "currency": "USD",
        "total_tokens": 2000000,
        "token_reservation": 100000,
    }
    with ExitStack() as stack:
        adapter, requests, server = stack.enter_context(backend())
        adapters = {"test": adapter}
        if continuation:
            reviewer, review_requests, review_server = stack.enter_context(backend())
            reviewer.model = "fixture-internal-review"
            review_generate = reviewer.generate
            reviewer.generate = lambda request: replace(
                review_generate(request), actual_amount=Decimal(0), currency="USD"
            )
            adapters["internal-review"] = reviewer
        generate = adapter.generate

        def settled_fixture(request):
            return replace(generate(request), actual_amount=Decimal(0), currency="USD")

        if outcome != "unknown_cost":
            monkeypatch.setattr(adapter, "generate", settled_fixture)
        gateway = AgentGateway(
            kernel, adapters, material_reader=production_material_reader(kernel)
        )
        expected = {
            "title": "测试计划",
            "protagonist": "人物甲",
            "place": "地点乙",
            "storyline": "验" * 4000,
            "world_rules": ["公开的定价有效"],
            "chapter_goals": ["选择", "解决"],
        }

        next_reviews = []
        if outcome == "quality_continue":
            allowance["production_mode"] = "quality"

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
                if outcome in {"approved", "continue", "quality_continue"}
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
            if outcome == "quality_continue" and "actual_text" in json.loads(packet["assignment"]):
                next_reviews.append(ref)
                mandatory = len(next_reviews) == 1
                issues = [{"issue_id": str(uuid4()), "category": "CONTINUITY", "severity": "BLOCKER" if mandatory else "SUGGESTION",
                           "subject_ref": ref, "description": "Synthetic continuation review",
                           "evidence_refs": [], "suggested_change": "Check continuity", "disposition": "OPEN",
                           "issue_kind": "CHANGE_REQUEST" if mandatory else "SEMANTIC_CONCERN"}]
            return {
                "summary": "fixture checked design",
                "issues": issues,
                "counterinterpretations": [],
                "evidence_refs": [],
            }

        server.packet_candidate = candidate
        if continuation:
            review_server.packet_candidate = candidate
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
        if continuation:
            # An already materialized pre-policy idea can opt in at its first
            # window, with every subsequent window inheriting that allowance.
            allowance["review_adapter_id"] = "internal-review"
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

        if continuation:
            canon = cli._load_private(private / "supervisor.json")
            owner, wr = kernel.authenticate(canon["token"]), receipt["world_ref"]
            for _ in range(65):
                step = RuntimeSupervisor(kernel, gateway).run_once(
                    owner, wr["work_id"], wr["branch_id"]
                )
                if step["status"] in {"NEXT_PLAN_BLOCKED", "NEXT_PLAN_ACCEPTED"}:
                    break
            assert step["status"] == "NEXT_PLAN_ACCEPTED", step
            if outcome == "quality_continue":
                assert len(next_reviews) == 2 and next_reviews[0] != next_reviews[1]
                with kernel.db.uow(write=False) as repo:
                    row = repo.one("receipts", {"work_id": wr["work_id"], "command_type": "P6_NEXT_PLAN_REVISE"})
                    assert row["payload"]["prior_task"] != step["plan_task"]
                    assert row["payload"]["previous_ref"] == next_reviews[0]

            cursor = rolling_status(kernel, owner, wr["work_id"], wr["branch_id"])
            assert cursor["chapter_count"] == 3 and cursor["window_count"] == 2
            assert cursor["frozen_chapters"] == 2
            board = pipeline_status(
                kernel, owner, wr["work_id"], wr["branch_id"], include_outputs=True
            )
            facts = [key for key, node in board["nodes"].items() if node["stage"] == "facts"]
            assert len(facts) == 2 and all(board["outputs"][key]["acceptance_ref"] for key in facts)
            assert step["plan_task"] != step["review_task"]
            assert {r["model"] for r in review_requests} == {reviewer.model}
            assert {r["model"] for r in requests} == {adapter.model}
            tasks_by_route = {}
            with kernel.db.uow(write=False) as repo:
                for route, packets in (("test", requests[2:]), ("internal-review", review_requests)):
                    tasks_by_route[route] = set()
                    for packet in packets:
                        ctx = json.loads(packet["messages"][1]["content"])["context"]
                        job = repo.one("supervisor_jobs", {"task_id": ctx["task_id"]})
                        data = kernel.artifact(repo, job["config"]["dispatch"]["input_ref"], work)["payload"]
                        tasks_by_route[route].add((data["role_id"], data["task_kind"]))
                        assert data["adapter_id"] == data["routing_decision"]["profile_id"] == route
                        assert data["model_config_sha256"] == data["routing_decision"]["configuration_sha256"]
            assert tasks_by_route == {
                "test": {("writer", "scene_draft")},
                "internal-review": {("reviewer", "epistemic"), ("reviewer", "literary"),
                                    ("reviewer", "fact"), ("evidence_maintainer", "assertion_extract"),
                                    ("volume_director", "window_plan")},
            }


@pytest.mark.parametrize("first_result", ["approved", "missing_opening", "review_rejected", "historical_duplicate", "historical_null_note",
                                          "repaired_then_question", "quality_revision_schema", "ending_plan", "unresolved_ending", "historical_ending", "editor_fix"])
def test_cli_persists_two_model_roles_across_intake_restarts(kernel_env, tmp_path, monkeypatch, capsys, first_result):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    cli = importlib.import_module("p6_idea")
    idea_path = tmp_path / "idea.json"
    idea_path.write_text(json.dumps({"idea": "画师发现另一份落款。", "length": "short", "genre": "现实"}))
    private = tmp_path / "project"
    monkeypatch.setenv("NF_DATABASE_URL", kernel_env["url"])
    monkeypatch.setenv("NF_OBJECT_ROOT", str(kernel_env["store"].root))
    monkeypatch.setenv("NF_RUNTIME_PROFILES", "synthetic-catalog")
    model_routes = {"creative": "flash-fixture", "review": "pro-fixture"}
    flags = ["--adapter", model_routes["creative"], "--review-adapter", model_routes["review"]]

    def call(action, *args, status=0):
        monkeypatch.setattr(sys, "argv", ["p6_idea.py", action, "--private-dir", str(private), *args])
        assert cli.main() == status
        captured = capsys.readouterr()
        assert not captured.err, captured.err.splitlines()[-2:]
        return json.loads(captured.out)

    with backend() as (creative, creative_wire, creative_server), backend() as (review, review_wire, review_server):
        creative_calls, review_calls = [], []
        creative.model, review.model = "fixture-creative", "fixture-internal-review"
        adapters = {model_routes["creative"]: creative, model_routes["review"]: review}
        monkeypatch.setattr(cli, "load_runtime_profiles", lambda path, journal: adapters)
        for adapter in adapters.values():
            generate = adapter.generate
            adapter.generate = lambda request, send=generate: replace(send(request), actual_amount=Decimal(0), currency="USD")

        design = {
            "title": "双落款", "protagonist": "画师", "place": "画室", "storyline": "辨别两份相互冲突的委托。",
            "world_rules": ["委托须本人签收。"], "chapter_goals": ["画师核对落款。"],
            "opening": {"counterpart": "送信人", "other_place": "档案室",
                        "protagonist_knowledge": "画师见过原件，怀疑眼前的落款。",
                        "counterpart_knowledge": "送信人保管委托，急于确认签收。",
                        "situation": "两人需要在收件时处理相互冲突的委托。",
                        "environment": {"moving_entity": "刚到的邮车"}},
        }
        def arc(packet):
            complete = first_result != "unresolved_ending" and (first_result not in {"ending_plan", "historical_ending", "editor_fix"} or len(review_calls) > 1)
            assert "short_arc_review" in json.loads(packet["assignment"])
            return {"beginning": "原件出现。", "development": "送信人辨认墨色。", "turning_point": "两人对证。",
                    "ending": "双方撤回误认的落款。" if complete else "没有给出最后结果。",
                    "decision": "COMPLETE" if complete else "ENDING_PLAN_REQUIRED"}
        review_generate = review.generate
        if first_result in {"ending_plan", "unresolved_ending", "historical_ending", "editor_fix"}:
            def review_with_questions(request):
                response = review_generate(request)
                envelope = json.loads(response.text)
                if envelope["role_id"] == "reviewer" and len(review_calls) == 1 and envelope["payload"]["short_arc"]["decision"] == "ENDING_PLAN_REQUIRED":
                    envelope["unresolved_questions"] = ["画师最后如何处理委托？"]
                return replace(response, text=json.dumps(envelope, ensure_ascii=False))
            review.generate = review_with_questions
        def complete_review(packet):
            content = json.loads(packet["assignment"])
            result = {"short_arc": arc(packet), "summary": "已核查开场人物、设定和计划。", "issues": [],
                      "counterinterpretations": [], "evidence_refs": []}
            if "ending_plan_source" in content:
                source = content["ending_plan_source"]
                assert source["ordered_stages"] == ["第一项", "第二项", "最后签字"]
                fixed = first_result == "editor_fix" and len(review_calls) == 3
                assert content["candidate"]["chapter_goals"] == (["两人对证后签字。"] if fixed else ["第一项\n第二项\n最后签字"])
                if fixed:
                    result["issue_resolutions"] = [{"issue_id": i["issue_id"], "decision": "RESOLVED",
                                                    "reason": "章末双方已经对证。"} for i in content["prior_blockers"]]
                assert content["candidate"]["opening"] == design["opening"]
                result["ending_plan_review"] = {"candidate_ref": content["candidate_ref"],
                    "opening_and_chapters_confirmed": first_result != "unresolved_ending",
                    "answers": [{"question": q, "answer": "送信人接受签字。"} for q in source["unresolved_questions"]]}
                if first_result == "editor_fix" and len(review_calls) == 2:
                    result["ending_plan_review"]["opening_and_chapters_confirmed"] = False
                    result["issues"] = [{"issue_id": str(uuid4()), "category": "CONTINUITY", "severity": "BLOCKER",
                                         "issue_kind": "HARD_CONFLICT", "disposition": "OPEN", "evidence_refs": [],
                                         "subject_ref": content["candidate_ref"], "description": "签字顺序冲突。",
                                         "suggested_change": "对证后签收。"}]
            return result
        review_server.packet_candidate = complete_review
        if first_result in {"ending_plan", "historical_ending", "editor_fix"}:
            original = review.generate
            def ending_with_question(request):
                result = original(request)
                if json.loads(result.text)["role_id"] == "world_designer" and len(creative_calls) > 1:
                    envelope = json.loads(result.text)
                    envelope["unresolved_questions"] = ["签字后送信人怎样行动？"]
                    return replace(result, text=json.dumps(envelope, ensure_ascii=False))
                return result
            review.generate = ending_with_question
        def design_reply(packet):
            if len(creative_calls) == 1 and first_result in {"missing_opening", "repaired_then_question"}:
                return {k: v for k, v in design.items() if k != "opening"}
            if first_result == "historical_duplicate":
                return {**design, "unresolved_questions": []}
            if first_result == "historical_null_note":
                return {**design, "chapter_goals_note": None}
            if len(creative_calls) > 1:
                handed = json.loads(packet["assignment"])
                assert handed["candidate"]["title"] == design["title"]
                if first_result == "quality_revision_schema":
                    assert handed["issues"][0]["suggested_change"] == "写明两次委托的先后"
                    if len(creative_calls) == 2:
                        return {**design, "unexpected_field": True}
                    assert handed["validation_errors"][0]["keyword"] == "additionalProperties"
                    return design
                if first_result == "editor_fix" and len(creative_calls) == 3:
                    return {"opening_changes": [], "chapter_changes": [{"chapter": 1, "replacement": "两人对证后签字。",
                             "issue_ids": handed["editor_decision"]["issue_ids"]}]}
                if first_result == "review_rejected" or len(creative_calls) == 3:
                    assert handed["issues"][0]["suggested_change"] == "写明两次委托的先后"
                elif first_result in {"ending_plan", "unresolved_ending", "historical_ending", "editor_fix"}:
                    assert handed["short_arc"]["decision"] == "ENDING_PLAN_REQUIRED"
                    assert handed["unresolved_questions"] == ["画师最后如何处理委托？"]
                    stages = ["第一项", "第二项", "最后签字"]
                    if first_result == "historical_ending":
                        return {**{k: v for k, v in design.items() if k != "opening"}, "chapter_goals": stages}
                    return {"storyline": "在画室签收。", "chapter_goals": stages}
                else:
                    assert "opening" in handed["task"] and "opening" not in handed["candidate"]
            return design
        creative_server.packet_candidate = design_reply
        if first_result in {"review_rejected", "repaired_then_question", "quality_revision_schema"}:
            def review_reply(packet):
                ref = json.loads(packet["assignment"])["candidate_ref"]
                issue = {"issue_id": str(uuid4()), "category": "CONTINUITY", "severity": "BLOCKER",
                         "subject_ref": ref, "description": "两次委托顺序不清楚", "evidence_refs": [],
                         "suggested_change": "写明两次委托的先后", "disposition": "OPEN", "issue_kind": "HARD_CONFLICT"}
                if first_result == "repaired_then_question":
                    issue.update(severity="QUESTION", issue_kind="SEMANTIC_CONCERN")
                return {"short_arc": arc(packet), "summary": "独立核查设定", "counterinterpretations": [], "evidence_refs": [],
                        "issues": [issue] if len(review_calls) == 1 or first_result == "repaired_then_question" else []}
            review_server.packet_candidate = review_reply
        # Planning and design use the planning/review route. Partition fixture
        # observations by task role, independently of the HTTP connection.
        review_reply_callback = review_server.packet_candidate
        def routed_candidate(packet):
            if packet["context"]["role_id"] == "world_designer":
                creative_calls.append({"model": review.model})
                return design_reply(packet)
            review_calls.append({"model": review.model})
            return review_reply_callback(packet)
        review_server.packet_candidate = routed_candidate
        creative_server.packet_candidate = lambda _: pytest.fail("Planning reached the writing provider")
        preview = call("preflight", "--idea", str(idea_path), *flags)
        assert preview["model_routes"] == model_routes and preview["model_requests"] == 0
        review.model = creative.model
        assert call("preflight", "--idea", str(idea_path), *flags, status=2)["route_status"] == "INVALID_MODEL_CONFIG"
        review.model = "fixture-internal-review"
        if first_result == "quality_revision_schema":
            flags.extend(["--production-mode", "quality"])
        enrolled = call("enroll", "--idea", str(idea_path), *flags, "--reserve-amount", "0.1",
                        "--total-amount", "2", "--reserve-tokens", "100000", "--total-tokens", "600000")
        assert enrolled["model_routes"] == model_routes and not creative_calls and not review_calls
        assert call("advance", "--adapter", "pro-fixture", status=2)["error"] == "IDEMPOTENCY_MISMATCH"
        if first_result in {"historical_duplicate", "historical_null_note"}:
            from novel_factory.application import agent_gateway
            from novel_factory.contracts.registry import validate_contract
            # First call uses the historical strict parser to produce a real terminal REJECTED receipt.
            with monkeypatch.context() as old:
                old.setattr(agent_gateway, "validated_payload",
                            lambda envelope, name, version: validate_contract(name, envelope["payload"], version))
                for _ in range(4):
                    call("advance")
                    if creative_calls:
                        break
        advance_states = []
        for _ in range(22):
            if first_result == "historical_ending" and len(review_calls) == 1 and len(creative_calls) == 1:
                # Recreate the earlier contract frozen into the exceptional real ending-plan task.
                from novel_factory.application.runtime_tasks import RuntimeTaskService
                prepare = RuntimeTaskService.prepare
                def previous_contract(service, *args, _prepare=prepare, **kwargs):
                    if kwargs["role_id"] == "world_designer":
                        service.gateway.output_contracts[("world_designer", "initial_world")] = ("EnvironmentalOpeningWorldDesign", "production_profile")
                    return _prepare(service, *args, **kwargs)
                with monkeypatch.context() as old:
                    old.setattr(RuntimeTaskService, "prepare", previous_contract)
                    result = call("advance")
            else:
                result = call("advance")
            advance_states.append({key: result[key] for key in ("status", "reason", "error", "cost_status") if key in result})
            if first_result == "editor_fix" and result.get("reason") == "ENDING_PLAN_BLOCKERS":
                attempt = result["review_attempt"]
                assert call("advance") == result and len(creative_calls) == len(review_calls) == 2
                assert call("authorize-ending-fix", "--review-attempt", str(uuid4()), status=2)["error"] == "REVIEW_REQUIRED"
                decision = call("authorize-ending-fix", "--review-attempt", attempt)
                assert len(decision["issue_ids"]) == 1 and decision == call("authorize-ending-fix", "--review-attempt", attempt)
                assert len(creative_calls) == len(review_calls) == 2
                continue
            if result.get("status") in {"INITIAL_PLAN_MATERIALIZED", "DESIGN_REWORK_REQUIRED"}:
                break
        if first_result == "unresolved_ending":
            assert result["status"] == "DESIGN_REWORK_REQUIRED" and result["reason"] == "SHORT_ENDING_REQUIRED"
            assert result == call("advance") and len(creative_calls) == len(review_calls) == 2
            return
        assert result["status"] == "INITIAL_PLAN_MATERIALIZED", json.dumps(advance_states[:3])
        assert result["model"] == review.model and result["design_review"]["model"] == review.model
        assert result["model_routes"] == model_routes
        assert call("advance") == result
        creative_count = {"approved": 1, "historical_duplicate": 1, "historical_null_note": 1, "repaired_then_question": 3, "quality_revision_schema": 3, "editor_fix": 3}.get(first_result, 2)
        assert not creative_wire and len(review_wire) == len(creative_calls) + len(review_calls)
        assert [r["model"] for r in creative_calls] == [review.model] * creative_count
        assert [r["model"] for r in review_calls] == [review.model] * (3 if first_result == "editor_fix" else 2 if first_result in {"review_rejected", "repaired_then_question", "quality_revision_schema", "ending_plan", "historical_ending"} else 1)
        if creative_count > 1:
            assert result["original_input_ref"] == enrolled["input_ref"] != result["source_input_ref"]
            assert result["design_revision_round"] == creative_count - 1
        kernel = kernel_env["kernel"]
        with kernel.db.uow(write=False) as repo:
            if first_result in {"historical_duplicate", "historical_null_note"}:
                norm = result["design_normalization"]
                assert norm["output_ref"] == result["result_ref"]
                task = repo.one("tasks", {"task_id": result["task_id"]})
                assert task["state"] == "FAILED"
                audit = kernel.artifact(repo, norm["audit_ref"], task["work_id"])["payload"]
                assert audit["outcome"] == "REJECTED" and audit["output_ref"] is None
                raw = kernel.artifact(repo, norm["response_ref"], task["work_id"])["payload"]
                original = json.loads(raw["text"])["payload"]
                if first_result == "historical_null_note":
                    assert norm["removed_fields"] == ["chapter_goals_note"] and original["chapter_goals_note"] is None
                else:
                    assert original["unresolved_questions"] == []
                assert kernel.artifact(repo, norm["output_ref"], task["work_id"])["payload"] == design
            if first_result in {"historical_ending", "ending_plan"}:
                derived = result["design_ending_plan"]
                assert derived["output_ref"] == result["result_ref"]
                assert derived["source"]["unresolved_questions"] == ["签字后送信人怎样行动？"]
                task = repo.one("tasks", {"task_id": result["task_id"]})
                assert task["state"] == ("FAILED" if first_result == "historical_ending" else "ACCEPTED")
            if first_result == "repaired_then_question":
                assert result["design_review"]["advisories"][0]["severity"] == "QUESTION"
            policies = [kernel.artifact(repo, item["routing_policy_ref"], item["world_ref"]["work_id"],
                                        kind="RUNTIME_ROUTING_POLICY")["payload"]
                        for item in (enrolled, result)]
        assert policies[0]["rules"] == policies[1]["rules"]
        assert policies[0]["work_id"] != policies[1]["work_id"]
