"""Actual PostgreSQL/MCP handoffs with synthetic native/Flash responses only.

The source text and semantic findings are explicit engineering fixtures. This
checks orchestration/proof/accounting, never a claim about literary quality.
"""
import asyncio
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
from novel_factory.application.rolling_production import rolling_status
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
                         *[{"kind": "ARTIFACT", "artifact_ref": draft["text_ref"],
                            "span": span_for(draft["text"], start, start + 1)} for start in range(2)]]
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


def coverage_fixture(kernel, monkeypatch, *, diagnosis_cycle=False, pending_child=False, covered=False, unknown_query=False,
                     editorial_diagnosis=False, wrong_parent_time=False, protected_author=False):
    """Use normal snapshots, native journals and actual scoped MCP state reads."""
    import test_production_strategy as strategy_fixture
    from test_release import setup_batch

    from novel_factory.application.review_mcp import ReviewMcpBroker
    from novel_factory.fixtures import fixture_definition, point

    protected_clause = "测试保护：结局签字须由作者决定。"
    if protected_author:
        from novel_factory.application.planning import PlanningService
        original_contract = PlanningService.save_volume_contract
        def save_contract(service, principal, payload, key):
            from uuid import uuid4
            with kernel.db.uow(write=False) as repo:
                policy = repo.one("works", {"work_id": payload["input_world_ref"]["work_id"]})["policy_version"]
            clause = {"commitment_id": str(uuid4()), "description": protected_clause, "policy_ref": policy, "evidence_refs": []}
            return original_contract(service, principal, {**payload, "protected_commitments": [clause]}, key)
        monkeypatch.setattr(PlanningService, "save_volume_contract", save_contract)

    def uncovered_batch(kernel, **kwargs):
        definition = fixture_definition("r1_sword")
        created = kernel.bootstrap_work("TEST_ONLY:pending-coverage", definition["genesis"],
            frontiers=definition["frontiers"] if covered else {
                entity: point(0, 1) for entity in definition["genesis"]["entities"]})
        return setup_batch(kernel, fixture={**definition, **created}, **kwargs)
    monkeypatch.setattr(strategy_fixture, "setup_batch", uncovered_batch)
    gateway, owner, wr, calls, diagnoses = synthetic_production(kernel, monkeypatch,
        **({"lengths": [1, 5, 3, 2, 2, 7]} if diagnosis_cycle else {}), editorial=editorial_diagnosis)
    with kernel.db.uow(write=False) as repo:
        board = current_board(kernel, wr)
        writer = next(node for node in board["nodes"].values() if node["stage"] == "draft")
        scene = kernel.artifact(repo, writer["scene_ref"], wr["work_id"])["payload"]
        lookup = {key: scene["entry_checks"][0][key] for key in ("subject_id", "predicate", "at", "plane")}
    additional_subject = fixture_definition("r1_sword")["ids"]["friend"]
    if unknown_query or pending_child:
        from novel_factory.application.production_pipeline import _save
        if unknown_query:
            lookup["predicate"] = "fixture_unresolved_predicate"
        with kernel.objects.lock(), kernel.db.uow() as repo:
            if unknown_query:
                board["predicates"].append(lookup["predicate"])
            if pending_child:
                board["subject_ids"].append(additional_subject)
            _save(kernel, repo, owner, board)
    def actual_mcp(kernel, request, thread, turn, **_options):
        with kernel.db.uow(write=False) as repo:
            call = repo.one("call_attempts", {"attempt_id": request.attempt_id})
            task = repo.one("tasks", {"task_id": call["task_id"]})
        if task["role_id"] == "chief_editor":
            from native_review_helpers import run_review_mcp
            return run_review_mcp(kernel, request, thread, turn)
        async def run():
            async with ReviewMcpBroker.from_attempt(kernel, request) as broker:
                broker.bind(thread, turn)
                await broker.call({"threadId": thread, "turnId": turn, "callId": "coverage-context",
                    "tool": "nf_context_get", "arguments": {}})
                if "nf_query_batch" in broker.allowed_tools:
                    selected = lookup
                    if pending_child or wrong_parent_time:
                        with kernel.db.uow(write=False) as repo:
                            job = repo.one("supervisor_jobs", {"task_id": broker.scope["task_id"]})
                            board = current_board(kernel, broker.scope["world_ref"])
                            node = board["nodes"].get(job["config"].get("production_node"), {})
                            if pending_child and node.get("internal_case", {}).get("parent_case_hash"):
                                selected = {**lookup, "subject_id": additional_subject}
                            elif (wrong_parent_time and node.get("internal_case")
                                  and not node["internal_case"].get("parent_case_hash") and not node.get("evidence_repairs")):
                                selected = {**lookup, "at": scene["scene_contract"]["story_interval"]["end"]}
                    await broker.call({"threadId": thread, "turnId": turn, "callId": "coverage-query",
                        "tool": "nf_query_batch", "arguments": {"lookups": [selected]}})
                for index, reference in enumerate(broker.scope["material_refs"]):
                    await broker.call({"threadId": thread, "turnId": turn, "callId": f"coverage-material-{index}",
                        "tool": "nf_review_material", "arguments": {"artifact_ref": reference}})
                return list(broker.receipt_refs)
        return asyncio.run(run())
    monkeypatch.setattr(strategy_fixture, "run_review_mcp", actual_mcp)
    original = RuntimeModelAdapter.generate
    observed = []
    from novel_factory.application import production_verification as verification_service
    original_record = verification_service.record_resolution
    def record_resolution(*args, **kwargs):
        try:
            return original_record(*args, **kwargs)
        except Exception as error:
            observed.append({"delivery_error": {"type": type(error).__name__, "code": getattr(error, "code", None),
                "message": getattr(error, "safe_message", None), "frames": [
                    (Path(frame.filename).name, frame.lineno, frame.name) for frame in traceback.extract_tb(error.__traceback__)]}})
            raise
    monkeypatch.setattr(verification_service, "record_resolution", record_resolution)
    def generate(adapter, packet):
        try:
            response = original(adapter, packet)
        except Exception as error:
            def details(error):
                return {"type": type(error).__name__, "code": getattr(error, "code", None),
                    "message": getattr(error, "safe_message", None), "frames": [
                        (Path(frame.filename).name, frame.lineno, frame.name) for frame in traceback.extract_tb(error.__traceback__)],
                    "children": [details(child) for child in getattr(error, "exceptions", [])]}
            diagnostic = {"adapter_error": details(error)}
            if diagnosis_cycle:
                diagnostic["assignment_tail"] = json.loads(packet.messages[1]["content"])["assignment"][-600:]
            observed.append(diagnostic)
            raise
        envelope = json.loads(response.text)
        with kernel.db.uow(write=False) as repo:
            task = repo.one("tasks", {"task_id": envelope["task_id"]})
            if task["role_id"] == "chief_editor" and (diagnosis_cycle or editorial_diagnosis):
                board = current_board(kernel, envelope["world_ref"])
                job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})
                if board["nodes"][job["config"]["production_node"]]["reassessment_request"]["round"] == (1 if editorial_diagnosis else 2):
                    envelope["payload"]["deferred_questions"] = ["Synthetic exact-scene world coverage question"]
                    return replace(response, text=json.dumps(envelope))
            if task["task_kind"] != "verification":
                return response
            receipts = [(ref, kernel.artifact(repo, ref, task["work_id"])["payload"])
                        for ref in response.raw_response["review_mcp_receipt_refs"]]
            case = next(receipt["result"]["structuredContent"]["case"] for _, receipt in receipts
                        if receipt["tool"] == "nf_review_material" and "case" in receipt["result"]["structuredContent"])
            query_ref, query = next((ref, receipt) for ref, receipt in receipts if receipt["tool"] == "nf_query_batch")
            answer = query["result"]["structuredContent"]["items"][0]["payload"]["state_items"][0]["answer"]
            status = ("PENDING_EVALUATION" if answer["evaluation_status"] == "PENDING" else
                      "UNKNOWN" if answer["value_status"] == "UNRESOLVED" else "NO_CONFLICT")
            citations = [{"kind": "ARTIFACT", "artifact_ref": query_ref},
                         {"kind": "ARTIFACT", "artifact_ref": case["report_ref"]}]
            explanation = "Synthetic exact-scene state read, preserved with its actual receipt."
            if protected_author:
                policy = next(receipt for _, receipt in receipts if receipt["tool"] == "nf_review_material"
                              and receipt["result"]["structuredContent"].get("projection") == "REGISTERED_AUTHOR_PROTECTION")
                assert policy["result"]["structuredContent"]["protected_commitments"][0]["description"] == protected_clause
                status = "EXTERNAL_DECISION_REQUIRED"
                citations.append({"kind": "ARTIFACT", "artifact_ref": policy["arguments"]["artifact_ref"]})
                explanation = "已读取作者明确条款：" + protected_clause
            result = {"case_hash": case["case_hash"], "answers": [{"index": question["index"],
                "status": status, "explanation": explanation,
                "correction": None, "evidence_refs": citations} for question in case["questions"]]}
            if case["source_kind"] == "DIAGNOSIS":
                result["method"] = "Synthetic method: reread the current scene on its formally closed world cut."
            observed.append({"case": deepcopy(case), "query": deepcopy(answer)})
        envelope["payload"] = {"decision": json.dumps(result), "reason": "Synthetic fixed-cut coverage inquiry",
                               "deferred_questions": [], "evidence_refs": citations}
        return replace(response, text=json.dumps(envelope))
    monkeypatch.setattr(RuntimeModelAdapter, "generate", generate)
    steps = []
    original_run = RuntimeSupervisor.run_once
    def run_once(supervisor, *args, **kwargs):
        result = original_run(supervisor, *args, **kwargs)
        steps.append({key: result[key] for key in ("status", "reason", "node_id", "state") if key in result})
        return result
    monkeypatch.setattr(RuntimeSupervisor, "run_once", run_once)
    gateway._coverage_fixture_steps = steps
    return gateway, owner, wr, calls, diagnoses, observed


