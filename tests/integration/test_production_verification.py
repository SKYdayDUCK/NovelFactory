"""Actual PostgreSQL/MCP handoffs with synthetic native/Flash responses only.

The source text and semantic findings are explicit engineering fixtures. This
checks orchestration/proof/accounting, never a claim about literary quality.
"""
import json
import traceback
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from test_production_strategy import current_board, drive, synthetic_production

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.internal_review_resolution import COMMAND
from novel_factory.application.rolling_facts import passage_index
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.canonical import canonical_bytes, fingerprint
from novel_factory.domain.reconciliation import span_for
from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def intercept(kernel, monkeypatch, gateway, *, facts=False, second_diagnosis=False, verification_failures=0):
    """Keep the existing synthetic Host's real MCP calls and replace only payloads."""
    original = RuntimeModelAdapter.generate
    observed = {"verification": [], "facts": 0, "input_sizes": [], "projected_windows": 0,
                "fact_passage_packets": 0, "reviewer_passage_packets": 0, "adapter_errors": []}
    original_messages = AgentGateway._messages
    def measured_messages(gateway, principal, binding):
        messages = original_messages(gateway, principal, binding)
        if binding["task"]["role_id"] == "evidence_maintainer":
            user = json.loads(messages[1]["content"])
            observed["input_sizes"].append({"total_bytes": len(canonical_bytes(list(messages))),
                "section_bytes": {key: len(canonical_bytes(value)) for key, value in user.items()},
                "material_bytes": [len(item["content"].encode("utf-8")) for item in user.get("authorized_materials", [])]})
        return messages
    monkeypatch.setattr(AgentGateway, "_messages", measured_messages)
    def error_details(error):
        details = {"type": type(error).__name__, "code": getattr(error, "code", None),
            "message": getattr(error, "safe_message", None), "frames": [
                {"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                for frame in traceback.extract_tb(error.__traceback__)]}
        if isinstance(error, BaseExceptionGroup):
            details["children"] = [error_details(child) for child in error.exceptions]
        trace = error.__traceback__
        while trace:
            frame = trace.tb_frame
            if Path(frame.f_code.co_filename).name == "review_mcp.py" and frame.f_code.co_name == "call":
                broker = frame.f_locals.get("self")
                result_bytes = frame.f_locals.get("result_bytes")
                details["capacity"] = {"tool": frame.f_locals.get("name"),
                    "new_result_bytes": len(result_bytes) if isinstance(result_bytes, bytes) else None,
                    "used_result_bytes": getattr(broker, "_total_bytes", None), "limit": getattr(broker, "_result_limit", None)}
            trace = trace.tb_next
        return details
    def generate(adapter, packet):
        try:
            response = original(adapter, packet)
        except Exception as error:
            observed["adapter_errors"].append(error_details(error))
            raise
        envelope = json.loads(response.text)
        with kernel.db.uow(write=False) as repo:
            task = repo.one("tasks", {"task_id": envelope["task_id"]})
            board = current_board(kernel, envelope["world_ref"])
            job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
            node = board["nodes"].get(job["config"].get("production_node")) if job else None
        if facts and task["role_id"] == "writer":
            envelope["payload"]["text"] = "\n".join(["窗边雨声轻响。"] * 35)
        elif facts and task["role_id"] == "evidence_maintainer":
            observed["facts"] += 1
            with kernel.db.uow(write=False) as repo:
                for proof in response.raw_response["review_mcp_receipt_refs"]:
                    receipt = kernel.artifact(repo, proof, task["work_id"])["payload"]
                    material = receipt["result"].get("structuredContent", {})
                    if receipt["tool"] != "nf_review_material" or "draft_ref" not in material:
                        continue
                    entries = passage_index(material["text"])
                    assert material["passages"] == [{"id": row["id"], "text": row["text"]} for row in entries]
                    assert "".join(row["text"] for row in material["passages"]) == material["text"].replace("\n", "")
                    observed["fact_passage_packets"] += 1
            if observed["facts"] == 1:
                assertions = [{"passage_ids": [i], "classification": "UNCERTAIN", "summary": "合成非持久雨声待独立核查。",
                    "event_ref": None, "world_check": None, "knowledge_use": None, "proposed_command": None,
                    "disclosure_id": None, "declared_delta_index": None} for i in range(1, 36)]
                envelope["payload"]["text"] = json.dumps({"assertions": assertions})
            else:
                with kernel.db.uow(write=False) as repo:
                    for proof in response.raw_response["review_mcp_receipt_refs"]:
                        receipt = kernel.artifact(repo, proof, task["work_id"])["payload"]
                        packet = receipt["result"].get("structuredContent", {})
                        if receipt["tool"] != "nf_review_material" or packet.get("projection") != "VERIFIED_FINDINGS":
                            continue
                        original_record = kernel.artifact(repo, packet["source_ref"], task["work_id"], kind="INTERNAL_REVIEW_RESOLUTION")["payload"]
                        projected = packet["resolution"]
                        expanded = [{**{key: value for key, value in answer.items() if key not in {"evidence_indices", "assertion_id", "span"}},
                                     "evidence_refs": [projected["evidence_table"][i] for i in answer["evidence_indices"]]}
                                    for answer in projected["answers"]]
                        assert expanded == original_record["answers"]
                        questions = {q["index"]: q for q in original_record["request"]["questions"]}
                        assert all(answer["assertion_id"] == questions[answer["index"]]["assertion_id"]
                                   and answer["span"] == questions[answer["index"]]["span"] for answer in projected["answers"])
                        observed["projected_windows"] += 1
        elif second_diagnosis and task["role_id"] == "chief_editor" and node["reassessment_request"]["round"] == 2:
            envelope["payload"]["deferred_questions"] = ["Synthetic exact-version internal question"]
        elif task["task_kind"] == "verification":
            proof_refs = response.raw_response["review_mcp_receipt_refs"]
            with kernel.db.uow(write=False) as repo:
                actual = [(ref, kernel.artifact(repo, ref, task["work_id"])["payload"]) for ref in proof_refs]
                query_ref = next(ref for ref, receipt in actual if receipt["tool"] == "nf_query_batch")
                request = next(receipt["result"]["structuredContent"]["case"] for _, receipt in actual
                    if receipt["tool"] == "nf_review_material" and receipt["arguments"]["artifact_ref"] == node["internal_case"]["report_ref"])
                assert request == node["internal_case"]
                draft = next(receipt["result"]["structuredContent"] for _, receipt in actual
                    if receipt["tool"] == "nf_review_material" and receipt["arguments"]["artifact_ref"] == request["source_draft_ref"])
                assert all(row["span"] == span_for(draft["text"], row["span"]["start"], row["span"]["end"]) for row in draft["passages"])
                observed["reviewer_passage_packets"] += 1
            citations = [{"kind": "ARTIFACT", "artifact_ref": query_ref},
                         {"kind": "ARTIFACT", "artifact_ref": request["report_ref"]}]
            inner = {"case_hash": request["case_hash"], "answers": [{"index": q["index"], "status": "NO_CONFLICT",
                "explanation": "Synthetic real-MCP read of the exact current source and fixed world.",
                "correction": None, "evidence_refs": citations} for q in request["questions"]]}
            if request["source_kind"] == "DIAGNOSIS":
                inner["method"] = "Synthetic verified method: check the measured extent against the exact current draft."
            if len(observed["verification"]) < verification_failures:
                if len(observed["verification"]) % 2 == 0:
                    inner.pop("method", None)  # Outer schema is valid; inner continuation contract is not.
                else:
                    for answer in inner["answers"]:
                        answer["evidence_refs"] = citations[:1]  # Real query, but no current-material citation.
            envelope["payload"] = {"decision": json.dumps(inner), "reason": "Synthetic evidence-backed internal inquiry",
                                   "deferred_questions": [], "evidence_refs": citations}
            observed["verification"].append(deepcopy(request))
        return replace(response, text=json.dumps(envelope))
    monkeypatch.setattr(RuntimeModelAdapter, "generate", generate)
    return observed


def test_second_diagnosis_queries_world_then_continues_once_without_renewing_old_budget(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, diagnoses = synthetic_production(kernel, monkeypatch, lengths=[1, 5, 3, 2, 2, 7])
    observed = intercept(kernel, monkeypatch, gateway, second_diagnosis=True)
    board, _ = drive(kernel, gateway, owner, wr,
        until=lambda b: any(n["stage"] == "internal_verification" and n["state"] == "PENDING" for n in b["nodes"].values()))
    pending = next(n for n in board["nodes"].values() if n["stage"] == "internal_verification")
    request = pending["internal_case"]
    assert request["diagnosis_round"] == 2
    with kernel.db.uow(write=False) as repo:
        original_call = repo.one("call_attempts", {"attempt_id": request["source_attempt_id"]})
        original_record = kernel.artifact(repo, request["source_ref"], wr["work_id"])["payload"]
        limits = {row["budget_id"]: row["limit_amount"] for row in repo.many("budgets", {"work_id": wr["work_id"]})}
    board, cursor = drive(kernel, gateway, owner, wr)
    writer = board["nodes"][request["target"]]
    assert cursor["state"] == "FROZEN" and len(writer["reassessments"]) == len(diagnoses) == 2
    assert len(observed["verification"]) == 1
    task = next(context for context, _, _ in calls if context["task_kind"] == "verification")
    with kernel.db.uow(write=False) as repo:
        assert repo.one("call_attempts", {"attempt_id": request["source_attempt_id"]}) == original_call
        assert kernel.artifact(repo, request["source_ref"], wr["work_id"])["payload"] == original_record
        assert {row["budget_id"]: row["limit_amount"] for row in repo.many("budgets", {"work_id": wr["work_id"]})} == limits
        context = repo.one("contexts", {"context_bundle_id": repo.one("tasks", {"task_id": task["task_id"]})["context_bundle_id"]})
        assert context["read_cut"]["knowledge_cut"]["plane"] == "WORLD"
        assert len(repo.many("receipts", {"work_id": wr["work_id"], "command_type": COMMAND})) == 1
    before = len(calls)
    for _ in range(4):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
    assert len(calls) == before


def test_thirty_five_unresolved_facts_are_verified_in_five_windows_before_one_rereading(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, diagnoses = synthetic_production(kernel, monkeypatch, target=(200, 400))
    observed = intercept(kernel, monkeypatch, gateway, facts=True)
    try:
        board, cursor = drive(kernel, gateway, owner, wr, ticks=180)
    except pytest.fail.Exception as error:
        error.add_note("Synthetic facts input byte sizes: " + json.dumps(observed["input_sizes"][-2:]))
        error.add_note("Synthetic adapter error frames: " + json.dumps(observed["adapter_errors"]))
        raise
    requests = observed["verification"]
    assert cursor["state"] == "FROZEN" and not diagnoses
    assert [request["question_start"] for request in requests] == [0, 8, 16, 24, 32]
    assert [len(request["questions"]) for request in requests] == [8, 8, 8, 8, 3]
    assert [question["index"] for request in requests for question in request["questions"]] == list(range(35))
    assert len({request["full_questions_sha256"] for request in requests}) == 1
    assert observed["facts"] == 2  # No reading is replaced until every internal window completes.
    assert observed["projected_windows"] == 5  # Expanding evidence indices exactly reproduces every immutable answer.
    assert observed["fact_passage_packets"] == 2 and observed["reviewer_passage_packets"] == 5
    writer = next(node for node in board["nodes"].values() if node["stage"] == "draft")
    assert writer.get("revision_count", 0) == 0 and not writer.get("reassessments")
    with kernel.db.uow(write=False) as repo:
        receipts = repo.many("receipts", {"work_id": wr["work_id"], "command_type": COMMAND})
        assert len(receipts) == 5 and len({fingerprint(row["payload"]["resolution_ref"]) for row in receipts}) == 5
        extraction = kernel.artifact(repo, requests[0]["extraction_ref"], wr["work_id"], kind="ASSERTION_EXTRACTION")["payload"]
        draft = kernel.artifact(repo, requests[0]["source_draft_ref"], wr["work_id"], kind="NARRATIVE_DRAFT")["payload"]
        text = kernel.objects.read(draft["text_ref"]["sha256"]).decode("utf-8")
        assert [item["span"] for item in extraction["assertions"]] == [span_for(text, row["start"], row["end"]) for row in passage_index(text)]
    before = len(calls)
    for _ in range(3):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
    assert len(calls) == before


def test_internal_json_and_per_question_evidence_errors_receive_only_two_bounded_corrections(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, _calls, diagnoses = synthetic_production(kernel, monkeypatch, lengths=[1, 5, 3, 2, 2, 7])
    observed = intercept(kernel, monkeypatch, gateway, second_diagnosis=True, verification_failures=2)
    board, cursor = drive(kernel, gateway, owner, wr)
    assert cursor["state"] == "FROZEN" and len(diagnoses) == 2
    assert len(observed["verification"]) == 3 and len({item["case_hash"] for item in observed["verification"]}) == 1
    node = next(node for node in board["nodes"].values() if node["stage"] == "internal_verification")
    repairs = node["evidence_repairs"]
    assert len(repairs) == 2 and {row["code"] for row in repairs} == {"INVALID_ARGUMENT", "INVALID_EVIDENCE"}
    assert node["state"] == "VERIFICATION_CONSUMED"
    with kernel.db.uow(write=False) as repo:
        for repair in repairs:
            assert repo.one("tasks", {"task_id": repair["task_id"]})["state"] == "WAITING_REVIEW"
            call = repo.one("call_attempts", {"attempt_id": repair["attempt_id"]})
            assert call["state"] == "UNKNOWN" and call["actual_amount"] is None
            assert kernel.artifact(repo, repair["candidate_ref"], wr["work_id"])["kind"] == "AGENT_CANDIDATE"


def test_repeated_inner_errors_stop_after_two_corrections_without_more_model_work(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, _ = synthetic_production(kernel, monkeypatch, lengths=[1, 5, 3, 2, 2])
    observed = intercept(kernel, monkeypatch, gateway, second_diagnosis=True, verification_failures=3)
    board, _ = drive(kernel, gateway, owner, wr,
        until=lambda b: any(node.get("reason") == "INTERNAL_VERIFICATION_REPAIR_EXHAUSTED" for node in b["nodes"].values()))
    assert len(observed["verification"]) == 3
    node = next(node for node in board["nodes"].values() if node["stage"] == "internal_verification")
    assert len(node["evidence_repairs"]) == 2
    before = len(calls)
    for _ in range(5):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
    assert len(calls) == before
