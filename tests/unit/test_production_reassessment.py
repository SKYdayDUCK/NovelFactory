"""Pure diagnosis-policy checks; no model, database or generated story text."""
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from novel_factory.application.production_reassessment import (
    BLOCKED,
    EXHAUSTED,
    MAX_REASSESSMENTS,
    MAX_REVISIONS,
    READY,
    assignment_request_sha256,
    reassessment_assignment,
    reassessment_request,
    record_reassessment,
    repair_budget,
)
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError


def uid(label):
    return str(uuid5(NAMESPACE_URL, label))


def ref(label):
    return {"artifact_id": uid(label), "artifact_revision_id": uid(label + ":revision"),
            "sha256": fingerprint(label)}


def setup_case(*, extent=False, round=1):
    wr = {"work_id": uid("work"), "branch_id": uid("branch"), "world_revision_id": uid("world")}
    writer = {"stage": "draft", "state": "STOPPED" if extent else "DRAFT_STORED",
              "task_id": uid("writer"), "generation": 2, "output_ref": ref("draft"),
              "revision_count": 4, "repair_plans": [{"signature": "old", "method": "Prior approach"}],
              "reassessments": [{"method": "First diagnosis method"}] if round == 2 else []}
    board = {"world_ref": wr, "nodes": {"draft:scene": writer}}
    reader = writer if extent else {"stage": "review", "output_ref": ref("review")}
    plan = {"signature": "failure", "reflection_ref": ref("extent-reflection")}
    request = reassessment_request(board, "draft:scene", reader, [{"diagnostic": "unresolved"}], plan)
    writer["reassessments"].append({"round": round})
    task = {"task_id": uid("diagnosis"), "generation": 1, "role_id": "chief_editor",
            "task_kind": "editorial_decision", "work_id": wr["work_id"], "branch_id": wr["branch_id"]}
    evidence = {"task_id": task["task_id"], "generation": 1, "world_ref": wr,
                "attempt_id": uid("attempt"), "output_ref": ref("candidate"),
                "input_ref": ref("agent-input"),
                "model_configuration": {"adapter_kind": "codex-cli"}}
    candidate = {"decision": "Use a distinct evidence-first revision method", "reason": "Prior ordering omitted verification",
                 "deferred_questions": [], "evidence_refs": [
                     {"kind": "ARTIFACT", "artifact_ref": request[key]}
                     for key in ("source_draft_ref", "report_ref")]}
    return board, {"reassessment_request": request}, task, evidence, candidate


class Repo:
    def __init__(self, task, sources, request):
        self.task = task
        self.sources = [*sources, ref("agent-input")]
        self.artifacts = {ref("agent-input")["artifact_revision_id"]: {
            "ref": ref("agent-input"), "kind": "AGENT_INPUT",
            "payload": {"assignment": reassessment_assignment(request)}}}

    def one(self, table, query):
        if table == "task_inputs":
            assert query == {"task_id": self.task["task_id"], "generation": self.task["generation"]}
            return {"spec": {"input_artifacts": self.sources}}
        assert table == "artifacts"
        return self.artifacts.get(query["artifact_revision_id"])


class Kernel:
    def __init__(self):
        self.puts = []
        self.objects = SimpleNamespace(put=self.puts.append)

    def authorize(self, repo, owner, work, capability, branch):
        assert capability == "ADMIN"

    def artifact(self, repo, reference, work, kind=None):
        row = repo.artifacts.get(reference["artifact_revision_id"])
        if kind and (not row or row["ref"] != reference or row["kind"] != kind):
            raise DomainError("OBJECT_CORRUPT", "Immutable result differs")
        return row or {"ref": reference}

    def _register_artifact(self, repo, reference, work, payload, kind, principal):
        repo.artifacts[reference["artifact_revision_id"]] = {
            "ref": reference, "payload": deepcopy(payload), "kind": kind}


def deliver(case, *, kernel=None, repo=None):
    board, node, task, evidence, candidate = case
    request = node["reassessment_request"]
    kernel = kernel or Kernel()
    repo = repo or Repo(task, [request["source_draft_ref"], request["report_ref"]], request)
    result = record_reassessment(kernel, repo, SimpleNamespace(principal_id=uid("owner")),
                                 board, node, task, evidence, candidate)
    return result, kernel, repo


