"""Scoped instruction proposals and experimental preregistration from signed NF reading evidence."""

from __future__ import annotations

import json
import os
import re
import subprocess
from hashlib import sha256
from pathlib import Path
from random import Random
from uuid import UUID, uuid4, uuid5

from novel_factory.application.authorization import Principal
from novel_factory.application.creative_context import runtime_provenance
from novel_factory.application.creative_review import (
    CreativeReviewService,
    _artifact,
    _derived,
    _identity,
    _latest_decision,
    _quota,
    _review_history,
)
from novel_factory.application.editorial import ReviewService
from novel_factory.application.kernel import require
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.semantic_dependencies import check_task_dependencies
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.creative_review import RUBRICS
from novel_factory.domain.errors import DomainError
from novel_factory.domain.temporal import point_key
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig

MATERIAL_SECTIONS = (
    "beliefs",
    "facts",
    "hypotheses",
    "uncertainties",
    "prose",
    "style",
    "scene",
    "continuity",
    "conditional_plans",
    "evidence",
    "reading",
)
CHECKS = frozenset(pattern for patterns in RUBRICS.values() for pattern in patterns)
CHANGES = ("ROLE_INSTRUCTIONS", "SKILL_INSTRUCTIONS", "CONTEXT_RECIPE", "REVIEW_RUBRIC")
CRAFT = {
    "UNUSED_EXPOSITION": "只在眼前的选择需要时引入设定，保留对人物确有意义的说明。",
    "ENGINEERING_SERIALIZATION": "将规则的后果落实到人物看见的细节，允许人物在必要时使用行话。",
    "REPEATED_NAME_FORM": "借助称呼来源、身份或音节区分容易混淆的人名，并保留有意的家族命名。",
    "MOTIVATION_GAP": "在选择改变之前寻找新消息、代价或承诺，保留人物可能误解的空间。",
    "MISSING_CONSEQUENCE": "跟踪一次选择如何改变后续人物可做的事，不将有意悬置误判为遗漏。",
    "UNBRIDGED_MOVEMENT": "核对人物在场景边界的位置与赶路所需的时间，允许有提示的跳切。",
    "SHARED_VOICE": "比较两位说话者在相同压力下的措辞和回避方式，保留相互模仿的可能。",
    "EMOTION_AS_RULE": "在下一次行动里检查情绪如何影响目标，保留叙述者直接命名感受的自由。",
    "REPEATED_IMAGE": "追踪同一意象再次出现时意义是否改变，保留有意的复沓。",
    "MECHANICAL_CADENCE": "比较重复句在前后三处的功能，只调整没有新作用的那一次，保留哀歌或祷词的节奏。",
    "EXCESS_SUMMARY": "检查解释与行动是否重复承担同一作用，保留有清晰叙述目的的概述。",
    "UNBRIDGED_SEAM": "让上一场结尾的位置、所知或选择能与下一场开头相接。",
    "REPEATED_SEAM": "检查相邻章节边界是否无意重述，保留改变阅读角度的回望。",
    "DROPPED_PROMISE": "核对早先承诺在卷尾的落点，区分遗忘和有意留下的未决问题。",
    "REPEATED_BEAT": "比较重复情节的代价、选择或后果是否发生变化。",
    "WORLD_CONTRADICTION": "将叙述者的事实断言与获准的世界证据核对，并区别传闻和人物判断。",
    "POV_LEAK": "检查行动所依赖的消息是否已到达该人物，允许人物依据错误信念作出选择。",
}
ROLE_FOCUS = {
    "world_designer": "WORLD_DESIGN",
    "volume_director": "PLOT_DIRECTION",
    "environment_director": "STAGING",
    "character_actor": "ACTOR_BEHAVIOR",
    "writer": "PROSE",
    "literary_editor": "LITERARY_EDITING",
    "reviewer": "REVIEW_METHOD",
    "chief_editor": "EDITORIAL_METHOD",
    "evidence_maintainer": "CONTEXT",
    "adjudication_advisor": "ADJUDICATION",
}


def _ref(row):
    return {key: row[key] for key in ("artifact_id", "artifact_revision_id", "sha256")}


def _read(kernel, repo, wr, ref, kind, *, branch_only=False):
    value = kernel.artifact(repo, ref, wr["work_id"], kind=kind)["payload"]
    scope = value["scope"]["world_ref"]
    same_scope = (
        all(scope[k] == wr[k] for k in ("work_id", "branch_id")) if branch_only else scope == wr
    )
    require(
        fingerprint(value) == ref["sha256"] and same_scope,
        "VIEW_DENIED",
        "Instruction evidence is outside its frozen work and revision",
    )
    return value


def load_instruction(kernel, repo, world_ref, instruction_ref):
    item = _read(
        kernel, repo, world_ref, instruction_ref, "CREATIVE_INSTRUCTION_TEXT", branch_only=True
    )
    require(
        sha256(item["body"].encode()).hexdigest() == item["instruction_sha256"],
        "OBJECT_CORRUPT",
        "Instruction content differs from its immutable digest",
    )
    return item


def _controller(kernel, repo, controller, wr):
    grant = kernel.authorize(repo, controller, wr["work_id"], "ADMIN", wr["branch_id"])
    require(
        grant["role_id"] == "control_operator",
        "VIEW_DENIED",
        "An authorized instruction controller is required",
    )
    kernel.revision(repo, wr)
    return grant


def _target(kernel, repo, wr, task_id, generation):
    row = repo.one(
        "tasks", {"work_id": wr["work_id"], "branch_id": wr["branch_id"], "task_id": task_id}
    )
    require(row, "VIEW_DENIED", "Instruction source task is outside the selected branch")
    owner = Principal(row["grant_id"], row["principal_id"])
    task, grant, spec = WorldQueryService(kernel)._task(repo, owner, task_id, generation)
    require(
        spec["world_ref"] == wr and grant["role_id"] == task["role_id"],
        "VIEW_DENIED",
        "Target runtime scope differs",
    )
    check_task_dependencies(repo, task_id, generation)
    rows = repo.many(
        "artifacts",
        {
            "work_id": wr["work_id"],
            "kind": "AGENT_INPUT",
            "artifact_revision_id": [r["artifact_revision_id"] for r in spec["input_artifacts"]],
        },
    )
    matches = [
        r
        for r in rows
        if (r["payload"]["role_id"], r["payload"]["task_kind"], r["payload"]["runtime_sha256"])
        == (task["role_id"], task["task_kind"], spec["runtime_contract_version"])
    ]
    require(
        len(matches) == 1,
        "INVALID_EVIDENCE",
        "Instruction target requires one exact frozen AgentInput",
    )
    data = matches[0]["payload"]
    runtime = kernel.artifact(repo, data["runtime_ref"], wr["work_id"], kind="AGENT_RUNTIME")[
        "payload"
    ]
    require(
        fingerprint(runtime) == spec["runtime_contract_version"]
        and fingerprint(data) == matches[0]["sha256"],
        "OBJECT_CORRUPT",
        "Runtime provenance mismatch",
    )
    return task, grant, spec, data, runtime


