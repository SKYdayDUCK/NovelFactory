"""Real PG17 native windows; model output is a local deterministic engineering fixture."""

import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from uuid import uuid4

import pytest
from editorial_helpers import approve_binding, binding_for
from test_continuity import entry, qualify, reviewer
from test_continuity import request as continuity_request
from test_p5_supervisor import backend
from test_release import accept_draft, release_request, setup_batch, write_scene

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.continuity import ContinuityService
from novel_factory.application.production_pipeline import (
    pipeline_status,
    production_material_reader,
)
from novel_factory.application.release import ReleaseService
from novel_factory.application.rolling_production import (
    advance_rolling,
    register_rolling,
    retry_rolling,
    rolling_status,
)
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import point

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def uid():
    return str(uuid4())


def request(kernel, f, size, preset="short"):
    work = f["world_ref"]["work_id"]
    return {
        "world_ref": f["world_ref"],
        "series_id": uid(),
        "profile": {"preset": preset, "genre": "灵异", "overrides": {"window_chapters": size}},
        "volumes": [
            {
                "volume_id": f["volume_id"],
                "chapters": [
                    {"chapter_id": uid(), "scenes": [{"scene_ref": scene, "handoff_refs": []}]}
                    for scene in f["scene_refs"]
                ],
            }
        ],
        "subject_ids": [f["ids"]["protagonist"], f["ids"]["sword"]],
        "story_time": point(12),
        "predicates": ["possessed_by"],
        "routes": dict.fromkeys(("draft", "review", "plan"), "fixture"),
        "budget_id": kernel.tasks.create_budget(work, "8"),
        "reserve_amount": "0.10",
        "token_reservation": 100000,
        "deadline_seconds": 3600,
        "idempotency_key": uid(),
    }


def connect(kernel, adapter):
    return AgentGateway(
        kernel, {"fixture": adapter}, material_reader=production_material_reader(kernel)
    )


def answer(packet):
    if packet["context"]["role_id"] == "writer":
        return {
            "text": "北窗的雨声很轻。",
            "event_refs": [],
            "important_deltas": [],
            "evidence_refs": [],
        }
    if packet["context"]["role_id"] == "evidence_maintainer":
        read = json.loads(packet["authorized_materials"][0]["content"])
        assertion = {
            "quote": read["text"],
            "classification": "SURFACE_DETAIL",
            "summary": "雨声",
            "event_ref": None,
            "world_check": None,
            "knowledge_use": None,
            "proposed_command": None,
            "disclosure_id": None,
            "declared_delta_index": None,
        }
        return {
            "text": json.dumps({"assertions": [assertion]}),
            "event_refs": [],
            "important_deltas": [],
            "evidence_refs": [],
        }
    return {
        "summary": "工程测试已阅读。",
        "issues": [],
        "counterinterpretations": [],
        "evidence_refs": [],
    }


