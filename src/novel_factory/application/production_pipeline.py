"""Bounded production across one accepted volume and up to two future volumes.

The board is an immutable projection of handoffs. NF tasks, attempts, drafts,
published continuity and selected plans keep their existing authorities.
"""
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from uuid import UUID, uuid5

from sqlalchemy import text

from novel_factory.application.authorization import Principal
from novel_factory.application.candidate_delivery import CandidateDelivery
from novel_factory.application.kernel import require
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.planning import PlanningService
from novel_factory.application.release import ReleaseService
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.scene_revision import editorial_blockers, revise_scene
from novel_factory.application.volume_successor import _selected
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import canonical_bytes, fingerprint, strict_json_loads
from novel_factory.domain.errors import DomainError
from novel_factory.domain.planning import condition_result
from novel_factory.domain.temporal import point_key

ROLES = {"draft": ("writer", "scene_draft"), "review": ("reviewer", "literary"),
         "plan": ("volume_director", "window_plan")}
READING_STAGES = {"epistemic", "facts", "review"}
PLAN_EDGES = {"HARD_CAUSAL", "CHARACTER_BELIEF", "STYLE_OR_VOICE", "LITERARY_COMMITMENT",
              "SOFT_PREFERENCE", "READER_DISCLOSURE"}


def _key(branch):
    return "P58_PIPELINE:" + branch


def _bounded(value, maximum, name, minimum=1):
    require(type(value) is int and minimum <= value <= maximum, "INVALID_ARGUMENT", name)
    return value


def _list(value, maximum, label, minimum=0):
    require(isinstance(value, list) and minimum <= len(value) <= maximum, "INVALID_ARGUMENT", label)
    return value


def _fields(obj, keys):
    require(isinstance(obj, dict) and set(obj) == set(keys.split()), "INVALID_ARGUMENT",
            "Missing or unsupported production fields")


def _artifact(kernel, repo, work, owner, kind, payload):
    ref = kernel._blob(payload)
    kernel._register_artifact(repo, ref, work, payload, kind, owner.principal_id)
    return ref


def _load(kernel, repo, work, branch):
    selected = _selected(repo, work, _key(branch))
    return selected and kernel.artifact(repo, selected, work, kind="PRODUCTION_BOARD")["payload"]


def _save(kernel, repo, owner, board):
    wr = board["world_ref"]
    ref = _artifact(kernel, repo, wr["work_id"], owner, "PRODUCTION_BOARD", board)
    PlanningService(kernel)._select(repo, wr, _key(wr["branch_id"]), ref)
    repo.outbox("production.board.changed", "volume", board["volume_id"], {"board_ref": ref},
                "production:" + ref["artifact_revision_id"])
    return ref


def _current_owner(kernel, repo, board):
    wr = board["world_ref"]
    fence = board["authorization"]
    owner = Principal(fence["grant_id"], fence["principal_id"])
    grant = kernel.authorize(repo, owner, wr["work_id"], "ADMIN", wr["branch_id"])
    require(grant["epoch"] == fence["epoch"] and
            repo.one("works", {"work_id": wr["work_id"]})["policy_version"] == fence["policy_version"],
            "VIEW_DENIED", "Production authorization changed")
    require(repo.now() < datetime.fromisoformat(fence["deadline"]), "TASK_EXPIRED", "Production allowance expired")
    return owner


def _dependencies(assumptions, plan_nodes, subjects, predicates):
    deps = []
    for dep in _list(assumptions, 32, "Each candidate needs 1..32 typed assumptions", 1):
        require(isinstance(dep, dict), "INVALID_ARGUMENT", "Invalid dependency")
        if dep.get("kind") == "WORLD_FACT":
            _fields(dep, "kind condition")
            condition = validate_contract("PlanPrecondition", dep["condition"], "planning")
            require(all(condition[k] in subjects for k in ("subject_id", "account_id", "resource_id") if k in condition)
                    and ("predicate" not in condition or condition["predicate"] in predicates),
                    "VIEW_DENIED", "The assumption exceeds the declared planning scope")
            if condition.get("expected_value", {}).get("kind") == "ENTITY":
                require(condition["expected_value"]["entity_id"] in subjects,
                        "VIEW_DENIED", "The expected identity is outside the declared scope")
            deps.append(dep)
        else:
            _fields(dep, "kind node_id")
            require(isinstance(dep["kind"], str) and dep["kind"] in PLAN_EDGES and
                    validate_contract("UUID", dep["node_id"]) in plan_nodes,
                    "INVALID_ARGUMENT", "Expected a typed dependency on an exact source plan node")
            deps.append({**dep, "node_hash": fingerprint(plan_nodes[dep["node_id"]])})
    return deps


def _nodes(kernel, repo, wr, current_volume, current_plan, chapters, future, lookahead, subjects, predicates, *, rolling=False):
    work, nodes, used_scenes = wr["work_id"], {}, set()
    plan_nodes = {n["node_id"]: n for n in current_plan["nodes"]}
    current_ref = _selected(repo, work, PlanningService.selection_key(current_volume))
    for chapter in _list(chapters, 32, "A run contains 1..32 chapters", 1):
        _fields(chapter, "chapter_id scenes")
        chapter_id = validate_contract("UUID", chapter["chapter_id"])
        review_id = "review:" + chapter_id
        require(review_id not in nodes, "INVALID_ARGUMENT", "Chapter repeated")
        parents = []
        for item in _list(chapter["scenes"], 32, "A chapter contains 1..32 scenes", 1):
            _fields(item, "scene_ref handoff_refs")
            scene = kernel.artifact(repo, item["scene_ref"], work, kind="SCENE_PLAN")["payload"]
            scene_id = scene["scene_contract"]["scene_id"]
            require(scene["plan_ref"] == current_ref and scene.get("batch_ref") and
                    scene["scene_contract"]["input_world_ref"] == wr and scene_id not in used_scenes,
                    "STALE_PLAN", "Each distinct writing card needs the selected volume plan and fixed world")
            used_scenes.add(scene_id)
            for ref in _list(item["handoff_refs"], 32, "Bounded predecessor acceptances"):
                kernel.artifact(repo, ref, work, kind="NARRATIVE_ACCEPTANCE")
            waiting = rolling and not item["handoff_refs"] and bool(scene["required_predecessor_scene_ids"])
            require(waiting or len(item["handoff_refs"]) == len(scene["required_predecessor_scene_ids"]),
                    "TASK_REBIND_REQUIRED", "A dependent scene requires exact accepted handoffs")
            node_id = "draft:" + scene_id
            parents.append(node_id)
            nodes[node_id] = {"stage": "draft", "chapter_id": chapter_id, **item,
                              "state": "WAITING_HANDOFF" if waiting else "PENDING"}
            if waiting:
                nodes[node_id]["predecessor_scene_ids"] = scene["required_predecessor_scene_ids"]
            if rolling:
                knowledge = "knowledge:" + scene_id
                nodes[knowledge] = {"stage": "epistemic", "chapter_id": chapter_id,
                                    "parents": [node_id], "state": "PENDING"}
                nodes["facts:" + scene_id] = {"stage": "facts", "chapter_id": chapter_id,
                                              "parents": [node_id], "knowledge_node": knowledge, "state": "PENDING"}
        nodes[review_id] = {"stage": "review", "chapter_id": chapter_id, "parents": parents, "state": "PENDING"}
    future_volumes = set()
    for item in _list(future, 8, "At most eight provisional candidates per run"):
        _fields(item, "candidate_id volume_contract_ref distance assumptions")
        identity = "plan:" + validate_contract("UUID", item["candidate_id"])
        distance = _bounded(item["distance"], lookahead, "Candidate exceeds lookahead")
        contract = kernel.artifact(repo, item["volume_contract_ref"], work, kind="VOLUME_CONTRACT")["payload"]
        volume = contract["volume_id"]
        require(volume != current_volume and repo.ancestor(contract["input_world_ref"], wr)
                and contract["input_world_ref"]["branch_id"] == wr["branch_id"] and identity not in nodes,
                "INVALID_ARGUMENT", "Invalid future contract or duplicate candidate")
        future_volumes.add((distance, volume))
        deps = _dependencies(item["assumptions"], plan_nodes, subjects, predicates)
        nodes[identity] = {"stage": "plan", "state": "PENDING", "volume_id": volume,
                           "distance": distance, "contract_ref": item["volume_contract_ref"], "dependencies": deps}
    require(len({d for d, _ in future_volumes}) == len({v for _, v in future_volumes}) == len(future_volumes)
            and sorted(d for d, _ in future_volumes) == list(range(1, len(future_volumes) + 1)),
            "INVALID_ARGUMENT", "Future volumes require consecutive, distinct ordinals")
    require(len(nodes) <= 128, "CAPACITY_LIMIT", "At most 128 nodes per branch run")
    return nodes