def _labels(report, details):
    labels = []
    for finding, note in zip(report["findings"], details["findings"], strict=True):
        span = finding["text_span"]
        labels.append(
            {
                "pattern": note["pattern"],
                "judgment": finding["judgment"],
                "severity": finding["severity"],
                "start": span["start"],
                "end": span["end"],
                "finding_id": note["finding_id"],
                "upstream_roles": note["upstream_roles"],
                "reason": finding["reason"],
                "counterinterpretation": note["counterinterpretation"],
                "requested_change": finding["requested_change"],
            }
        )
    return labels


def _related_world(repo, original, current):
    return original == current or (
        original["work_id"] == current["work_id"]
        and original["branch_id"] == current["branch_id"]
        and repo.ancestor(original, current)
    )


def _material_map(value):
    if isinstance(value, dict):
        return value
    require(
        isinstance(value, list)
        and all(
            isinstance(item, dict)
            and set(item) == {"section", "items"}
            and item["section"] in MATERIAL_SECTIONS
            and isinstance(item["items"], list)
            for item in value
        ),
        "INVALID_EVIDENCE",
        "Malformed released material ordering",
    )
    mapped = {item["section"]: item["items"] for item in value}
    require(
        len(mapped) == len(value),
        "INVALID_EVIDENCE",
        "Duplicated material section in the frozen packet",
    )
    return mapped


def _writer_history(kernel, repo, row, task, report, details):
    """Allow exactly the selection transition made by accepting this signed writer output."""
    from .creative_context_store import _check_sources

    work, tid, gen = task["work_id"], task["task_id"], task["generation"]
    snap = row["payload"]
    ref = {key: row[key] for key in ("artifact_id", "artifact_revision_id", "sha256")}
    draft_ref = report["target_ref"]
    draft = kernel.artifact(repo, draft_ref, work, kind="NARRATIVE_DRAFT")["payload"]
    require(
        fingerprint(snap) == ref["sha256"]
        and fingerprint(draft) == draft_ref["sha256"]
        and task["state"] == "ACCEPTED"
        and task["role_id"] == "writer"
        and draft["task_id"] == tid
        and draft["generation"] == gen
        and draft["agent_output_ref"]
        and draft["text_ref"] == details["text_ref"]
        and snap["packet"]["scope"]
        == {"world_ref": draft["input_world_ref"], "task_id": tid, "generation": gen}
        and snap["packet"]["context"]["context_bundle_id"]
        == draft["context_bundle_id"]
        == task["context_bundle_id"],
        "TASK_REBIND_REQUIRED",
        "The changed selection is outside this signed writer result",
    )
    result = repo.one("task_results", {"result_id": task["current_result_id"]})
    require(
        result
        and result["disposition"] == "CURRENT"
        and result["task_id"] == tid
        and result["generation"] == gen
        and fingerprint(result["payload"]) == result["payload_hash"]
        and draft["agent_output_ref"] in result["payload"]["artifacts"],
        "TASK_REBIND_REQUIRED",
        "The reviewed draft has no current model result",
    )
    audit_ref = result["payload"]["artifacts"][0]
    audit = kernel.artifact(repo, audit_ref, work, kind="AGENT_CALL_EVIDENCE")["payload"]
    require(
        fingerprint(audit) == audit_ref["sha256"]
        and audit["task_id"] == tid
        and audit["generation"] == gen
        and audit["input_ref"] == snap["input_ref"]
        and audit.get("creative_context_ref") == ref
        and audit["outcome"] == "VALIDATED_CANDIDATE"
        and audit["output_ref"] == draft["agent_output_ref"],
        "TASK_REBIND_REQUIRED",
        "The accepted output was generated from a different context",
    )
    scene = kernel.artifact(repo, draft["scene_ref"], work, kind="SCENE_PLAN")["payload"][
        "scene_contract"
    ]
    key = "NARRATIVE_SCENE:" + scene["scene_id"]
    fences = snap["source_versions"]
    prior = next((entry for entry in fences["selections"] if entry["key"] == key), None)
    now = repo.one("selections", {"work_id": work, "selection_kind": key})
    require(
        prior and now and now["epoch"] == (prior["epoch"] or 0) + 1,
        "TASK_REBIND_REQUIRED",
        "A separate literary selection changed",
    )
    acceptance_ref = now["artifact_ref"]
    accepted = kernel.artifact(repo, acceptance_ref, work, kind="NARRATIVE_ACCEPTANCE")["payload"]
    require(
        fingerprint(accepted) == acceptance_ref["sha256"]
        and accepted["draft_ref"] == draft_ref
        and accepted["text_ref"] == draft["text_ref"]
        and accepted["scene_ref"] == draft["scene_ref"]
        and accepted["world_ref"] == report["scope"]["world_ref"]
        and accepted["task_id"] == tid
        and accepted["generation"] == gen,
        "TASK_REBIND_REQUIRED",
        "The scene now selects another manuscript",
    )
    if prior["ref"]:
        previous = kernel.artifact(repo, prior["ref"], work, kind="NARRATIVE_ACCEPTANCE")["payload"]
        require(
            previous["draft_ref"] == draft["prior_draft_ref"],
            "TASK_REBIND_REQUIRED",
            "Writer lineage changed",
        )
    checked = {
        **fences,
        "selections": [
            {"key": key, "ref": acceptance_ref, "epoch": now["epoch"]} if x["key"] == key else x
            for x in fences["selections"]
        ],
        "draft_ids": [*fences["draft_ids"], draft_ref["artifact_revision_id"]],
    }
    _check_sources(kernel, repo, task, checked, snap["input_ref"])
    return ref, snap