@pytest.mark.parametrize("size", [1, 3])
def test_fresh_dependent_scenes_wait_for_acceptance_and_recover_to_release(kernel_env, size):
    kernel = kernel_env["kernel"]
    f = setup_batch(kernel, count=3, dependent=True)
    owner, wr = f["principal"], f["world_ref"]
    work, branch = wr["work_id"], wr["branch_id"]
    req = request(kernel, f, size)
    from novel_factory.application.production_extent import bind_extent

    with kernel.objects.lock(), kernel.db.uow() as repo:
        bind_extent(kernel, repo, owner, wr,
                    {"preset": "short", "unit": "han_characters", "min": 17, "max": 23})
    with backend() as (adapter, calls, server):
        server.packet_candidate = answer
        # This specific isolated backend charges exactly zero; production HTTP usage alone
        # gives no financial receipt. Unknown accounting is tested separately below.
        generate = adapter.generate
        adapter.generate = lambda packet: replace(
            generate(packet), actual_amount=Decimal(0), currency="USD"
        )

        gateway = connect(kernel, adapter)
        receipt = register_rolling(kernel, gateway, owner, **req)
        assert register_rolling(kernel, gateway, owner, **req) == receipt and not calls
        changed = deepcopy(req)
        changed["profile"]["genre"] = "悬疑"
        with pytest.raises(DomainError) as mismatch:
            register_rolling(kernel, gateway, owner, **changed)
        assert mismatch.value.code == "IDEMPOTENCY_MISMATCH"
        accepted = {}
        for _ in range(80):
            RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)
            board = pipeline_status(kernel, owner, work, branch, include_outputs=True)
            for key, node in board["nodes"].items():
                if node["stage"] == "facts" and node["state"] == "FACTS_ACCEPTED":
                    draft_key = node["parents"][0]
                    accepted[draft_key] = {**board["outputs"][key], **board["outputs"][draft_key]}
            cursor = rolling_status(kernel, owner, work, branch)
            if cursor["state"] == "FROZEN" and cursor["frozen_chapters"] == 3:
                break
        assert cursor["frozen_chapters"] == 3 and len(calls) == 12, (
            cursor["state"],
            cursor["reason"],
            [(n["state"], n.get("reason")) for n in board["nodes"].values()],
        )
        assert cursor["index"] == (2 if size == 1 else 0)
        writer_packets = [json.loads(call["messages"][1]["content"]) for call in calls]
        lengths = [p["assignment"] for p in writer_packets if p["context"]["role_id"] == "writer"]
        assert len(lengths) == 3
        cards = [json.loads(p["authorized_materials"][0]["content"]) for p in writer_packets
                 if p["context"]["role_id"] == "writer"]
        assert all("predecessor_passages" not in card for card in cards[:2])
        passages = cards[2]["predecessor_passages"]
        assert len(passages) == 2
        assert len({p["acceptance_ref"]["artifact_revision_id"] for p in passages}) == 2
        for passage in passages:
            assert passage["text"] == "北窗的雨声很轻。"
            assert passage["end"] - passage["start"] == len(passage["text"])
        assert all("6–8 个汉字" in assignment for assignment in lengths[:2])
        assert "5–7 个汉字" in lengths[2]
        for packet in writer_packets:
            role_id = packet["context"]["role_id"]
            if role_id in ("reviewer", "evidence_maintainer"):
                for material in packet["authorized_materials"]:
                    delivery = json.loads(material["content"])
                    if role_id == "evidence_maintainer":
                        assert delivery["required_event_refs"] == delivery["declared_important_deltas"] == []
                    else:
                        assert "required_event_refs" not in delivery and "declared_important_deltas" not in delivery
        advance_rolling(kernel, gateway, owner, work, branch)
        assert rolling_status(kernel, owner, work, branch)["reason"] == "RELEASE_INCOMPLETE"
        # Same-draft retries remain legal. Replacing a frozen scene with other approved prose fails.
        first = f["scenes"][0]["scene_contract"]["scene_id"]
        with pytest.raises(DomainError) as frozen:
            accept_draft(
                kernel,
                f,
                write_scene(
                    kernel,
                    f,
                    0,
                    "另外的工程样文。",
                    prior_draft_ref=accepted["draft:" + first]["draft_ref"],
                ),
            )
        assert frozen.value.code == "SERIAL_PREFIX_PROTECTED"
        ordered = [
            accepted["draft:" + scene["scene_contract"]["scene_id"]] for scene in f["scenes"]
        ]
        binding = binding_for(f, ordered)
        proof = approve_binding(kernel, f, binding)
        with pytest.raises(DomainError) as no_blind_scores:
            ReleaseService(kernel).freeze(
                owner, release_request(f, binding, proof["approval_ref"]), uid()
            )
        assert no_blind_scores.value.code == "INDEPENDENT_REVIEW_REQUIRED"
        cursor = rolling_status(kernel, owner, work, branch)
        assert cursor["state"] == "FROZEN" and cursor["release_refs"] == []
        assert advance_rolling(kernel, gateway, owner, work, branch) is None and len(calls) == 12


def test_unknown_attempt_cannot_retry_or_manufacture_an_acceptance(kernel_env):
    kernel = kernel_env["kernel"]
    f = setup_batch(kernel, count=1)
    wr, owner = f["world_ref"], f["principal"]
    req = request(kernel, f, 1)
    with backend() as (adapter, calls, server):
        server.lose_response = True
        gateway = connect(kernel, adapter)
        result = register_rolling(kernel, gateway, owner, **req)
        for _ in range(10):
            if (
                RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])[
                    "status"
                ]
                == "IDLE"
            ):
                break
        board = pipeline_status(kernel, owner, wr["work_id"], wr["branch_id"])
        node_id = "draft:" + f["scenes"][0]["scene_contract"]["scene_id"]
        assert board["nodes"][node_id]["state"] == "STOPPED"
        with pytest.raises(DomainError) as unsafe:
            retry_rolling(
                kernel,
                owner,
                world_ref=wr,
                run_id=result["run_id"],
                window_index=0,
                node_id=node_id,
                idempotency_key=uid(),
            )
        assert unsafe.value.code == "CALL_RECONCILIATION_REQUIRED" and len(calls) == 1
        assert rolling_status(kernel, owner, wr["work_id"], wr["branch_id"])["frozen_chapters"] == 0


