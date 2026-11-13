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
@pytest.mark.parametrize("return_final", [False, True])
def test_one_frozen_volume_needs_actual_model_opinions_and_a_separate_decision(
    kernel_env, monkeypatch, return_final
):
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
        register_rolling(kernel, gateway, owner, **request(kernel, f, 1))
        for _ in range(30):
            RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)
            if rolling_status(kernel, owner, work, branch)["state"] == "FROZEN":
                break
        first = len(calls)
        seen = []

        def judgment(packet):
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
                    "decision": "RETURNED" if return_final else "APPROVED",
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
            return {
                "decision": "APPROVED",
                "summary": "保留北窗和雨声。",
                "scene_functions": [{"scene": 1, "function": "建立观察位置", "quote": "北窗"}]
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
            if state["status"] != "EDITING_ACTIVE" or turn.get("status") == "BLOCKED":
                break
        assert state["status"] == ("EDITING_RETURNED" if return_final else "EDITORIAL_APPROVED"), (
            state["reason"],
            state["index"],
            seen,
            observed[-3:],
        )
        assert seen == ["SCENE", "CHAPTER", "VOLUME", "STRUCTURE", "SEAM", "LANGUAGE", "APPROVAL"]
        assert len({r["attempt_id"] for r in state["receipts"]}) == len(seen)
        if return_final:
            with pytest.raises(DomainError, match="editorial approval is missing"):
                final_review_request(kernel, owner, work, branch)
        else:
            assert (
                final_review_request(kernel, owner, work, branch)["editorial_approval_ref"]
                == state["approval_ref"]
            )
