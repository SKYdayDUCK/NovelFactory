"""Two consecutive chapters traverse actual PG/MCP automation and book rework.

All prose and Host responses are engineering fixtures; real model sends retain
the synthetic host's outbound fences. No manually constructed editing binding.
"""
import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import pytest
import test_native_final_book as final_fixture
import test_production_strategy as strategy_fixture
from sqlalchemy import text
from test_multiscene_coverage import two_scenes, uid

from novel_factory.application import production_pipeline, rolling_production
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.planning import PlanningService
from novel_factory.application.production_pipeline import _artifact
from novel_factory.application.returned_edition_acceptance import (
    PACKET_KIND,
    PROOF_KIND,
    _checked_child,
    _delivered_predecessors,
    _validate_handoff,
    accepted_edition_handoff,
)
from novel_factory.application.review_mcp import ReviewMcpBroker
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.reconciliation import span_for
from novel_factory.fixtures import entity_value, fixture_definition, point
from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_two_covered_chapters_automatically_rework_and_freeze_book(kernel_env, monkeypatch):
    kernel = kernel_env["kernel"]
    captured, cases, steps, frozen_prefixes, diagnostic, reading_cuts = {}, [], [], [], {}, []
    original_editorial = final_fixture._editorial
    original_mcp = final_fixture.run_review_mcp
    original_freeze = rolling_production._freeze_window

    def final_mcp(bound, packet, *args, **kwargs):
        identity = json.loads(packet.messages[-1]["content"].split("\nIdentity: ", 1)[1].splitlines()[0])
        diagnostic["final_role"] = identity["role_id"]
        return original_mcp(bound, packet, *args, **kwargs)

    def freeze(*args, **kwargs):
        try:
            return original_freeze(*args, **kwargs)
        except DomainError as error:
            diagnostic["freeze"] = {"code": error.code, "message": error.safe_message}
            raise

    def editorial(phase, text):
        payload = original_editorial("SCENE" if phase == "SEAM" and diagnostic.get("final_role") == "reviewer" else phase, text)
        if phase == "STRUCTURE":
            payload["scene_functions"] = [{"scene": index + 1, "function": "Synthetic distinct scene function.",
                                           "quote": text} for index in range(2)]
        return payload

    def production(bound, patch, **_options):
        ids = fixture_definition("r1_sword")["ids"]
        save_scene = NarrativeService.save_scene

        def scene_with_pending_check(service, principal, payload, key):
            payload = deepcopy(payload)
            payload["entry_checks"] = [{"check_id": uid(), "plane": "WORLD", "subject_id": ids["sword"],
                "predicate": "possessed_by", "at": payload["scene_contract"]["story_interval"]["start"],
                "expected_status": "VALUE", "expected_value": entity_value(ids["smith"]),
                "actor_id": None, "known_at": None}]
            return save_scene(service, principal, payload, key)

        patch.setattr(NarrativeService, "save_scene", scene_with_pending_check)
        fixture = two_scenes(bound, dependent=True)
        patch.setattr(NarrativeService, "save_scene", save_scene)
        patch.setattr(strategy_fixture, "setup_batch", lambda *_args, **_kwargs: fixture)

        def actual_mcp(bound, packet, thread, turn, **_options):
            with bound.db.uow(write=False) as repo:
                call = repo.one("call_attempts", {"attempt_id": packet.attempt_id})
                task = repo.one("tasks", {"task_id": call["task_id"]})
                job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})["config"]
                board = production_pipeline._load(bound, repo, task["work_id"], task["branch_id"])
                node = board["nodes"][job["production_node"]]
                current = node
                while not current.get("scene_ref"):
                    current = board["nodes"][current["parents"][0]]
                scene = bound.artifact(repo, current["scene_ref"], task["work_id"], kind="SCENE_PLAN")["payload"]
                at = scene["scene_contract"]["story_interval"]["start"]
                spec = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})["spec"]
                head = bound.head(repo, task["work_id"], task["branch_id"])
                assert spec["world_ref"] == head, {"role": task["role_id"], "kind": task["task_kind"],
                                                  "spec_world_ref": spec["world_ref"], "head": head}

            async def run():
                async with ReviewMcpBroker.from_attempt(bound, packet) as broker:
                    broker.bind(thread, turn)
                    await broker.call({"threadId": thread, "turnId": turn, "callId": "two-chapter-context",
                                       "tool": "nf_context_get", "arguments": {}})
                    if "nf_query_batch" in broker.allowed_tools:
                        await broker.call({"threadId": thread, "turnId": turn, "callId": "two-chapter-world",
                            "tool": "nf_query_batch", "arguments": {"lookups": [{"subject_id": ids["sword"],
                                "predicate": "possessed_by", "at": at, "plane": "WORLD"}]}})
                    for index, ref in enumerate(broker.scope["material_refs"]):
                        await broker.call({"threadId": thread, "turnId": turn, "callId": f"two-chapter-material-{index}",
                                           "tool": "nf_review_material", "arguments": {"artifact_ref": ref}})
                    assert broker.scope["world_ref"] == head
                    reading_cuts.append({"task_id": task["task_id"], "role": task["role_id"],
                                         "kind": task["task_kind"], "world_ref": head})
                    return list(broker.receipt_refs)
            return asyncio.run(run())

        patch.setattr(strategy_fixture, "run_review_mcp", actual_mcp)
        gateway, owner, source, calls, diagnoses = strategy_fixture.synthetic_production(
            bound, patch, lengths=[7, 7], target=(12, 20))
        generate = RuntimeModelAdapter.generate

        def typed_verification(adapter, packet):
            response = generate(adapter, packet)
            envelope = json.loads(response.text)
            with bound.db.uow(write=False) as repo:
                task = repo.one("tasks", {"task_id": envelope["task_id"]})
                if task["task_kind"] != "verification":
                    return response
                receipts = [(ref, bound.artifact(repo, ref, task["work_id"])["payload"])
                            for ref in response.raw_response["review_mcp_receipt_refs"]]
            case = next(receipt["result"]["structuredContent"]["case"] for _, receipt in receipts
                        if receipt["tool"] == "nf_review_material" and "case" in receipt["result"]["structuredContent"])
            query_ref, query = next((ref, receipt["result"]["structuredContent"]["items"][0]["payload"]["state_items"][0]["answer"])
                                    for ref, receipt in receipts if receipt["tool"] == "nf_query_batch")
            read = next(receipt["result"]["structuredContent"] for _, receipt in receipts
                        if receipt["tool"] == "nf_review_material"
                        and receipt["arguments"]["artifact_ref"] == case["source_draft_ref"])
            citations = [{"kind": "ARTIFACT", "artifact_ref": query_ref},
                         *[{"kind": "ARTIFACT", "artifact_ref": read["text_ref"],
                            "span": span_for(read["text"], start, start + 1)} for start in range(2)]]
            status = "PENDING_EVALUATION" if query["evaluation_status"] == "PENDING" else "NO_CONFLICT"
            assert status == "PENDING_EVALUATION" or query["knowledge_status"] == "KNOWN"
            answers = [{"index": item["index"], "status": status, "explanation": "Synthetic exact-scene system inquiry.",
                        "correction": None, "evidence_refs": citations} for item in case["questions"]]
            envelope["payload"] = {"decision": json.dumps({"case_hash": case["case_hash"], "answers": answers}),
                "reason": "Synthetic actual-MCP fixed-cut verification.", "deferred_questions": [], "evidence_refs": citations}
            cases.append({"case": deepcopy(case), "answer": deepcopy(query)})
            return replace(response, text=json.dumps(envelope))

        patch.setattr(RuntimeModelAdapter, "generate", typed_verification)
        work, branch = source["work_id"], source["branch_id"]
        idle = 0
        for _ in range(350):
            step = RuntimeSupervisor(bound, gateway).run_once(owner, work, branch)
            steps.append({key: step[key] for key in ("status", "reason", "node_id", "state") if key in step})
            with bound.db.uow(write=False) as repo:
                cursor = rolling_production._load(bound, repo, work, branch)
                board = production_pipeline._load(bound, repo, work, branch)
                idle = idle + 1 if step["status"] == "IDLE" else 0
                if idle >= 8:
                    diagnostic["conflicts"] = [dict(row) for row in repo.conn.execute(text("""
                        SELECT task_id::text,generation,conflict_type,strategy,
                          details->>'detection_method' AS detection_method,details->'scopes' AS scopes,
                          details->'source_world_refs' AS source_world_refs
                        FROM nf_conflict_cases WHERE work_id=:work
                    """), {"work": work}).mappings()]
                    diagnostic["observations"] = [dict(row) for row in repo.conn.execute(text("""
                        SELECT r.task_id::text,t.role_id,t.task_kind,t.state,r.world_revision_id::text,r.keys,
                          r.details->>'origin' AS origin,r.details->'base_world_ref' AS base_world_ref,
                          r.details->'story_time' AS story_time,r.details->'uncertain_keys' AS uncertain_keys,
                          ctx.read_cut->'world_ref' AS context_world_ref
                        FROM nf_semantic_reads r JOIN nf_tasks t USING(task_id)
                        LEFT JOIN nf_contexts ctx ON ctx.task_id=t.task_id AND ctx.generation=t.generation
                        WHERE r.work_id=:work AND t.state='PAUSED'
                    """), {"work": work}).mappings()]
                    diagnostic["changes"] = [dict(row) for row in repo.conn.execute(text("""
                        SELECT world_revision_id::text,branch_seq,keys,details->'story_points' AS story_points
                        FROM nf_semantic_changes WHERE work_id=:work ORDER BY branch_seq
                    """), {"work": work}).mappings()]
                    pytest.fail("Two-chapter automation is idle: " + json.dumps({"cursor_state": cursor["state"],
                        "cursor_reason": cursor["reason"], "diagnostic": diagnostic,
                        "cases": [{"target": item["case"]["target"], "world_ref": item["case"]["world_ref"],
                                   "answer": item["answer"]} for item in cases],
                        "nodes": [{key: node[key] for key in ("stage", "state", "reason") if key in node}
                                  for node in board["nodes"].values()], "steps": steps[-12:]}))
                if cursor["state"] == "FROZEN" and cursor["last_checkpoint"] not in frozen_prefixes:
                    frozen_prefixes.append(deepcopy(cursor["last_checkpoint"]))
                if cursor["state"] == "FROZEN" and cursor["frozen_chapters"] == 2:
                    captured.update(fixture=fixture, source=source, owner=owner, checkpoint=cursor["last_checkpoint"],
                                    adopted=bound.head(repo, work, branch))
                    break
        else:
            pytest.fail("Two-chapter automation stalled: " + json.dumps({"cursor": cursor, "steps": steps[-15:]}))
        assert not diagnoses and len([identity for identity, _, _ in calls if identity["role_id"] == "writer"]) == 2
        assert all(identity["world_ref"] == source for identity, _, _ in calls if identity["role_id"] == "writer")
        return gateway, owner, source, calls, diagnoses

    monkeypatch.setattr(final_fixture, "synthetic_production", production)
    monkeypatch.setattr(final_fixture, "_editorial", editorial)
    monkeypatch.setattr(final_fixture, "run_review_mcp", final_mcp)
    monkeypatch.setattr(rolling_production, "_freeze_window", freeze)
    final_fixture.test_native_editing_blind_reading_restart_and_final_freeze(kernel_env, monkeypatch, "book_rework")
    source, adopted = captured["source"], captured["adopted"]
    assert len(cases) == 4 and len(frozen_prefixes) == 2
    assert reading_cuts and {row["world_ref"]["world_revision_id"] for row in reading_cuts} == {
        source["world_revision_id"], cases[1]["case"]["world_ref"]["world_revision_id"], adopted["world_revision_id"]}
    assert [item["answer"]["evaluation_status"] for item in cases] == ["PENDING", "EVALUATED"] * 2
    assert [item["answer"]["story_time"] for item in cases] == [point(2), point(2), point(3), point(3)]
    assert cases[1]["case"]["world_ref"] != cases[3]["case"]["world_ref"] == adopted
    with kernel.db.uow() as repo:
        closures = repo.many("receipts", {"work_id": source["work_id"], "command_type": "WORLD_COVERAGE_RECOVERY"})
        assert len(closures) == 2 and all(not row["payload"]["worldclosure_receipt"]["accepted_event_refs"] for row in closures)
        for index, item in enumerate((cases[1], cases[3])):
            revision = kernel.revision(repo, item["case"]["world_ref"])
            assert revision["frontiers"][captured["fixture"]["ids"]["sword"]] == point(index + 2, 1)
        cursor = rolling_production._load(kernel, repo, source["work_id"], source["branch_id"])
        scenes = rolling_production.frozen_volume_scenes(kernel, repo, cursor, captured["fixture"]["volume_id"])
        assert len(scenes) == 2
        for scene in scenes:
            accepted = kernel.artifact(repo, scene["acceptance_ref"], source["work_id"], kind="NARRATIVE_ACCEPTANCE")["payload"]
            draft = kernel.artifact(repo, scene["draft_ref"], source["work_id"], kind="NARRATIVE_DRAFT")["payload"]
            assert accepted["world_ref"] == adopted and draft["input_world_ref"] == source
            assert draft["prior_draft_ref"] and accepted["world_effects_applied"] == 0
            facts = kernel.artifact(repo, scene["facts_report_ref"], source["work_id"], kind="ROLLING_FACT_REPORT")["payload"]
            assert facts["acceptance_ref"] == scene["acceptance_ref"] and facts["knowledge_ref"] == scene["knowledge_ref"]
        checkpoint = kernel.artifact(repo, cursor["last_checkpoint"], source["work_id"], kind="ROLLING_CHECKPOINT")["payload"]
        manifest = kernel.artifact(repo, checkpoint["edition_ref"], source["work_id"], kind="P6_RETURNED_EDITION_CANDIDATE")["payload"]
        children = manifest["candidates"]
        proof = kernel.artifact(repo, children[1]["handoff_proof_ref"], source["work_id"], kind=PROOF_KIND)["payload"]
        packet = kernel.artifact(repo, proof["packet_ref"], source["work_id"], kind=PACKET_KIND)["payload"]
        assert packet["predecessors"][0]["draft_ref"] == children[0]["draft_ref"] and proof["delivery_refs"]

        # An actual earlier writer did not receive this new predecessor packet.
        earlier = repo.one("tasks", {"task_id": children[0]["writer_task_id"]})
        from novel_factory.application.agent_gateway import AgentGateway
        from novel_factory.application.candidate_delivery import CandidateDelivery
        audit, _ = CandidateDelivery(kernel, AgentGateway(kernel, {})).audited_candidate(repo, earlier, production_accounting=True)
        with pytest.raises(DomainError) as unread:
            _delivered_predecessors(kernel, repo, earlier, audit, proof["packet_ref"], packet)
        assert unread.value.code == "STALE_SCENE_HANDOFF"

        old_second = kernel.artifact(repo, captured["checkpoint"], source["work_id"], kind="ROLLING_CHECKPOINT")["payload"]["chapters"][0]["scenes"][0]
        ordinary = kernel.artifact(repo, old_second["acceptance_ref"], source["work_id"], kind="NARRATIVE_ACCEPTANCE")["payload"]
        selected = kernel.artifact(repo, scenes[0]["acceptance_ref"], source["work_id"], kind="NARRATIVE_ACCEPTANCE")["payload"]
        with pytest.raises(DomainError) as non_edition:
            accepted_edition_handoff(kernel, repo, {"acceptance": ordinary}, selected, selected)
        assert non_edition.value.code == "STALE_SCENE_HANDOFF"

        for tamper in ("token", "foreign_draft"):
            transaction = repo.conn.begin_nested()
            try:
                bad_packet = deepcopy(packet)
                if tamper == "token":
                    bad_packet["handoff_token"] = "0" * 64
                else:
                    bad_packet["predecessors"][0]["draft_ref"] = children[1]["draft_ref"]
                    bad_packet["handoff_token"] = fingerprint({key: value for key, value in bad_packet.items() if key != "handoff_token"})
                packet_ref = _artifact(kernel, repo, source["work_id"], captured["owner"], PACKET_KIND, bad_packet)
                bad_proof = {**proof, "packet_ref": packet_ref}
                proof_ref = _artifact(kernel, repo, source["work_id"], captured["owner"], PROOF_KIND, bad_proof)
                child = {**children[1], "handoff_proof_ref": proof_ref}
                with pytest.raises(DomainError) as invalid:
                    _validate_handoff(kernel, repo, manifest, child, [children[0], child])
                assert invalid.value.code == "STALE_SCENE_HANDOFF"
            finally:
                transaction.rollback()

        transaction = repo.conn.begin_nested()
        try:
            facts = deepcopy(kernel.artifact(repo, children[0]["facts_ref"], source["work_id"], kind="ROLLING_FACT_REPORT")["payload"])
            facts["extraction_ref"] = children[1]["extraction_ref"]
            mixed_ref = _artifact(kernel, repo, source["work_id"], captured["owner"], "ROLLING_FACT_REPORT", facts)
            with pytest.raises(DomainError) as mixed:
                _checked_child(kernel, repo, captured["owner"], adopted, {**children[0], "facts_ref": mixed_ref})
            assert mixed.value.code == "INVALID_EVIDENCE"
        finally:
            transaction.rollback()
        with pytest.raises(DomainError) as mixed_knowledge:
            _checked_child(kernel, repo, captured["owner"], adopted, {**children[0], "knowledge_ref": children[1]["knowledge_ref"]})
        assert mixed_knowledge.value.code == "EPISTEMIC_REVIEW_REQUIRED"
        current_second = kernel.artifact(repo, scenes[1]["acceptance_ref"], source["work_id"], kind="NARRATIVE_ACCEPTANCE")["payload"]
        second_draft = kernel.artifact(repo, children[1]["draft_ref"], source["work_id"], kind="NARRATIVE_DRAFT")["payload"]
        parent = kernel.artifact(repo, second_draft["handoff_refs"][0], source["work_id"], kind="NARRATIVE_ACCEPTANCE")["payload"]
        assert accepted_edition_handoff(kernel, repo, {"acceptance": current_second}, parent, selected)
        for corruption in ("missing_scene", "foreign_scene_draft"):
            transaction = repo.conn.begin_nested()
            try:
                overlay = deepcopy(checkpoint)
                overlay.update(source_checkpoint_ref=cursor["last_checkpoint"], revalidation_kind="PURE_FORMAL_COVERAGE")
                if corruption == "missing_scene":
                    overlay["chapters"][0]["scenes"] = []
                else:
                    overlay["chapters"][0]["scenes"][0]["draft_ref"] = children[1]["draft_ref"]
                overlay_ref = _artifact(kernel, repo, source["work_id"], captured["owner"], "ROLLING_CHECKPOINT", overlay)
                PlanningService(kernel)._select(repo, adopted, "P6_FROZEN_SCENE:" + children[0]["scene_id"], overlay_ref)
                with pytest.raises(DomainError) as corrupt:
                    accepted_edition_handoff(kernel, repo, {"acceptance": current_second}, parent, selected)
                assert corrupt.value.code == "STALE_SCENE_HANDOFF"
            finally:
                transaction.rollback()
        historic = kernel.artifact(repo, frozen_prefixes[0], source["work_id"], kind="ROLLING_CHECKPOINT")["payload"]
        first = kernel.artifact(repo, historic["chapters"][0]["scenes"][0]["acceptance_ref"], source["work_id"])["payload"]
        assert first["world_ref"] == cases[1]["case"]["world_ref"] != adopted
        assert kernel.head(repo, source["work_id"], source["branch_id"]) == adopted