def test_release_and_continuity_gate_two_volumes_across_restart(kernel_env):
    kernel = kernel_env["kernel"]
    first = setup_batch(kernel, count=1)
    second = setup_batch(kernel, count=1, fixture=first)
    owner, wr = first["principal"], first["world_ref"]
    work, branch = wr["work_id"], wr["branch_id"]
    req = request(kernel, first, 1, preset="medium_long")
    req["volumes"].append(
        {
            "volume_id": second["volume_id"],
            "chapters": [
                {
                    "chapter_id": uid(),
                    "scenes": [{"scene_ref": second["scene_refs"][0], "handoff_refs": []}],
                }
            ],
        }
    )
    with backend() as (adapter, calls, server):
        server.packet_candidate = answer
        generate = adapter.generate
        adapter.generate = lambda packet: replace(
            generate(packet), actual_amount=Decimal(0), currency="USD"
        )
        gateway = connect(kernel, adapter)
        register_rolling(kernel, gateway, owner, **req)
        released = None
        for volume_no, f in enumerate((first, second)):
            draft = None
            for _ in range(35):
                RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)
                info = pipeline_status(kernel, owner, work, branch, include_outputs=True)
                if info["volume_id"] != f["volume_id"]:
                    continue
                key = "draft:" + f["scenes"][0]["scene_contract"]["scene_id"]
                fact_key = "facts:" + f["scenes"][0]["scene_contract"]["scene_id"]
                if fact_key in info["outputs"]:
                    draft = {**info["outputs"][fact_key], **info["outputs"][key]}
                state = rolling_status(kernel, owner, work, branch)
                if state["frozen_chapters"] == volume_no + 1:
                    break
            assert state["frozen_chapters"] == volume_no + 1, (
                state["state"],
                state["reason"],
                info,
            )
            from novel_factory.application.rolling_publication import final_review_request

            with pytest.raises(DomainError) as missing_final:
                final_review_request(kernel, owner, work, branch)
            assert missing_final.value.code == "RELEASE_INCOMPLETE"
            if volume_no == 1:
                assert state["release_refs"] == [released["release_ref"]]
                final_approval = approve_binding(kernel, f, binding_for(f, [draft]))
                final = final_review_request(kernel, owner, work, branch)
                assert final["editorial_approval_ref"] == final_approval["approval_ref"]
                assert final["volume_id"] == second["volume_id"]
                assert final["ordered_acceptance_refs"] == [draft["acceptance_ref"]]
                with pytest.raises(DomainError):
                    ReleaseService(kernel).freeze(owner, final, "no-final-blind-scores")
                break
            assert state["final_volume_id"] == second["volume_id"] and state["state"] == "FROZEN"
            assert len(calls) == 4
            binding = binding_for(f, [draft])
            from novel_factory.application.editorial import ReviewService

            missing = advance_rolling(kernel, gateway, owner, work, branch)
            assert (missing or rolling_status(kernel, owner, work, branch))[
                "reason"
            ] == "RELEASE_INCOMPLETE"
            proof = approve_binding(kernel, f, binding)
            editorial = ReviewService(kernel)
            editorial.revoke_editorial(owner, wr, proof["approval_ref"], "测试已撤回的审核", uid())
            blocked = advance_rolling(kernel, gateway, owner, work, branch)
            assert blocked["reason"] == "EDITORIAL_APPROVAL_REVOKED"
            with kernel.db.uow(write=False) as repo:
                assert not repo.one(
                    "selections", {"work_id": work, "selection_kind": "RELEASE:" + f["volume_id"]}
                )
            approval = editorial.approve_editorial(
                owner, {**proof["approval_request"], "idempotency_key": uid()}
            )
            auto = RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)
            assert auto["status"] == "ROLLING_VOLUME_RELEASED", auto
            released = ReleaseService(kernel).get_release(owner, work, auto["release_ref"])
            assert released["record"]["editorial_approval_ref"] == approval
            next_step = advance_rolling(kernel, gateway, owner, work, branch)
            assert next_step["reason"] == "CONTINUITY_REVALIDATION_REQUIRED"
            service = ContinuityService(kernel)
            independent = reviewer(kernel, f)
            empty = continuity_request(f, [released], [], series_id=req["series_id"])
            qualify(service, owner, independent, work, empty)
            advance_rolling(kernel, gateway, owner, work, branch)
            with kernel.db.uow(write=False) as repo:
                assert not repo.one(
                    "selections",
                    {"work_id": work, "selection_kind": "CONTINUITY:" + req["series_id"]},
                )
            report = continuity_request(
                f, [released], [entry(f, released, 1)], series_id=req["series_id"]
            )
            pub, _, review_payload = qualify(service, owner, independent, work, report)
            service.review(
                independent,
                work,
                {
                    **review_payload,
                    "decision": "CHANGES_REQUIRED",
                    "reason": "回归用例要求再读",
                    "idempotency_key": uid(),
                },
            )
            advance_rolling(kernel, gateway, owner, work, branch)
            with kernel.db.uow(write=False) as repo:
                assert not repo.one(
                    "selections",
                    {"work_id": work, "selection_kind": "CONTINUITY:" + req["series_id"]},
                )
            approved = service.review(
                independent, work, {**review_payload, "idempotency_key": uid()}
            )
            auto = RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)
            assert auto["status"] == "ROLLING_CONTINUITY_PUBLISHED", auto
            with kernel.db.uow(write=False) as repo:
                snapshot = kernel.artifact(
                    repo, auto["snapshot_ref"], work, kind="CONTINUITY_SNAPSHOT"
                )["payload"]
                assert snapshot["review_ref"] == approved and approved != pub["review_ref"]
                assert snapshot["request"]["ordered_release_refs"] == [released["release_ref"]]
            # First supervisor object is discarded after the durable event.
            gateway = connect(kernel, adapter)
        assert len(calls) == 8

        packet = json.loads(calls[4]["messages"][1]["content"])
        assert len(packet["authorized_materials"]) == 2


