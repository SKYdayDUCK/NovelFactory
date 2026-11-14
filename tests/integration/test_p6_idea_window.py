"""Native, restartable intake format correction on isolated PostgreSQL + synthetic HTTP."""
import importlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from test_p5_supervisor import backend

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.prompt_schema import prompt_schema
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.domain.errors import DomainError

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def payload_schema(request):
    # The schema occupies one JSON value. Frozen method instructions may follow it.
    system = request["messages"][0]["content"]
    schema, _ = json.JSONDecoder().raw_decode(system.split("\nPayload JSON Schema: ", 1)[1])
    return schema


@pytest.mark.parametrize("case", ["legacy_input", "new_input", "exhausted", "unknown", "drift"])
def test_window_protocol_keeps_first_attempt_and_corrects_only_the_requested_array(kernel_env, tmp_path, monkeypatch, case):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    cli = importlib.import_module("p6_idea")
    kernel, private = kernel_env["kernel"], tmp_path / "synthetic-project"
    idea = {"idea": "结构校验测试", "genre": "测试", "length": {"preset": "short", "unit": "chapters", "min": 2, "max": 2}}
    allowance = {"adapter_id": "fixture", "currency": "USD", "reserve_amount": "0.1", "total_amount": "2",
                 "token_reservation": 100000, "total_tokens": 800000}
    design = {"title": "输入", "protagonist": "甲", "place": "乙", "storyline": "流程测试。",
              "world_rules": ["测试规则。"], "chapter_goals": ["一", "一", "二"]}
    valid = {**design, "chapter_goals": ["一", "二"]}
    findings = [{"path": "/chapter_goals", "keyword": "maxItems", "limit": 2, "actual": 3},
                {"path": "/chapter_goals", "keyword": "uniqueItems", "duplicates": [[0, 1]]}]
    with backend() as (adapter, calls, server):
        generate = adapter.generate
        if case != "unknown":
            adapter.generate = lambda request: replace(generate(request), actual_amount=Decimal(0), currency="USD")
        else:
            server.lose_response = True

        def response(packet):
            if packet["context"]["role_id"] == "reviewer":
                return {"summary": "合成审核", "counterinterpretations": [], "evidence_refs": [], "issues": []}
            assignment = json.loads(packet["assignment"])
            if "validation_errors" in assignment:
                assert assignment["validation_errors"] == findings and assignment["candidate"] == design
                assert not {"extent_instruction", "short_arc", "issues"}.intersection(assignment)
                return design if case == "exhausted" else {**valid, **({"place": "丙"} if case == "drift" else {})}
            return design
        server.packet_candidate = response

        def run(mode):
            return cli.run_intake(mode, kernel, AgentGateway(kernel, {"fixture": adapter}), private, idea, allowance)

        with monkeypatch.context() as historical:
            prepare = RuntimeTaskService.prepare
            def bounded_input(service, *args, **kwargs):
                selection = json.loads(kwargs["assignment"])
                selection["first_window_limit"] = 2
                if case == "legacy_input":
                    selection.pop("window_constraints_version")
                kwargs["assignment"] = json.dumps(selection, ensure_ascii=False)
                return prepare(service, *args, **kwargs)
            historical.setattr(RuntimeTaskService, "prepare", bounded_input)
            enrolled = run("enroll")
        run("advance")
        if case == "unknown":
            for _ in range(2):
                try:
                    run("advance")
                except DomainError as error:
                    assert error.code in {"CALL_RECONCILIATION_REQUIRED", "TASK_NOT_ACCEPTABLE"}
            assert len(calls) == 1
            with kernel.db.uow(write=False) as repo:
                assert not repo.many("receipts", {"work_id": enrolled["world_ref"]["work_id"], "command_type": "P6_IDEA_REVISE"})
            return
        schema = payload_schema(calls[0])
        validator = Draft202012Validator(schema)
        assert validator.is_valid(valid) and validator.is_valid(design) == (case == "legacy_input")
        assert validator.is_valid({**valid, "chapter_goals": ["一", "一"]}) == (case == "legacy_input")
        if case == "legacy_input":
            assert schema == json.loads(prompt_schema("FirstWorldDesign", "production_profile"))
        proposal = run("advance")
        assert proposal["revision_purpose"] == "WINDOW_SCHEMA_CORRECTION" and proposal["validation_errors"] == findings
        assert len(calls) == 1 and run("status")["task_id"] == proposal["task_id"] != enrolled["task_id"]
        with kernel.db.uow(write=False) as repo:
            original = repo.one("tasks", {"task_id": enrolled["task_id"]})
            attempt = repo.one("call_attempts", {"task_id": original["task_id"]})
            assert original["state"] == "RESULT_STORED" and attempt["attempt_id"] == proposal["source_attempt_id"]
            assert kernel.artifact(repo, proposal["candidate_ref"], original["work_id"])["payload"] == design
        run("advance")
        repaired_schema = payload_schema(calls[1])
        assert not Draft202012Validator(repaired_schema).is_valid(design)
        if case == "exhausted":
            stopped = run("advance")
            assert stopped["status"] == "DESIGN_SCHEMA_REWORK_REQUIRED" and stopped["validation_errors"] == findings
            assert run("advance") == stopped and len(calls) == 2
        elif case == "drift":
            with pytest.raises(DomainError) as changed:
                run("advance")
            assert changed.value.code == "INVALID_MODEL_OUTPUT" and len(calls) == 2
        else:
            for _ in range(5):
                final = run("advance")
                if final["status"] == "INITIAL_PLAN_MATERIALIZED":
                    break
            assert final["status"] == "INITIAL_PLAN_MATERIALIZED"
            assert final == run("advance") and len(calls) == 3
            with kernel.db.uow(write=False) as repo:
                assert kernel.artifact(repo, final["design_ref"], final["world_ref"]["work_id"])["payload"] == valid
                assert len(repo.many("call_attempts", {"task_id": original["task_id"]})) == 1


