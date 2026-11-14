"""Drive native scene revisions on PostgreSQL through the supervisor with a local fixture."""
import json
from dataclasses import replace
from decimal import Decimal

import pytest
from test_p5_supervisor import backend
from test_p6_rolling_production import answer, request, uid
from test_quality_memory_native import NativeReading
from test_release import setup_batch

from agent_runtime import native_codex, native_review, providers
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.production_pipeline import (
    pipeline_status,
    production_material_reader,
)
from novel_factory.application.review_mcp import ReviewMcpBroker
from novel_factory.application.rolling_production import register_rolling, rolling_status
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.domain.reconciliation import span_for
from novel_factory.fixtures import entity_value
from novel_factory.infrastructure.runtime_providers import RuntimeModelAdapter

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


@pytest.mark.parametrize("defect", ["facts", "epistemic", "literary", "exhausted", "uncertain"])
def test_targeted_scene_revision_rechecks_the_new_text(kernel_env, monkeypatch, defect):
    kernel = kernel_env["kernel"]
    f = setup_batch(kernel, count=1)
    wr, owner = f["world_ref"], f["principal"]
    work, branch = wr["work_id"], wr["branch_id"]
    req = request(kernel, f, size=1)
    req["routes"] = {"draft": "fixture", "review": "native", "plan": "native"}
    req["subject_ids"] = [f["ids"]["sword"], f["ids"]["protagonist"]]
    asserted_at = f["scenes"][0]["entry_checks"][0]["at"]
    actual_call = ReviewMcpBroker.call

    async def reader_queries_scene_time(broker, invocation):
        # This is the synthetic model's actual tool choice, before the broker
        # executes/audits it. Query the asserted scene time, not a future cut.
        if invocation["tool"] == "nf_query_batch":
            invocation = {**invocation, "arguments": {"lookups": [
                {**lookup, "at": asserted_at} for lookup in invocation["arguments"]["lookups"]]}}
        return await actual_call(broker, invocation)

    monkeypatch.setattr(ReviewMcpBroker, "call", reader_queries_scene_time)
    with kernel.db.uow() as repo:
        kernel._receipt(repo, owner, wr, "P6_IDEA_PROVENANCE", "initial", {},
                        {"creative_input": {"length": "short"}, "model_routes": {"creative": "fixture", "review": "native"}})
    forbidden = lambda *args, **kwargs: pytest.fail("Synthetic scene regression reached a real provider boundary")
    for module in (native_codex, native_review):
        monkeypatch.setattr(module, "generate", forbidden)
        monkeypatch.setattr(module, "start_host", forbidden)
    monkeypatch.setattr(providers, "post", forbidden)
    with backend() as (adapter, _calls, server):
        texts = []
        fact_reads = []
        verifications = []
        factual_queries = []

        def query_ref(packet):
            with kernel.db.uow(write=False) as repo:
                matches = [row for row in repo.many("artifacts", {"work_id": work, "kind": "NATIVE_REVIEW_TOOL_RECEIPT"})
                           if row["payload"]["task_id"] == packet["context"]["task_id"]
                           and row["payload"]["tool"] == "nf_query_batch" and row["payload"]["outcome"] == "OK"]
                assert len(matches) == 1
                if packet["context"]["role_id"] == "evidence_maintainer":
                    factual_queries.append(matches[0]["payload"])
                return {k: matches[0][k] for k in ("artifact_id", "artifact_revision_id", "sha256")}

        def respond(packet):
            reply = answer(packet)
            role = packet["context"]["role_id"]
            with kernel.db.uow(write=False) as repo:
                kind = repo.one("tasks", {"task_id": packet["context"]["task_id"]})["task_kind"]
            assignment = packet["assignment"]
            if role == "writer":
                if defect == "facts" and not texts:
                    reply["text"] = "主人公手里握着这把剑。"
                if texts:
                    assert any(texts[0] in mat["content"] for mat in packet["authorized_materials"])
                    assert "修订" in assignment
                    reply["text"] = "屋檐下落了几滴雨。"
                texts.append(reply["text"])
            elif role == "evidence_maintainer":
                fact_reads.append(packet["context"]["task_id"])
                if defect not in {"facts", "uncertain"} or len(fact_reads) != 1:
                    return reply
                doc = json.loads(reply["text"])
                claim = doc["assertions"][0]
                claim["classification"] = "HISTORICAL_CONFLICT" if defect == "facts" else "UNCERTAIN"
                if defect == "facts":
                    claim["world_check"] = {**f["scenes"][0]["entry_checks"][0], "check_id": uid(),
                                            "at": asserted_at, "expected_value": entity_value(f["ids"]["protagonist"])}
                    claim["summary"] = "原句称主人公持剑；固定世界记录铁匠持有，应删去该持有断言，改写不涉及持有权的雨声观察。"
                    reply["evidence_refs"] = [{"kind": "ARTIFACT", "artifact_ref": query_ref(packet)}]
                reply["text"] = json.dumps(doc)
            elif role == "reviewer" and kind == "verification":
                values = [json.loads(item["content"]) for item in packet["authorized_materials"]]
                case = next(value["case"] for value in values if "case" in value)
                draft = next(value for value in values if "draft_ref" in value and "text_ref" in value)
                citations = [{"kind": "ARTIFACT", "artifact_ref": query_ref(packet)},
                             {"kind": "ARTIFACT", "artifact_ref": case["report_ref"]},
                             {"kind": "ARTIFACT", "artifact_ref": draft["text_ref"], "span": span_for(draft["text"])}]
                verifications.append(case)
                return {"decision": json.dumps({"case_hash": case["case_hash"], "answers": [
                            {"index": question["index"], "status": "NO_CONFLICT", "explanation": "本句仅表现非持久雨声，未主张持有权或世界变化。",
                             "correction": None, "evidence_refs": citations} for question in case["questions"]]}),
                        "reason": "已实际查询固定世界并读取本版原文及完整事实报告；未知并非已证实矛盾。",
                        "deferred_questions": [], "evidence_refs": citations}
            elif (role == "reviewer" and (len(texts) == 1 or defect == "exhausted")
                  and (("人物所知" in assignment and defect in {"epistemic", "exhausted"})
                       or ("独立阅读本章" in assignment and defect == "literary"))):
                source = json.loads(packet["authorized_materials"][0]["content"])
                reply["issues"] = [{"issue_id": uid(), "category": "CONTINUITY", "severity": "BLOCKER",
                                        "subject_ref": source["draft_ref"], "description": "原句缺少观察依据。",
                                        "evidence_refs": [{"kind": "ARTIFACT", "artifact_ref": source["text_ref"],
                                                           "span": span_for(source["text"])}],
                                        "suggested_change": "将本句替换为可直接观察到的屋檐雨滴，保留其余原稿。",
                                        "disposition": "OPEN", "issue_kind": "CHANGE_REQUEST"}]
            return reply
        server.packet_candidate = respond
        generate = adapter.generate
        adapter.generate = lambda packet: replace(generate(packet), actual_amount=Decimal(0), currency="USD")
        native_fixture = NativeReading(kernel, "valid")
        native_fixture.respond = respond
        native = RuntimeModelAdapter(providers.Profile("codex-cli", "gpt-6.1-sol", "NF_SYNTHETIC_NO_KEY", 1024,
            auth_mode="host-login", model_output_ceiling=1024), HostJournal(kernel))
        monkeypatch.setattr(native, "check_available", lambda: None)
        monkeypatch.setattr(native, "generate", native_fixture.generate)
        gateway = AgentGateway(kernel, {"fixture": adapter, "native": native}, material_reader=production_material_reader(kernel))
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
        changed = defect != "uncertain"
        diagnostic = [(node["stage"], node["state"], node.get("reason"))
                      for node in board["nodes"].values() if node["state"] == "STOPPED"]
        assert len(texts) == (2 if changed else 1), diagnostic
        assert writer.get("revision_count", 0) == int(changed) and len(writer.get("history", [])) == int(changed)
        with kernel.db.uow(write=False) as repo:
            task = repo.one("tasks", {"task_id": writer["task_id"]})
            draft = kernel.artifact(repo, writer["output_ref"], work)["payload"]
            assert task["task_kind"] == ("targeted_revision" if changed else "scene_draft")
            if changed:
                assert draft["prior_draft_ref"] == writer["history"][0]["output_ref"]
        assert len(verifications) == int(not changed)
        if defect == "facts":
            states = [state for item in factual_queries[0]["result"]["structuredContent"]["items"]
                      for state in item["payload"]["state_items"]]
            assert any(state["subject_id"] == f["ids"]["sword"] and state["answer"]["value_status"] == "VALUE"
                       and state["answer"]["data"] == entity_value(f["ids"]["smith"]) for state in states)
        assert state["frozen_chapters"] == int(defect != "exhausted"), events