def register_pipeline(kernel, gateway, owner, *, world_ref, series_id, volume_id, chapters, future,
                      story_time, subject_ids, predicates, routes, budget_id, reserve_amount,
                      token_reservation, limits, deadline_seconds, idempotency_key):
    """Persist a finite controller allowance. Registration sends no model requests."""
    return _register(kernel, gateway, owner, world_ref=world_ref, series_id=series_id, volume_id=volume_id,
        chapters=chapters, future=future, story_time=story_time, subject_ids=subject_ids, predicates=predicates,
        routes=routes, budget_id=budget_id, reserve_amount=reserve_amount, token_reservation=token_reservation,
        limits=limits, deadline_seconds=deadline_seconds, idempotency_key=idempotency_key, rolling=None)


def _register(kernel, gateway, owner, *, world_ref, series_id, volume_id, chapters, future, story_time,
              subject_ids, predicates, routes, budget_id, reserve_amount, token_reservation, limits,
              deadline_seconds, idempotency_key, rolling, continuity_ref=None, scene_extents=None, max_reworks=0, quality=False):
    validate_contract("WorldRef", world_ref)
    validate_contract("StoryPoint", story_time)
    for uid in (series_id, volume_id, budget_id):
        validate_contract("UUID", uid)
    validate_contract("InputManifest", {"subject_ids": subject_ids, "predicates": predicates}, "kernel")
    _fields(routes, "draft review plan")
    _fields(limits, "draft review plan review_backlog lookahead window_goals")
    for stage in ROLES:
        _bounded(limits[stage], 16, "Per-stage WIP must be 1..16")
        require(isinstance(routes[stage], str) and (routes[stage] in gateway.adapters or routes[stage] == "@policy"),
                "MODEL_UNAVAILABLE", "Unknown stage route")
    _bounded(limits["review_backlog"], 32, "Review backlog must be 1..32")
    _bounded(limits["lookahead"], 2, "Invalid lookahead", 0 if rolling else 1)
    _bounded(limits["window_goals"], 32, "Future window must be 1..32")
    _bounded(deadline_seconds, 86400, "Deadline must be 1..86400 seconds")
    PlanningService._key(idempotency_key)
    wr, work, branch = world_ref, world_ref["work_id"], world_ref["branch_id"]
    request = {"world_ref": wr, "series_id": series_id, "volume_id": volume_id, "chapters": chapters,
                   "future": future, "story_time": story_time, "subject_ids": subject_ids, "predicates": predicates,
                   "routes": routes, "budget_id": budget_id, "reserve_amount": reserve_amount,
                   "token_reservation": token_reservation, "limits": limits, "deadline_seconds": deadline_seconds}
    with kernel.objects.lock(), kernel.db.uow() as repo:
        current_work = repo.one("works", {"work_id": work}, lock="update")
        grant, _ = PlanningService(kernel)._authorize(repo, owner, wr, capability="ADMIN")
        previous = kernel._prior(repo, owner, work, "PRODUCTION_REGISTER", idempotency_key, request)
        if previous:
            return previous
        if rolling is None:
            from .rolling_production import _load as rolling_cursor
            cursor = rolling_cursor(kernel, repo, work, branch)
            require(not cursor or cursor["state"] == "COMPLETED", "CAPACITY_LIMIT", "A rolling allowance owns this branch")
        active = _load(kernel, repo, work, branch)
        require(not active or all(n["state"] in {"DRAFT_STORED", "ADVISORY_REVIEW", "WINDOW_TRANSFERRED",
                "REPLAN_REQUIRED", "KNOWLEDGE_CLEAR", "FACTS_ACCEPTED", "STOPPED"} for n in active["nodes"].values()), "CAPACITY_LIMIT",
                "The branch has an unfinished production allowance")
        if active:
            previous_tasks = [record["task_id"] for n in active["nodes"].values() for record in [n, *n.get("history", [])]
                              if "task_id" in record]
            if previous_tasks:
                for call in repo.many("call_attempts", {"task_id": previous_tasks,
                                                        "state": ["PREPARED", "SENT", "UNKNOWN"]}):
                    task = repo.one("tasks", {"task_id": call["task_id"]})
                    require(call["state"] == "UNKNOWN" and task["state"] == "ACCEPTED"
                            and call["generation"] == task["generation"]
                            and call["call_key"] == "supervised-candidate", "CALL_RECONCILIATION_REQUIRED",
                            "Reconcile unresolved dispatches before replacing this production allowance")
                    CandidateDelivery(kernel, gateway).audited_candidate(repo, task, production_accounting=True)
        require(kernel.head(repo, work, branch) == wr and repo.one("budgets", {"work_id": work, "budget_id": budget_id}),
                "VIEW_DENIED", "Current work head and a work-owned budget are required")
        plan_ref = _selected(repo, work, PlanningService.selection_key(volume_id))
        manifest = _selected(repo, work, "PLAN_MANIFEST:" + volume_id)
        require(plan_ref and manifest, "STALE_PLAN", "The source volume requires an adopted fine outline")
        plan = kernel.artifact(repo, plan_ref, work, kind="PLAN_REVISION")["payload"]
        require(plan["input_world_ref"] == wr, "STALE_PLAN", "The source outline must match the fixed world")
        validate_contract("AgentDispatchRequest", {"schema_version": "2.1", "task_id": volume_id, "generation": 1,
                          "worker_id": "registration", "call_key": "pipeline", "input_ref": plan_ref,
                          "budget_id": budget_id, "reserve_amount": reserve_amount,
                          "token_reservation": token_reservation}, "agent")
        nodes = _nodes(kernel, repo, wr, volume_id, plan, chapters, future, limits["lookahead"], subject_ids, predicates, rolling=bool(rolling))
        if scene_extents:
            for node in nodes.values():
                if node["stage"] == "draft":
                    node["han_characters"] = scene_extents[node["scene_ref"]["artifact_revision_id"]]
        board = {k: v for k, v in request.items() if k not in {"chapters", "future", "deadline_seconds"}}
        if rolling is not None:
            board["rolling"] = rolling
            if quality:
                board["production_mode"] = "quality"
            if max_reworks:
                board["max_reworks"] = max_reworks
            if continuity_ref:
                board["continuity_ref"] = continuity_ref
        board.update(nodes=nodes, plan_ref=plan_ref, manifest_ref=manifest, authorization={
            "grant_id": owner.grant_id, "principal_id": owner.principal_id, "epoch": grant["epoch"],
            "policy_version": current_work["policy_version"],
            "deadline": (repo.now() + timedelta(seconds=deadline_seconds)).isoformat()})
        from .production_profiles import load_profile
        binding = load_profile(kernel, repo, owner, work, branch)
        if binding and binding["profile"].get("production_mode") == "quality":
            board["production_mode"] = "quality"
        if active:
            board["previous_board_ref"] = _selected(repo, work, _key(branch))
        result = {"board_ref": _save(kernel, repo, owner, board), "status": "REGISTERED", "nodes": len(nodes)}
        kernel._receipt(repo, owner, wr, "PRODUCTION_REGISTER", idempotency_key, request, result)
        repo.conn.execute(text("SELECT pg_notify('nf_supervisor_wake', '')"))
        return result


