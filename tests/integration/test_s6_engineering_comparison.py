"""Three preregistered offline windows on isolated NF and Workshop PostgreSQL services."""

import json
import os
import platform
import random
import re
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from time import perf_counter_ns
from types import SimpleNamespace
from uuid import uuid4

import pytest

if not os.environ.get("AW_DUAL_TEST_NF_ADMIN_URL"):
    pytest.skip("S6 requires separate NF and Workshop PostgreSQL clusters", allow_module_level=True)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "workshop" / "integration"))
from adaptive_helpers import setup_adaptive_case
from editorial_helpers import binding_for
from test_adaptive_promotion import enroll_case, release_inputs
from test_dynamic_skill_dispatch import (
    OfflineAdapter,
    dynamic_site,  # noqa: F401 -- pytest fixture
)
from test_narrative import assertion, extract
from test_release import accept_draft, setup_batch, write_scene
from test_review_independence import enroll_native_case

from novel_factory.application.adaptive_evaluation import AdaptiveEvaluationService
from novel_factory.application.adaptive_instructions import (
    AdaptiveInstructionService,
    deployed_module_digests,
)
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.creative_review import CreativeReviewService
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.planning import PlanningService
from novel_factory.application.release import ReleaseService
from novel_factory.application.review_independence import ReviewIndependenceService
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import canonical_bytes, fingerprint
from novel_factory.fixtures import point
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig
from novel_factory.infrastructure.model_gateway import ModelResponse
from workshop.integration.conftest import dual_databases  # noqa: F401 -- pytest fixture

pytestmark = [pytest.mark.postgres, pytest.mark.integration]
SEED = 20261111
FLAGS = {
    "BASELINE": CreativeHarnessConfig(),
    "HARNESS": CreativeHarnessConfig(enabled=True),
    "HARNESS_ADAPTIVE": CreativeHarnessConfig(enabled=True, dynamic_agents=True, adaptive_instructions=True),
}
FIRST = "小渡把那盏手提灯举起来，朝江心等了片刻。"
SECOND = "潮水一直没过第一层台阶，岸上没有应声。"
SKILL = "核对灯的先后动作，按原有边界交接。"
PROFILE = "向接手场景的作者指出灯与门闩的动作顺序。"
GOAL = "写完渡口关门前这一场。保留已给的背景与未知处，让最后一个动作结束这场。"
PROMPT_NOTE = "\n上一工序观察（候选材料）："


def digest(value):
    return sha256(value.encode("utf-8")).hexdigest()


def prose(user):
    if "creative_materials" in user:
        return user["creative_materials"]["prose"][0]
    values = (json.loads(x["content"]) for x in user["authorized_materials"])
    return next(v["text"] for v in values if "text" in v and "span" in v)


class SceneFixture:
    """A fixed, inspectable calibration program. It never receives scoring labels or arm names."""

    def generate(self, req):
        system = req.messages[-1]["content"]
        user = json.loads(req.messages[1]["content"])
        identity = json.loads(system.split("\nIdentity: ", 1)[1].splitlines()[0])
        role = identity["role_id"]
        if role == "reviewer":
            source = prose(user)
            lines = re.findall(r"[^。\n]+。", source)
            repeated = next((line for line in lines if len(line) >= 12 and lines.count(line) >= 3), None)
            body = {
                "summary": repeated or "无可定位的重复。",
                "issues": [],
                "counterinterpretations": ["重复可能回应场中人物的犹疑。"],
                "evidence_refs": [],
            }
        else:
            if role == "evidence_maintainer":
                value = (
                    "守灯人抬灯后才放下门闩。"
                    if SKILL in system and PROFILE in system
                    else "桥边的灯还亮着。"
                )
            else:
                assert role == "writer"
                source = prose(user)
                note = user["assignment"].split(PROMPT_NOTE)[1]
                last = source.strip().split("。")[-2].split("\n")[-1]
                value = "\n\n".join(
                    [
                        "小渡隔着芦苇望向旧渡口。前夜有人提过那枚铜印：" + last + "。",
                        FIRST * 3,
                        "他摸到门柱上的绳结，湿麻贴住了拇指。" + note,
                        SECOND * 3,
                        "小渡把钥匙留在灯座下面，合上木门，踩着退潮时露出来的石阶往家里走。",
                    ]
                )
            body = {"text": value, "event_refs": [], "important_deltas": [], "evidence_refs": []}
        output = json.dumps(
            {**identity, "status": "SUCCEEDED", "unresolved_questions": [], "payload": body},
            ensure_ascii=False,
        )
        return ModelResponse(
            text=output,
            provider_receipt="offline:" + req.request_sha256,
            input_tokens=len(canonical_bytes(req.messages)),
            output_tokens=len(output.encode()),
            cached_input_tokens=0,
            actual_amount=Decimal(0),
            currency="USD",
            request_sha256=req.request_sha256,
            raw_response={"execution": "LOCAL_FIXTURE"},
        )


