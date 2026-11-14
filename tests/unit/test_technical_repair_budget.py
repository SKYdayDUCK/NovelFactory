"""Technical recovery stays finite when diagnostics change or fixed methods repeat."""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from novel_factory.application import board_schema_repair as repair
from novel_factory.application.idea_repair import UnparsedDesign
from novel_factory.application.quality_memory import PAUSED
from novel_factory.application.technical_repair_budget import (
    EXHAUSTED,
    MAX_TECHNICAL_REPAIRS,
    repair_count,
    technical_repair_budget,
)
from novel_factory.domain.errors import DomainError


def synthetic_recovery(monkeypatch, diagnostic):
    clock = datetime.now(UTC)
    node = {"stage": "epistemic", "state": "ENROLLED", "task_id": "initial"}
    board = {"world_ref": {"work_id": "work"}, "nodes": {"knowledge:scene": node}}
    calls = []
    def returned(*args, **kwargs):
        calls.append(args[3]["task_id"])
        return UnparsedDesign("synthetic", diagnostic(len(calls))), {"attempt_id": "attempt"}, {}
    def one(name, where, **kwargs):
        if name == "supervisor_jobs":
            return {"config": {"production_node": "knowledge:scene", "grant_epoch": 0,
                               "dispatch": {"input_ref": {}}}}
        if name == "grants":
            return {"active": True, "epoch": 0}
        if name == "tasks":
            return {"task_id": node["task_id"], "generation": 1}
        return None
    repo = SimpleNamespace(one=one, now=lambda: clock)
    kernel = SimpleNamespace(artifact=lambda *a, **k: {"payload": {"model_config_sha256": "model",
        "runtime_sha256": "runtime", "output_contract": {"name": "AgentReviewOutput", "version": "agent"}}})
    monkeypatch.setattr(repair, "returned_design", returned)
    monkeypatch.setattr(repair, "check_task_dependencies", lambda *a: None)
    monkeypatch.setattr(repair, "reflect_repair", lambda *a: {**a[-2], "reflection_ref": {}})
    def advance(index):
        node.update(state="ENROLLED", task_id=str(index))
        task = {"task_id": str(index), "generation": 1, "state": "FAILED", "grant_id": "grant",
                "deadline": clock + timedelta(hours=1)}
        repair.recover_board_node(kernel, repo, None, None, board, "knowledge:scene", task)
    return node, board, kernel, repo, calls, advance


def test_changing_diagnostics_cannot_reset_total_technical_allowance(monkeypatch):
    node, _, _, _, calls, advance = synthetic_recovery(monkeypatch, lambda i: {"keyword": "required", "missing": [str(i)]})
    for index in range(MAX_TECHNICAL_REPAIRS):
        advance(index)
        assert node["state"] == "PENDING"
    advance(MAX_TECHNICAL_REPAIRS)
    assert node["state"] == "STOPPED" and node["reason"] == EXHAUSTED
    assert len(calls) == repair_count(node) == MAX_TECHNICAL_REPAIRS


def test_repeated_fixed_review_format_method_stops_instead_of_looping(monkeypatch):
    diagnostic = {"keyword": "additionalProperties", "location": "envelope", "move_inside_payload": ["summary"]}
    node, board, kernel, repo, calls, advance = synthetic_recovery(monkeypatch, lambda _: diagnostic)
    for index in range(20):
        advance(index)
        if node["state"] == "STOPPED" and node["reason"] == PAUSED:
            assert repair.resume_review_format(kernel, repo, None, board, "knowledge:scene")
        if node["state"] == "STOPPED" and node["reason"] == EXHAUSTED:
            break
    else:
        pytest.fail("Fixed review format method never reached its lifetime cap")
    assert repair_count(node) == MAX_TECHNICAL_REPAIRS
    assert len(calls) <= MAX_TECHNICAL_REPAIRS


def test_fact_reading_cap_precedes_any_evidence_or_provider_access():
    node = {"stage": "facts", "state": "STOPPED", "fact_format_repairs": [{} for _ in range(6)]}
    assert repair.recover_fact_reading(None, None, None, None, {"nodes": {"facts:scene": node}}, "facts:scene")
    assert node["reason"] == EXHAUSTED


def test_legacy_baseline_is_finite_without_rewriting_thirty_repairs():
    node = {"schema_repairs": [{"signature": str(i)} for i in range(30)],
            "technical_repair_allowance": {"repair_baseline": 30}}
    assert technical_repair_budget(node) is None
    for index in range(6):
        node["schema_repairs"].append({"signature": "new" + str(index)})
    assert technical_repair_budget(node) == EXHAUSTED
    assert node["schema_repairs"][:30] == [{"signature": str(i)} for i in range(30)]


@pytest.mark.parametrize("baseline", [-1, 1, True, "0"])
def test_invalid_technical_baseline_cannot_expand_allowance(baseline):
    with pytest.raises(DomainError, match="baseline"):
        technical_repair_budget({"technical_repair_allowance": {"repair_baseline": baseline}})
