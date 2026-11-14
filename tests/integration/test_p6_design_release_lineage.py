"""Release verification uses completed, synthetic HTTP returns in an isolated PostgreSQL database."""

import importlib
import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from test_p6_postpaid_delivery import fake_transport

from agent_runtime import providers
from novel_factory.application import agent_gateway
from novel_factory.application.candidate_delivery import CandidateDelivery
from novel_factory.application.design_release_evidence import verify_adopted_design
from novel_factory.application.production_extent import _idea_evidence
from novel_factory.application.production_pipeline import production_material_reader
from novel_factory.application.supervisor import HostJournal
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.mark.parametrize(
    "scenario",
    [
        "normalized",
        "revised",
        "null_annotation",
        "structural_then_ending",
        "legacy_ending",
        "authorized_fix",
        "malformed_json",
        "quality_recovery",
        "quality_pause",
        "quality_no_wire",
        "malformed_exhausted",
        "duplicate_key",
        "syntax_then_duplicate",
        "structural_then_duplicate",
        "ordinary_after_ending",
        "historical_ordinary_patch",
    ],
)
def test_adopted_design_and_review_bound_to_release(kernel_env, tmp_path, monkeypatch, scenario):
    kernel = kernel_env["kernel"]
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    cli = importlib.import_module("p6_idea")
    counts = {"world_designer": 0, "reviewer": 0}
    design = {
        "title": "夹具双落款",
        "protagonist": "画师",
        "place": "画室",
        "storyline": "核对两份委托。",
        "chapter_goals": ["送信人核对落款，画师签收原件。"],
        "world_rules": ["委托须本人签收。"],
        "opening": {
            "counterpart": "送信人",
            "other_place": "档案室",
            "protagonist_knowledge": "画师见过原件。",
            "counterpart_knowledge": "送信人保管委托。",
            "situation": "两人就签收有分歧。",
            "environment": {"moving_entity": "邮车"},
        },
    }

    production = False
    after_ending = scenario in {"ordinary_after_ending", "historical_ordinary_patch"}
    from_ending = after_ending or scenario in {"structural_then_ending", "legacy_ending", "authorized_fix"}
    final_patch = {"storyline": "双方确认交接。", "chapter_goals": ["当场签收。"]}

    duplicate_case = scenario in {"duplicate_key", "syntax_then_duplicate", "structural_then_duplicate"}
    syntax_case = duplicate_case or scenario in {"malformed_json", "malformed_exhausted"}
    malformed_texts = []

    def respond(packet):
        if production:
            return _production_reply(packet)
        role = packet["context"]["role_id"]
        counts[role] += 1
        if role == "world_designer":
            if scenario.startswith("quality_"):
                return design
            if syntax_case:
                if counts[role] == 1 and scenario == "structural_then_duplicate":
                    return {k: v for k, v in design.items() if k != "opening"}
                if counts[role] == 2:
                    handed = json.loads(packet["assignment"])
                    if scenario == "structural_then_duplicate":
                        assert "opening" not in handed["candidate"] and "raw_response_text" not in handed
                    else:
                        assert handed["raw_response_text"] == malformed_texts[0] and "candidate" not in handed
                        assert handed["validation_errors"][0]["keyword"] == ("duplicateKey" if scenario == "duplicate_key" else "jsonSyntax")
                return design
            if from_ending and counts[role] == 1:
                return {k: v for k, v in design.items() if k != "opening"}
            if scenario == "null_annotation":
                return {**design, "chapter_goals_note": None}
            if counts[role] == 1:
                return {**design, "unresolved_questions": []}
            if scenario == "authorized_fix" and counts[role] == 4:
                ids = json.loads(packet["assignment"])["editor_decision"]["issue_ids"]
                return {
                    "chapter_changes": [
                        {"chapter": 1, "replacement": "核对后由本人签收。", "issue_ids": ids}
                    ],
                    "opening_changes": [],
                }
            if scenario == "legacy_ending" and counts[role] == 3:
                return {
                    **{k: v for k, v in design.items() if k != "opening"},
                    "chapter_goals": ["核对。", "签收。"],
                }
            if after_ending and counts[role] == 4:
                handed = json.loads(packet["assignment"])
                assert handed["candidate"]["storyline"] == "由画师签收。"
                return final_patch if scenario == "historical_ordinary_patch" else {**handed["candidate"], **final_patch}
            if from_ending and counts[role] == 3:
                return {"storyline": "由画师签收。", "chapter_goals": ["核对。", "签收。"]}
            return {**design, "storyline": "画师核对两份委托，向送信人签收原件。"}
        ref = json.loads(packet["assignment"])["candidate_ref"]
        issue = {
            "issue_id": str(uuid4()),
            "category": "CONTINUITY",
            "severity": "SUGGESTION",
            "subject_ref": ref,
            "description": "一处措辞可简化",
            "evidence_refs": [],
            "suggested_change": "润饰文字",
            "disposition": "OPEN",
            "issue_kind": "SEMANTIC_CONCERN",
        }
        if scenario == "revised" and counts[role] == 1:
            issue.update(issue_kind="CHANGE_REQUEST")
        arc = {
            "beginning": "收到信件",
            "development": "出现冲突",
            "turning_point": "核对落款",
            "ending": "完成签收",
            "decision": "COMPLETE",
        }
        if from_ending and counts[role] == 1:
            arc.update(decision="ENDING_PLAN_REQUIRED", ending="需交代最终签收。")
        if after_ending and counts[role] == 2:
            issue.update(severity="QUESTION", issue_kind="SEMANTIC_CONCERN")
        if scenario == "authorized_fix" and counts[role] == 2:
            issue.update(severity="BLOCKER", issue_kind="CHANGE_REQUEST")
        extra = {}
        source = json.loads(packet["assignment"]).get("ending_plan_source")
        if source and source.get("issue_ids"):
            extra["issue_resolutions"] = [
                {"issue_id": i, "decision": "RESOLVED", "reason": "已在终章现场核对。"}
                for i in source["issue_ids"]
            ]
        if source:
            extra["ending_plan_review"] = {
                "candidate_ref": ref,
                "opening_and_chapters_confirmed": True,
                "answers": [
                    {"question": q, "answer": "确认由画师当面签收。"}
                    for q in source["unresolved_questions"]
                ],
            }
        return {
            "summary": "开场与章末核对完成。",
            **extra,
            "issues": [issue],
            "counterinterpretations": [],
            "evidence_refs": [],
            "short_arc": arc,
        }

    def questions(packet, candidate):
        if (
            from_ending
            and packet["context"]["role_id"] == "world_designer"
            and counts["world_designer"] == 1
        ):
            return ["尚需设计明确开场地点。"]
        if (
            from_ending
            and packet["context"]["role_id"] == "world_designer"
            and counts["world_designer"] == 3
        ):
            return ["为何签收？", "签收了什么？"]
        return (
            ["结局如何回应委托？"]
            if candidate.get("short_arc", {}).get("decision") == "ENDING_PLAN_REQUIRED"
            else []
        )

    def original_identity(packet):
        return (
            from_ending
            and packet["context"]["role_id"] == "world_designer"
            and counts["world_designer"] == 1
        )

    adapter, requests = fake_transport(
        kernel,
        monkeypatch,
        "defer",
        respond,
        questions=questions,
        nested_identity=original_identity,
    )
    # Synthetic protocol tests must not depend on installed provider catalogs or
    # operator authorization; the HTTP boundary above is fully replaced.
    from agent_runtime import native_codex
    monkeypatch.setattr(providers.Profile, "preflight", lambda _: None)
    forbidden = lambda *args, **kwargs: pytest.fail("Synthetic lineage test reached a native model boundary")
    monkeypatch.setattr(native_codex, "generate", forbidden)
    monkeypatch.setattr(native_codex, "start_host", forbidden)
    if scenario.startswith("quality_"):
        fixture_post = providers.post
        def contract_failure(**kwargs):
            raw, receipt, window = fixture_post(**kwargs)
            supplied = json.loads(raw)
            if counts["reviewer"] == 0:
                choice = supplied["choices"][0]
                value = json.loads(choice["message"]["content"])
                number = counts["world_designer"]
                if scenario == "quality_pause" or number <= 2:
                    del value["payload"]["title"]
                elif number == 3:
                    value["world_ref"]["work_id"] = str(uuid4())
                elif number == 4:
                    choice["finish_reason"] = "length"
                elif number == 5:
                    value["payload"]["world_rules"] = "incorrect type"
                choice["message"]["content"] = json.dumps(value)
            body = json.dumps(supplied).encode()
            if scenario == "quality_recovery":
                # Exercise the independent on-disk worker evidence fence with synthetic bytes.
                # No physical network invocation or real model is claimed by this test.
                from agent_runtime._http_worker import _persist
                _persist(kwargs, 200, body, True, receipt, window)
            if scenario == "quality_no_wire" and counts["reviewer"] == 1:
                v = json.loads(supplied["choices"][0]["message"]["content"])
                del v["payload"]["summary"]
                supplied["choices"][0]["message"]["content"] = json.dumps(v)
                body = json.dumps(supplied).encode()
            return body, receipt, window
        monkeypatch.setattr(providers, "post", contract_failure)
    if syntax_case:
        fixture_post = providers.post
        def syntax_failure(**kwargs):
            payload, receipt, window = fixture_post(**kwargs)
            designer_no = counts["world_designer"]
            duplicate_reply = scenario == "duplicate_key" and designer_no == 1 or (
                scenario in {"syntax_then_duplicate", "structural_then_duplicate"} and designer_no == 2)
            comma_reply = scenario != "structural_then_duplicate" and (designer_no == 1 or scenario == "malformed_exhausted")
            if counts["reviewer"] == 0 and (comma_reply or duplicate_reply):
                supplied = json.loads(payload)
                message = supplied["choices"][0]["message"]
                if duplicate_reply:
                    # Contradictory nested values must stay in the original response as-is.
                    raw = message["content"].replace('"title":', '"title":"first fixture value","title":', 1)
                    assert raw != message["content"] and raw.count('"title"') == 2
                    message["content"] = raw
                else:
                    message["content"] = message["content"].replace(', "generation":', ' "generation":', 1)
                    with pytest.raises(json.JSONDecodeError):
                        json.loads(message["content"])
                malformed_texts.append(message["content"])
                payload = json.dumps(supplied).encode()
            return payload, receipt, window
        monkeypatch.setattr(providers, "post", syntax_failure)
    review = RuntimeModelAdapter(
        providers.Profile(
            kind="deepseek",
            model="deepseek-v4-pro",
            max_output_tokens=2048,
            secret_ref="NF_TEST_SYNTHETIC_KEY",
            defer_invoice=True,
        ),
        HostJournal(kernel),
    )
    gateway = agent_gateway.AgentGateway(
        kernel,
        {"flash": adapter, "pro": review},
        material_reader=production_material_reader(kernel),
    )
    idea = {
        "idea": "画师收到两份原件。",
        "length": {"preset": "short", "unit": "chapters", "min": 1, "max": 1},
        "genre": "现实",
    }
    allowance = {
        "adapter_id": "flash",
        "review_adapter_id": "pro",
        "reserve_amount": "0.1",
        "total_amount": "5",
        "currency": "USD",
        "total_tokens": 2000000,
        "token_reservation": 100000,
    }

    if scenario.startswith("quality_"):
        allowance["production_mode"] = "quality"

    def run(action):
        return cli.run_intake(action, kernel, gateway, tmp_path / "project", idea, allowance)

    if scenario in {"legacy_ending", "historical_ordinary_patch"}:
        from novel_factory.application.runtime_tasks import RuntimeTaskService

        prepare = RuntimeTaskService.prepare

        def historical_ending_contract(service, *args, **kwargs):
            if (scenario == "historical_ordinary_patch" and kwargs.get("role_id") == "world_designer"
                    and counts["reviewer"] == 2):
                service.gateway.output_contracts[("world_designer", "initial_world")] = ("ShortEndingPlan", "production_profile")
            if scenario == "legacy_ending" and kwargs.get("role_id") == "world_designer" and '"short_arc"' in kwargs["assignment"]:
                service.gateway.output_contracts[("world_designer", "initial_world")] = (
                    "OpeningWorldDesign",
                    "production_profile",
                )
            return prepare(service, *args, **kwargs)

        monkeypatch.setattr(RuntimeTaskService, "prepare", historical_ending_contract)

    enrolled = run("enroll")
    with monkeypatch.context() as strict_gateway:
        strict_gateway.setattr(
            agent_gateway,
            "validated_payload",
            lambda envelope, name, version: validate_contract(name, envelope["payload"], version),
        )
        for _ in range(4):
            run("advance")
            if requests:
                break
    if syntax_case:
        access = cli._load_private(tmp_path / "project" / "preparation.json")
        owner = kernel.authenticate(access["token"])
        repair = run("advance")
        assert len(requests) == 1
        if scenario == "structural_then_duplicate":
            assert "revision_purpose" not in repair and repair["status"] == "DESIGN_REVISION_ENROLLED"
        else:
            assert repair["revision_purpose"] == "JSON_SYNTAX_CORRECTION"
        with kernel.objects.lock(), kernel.db.uow() as repo:
            original = repo.one("tasks", {"task_id": enrolled["task_id"]})
            assert original["state"] == "FAILED"
            assert repo.one("call_attempts", {"task_id": original["task_id"]})["state"] == "UNKNOWN"
            raw = kernel.artifact(repo, repair["candidate_ref"], original["work_id"], kind="AGENT_RESPONSE")
            if scenario != "structural_then_duplicate":
                assert raw["payload"]["text"] == malformed_texts[0]
            assert kernel._prior(repo, owner, original["work_id"], "P6_IDEA_REVISE", "1", {}) == repair
        assert run("status")["task_id"] == repair["task_id"] != enrolled["task_id"] and len(requests) == 1
    for _ in range(32):
        result = run("advance")
        if scenario in {"malformed_exhausted", "syntax_then_duplicate", "structural_then_duplicate"} and result.get("status") == "DESIGN_SCHEMA_REWORK_REQUIRED":
            assert result["reason"] == "JSON_SYNTAX_REPAIR_EXHAUSTED"
            assert result["validation_errors"][0]["keyword"] == ("duplicateKey" if duplicate_case else "jsonSyntax")
            assert run("advance") == result and len(requests) == 2 and counts["reviewer"] == 0
            with kernel.db.uow(write=False) as repo:
                work = enrolled["world_ref"]["work_id"]
                assert enrolled["profile"]["max_reworks"] == 1
                assert len(repo.many("receipts", {"work_id": work, "command_type": "P6_IDEA_REVISE"})) == 1
                assert not repo.many("artifacts", {"work_id": work, "kind": "AGENT_CANDIDATE"})
                assert repo.one("tasks", {"task_id": repair["task_id"]})["state"] == "FAILED"
                if duplicate_case:
                    rejected = kernel.artifact(repo, result["response_ref"], work, kind="AGENT_RESPONSE")["payload"]["text"]
                    assert rejected == malformed_texts[-1] and rejected.count('"title"') == 2
            return
        if scenario == "quality_pause" and result.get("reason") == "STRATEGY_REASSESSMENT_REQUIRED":
            assert result["repair_plan"]["occurrences"] == 3 and len(requests) == 3
            assert result == run("advance")
            with kernel.db.uow(write=False) as repo:
                assert not repo.one("artifacts", {"work_id": enrolled["world_ref"]["work_id"], "kind": "P6_EXPERIENCE_MEMORY"})
            return
        if result.get("reason") == "ENDING_PLAN_BLOCKERS" and scenario == "authorized_fix":
            from novel_factory.application.idea_ending import authorize_fix

            access = cli._load_private(tmp_path / "project" / "preparation.json")
            owner = kernel.authenticate(access["token"])
            permit = authorize_fix(
                kernel, gateway, owner, access["world_ref"], result["review_attempt"]
            )
            assert (
                authorize_fix(kernel, gateway, owner, access["world_ref"], result["review_attempt"])
                == permit
            )
        if result.get("status") == "INITIAL_PLAN_MATERIALIZED":
            break
    assert result["status"] == "INITIAL_PLAN_MATERIALIZED", result
    if syntax_case:
        assert result == run("advance") and len(requests) == 3
    wr = result["world_ref"]
    delivery = CandidateDelivery(kernel, gateway)
    with kernel.db.uow(write=False) as repo:
        assert (
            _idea_evidence(kernel, repo, wr, result["length_ref"], idea["length"], delivery)
            == "NATIVE_IDEA"
        )
        first = repo.one("tasks", {"task_id": enrolled["task_id"]})
        call = repo.one("call_attempts", {"task_id": first["task_id"]})
        audit = gateway._read_audit(repo, call["attempt_id"])["payload"]
        assert (
            first["state"] == "FAILED"
            and call["state"] == "UNKNOWN"
            and call["actual_amount"] is None
        )
        assert audit["outcome"] == "REJECTED" and audit["output_ref"] is None
        if syntax_case:
            assert audit["reason"] == "INVALID_MODEL_OUTPUT" and audit["response_ref"] == repair["candidate_ref"]
            assert repo.one("tasks", {"task_id": repair["task_id"]})["state"] == "ACCEPTED"
            assert len(repo.many("call_attempts", {"task_id": enrolled["task_id"]})) == 1
        if scenario == "legacy_ending":
            ending_audit = gateway._read_audit(repo, result["attempt_id"])["payload"]
            assert ending_audit["outcome"] == "REJECTED" and ending_audit["output_ref"] is None
            assert repo.one("tasks", {"task_id": result["task_id"]})["state"] == "FAILED"
            assert result["design_ending_plan"]["source"]["unresolved_questions"] == [
                "为何签收？",
                "签收了什么？",
            ]
        adopted = kernel.artifact(repo, result["design_ref"], wr["work_id"])["payload"]
        if after_ending:
            assert adopted == {**design, **final_patch}
            bound = kernel.artifact(repo, result["source_input_ref"], enrolled["world_ref"]["work_id"])["payload"]
            writer_task = repo.one("tasks", {"task_id": result["task_id"]})
            assert writer_task["state"] == "ACCEPTED" and len(repo.many("call_attempts", {"task_id": writer_task["task_id"]})) == 1
            proof = result.get("design_revision_patch")
            if scenario == "historical_ordinary_patch":
                assert bound["output_contract"]["name"] == "ShortEndingPlan"
                assert proof["output_ref"] == result["result_ref"] != proof["patch_ref"]
                assert kernel.artifact(repo, proof["patch_ref"], writer_task["work_id"])["payload"] == final_patch
                packet = json.loads(bound["assignment"])
                assert proof["prior_design_ref"] == packet["candidate_ref"] and proof["review_ref"] == packet["review_ref"]
                edited = deepcopy(result)
                edited["design_revision_patch"]["patch_ref"] = edited["result_ref"]
                with pytest.raises(DomainError, match="lineage"):
                    verify_adopted_design(kernel, repo, delivery, edited, adopted)
            else:
                expected = "EnvironmentalOpeningWorldDesign" if enrolled["profile"]["environment_processes"] else "OpeningWorldDesign"
                assert bound["output_contract"]["name"] == expected and proof is None
        with pytest.raises(DomainError, match="lineage"):
            verify_adopted_design(kernel, repo, delivery, result, {**adopted, "title": "另一版本"})
        altered = deepcopy(result)
        altered["design_review"]["attempt_id"] = result["attempt_id"]
        with pytest.raises(DomainError, match="lineage"):
            verify_adopted_design(kernel, repo, delivery, altered, adopted)
        assert (
            counts["world_designer"]
            == {
                "malformed_json": 2,
                "duplicate_key": 2,
                "quality_recovery": 6,
                "quality_no_wire": 6,
                "normalized": 1,
                "null_annotation": 1,
                "revised": 2,
                "structural_then_ending": 3,
                "legacy_ending": 3,
                "authorized_fix": 4,
                "ordinary_after_ending": 4,
                "historical_ordinary_patch": 4,
            }[scenario]
        )
        assert len(requests) == sum(counts.values())

    if scenario == "quality_no_wire":
        assert counts["reviewer"] == 2
        with kernel.db.uow(write=False) as repo:
            assert not repo.one("artifacts", {"work_id": enrolled["world_ref"]["work_id"], "kind": "P6_EXPERIENCE_MEMORY"})
    if scenario == "quality_recovery":
        from novel_factory.application.quality_memory import recall, revoke_experience, transfer_verified_method
        prep = enrolled["world_ref"]
        with kernel.db.uow(write=False) as repo:
            mem = repo.one("artifacts", {"work_id": prep["work_id"], "kind": "P6_EXPERIENCE_MEMORY"})
            assert mem["payload"]["fix_attempt_id"] == result["attempt_id"]
            scope = mem["payload"]["scope"]
            frozen = recall(kernel, repo, prep, scope)
            assert frozen and recall(kernel, repo, wr, scope) is None
            assert recall(kernel, repo, prep, {**scope, "role_id": "writer"}) is None
            rounds = sorted((r["payload"] for r in repo.many("receipts", {"work_id": prep["work_id"], "command_type": "P6_IDEA_REVISE"})), key=lambda r: r["round"])
            assert rounds[1]["repair_plan"]["decision"] == "CHANGED_STRATEGY"
            assert [r["validation_errors"][0]["keyword"] for r in rounds] == ["required", "required", "identityMismatch", "truncated", "type"]
            assert len(repo.many("call_attempts", {"task_id": enrolled["task_id"]})) == 1
        access = cli._load_private(tmp_path / "project" / "preparation.json")
        prep_owner = kernel.authenticate(access["token"])
        literary = cli._load_private(tmp_path / "project" / "supervisor.json")
        literary_owner = kernel.authenticate(literary["token"])
        transferred = transfer_verified_method(kernel, prep_owner, prep, frozen["experience_ref"], literary_owner, wr)
        assert transfer_verified_method(kernel, prep_owner, prep, frozen["experience_ref"], literary_owner, wr) == transferred
        with kernel.db.uow(write=False) as repo:
            exported = kernel.artifact(repo, transferred, wr["work_id"])["payload"]
            assert exported["method"] == frozen["method"] and "actual_fix" not in exported and "error_ref" not in exported
        revoke_experience(kernel, prep_owner, prep, frozen["experience_ref"])
        with kernel.db.uow(write=False) as repo:
            assert recall(kernel, repo, prep, scope) is None
            assert kernel.artifact(repo, frozen["experience_ref"], prep["work_id"])["payload"]["status"] == "VERIFIED"
    if scenario in {"revised", "authorized_fix", "historical_ordinary_patch", "malformed_json"}:
        production = True
        run("register-window")
        access = cli._load_private(tmp_path / "project" / "supervisor.json")
        _finish_edition(
            kernel,
            kernel.authenticate(access["token"]),
            access["work_id"],
            access["branch_id"],
            gateway,
        )