def pipeline_status(kernel, owner, work, branch, *, include_outputs=False):
    with kernel.db.uow(write=False, repeatable_read=True) as repo:
        kernel.authorize(repo, owner, work, "ADMIN", branch)
        board = _load(kernel, repo, work, branch)
        require(board is not None, "INVALID_ARGUMENT", "No production allowance")
        nodes = board["nodes"]
        records = [record for node in nodes.values() for record in (node, *node.get("history", []))]
        task_ids = {record["task_id"] for record in records if record.get("task_id")}
        attempts = defaultdict(list)
        if task_ids:
            for call in repo.many("call_attempts", {"task_id": task_ids}):
                attempts[call["task_id"]].append(call["attempt_id"])
        for record in records:
            record["attempt_ids"] = attempts.get(record.get("task_id"), [])
        outputs = {}
        for key, node in nodes.items():
            if not include_outputs or "output_ref" not in node:
                continue
            stage, ref = node["stage"], node["output_ref"]
            if stage == "draft":
                draft = kernel.artifact(repo, ref, work, kind="NARRATIVE_DRAFT")["payload"]
                kernel.artifact(repo, draft["text_ref"], work, kind="TEXT")
                outputs[key] = {"draft_ref": ref, "text_ref": draft["text_ref"],
                                "text": kernel.objects.read(draft["text_ref"]["sha256"]).decode("utf-8")}
            elif stage == "epistemic":
                outputs[key] = kernel.artifact(repo, ref, work, kind="EPISTEMIC_READING")["payload"]
            elif stage == "facts":
                outputs[key] = kernel.artifact(repo, ref, work, kind="ROLLING_FACT_REPORT")["payload"]
            elif stage == "review":
                advice = kernel.artifact(repo, ref, work, kind="CHAPTER_READING_ADVICE")["payload"]
                outputs[key] = {**advice, "advice": kernel.artifact(repo, advice["candidate_ref"], work,
                                                                      kind="AGENT_CANDIDATE")["payload"]}
            elif stage == "reassessment":
                outputs[key] = kernel.artifact(repo, ref, work, kind="PRODUCTION_REASSESSMENT")["payload"]
            elif stage == "internal_verification":
                outputs[key] = kernel.artifact(repo, ref, work, kind="INTERNAL_REVIEW_RESOLUTION")["payload"]
            else:
                outputs[key] = kernel.artifact(repo, ref, work, kind="AGENT_CANDIDATE")["payload"]
        return {"world_ref": board["world_ref"], "volume_id": board["volume_id"], "nodes": nodes,
                **({"outputs": outputs, "literary_quality": "NOT_EVALUATED"} if include_outputs else {})}


def _ready(board):
    nodes, limits = board["nodes"], board["limits"]
    if any(n["state"] == "STOPPED" and n.get("reason") in {
            "REVIEW_EVIDENCE_REPAIR_EXHAUSTED", "TECHNICAL_REPAIR_BUDGET_EXHAUSTED",
            "INTERNAL_VERIFICATION_REPAIR_EXHAUSTED", "INTERNAL_VERIFICATION_EXHAUSTED",
            "REPAIR_BUDGET_EXHAUSTED", "NEEDS_WORLD_INPUT", "NEEDS_AUTHOR_DECISION"}
           for n in nodes.values()):
        return None
    busy = Counter(n["stage"] for n in nodes.values() if n["state"] == "ENROLLED")
    diagnoses = sorted(k for k, n in nodes.items() if n["stage"] in {"reassessment", "internal_verification"} and n["state"] == "PENDING")
    if diagnoses and sum(busy[s] for s in READING_STAGES | {"reassessment", "internal_verification"}) < limits["review"]:
        return diagnoses[0]
    reviews = [k for k, n in nodes.items() if n["stage"] in READING_STAGES and n["state"] == "PENDING"
               and all(nodes[p]["state"] == "DRAFT_STORED" for p in n["parents"])
               and ("knowledge_node" not in n or nodes[n["knowledge_node"]]["state"] == "KNOWLEDGE_CLEAR")]
    if (board.get("production_mode") == "quality" or board.get("max_reworks")):
        # A later scene within this chapter may need this exact acceptance before it can be written.
        # In that case the full chapter reading can only follow the predecessor's factual clearance.
        def chapter_ready(key):
            node = nodes[key]
            if node["stage"] != "facts":
                return True
            chapter = nodes["review:" + node["chapter_id"]]
            return chapter["state"] == "ADVISORY_REVIEW" or any(
                nodes[parent]["state"] == "WAITING_HANDOFF" and
                key[6:] in nodes[parent].get("predecessor_scene_ids", [])
                for parent in chapter["parents"]
            )
        reviews = [key for key in reviews if chapter_ready(key)]
    reading_wip = sum(busy[s] for s in READING_STAGES | {"reassessment", "internal_verification"})
    if reviews and reading_wip < limits["review"]:
        return min(reviews)
    if len(reviews) + reading_wip >= limits["review_backlog"]:
        return None
    for stage in ("plan", "draft"):
        if busy[stage] < limits[stage]:
            pending = sorted(k for k, n in nodes.items() if n["stage"] == stage and n["state"] == "PENDING")
            if pending:
                return pending[0]
    return None