def _case_materials(kernel, repo, wr, target, runtime, report, details):
    from .creative_context_store import load_snapshot

    target_grant = repo.one("grants", {"grant_id": target["grant_id"]})
    target_context = (
        repo.one("contexts", {"context_bundle_id": target["context_bundle_id"]})
        if target["context_bundle_id"]
        else None
    )
    refs = [details["source_versions"]["writer"], report["scope"]]
    refs.extend(
        details["upstream_instruction_versions"].get(target["role_id"], {}).get("traces", [])
    )
    for reference in refs:
        origin = repo.one(
            "tasks",
            {
                "task_id": reference["task_id"],
                "work_id": wr["work_id"],
                "branch_id": wr["branch_id"],
            },
        )
        if (
            not origin
            or not origin["context_bundle_id"]
            or origin["generation"] != reference["generation"]
        ):
            continue
        if (origin["role_id"], origin["task_kind"]) != (target["role_id"], target["task_kind"]):
            continue
        revision = str(
            uuid5(UUID(origin["context_bundle_id"]), "creative-context-snapshot-v1:revision")
        )
        row = repo.one(
            "artifacts",
            {
                "work_id": wr["work_id"],
                "kind": "CREATIVE_CONTEXT_SNAPSHOT",
                "artifact_revision_id": revision,
            },
        )
        if not row or not target_context:
            continue
        if row["payload"].get("dynamic_assignment_ref") and origin["task_id"] != target["task_id"]:
            # A different Workshop task needs its own present employee attestation.
            # Its old delegated prompt cannot supply another assignment's replay.
            continue
        packet = row["payload"]["packet"]
        knowledge = packet["knowledge_cut"]
        if (
            packet["runtime"]["skill_name"] != runtime["skill_name"]
            or knowledge["plane"] != target_context["read_cut"]["knowledge_cut"]["plane"]
            or not _related_world(repo, packet["scope"]["world_ref"], wr)
            or knowledge["plane"] not in target_grant["planes"]
            or point_key(packet["context"]["story_time"])
            > point_key(target_context["payload"]["story_time"])
            or point_key(packet["context"]["story_time"])
            > point_key(target_grant["max_story_time"])
        ):
            continue
        if knowledge["plane"] == "BELIEF" and (
            knowledge["actor_id"] != target_grant["actor_id"]
            or point_key(knowledge["known_at"])
            > point_key(target_context["read_cut"]["knowledge_cut"]["known_at"])
        ):
            continue
        # Only the originating task's scoped, still-authorized delivery is an A/B input.
        owner = Principal(origin["grant_id"], origin["principal_id"])
        _, holder, spec = WorldQueryService(kernel)._task(
            repo, owner, origin["task_id"], origin["generation"]
        )
        ctx = repo.one(
            "contexts",
            {
                "context_bundle_id": origin["context_bundle_id"],
                "task_id": origin["task_id"],
                "generation": origin["generation"],
            },
        )
        require(
            ctx
            and ctx["payload"] == packet["context"]
            and ctx["read_cut"]["knowledge_cut"] == knowledge
            and ctx["read_cut"]["permission_epoch"] == holder["epoch"]
            and ctx["grant_id"] == holder["grant_id"]
            and ctx["principal_id"] == owner.principal_id
            and knowledge["plane"] in holder["planes"]
            and packet["context"]["actor_id"] == holder["actor_id"]
            and row["payload"]["input_ref"] in spec["input_artifacts"]
            and packet["scope"]["world_ref"] == spec["world_ref"]
            and point_key(spec["story_time"]) <= point_key(holder["max_story_time"]),
            "VIEW_DENIED",
            "The historical creative packet exceeds the current holder's original cut",
        )
        check_task_dependencies(repo, origin["task_id"], origin["generation"])
        try:
            context_ref, item = load_snapshot(
                kernel, repo, origin, row["payload"]["input_ref"], required=True
            )
        except DomainError as exc:
            if exc.code != "TASK_REBIND_REQUIRED" or origin["role_id"] != "writer":
                raise
            context_ref, item = _writer_history(kernel, repo, row, origin, report, details)
        material = _material_map(item["model_input"]["creative_materials"])
        # Match raw-free literary sections against S1's immutable source digests.
        section_refs = {}
        for name, contents in material.items():
            hashes = {fingerprint([part]) for part in contents}
            section_refs[name] = _unique(
                [
                    source["evidence"]["artifact_ref"]
                    for source in packet["sources"]
                    if source["content_sha256"] in hashes and "artifact_ref" in source["evidence"]
                ]
            )
        return context_ref, material, packet["token_budget"], section_refs, item["model_input"]
    return None, None, None, {}, None