def test_retry_cancelled_unsent_node_and_replace_only_unopened_window(kernel_env):
    from novel_factory.application.authorization import Principal
    from novel_factory.application.rolling_production import replace_planned_window

    kernel = kernel_env["kernel"]
    f = setup_batch(kernel, count=2)
    wr, owner = f["world_ref"], f["principal"]
    work, branch = wr["work_id"], wr["branch_id"]
    req = request(kernel, f, 1, preset="medium_long")
    with backend() as (adapter, calls, _server):
        gateway = connect(kernel, adapter)
        receipt = register_rolling(kernel, gateway, owner, **req)
        state = rolling_status(kernel, owner, work, branch)
        replanning = {
            "world_ref": wr,
            "run_id": receipt["run_id"],
            "window_index": 0,
            "expected_window_ref": state["window_refs"][0],
            "chapters": req["volumes"][0]["chapters"][:1],
            "idempotency_key": uid(),
        }
        with pytest.raises(DomainError) as active:
            replace_planned_window(kernel, owner, **replanning)
        assert active.value.code == "STALE_PLAN"
        replanning.update(
            window_index=1,
            expected_window_ref=state["window_refs"][1],
            chapters=req["volumes"][0]["chapters"][1:],
            idempotency_key=uid(),
        )
        changed = replace_planned_window(kernel, owner, **replanning)
        assert (
            changed["status"] == "REPLANNED"
            and replace_planned_window(kernel, owner, **replanning) == changed
        )
        supervisor = RuntimeSupervisor(kernel, gateway)
        supervisor.run_once(owner, work, branch)
        board = pipeline_status(kernel, owner, work, branch)
        node_id = "draft:" + f["scenes"][0]["scene_contract"]["scene_id"]
        node = board["nodes"][node_id]
        with kernel.db.uow(write=False) as repo:
            task = repo.one("tasks", {"task_id": node["task_id"]})
        kernel.cancel_task(
            Principal(task["grant_id"], task["principal_id"]),
            task["task_id"],
            task["generation"],
            uid(),
        )
        supervisor.run_once(owner, work, branch)
        args = {
            "world_ref": wr,
            "run_id": receipt["run_id"],
            "node_id": node_id,
            "window_index": 0,
            "idempotency_key": uid(),
        }
        retry = retry_rolling(kernel, owner, **args)
        assert retry["status"] == "QUEUED" and retry_rolling(kernel, owner, **args) == retry
        RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)
        restored = pipeline_status(kernel, owner, work, branch)["nodes"][node_id]
        assert (
            restored["task_id"] != node["task_id"]
            and restored["history"][0]["task_id"] == node["task_id"]
        )
        assert calls == []