def drive_coverage(kernel, gateway, owner, wr, *, ticks=160, until=None):
    """World revalidation makes durable progress outside the production board."""
    idle = 0
    for _ in range(ticks):
        step = RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
        board = current_board(kernel, wr)
        cursor = rolling_status(kernel, owner, wr["work_id"], wr["branch_id"])
        if cursor["state"] == "FROZEN" or until and until(board):
            return board, cursor
        idle = idle + 1 if step["status"] == "IDLE" else 0
        if idle >= 5:
            break
    pytest.fail("Coverage flow did not complete within its durable-step bound: " + json.dumps({
        "steps": gateway._coverage_fixture_steps[-10:], "nodes": [
            {key: node[key] for key in ("stage", "state", "reason") if key in node} for node in board["nodes"].values()]}))


def test_pending_world_closes_formally_then_rereads_same_draft_to_frozen(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, diagnoses, observed = coverage_fixture(kernel, monkeypatch)
    try:
        board, cursor = drive_coverage(kernel, gateway, owner, wr, ticks=160)
    except pytest.fail.Exception as error:
        error.add_note("Coverage fixture reads: " + json.dumps(observed))
        raise
    assert cursor["state"] == "FROZEN" and not diagnoses
    assert len(observed) == 2
    parent, child = (entry["case"] for entry in observed)
    assert parent["world_ref"] == wr and child["world_ref"] != wr
    assert child["parent_case_hash"] == parent["case_hash"]
    assert observed[0]["query"]["evaluation_status"] == "PENDING"
    assert observed[1]["query"]["evaluation_status"] == "EVALUATED"
    writer = board["nodes"][parent["target"]]
    assert writer["output_ref"] == parent["source_draft_ref"] == child["source_draft_ref"]
    assert writer.get("revision_count", 0) == 0 and not writer.get("reassessments")
    assert len([identity for identity, _, _ in calls if identity["role_id"] == "writer"]) == 1
    with kernel.db.uow(write=False) as repo:
        closure = repo.many("receipts", {"work_id": wr["work_id"], "command_type": "WORLD_COVERAGE_RECOVERY"})
        assert len(closure) == 1 and not closure[0]["payload"]["worldclosure_receipt"]["accepted_event_refs"]
        assert kernel.revision(repo, wr)["frontiers"] != kernel.revision(repo, child["world_ref"])["frontiers"]
        knowledge = board["nodes"]["knowledge:" + parent["target"][6:]]
        record = kernel.artifact(repo, knowledge["output_ref"], wr["work_id"])["payload"]
        assert record["world_ref"] == child["world_ref"] and record["draft_ref"] == writer["output_ref"]
    before = len(calls)
    for _ in range(3):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
    assert len(calls) == before


def test_coverage_child_cannot_commission_an_unbounded_grandchild(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, _, observed = coverage_fixture(kernel, monkeypatch, pending_child=True)
    board, _ = drive_coverage(kernel, gateway, owner, wr, ticks=160,
        until=lambda current: any(node.get("reason") == "COVERAGE_RETRY_EXHAUSTED" for node in current["nodes"].values()))
    assert len(observed) == 2 and observed[1]["query"]["evaluation_status"] == "PENDING"
    assert len([node for node in board["nodes"].values() if node["stage"] == "internal_verification"]) == 2
    before = len(calls)
    for _ in range(4):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
    assert len(calls) == before


def test_coverage_diagnosis_preserves_slot_and_still_enforces_current_extent(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, diagnoses, observed = coverage_fixture(kernel, monkeypatch, diagnosis_cycle=True)
    try:
        board, cursor = drive_coverage(kernel, gateway, owner, wr, ticks=200)
    except pytest.fail.Exception as error:
        error.add_note("Coverage diagnosis reads: " + json.dumps(observed))
        raise
    assert cursor["state"] == "FROZEN" and len(diagnoses) == 2
    assert len(observed) == 2
    writer = board["nodes"][observed[0]["case"]["target"]]
    assert len(writer["reassessments"]) == 2
    assert writer["revision_feedback"]["adopted_world_ref"] == observed[1]["case"]["world_ref"]
    assert writer["revision_feedback"]["findings"][-1]["actual_han"] == 2
    assert len([identity for identity, _, _ in calls if identity["role_id"] == "writer"]) == 6


def test_evaluated_unknown_waits_for_evidence_without_claiming_missing_registration(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, _, observed = coverage_fixture(kernel, monkeypatch,
        diagnosis_cycle=True, covered=True, unknown_query=True)
    board, _ = drive_coverage(kernel, gateway, owner, wr, ticks=160,
        until=lambda current: any(node["state"] == "WAITING_WORLD_EVIDENCE" for node in current["nodes"].values()))
    assert len(observed) == 1
    actual = observed[0]["query"]
    assert actual["evaluation_status"] == "EVALUATED" and actual["value_status"] == "UNRESOLVED"
    node = next(node for node in board["nodes"].values() if node["stage"] == "internal_verification")
    assert node["state"] == "WAITING_WORLD_EVIDENCE" and node["reason"] == "NEEDS_WORLD_EVIDENCE"
    before = len(calls)
    for _ in range(4):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
    assert len(calls) == before
    with kernel.db.uow(write=False) as repo:
        assert not repo.many("receipts", {"work_id": wr["work_id"], "command_type": "WORLD_COVERAGE_RECOVERY"})


def test_coverage_diagnosis_without_current_defect_does_not_rewrite_the_draft(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, diagnoses, observed = coverage_fixture(kernel, monkeypatch, editorial_diagnosis=True)
    try:
        board, cursor = drive_coverage(kernel, gateway, owner, wr, ticks=160)
    except pytest.fail.Exception as error:
        from novel_factory.application.production_pipeline import _ready
        error.add_note("Coverage program steps: " + json.dumps(gateway._coverage_fixture_steps[-8:]))
        error.add_note("Ready node: " + str(_ready(current_board(kernel, wr))))
        raise
    assert cursor["state"] == "FROZEN" and len(diagnoses) == 1
    assert len(observed) == 2 and all(entry["case"]["source_kind"] == "DIAGNOSIS" for entry in observed)
    writer = board["nodes"][observed[0]["case"]["target"]]
    assert writer["output_ref"] == observed[0]["case"]["source_draft_ref"]
    assert writer["revision_count"] == observed[0]["case"]["revision_count"]
    assert len(writer["reassessments"]) == 1
    assert writer["coverage_continuation"]["method_validation"] == "INTERNAL_QUERY_ONLY"
    assert len([identity for identity, _, _ in calls if identity["role_id"] == "writer"]) == writer["revision_count"] + 1


def test_wrong_authorization_time_is_corrected_before_any_coverage_acceptance(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, _, observed = coverage_fixture(kernel, monkeypatch, wrong_parent_time=True)
    board, cursor = drive_coverage(kernel, gateway, owner, wr)
    queries = [entry for entry in observed if "case" in entry]
    assert cursor["state"] == "FROZEN" and len(queries) == 3
    assert queries[0]["case"]["case_hash"] == queries[1]["case"]["case_hash"]
    assert queries[0]["query"]["story_time"] == {"tick": 1, "phase": 1}
    assert queries[1]["query"]["story_time"] == {"tick": 1, "phase": 0}
    parent = next(node for node in board["nodes"].values()
                  if node["stage"] == "internal_verification" and not node["internal_case"].get("parent_case_hash"))
    assert len(parent["evidence_repairs"]) == 1
    assert "current scene interval" in parent["evidence_repairs"][0]["diagnostic"]
    assert len([identity for identity, _, _ in calls if identity["role_id"] == "writer"]) == 1
    with kernel.db.uow(write=False) as repo:
        attempt = repo.one("call_attempts", {"attempt_id": parent["evidence_repairs"][0]["attempt_id"]})
        assert attempt["state"] == "UNKNOWN" and attempt["actual_amount"] is None
        assert len(repo.many("receipts", {"work_id": wr["work_id"], "command_type": "WORLD_COVERAGE_RECOVERY"})) == 1


def test_actual_current_author_protection_is_read_before_requesting_author_decision(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    gateway, owner, wr, calls, _, observed = coverage_fixture(kernel, monkeypatch,
        editorial_diagnosis=True, protected_author=True)
    _board, _ = drive_coverage(kernel, gateway, owner, wr,
        until=lambda current: any(node["stage"] == "internal_verification" and node["state"] == "VERIFICATION_HELD"
                                  and node.get("reason") == "NEEDS_AUTHOR_DECISION" for node in current["nodes"].values()))
    assert len([entry for entry in observed if "case" in entry]) == 1
    before = len(calls)
    for _ in range(3):
        RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
    assert len(calls) == before
    with kernel.db.uow(write=False) as repo:
        assert not repo.many("receipts", {"work_id": wr["work_id"], "command_type": "WORLD_COVERAGE_RECOVERY"})