def _sample(kernel, repo, wr, ref, live, *, remaining_bytes, target, runtime):
    reading = CreativeReviewService(kernel)
    report, details = _review_history(kernel, repo, wr["work_id"], ref)
    require(
        _related_world(repo, report["scope"]["world_ref"], wr) and report["status"] == "ADVISORY",
        "VIEW_DENIED",
        "Optimization requires a scoped advisory report",
    )
    native = reading._current_review(repo, report, details, live)
    decision = _latest_decision(kernel, repo, wr["work_id"], ref)
    outcome = decision["action"] if decision else None
    require(
        outcome != "WITHDRAW",
        "REVIEW_REVOKED",
        "Withdrawn reports cannot qualify instruction evidence",
    )
    labels = _labels(report, details)
    qual = any(
        x["judgment"] in {"PREFERENCE", "CONTRADICTION"} and x["requested_change"] for x in labels
    )
    status = (
        "SUCCESS"
        if outcome in {"RETAIN", "ACCEPT_REVISION"}
        else "FAILURE"
        if qual
        else "OBSERVATION"
        if labels
        else "SUCCESS"
    )
    texts = []
    for draft_ref in details["scope_draft_refs"]:
        draft = kernel.artifact(repo, draft_ref, wr["work_id"], kind="NARRATIVE_DRAFT")["payload"]
        require(
            draft["input_world_ref"]["branch_id"] == wr["branch_id"],
            "VIEW_DENIED",
            "Manuscript outside branch",
        )
        obj = repo.one("objects", {"hash": draft["text_ref"]["sha256"]})
        require(
            obj and obj["size"] <= remaining_bytes,
            "BUDGET_EXCEEDED",
            "Source corpus exceeds frozen input allowance",
        )
        remaining_bytes -= obj["size"]
        text = ReviewService(kernel)._text(repo, draft["text_ref"], wr["work_id"])
        texts.append({"draft_ref": draft_ref, "text_ref": draft["text_ref"], "text": text})
    primary = next(t for t in texts if t["draft_ref"] == report["target_ref"])
    require(
        primary["text_ref"] == details["text_ref"],
        "INVALID_EVIDENCE",
        "Review points at another text",
    )
    context_ref, material, budget, section_refs, role_model_input = _case_materials(
        kernel, repo, wr, target, runtime, report, details
    )
    evidence = [
        e["artifact_ref"]
        for f in report["findings"]
        for e in f["evidence_refs"]
        if "artifact_ref" in e
    ]
    return {
        "case_id": ref["artifact_revision_id"],
        "sample_ref": ref,
        "report_ref": ref,
        **primary,
        "ordered_texts": texts,
        "classification": status,
        "layer": details["layer"],
        "expected_findings": labels,
        "dimension": native["submission"]["dimension"],
        "verified_evidence": details["verified_evidence"],
        "context_evidence_refs": _unique(evidence),
        "section_source_refs": section_refs,
        "role_model_input": role_model_input,
        "world_ref": report["scope"]["world_ref"],
        "creative_materials": material,
        "material_budget": budget,
        "context_ref": context_ref,
        "independent_label": bool(
            details["reader_observations"]
            or details["verified_evidence"]
            or outcome in {"RETAIN", "ACCEPT_REVISION"}
        ),
        "source_versions": details["source_versions"],
        "upstream_instruction_versions": details["upstream_instruction_versions"],
        "review_binding": report["review_binding"],
        "rubric_ref": report["rubric_ref"],
        "decision": outcome,
        "decision_ref": decision["decision_ref"] if decision else None,
        "preserved_readings": [
            {k: obs[k] for k in ("pattern", "start", "end", "narrative_function")}
            for obs in details["reader_observations"]
            if obs.get("disposition") == "PRESERVE"
        ],
    }


def _proofs(kernel, repo, wr, refs):
    gathered = {}
    for ref in refs:
        report, details = _review_history(kernel, repo, wr["work_id"], ref)
        require(
            _related_world(repo, report["scope"]["world_ref"], wr),
            "VIEW_DENIED",
            "Review belongs to a different world selection",
        )
        for proof in [
            report["review_binding"]["independence_receipt"],
            *(item["independence"]["independence_receipt"] for item in details["related_bindings"]),
        ]:
            gathered[proof["artifact_revision_id"]] = proof
    require(
        len(gathered) <= 128,
        "CAPACITY_LIMIT",
        "Too many signed reading cases in a single experiment",
    )
    return list(gathered.values())


def _unique(refs):
    return list({fingerprint(r): r for r in refs}.values())


def _baseline(kernel, repo, wr, scope, task, runtime, data, change_kind, dynamic_live):
    role_text, skill_text = runtime["role_contract"], runtime["skill_instructions"]
    instance = None
    dynamic_ref = data.get("dynamic_assignment_ref")
    if dynamic_ref:
        from .dynamic_runtime import verified

        owner = Principal(task["grant_id"], task["principal_id"])
        grant = kernel.authorize(repo, owner, wr["work_id"], "READ", wr["branch_id"])
        context = repo.one("contexts", {"context_bundle_id": task["context_bundle_id"]})
        require(
            context is not None,
            "CONTEXT_REQUIRED",
            "Dynamic source requires its actual knowledge cut",
        )
        dynamic, _ = verified(
            kernel,
            repo,
            owner,
            task,
            grant,
            dynamic_ref,
            dynamic_live,
            plane=context["read_cut"]["knowledge_cut"]["plane"],
        )
        instance = dynamic["instance_ref"]
        role_text += "\n" + dynamic["role_instructions"]
        skill_text += "\n" + dynamic["skill_instructions"]
    blocks = []
    if data.get("adaptive_instruction_ref"):
        from .adaptive_runtime import read_selection

        blocks = read_selection(kernel, repo, wr, data["adaptive_instruction_ref"])
    baseline = {
        "scope": scope,
        "role_id": task["role_id"],
        "task_kind": task["task_kind"],
        "skill_name": runtime["skill_name"],
        "instance_ref": instance,
        "change_kind": change_kind,
        "role_instructions": role_text,
        "skill_instructions": skill_text,
        "body": "",
        "base_role_instructions": role_text,
        "base_skill_instructions": skill_text,
        "blocks": [],
        "review_checks": sorted(CHECKS),
        "material_order": [],
        "source_runtime_ref": data["runtime_ref"],
        "source_assignment_ref": dynamic_ref,
        "source_adaptive_ref": data.get("adaptive_instruction_ref"),
    }
    for block in blocks:
        baseline = _apply(baseline, block)
    baseline["instruction_sha256"] = fingerprint(
        [
            baseline["role_instructions"],
            baseline["skill_instructions"],
            baseline["material_order"],
            baseline["review_checks"],
        ]
    )
    return baseline


def _apply(base, instruction):
    item = {**base, **{k: instruction[k] for k in ("change_kind", "body", "instruction_sha256")}}
    dest = (
        "role_instructions"
        if instruction["change_kind"] == "ROLE_INSTRUCTIONS"
        else "skill_instructions"
    )
    item[dest] = base[dest] + "\n" + instruction["body"]
    item["blocks"] = [*base["blocks"], instruction]
    item["review_checks"] = instruction["review_checks"]
    if instruction["change_kind"] == "CONTEXT_RECIPE":
        item["material_order"] = instruction["material_order"]
    return item