@pytest.mark.parametrize("failure", ["unresolved_fact", "missed_ending", "epistemic_conflict"])
def test_blocked_or_incomplete_factual_reading_cannot_accept(kernel_env, failure):
    kernel = kernel_env["kernel"]
    f = setup_batch(kernel, count=1)
    wr, owner = f["world_ref"], f["principal"]
    work, branch = wr["work_id"], wr["branch_id"]
    with backend() as (adapter, calls, server):

        def candidate(packet):
            response = answer(packet)
            if failure == "epistemic_conflict" and "逐项核查人物所知" in packet["assignment"]:
                read = json.loads(packet["authorized_materials"][0]["content"])
                response["issues"] = [
                    {
                        "issue_id": uid(),
                        "category": "HARD_CONSISTENCY",
                        "severity": "BLOCKER",
                        "subject_ref": read["draft_ref"],
                        "description": "角色获知信息的来源不明",
                        "evidence_refs": [],
                        "suggested_change": "补上可验证的获知时序",
                        "disposition": "OPEN",
                        "issue_kind": "HARD_CONFLICT",
                    }
                ]
            if packet["context"]["role_id"] == "evidence_maintainer":
                doc = json.loads(response["text"])
                claim = doc["assertions"][0]
                if failure == "unresolved_fact":
                    claim["classification"] = "UNCERTAIN"
                else:
                    claim["quote"] = claim["quote"][:-1]
                response["text"] = json.dumps(doc)
            return response

        server.packet_candidate = candidate
        generate = adapter.generate
        adapter.generate = lambda packet: replace(
            generate(packet), actual_amount=Decimal(0), currency="USD"
        )
        gateway = connect(kernel, adapter)
        register_rolling(kernel, gateway, owner, **request(kernel, f, 1))
        for _ in range(25):
            if RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)["status"] == "IDLE":
                break
        board = pipeline_status(kernel, owner, work, branch, include_outputs=True)
        key = "facts:" + f["scenes"][0]["scene_contract"]["scene_id"]
        assert board["nodes"][key]["state"] == "STOPPED"
        if failure == "unresolved_fact":
            evidence = board["outputs"][key]
            assert evidence["status"] == "BLOCKED" and "acceptance_ref" not in evidence
            with kernel.db.uow(write=False) as repo:
                report = kernel.artifact(
                    repo, evidence["reconciliation_ref"], work, kind="RECONCILIATION_REPORT"
                )
            assert report["payload"]["hard_blockers"]
        elif failure == "epistemic_conflict":
            knowledge = board["nodes"][key]["knowledge_node"]
            assert board["outputs"][knowledge]["status"] == "BLOCKED"
            assert board["nodes"][key]["reason"] == "PREDECESSOR_STOPPED"
            assert "task_id" not in board["nodes"][key]
        else:
            assert board["nodes"][key]["reason"] == "INVALID_EVIDENCE"
        with kernel.db.uow(write=False) as repo:
            assert not repo.many("artifacts", {"work_id": work, "kind": "NARRATIVE_ACCEPTANCE"})
        assert rolling_status(kernel, owner, work, branch)["frozen_chapters"] == 0
        assert len(calls) == (3 if failure == "epistemic_conflict" else 4)


