import json
from dataclasses import replace
from decimal import Decimal

import pytest
from test_p5_supervisor import backend
from test_p6_rolling_production import answer, connect, request
from test_release import setup_batch

from novel_factory.application import frozen_editing
from novel_factory.application.rolling_production import register_rolling, rolling_status
from novel_factory.application.rolling_publication import final_review_request
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.errors import DomainError


@pytest.mark.postgres
@pytest.mark.parametrize("return_final", [False, True, "quality_repair", "child_edition", "unchanged_child"])
def test_one_frozen_volume_needs_actual_model_opinions_and_a_separate_decision(
    kernel_env, monkeypatch, return_final
):
    child_case = return_final in {"child_edition", "unchanged_child"}
    kernel = kernel_env["kernel"]
    f = setup_batch(kernel, count=1)
    wr, owner = f["world_ref"], f["principal"]
    work, branch = wr["work_id"], wr["branch_id"]
    with backend() as (adapter, calls, server):
        real_generate = adapter.generate
        monkeypatch.setattr(
            adapter,
            "generate",
            lambda req: replace(real_generate(req), actual_amount=Decimal(0), currency="USD"),
        )
        gateway = connect(kernel, adapter)
        server.packet_candidate = answer
        params = request(kernel, f, 1)
        if return_final in {"quality_repair", "child_edition", "unchanged_child"}:
            params["profile"].setdefault("overrides", {})["production_mode"] = "quality"
        register_rolling(kernel, gateway, owner, **params)
        for _ in range(30):
            RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)
            if rolling_status(kernel, owner, work, branch)["state"] == "FROZEN":
                break
        first = len(calls)
        seen = []

        def judgment(packet):
            assignment = json.loads(packet["assignment"])
            if assignment.get("operation") == "revise_returned_edition":
                assert packet["context"]["role_id"] == "writer"
                scoped_feedback = json.loads(packet["authorized_materials"][-1]["content"])
                assert scoped_feedback["editor_reason"] and scoped_feedback["source_draft_ref"]
                assert any("北窗" in p["content"] for p in packet["authorized_materials"])
                original_output = answer(packet)
                return {**original_output, "text": original_output["text"] + ("。" if return_final == "child_edition" else "")}
            if assignment.get("operation") == "read_returned_edition":
                assert packet["context"]["role_id"] == "reviewer"
                before, after = [json.loads(i["content"])["text"] for i in packet["authorized_materials"]]
                assert after == before + ("。" if return_final == "child_edition" else "")
                return {"summary": "Fixture compared both exact versions.", "issues": [],
                        "counterinterpretations": [], "evidence_refs": []}
            original = json.loads(packet["authorized_materials"][0]["content"])
            assert original["text"] == "北窗的雨声很轻。" and original["coverage_ref"]
            assignment = json.loads(packet["assignment"])
            phase = assignment["phase"]
            seen.append(phase)
            if phase in {"SCENE", "CHAPTER", "VOLUME"}:
                return {
                    "summary": "原文以雨声定位叙述距离。",
                    "issues": [],
                    "counterinterpretations": [],
                    "evidence_refs": [],
                }
            if phase == "APPROVAL":
                return {
                    "decision": "RETURNED" if return_final is True or child_case else "APPROVED",
                    "reason": "复核已完成的编辑阶段和原文。",
                    "deferred_questions": [],
                    "evidence_refs": [],
                }
            options = [
                {
                    "summary": "保留原顺序",
                    "disposition": "SELECTED",
                    "reason": "原文由近及远",
                    "benefits": ["明确方位"],
                    "costs": ["保持短句"],
                },
                {
                    "summary": "置换句序",
                    "disposition": "REJECTED",
                    "reason": "只有一个场景",
                    "benefits": ["改变节奏"],
                    "costs": ["损失定位"],
                },
            ]
            quote = "北窗"
            if return_final == "quality_repair" and phase == "STRUCTURE":
                if seen.count("STRUCTURE") == 1:
                    quote = "missing from the original"
                else:
                    assert assignment["validation_errors"][0]["kind"] == "INVALID_EVIDENCE"
            return {
                "decision": "APPROVED",
                "summary": "保留北窗和雨声。",
                "scene_functions": [{"scene": 1, "function": "建立观察位置", "quote": quote}]
                if phase == "STRUCTURE"
                else [],
                "considered_options": options if phase == "STRUCTURE" else [],
                "issue_decisions": [],
            }

        server.packet_candidate = judgment
        en = frozen_editing.start(kernel, owner, work, branch)
        assert en["status"] == "EDITING_ACTIVE" and len(calls) == first
        assert frozen_editing.start(kernel, owner, work, branch)["status"] == "EDITING_ACTIVE"
        observed = []
        for _ in range(45):
            turn = RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)
            observed.append((turn.get("status"), turn.get("reason")))
            state = frozen_editing.status(kernel, owner, work, branch)
            if state["status"] not in {"EDITING_ACTIVE", "EDITION_REVISING"} or turn.get("status") == "BLOCKED":
                break
        if child_case:
            # This legacy HTTP-only fixture is deliberately not a native MCP
            # reader. Preserve its child, but never accept it without that proof.
            assert state["status"] == "EDITION_REVISION_BLOCKED", (state["status"], state["reason"], observed[-5:])
            assert state["reason"] == "MODEL_UNAVAILABLE" and state["approval_ref"] is None
            from novel_factory.application.volume_successor import _selected
            with kernel.db.uow(write=False) as repo:
                full = frozen_editing._state(kernel, repo, work, branch)
                edition = full["edition"]
                frozen_ref = _selected(repo, work, "P6_FROZEN_EDITING:" + branch)
                child = edition["candidates"][0]
                assert _selected(repo, work, "P6_FROZEN_SCENE:" + child["scene_id"]) == edition["parent_checkpoint"]
                original = kernel.artifact(repo, _selected(repo, work, "NARRATIVE_SCENE:" + child["scene_id"]), work)["payload"]
                candidate = kernel.artifact(repo, child["draft_ref"], work, kind="NARRATIVE_DRAFT")["payload"]
                assert original["draft_ref"] == candidate["prior_draft_ref"] == child["parent_ref"]
                assert (original["text_ref"]["sha256"] != candidate["text_ref"]["sha256"]) == (return_final == "child_edition")
                reflection = kernel.artifact(repo, edition["reflection_ref"], work, kind="P6_REFLECTION_RECORD")["payload"]
                assert reflection["error_ref"] == edition["error_ref"] and reflection["attempt_id"] == edition["editor_attempt_id"]
                assert edition["editor_output_ref"] == state["returned_ref"] and edition["readings"] == []
                assert not _selected(repo, work, "RELEASE:" + full["volume"])
            ordinary = kernel.issue_grant(owner, work, role_id="writer", branch_ids=[branch], planes=["WORLD"])["principal"]
            with pytest.raises(DomainError):
                kernel.get_artifact(ordinary, work, frozen_ref)
            assert json.loads(kernel.get_artifact(owner, work, frozen_ref))["edition"] == edition
            assert frozen_editing.start(kernel, owner, work, branch)["status"] == "EDITION_REVISION_BLOCKED"
            sent = len(calls)
            for _ in range(3):
                RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)
            assert len(calls) == sent
            with pytest.raises(DomainError, match="editorial approval is missing"):
                final_review_request(kernel, owner, work, branch)
            return
        assert state["status"] == ("EDITING_RETURNED" if return_final is True else "EDITORIAL_APPROVED"), (
            state["reason"],
            state["index"],
            seen,
            observed[-3:],
        )
        assert seen == ["SCENE", "CHAPTER", "VOLUME", "STRUCTURE", *(["STRUCTURE"] if return_final == "quality_repair" else []), "SEAM", "LANGUAGE", "APPROVAL"]
        assert len({r["attempt_id"] for r in state["receipts"]}) == len(seen) - int(return_final == "quality_repair")
        if return_final is True:
            with pytest.raises(DomainError, match="editorial approval is missing"):
                final_review_request(kernel, owner, work, branch)
        else:
            assert (
                final_review_request(kernel, owner, work, branch)["editorial_approval_ref"]
                == state["approval_ref"]
            )
            from novel_factory.application.rolling_publication import queue_final_readers
            handed = queue_final_readers(kernel, owner, work, branch)
            assert handed == queue_final_readers(kernel, owner, work, branch)
            assert len(set(handed["reader_tickets"])) == 2
            with kernel.db.uow(write=False) as repo:
                rows = repo.many("outbox", {"topic": "production.independent_gpt_review.requested", "aggregate_id": work})
                assert sorted(r["payload"]["track"] for r in rows) == [1, 2]
                assert all(r["payload"]["edition_sha256"] == handed["edition_sha256"] for r in rows)