def _hypotheses(samples, role, change_kind):
    evidence = []
    for sample in samples:
        for note in sample["expected_findings"]:
            if (
                note["judgment"] not in {"PREFERENCE", "CONTRADICTION"}
                or not note["requested_change"]
            ):
                continue
            traces = sample["upstream_instruction_versions"]
            attributed = [
                {
                    "role_id": r,
                    "cause": ROLE_FOCUS[r],
                    "provenance": traces.get(r, {"status": "UNKNOWN", "traces": []}),
                }
                for r in note["upstream_roles"]
            ]
            if change_kind == "REVIEW_RUBRIC":
                require(
                    role in {"reviewer", "literary_editor", "chief_editor"},
                    "ROLE_MODE_DENIED",
                    "This assignment does not review literary work",
                )
                attributed.append(
                    {
                        "role_id": role,
                        "cause": "REVIEW_METHOD",
                        "provenance": {
                            "status": "READER_EVIDENCE",
                            "source": sample["source_versions"]["reviewer"],
                        },
                    }
                )
            if change_kind == "CONTEXT_RECIPE":
                attributed.append(
                    {
                        "role_id": role,
                        "cause": "CONTEXT_ORGANIZATION",
                        "provenance": {
                            "status": "MATERIAL_AVAILABLE"
                            if sample["context_ref"]
                            else "NO_CAPTURE",
                            "context_ref": sample["context_ref"],
                        },
                    }
                )
            evidence.append(
                {
                    "report_ref": sample["report_ref"],
                    "finding_id": note["finding_id"],
                    "pattern": note["pattern"],
                    "judgment": note["judgment"],
                    "text_ref": sample["text_ref"],
                    "start": note["start"],
                    "end": note["end"],
                    "reason": note["reason"],
                    "counterinterpretation": note["counterinterpretation"],
                    "attributions": attributed,
                    "status": "HYPOTHESIS_REQUIRES_COMPARISON",
                }
            )
    require(
        any(a["role_id"] == role for e in evidence for a in e["attributions"]),
        "INVALID_EVIDENCE",
        "No cited finding supports investigating the proposed upstream role",
    )
    return evidence


ROLE_METHODS = {
    "world_designer": "从地点的生活、族群关系或制度沿革寻找设定依据，用它解释人物为什么会这样称呼或选择。",
    "volume_director": "在相邻场次之间复核目标、阻力、选择和后果，区分未收束的伏笔与遗漏。",
    "environment_director": "顺着人物的位置、可用时间和场地阻力重走一遍这段行动。",
    "character_actor": "从角色当时亲历、听说或误信的事情出发，给其选择留下可见的代价。",
    "writer": "在原句附近寻找叙述转折，只改会妨碍读者辨明动作或声音的部分。",
    "literary_editor": "并读相关段落并保留相反解释，以局部修改检验批评是否成立。",
    "reviewer": "逐处核对原句、邻文与可能的反例，把主观取舍与可证矛盾分别记录。",
    "chief_editor": "在整卷承诺和当前场次之间核对修改的收益，记录未采纳的意见。",
    "evidence_maintainer": "在已获准的材料中定位可复验来源，保留冲突、不确定性和材料缺口。",
    "adjudication_advisor": "将规则允许的结果与尚无依据的推断分开，保留需要裁定的问题。",
}


def _instruction(scope, baseline, evidence, change_kind, instructions, order, successes):
    relevant = sorted(
        {
            e["pattern"]
            for e in evidence
            if any(a["role_id"] == baseline["role_id"] for a in e["attributions"])
        }
    )
    if instructions is None:
        body = "\n".join([ROLE_METHODS[baseline["role_id"]], *(CRAFT[p] for p in relevant)])
        if any(case["preserved_readings"] for case in successes):
            body += "\n保留独立读者已用原文说明其叙事作用的表达，不因形式相似而要求修改。"
    else:
        require(
            isinstance(instructions, str)
            and 0 < len(instructions.strip())
            and len(instructions.encode()) <= 16000
            and "NF_REVIEW_CHECKS:" not in instructions,
            "INVALID_ARGUMENT",
            "Invalid custom instruction body",
        )
        body = instructions
    body += "\nNF_REVIEW_CHECKS: " + json.dumps(relevant, separators=(",", ":"))
    if change_kind == "CONTEXT_RECIPE":
        order = (
            order
            if order is not None
            else ["uncertainties", "beliefs", "scene", "continuity", "prose", "style"]
        )
        require(
            isinstance(order, list)
            and 0 < len(order) <= len(MATERIAL_SECTIONS)
            and all(isinstance(key, str) and key in MATERIAL_SECTIONS for key in order)
            and len(set(order)) == len(order),
            "INVALID_ARGUMENT",
            "Context recipe can only order authorized material sections",
        )
    else:
        require(order is None, "INVALID_ARGUMENT", "Material order requires a context recipe")
        order = []
    common = {
        k: baseline[k]
        for k in (
            "role_id",
            "task_kind",
            "skill_name",
            "instance_ref",
            "source_runtime_ref",
            "source_assignment_ref",
            "source_adaptive_ref",
        )
    }
    return {
        **common,
        "scope": scope,
        "body": body,
        "instruction_sha256": sha256(body.encode()).hexdigest(),
        "change_kind": change_kind,
        "material_order": order,
        "review_checks": relevant,
    }


def _release_modules():
    package = Path(__file__).resolve().parent
    modules = [
        package / (name + ".py")
        for name in (
            "adaptive_instructions",
            "adaptive_evaluation",
            "adaptive_runtime",
            "adaptive_promotion",
            "agent_gateway",
            "dynamic_runtime",
            "dynamic_agents",
            "creative_context_store",
            "runtime_tasks",
            "candidate_delivery",
            "creative_review",
            "creative_context",
        )
    ]
    modules.append(package.parent / "domain" / "creative_review.py")
    return modules


def deployed_module_digests():
    package = Path(__file__).resolve().parents[1]
    return {
        str(path.relative_to(package)): sha256(path.read_bytes()).hexdigest()
        for path in _release_modules()
    }


def _code_release():
    source = Path(__file__).resolve()
    digests = deployed_module_digests()
    checkout = source.parents[3]
    if (checkout / ".git").exists() and (
        checkout / "src" / "novel_factory" / "application" / source.name
    ) == source:
        cmd = ["git", "-C", str(checkout)]
        commit = subprocess.check_output(
            [*cmd, "rev-parse", "--verify", "HEAD"], text=True, timeout=5
        ).strip()
        relevant = [str(path.relative_to(checkout)) for path in _release_modules()]
        dirty = bool(
            subprocess.check_output([*cmd, "status", "--porcelain", "--", *relevant], timeout=5)
        )
        origin = "CHECKOUT_WITH_LOCAL_MODULES" if dirty else "CHECKOUT"
    else:
        commit = os.environ.get("NF_CREATIVE_RELEASE_COMMIT", "")
        origin = "DEPLOYMENT_RELEASE"
    require(
        re.fullmatch(r"[0-9a-f]{40}", commit) and commit != "0" * 40,
        "RELEASE_METADATA_REQUIRED",
        "Experimental preregistration requires the deployed Git revision",
    )
    return {"source_commit": commit, "origin": origin, "module_sha256": digests}


