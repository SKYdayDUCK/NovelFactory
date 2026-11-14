"""PG17 case/receipt invariants with a synthetic trusted MCP gate, no model I/O.

Native MCP transport proof itself is covered by its separate gate tests. Here
the gate is explicitly injected so replay, scope and accounting can be checked.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import uuid4

import pytest
from test_agent_gateway import FixtureAdapter

from agent_runtime import native_codex, providers
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.internal_review_resolution import (
    AUTHOR_DECISION,
    COMMAND,
    KIND,
    RESOLVED,
    WORLD_INPUT,
    assignment,
    consume_resolution,
    record_resolution,
    request_from_blocked,
    request_from_fact_report,
)
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter, TrustedHostAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.fixture(autouse=True)
def forbid_live_send(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Internal resolution invariant test reached a model transport")
    for module, name in ((providers, "post"), (native_codex, "generate"), (native_codex, "start_host"),
                         (OpenAICompatibleAdapter, "generate"), (TrustedHostAdapter, "generate"), (FixtureAdapter, "generate")):
        monkeypatch.setattr(module, name, forbidden)


def setup(kernel, status="NO_CONFLICT", *, review_mode="historical", assignment_suffix=""):
    world = load_fixture(kernel)
    wr, owner = world["world_ref"], world["principal"]
    gateway = AgentGateway(kernel, {"fixture": FixtureAdapter()})
    def put(payload, kind):
        with kernel.objects.lock(), kernel.db.uow() as repo:
            reference = kernel._blob(payload)
            kernel._register_artifact(repo, reference, wr["work_id"], payload, kind, owner.principal_id)
            return reference
    plan_manifest = kernel.put_artifact(owner, wr["work_id"], {"schema_version": "p1", "plans": []}, "PLAN_MANIFEST")
    def prepare(role, kind, refs=(), assignment_text="Synthetic internal case"):
        cut = {"plane": "PLAN", "plan_manifest_ref": plan_manifest} if role == "chief_editor" else {"plane": "WORLD"}
        return RuntimeTaskService(kernel, gateway).prepare(owner, wr, role_id=role, task_kind=kind,
            story_time=point(1), subject_ids=[world["ids"]["sword"]], predicates=["owner"],
            assignment=assignment_text, adapter_id="fixture", input_artifacts=[plan_manifest, *refs], knowledge_cut=cut)
    writer = prepare("writer", "scene_draft")
    diagnosis = prepare("chief_editor", "editorial_decision")
    budget = kernel.tasks.create_budget(wr["work_id"], "1")
    call = kernel.tasks.reserve_call(diagnosis.task["task_id"], diagnosis.task["generation"], diagnosis.worker_id,
        budget_id=budget, amount="0.2", token_amount=100, call_key="supervised-candidate", request_hash="a" * 64)
    kernel.tasks.mark_call_sent(call["attempt_id"], worker_id=diagnosis.worker_id)
    kernel.tasks.settle_call(call["attempt_id"], state="UNKNOWN")
    draft_ref = put({"synthetic": "immutable draft identity"}, "NARRATIVE_DRAFT")
    report_ref = put({"synthetic": "immutable source report"}, "P6_REFLECTION_RECORD")
    target, diagnosis_id = "draft:" + str(uuid4()), "reassessment:synthetic:2"
    source = {"target": target, "world_ref": wr, "source_draft_ref": draft_ref, "report_ref": report_ref,
        "writer_task_id": writer.task["task_id"], "writer_generation": writer.task["generation"], "revision_count": 76,
        "repair_allowance": {"revision_baseline": 75, "reassessment_baseline": 0}, "round": 2}
    old = {"request": source, "outcome": "REASSESSMENT_BLOCKED", "reason": "REASSESSMENT_NEEDS_INPUT",
        "task_id": diagnosis.task["task_id"], "generation": diagnosis.task["generation"], "attempt_id": call["attempt_id"],
        "deferred_questions": ["Synthetic internal registration question"]}
    diagnosis_ref = put(old, "PRODUCTION_REASSESSMENT")
    node = {"stage": "reassessment", "state": "STRATEGY_CONSUMED", "task_id": diagnosis.task["task_id"],
            "generation": diagnosis.task["generation"], "output_ref": diagnosis_ref}
    board = {"world_ref": wr, "story_time": point(1), "nodes": {diagnosis_id: node, target: {
        "stage": "draft", "state": "STOPPED", "output_ref": draft_ref, "task_id": writer.task["task_id"],
        "generation": writer.task["generation"], "revision_count": 76, "repair_allowance": source["repair_allowance"],
        "reassessments": [{"node_id": "old:1"}, {"node_id": diagnosis_id}]}}}
    request = request_from_blocked(board, target, node, old)
    role, mode = ("reviewer", "verification") if review_mode == "verification" else ("chief_editor", "editorial_decision")
    review = prepare(role, mode, [draft_ref, report_ref, diagnosis_ref], assignment(request) + assignment_suffix)
    citation = {"kind": "ARTIFACT", "artifact_ref": report_ref}
    candidate = {"decision": json.dumps({"case_hash": request["case_hash"], "answers": [{"index": 0, "status": status,
        "explanation": "Synthetic proof-backed check", "correction": None, "evidence_refs": [citation]}],
        "method": "按固定版本证据重新独立核查本场。"}), "reason": "Synthetic internal verification",
        "deferred_questions": [], "evidence_refs": [citation]}
    candidate_ref = put(candidate, "AGENT_CANDIDATE")
    evidence = {"task_id": review.task["task_id"], "generation": review.task["generation"], "world_ref": wr,
                "attempt_id": str(uuid4()), "output_ref": candidate_ref, "input_ref": review.agent_input["input_ref"],
                "model_configuration": {"adapter_kind": "codex-cli"}}
    receipt = put({"synthetic_trusted_query_proof": True,
        "case_hash": request["case_hash"], "task_id": review.task["task_id"]}, "MCP_QUERY_RECEIPT")
    def gate(k, repo, frozen, task, audit, receipt_ref, parsed):
        proof = k.artifact(repo, receipt_ref, wr["work_id"])["payload"]
        return (proof.get("synthetic_trusted_query_proof") is True and proof["case_hash"] == frozen["case_hash"]
                and proof["task_id"] == task["task_id"] == audit["task_id"])
    return {"owner": owner, "board": board, "request": request, "task": review.task, "evidence": evidence,
            "candidate": candidate, "query_receipt_ref": receipt, "query_gate": gate}


def snapshot(kernel, item):
    work = item["request"]["world_ref"]["work_id"]
    with kernel.db.uow(write=False) as repo:
        tasks = repo.many("tasks", {"work_id": work}, order=["task_id"])
        return {"tasks": tasks, "calls": repo.many("call_attempts", {"task_id": [task["task_id"] for task in tasks]}, order=["attempt_id"]),
                "budgets": repo.many("budgets", {"work_id": work}, order=["budget_id"])}


def record(kernel, item):
    with kernel.objects.lock(), kernel.db.uow() as repo:
        return record_resolution(kernel, repo, **item)


def consume(kernel, item, reference, owner=None, board=None):
    with kernel.db.uow() as repo:
        return consume_resolution(kernel, repo, owner or item["owner"], board or item["board"], item["request"], reference)


def fact_windows(kernel):
    """Synthetic 35-question report, with real task/grant/UNKNOWN journal identities."""
    base = setup(kernel)
    owner, board = base["owner"], base["board"]
    wr, target = board["world_ref"], base["request"]["target"]
    def put(payload, kind):
        with kernel.objects.lock(), kernel.db.uow() as repo:
            reference = kernel._blob(payload)
            kernel._register_artifact(repo, reference, wr["work_id"], payload, kind, owner.principal_id)
            return reference
    with kernel.db.uow(write=False) as repo:
        subject = next(iter(kernel.revision(repo, wr)["snapshot"]["entities"]))
        spec = repo.one("task_inputs", {"task_id": base["task"]["task_id"], "generation": base["task"]["generation"]})["spec"]
        manifest = next(ref for ref in spec["input_artifacts"] if kernel.artifact(repo, ref, wr["work_id"])["kind"] == "PLAN_MANIFEST")
    gateway = AgentGateway(kernel, {"fixture": FixtureAdapter()})
    def prepare(role, kind, refs=(), assignment_text="Synthetic bounded fact inquiry"):
        return RuntimeTaskService(kernel, gateway).prepare(owner, wr, role_id=role, task_kind=kind,
            story_time=point(1), subject_ids=[subject], predicates=["owner"], assignment=assignment_text,
            knowledge_cut={"plane": "PLAN", "plan_manifest_ref": manifest} if role == "chief_editor" else {"plane": "WORLD"},
            adapter_id="fixture", input_artifacts=[manifest, *refs])
    facts = prepare("evidence_maintainer", "assertion_extract")
    budget = kernel.tasks.create_budget(wr["work_id"], "1")
    call = kernel.tasks.reserve_call(facts.task["task_id"], facts.task["generation"], facts.worker_id,
        budget_id=budget, amount="0.2", token_amount=100, call_key="supervised-candidate", request_hash="f" * 64)
    kernel.tasks.mark_call_sent(call["attempt_id"], worker_id=facts.worker_id)
    kernel.tasks.settle_call(call["attempt_id"], state="UNKNOWN")
    extraction = {"draft_ref": board["nodes"][target]["output_ref"], "task_id": facts.task["task_id"], "generation": facts.task["generation"],
        "assertions": [{"assertion_id": str(uuid4()), "span": None, "summary": "Synthetic unresolved registration " + str(i),
                        "world_check": None, "knowledge_use": None} for i in range(35)]}
    extraction_ref = put(extraction, "ASSERTION_EXTRACTION")
    report = {"status": "BLOCKED", "draft_ref": extraction["draft_ref"], "adopted_world_ref": wr, "extraction_ref": extraction_ref,
              "hard_blockers": [{"assertion_id": row["assertion_id"], "code": "UNRESOLVED_STATE", "message": row["summary"]}
                                for row in extraction["assertions"]]}
    report_ref = put(report, "RECONCILIATION_REPORT")
    source_ref = put({"reconciliation_ref": report_ref, "extraction_ref": extraction_ref}, "ROLLING_FACT_REPORT")
    node = {"stage": "facts", "state": "STOPPED", "parents": [target], "task_id": facts.task["task_id"],
            "generation": facts.task["generation"], "output_ref": source_ref}
    board["nodes"]["facts:synthetic"] = node
    items = []
    for start in range(0, 35, 8):
        request = request_from_fact_report(board, target, node, report_ref, report, extraction,
                                          attempt_id=call["attempt_id"], question_start=start)
        reviewer = prepare("reviewer", "verification", [extraction["draft_ref"], report_ref, source_ref, extraction_ref], assignment(request))
        citation = {"kind": "ARTIFACT", "artifact_ref": report_ref}
        candidate = {"decision": json.dumps({"case_hash": request["case_hash"], "answers": [{"index": q["index"], "status": "NO_CONFLICT",
            "explanation": "Synthetic trusted fixed-cut inquiry", "correction": None, "evidence_refs": [citation]} for q in request["questions"]]}),
            "reason": "Synthetic fact inquiries do not accept the text", "deferred_questions": [], "evidence_refs": [citation]}
        evidence = {"task_id": reviewer.task["task_id"], "generation": reviewer.task["generation"], "world_ref": wr,
                    "attempt_id": str(uuid4()), "output_ref": put(candidate, "AGENT_CANDIDATE"),
                    "input_ref": reviewer.agent_input["input_ref"], "model_configuration": {"adapter_kind": "codex-cli"}}
        proof = put({"case_hash": request["case_hash"], "task_id": reviewer.task["task_id"]}, "MCP_QUERY_RECEIPT")
        def gate(k, repo, frozen, task, audit, receipt_ref, parsed):
            payload = k.artifact(repo, receipt_ref, wr["work_id"])["payload"]
            return payload["case_hash"] == frozen["case_hash"] and payload["task_id"] == task["task_id"] == audit["task_id"]
        items.append({"owner": owner, "board": board, "request": request, "task": reviewer.task, "evidence": evidence,
                      "candidate": candidate, "query_receipt_ref": proof, "query_gate": gate})
    return items


def test_record_and_once_consumption_preserve_unknown_accounting_and_original_slot(kernel_env):
    kernel = kernel_env["kernel"]
    item = setup(kernel)
    before, original_board = snapshot(kernel, item), deepcopy(item["board"])
    reference, outcome = record(kernel, item)
    assert outcome == RESOLVED and record(kernel, item) == (reference, outcome)
    first, replay = consume(kernel, item, reference), consume(kernel, item, reference)
    assert first["consumed_now"] is True and replay["consumed_now"] is False
    assert first["diagnosis_round"] == 2 and first["allowance_changed"] is False
    assert item["board"] == original_board and snapshot(kernel, item) == before
    assert before["calls"][0]["state"] == "UNKNOWN"
    with kernel.db.uow(write=False) as repo:
        assert len(repo.many("receipts", {"work_id": item["request"]["world_ref"]["work_id"], "command_type": COMMAND})) == 1


@pytest.mark.parametrize("suffix", ["", '\nSchema repair follows\n{"diagnostic":"synthetic"}\nnon-JSON suffix'])
def test_world_verification_role_has_exact_case_binding_even_with_schema_suffix(kernel_env, suffix):
    kernel = kernel_env["kernel"]
    item = setup(kernel, review_mode="verification", assignment_suffix=suffix)
    reference, outcome = record(kernel, item)
    assert outcome == RESOLVED and consume(kernel, item, reference)["consumed_now"]


def test_mismatched_final_case_metadata_cannot_be_reused_for_another_case(kernel_env):
    kernel = kernel_env["kernel"]
    item = setup(kernel, review_mode="verification", assignment_suffix='\n{"case_hash":"different-case"}')
    with pytest.raises(DomainError, match="bound to another case"):
        record(kernel, item)


@pytest.mark.parametrize("gate", [None, False, "model-says-it-queried"])
def test_no_self_report_or_absent_query_gate_can_record_resolution(kernel_env, gate):
    kernel = kernel_env["kernel"]
    item = setup(kernel)
    item["query_gate"] = (lambda *a: gate) if gate is not None else None
    before = snapshot(kernel, item)
    with pytest.raises(DomainError, match="Trusted actual MCP"):
        record(kernel, item)
    assert snapshot(kernel, item) == before
    with kernel.db.uow(write=False) as repo:
        assert not repo.many("artifacts", {"work_id": item["request"]["world_ref"]["work_id"], "kind": KIND})


@pytest.mark.parametrize("status,outcome", [("UNREGISTERED", WORLD_INPUT), ("EXTERNAL_DECISION_REQUIRED", AUTHOR_DECISION)])
def test_world_or_protected_author_decision_cannot_consume_or_clear_anything(kernel_env, status, outcome):
    kernel = kernel_env["kernel"]
    item = setup(kernel, status)
    before = snapshot(kernel, item)
    reference, actual = record(kernel, item)
    assert actual == outcome
    with pytest.raises(DomainError, match="still required"):
        consume(kernel, item, reference)
    assert snapshot(kernel, item) == before


def test_changed_draft_cannot_adopt_answers_and_old_receipt_replay_is_safe(kernel_env):
    kernel = kernel_env["kernel"]
    item = setup(kernel)
    reference, _ = record(kernel, item)
    old = deepcopy(item["board"])
    item["board"]["nodes"][item["request"]["target"]]["revision_count"] += 1
    with pytest.raises(DomainError, match="selected draft"):
        consume(kernel, item, reference)
    assert consume(kernel, item, reference, board=old)["consumed_now"]
    assert consume(kernel, item, reference)["consumed_now"] is False


def test_consumption_is_global_once_even_for_another_admin_and_concurrent_callers(kernel_env):
    kernel = kernel_env["kernel"]
    item = setup(kernel)
    reference, _ = record(kernel, item)
    wr = item["request"]["world_ref"]
    second = kernel.issue_grant(item["owner"], wr["work_id"], role_id="control_operator", actor_id=None,
        branch_ids=[wr["branch_id"]], capabilities=["ADMIN"], planes=["WORLD"], max_story_time=point(1))["principal"]
    with ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(consume, kernel, item, reference, who) for who in (item["owner"], second)]
        results = [future.result() for future in futures]
    assert sorted(row["consumed_now"] for row in results) == [False, True]


def test_failed_receipt_rolls_back_consumption_without_changing_old_call(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    item = setup(kernel)
    reference, _ = record(kernel, item)
    original = kernel._receipt
    def fail(repo, owner, wr, command, *args, **kwargs):
        if command == COMMAND:
            raise RuntimeError("synthetic failed receipt")
        return original(repo, owner, wr, command, *args, **kwargs)
    before = snapshot(kernel, item)
    monkeypatch.setattr(kernel, "_receipt", fail)
    with pytest.raises(RuntimeError, match="synthetic failed"):
        consume(kernel, item, reference)
    monkeypatch.setattr(kernel, "_receipt", original)
    assert consume(kernel, item, reference)["consumed_now"]
    assert snapshot(kernel, item) == before


def test_thirty_five_fact_questions_have_five_independent_immutable_receipts(kernel_env):
    kernel = kernel_env["kernel"]
    items = fact_windows(kernel)
    before, board = snapshot(kernel, items[0]), deepcopy(items[0]["board"])
    results, references = [], []
    for item in items:
        reference, outcome = record(kernel, item)
        assert outcome == RESOLVED
        references.append(reference)
        results.append(consume(kernel, item, reference))
    assert len({ref["artifact_revision_id"] for ref in references}) == 5
    assert [row["next_question_start"] for row in results] == [8, 16, 24, 32, None]
    assert [answer["index"] for row in results for answer in row["answers"]] == list(range(35))
    assert record(kernel, items[0]) == (references[0], RESOLVED)
    assert consume(kernel, items[0], references[0])["consumed_now"] is False
    assert snapshot(kernel, items[0]) == before and items[0]["board"] == board
    assert all(row["allowance_changed"] is False and row["provider_calls_sent"] == 0 for row in results)