def _enroll(kernel, repo, gateway, owner, board, identity, *, frozen_assignment=None):
    from novel_factory.application.supervisor import RuntimeSupervisor

    node = board["nodes"][identity]
    wr, stage = node.get("world_ref", board["world_ref"]), node["stage"]
    work = wr["work_id"]
    cut, refs, materials = {"plane": "WORLD"}, [], []
    story_time = board["story_time"]
    if stage == "plan":
        manifest_ref = node.get("manifest_ref", board["manifest_ref"])
        cut = {"plane": "PLAN", "plan_manifest_ref": manifest_ref}
        require(_selected(repo, work, "PLAN_MANIFEST:" + board["volume_id"]) == manifest_ref,
                "STALE_PLAN", "Source plan changed before enrollment")
        contract = kernel.artifact(repo, node["contract_ref"], work, kind="VOLUME_CONTRACT")["payload"]
        refs = [manifest_ref, node["contract_ref"]]
        assignment = ("在下列前卷假设条件下为未来卷提供候选细纲，使用获准时点之后的窗口，不宣称结局已经发生。"
                      + json.dumps({"contract_revision_id": contract["contract_revision_id"],
                                    "brief": contract["creative_brief"], "intentions": contract["narrative_intents"],
                                    "assumptions": node["dependencies"], "goal_limit": board["limits"]["window_goals"],
                                    "authorized_subject_ids": board["subject_ids"]},
                                   ensure_ascii=False))
    elif stage in {"reassessment", "internal_verification"}:
        from .internal_review_resolution import assignment as verification_assignment
        from .production_reassessment import reassessment_assignment
        request = node["internal_case"] if stage == "internal_verification" else node["reassessment_request"]
        manifest_ref = board["manifest_ref"]
        if stage == "reassessment":
            require(_selected(repo, work, "PLAN_MANIFEST:" + board["volume_id"]) == manifest_ref,
                    "STALE_PLAN", "Diagnosis plan changed before enrollment")
            cut = {"plane": "PLAN", "plan_manifest_ref": manifest_ref}
        materials = [request["source_draft_ref"], request["report_ref"]]
        refs = [request["report_ref"], *([manifest_ref] if stage == "reassessment" else [])]
        if stage == "internal_verification":
            for key in ("source_ref", "extraction_ref"):
                if request.get(key) and request[key] not in refs:
                    refs.append(request[key])
        assignment = verification_assignment(request) if stage == "internal_verification" else reassessment_assignment(request)
    elif stage == "draft":
        scene = kernel.artifact(repo, node["scene_ref"], work, kind="SCENE_PLAN")["payload"]
        materials = [node["scene_ref"]]
        if board.get("continuity_ref"):
            materials.append(board["continuity_ref"])
        refs = [board["plan_ref"], scene["batch_ref"], *node["handoff_refs"]]
        assignment = "依据获准场景卡写出正文，保留人物视角、披露边界和已接受前序交接。"
        if extent := node.get("han_characters"):
            target = (extent["min"] + extent["max"]) // 2
            assignment += (f"本场正文分配 {extent['min']}–{extent['max']} 个汉字，目标约 {target} 个。"
                           "字数只计 text 中的汉字，标点、空白和 JSON 字段不计。")
    else:
        materials = [board["nodes"][p]["output_ref"] for p in node["parents"]]
        assignment = "独立阅读本章给定顺序的全部场景，报告文学观察、证据和仍未判断的问题。"
    if stage == "review" and (board.get("production_mode") == "quality" or board.get("max_reworks")):
        assignment += "须改的具体问题标为 BLOCKER 或 CHANGE_REQUEST；每项 subject_ref 引用给定原稿的 draft_ref。可选的文学意见列为建议。"
    if stage == "facts":
        from .rolling_facts import assignment as fact_assignment
        parent = board["nodes"][node["parents"][0]]
        scene = kernel.artifact(repo, parent["scene_ref"], work, kind="SCENE_PLAN")["payload"]
        scope = {"subject_ids": board["subject_ids"], "predicates": board["predicates"],
                 "story_interval": scene["scene_contract"]["story_interval"]}
        assignment = fact_assignment() + "\n可核对的本场标识与时段：" + json.dumps(scope, ensure_ascii=False)
        # Qualify the source of reconciliation obligations for this exact draft.
        refs.append(board["nodes"][node["parents"][0]]["scene_ref"])
        for ref in node.get("internal_resolution_refs", []):
            resolved = kernel.artifact(repo, ref, work, kind="INTERNAL_REVIEW_RESOLUTION")["payload"]["request"]
            if resolved["source_draft_ref"] == parent.get("output_ref") and resolved["world_ref"] == wr:
                materials.append(ref)
        role, kind = "evidence_maintainer", "assertion_extract"
    elif stage == "epistemic":
        parent = board["nodes"][node["parents"][0]]
        scene = kernel.artifact(repo, parent["scene_ref"], work, kind="SCENE_PLAN")["payload"]
        story_time = scene["scene_contract"]["story_interval"]["end"]
        assignment = "独立通读本场正文，逐项核查人物所知、获知来源及披露时序。把无来源、时序矛盾或无法判断的知识归为问题，引用原句及可用证据。明确区分可验证的硬性认知矛盾、疑问和可选的文学建议，标注准确的严重程度与处置状态。"
        role, kind = "reviewer", "epistemic"
    elif stage == "internal_verification":
        role, kind = "reviewer", "verification"
    elif stage == "reassessment":
        role, kind = "chief_editor", "editorial_decision"
    else:
        role, kind = ROLES[stage]
    if stage == "draft" and node.get("prior_draft_ref"):
        role, kind = "writer", "targeted_revision"
        materials.append(node["prior_draft_ref"])
        refs.append(node["revision_feedback"]["report_ref"])
        for label in ("reflection_ref", "error_ref"):
            if (ref := node["revision_feedback"].get(label)) and ref not in refs:
                refs.append(ref)
        if strategy_ref := node["revision_feedback"].get("strategy_ref"):
            refs.append(strategy_ref)
        if strategy := node["revision_feedback"].get("strategy_instruction"):
            assignment += strategy
        assignment += "请只修订指出的问题并返回完整新正文；其余原稿的视角、措辞和事件边界尽量保留。"
        if frozen_assignment is None:
            brief = _artifact(kernel, repo, work, owner, "PRODUCTION_REVISION_BRIEF", {
                "world_ref": wr, "scene_ref": node["scene_ref"], "prior_draft_ref": node["prior_draft_ref"],
                "feedback": node["revision_feedback"]})
            node["revision_brief_ref"] = brief
            materials.append(brief)
            refs.append(brief)
            assignment += "完整修订问题在 authorized_materials 的 PRODUCTION_REVISION_BRIEF 中，逐项核验，不得忽略硬问题。"
        elif node.get("revision_brief_ref"):
            materials.append(node["revision_brief_ref"])
            refs.append(node["revision_brief_ref"])
        else:
            assignment += json.dumps(node["revision_feedback"]["findings"], ensure_ascii=False)
    seconds = int((datetime.fromisoformat(board["authorization"]["deadline"]) - repo.now()).total_seconds())
    require(seconds > 0, "TASK_EXPIRED", "No time remains")
    repair = (node.get("schema_repairs") or [None])[-1]
    if node.get("reason") == "READING_POLICY_UPGRADED":
        repair = None
    if stage == "facts" and node.get("reason") == "FACT_FORMAT_REPAIR_REQUESTED":
        repair = node["fact_format_repairs"][-1]
        require(repair["source_draft_ref"] == materials[0], "STALE_GENERATION", "Repaired reading source changed")
    if repair and repair.get("actual_fix"):
        repair = None
    if repair:
        assignment += "\n上一次任务的结构化输出诊断与修复方法：" + json.dumps(
            {"diagnostic": repair["diagnostic"], "method": repair.get("reassessment", repair)["method"]}, ensure_ascii=False)
    evidence_repair = node.get("reason") == "REVIEW_EVIDENCE_REREAD"
    if evidence_repair:
        assignment += "\n审核证据补查：" + node["evidence_repairs"][-1]["method"]
    verification_repair = node.get("reason") == "INTERNAL_VERIFICATION_REPAIR_REQUIRED"
    if verification_repair:
        require(node["verification_repair"]["case_hash"] == node["internal_case"]["case_hash"],
                "STALE_GENERATION", "Internal correction changed its frozen questions")
        assignment += "\n核查答复校正：" + node["verification_repair"]["method"]
    if frozen_assignment is not None:
        # Only the administrator lost-review recovery supplies this already
        # authenticated immutable input. Do not regenerate historical wording.
        assignment = frozen_assignment
    task = RuntimeTaskService(kernel, gateway).prepare(owner, wr, role_id=role, task_kind=kind,
        skill_name="edit-volume" if stage == "reassessment" else None,
        review_belief=stage in {"epistemic", "internal_verification"},
        knowledge_reread_reason="known_error" if repair or evidence_repair or verification_repair
        or node.get("reason") == "READING_POLICY_UPGRADED" else None,
        assignment=assignment, knowledge_cut=cut, subject_ids=board["subject_ids"], predicates=board["predicates"],
        story_time=story_time, material_refs=materials, input_artifacts=refs,
        adapter_id=board["routes"]["review" if stage in READING_STAGES | {"reassessment", "internal_verification"} else stage], max_amount=board["reserve_amount"], token_budget=board["token_reservation"],
        currency=repo.one("budgets", {"work_id": work, "budget_id": board["budget_id"]})["currency"],
        queue_state="READY", lease_seconds=min(600, seconds), deadline_seconds=seconds)
    if stage in {"reassessment", "internal_verification"}:
        frozen_input = kernel.artifact(repo, task.agent_input["input_ref"], work, kind="AGENT_INPUT")["payload"]
        require(gateway.adapters[frozen_input["adapter_id"]].configuration.get("adapter_kind") == "codex-cli",
                "MODEL_UNAVAILABLE", "Production strategy diagnosis requires Codex")
        if verification_repair:
            require(all(frozen_input[key] == node["verification_repair"][key]
                        for key in ("runtime_sha256", "model_config_sha256", "output_contract")),
                    "TASK_REBIND_REQUIRED", "Internal correction cannot change its role, model or output contract")
    if repair:
        node["active_format_repair"] = {
            "history": "fact_format_repairs" if stage == "facts" and node.get("reason") == "FACT_FORMAT_REPAIR_REQUESTED" else "schema_repairs",
            "source_task_id": repair["task_id"],
        }
        frozen = kernel.artifact(repo, task.agent_input["input_ref"], work, kind="AGENT_INPUT")["payload"]
        require(all(frozen[k] == repair[k] for k in ("runtime_sha256", "model_config_sha256", "output_contract")),
                "TASK_REBIND_REQUIRED", "Technical repair cannot silently change the role, model or output contract")
        node.pop("reason", None)
    supervisor = RuntimeSupervisor(kernel, gateway)
    role_grant = repo.one("grants", {"grant_id": task.principal.grant_id})
    config, _ = supervisor.dispatch_config(repo, task.spec, role_grant, manifest_ref=task.manifest_ref,
        knowledge_cut=cut, context_tokens=min(100000, board["token_reservation"]) if stage == "facts" else 20000,
        input_ref=task.agent_input["input_ref"],
        budget_id=board["budget_id"], reserve_amount=board["reserve_amount"], token_reservation=board["token_reservation"])
    supervisor.install_job(repo, task.task, {**config, "production_node": identity,
        **({"required_review_mcp": True} if stage in READING_STAGES | {"reassessment", "internal_verification"}
           and gateway.adapters[kernel.artifact(repo, task.agent_input["input_ref"], work)["payload"]["adapter_id"]]
               .configuration.get("adapter_kind") == "codex-cli" else {}),
        **({"production_revision_brief_ref": node["revision_brief_ref"]}
           if stage == "draft" and node.get("revision_brief_ref") else {})})
    node.update(state="ENROLLED", task_id=task.task["task_id"], enrolled_at=repo.now().isoformat())
    repo.conn.execute(text("INSERT INTO nf_trace_tasks(task_id,trace_id,volume_id) VALUES (:task,:trace,:volume)"),
                      {"task": task.task["task_id"], "trace": node.get("chapter_id", board["series_id"]),
                       "volume": node.get("volume_id", board["volume_id"])})