def _corpus(kernel, repo, wr, refs, live, quota, task, runtime):
    remaining = min(quota["max_tokens"], 4_000_000)
    corpus = {}
    for ref in refs:
        sample = _sample(
            kernel, repo, wr, ref, live, remaining_bytes=remaining, target=task, runtime=runtime
        )
        remaining -= sum(len(doc["text"].encode()) for doc in sample["ordered_texts"])
        corpus[sample["case_id"]] = sample
    return corpus


class AdaptiveInstructionService:
    def __init__(self, kernel, *, config=None):
        self.kernel, self.config = kernel, config

    def _enabled(self):
        config = self.config or CreativeHarnessConfig.from_env()
        require(
            config.enabled and config.adaptive_instructions,
            "ADAPTIVE_INSTRUCTIONS_DISABLED",
            "Adaptive instruction proposals are disabled",
        )
        return config

    def propose(
        self,
        controller,
        world_ref,
        *,
        task_id,
        generation,
        failure_report_refs,
        success_report_refs,
        change_kind,
        quota,
        order_seed,
        repetitions,
        idempotency_key,
        instructions=None,
        material_order=None,
        evaluation_report_refs=None,
    ):
        from .adaptive_evaluation import REPLAY_MODEL_CONFIG, REPLAY_RUBRIC

        flags = self._enabled()
        validate_contract("Quota", quota, "harness")
        wr = validate_contract("WorldRef", world_ref)
        require(
            isinstance(idempotency_key, str)
            and 0 < len(idempotency_key.strip()) <= 96
            and change_kind in CHANGES
            and type(order_seed) is int
            and 0 <= order_seed <= 2147483647
            and type(repetitions) is int
            and 1 <= repetitions <= 32,
            "INVALID_ARGUMENT",
            "Invalid experimental design",
        )
        require(
            isinstance(failure_report_refs, list)
            and 1 <= len(failure_report_refs) <= 32
            and isinstance(success_report_refs, list)
            and len(success_report_refs) <= 32,
            "INVALID_ARGUMENT",
            "Source examples must include a qualified failure",
        )
        train = failure_report_refs + success_report_refs
        evals = train if evaluation_report_refs is None else evaluation_report_refs
        require(
            isinstance(evals, list) and 1 <= len(evals) <= 32,
            "INVALID_ARGUMENT",
            "Provide a nonempty test manifest",
        )
        for ref in train + evals:
            validate_contract("ArtifactRef", ref)
        refs = _unique(train + evals)
        require(
            len(_unique(train)) == len(train)
            and len(_unique(evals)) == len(evals)
            and len(refs) <= 64,
            "INVALID_ARGUMENT",
            "Duplicate or excessive review examples",
        )
        request = {
            "world_ref": wr,
            "task_id": task_id,
            "generation": generation,
            "failures": failure_report_refs,
            "successes": success_report_refs,
            "evaluation": evals,
            "change_kind": change_kind,
            "quota": quota,
            "order_seed": order_seed,
            "repetitions": repetitions,
            "instructions": instructions,
            "material_order": material_order,
        }
        build = _code_release()
        kernel, work = self.kernel, wr["work_id"]
        with kernel.db.uow(write=False) as repo:
            _controller(kernel, repo, controller, wr)
            target, _, _, data, _ = _target(kernel, repo, wr, task_id, generation)
            proofs = _proofs(kernel, repo, wr, refs)
        live = CreativeReviewService(kernel)._refresh_many(proofs)
        from .dynamic_runtime import refresh

        dynamic_live = refresh(
            kernel,
            Principal(target["grant_id"], target["principal_id"]),
            task_id,
            generation,
            data.get("dynamic_assignment_ref"),
        )
        with kernel.objects.lock(), kernel.db.uow() as repo:
            repo.one("works", {"work_id": work}, lock="update")
            grant = _controller(kernel, repo, controller, wr)
            task, source_grant, spec, data, runtime = _target(kernel, repo, wr, task_id, generation)
            scope = {"world_ref": wr, "task_id": task_id, "generation": generation}
            samples = _corpus(kernel, repo, wr, refs, live, quota, task, runtime)
            fail = [samples[r["artifact_revision_id"]] for r in failure_report_refs]
            successes = [samples[r["artifact_revision_id"]] for r in success_report_refs]
            require(
                all(s["classification"] == "FAILURE" for s in fail)
                and all(s["classification"] == "SUCCESS" for s in successes),
                "INVALID_EVIDENCE",
                "Sample labels disagree with signed review findings or later editorial decisions",
            )
            units = sum(
                len(text["text"].encode()) for s in samples.values() for text in s["ordered_texts"]
            )
            usage = _quota(repo, task, spec, quota, units)
            require(
                len(evals) * 2 * repetitions <= 512,
                "CAPACITY_LIMIT",
                "Experiment exceeds its bounded arm allocation",
            )
            baseline = _baseline(
                kernel, repo, wr, scope, task, runtime, data, change_kind, dynamic_live
            )
            causes = _hypotheses(fail, task["role_id"], change_kind)
            instruction = _instruction(
                scope, baseline, causes, change_kind, instructions, material_order, successes
            )
            prior = kernel._prior(
                repo, controller, work, "ADAPTIVE_PROPOSE", idempotency_key, request
            )
            if prior:
                return prior

            def put(kind, payload, *, revision=None):
                return _artifact(
                    kernel,
                    repo,
                    work,
                    controller,
                    "CREATIVE_INSTRUCTION_" + kind,
                    payload,
                    revision=revision,
                )

            baseline_ref = put("BASELINE", baseline)
            text_ref = put("TEXT", instruction)
            training_hashes = {
                entry["text_ref"]["sha256"]
                for s in fail + successes
                for entry in s["ordered_texts"]
            }
            cases = []
            for ref in evals:
                case = samples[ref["artifact_revision_id"]]
                held = all(
                    entry["text_ref"]["sha256"] not in training_hashes
                    for entry in case["ordered_texts"]
                )
                cases.append(
                    {
                        "case_id": case["case_id"],
                        "report_ref": ref,
                        "text_ref": case["text_ref"],
                        "sample_sha256": fingerprint(case),
                        "held_out": held,
                    }
                )
            manifest = put("CASES", {"scope": scope, "cases": cases})
            all_sources = put(
                "INPUTS",
                {
                    "scope": scope,
                    "source_reports": refs,
                    "training_text_sha256": sorted(training_hashes),
                },
            )
            budget_ref = put("BUDGET", {"scope": scope, "quota": quota, "usage_at_proposal": usage})
            rubric_ref = put(
                "RUBRIC",
                {"scope": scope, "rubric": REPLAY_RUBRIC, "sha256": fingerprint(REPLAY_RUBRIC)},
            )
            generator = Random(order_seed)
            allocation = []
            for repetition in range(1, repetitions + 1):
                shuffled = cases.copy()
                generator.shuffle(shuffled)
                for case in shuffled:
                    arms = ["CONTROL", "TREATMENT"]
                    generator.shuffle(arms)
                    allocation.append(
                        {
                            "case_id": case["case_id"],
                            "repetition": repetition,
                            "order": arms,
                            "blind_ids": [str(uuid4()), str(uuid4())],
                        }
                    )
            blind_ref = put("BLIND_ALLOCATION", {"scope": scope, "allocation": allocation})
            publisher = _identity(controller, grant)
            source_owner = Principal(task["grant_id"], task["principal_id"])
            target = {
                k: baseline[k] for k in ("role_id", "task_kind", "skill_name", "instance_ref")
            }
            design = {
                "scope": scope,
                "target": target,
                "producers": _unique([publisher, _identity(source_owner, source_grant)]),
                "source_runtime_ref": data["runtime_ref"],
                "source_assignment_ref": baseline["source_assignment_ref"],
                "source_adaptive_ref": baseline["source_adaptive_ref"],
                "baseline_ref": baseline_ref,
                "case_manifest_ref": manifest,
                "blind_allocation_ref": blind_ref,
                "shared_input_ref": all_sources,
                "budget_policy_ref": budget_ref,
                "rubric_ref": rubric_ref,
                "causes": causes,
                "source_reports": refs,
                "source_hashes": {s["case_id"]: fingerprint(s) for s in samples.values()},
                "report_reviewers": _unique(
                    [s["review_binding"]["reviewer"] for s in samples.values()]
                ),
                "quota": quota,
                "usage": usage,
                "optimization_model_calls": 0,
                "source_method": "AUTHORIZED_OPERATOR"
                if instructions is not None
                else "DETERMINISTIC_EVIDENCE_RULES",
                "order_seed": order_seed,
                "repetitions": repetitions,
                "model_config": REPLAY_MODEL_CONFIG,
                "release": build,
            }
            design_ref = put("DESIGN", design)
            snapshot = {
                "role_id": task["role_id"],
                "task_kind": task["task_kind"],
                "role_version": runtime["role_contract_version"],
                "skill_name": runtime["skill_name"],
                "skill_version": runtime["skill_version"],
                "runtime_sha256": fingerprint(runtime),
                **runtime_provenance(runtime),
            }
            risk = "A localized preference can be mistaken for a general rule. Preserve deliberate counterexamples and compare held-out manuscripts."
            candidate = {
                "schema_version": "1.0",
                "kind": "INSTRUCTION_CANDIDATE",
                "scope": scope,
                "baseline_runtime": snapshot,
                "change_kind": change_kind,
                "instruction_ref": text_ref,
                "change_summary": f"{task['role_id']}/{task['task_kind']} · "
                + ", ".join(instruction["review_checks"]),
                "evidence_refs": [design_ref],
                "failure_examples": failure_report_refs,
                "success_examples": success_report_refs,
                "expected_effect": "Compare the treatment's handling of "
                + ", ".join(instruction["review_checks"])
                + " against the frozen source, including retained readings.",
                "risk": risk,
                "freeze_ref": design_ref,
                "rollback_ref": baseline["source_adaptive_ref"] or baseline_ref,
                "status": "PROPOSED_NOT_ACTIVE",
            }
            parent_ref, sequence = None, 1
            if baseline["source_adaptive_ref"]:
                previous = kernel.artifact(
                    repo,
                    baseline["source_adaptive_ref"],
                    work,
                    kind="CREATIVE_INSTRUCTION_SELECTION",
                )["payload"]
                parent_ref = previous["candidate_ref"]
                parent = _read(
                    kernel, repo, wr, parent_ref, "CREATIVE_INSTRUCTION_CANDIDATE", branch_only=True
                )
                sequence = parent["candidate"]["revision"] + 1
            candidate["candidate"] = {
                "candidate_id": str(uuid4()),
                "revision": sequence,
                "parent_ref": parent_ref,
                "created_by": publisher,
                "content_sha256": fingerprint(candidate),
            }
            validate_contract("InstructionCandidate", candidate, "harness")
            candidate_ref = put("CANDIDATE", candidate)
            frozen_flags = {
                **flags.as_dict()["creative_harness"],
                "adaptive_instructions": {"enabled": True, "auto_promote": False},
            }
            works = _unique([wr, *(sample["world_ref"] for sample in samples.values())])
            require(
                len(works) <= 32,
                "CAPACITY_LIMIT",
                "Experiment exceeds the supported number of world revisions",
            )
            freeze = {
                "schema_version": "1.0",
                "kind": "EXPERIMENT_FREEZE",
                "experiment_id": str(uuid4()),
                "frozen_at": repo.now().isoformat(),
                "source_commit": build["source_commit"],
                "works": works,
                "shared_input_ref": all_sources,
                "case_manifest_ref": manifest,
                "model_config_sha256": fingerprint(REPLAY_MODEL_CONFIG),
                "budget_policy_ref": budget_ref,
                "quota": quota,
                "rubric_ref": rubric_ref,
                "blind_allocation_ref": blind_ref,
                "order_seed": order_seed,
                "repetitions": repetitions,
                "design": "A_B",
                "sampling": "ALL_PREREGISTERED_CASES_INCLUDING_FAILURES",
                "arms": [
                    {"arm_id": name, "candidate_ref": ref, "creative_harness": frozen_flags}
                    for name, ref in (("CONTROL", baseline_ref), ("TREATMENT", candidate_ref))
                ],
                "metrics": {
                    **dict.fromkeys(
                        (
                            "engineering",
                            "review_effectiveness",
                            "tokens",
                            "cost",
                            "latency",
                            "rework",
                        ),
                        "REQUIRED",
                    ),
                    "literary": "INDEPENDENT_READING_OR_UNPROVEN",
                },
            }
            validate_contract("ExperimentFreeze", freeze, "harness")
            experiment_ref = put(
                "EXPERIMENT",
                {
                    "scope": scope,
                    "freeze": freeze,
                    "candidate_ref": candidate_ref,
                    "design_ref": design_ref,
                },
                revision=_derived(candidate_ref, "experiment"),
            )
            result = {
                "candidate_ref": candidate_ref,
                "experiment_ref": experiment_ref,
                "design_ref": design_ref,
                "baseline_ref": baseline_ref,
                "instruction_ref": text_ref,
            }
            kernel._receipt(
                repo, controller, wr, "ADAPTIVE_PROPOSE", idempotency_key, request, result
            )
            return result

    def refresh_sources(self, controller, world_ref, candidate_ref):
        return refresh_sources(self.kernel, controller, world_ref, candidate_ref)

    def load_evaluation(self, repo, controller, world_ref, candidate_ref, *, live):
        kernel, wr = self.kernel, world_ref
        _controller(kernel, repo, controller, wr)
        candidate = _read(kernel, repo, wr, candidate_ref, "CREATIVE_INSTRUCTION_CANDIDATE")
        design_ref = candidate["freeze_ref"]
        design = _read(kernel, repo, wr, design_ref, "CREATIVE_INSTRUCTION_DESIGN")
        task, _, _, data, runtime = _target(
            kernel, repo, wr, design["scope"]["task_id"], design["scope"]["generation"]
        )
        original = _baseline(
            kernel,
            repo,
            wr,
            design["scope"],
            task,
            runtime,
            data,
            candidate["change_kind"],
            live["dynamic"],
        )
        baseline = _read(kernel, repo, wr, design["baseline_ref"], "CREATIVE_INSTRUCTION_BASELINE")
        require(
            original == baseline,
            "TASK_REBIND_REQUIRED",
            "Source instructions no longer match the frozen baseline",
        )
        source_samples = _corpus(
            kernel,
            repo,
            wr,
            design["source_reports"],
            live["reviews"],
            design["quota"],
            task,
            runtime,
        )
        require(
            {key: fingerprint(s) for key, s in source_samples.items()} == design["source_hashes"],
            "REVIEW_REVOKED",
            "Review qualification, manuscript, or selection changed after preregistration",
        )
        exp = repo.one(
            "artifacts",
            {
                "work_id": wr["work_id"],
                "kind": "CREATIVE_INSTRUCTION_EXPERIMENT",
                "artifact_revision_id": _derived(candidate_ref, "experiment"),
            },
        )
        require(exp is not None, "INVALID_EVIDENCE", "Missing frozen A/B experiment")
        experiment_ref = _ref(exp)
        envelope = _read(kernel, repo, wr, experiment_ref, "CREATIVE_INSTRUCTION_EXPERIMENT")
        freeze = validate_contract("ExperimentFreeze", envelope["freeze"], "harness")
        require(
            envelope["candidate_ref"] == candidate_ref
            and envelope["design_ref"] == design_ref
            and freeze["arms"][0]["candidate_ref"] == design["baseline_ref"]
            and freeze["arms"][1]["candidate_ref"] == candidate_ref,
            "INVALID_EVIDENCE",
            "Experimental arms changed",
        )
        manifest = _read(
            kernel, repo, wr, freeze["case_manifest_ref"], "CREATIVE_INSTRUCTION_CASES"
        )
        allocation = _read(
            kernel,
            repo,
            wr,
            freeze["blind_allocation_ref"],
            "CREATIVE_INSTRUCTION_BLIND_ALLOCATION",
        )
        samples = [
            {**source_samples[s["case_id"]], "held_out": s["held_out"]} for s in manifest["cases"]
        ]
        treatment = _apply(
            baseline, load_instruction(kernel, repo, wr, candidate["instruction_ref"])
        )
        return {
            "scope": design["scope"],
            "candidate": candidate,
            "candidate_ref": candidate_ref,
            "freeze": freeze,
            "experiment_ref": experiment_ref,
            "design_ref": design_ref,
            "design": design,
            "baseline": baseline,
            "treatment": treatment,
            "samples": samples,
            "allocation": allocation["allocation"],
        }


