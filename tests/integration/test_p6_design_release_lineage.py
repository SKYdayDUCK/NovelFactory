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
    from_ending = scenario in {"structural_then_ending", "legacy_ending", "authorized_fix"}

    def respond(packet):
        if production:
            return _production_reply(packet)
        role = packet["context"]["role_id"]
        counts[role] += 1
        if role == "world_designer":
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

    def run(action):
        return cli.run_intake(action, kernel, gateway, tmp_path / "project", idea, allowance)

    if scenario == "legacy_ending":
        from novel_factory.application.runtime_tasks import RuntimeTaskService

        prepare = RuntimeTaskService.prepare

        def historical_ending_contract(service, *args, **kwargs):
            if kwargs.get("role_id") == "world_designer" and '"short_arc"' in kwargs["assignment"]:
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
    for _ in range(32):
        result = run("advance")
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
        if scenario == "legacy_ending":
            ending_audit = gateway._read_audit(repo, result["attempt_id"])["payload"]
            assert ending_audit["outcome"] == "REJECTED" and ending_audit["output_ref"] is None
            assert repo.one("tasks", {"task_id": result["task_id"]})["state"] == "FAILED"
            assert result["design_ending_plan"]["source"]["unresolved_questions"] == [
                "为何签收？",
                "签收了什么？",
            ]
        adopted = kernel.artifact(repo, result["design_ref"], wr["work_id"])["payload"]
        with pytest.raises(DomainError, match="lineage"):
            verify_adopted_design(kernel, repo, delivery, result, {**adopted, "title": "另一版本"})
        altered = deepcopy(result)
        altered["design_review"]["attempt_id"] = result["attempt_id"]
        with pytest.raises(DomainError, match="lineage"):
            verify_adopted_design(kernel, repo, delivery, altered, adopted)
        assert (
            counts["world_designer"]
            == {
                "normalized": 1,
                "null_annotation": 1,
                "revised": 2,
                "structural_then_ending": 3,
                "legacy_ending": 3,
                "authorized_fix": 4,
            }[scenario]
        )
        assert len(requests) == sum(counts.values())

    if scenario in {"revised", "authorized_fix"}:
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