def guard_send(kernel, repo, task, job, request=None):
    board = _load(kernel, repo, task["work_id"], task["branch_id"])
    node = board and board["nodes"].get(job["production_node"])
    require(node and node["state"] == "ENROLLED" and node["task_id"] == task["task_id"],
            "VIEW_DENIED", "Production handoff no longer authorizes this task")
    owner = _current_owner(kernel, repo, board)
    if request is not None:
        require(request["call_key"] == "supervised-candidate"
                and all(request[k] == v for k, v in job["dispatch"].items()),
                "VIEW_DENIED", "The production task requires its enrolled call")
    return board, node, owner


def production_material_reader(kernel):
    """Install only the exact chapter/scene reading port for enrolled production jobs."""
    def read(owner, *, task_id, generation, context_bundle_id, artifact_ref):
        with kernel.db.uow(write=False) as repo:
            job = repo.one("supervisor_jobs", {"task_id": task_id})
            if not job or not ({"production_node", "returned_edition"} & job["config"].keys()):
                return None
            if "returned_edition" in job["config"]:
                from .returned_edition import material
                value = material(kernel, owner, task_id, generation, context_bundle_id, artifact_ref)
                if value is not None:
                    return value
            task = repo.one("tasks", {"task_id": task_id})
            board = _load(kernel, repo, task["work_id"], task["branch_id"])
            node = board and board["nodes"].get(job["config"].get("production_node"))
            if node and node["stage"] == "internal_verification" and artifact_ref != node["internal_case"]["source_draft_ref"]:
                task, _, spec, _ = NarrativeService(kernel)._task(repo, owner, task_id, generation)
                request = node["internal_case"]
                frozen = kernel.artifact(repo, job["config"]["dispatch"]["input_ref"], task["work_id"], kind="AGENT_INPUT")["payload"]
                assigned = [strict_json_loads(line) for line in frozen["assignment"].splitlines() if line.startswith("{")]
                require(node["state"] == "ENROLLED" and node.get("task_id") == task_id
                        and task["context_bundle_id"] == context_bundle_id
                        and (task["role_id"], task["task_kind"]) == ("reviewer", "verification")
                        and artifact_ref in frozen["material_refs"] and artifact_ref in spec["input_artifacts"]
                        and spec["world_ref"] == request["world_ref"]
                        and any(a.get("case_hash") == request["case_hash"] for a in assigned),
                        "VIEW_DENIED", "Internal evidence is outside its exact frozen verification case")
                source = kernel.artifact(repo, artifact_ref, task["work_id"])
                diagnostic_source = {}
                if request["source_kind"] == "DIAGNOSIS":
                    diagnosis = kernel.artifact(repo, request["source_ref"], task["work_id"], kind="PRODUCTION_REASSESSMENT")["payload"]
                    original = diagnosis["request"]
                    diagnostic_source = {"diagnosis_source_ref": request["source_ref"],
                        "diagnosis_request_sha256": fingerprint(original),
                        "diagnosis_source": {k: original[k] for k in
                                             ("world_ref", "source_draft_ref", "report_ref", "findings")},
                        "diagnosis_outcome": diagnosis["outcome"], "diagnosis_reason": diagnosis["reason"]}
                return canonical_bytes({"source_ref": artifact_ref, "source_kind": source["kind"],
                    "projection": "INTERNAL_CASE_WINDOW", "source_payload_sha256": fingerprint(source["payload"]),
                    "case": request, **diagnostic_source,
                    **({"report": source["payload"]} if request["source_kind"] == "DIAGNOSIS" else {})})
            if node and node["stage"] == "facts" and artifact_ref in node.get("internal_resolution_refs", []):
                task, _, spec, _ = NarrativeService(kernel)._task(repo, owner, task_id, generation)
                require(node["state"] == "ENROLLED" and node.get("task_id") == task_id
                        and task["context_bundle_id"] == context_bundle_id
                        and artifact_ref in spec["input_artifacts"], "VIEW_DENIED", "Unassigned internal resolution")
                record = kernel.artifact(repo, artifact_ref, task["work_id"], kind="INTERNAL_REVIEW_RESOLUTION")["payload"]
                require(record["request"]["source_draft_ref"] == board["nodes"][node["parents"][0]]["output_ref"],
                        "STALE_GENERATION", "Internal resolution belongs to another draft")
                questions = {q["index"]: q for q in record["request"]["questions"]}
                evidence_table, answers = [], []
                for answer in record["answers"]:
                    indices = []
                    for cited in answer["evidence_refs"]:
                        if cited not in evidence_table:
                            evidence_table.append(cited)
                        indices.append(evidence_table.index(cited))
                    answers.append({**{k: v for k, v in answer.items() if k != "evidence_refs"},
                        "evidence_indices": indices, "assertion_id": questions[answer["index"]].get("assertion_id"),
                        "span": questions[answer["index"]].get("span")})
                return canonical_bytes({"source_ref": artifact_ref, "projection": "VERIFIED_FINDINGS",
                    "resolution": {"case_hash": record["request"]["case_hash"],
                        "source_draft_ref": record["request"]["source_draft_ref"], "world_ref": record["request"]["world_ref"],
                        "outcome": record["outcome"], "method": record["method"],
                        "evidence_table": evidence_table, "answers": answers}})
            brief_ref = job["config"].get("production_revision_brief_ref")
            if brief_ref and artifact_ref == brief_ref:
                task, _, spec, _ = NarrativeService(kernel)._task(repo, owner, task_id, generation)
                require(node and node["state"] == "ENROLLED" and node.get("task_id") == task_id
                        and task["context_bundle_id"] == context_bundle_id
                        and (task["role_id"], task["task_kind"]) == ("writer", "targeted_revision")
                        and artifact_ref in spec["input_artifacts"],
                        "VIEW_DENIED", "Revision brief is outside this writer task")
                brief = kernel.artifact(repo, brief_ref, task["work_id"], kind="PRODUCTION_REVISION_BRIEF")["payload"]
                require(brief["world_ref"] == spec["world_ref"] and brief["scene_ref"] == node["scene_ref"]
                        and brief["prior_draft_ref"] == node["prior_draft_ref"]
                        and brief["feedback"] == node["revision_feedback"],
                        "STALE_GENERATION", "Revision brief differs from the enrolled draft and feedback")
                return canonical_bytes({"source_ref": brief_ref, **brief})
            if node and node["stage"] == "reassessment" and artifact_ref == node["reassessment_request"]["report_ref"]:
                from .production_reassessment import assignment_request_sha256
                task, _, spec, _ = NarrativeService(kernel)._task(repo, owner, task_id, generation)
                request = node["reassessment_request"]
                frozen = kernel.artifact(repo, job["config"]["dispatch"]["input_ref"], task["work_id"], kind="AGENT_INPUT")["payload"]
                require(node["state"] == "ENROLLED" and node.get("task_id") == task_id
                        and task["context_bundle_id"] == context_bundle_id
                        and (task["role_id"], task["task_kind"]) == ("chief_editor", "editorial_decision")
                        and artifact_ref in spec["input_artifacts"] and artifact_ref in frozen["material_refs"]
                        and assignment_request_sha256(frozen["assignment"]) == fingerprint(request)
                        and spec["world_ref"] == request["world_ref"],
                        "VIEW_DENIED", "Diagnosis report is outside this frozen reading task")
                report = kernel.artifact(repo, artifact_ref, task["work_id"])
                return canonical_bytes({"source_ref": artifact_ref, "report": report["payload"],
                    "findings": request["findings"], "prior_methods": request["prior_methods"],
                    "history_fingerprint": request["history_fingerprint"]})
        return NarrativeService(kernel).read_agent_material(owner, task_id=task_id, generation=generation,
            context_bundle_id=context_bundle_id, artifact_ref=artifact_ref)
    return read


def prepare_materials(kernel, task, config):
    """Bind compiled reader/writer generations before the existing paid-call fence."""
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": task["work_id"]}, lock="update")
        bound = kernel.in_transaction(repo)
        board, node, owner = guard_send(bound, repo, task, config)
        tid, gen = task["task_id"], task["generation"]
        key = f"production:{tid}:{gen}"
        if node["stage"] == "draft":
            scene = bound.artifact(repo, node["scene_ref"], task["work_id"], kind="SCENE_PLAN")["payload"]
            PlanningService(bound).bind_task_for_controller(owner, tid, gen, board["plan_ref"], scene["plan_node_ids"], key)
            actor = Principal(task["grant_id"], task["principal_id"])
            ReleaseService(bound).bind_writer_task(actor, tid, gen, node["scene_ref"], node["handoff_refs"], key)
            if node.get("prior_draft_ref"):
                NarrativeService(bound).bind_reader_task(owner, tid, gen, [node["prior_draft_ref"]], key)
        elif node["stage"] in READING_STAGES | {"reassessment", "internal_verification"}:
            drafts = [board["nodes"][p]["output_ref"] for p in node["parents"]]
            for parent in node["parents"]:
                source = repo.one("tasks", {"task_id": board["nodes"][parent]["task_id"]})
                require(source["principal_id"] != task["principal_id"] and source["grant_id"] != task["grant_id"],
                        "INDEPENDENT_REVIEW_REQUIRED", "Chapter reader must differ from every writer")
            NarrativeService(bound).bind_reader_task(owner, tid, gen, drafts, key)


