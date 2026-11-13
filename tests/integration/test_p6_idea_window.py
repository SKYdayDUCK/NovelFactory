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
            if case == "legacy_input":
                prepare = RuntimeTaskService.prepare
                def old_input(service, *args, **kwargs):
                    selection = json.loads(kwargs["assignment"])
                    selection.pop("window_constraints_version")
                    kwargs["assignment"] = json.dumps(selection, ensure_ascii=False)
                    return prepare(service, *args, **kwargs)
                historical.setattr(RuntimeTaskService, "prepare", old_input)
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
        schema = json.loads(calls[0]["messages"][0]["content"].split("Payload JSON Schema: ", 1)[1])
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
        repaired_schema = json.loads(calls[1]["messages"][0]["content"].split("Payload JSON Schema: ", 1)[1])
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