def issuer_for_candidate(kernel, repo, world_ref, candidate_ref):
    item = _read(kernel, repo, world_ref, candidate_ref, "CREATIVE_INSTRUCTION_CANDIDATE")
    row = kernel.artifact(
        repo, candidate_ref, world_ref["work_id"], kind="CREATIVE_INSTRUCTION_CANDIDATE"
    )
    issuer = item["candidate"]["created_by"]
    principal = Principal(issuer["grant_id"], issuer["principal_id"])
    current = _controller(kernel, repo, principal, world_ref)
    require(
        row["created_by"] == principal.principal_id
        and current["epoch"] == issuer["permission_epoch"],
        "VIEW_DENIED",
        "Instruction proposal authority has changed",
    )
    return principal


def refresh_sources(kernel, controller, world_ref, candidate_ref):
    with kernel.db.uow(write=False) as repo:
        _controller(kernel, repo, controller, world_ref)
        candidate = _read(kernel, repo, world_ref, candidate_ref, "CREATIVE_INSTRUCTION_CANDIDATE")
        design = _read(
            kernel, repo, world_ref, candidate["freeze_ref"], "CREATIVE_INSTRUCTION_DESIGN"
        )
        proofs = _proofs(kernel, repo, world_ref, design["source_reports"])
        scope = design["scope"]
        task, _, _, data, _ = _target(
            kernel, repo, world_ref, scope["task_id"], scope["generation"]
        )
    from .dynamic_runtime import refresh

    dynamic = refresh(
        kernel,
        Principal(task["grant_id"], task["principal_id"]),
        task["task_id"],
        task["generation"],
        data.get("dynamic_assignment_ref"),
    )
    return {"reviews": CreativeReviewService(kernel)._refresh_many(proofs), "dynamic": dynamic}