def _deliver(kernel, repo, gateway, board, node, task):
    from novel_factory.application.semantic_dependencies import check_task_dependencies

    from .board_schema_repair import record_contract_recovery

    owner = _current_owner(kernel, repo, board)
    check_task_dependencies(repo, task["task_id"], task["generation"])
    evidence, candidate = CandidateDelivery(kernel, gateway).audited_candidate(
        repo, task, production_accounting=bool(board.get("rolling")))
    model_audit = evidence
    key = "production-result:" + task["task_id"]
    if node["stage"] == "draft":
        actor = Principal(task["grant_id"], task["principal_id"])
        ref = NarrativeService(kernel).store_draft(actor, task["task_id"], task["generation"],
            node["scene_ref"], candidate["text"], key, declared_important_deltas=candidate["important_deltas"],
            handoff_refs=node["handoff_refs"], prior_draft_ref=node.get("prior_draft_ref"), agent_output_ref=evidence["output_ref"])
        from .scene_revision import repair_scene_extent
        if repair_scene_extent(kernel, repo, owner, board, node, task, evidence, ref, candidate["text"]):
            record_contract_recovery(node, task, evidence, ref)
            kernel.tasks.accept_in_transaction(repo.conn, task["task_id"], task["generation"])
            return
        state = "DRAFT_STORED"
    elif node["stage"] == "internal_verification":
        from .production_verification import deliver_verification
        ref, state = deliver_verification(kernel, repo, owner, board, node, task, evidence, candidate)
    elif node["stage"] == "reassessment":
        from .production_reassessment import record_reassessment
        ref, state = record_reassessment(kernel, repo, owner, board, node, task, evidence, candidate)
    elif node["stage"] == "epistemic":
        from .rolling_facts import record_knowledge
        ref, state = record_knowledge(kernel, repo, owner, board, node, task, evidence, candidate)
        if state == "STOPPED":
            node["reason"] = "EPISTEMIC_REVIEW_REQUIRED"
    elif node["stage"] == "facts":
        from .rolling_facts import audit
        evidence = audit(kernel, repo, owner, board, node, task, candidate)
        ref = _artifact(kernel, repo, task["work_id"], owner, "ROLLING_FACT_REPORT", evidence)
        if (repairs := node.get("fact_format_repairs")) and not repairs[-1].get("actual_fix"):
            repairs[-1].update(actual_fix=ref, corrected_attempt_id=model_audit["attempt_id"],
                               regression_result="ASSERTION_READING_VALIDATED")
        if evidence["status"] == "CLEAR":
            state = "FACTS_ACCEPTED"
        else:
            state = "STOPPED"
            node["reason"] = "NARRATIVE_ALIGNMENT_REQUIRED"
    elif node["stage"] == "review":
        drafts = [board["nodes"][p]["output_ref"] for p in node["parents"]]
        ref = _artifact(kernel, repo, task["work_id"], owner, "CHAPTER_READING_ADVICE",
                        {"draft_refs": drafts, "generation": task["generation"], "task_id": task["task_id"],
                         "candidate_ref": evidence["output_ref"], "literary_quality": "NOT_EVALUATED"})
        state = "ADVISORY_REVIEW"
        if (board.get("production_mode") == "quality" or board.get("max_reworks")) and (issues := editorial_blockers(candidate["issues"])):
            state = "STOPPED"
            node["reason"] = "CHAPTER_REWORK_REQUIRED"
            node["revision_targets"] = [key for key in node["parents"]
                                        if any(issue["subject_ref"] == board["nodes"][key]["output_ref"] for issue in issues)]
    else:
        wr = node.get("world_ref", board["world_ref"])
        require(candidate["input_world_ref"] == wr and candidate["dependency_fingerprint"] == task["dependency_fingerprint"]
                and point_key(candidate["story_interval"]["start"]) > point_key(board["story_time"])
                and 0 < len(candidate["candidate_goals"]) <= board["limits"]["window_goals"]
                and set(candidate["scope_ids"] + candidate["focus_actor_ids"]) <= set(board["subject_ids"]),
                "WINDOW_CONFLICT", "Candidate is outside its authorized future window")
        context = repo.one("contexts", {"context_bundle_id": task["context_bundle_id"]})["payload"]
        allowed = [r["event_ref"] for r in context["permitted_evidence_refs"] if r.get("kind") == "EVENT"]
        require(all(r in allowed for r in candidate["adopted_event_refs"]) and not candidate["process_ids"],
                "VIEW_DENIED", "Lookahead may cite only its delivered evidence")
        PlanningService(kernel)._validate_window(repo, kernel.revision(repo, wr), candidate, node["contract_ref"])
        ref = evidence["output_ref"]
        state = "PROVISIONAL_WITH_ASSUMPTIONS"
    if state in {"KNOWLEDGE_CLEAR", "FACTS_ACCEPTED", "ADVISORY_REVIEW"} and board.get("production_mode") == "quality":
        from .quality_memory import verify_method
        for target in node["parents"]:
            writer = board["nodes"][target]
            plans = writer.get("repair_plans", [])
            if not plans or plans[-1].get("review_stage") != node["stage"]:
                continue
            writer_call = repo.one("call_attempts", {"task_id": writer["task_id"], "generation": writer["generation"],
                                                    "call_key": "supervised-candidate"})
            writer_audit = gateway._read_audit(repo, writer_call["attempt_id"])["payload"]
            verify_method(kernel, repo, owner, board["world_ref"], plans[-1], writer_audit, "targeted_revision",
                          writer["output_ref"], model_audit["attempt_id"], ref)
    record_contract_recovery(node, task, model_audit, ref)
    kernel.tasks.accept_in_transaction(repo.conn, task["task_id"], task["generation"])
    node.update(state=state, output_ref=ref, generation=task["generation"], completed_at=repo.now().isoformat())


def _settlement(kernel, repo, board):
    work = board["world_ref"]["work_id"]
    ref = _selected(repo, work, "CONTINUITY:" + board["series_id"])
    if not ref:
        return None
    snapshot = kernel.artifact(repo, ref, work, kind="CONTINUITY_SNAPSHOT")["payload"]
    wr = snapshot["request"]["world_ref"]
    volumes = [v["volume_id"] for v in snapshot["volumes"]]
    if (board["volume_id"] not in volumes or wr["branch_id"] != board["world_ref"]["branch_id"]
            or kernel.head(repo, work, wr["branch_id"]) != wr or not repo.ancestor(board["world_ref"], wr)):
        return None
    return ref, snapshot, volumes


def _revalidate(kernel, repo, board, node, settlement):
    ref, snapshot, volumes = settlement
    previous = board["volume_id"] if node["distance"] == 1 else next(
        n["volume_id"] for n in board["nodes"].values() if n.get("distance") == node["distance"] - 1)
    if volumes[-1] != previous or node["volume_id"] in volumes:
        return None
    wr = snapshot["request"]["world_ref"]
    revision = kernel.revision(repo, wr)
    plan_ref = _selected(repo, wr["work_id"], PlanningService.selection_key(board["volume_id"]))
    plan = kernel.artifact(repo, plan_ref, wr["work_id"], kind="PLAN_REVISION")["payload"]
    require(plan["input_world_ref"]["branch_id"] == wr["branch_id"], "STALE_PLAN", "Source plan branch changed")
    hashes = {n["node_id"]: fingerprint(n) for n in plan["nodes"]}
    verdicts = []
    for dep in node["dependencies"]:
        if dep["kind"] == "WORLD_FACT":
            verdicts.append(condition_result(dep["condition"], revision["snapshot"], revision["selection"]))
        else:
            verdicts.append("SATISFIED" if hashes.get(dep["node_id"]) == dep["node_hash"] else "BROKEN")
    return {"state": "READY_FOR_ADOPTION" if all(v == "SATISFIED" for v in verdicts) else "REPLAN_REQUIRED",
            "settlement_ref": ref, "checked_world_ref": wr, "dependency_verdicts": verdicts, "source_plan_ref": plan_ref}