@pytest.fixture
def local_scene_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            packet = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            messages = packet["messages"]
            assert "tools" not in packet
            token = fingerprint(messages)
            result = SceneFixture().generate(SimpleNamespace(messages=messages, request_sha256=token))
            data = canonical_bytes(
                {
                    "id": "s6-fixture:" + token,
                    "choices": [{"message": {"content": result.text}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": result.input_tokens,
                        "completion_tokens": result.output_tokens,
                    },
                }
            )
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    class LocalRoute(OfflineAdapter):
        def generate(self, request):
            response = super().generate(request)
            assert response.provider_receipt == "s6-fixture:" + fingerprint(list(request.messages))
            return replace(response, actual_amount=Decimal(0), currency="USD")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield LocalRoute(
            endpoint=f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            model="s6-scene-calibration-v1",
            api_key="local-no-billing",
            allow_loopback_http=True,
            max_output_tokens=5000,
        )
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


def payload(k, wr, ref):
    with k.db.uow(write=False) as repo:
        return k.artifact(repo, ref, wr["work_id"])["payload"]


def flags_for(s, monkeypatch, flags):
    monkeypatch.setenv("NF_CREATIVE_HARNESS_CONFIG", json.dumps(flags.as_dict()))
    # The S4 delegation provider freezes its gateway's server settings at provisioning.
    s.gateway.creative_harness = flags


def obtain_method(s, training, candidate, tmp_path):
    k = s.kernel
    run = AdaptiveEvaluationService(k).run(
        s.owner, s.wr, candidate_ref=candidate, idempotency_key="measure-s6-training"
    )
    state = release_inputs(training, run)
    enroll_case(state, tmp_path)
    promotion = state["service"]
    until = (datetime.now(UTC) + timedelta(minutes=4)).isoformat()
    verdict = promotion.review(
        state["reviewer"].principal,
        s.wr,
        **state["refs"],
        **state["job"],
        publisher_grant_id=state["publisher"].grant_id,
        verdict="APPROVE",
        reason="采用一正一负留出原文上经过核对的定位方法。",
        valid_until=until,
        idempotency_key="s6-opinion",
    )
    decision = promotion.decide(
        state["publisher"],
        s.wr,
        **state["refs"],
        review_ref=verdict["review_ref"],
        action="APPROVE",
        expected_epoch=0,
        reason="在随后分配的独立审稿岗位使用该版本。",
        valid_until=until,
        idempotency_key="s6-publish",
    )
    with k.db.uow(write=False) as repo:
        evidence = AdaptiveEvaluationService(k).inspect(repo, s.wr, **state["refs"])
    return {
        "proposal_ref": candidate,
        **state["refs"],
        "review_ref": verdict["review_ref"],
        "selection_ref": decision["selection_ref"],
        "arm_totals": evidence["arm_totals"],
        "quality": evidence["quality"],
        "local_engineering_executions": len(evidence["results"]),
        "model_attempts": 0,
    }


def prepare(k, w, gateway, role, kind, assignment, inputs, materials):
    return RuntimeTaskService(k, gateway).prepare(
        w["principal"],
        w["world_ref"],
        role_id=role,
        task_kind=kind,
        story_time=point(1, 1),
        subject_ids=[w["ids"]["sword"]],
        predicates=["owner"],
        assignment=assignment,
        adapter_id="fixture",
        input_artifacts=inputs,
        material_refs=materials,
        token_budget=60000,
        max_amount="1",
        lease_seconds=1200,
    )


def request(job, budget):
    return {
        "schema_version": "2.1",
        "task_id": job.task["task_id"],
        "generation": job.task["generation"],
        "worker_id": job.worker_id,
        "input_ref": job.agent_input["input_ref"],
        "budget_id": budget,
        "call_key": "s6:" + job.task["task_id"],
        "reserve_amount": "1",
        "token_reservation": 60000,
    }


def invoke(s, gateway, principal, req, role, row):
    call = {
        "role": role,
        "task_id": req["task_id"],
        "generation": req["generation"],
        "input_ref": req["input_ref"],
        "status": "DISPATCHING",
    }
    row["stages"].append(call)
    receipt = gateway.execute(principal, req)
    audit = payload(s.kernel, s.wr, receipt["evidence_ref"])
    call.update(
        {
            "status": receipt["validation_status"],
            "evidence_ref": receipt["evidence_ref"],
            **{
                k: audit.get(k)
                for k in (
                    "attempt_id",
                    "request_sha256",
                    "prompt_ref",
                    "response_ref",
                    "output_ref",
                    "elapsed_seconds",
                    "creative_context_ref",
                    "dynamic_assignment_ref",
                    "adaptive_instruction_ref",
                    "usage",
                )
            },
        }
    )
    user_input = payload(s.kernel, s.wr, call["input_ref"])
    call["input"] = {
        k: user_input[k]
        for k in (
            "role_id",
            "task_kind",
            "runtime_ref",
            "runtime_sha256",
            "model_config_sha256",
            "output_contract",
            "creative_harness",
            "dynamic_assignment_ref",
            "adaptive_instruction_ref",
        )
        if k in user_input
    }
    prompt = payload(s.kernel, s.wr, call["prompt_ref"])
    call["visible_user_sha256"] = digest(prompt["messages"][1]["content"])
    call["visible_system_sha256"] = digest(prompt["messages"][0]["content"])
    assert call["status"] == "VALIDATED_CANDIDATE", call["status"]
    return payload(s.kernel, s.wr, call["output_ref"]), receipt["output_ref"]


def window(s, w, prior, arm, instance, selected, tmp_path, monkeypatch, row):
    k, wr = s.kernel, s.wr
    flags = FLAGS[arm]
    flags_for(s, monkeypatch, flags)
    adaptive = flags.adaptive_instructions
    specialist = s.delegate("s6-" + arm.lower(), instance if adaptive else None)
    summary, summary_ref = invoke(
        s, s.gateway, specialist.principal, specialist.request, "evidence_maintainer", row
    )
    narrative = NarrativeService(k)
    gw = AgentGateway(
        k,
        {"fixture": s.gateway.adapters["fixture"]},
        material_reader=narrative.read_agent_material,
        creative_harness=flags,
    )
    scene = w["scene_refs"][5]
    job = prepare(
        k,
        w,
        gw,
        "writer",
        "scene_draft",
        GOAL + PROMPT_NOTE + summary["text"],
        [w["plan_ref"], w["batch_ref"], summary_ref],
        [scene, prior["draft_ref"]],
    )
    tid, gen = job.task["task_id"], job.task["generation"]
    PlanningService(k).bind_task_for_controller(
        s.owner, tid, gen, w["plan_ref"], w["scenes"][5]["plan_node_ids"], "plan:" + tid
    )
    ReleaseService(k).bind_writer_task(job.principal, tid, gen, scene, [], "writer:" + tid)
    narrative.bind_reader_task(s.owner, tid, gen, [prior["draft_ref"]], "read:" + tid)
    output, outref = invoke(s, gw, job.principal, request(job, s.budget_id), "writer", row)
    text = output["text"]
    draft = narrative.store_draft(
        job.principal, tid, gen, scene, text, "draft:" + tid, handoff_refs=[], agent_output_ref=outref
    )
    row.update(draft_ref=draft, manuscript=text, manuscript_sha256=digest(text), summary=summary["text"])
    # Freeze a candidate and check it at the native reconciliation port. Rival arms never replace one another's scene.
    item = {
        "writer": job.principal,
        "task": job.task,
        "scene": w["scenes"][5],
        "text": text,
        "draft_ref": draft,
    }
    extraction = extract(
        k, w, item, [assertion(text, summary="一个完整的离场动作；该工程材料不提交持久世界改动。")]
    )
    reconciliation = narrative.reconcile(s.owner, draft, extraction["extraction_ref"], wr, "reconcile:" + tid)
    row["reconciliation"] = reconciliation
    reader = prepare(
        k,
        w,
        gw,
        "reviewer",
        "literary",
        "找出有原文依据的重复，同时容许有意的复沓。",
        [reconciliation["reconciliation_ref"]],
        [draft],
    )
    rid, rg = reader.task["task_id"], reader.task["generation"]
    narrative.bind_reader_task(s.owner, rid, rg, [draft], "r:" + rid)
    review, _ = invoke(s, gw, reader.principal, request(reader, s.budget_id), "reviewer", row)
    sent = review["summary"]
    starts = [match.start() for match in re.finditer(re.escape(sent), text)]
    assert len(starts) >= 3
    spans = [{"start": starts[2], "end": starts[2] + len(sent), "pattern": "MECHANICAL_CADENCE"}]
    if flags.enabled:
        # The gateway has committed the candidate and closed its host lease. The
        # separately enrolled advisory reader receives that exact candidate and draft.
        guide = prepare(
            k,
            w,
            gw,
            "reviewer",
            "literary",
            "核查上一步实际标出的原句，并应用本次分配冻结的审阅办法。",
            [reconciliation["reconciliation_ref"], row["stages"][-1]["output_ref"]],
            [draft],
        )
        rid, rg = guide.task["task_id"], guide.task["generation"]
        reader = guide
        narrative.bind_reader_task(s.owner, rid, rg, [draft], "s6-advisory:" + rid)
        row["advisory_task_id"] = rid
        row["advisory_input_ref"] = reader.agent_input["input_ref"]
        review_service = CreativeReviewService(k)
        case = {"manuscript_ref": draft, "reviewer_task_id": rid, "reviewer_generation": rg}
        enroll_native_case(k, tmp_path, monkeypatch, **case, controller=s.owner)
        proof = ReviewIndependenceService(k).issue(s.owner, **case)["independence_ref"]
        reading = review_service.read(
            reader.principal, rid, rg, draft_ref=draft, idempotency_key="s6", worker_id=reader.worker_id
        )
        binding = binding_for(
            w,
            [
                {
                    "draft_ref": draft,
                    "acceptance_ref": None,
                    "reconciliation_ref": reconciliation["reconciliation_ref"],
                }
            ],
        )
        binding["ordered_acceptance_refs"] = []
        note = {
            **spans[0],
            "reason": "三处句子完全相同。",
            "disposition": "PREFERENCE",
            "counterinterpretation": "重复也可能承担哀歌的节奏。",
            "narrative_function": "定位人物等待中的重复动作。",
            "requested_change": "核对第三处是否需要新的动作。",
            "related_spans": [
                {"text_ref": reading["text_ref"], "start": p, "end": p + len(sent)} for p in starts[:2]
            ],
        }
        advice = review_service.review(
            reader.principal,
            rid,
            rg,
            binding=binding,
            target_ref=draft,
            scope_draft_refs=[draft],
            coverage_refs=[reading["coverage_ref"]],
            layer="PROSE",
            observations=[note],
            rework_limit=1,
            quota={
                "budget_id": s.budget_id,
                "currency": "USD",
                "max_amount": "1",
                "max_tokens": 60000,
                "max_attempts": 3,
                "deadline": (datetime.now(UTC) + timedelta(minutes=8)).isoformat(),
            },
            independence_ref=proof,
            idempotency_key="s6-review",
            worker_id=reader.worker_id,
        )
        row["advisory_ref"] = advice["report_ref"]
        spans = [
            {"pattern": f["pattern"], "start": f["span"]["start"], "end": f["span"]["end"]}
            for f in advice["findings"]
        ]
    row["review_spans"] = spans
    assignments = [st["input"] for st in row["stages"]]
    assert bool(assignments[0].get("dynamic_assignment_ref")) == adaptive
    assert assignments[2].get("adaptive_instruction_ref") == (selected if adaptive else None)
    assert all(("creative_harness" in item) == flags.enabled for item in assignments)
    row["status"] = "COMPLETE"


def save(path, report):
    if path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n")
        tmp.replace(path)


def official_selection(repo, world):
    work = world["work_id"]
    edition = repo.many(
        "artifacts",
        {"work_id": work, "kind": ["NARRATIVE_ACCEPTANCE", "RELEASE_RECORD", "RELEASE_MANIFEST"]},
        order=["artifact_revision_id"],
    )
    events = repo.many("events", {"work_id": work}, order=["event_version_id"])
    return {
        "interpretation_epoch": repo.one("works", {"work_id": work})["interpretation_epoch"],
        "event_count": len(events),
        "selected_event_versions_sha256": fingerprint([e["event_version_id"] for e in events]),
        "official_artifacts_sha256": fingerprint(
            [[r["kind"], r["artifact_revision_id"], r["sha256"]] for r in edition]
        ),
    }


def test_frozen_three_arm_complete_scene(request, local_scene_server, tmp_path, monkeypatch, record_property):
    s = request.getfixturevalue("dynamic_site")
    k, wr = s.kernel, s.wr
    s.gateway.adapters["fixture"] = local_scene_server
    enrolled_at = perf_counter_ns()
    # The historical training reports all belong to other manuscripts. No measured role
    # receives these source labels, blind allocation or the scorer's expected substrings.
    w = setup_batch(k, fixture=s.world, count=6)
    training = setup_adaptive_case(k, tmp_path, monkeypatch, world=w)
    flags_for(s, monkeypatch, FLAGS["HARNESS_ADAPTIVE"])
    raw = (ROOT / "evidence/p2/batch3/short-volume/run/release.txt").read_text().split("\n\n")
    prior_text = "\n\n".join(raw[:3]).strip()
    prior = accept_draft(k, w, write_scene(k, w, 4, prior_text))
    common_history_ns = perf_counter_ns() - enrolled_at
    authoring_at = perf_counter_ns()
    candidate = AdaptiveInstructionService(k).propose(s.owner, wr, **training["args"])
    skill = s.share("s6-w4", SKILL)
    registrar = s.delegate("s6-registrar")
    # Registration produces a versioned, immutably selected employee; no model is called here.
    revision = s.activate(registrar, skill, SKILL, "s6-profile", role_text=PROFILE)
    authoring_ns = perf_counter_ns() - authoring_at

    def put(data):
        return k.put_artifact(s.owner, wr["work_id"], data, "EVIDENCE")

    shared = {
        "world_ref": wr,
        "plan_ref": w["plan_ref"],
        "batch_ref": w["batch_ref"],
        "scene_ref": w["scene_refs"][5],
        "adjacent_draft_ref": prior["draft_ref"],
        "adjacent_sha256": digest(prior_text),
        "original_source_sha256": sha256(
            (ROOT / "evidence/p2/batch3/short-volume/run/release.txt").read_bytes()
        ).hexdigest(),
        "goal": GOAL,
        "stages": ["evidence_maintainer/summary", "writer/scene_draft", "reviewer/literary"],
        "target_story_time": point(1, 1),
        "subject_id": w["ids"]["sword"],
        "reader_cut_ordinal": 5,
        "world_effects_permitted": False,
        "outcome": "validated draft and checked review; official edition unchanged",
    }
    controls = {
        arm: {
            **shared,
            "flags": flags.as_dict(),
            "fixture_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
            **(
                {
                    "employee_revision": revision,
                    "instruction_proposal": candidate,
                    "skill_source": {
                        key: skill[key] for key in ("skill_id", "source_revision", "content_sha")
                    },
                }
                if flags.dynamic_agents
                else {}
            ),
        }
        for arm, flags in FLAGS.items()
    }
    codes = [str(uuid4()) for _ in FLAGS]
    rng = random.Random(SEED)
    rng.shuffle(codes)
    allocation = [[a, blind] for a, blind in zip(FLAGS, codes, strict=True)]
    rng.shuffle(allocation)
    labels = {
        "known_calibration_anchors": [FIRST, SECOND],
        "expected_occurrence": 3,
        "negative_control": "No serialization identifiers may appear in manuscript prose.",
        "reader_judgments": 0,
    }
    rubric = {
        "sha256_of_sealed_labels": fingerprint(labels),
        "judgment": "one true positive for exact third copy, count all extra or missing findings, preserve equality",
        "independent_batch": False,
        "interpretation": "calibration only; the fixture intentionally generates the preregistered motifs",
    }
    quota = {
        "budget_id": s.budget_id,
        "currency": "USD",
        "max_amount": "9",
        "max_tokens": 540000,
        "max_attempts": 9,
        "deadline": (datetime.now(UTC) + timedelta(minutes=24)).isoformat(),
    }
    budget_policy = {
        "per_role_attempts": 1,
        "reserve_amount": "1",
        "per_call_allowance": 60000,
        "retries": 0,
        "parallel_arms": 1,
        "rework_limit": 0,
        "fixture_token_unit": "UTF-8 bytes (synthetic NF metering); billed provider tokens zero",
        "paid_provider_spend": "0",
        "optimization_quota": training["args"]["quota"],
    }
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    cache = {
        "shared_input_ref": put(shared),
        "case_manifest_ref": put(
            {
                "works": [wr],
                "original_inputs": [shared],
                "calibration_batches": 1,
                "confirmatory_batches": 0,
                "labels_ref": put(labels),
            }
        ),
        "budget_policy_ref": put(budget_policy),
        "rubric_ref": put(rubric),
        "blind_allocation_ref": put({"allocation": allocation, "seed": SEED}),
    }
    freeze = {
        "schema_version": "1.0",
        "kind": "EXPERIMENT_FREEZE",
        "experiment_id": str(uuid4()),
        "frozen_at": datetime.now(UTC).isoformat(),
        "source_commit": source,
        "works": [wr],
        **cache,
        "model_config_sha256": fingerprint(local_scene_server.configuration),
        "quota": quota,
        "order_seed": SEED,
        "repetitions": 1,
        "arms": [
            {
                "arm_id": a,
                "candidate_ref": put(controls[a]),
                "creative_harness": FLAGS[a].as_dict()["creative_harness"],
            }
            for a in FLAGS
        ],
        "sampling": "ALL_PREREGISTERED_CASES_INCLUDING_FAILURES",
        "design": "THREE_ARM",
        "metrics": {
            **dict.fromkeys(
                ("engineering", "review_effectiveness", "tokens", "cost", "latency", "rework"), "REQUIRED"
            ),
            "literary": "INDEPENDENT_READING_OR_UNPROVEN",
        },
    }
    validate_contract("ExperimentFreeze", freeze, "harness")
    ref = put(freeze)
    out = os.environ.get("NF_S6_COMPARISON_OUTPUT")
    rows = [
        {"arm": a, "blind_id": blind, "status": "NOT_RUN", "stages": [], "attempts": []}
        for a, blind in allocation
    ]
    runtime_hashes = {
        r: fingerprint(s.gateway.catalog.snapshot(r, mode))
        for r, mode in [
            ("writer", "scene_draft"),
            ("reviewer", "literary"),
            ("evidence_maintainer", "summary"),
        ]
    }
    experiment = {
        "freeze_ref": ref,
        "freeze": freeze,
        "shared_input": shared,
        "source_locator": {
            "path": "evidence/p2/batch3/short-volume/run/release.txt",
            "paragraphs": [1, 2, 3],
            "sha256": digest(prior_text),
        },
        "budget_policy": budget_policy,
        "rubric": rubric,
        "allocation": allocation,
        "arm_sources": controls,
        "runtime_sha256": runtime_hashes,
        "source_module_sha256": deployed_module_digests(),
        "runtime": {"python": platform.python_version(), "platform": platform.platform()},
        "config": local_scene_server.configuration,
        "calibration_batches": 1,
        "confirmatory_batches": 0,
        "literary": "NOT_EVALUATED",
        "real_model_calls": 0,
        "paid_cost_usd": "0",
        "planned_windows": len(rows),
        "rows": rows,
    }
    save(out, experiment)
    with k.db.uow(write=False) as repo:
        assert not repo.many("call_attempts", {"task_id": registrar.task_id})
        world_before = k.head(repo, wr["work_id"], wr["branch_id"])
        experiment["official_state_before"] = official_selection(repo, wr)
    for row in rows:
        start = perf_counter_ns()
        try:
            if datetime.now(UTC) >= datetime.fromisoformat(quota["deadline"]):
                row["status"] = "DEADLINE_EXCEEDED"
                continue
            if row["arm"] == "HARNESS_ADAPTIVE":
                flags_for(s, monkeypatch, FLAGS[row["arm"]])
                t = perf_counter_ns()
                experiment["optimization"] = obtain_method(s, training, candidate["candidate_ref"], tmp_path)
                experiment["optimization"]["evaluation_and_promotion_latency_ns"] = perf_counter_ns() - t
            selection = experiment.get("optimization", {}).get("selection_ref")
            window(s, w, prior, row["arm"], revision["instance_ref"], selection, tmp_path, monkeypatch, row)
        except Exception as exc:  # noqa: BLE001 -- retain every preregistered failed arm
            row["status"] = "FAILED"
            row["error"] = {
                "type": type(exc).__name__,
                "code": getattr(exc, "code", None),
            }
        finally:
            row["elapsed_ns"] = perf_counter_ns() - start
            with k.db.uow(write=False) as repo:
                tids = [stage["task_id"] for stage in row["stages"]]
                attempts = repo.many("call_attempts", {"task_id": tids}) if tids else []
                row["attempts"] = [
                    {
                        key: str(a[key]) if a[key] is not None else None
                        for key in (
                            "attempt_id",
                            "task_id",
                            "generation",
                            "state",
                            "actual_amount",
                            "actual_tokens",
                        )
                    }
                    for a in attempts
                ]
                row["world_ref_after"] = k.head(repo, wr["work_id"], wr["branch_id"])
                row["official_state_after"] = official_selection(repo, wr)
            save(out, experiment)
    # Decode blind IDs and unseal labels only after every window has a terminal state.
    experiment["unsealed_labels"] = {
        "sha256": fingerprint(labels),
        "expected_count": len(labels["known_calibration_anchors"]),
        "expected_occurrence": labels["expected_occurrence"],
        "reader_judgments": 0,
    }
    for row in rows:
        if "manuscript" not in row:
            continue
        text = row["manuscript"]
        expected = set()
        for line in labels["known_calibration_anchors"]:
            positions = [m.start() for m in re.finditer(re.escape(line), text)]
            if len(positions) >= 3:
                expected.add((positions[2], positions[2] + len(line)))
        observed = {(x["start"], x["end"]) for x in row.get("review_spans", [])}
        row["quality"] = {
            "expected": len(expected),
            "true_positive": len(expected & observed),
            "false_negative": len(expected - observed),
            "false_positive": len(observed - expected),
            "control_fields_in_prose": sum(
                text.count(x) for x in ("task_id", "world_ref", "grant_id", "artifact_revision_id")
            ),
            "rework": 0,
            "native_narrative_accepted": False,
            "literary": "NOT_EVALUATED",
        }
    attestations = []
    for line in s.wire.read_text().splitlines():
        message = json.loads(line)
        command = message.get("params", {})
        if command.get("name") == "aw_dynamic_skill_attest":
            args = command["arguments"]
            attestations.append(
                {
                    "task_id": (args.get("task") or {}).get("task_id"),
                    "freeze_task": args["freeze_task"],
                    "binding_id_sha256": digest(args["binding_id"]),
                }
            )
    experiment["workshop_attestations"] = attestations
    experiment["public_data"] = (
        "Synthetic arm outputs, immutable refs, source digests and objective results. No original source corpus or bearer credentials."
    )
    experiment["common_training_preparation_latency_ns"] = common_history_ns
    experiment["adaptive_proposal_registration_and_selection_latency_ns"] = authoring_ns
    experiment["total_nf_attempts"] = sum(len(row["attempts"]) for row in rows)
    for row in rows:
        row["local_meter_units"] = sum(int(a["actual_tokens"] or 0) for a in row["attempts"])
        row["actual_amount_usd"] = format(
            sum(
                (Decimal(a["actual_amount"]) for a in row["attempts"] if a["actual_amount"] is not None),
                Decimal(0),
            ),
            "f",
        )
        row["unsettled_attempts"] = sum(a["actual_amount"] is None for a in row["attempts"])
    experiment["arm_durations_include"] = (
        "All measured runtime cases include preparation, delegation and review. The adaptive window also includes its evaluation and approval; preregistration authoring latency is listed separately."
    )
    experiment["complete_windows"] = sum(row["status"] == "COMPLETE" for row in rows)
    experiment["completed_at"] = datetime.now(UTC).isoformat()
    save(out, experiment)
    record_property("s6_three_arm_comparison", json.dumps(experiment, ensure_ascii=False, sort_keys=True))
    assert all(row["status"] == "COMPLETE" for row in rows), [(row["arm"], row.get("error")) for row in rows]
    assert any(
        a["task_id"] == next(r for r in rows if r["arm"] == "HARNESS_ADAPTIVE")["stages"][0]["task_id"]
        and a["freeze_task"]
        for a in attestations
    )
    assert all(
        len(row["attempts"]) == 3
        and row["world_ref_after"] == world_before
        and row["official_state_after"] == experiment["official_state_before"]
        for row in rows
    )
    assert all({a["state"] for a in row["attempts"]} == {"SUCCEEDED"} for row in rows)
    by_arm = {r["arm"]: r for r in rows}
    assert by_arm["BASELINE"]["manuscript"] == by_arm["HARNESS"]["manuscript"]
    assert by_arm["HARNESS_ADAPTIVE"]["manuscript"] != by_arm["HARNESS"]["manuscript"]
    assert [by_arm[a]["quality"]["true_positive"] for a in FLAGS] == [1, 1, 2]
    assert all(
        r["quality"]["false_positive"] == 0
        and r["quality"]["control_fields_in_prose"] == 0
        and r["reconciliation"]["status"] == "CLEAR"
        for r in rows
    )