def test_request_freezes_extent_evidence_and_method_history():
    board, node, *_ = setup_case(extent=True)
    request = node["reassessment_request"]
    assert request["review_stage"] == "extent"
    assert request["report_ref"] == ref("extent-reflection")
    assert request["source_draft_ref"] == ref("draft")
    assert request["writer_generation"] == 2 and request["round"] == 1
    board["nodes"]["draft:scene"]["repair_plans"][0]["method"] = "Later mutation"
    assert request["history"]["repair_plans"][0]["method"] == "Prior approach"
    assert "AgentDecisionOutput" in reassessment_assignment(request)


@pytest.mark.parametrize("escaped", [False, True])
def test_assignment_bounds_long_history_without_copying_or_mutating_hard_findings(escaped):
    _, node, *_ = setup_case(extent=True)
    request = node["reassessment_request"]
    method = "\x00" * 999 if escaped else "旧方法" * 10000
    request["prior_methods"] = [method + str(index) for index in range(75)] + ["Short latest method"] * 3
    rows = [{"method": method + str(index), "decision": "CHANGED_STRATEGY", "signature": fingerprint(index),
             "actual": index, "distance": 100 - index, "private_diagnostic": "must stay off assignment" * 1000}
            for index in range(75)]
    request["history"] = {"repair_plans": rows[:41], "extent_repairs": rows[41:]}
    request["history_fingerprint"] = fingerprint(request["history"])
    request["findings"] = [{"description": "Full hard issue must remain in scoped material" * 1000}] * 20
    original = deepcopy(request)
    assignment = reassessment_assignment(request)
    compact = json.loads(assignment.rsplit("\n", 1)[1])
    assert len(assignment) <= 32000 and "findings" not in compact
    assert compact["findings_count"] == 20 and compact["findings_sha256"] == fingerprint(request["findings"])
    assert compact["request_sha256"] == fingerprint(request)
    assert len(compact["history"]) <= 6 and len(compact["prior_methods"]) <= 6
    assert set().union(*(set(row) for row in compact["history"])) <= {
        "family", "method", "decision", "signature", "actual", "distance"}
    assert "private_diagnostic" not in assignment and request == original


@pytest.mark.parametrize("characters,expected", [(1000, READY), (1001, BLOCKED)])
def test_new_method_fits_verified_experience_contract(characters, expected):
    case = setup_case()
    case[-1]["decision"] = "步" * characters
    (_, status), _, _ = deliver(case)
    assert status == expected


def test_repeat_detection_uses_complete_prior_methods_even_when_assignment_omits_old_one():
    case = setup_case()
    request = case[1]["reassessment_request"]
    request["prior_methods"] = ["Old omitted method", *[f"Recent method {index}" for index in range(6)]]
    compact = json.loads(reassessment_assignment(request).rsplit("\n", 1)[1])
    assert "Old omitted method" not in compact["prior_methods"]
    case[-1]["decision"] = "Old omitted method"
    (reference, status), _, repo = deliver(case)
    assert status == BLOCKED
    assert repo.artifacts[reference["artifact_revision_id"]]["payload"]["reason"] == "REASSESSMENT_REPEATED_METHOD"


def test_changed_findings_cannot_be_adopted_against_an_old_frozen_assignment():
    case = setup_case()
    request = case[1]["reassessment_request"]
    repo = Repo(case[2], [request["source_draft_ref"], request["report_ref"]], request)
    request["findings"].append({"diagnostic": "Different hard issue after enrollment"})
    with pytest.raises(DomainError, match="frozen assignment"):
        deliver(case, repo=repo)


def test_appended_format_repair_guidance_retains_frozen_request_binding():
    case = setup_case()
    request = case[1]["reassessment_request"]
    repo = Repo(case[2], [request["source_draft_ref"], request["report_ref"]], request)
    frozen = repo.artifacts[ref("agent-input")["artifact_revision_id"]]["payload"]
    frozen["assignment"] += '\n上一次任务的结构化输出诊断与修复方法：{"diagnostic":"required"}\n{"method":"repair JSON"}'
    assert assignment_request_sha256(frozen["assignment"]) == fingerprint(request)
    (_, status), _, _ = deliver(case, repo=repo)
    assert status == READY


@pytest.mark.parametrize("extra", ["f" * 64, "invalid", 123])
def test_conflicting_or_invalid_frozen_request_digest_is_rejected(extra):
    _, node, *_ = setup_case()
    assignment = reassessment_assignment(node["reassessment_request"])
    assignment += "\n" + json.dumps({"request_sha256": extra})
    with pytest.raises(DomainError, match="digest"):
        assignment_request_sha256(assignment)