def transfer_window(kernel, owner, *, world_ref, candidate_id, idempotency_key):
    """After checked continuity, copy only a revalidated draft window to native planning."""
    validate_contract("UUID", candidate_id)
    validate_contract("WorldRef", world_ref)
    PlanningService._key(idempotency_key)
    work, branch = world_ref["work_id"], world_ref["branch_id"]
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": work}, lock="update")
        PlanningService(kernel)._authorize(repo, owner, world_ref, capability="ADMIN")
        request = {"candidate_id": candidate_id, "world_ref": world_ref}
        prior = kernel._prior(repo, owner, work, "PRODUCTION_TRANSFER", idempotency_key, request)
        if prior:
            return prior
        board = _load(kernel, repo, work, branch)
        node = board and board["nodes"].get("plan:" + candidate_id)
        require(node and node["state"] in {"READY_FOR_ADOPTION", "PROVISIONAL_WITH_ASSUMPTIONS"},
                "CONTINUITY_REVALIDATION_REQUIRED", "No eligible provisional window")
        _current_owner(kernel, repo, board)
        settlement = _settlement(kernel, repo, board)
        verdict = settlement and _revalidate(kernel, repo, board, node, settlement)
        require(verdict and verdict["state"] == "READY_FOR_ADOPTION" and verdict["checked_world_ref"] == world_ref,
                "CONTINUITY_REVALIDATION_REQUIRED", "The current published continuity must satisfy every assumption")
        source = kernel.artifact(repo, node["output_ref"], work, kind="AGENT_CANDIDATE")["payload"]
        # The original attempt and candidate stay frozen at their provisional cut.
        from uuid import uuid4
        current = {**source, "window_plan_id": str(uuid4()), "input_world_ref": world_ref,
                   "dependency_fingerprint": fingerprint({"candidate": node["output_ref"], "review": verdict})}
        ref = PlanningService(kernel.in_transaction(repo)).save_window_plan(owner, current, node["contract_ref"], idempotency_key)
        node.update(verdict, state="WINDOW_TRANSFERRED", window_ref=ref)
        result = {"window_ref": ref, "source_ref": node["output_ref"], "board_ref": _save(kernel, repo, owner, board)}
        kernel._receipt(repo, owner, world_ref, "PRODUCTION_TRANSFER", idempotency_key, request, result)
        return result


def revise_window(kernel, owner, *, world_ref, candidate_id, expected_candidate_ref, assumptions, idempotency_key):
    """Authorize one new attempt only for a rejected provisional window; preserve every prior artifact."""
    validate_contract("UUID", candidate_id)
    validate_contract("WorldRef", world_ref)
    validate_contract("ArtifactRef", expected_candidate_ref)
    PlanningService._key(idempotency_key)
    work, branch = world_ref["work_id"], world_ref["branch_id"]
    request = {"candidate_id": candidate_id, "world_ref": world_ref,
               "expected_candidate_ref": expected_candidate_ref, "assumptions": assumptions}
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": work}, lock="update")
        PlanningService(kernel)._authorize(repo, owner, world_ref, capability="ADMIN")
        prior = kernel._prior(repo, owner, work, "PRODUCTION_REVISE", idempotency_key, request)
        if prior:
            return prior
        board = _load(kernel, repo, work, branch)
        key = "plan:" + candidate_id
        node = board and board["nodes"].get(key)
        require(node and node["state"] == "REPLAN_REQUIRED" and node["output_ref"] == expected_candidate_ref,
                "STALE_PLAN", "A revision must select the exact invalidated candidate")
        _current_owner(kernel, repo, board)
        settlement = _settlement(kernel, repo, board)
        require(settlement and settlement[1]["request"]["world_ref"] == world_ref,
                "CONTINUITY_REVALIDATION_REQUIRED", "Revision requires the current published predecessor")
        history = node.get("history", [])
        quality = board.get("production_mode") == "quality"
        if not quality:
            require(len(history) < 2, "CAPACITY_LIMIT", "The run allows two explicit local revisions per candidate")
        plan_ref = _selected(repo, work, PlanningService.selection_key(board["volume_id"]))
        plan = kernel.artifact(repo, plan_ref, work, kind="PLAN_REVISION")["payload"]
        replacement = {k: node[k] for k in ("stage", "volume_id", "distance", "contract_ref")}
        replacement.update(state="PENDING", dependencies=_dependencies(assumptions, {n["node_id"]: n for n in plan["nodes"]},
                                  board["subject_ids"], board["predicates"]),
                           world_ref=world_ref, manifest_ref=_selected(repo, work, "PLAN_MANIFEST:" + board["volume_id"]),
                           history=[*history, {k: v for k, v in node.items() if k != "history"}])
        verdict = _revalidate(kernel, repo, board, replacement, settlement)
        require(verdict and verdict["state"] == "READY_FOR_ADOPTION", "STALE_PLAN",
                "Revised assumptions already conflict with the published predecessor")
        if quality:
            basis = fingerprint([settlement[0], world_ref, plan_ref, replacement["dependencies"]])
            require(all(old.get("revision_basis") != basis for old in replacement["history"]),
                    "STRATEGY_REASSESSMENT_REQUIRED", "Reassess the unchanged assumptions and frozen evidence")
            replacement["revision_basis"] = basis
        board["nodes"][key] = replacement
        result = {"status": "REVISION_PENDING", "node_id": key, "prior_candidate_ref": expected_candidate_ref,
                  "board_ref": _save(kernel, repo, owner, board)}
        kernel._receipt(repo, owner, world_ref, "PRODUCTION_REVISE", idempotency_key, request, result)
        repo.conn.execute(text("SELECT pg_notify('nf_supervisor_wake', '')"))
        return result


def input_requirement(kernel, repo, gateway, task):
    """Bind a returned NEEDS_INPUT diagnostic without adopting its candidate or settling its bill."""
    call = repo.one("call_attempts", {"task_id": task["task_id"], "generation": task["generation"],
                                     "call_key": "supervised-candidate"})
    require(call and task["state"] == "BLOCKED"
            and task["current_result_id"] == str(uuid5(UUID(call["attempt_id"]), "task-result")),
            "INVALID_EVIDENCE", "Input requirement is not the current returned call")
    result = repo.one("task_results", {"task_id": task["task_id"], "generation": task["generation"],
                                      "result_id": task["current_result_id"], "disposition": "CURRENT"})
    audit_row = gateway._read_audit(repo, call["attempt_id"])
    require(result and audit_row, "INVALID_EVIDENCE", "Input requirement evidence unavailable")
    audit = audit_row["payload"]
    audit_ref = {key: audit_row[key] for key in ("artifact_id", "artifact_revision_id", "sha256")}
    require(audit["outcome"] == "VALIDATED_CANDIDATE" and audit["result_status"] == "NEEDS_INPUT"
            and audit["attempt_id"] == call["attempt_id"] and audit["task_id"] == task["task_id"]
            and audit["generation"] == task["generation"]
            and audit["role_id"] == task["role_id"]
            and (audit["world_ref"]["work_id"], audit["world_ref"]["branch_id"])
            == (task["work_id"], task["branch_id"])
            and audit["context_bundle_id"] == task["context_bundle_id"]
            and fingerprint(audit) == audit_ref["sha256"]
            and result["payload"]["status"] == "NEEDS_INPUT"
            and result["payload"]["artifacts"] == [audit_ref, audit["output_ref"]],
            "INVALID_EVIDENCE", "Input requirement does not match its task result and audit")
    response = kernel.artifact(repo, audit["response_ref"], task["work_id"], kind="AGENT_RESPONSE")["payload"]
    require(fingerprint(response) == audit["response_ref"]["sha256"],
            "INVALID_EVIDENCE", "Input requirement response hash differs")
    envelope = strict_json_loads(response["text"])
    validate_contract("AgentModelResponse", envelope, "agent")
    require(envelope["status"] == "NEEDS_INPUT" and all(envelope[key] == audit[key] for key in
            ("task_id", "generation", "role_id", "actor_id", "world_ref", "context_bundle_id")),
            "INVALID_EVIDENCE", "Input requirement response identity differs")
    return {"task_id": task["task_id"], "generation": task["generation"], "attempt_id": call["attempt_id"],
            "result_id": task["current_result_id"], "evidence_ref": audit_ref, "response_ref": audit["response_ref"],
            "input_ref": audit["input_ref"], "unresolved_question_count": len(envelope["unresolved_questions"])}