def _production_reply(packet):
    from test_p6_rolling_production import answer

    role = packet["context"]["role_id"]
    if role == "character_actor":
        return {
            **packet["proposal_protocol"]["copy_into_payload"],
            "intent": "循原件签收。",
            "targets": [],
            "resource_requests": [],
            "preconditions": [],
            "evidence_refs": [],
        }
    if role == "adjudication_advisor":
        protocol = packet["proposal_protocol"]
        return {
            **protocol["copy_into_payload"],
            "effects": [protocol["authorized_effect_choices"][0]],
            "reasoning_summary": "当面签收。",
            "unresolved_questions": [],
        }
    if packet.get("editorial", {}).get("operation") == "request_change":
        return {
            "text": "原计划仍可实行",
            "important_deltas": [],
            "event_refs": packet["editorial"]["accepted_event_refs"],
            "evidence_refs": [],
        }
    if role == "evidence_maintainer":
        doc = json.loads(packet["authorized_materials"][0]["content"])
        base = json.loads(answer(packet)["text"])["assertions"][0]
        claims = [
            {**base, "classification": "EXISTING_EVENT_PRESENTATION", "event_ref": ref}
            for ref in doc["required_event_refs"]
        ]
        return {**answer(packet), "text": json.dumps({"assertions": claims or [base]})}
    return answer(packet)


