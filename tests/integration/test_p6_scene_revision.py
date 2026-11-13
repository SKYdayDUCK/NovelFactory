"""Drive native scene revisions on PostgreSQL through the supervisor with a local fixture."""
import json
from dataclasses import replace
from decimal import Decimal

import pytest
from test_p5_supervisor import backend
from test_p6_rolling_production import answer, connect, request, uid
from test_release import setup_batch

from novel_factory.application.production_pipeline import pipeline_status
from novel_factory.application.rolling_production import register_rolling, rolling_status
from novel_factory.application.supervisor import RuntimeSupervisor


pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.mark.parametrize("defect", ["facts", "epistemic", "literary", "exhausted"])
def test_targeted_scene_revision_rechecks_the_new_text(kernel_env, defect):
    kernel = kernel_env["kernel"]
    f = setup_batch(kernel, count=1)
    wr, owner = f["world_ref"], f["principal"]
    work, branch = wr["work_id"], wr["branch_id"]
    req = request(kernel, f, size=1)
    with kernel.db.uow() as repo:
        kernel._receipt(repo, owner, wr, "P6_IDEA_PROVENANCE", "initial", {},
                        {"creative_input": {"length": "short"}, "model_routes": {"creative": "fixture", "review": "fixture"}})
    with backend() as (adapter, calls, server):
        texts = []
        def respond(packet):
            reply = answer(packet)
            role = packet["context"]["role_id"]
            assignment = packet["assignment"]
            if role == "writer":
                if texts:
                    assert any(texts[0] in mat["content"] for mat in packet["authorized_materials"])
                    assert "修订" in assignment
                    reply["text"] = "屋檐下落了几滴雨。"
                texts.append(reply["text"])
            elif role == "evidence_maintainer" and defect == "facts" and len(texts) == 1:
                doc = json.loads(reply["text"])
                doc["assertions"][0]["classification"] = "UNCERTAIN"
                reply["text"] = json.dumps(doc)
            elif role == "reviewer" and (len(texts) == 1 or defect == "exhausted"):
                if (("人物所知" in assignment and defect in {"epistemic", "exhausted"}) or
                        ("独立阅读本章" in assignment and defect == "literary")):
                    source = json.loads(packet["authorized_materials"][0]["content"])
                    reply["issues"] = [{"issue_id": uid(), "category": "CONTINUITY", "severity": "BLOCKER",
                                        "subject_ref": source["draft_ref"], "description": "原句缺少观察依据。",
                                        "evidence_refs": [], "suggested_change": "描写可观察到的雨滴。",
                                        "disposition": "OPEN", "issue_kind": "CHANGE_REQUEST"}]
            return reply
        server.packet_candidate = respond
        generate = adapter.generate
        adapter.generate = lambda packet: replace(generate(packet), actual_amount=Decimal(0), currency="USD")
        gateway = connect(kernel, adapter)
        register_rolling(kernel, gateway, owner, **req)
        events = []
        for _ in range(48):
            event = RuntimeSupervisor(kernel, gateway).run_once(owner, work, branch)
            events.append((event["status"], event.get("reason")))
            state = rolling_status(kernel, owner, work, branch)
            if state["frozen_chapters"] or event["status"] == "IDLE":
                break
        board = pipeline_status(kernel, owner, work, branch, include_outputs=True)
        key = "draft:" + f["scenes"][0]["scene_contract"]["scene_id"]
        writer = board["nodes"][key]
        assert len(texts) == 2, (events, writer)
        assert writer["revision_count"] == 1 and len(writer["history"]) == 1
        with kernel.db.uow(write=False) as repo:
            task = repo.one("tasks", {"task_id": writer["task_id"]})
            draft = kernel.artifact(repo, writer["output_ref"], work)["payload"]
            assert task["task_kind"] == "targeted_revision"
            assert draft["prior_draft_ref"] == writer["history"][0]["output_ref"]
        assert state["frozen_chapters"] == int(defect != "exhausted"), events