def test_quality_length_checked_before_readers_and_freeze(kernel_env, monkeypatch):
    from test_production_strategy import drive, synthetic_production
    kernel = kernel_env["kernel"]
    gateway, owner, wr, _calls, diagnoses = synthetic_production(
        kernel, monkeypatch, lengths=[1, 5, 3, 2, 7])
    work = wr["work_id"]
    board, cursor = drive(kernel, gateway, owner, wr)
    assert cursor["state"] == "FROZEN" and len(diagnoses) == 1
    writer = next(node for node in board["nodes"].values() if node["stage"] == "draft")
    assert [r["actual"] for r in writer["extent_repairs"]] == [1, 5, 3, 2]
    assert writer["extent_repairs"][-1]["decision"] == "STRATEGY_REASSESSMENT_REQUIRED"
    assert writer["prior_draft_ref"] == writer["history"][-1]["output_ref"]
    assert len({r["task_id"] for r in writer["history"]}) == 4
    with kernel.db.uow(write=False) as repo:
        assert len(repo.many("tasks", {"work_id": work, "role_id": "reviewer", "task_kind": "epistemic"})) == 1


def test_quality_reissues_only_a_verified_malformed_fact_reading(kernel_env):
    kernel = kernel_env["kernel"]
    f = setup_batch(kernel, count=1)
    owner, wr = f["principal"], f["world_ref"]
    params = request(kernel, f, 1)
    params["profile"]["overrides"]["production_mode"] = "quality"
    readings = []
    with backend() as (adapter, calls, server):
        def candidate(packet):
            result = answer(packet)
            if packet["context"]["role_id"] == "evidence_maintainer":
                reading = json.loads(result["text"])
                entry = reading["assertions"][0]
                entry.pop("quote")
                entry["passage_ids"] = [1]
                if not readings:
                    entry["proposed_command"] = "invalid"
                readings.append(packet["assignment"])
                result["text"] = json.dumps(reading)
            return result
        server.packet_candidate = candidate
        generate = adapter.generate
        adapter.generate = lambda packet: replace(generate(packet), actual_amount=Decimal(0), currency="USD")
        gateway = connect(kernel, adapter)
        register_rolling(kernel, gateway, owner, **params)
        for _ in range(40):
            RuntimeSupervisor(kernel, gateway).run_once(owner, wr["work_id"], wr["branch_id"])
            board = pipeline_status(kernel, owner, wr["work_id"], wr["branch_id"], include_outputs=True)
            fact = next(n for n in board["nodes"].values() if n["stage"] == "facts")
            if fact["state"] == "FACTS_ACCEPTED":
                break
        assert fact["state"] == "FACTS_ACCEPTED", (fact["state"], fact.get("reason"))
        assert len(readings) == 2 and len(calls) == 5
        assert "properties/proposed_command" in readings[1]
        repair, = fact["fact_format_repairs"]
        assert repair["corrected_attempt_id"] != repair["attempt_id"] and repair["actual_fix"] == fact["output_ref"]


def test_numbered_fact_spans_preserve_coverage_and_native_contract():
    from novel_factory.application.rolling_facts import (
        ReadingFormatError,
        extract_assertions,
        passage_index,
    )
    body = "一。\n二！？三"
    index = passage_index(body)
    assert "".join(p["text"] for p in index) == body.replace("\n", "")
    entry = {"classification": "SURFACE_DETAIL", "summary": "工程样例", "event_ref": None,
             "world_check": None, "knowledge_use": None, "proposed_command": None,
             "disclosure_id": None, "declared_delta_index": None}
    ids = list(range(1, len(index) + 1))
    def parse(numbers):
        return extract_assertions(body, json.dumps({"assertions": [{**entry, "passage_ids": numbers}]}), "fixture")
    assert parse(ids)[0]["span"]["end"] == len(body)
    for bad in ([0], [True], ids[1:], ids[::-1], [1, 1], [1, 3], [999]):
        with pytest.raises(ReadingFormatError):
            parse(bad)


def test_fact_reader_owns_missing_knowledge_binding():
    from novel_factory.application.rolling_facts import ReadingFormatError, extract_assertions
    body = "工程示例。"
    claim = {"passage_ids": [1], "classification": "SPEECH_OR_BELIEF", "summary": "工程样例", "event_ref": None,
             "world_check": None, "knowledge_use": None, "proposed_command": None, "disclosure_id": None,
             "declared_delta_index": None}
    with pytest.raises(ReadingFormatError) as missing:
        extract_assertions(body, json.dumps({"assertions": [claim]}), "test")
    assert missing.value.diagnostic["keyword"] == "missingSpeechOrBeliefBinding"