def advance_pipeline(kernel, gateway, controller, work, branch):
    """Project one recoverable handoff for the existing supervisor."""
    with kernel.db.uow(write=False) as repo:
        kernel.authorize(repo, controller, work, "ADMIN", branch)
        if not _selected(repo, work, _key(branch)):
            return None
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": work}, lock="update")
        kernel.authorize(repo, controller, work, "ADMIN", branch)
        repo.one("branches", {"work_id": work, "branch_id": branch}, lock="share")
        bound = kernel.in_transaction(repo)
        board = _load(bound, repo, work, branch)
        nodes = board["nodes"]
        expired = repo.now() >= datetime.fromisoformat(board["authorization"]["deadline"])
        if not expired:
            from .production_verification import consume_verification, queue_verification
            if verified := consume_verification(bound, repo, _current_owner(bound, repo, board), board):
                return {"status": "INTERNAL_VERIFICATION_UPDATED", "node_id": verified,
                        "board_ref": _save(bound, repo, controller, board)}
            if queued := queue_verification(bound, repo, board):
                return {"status": "INTERNAL_VERIFICATION_QUEUED", "node_id": queued,
                        "board_ref": _save(bound, repo, controller, board)}
            from .production_strategy import consume_strategy, resume_unsent_strategy
            if consumed := consume_strategy(bound, repo, board):
                return {"status": "STRATEGY_CONSUMED", "node_id": consumed,
                        "board_ref": _save(bound, repo, controller, board)}
            if resumed := resume_unsent_strategy(board):
                return {"status": "STRATEGY_DIAGNOSIS_RESUMED", "node_id": resumed,
                        "board_ref": _save(bound, repo, controller, board)}
        settlement = _settlement(bound, repo, board) if any(n["state"] in {
            "PROVISIONAL_WITH_ASSUMPTIONS", "READY_FOR_ADOPTION"} for n in nodes.values()) else None
        if (board.get("production_mode") == "quality" or board.get("max_reworks")) and not expired and any(n.get("output_ref") and n["state"] == "STOPPED" for n in nodes.values()):
            _current_owner(bound, repo, board)
            revised = revise_scene(bound, repo, board)
            if revised:
                status = nodes[revised].get("reason", "SCENE_REVISION_REQUESTED")
                return {"status": status, "node_id": revised, "board_ref": _save(bound, repo, controller, board)}
        for identity, node in nodes.items():
            if board.get("production_mode") == "quality" and not expired and node.get("reason") == "STRATEGY_REASSESSMENT_REQUIRED":
                from .board_schema_repair import resume_fact_reading_format, resume_review_format
                from .scene_revision import resume_extent_revision
                owner = _current_owner(bound, repo, board)
                if (resume_extent_revision(bound, repo, owner, board, identity)
                        or resume_review_format(bound, repo, owner, board, identity)
                        or resume_fact_reading_format(bound, repo, gateway, owner, board, identity)):
                    break
            if (board.get("production_mode") == "quality" and not expired and node["stage"] == "facts"
                    and node["state"] == "STOPPED" and node.get("task_id") and not node.get("output_ref")
                    and node.get("reason") in {"INVALID_ARGUMENT", "INVALID_EVIDENCE"}):
                from .board_schema_repair import recover_fact_reading
                try:
                    with repo.conn.begin_nested():
                        recovered = recover_fact_reading(bound, repo, gateway, _current_owner(bound, repo, board),
                                                         board, identity)
                except DomainError as error:
                    if node.get("reason") == error.code:
                        continue
                    node["reason"] = error.code
                    break
                if recovered:
                    break
            if node["state"] == "WAITING_HANDOFF":
                if expired:
                    node.update(state="STOPPED", reason="TASK_EXPIRED")
                    break
                refs = [_selected(repo, work, "NARRATIVE_SCENE:" + parent) for parent in node["predecessor_scene_ids"]]
                if not all(refs):
                    continue
                node.update(state="PENDING", handoff_refs=refs)
                break
            if node["state"] in {"PROVISIONAL_WITH_ASSUMPTIONS", "READY_FOR_ADOPTION"}:
                if expired:
                    node.update(state="STOPPED", reason="TASK_EXPIRED")
                    break
                verdict = _revalidate(bound, repo, board, node, settlement) if settlement else None
                if verdict:
                    if all(node.get(k) == v for k, v in verdict.items()):
                        continue
                    node.update(verdict)
                elif node["state"] == "READY_FOR_ADOPTION":
                    node["state"] = "PROVISIONAL_WITH_ASSUMPTIONS"
                    for field in ("settlement_ref", "checked_world_ref", "dependency_verdicts", "source_plan_ref"):
                        node.pop(field, None)
                else:
                    continue
            elif node["state"] == "ENROLLED":
                task = repo.one("tasks", {"task_id": node["task_id"]}, lock="update")
                if task["state"] == "PAUSED" and board.get("production_mode") == "quality" and not expired:
                    from .scene_revision import restore_unsent_extent_source
                    if restore_unsent_extent_source(bound, repo, board, identity, task):
                        break
                if task["state"] == "BLOCKED" and task["current_result_id"]:
                    try:
                        node["input_requirement"] = input_requirement(bound, repo, gateway, task)
                        node.update(state="STOPPED", reason="MODEL_INPUT_REQUIRED")
                    except DomainError as error:
                        node.update(state="STOPPED", reason=error.code)
                elif task["state"] == "FAILED" and board.get("production_mode") == "quality" and not expired:
                    from .board_schema_repair import recover_board_node
                    try:
                        with repo.conn.begin_nested():
                            recover_board_node(bound, repo, gateway, _current_owner(bound, repo, board),
                                               board, identity, task)
                    except DomainError as error:
                        node.update(state="STOPPED", reason=error.code)
                elif task["state"] in {"PAUSED", "STALE", "FAILED", "CANCELLED", "WAITING_REVIEW"}:
                    node.update(state="STOPPED", reason="TASK_" + task["state"])
                elif task["state"] == "RESULT_STORED":
                    try:
                        with repo.conn.begin_nested():
                            _deliver(bound, repo, gateway, board, node, task)
                    except DomainError as error:
                        if error.code == "CALL_RECONCILIATION_REQUIRED":
                            continue
                        from .production_verification import recover_verification
                        from .review_recovery import recover_review
                        if not (recover_review(bound, repo, gateway, node, task, error)
                                or recover_verification(bound, repo, gateway, node, task, error)):
                            node.update(state="STOPPED", reason=error.code)
                elif expired and task["state"] in {"READY", "BLOCKED", "RUNNING"}:
                    node.update(state="STOPPED", reason="TASK_EXPIRED")
                else:
                    continue
            elif (node["stage"] in READING_STAGES and node["state"] == "STOPPED"
                  and node.get("reason") == "PREDECESSOR_STOPPED" and not node.get("task_id")
                  and not node.get("output_ref") and not expired
                  and all(nodes[parent]["state"] == "DRAFT_STORED" for parent in node["parents"])
                  and ("knowledge_node" not in node or nodes[node["knowledge_node"]]["state"] == "KNOWLEDGE_CLEAR")):
                # The previous hold was inherited. Nothing has been dispatched for this node.
                node.update(state="PENDING")
                node.pop("reason")
            elif node["stage"] in READING_STAGES and node["state"] == "PENDING" and (
                    any(nodes[p]["state"] == "STOPPED" for p in node["parents"]) or
                    ("knowledge_node" in node and nodes[node["knowledge_node"]]["state"] == "STOPPED")):
                node.update(state="STOPPED", reason="PREDECESSOR_STOPPED")
            else:
                continue
            break
        else:
            identity = _ready(board)
            if identity is None:
                return None
            node = nodes[identity]
            try:
                with repo.conn.begin_nested():
                    owner = _current_owner(bound, repo, board)
                    _enroll(bound, repo, gateway.with_kernel(bound), owner, board, identity)
            except DomainError as error:
                node.update(state="STOPPED", reason=error.code)
        ref = _save(bound, repo, controller, board)
        return {"status": "PRODUCTION_HANDOFF", "node_id": identity, "state": node["state"],
                "board_ref": ref, **{k: node[k] for k in ("reason", "task_id", "output_ref") if k in node}}