def test_changing_failure_signatures_does_not_reset_global_allowance():
    writer = {"revision_count": MAX_REVISIONS, "repair_plans": [{"signature": str(i)} for i in range(12)]}
    assert repair_budget(writer) == EXHAUSTED
    assert repair_budget(writer, phase="revision") == EXHAUSTED
    writer = {"reassessments": [{"signature": str(i)} for i in range(MAX_REASSESSMENTS)]}
    assert repair_budget(writer) == EXHAUSTED
    assert repair_budget(writer, phase="revision") is None


def test_explicit_new_allowance_preserves_historical_repairs_and_bounds_new_work():
    writer = {"revision_count": 75, "reassessments": [{"method": str(i)} for i in range(5)],
              "repair_allowance": {"revision_baseline": 75, "reassessment_baseline": 5}}
    assert repair_budget(writer) is None
    writer["revision_count"] = 86
    assert repair_budget(writer) is None
    writer["revision_count"] = 87
    assert repair_budget(writer, phase="revision") == EXHAUSTED
    assert writer["repair_allowance"]["revision_baseline"] == 75
    writer["revision_count"] = 75
    writer["reassessments"].extend([{}, {}])
    assert repair_budget(writer) == EXHAUSTED
    assert repair_budget(writer, phase="revision") is None


@pytest.mark.parametrize("baseline", [-1, 76, True, "75"])
def test_invalid_baseline_cannot_bypass_budget(baseline):
    with pytest.raises(DomainError, match="baseline"):
        repair_budget({"revision_count": 75, "repair_allowance": {"revision_baseline": baseline}})


def test_second_reserved_diagnosis_can_be_consumed_without_approval_or_promotion():
    case = setup_case(round=2)
    (reference, status), kernel, repo = deliver(case)
    payload = repo.artifacts[reference["artifact_revision_id"]]["payload"]
    assert status == READY and payload["reusable"] is False
    assert payload["request"]["round"] == 2
    assert payload["method"] == case[-1]["decision"]
    assert case[0]["nodes"]["draft:scene"]["state"] == "DRAFT_STORED"
    assert len(kernel.puts) == 1


@pytest.mark.parametrize("fault,reason", [
    ("question", "REASSESSMENT_NEEDS_INPUT"),
    ("repeat", "REASSESSMENT_REPEATED_METHOD"),
    ("empty", "REASSESSMENT_INVALID_METHOD"),
    ("long", "REASSESSMENT_INVALID_METHOD"),
])
def test_unresolved_or_unchanged_strategy_is_persisted_as_terminal_blocker(fault, reason):
    case = setup_case()
    candidate = case[-1]
    if fault == "question":
        candidate["deferred_questions"] = ["Missing required evidence"]
    elif fault == "repeat":
        candidate["decision"] = "  PRIOR \n approach "
    elif fault == "empty":
        candidate["decision"] = " "
    else:
        candidate["decision"] = "步" * 6000
    (reference, status), _, repo = deliver(case)
    result = repo.artifacts[reference["artifact_revision_id"]]["payload"]
    assert status == BLOCKED and result["reason"] == reason and result["method"] is None


@pytest.mark.parametrize("field,value", [
    ("output_ref", ref("another-draft")), ("generation", 3), ("revision_count", 5),
])
def test_changed_source_or_generation_cannot_adopt_diagnosis(field, value):
    case = setup_case()
    case[0]["nodes"]["draft:scene"][field] = value
    with pytest.raises(DomainError, match="selected failed draft"):
        deliver(case)


def test_other_model_cannot_silently_replace_codex_diagnosis():
    case = setup_case()
    case[3]["model_configuration"]["adapter_kind"] = "openai-compatible"
    with pytest.raises(DomainError, match="bound Codex"):
        deliver(case)


@pytest.mark.parametrize("wrong_refs", ["foreign", "missing-report"])
def test_diagnosis_citations_must_bind_both_frozen_sources(wrong_refs):
    case = setup_case()
    if wrong_refs == "foreign":
        case[-1]["evidence_refs"].append({"kind": "ARTIFACT", "artifact_ref": ref("foreign")})
    else:
        case[-1]["evidence_refs"].pop()
    with pytest.raises(DomainError, match="both exact sources"):
        deliver(case)


def test_replay_preserves_one_result_and_rejects_mutated_candidate():
    case = setup_case()
    first, kernel, repo = deliver(case)
    second, _, _ = deliver(case, kernel=kernel, repo=repo)
    assert first == second and len(kernel.puts) == 1
    case[-1]["decision"] = "A changed candidate after original delivery"
    with pytest.raises(DomainError, match="Immutable result"):
        deliver(case, kernel=kernel, repo=repo)