def _finish_edition(kernel, owner, work, branch, gateway):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from editorial_helpers import approve_binding
    from test_p6_external_review import assessment

    from novel_factory.application import book_review, external_book_review, frozen_editing
    from novel_factory.application.idea_intake import _uid
    from novel_factory.application.release import ReleaseService
    from novel_factory.application.rolling_production import rolling_status
    from novel_factory.application.rolling_publication import final_review_request
    from novel_factory.application.supervisor import RuntimeSupervisor

    scope = (kernel, owner, work, branch)
    supervisor = RuntimeSupervisor(kernel, gateway)
    events = []
    state = None
    for _ in range(60):
        step = supervisor.run_once(owner, work, branch)
        events.append((step["status"], step.get("reason")))
        if state is None and step["status"] != "OPENING_ACCEPTED":
            continue
        state = rolling_status(*scope)
        if state and state.get("state") == "FROZEN":
            break
    assert state and state.get("frozen_chapters") == 1, events
    with pytest.raises(DomainError) as no_approval:
        final_review_request(*scope)
    assert no_approval.value.code == "RELEASE_INCOMPLETE"
    frozen_editing.start(*scope)
    with kernel.db.uow(write=False) as repo:
        binding = frozen_editing._state(kernel, repo, work, branch)["binding"]
    approve_binding(
        kernel, {"principal": owner, "ids": {"sword": _uid(work, "protagonist")}}, binding
    )
    release_request = final_review_request(*scope)
    keys = [ec.generate_private_key(ec.SECP256R1()) for _ in range(2)]
    judges = [
        {
            "session_id": f"fixture-native-release-{n}",
            "public_key": key.public_key()
            .public_bytes(serialization.Encoding.X962, serialization.PublicFormat.CompressedPoint)
            .hex(),
        }
        for n, key in enumerate(keys)
    ]
    book_review.start_review(
        kernel,
        gateway,
        owner,
        release_request=release_request,
        external_judges=judges,
        deadline_seconds=3600,
        idempotency_key="fixture-final-review",
    )
    for n, key in enumerate(keys, 1):
        pkg = external_book_review.export_assignment(*scope, track=n)
        body = assessment(pkg)
        signed = external_book_review.signed_bytes(pkg["binding"], **body)
        external_book_review.import_audit(
            *scope,
            {
                **body,
                "binding": pkg["binding"],
                "signature": key.sign(signed, ec.ECDSA(hashes.SHA256())).hex(),
            },
        )
    released = ReleaseService(kernel).freeze(owner, release_request, "fixture-final-release")
    assert (
        ReleaseService(kernel).get_release(owner, work, released["release_ref"])["record"][
            "volume_id"
        ]
        == release_request["volume_id"]
    )
    assert (
        book_review.export_reviewed_book(*scope, released["release_ref"])["result"]["status"]
        == "QUALITY_TARGET_MET"
    )