@pytest.mark.parametrize("historical_minimum", [False, True])
def test_quality_han_estimate_is_advisory_and_replays_frozen_window(kernel_env, tmp_path, monkeypatch, historical_minimum):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    cli = importlib.import_module("p6_idea")
    kernel, project = kernel_env["kernel"], tmp_path / "quality-intake"
    idea = {"idea": "结构校验测试", "genre": "测试",
            "length": {"preset": "medium", "unit": "han_characters", "min": 30000, "max": 60000}}
    allowance = {"adapter_id": "fixture", "currency": "USD", "reserve_amount": "0.1", "total_amount": "2",
                 "token_reservation": 100000, "total_tokens": 800000, "production_mode": "quality"}
    small = {"title": "输入", "protagonist": "甲", "place": "乙", "storyline": "流程测试。",
             "world_rules": ["测试规则。"], "chapter_goals": ["1"]}
    valid = {**small, "chapter_goals": list("123456")}
    with backend() as (adapter, calls, server):
        generate = adapter.generate
        adapter.generate = lambda request: replace(generate(request), actual_amount=Decimal(0), currency="USD")
        gateway = AgentGateway(kernel, {"fixture": adapter})

        def run(mode):
            return cli.run_intake(mode, kernel, gateway, project, idea, allowance)

        def response(packet):
            if packet["context"]["role_id"] == "reviewer":
                return {"summary": "合成审核", "counterinterpretations": [], "evidence_refs": [], "issues": []}
            assignment = json.loads(packet["assignment"])
            if "validation_errors" in assignment:
                assert assignment["first_window_minimum"] == 6 and assignment["first_window_limit"] == 8
                assert assignment["candidate"] == small and assignment["repair_strategy"]
                return valid
            return small

        server.packet_candidate = response
        with monkeypatch.context() as historical:
            if historical_minimum:
                original = RuntimeTaskService.prepare
                def old_input(service, *args, **kwargs):
                    assignment = json.loads(kwargs["assignment"])
                    assignment.update(window_constraints_version=2, first_window_minimum=6,
                                      planning_scope="Historical frozen whole-book window")
                    kwargs["assignment"] = json.dumps(assignment)
                    return original(service, *args, **kwargs)
                historical.setattr(RuntimeTaskService, "prepare", old_input)
            first = run("enroll")
        run("advance")
        schema = Draft202012Validator(payload_schema(calls[0]))
        assert schema.is_valid(small) is not historical_minimum and schema.is_valid(valid)
        if historical_minimum:
            repaired = run("advance")
            assert repaired["validation_errors"] == [{"path": "/chapter_goals", "keyword": "minItems", "limit": 6, "actual": 1}]
            assert repaired["revision_purpose"] == "WINDOW_SCHEMA_CORRECTION" and repaired["source_attempt_id"]
            with kernel.db.uow(write=False) as repo:
                wr = first["world_ref"]
                reflection = kernel.artifact(repo, repaired["repair_plan"]["reflection_ref"], wr["work_id"],
                                             kind="P6_REFLECTION_RECORD")["payload"]
                assert reflection["attempt_id"] == repaired["source_attempt_id"]
                assert kernel.artifact(repo, repaired["candidate_ref"], wr["work_id"])["payload"] == small
            run("advance")
            assert not Draft202012Validator(payload_schema(calls[1])).is_valid(small)
        for _ in range(5):
            final = run("advance")
            if final["status"] == "INITIAL_PLAN_MATERIALIZED":
                break
        assert final["status"] == "INITIAL_PLAN_MATERIALIZED" and len(calls) == (3 if historical_minimum else 2)
        with kernel.db.uow(write=False) as repo:
            adopted = kernel.artifact(repo, final["design_ref"], final["world_ref"]["work_id"])["payload"]
            assert adopted == (valid if historical_minimum else small)
