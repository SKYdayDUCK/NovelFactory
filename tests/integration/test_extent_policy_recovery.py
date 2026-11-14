"""Length is advisory; real narrative proof and independent reviews remain required."""
from copy import deepcopy

import pytest
from test_production_strategy import current_board, drive, synthetic_production

from novel_factory.application.extent_policy_recovery import recover_extent_policy
from novel_factory.application.production_pipeline import _key, _load, _save
from novel_factory.application.quality_memory import plan_repair, reflect_repair
from novel_factory.application.volume_successor import _selected
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def _legacy_hold(kernel, monkeypatch):
    gateway, owner, wr, calls, diagnoses = synthetic_production(kernel, monkeypatch, lengths=[20])
    board, _ = drive(kernel, gateway, owner, wr, until=lambda board: any(
        node["stage"] == "draft" and node["state"] == "DRAFT_STORED" for node in board["nodes"].values()))
    target = next(key for key, node in board["nodes"].items() if node["stage"] == "draft")
    with kernel.objects.lock(), kernel.db.uow() as repo:
        board = _load(kernel, repo, wr["work_id"], wr["branch_id"])
        writer = board["nodes"][target]
        task = repo.one("tasks", {"task_id": writer["task_id"]})
        call = repo.one("call_attempts", {"task_id": task["task_id"], "generation": task["generation"], "call_key": "supervised-candidate"})
        plan = reflect_repair(kernel, repo, owner, wr, task["task_id"], call["attempt_id"], writer["output_ref"],
                              plan_repair("extent", writer["han_characters"], []), "SCENE_HAN_EXTENT")
        writer.update(state="STOPPED", reason="REPAIR_BUDGET_EXHAUSTED", revision_count=80,
                      repair_allowance={"revision_baseline": 75, "reassessment_baseline": 0},
                      extent_repairs=[{**plan, "actual": 20, "distance": 10}],
                      reassessments=[{"legacy_round": 1}, {"legacy_round": 2}])
        board_ref = _save(kernel, repo, owner, board)
        before = {"writer": deepcopy(writer), "task": task, "call": call,
                  "result": repo.one("task_results", {"result_id": task["current_result_id"]}),
                  "budget": repo.one("budgets", {"budget_id": call["budget_id"]}),
                  "head": kernel.head(repo, wr["work_id"], wr["branch_id"])}
    args = {"node_id": target, "expected_board_sha256": board_ref["sha256"], "source_revision": "a" * 40,
            "idempotency_key": "adopt-author-length-policy"}
    return gateway, owner, wr, calls, diagnoses, before, args


def test_outside_historical_extent_runs_all_independent_readers_to_freeze(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, diagnoses = synthetic_production(kernel, monkeypatch, lengths=[20])
    board, cursor = drive(kernel, gateway, owner, wr)
    writer = next(node for node in board["nodes"].values() if node["stage"] == "draft")
    assert cursor["state"] == "FROZEN" and not diagnoses and len(calls) == 4
    assert writer["extent_observation"]["actual_han"] == 20 and writer["han_characters"]["max"] == 10
    assert writer["extent_observation"]["policy"] == "ADVISORY" and not writer.get("extent_repairs")
    assert writer.get("revision_count", 0) == 0
    assert {node["state"] for node in board["nodes"].values() if node["stage"] in {"facts", "epistemic", "review"}} == {
        "FACTS_ACCEPTED", "KNOWLEDGE_CLEAR", "ADVISORY_REVIEW"}


def test_advisory_recovery_preserves_paid_history_then_reads_same_draft(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, diagnoses, before, args = _legacy_hold(kernel, monkeypatch)
    preview = recover_extent_policy(kernel, gateway, owner, wr, **args)
    assert preview["status"] == "ADVISORY_EXTENT_PREVIEW" and len(calls) == 1
    assert current_board(kernel, wr)["nodes"][args["node_id"]] == before["writer"]
    adopted = recover_extent_policy(kernel, gateway, owner, wr, **args, apply=True)
    assert adopted["status"] == "ADVISORY_EXTENT_ADOPTED" and adopted["reviews_accepted"] == 0
    assert recover_extent_policy(kernel, gateway, owner, wr, **args, apply=True) == adopted
    with kernel.db.uow(write=False) as repo:
        assert repo.one("tasks", {"task_id": before["task"]["task_id"]}) == before["task"]
        assert repo.one("task_results", {"result_id": before["result"]["result_id"]}) == before["result"]
        assert repo.one("call_attempts", {"attempt_id": before["call"]["attempt_id"]}) == before["call"]
        assert repo.one("budgets", {"budget_id": before["call"]["budget_id"]}) == before["budget"]
        assert kernel.head(repo, wr["work_id"], wr["branch_id"]) == before["head"]
        writer = _load(kernel, repo, wr["work_id"], wr["branch_id"])["nodes"][args["node_id"]]
        for key in ("task_id", "output_ref", "revision_count", "repair_allowance", "extent_repairs", "reassessments"):
            assert writer[key] == before["writer"][key]
        assert writer["state"] == "DRAFT_STORED"
    board, cursor = drive(kernel, gateway, owner, wr)
    writer = board["nodes"][args["node_id"]]
    assert cursor["state"] == "FROZEN" and writer["output_ref"] == before["writer"]["output_ref"]
    assert len(calls) == 4 and not diagnoses
    assert len([context for context, _, _ in calls if context["role_id"] == "writer"]) == 1


@pytest.mark.parametrize("fault", ["stale_board", "foreign_failure", "unresolved_inquiry"])
def test_advisory_policy_does_not_override_other_scope_or_review_holds(kernel_env, monkeypatch, fault):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, _diagnoses, before, args = _legacy_hold(kernel, monkeypatch)
    if fault == "stale_board":
        args["expected_board_sha256"] = "0" * 64
    else:
        with kernel.objects.lock(), kernel.db.uow() as repo:
            board = _load(kernel, repo, wr["work_id"], wr["branch_id"])
            writer = board["nodes"][args["node_id"]]
            if fault == "foreign_failure":
                writer["extent_repairs"][-1]["actual"] = 21
            else:
                board["nodes"]["verification:pending-current"] = {"stage": "internal_verification", "state": "NEEDS_WORLD_INPUT",
                    "parents": [args["node_id"]], "internal_case": {"source_draft_ref": writer["output_ref"]}}
            args["expected_board_sha256"] = _save(kernel, repo, owner, board)["sha256"]
    frozen = fingerprint(current_board(kernel, wr))
    with pytest.raises(DomainError):
        recover_extent_policy(kernel, gateway, owner, wr, **args, apply=True)
    assert fingerprint(current_board(kernel, wr)) == frozen and len(calls) == 1
    with kernel.db.uow(write=False) as repo:
        assert repo.one("call_attempts", {"attempt_id": before["call"]["attempt_id"]}) == before["call"]
        assert _selected(repo, wr["work_id"], _key(wr["branch_id"]))["sha256"] == args["expected_board_sha256"] or fault == "stale_board"
