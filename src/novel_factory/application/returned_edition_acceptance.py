"""Atomically accept a verified child edition while preserving its frozen parent."""
import json
from copy import deepcopy

from sqlalchemy import text

from ..domain.canonical import fingerprint
from ..domain.review_gate import blocking_review_issues
from .agent_gateway import AgentGateway
from .authorization import Principal
from .candidate_delivery import CandidateDelivery
from .kernel import require
from .narrative import NarrativeService
from .planning import PlanningService
from .production_pipeline import _artifact, _current_owner
from .review_mcp import require_review_queries
from .volume_successor import _selected

PACKET_KIND = "RETURNED_EDITION_PREDECESSORS"
PROOF_KIND = "RETURNED_EDITION_HANDOFF_PROOF"


def _reading(kernel, repo, wr, child, field, kind):
    task = repo.one("tasks", {"task_id": child[field]})
    writer = repo.one("tasks", {"task_id": child["writer_task_id"]})
    require(task and writer and task["state"] == "ACCEPTED"
            and (task["role_id"], task["task_kind"]) == ("reviewer", kind)
            and task["principal_id"] != writer["principal_id"] and task["grant_id"] != writer["grant_id"],
            "INDEPENDENT_REVIEW_REQUIRED", "Child edition requires independent accepted readers")
    evidence, candidate = CandidateDelivery(kernel, AgentGateway(kernel, {})).audited_candidate(
        repo, task, production_accounting=True)
    require(evidence["world_ref"] == wr and not blocking_review_issues(candidate["issues"]),
            "REVIEW_REQUIRED", "Child edition still has mandatory reading findings")
    require_review_queries(kernel, repo, task, evidence)
    frozen = kernel.artifact(repo, evidence["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]
    require(child["draft_ref"] in frozen["material_refs"],
            "INVALID_EVIDENCE", "Child reader did not read this exact candidate")
    if kind == "literary":
        require(child["literary_ref"] == evidence["output_ref"], "INVALID_EVIDENCE", "Child literary report is outside its actual reader output")
    else:
        record = kernel.artifact(repo, child["knowledge_ref"], wr["work_id"], kind="EPISTEMIC_READING")["payload"]
        delivery = kernel.artifact(repo, record["delivery_ref"], wr["work_id"], kind="TEXT_DELIVERY")["payload"]
        require(record["reader_task"] == task["task_id"] and record["generation"] == task["generation"]
                and record["attempt_id"] == evidence["attempt_id"] and record["candidate_ref"] == evidence["output_ref"]
                and record["world_ref"] == wr and record["draft_ref"] == child["draft_ref"]
                and record["text_ref"] == child["text_ref"] and record["status"] == "CLEAR"
                and delivery["task_id"] == task["task_id"] and delivery["generation"] == task["generation"]
                and delivery["context_bundle_id"] == task["context_bundle_id"] and delivery["world_ref"] == wr
                and delivery["draft_ref"] == child["draft_ref"] and delivery["text_ref"] == child["text_ref"]
                and delivery["read_mode"] == "FULL_TEXT",
                "EPISTEMIC_REVIEW_REQUIRED", "Child knowledge record does not match its actual native reader and delivered manuscript")
    return evidence, candidate


def _checked_child(kernel, repo, owner, wr, child):
    _reading(kernel, repo, wr, child, "epistemic_task_id", "epistemic")
    _reading(kernel, repo, wr, child, "literary_task_id", "literary")
    facts = kernel.artifact(repo, child["facts_ref"], wr["work_id"], kind="ROLLING_FACT_REPORT")["payload"]
    require(facts["status"] == "CLEAR" and facts["reconciliation_ref"] == child["reconciliation_ref"]
            and facts["alignment_ref"] == child["alignment_ref"] and facts["knowledge_ref"] == child["knowledge_ref"],
            "NARRATIVE_ALIGNMENT_REQUIRED", "Child factual report changed")
    from .rolling_facts import validate_native_fact_source
    extraction = kernel.artifact(repo, facts["extraction_ref"], wr["work_id"], kind="ASSERTION_EXTRACTION")["payload"]
    task = repo.one("tasks", {"task_id": extraction["task_id"]})
    writer = repo.one("tasks", {"task_id": child["writer_task_id"]})
    epistemic = repo.one("tasks", {"task_id": child["epistemic_task_id"]})
    require(task and task["state"] == "ACCEPTED" and task["generation"] == extraction["generation"]
            and (task["role_id"], task["task_kind"]) == ("evidence_maintainer", "assertion_extract")
            and task["principal_id"] not in {writer["principal_id"], epistemic["principal_id"]}
            and task["grant_id"] not in {writer["grant_id"], epistemic["grant_id"]},
            "INDEPENDENT_REVIEW_REQUIRED", "Child factual extraction needs its own accepted independent reading")
    draft = kernel.artifact(repo, child["draft_ref"], wr["work_id"], kind="NARRATIVE_DRAFT")["payload"]
    evidence, _ = validate_native_fact_source(kernel, repo, task, extraction, draft)
    frozen = kernel.artifact(repo, evidence["input_ref"], wr["work_id"], kind="AGENT_INPUT")["payload"]
    require(evidence["world_ref"] == wr and child["draft_ref"] in frozen["material_refs"]
            and extraction["draft_ref"] == child["draft_ref"] and extraction["text_ref"] == child["text_ref"],
            "INVALID_EVIDENCE", "Child fact report must derive from this actual native candidate's full-text extraction")
    checked = NarrativeService(kernel).validate_for_acceptance(repo, owner, child["draft_ref"], child["reconciliation_ref"], wr)
    require(checked["reconciliation"]["extraction_ref"] == facts["extraction_ref"] == child["extraction_ref"]
            and facts["text_delivery_ref"] == extraction["delivery_ref"],
            "INVALID_EVIDENCE", "Child factual audit, delivery and native extraction must be one exact executed chain")
    return facts


def _predecessor_entries(children, required):
    by_scene = {child["scene_id"]: child for child in children}
    require(set(required) <= by_scene.keys(), "SCENE_DEPENDENCY_PENDING", "This edition's checked predecessors are not ready")
    return [{"scene_id": sid, "draft_ref": by_scene[sid]["draft_ref"], "text_ref": by_scene[sid]["text_ref"],
        "epistemic": {"status": "KNOWLEDGE_CLEAR", "report_ref": by_scene[sid]["knowledge_ref"],
                      "task_id": by_scene[sid]["epistemic_task_id"]},
        "facts": {"status": "CLEAR", "report_ref": by_scene[sid]["facts_ref"],
                  "reconciliation_ref": by_scene[sid]["reconciliation_ref"], "alignment_ref": by_scene[sid]["alignment_ref"]},
        "literary": {"status": "CLEAR", "report_ref": by_scene[sid]["literary_ref"],
                     "task_id": by_scene[sid]["literary_task_id"]}} for sid in required]


def predecessor_packet(kernel, repo, owner, state, scene):
    card = kernel.artifact(repo, scene["scene_ref"], state["binding"]["world_ref"]["work_id"], kind="SCENE_PLAN")["payload"]
    required = card["required_predecessor_scene_ids"]
    if not required:
        return None
    ed = state["edition"]
    entries = _predecessor_entries(ed["candidates"], required)
    for child in ed["candidates"]:
        if child["scene_id"] in required:
            _checked_child(kernel, repo, owner, state["binding"]["world_ref"], child)
    payload = {"version": 1, "parent_session": state["session"], "round": ed["round"],
        "parent_checkpoint": ed["parent_checkpoint"], "parent_binding_sha256": fingerprint(state["binding"]),
        "world_ref": state["binding"]["world_ref"], "scene_id": scene["scene_id"], "predecessors": entries}
    payload["handoff_token"] = fingerprint(payload)
    return payload


def _prompt_values(messages):
    """Decode only message and material envelopes, never JSON-looking prose."""
    seeds = [messages]
    for message in messages:
        content = message.get("content")
        if not isinstance(content, str) or content[:1] not in {"{", "["}:
            continue
        try:
            decoded = json.loads(content)
            seeds.append(decoded)
            if isinstance(decoded, dict):
                for material in decoded.get("authorized_materials", []):
                    raw = material.get("content")
                    if isinstance(raw, str) and raw[:1] in {"{", "["}:
                        seeds.append(json.loads(raw))
        except (ValueError, RecursionError):
            require(False, "STALE_SCENE_HANDOFF", "Writer prompt material envelopes are malformed or too deep")
    stack, visited = [(value, 0) for value in seeds], 0
    while stack:
        value, depth = stack.pop()
        visited += 1
        require(depth <= 16 and visited <= 20000, "STALE_SCENE_HANDOFF", "Writer prompt envelope exceeds bounded handoff verification")
        yield value
        items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
        stack.extend((item, depth + 1) for item in items)


def _delivered_predecessors(kernel, repo, task, audit, packet_ref, packet):
    """Verify exact bytes offered before this audited call, including Harness input."""
    work = task["work_id"]
    spec = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})["spec"]
    frozen = kernel.artifact(repo, audit["input_ref"], work, kind="AGENT_INPUT")["payload"]
    prompt = kernel.artifact(repo, audit["prompt_ref"], work, kind="AGENT_PROMPT")["payload"]
    call = repo.one("call_attempts", {"attempt_id": audit["attempt_id"]})
    refs = [packet_ref, *[item["draft_ref"] for item in packet["predecessors"]]]
    values = list(_prompt_values(prompt["messages"]))
    require(all(ref in frozen["material_refs"] and ref in spec["input_artifacts"] for ref in refs)
            and any(value == packet for value in values)
            and prompt["input_ref"] == audit["input_ref"] and fingerprint(prompt) == call["request_hash"],
            "STALE_SCENE_HANDOFF", "The exact edition packet was not offered in this writer's frozen prompt")
    deliveries = []
    for item in packet["predecessors"]:
        content = kernel.objects.read(item["text_ref"]["sha256"]).decode("utf-8")
        require(any(value == content for value in values), "STALE_SCENE_HANDOFF", "The new predecessor's complete prose was not offered")
        rows = repo.conn.execute(text("""SELECT artifact_id::text,artifact_revision_id::text,sha256 FROM nf_artifacts
            WHERE work_id=:work AND kind='TEXT_DELIVERY' AND created_by=:author AND created_at<=:sent
              AND payload->>'task_id'=:task AND (payload->>'generation')::int=:generation
              AND payload->'draft_ref'->>'artifact_revision_id'=:draft"""),
            {"work": work, "author": task["principal_id"], "sent": call["created_at"], "task": task["task_id"],
             "generation": task["generation"], "draft": item["draft_ref"]["artifact_revision_id"]}).mappings().all()
        matched = []
        for row in rows:
            ref = dict(row)
            delivery = kernel.artifact(repo, ref, work, kind="TEXT_DELIVERY")["payload"]
            if (delivery["draft_ref"] == item["draft_ref"] and delivery["text_ref"] == item["text_ref"]
                    and delivery["context_bundle_id"] == task["context_bundle_id"] and delivery["world_ref"] == spec["world_ref"]
                    and delivery["read_mode"] == "FULL_TEXT" and delivery["span"]["start"] == 0
                    and delivery["span"]["end"] == len(content) and delivery["span"]["quote_sha256"] == item["text_ref"]["sha256"]):
                matched.append(ref)
        require(matched, "STALE_SCENE_HANDOFF", "The writer has no pre-send full-text delivery of its new predecessor")
        deliveries.append(min(matched, key=lambda ref: ref["artifact_revision_id"]))
    return deliveries


def record_handoff(kernel, repo, owner, task, audit, draft_ref):
    job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})["config"]
    packet_ref = job.get("returned_predecessor_packet_ref")
    if not packet_ref:
        return None
    packet = kernel.artifact(repo, packet_ref, task["work_id"], kind=PACKET_KIND)["payload"]
    deliveries = _delivered_predecessors(kernel, repo, task, audit, packet_ref, packet)
    return _artifact(kernel, repo, task["work_id"], owner, PROOF_KIND, {
        "packet_ref": packet_ref, "task_id": task["task_id"], "generation": task["generation"],
        "context_bundle_id": task["context_bundle_id"], "attempt_id": audit["attempt_id"],
        "input_ref": audit["input_ref"], "prompt_ref": audit["prompt_ref"], "draft_ref": draft_ref,
        "delivery_refs": deliveries})


def _validate_handoff(kernel, repo, manifest, child, children):
    work = manifest["world_ref"]["work_id"]
    draft = kernel.artifact(repo, child["draft_ref"], work, kind="NARRATIVE_DRAFT")["payload"]
    card = kernel.artifact(repo, draft["scene_ref"], work, kind="SCENE_PLAN")["payload"]
    required = card["required_predecessor_scene_ids"]
    if not required:
        require(not child.get("handoff_proof_ref"), "STALE_SCENE_HANDOFF", "An independent scene cannot adopt an unrelated handoff")
        return
    require(child.get("handoff_proof_ref"), "STALE_SCENE_HANDOFF", "Dependent child edition requires its exact offered predecessor proof")
    proof = kernel.artifact(repo, child["handoff_proof_ref"], work, kind=PROOF_KIND)["payload"]
    packet = kernel.artifact(repo, proof["packet_ref"], work, kind=PACKET_KIND)["payload"]
    task = repo.one("tasks", {"task_id": child["writer_task_id"]})
    job = repo.one("supervisor_jobs", {"task_id": task["task_id"]})["config"]
    audit, _ = CandidateDelivery(kernel, AgentGateway(kernel, {})).audited_candidate(repo, task, production_accounting=True)
    core = {key: value for key, value in packet.items() if key != "handoff_token"}
    require(packet["handoff_token"] == fingerprint(core)
            and packet["parent_session"] == manifest["parent_session"] and packet["round"] == manifest["round"]
            and packet["parent_checkpoint"] == manifest["parent_checkpoint"]
            and packet["parent_binding_sha256"] == manifest["parent_binding_sha256"]
            and packet["world_ref"] == manifest["world_ref"] and packet["scene_id"] == child["scene_id"]
            and packet["predecessors"] == _predecessor_entries(children, required)
            and job.get("returned_predecessor_packet_ref") == proof["packet_ref"]
            and job.get("returned_edition") == {"parent_session": manifest["parent_session"], "phase": "write",
                                                "scene": children.index(child)}
            and proof["draft_ref"] == child["draft_ref"] and proof["task_id"] == task["task_id"]
            and proof["generation"] == child["writer_generation"] == task["generation"]
            and proof["context_bundle_id"] == task["context_bundle_id"]
            and all(proof[key] == audit[key] for key in ("attempt_id", "input_ref", "prompt_ref")),
            "STALE_SCENE_HANDOFF", "Edition handoff does not match its exact checked candidates and immutable writer")
    require(proof["delivery_refs"] == _delivered_predecessors(kernel, repo, task, audit, proof["packet_ref"], packet),
            "STALE_SCENE_HANDOFF", "Edition handoff material deliveries changed")


def _checkpoint_child(kernel, repo, owner, checkpoint, child, *, live_ref=None):
    from .release import _load_acceptance
    matches = [scene for chapter in checkpoint["chapters"] for scene in chapter["scenes"] if scene["scene_id"] == child["scene_id"]]
    require(len(matches) == 1 and matches[0]["draft_ref"] == child["draft_ref"] and matches[0]["text_ref"] == child["text_ref"],
            "STALE_SCENE_HANDOFF", "Selected checkpoint must contain this unique exact edition child")
    scene = matches[0]
    accepted, _ = _load_acceptance(repo, kernel, owner, scene["acceptance_ref"], checkpoint["world_ref"])
    facts = kernel.artifact(repo, scene["facts_report_ref"], checkpoint["world_ref"]["work_id"], kind="ROLLING_FACT_REPORT")["payload"]
    require((live_ref is None or scene["acceptance_ref"] == live_ref)
            and all(accepted[key] == scene[key] for key in ("draft_ref", "scene_ref", "text_ref"))
            and facts["status"] == "CLEAR" and facts["acceptance_ref"] == scene["acceptance_ref"]
            and facts["reconciliation_ref"] == accepted["reconciliation_ref"]
            and facts["alignment_ref"] == accepted["alignment_ref"] and facts.get("knowledge_ref") == scene.get("knowledge_ref"),
            "STALE_SCENE_HANDOFF", "Checkpoint acceptance and factual source do not bind the current exact child")


def guard_child(kernel, repo, principal, world_ref, draft_ref, edition_ref):
    """Narrow internal exception to the parent chapter guard, never to reconciliation."""
    from .frozen_editing import _state
    from .returned_edition import _guard
    from .rolling_production import _load

    wr = world_ref
    state = _state(kernel, repo, wr["work_id"], wr["branch_id"])
    cursor = _load(kernel, repo, wr["work_id"], wr["branch_id"])
    require(state and cursor and _current_owner(kernel, repo, cursor) == principal
            and state["status"] == "EDITION_REVISING" and state["edition"]["phase"] == "accept"
            and state.get("edition_ref") == edition_ref,
            "VIEW_DENIED", "Only the active edition's original controller may accept its children")
    manifest = kernel.artifact(repo, edition_ref, wr["work_id"], kind="P6_RETURNED_EDITION_CANDIDATE")["payload"]
    require(manifest["world_ref"] == wr == state["binding"]["world_ref"]
            and manifest["run"] == cursor["run_id"] == state["run"]
            and manifest["parent_session"] == state["session"]
            and manifest["parent_checkpoint"] == cursor["last_checkpoint"]
            and manifest["parent_binding_sha256"] == fingerprint(state["binding"])
            and manifest["controller"] == {"grant_id": principal.grant_id, "principal_id": principal.principal_id}
            and not _selected(repo, wr["work_id"], "RELEASE:" + state["volume"]),
            "STALE_GENERATION", "Child edition no longer matches its unpublished frozen parent")
    scenes = _guard(kernel, repo, state, cursor)
    children = manifest["candidates"]
    require([item["scene_id"] for item in children] == [scene["scene_id"] for scene in scenes]
            and any(item["changed"] for item in children)
            and any(item["draft_ref"] == draft_ref for item in children),
            "INVALID_EVIDENCE", "The accepted candidate is outside the complete changed edition")
    for scene, child in zip(scenes, children, strict=True):
        draft = kernel.artifact(repo, child["draft_ref"], wr["work_id"], kind="NARRATIVE_DRAFT")["payload"]
        require(child["parent_ref"] == scene["draft_ref"] == draft["prior_draft_ref"]
                and draft["scene_ref"] == scene["scene_ref"] and draft["text_ref"] == child["text_ref"],
                "INVALID_EVIDENCE", "Child lineage changed after independent checking")
        _checked_child(kernel, repo, principal, wr, child)
        _validate_handoff(kernel, repo, manifest, child, children)
    return True


def accepted_edition_handoff(kernel, repo, entry, prior, selected):
    """A changed predecessor is usable only after this complete edition committed."""
    draft_ref = entry["acceptance"]["draft_ref"]
    wr = entry["acceptance"]["world_ref"]
    work = wr["work_id"]
    edition_ref = _selected(repo, work, "RETURNED_EDITION_FOR_DRAFT:" + draft_ref["artifact_revision_id"])
    require(edition_ref, "STALE_SCENE_HANDOFF", "A changed predecessor requires a formally accepted child edition")
    manifest = kernel.artifact(repo, edition_ref, work, kind="P6_RETURNED_EDITION_CANDIDATE")["payload"]
    owner = Principal(**manifest["controller"])
    accepted_ref = _selected(repo, work, "RETURNED_EDITION_ACCEPTANCE:" + edition_ref["artifact_revision_id"])
    require(accepted_ref, "STALE_SCENE_HANDOFF", "The child edition has no atomic acceptance")
    atomic = kernel.artifact(repo, accepted_ref, work, kind="RETURNED_EDITION_ACCEPTANCE")["payload"]
    request = {"world_ref": manifest["world_ref"], "edition_ref": edition_ref, "parent_checkpoint": manifest["parent_checkpoint"]}
    receipt = repo.one("receipts", {"work_id": work, "branch_id": wr["branch_id"], "command_type": "RETURNED_EDITION_ACCEPT",
                                   "idempotency_key": edition_ref["artifact_revision_id"], "payload": accepted_ref})
    checkpoint = kernel.artifact(repo, atomic["checkpoint_ref"], work, kind="ROLLING_CHECKPOINT")["payload"]
    children = manifest["candidates"]
    committed = [scene for chapter in checkpoint["chapters"] for scene in chapter["scenes"]]
    require(receipt and receipt["principal_id"] == owner.principal_id and receipt["request_hash"] == fingerprint(request)
            and atomic["edition_ref"] == edition_ref and atomic["parent_checkpoint"] == manifest["parent_checkpoint"]
            and checkpoint["edition_ref"] == edition_ref and checkpoint["parent_checkpoint"] == manifest["parent_checkpoint"]
            and checkpoint["world_ref"] == manifest["world_ref"] and checkpoint["run_id"] == manifest["run"]
            and checkpoint["volume_id"] == manifest["volume"]
            and [scene["scene_id"] for scene in committed] == [child["scene_id"] for child in children]
            and atomic["acceptance_refs"] == [scene["acceptance_ref"] for scene in committed],
            "STALE_SCENE_HANDOFF", "Edition receipt and complete atomic checkpoint do not match")
    for child, scene in zip(children, committed, strict=True):
        acceptance = kernel.artifact(repo, scene["acceptance_ref"], work, kind="NARRATIVE_ACCEPTANCE")["payload"]
        accepted_request = {"draft_ref": child["draft_ref"], "reconciliation_ref": child["reconciliation_ref"],
                            "world_ref": manifest["world_ref"], "alignment_ref": child["alignment_ref"], "edition_ref": edition_ref}
        formal = repo.one("receipts", {"work_id": work, "branch_id": wr["branch_id"], "command_type": "NARRATIVE_ACCEPT",
                                      "payload": scene["acceptance_ref"], "request_hash": fingerprint(accepted_request)})
        live_ref = _selected(repo, work, "NARRATIVE_SCENE:" + child["scene_id"])
        require(formal and formal["principal_id"] == owner.principal_id and live_ref
                and acceptance["draft_ref"] == scene["draft_ref"] == child["draft_ref"]
                and acceptance["text_ref"] == scene["text_ref"] == child["text_ref"],
                "STALE_SCENE_HANDOFF", "Every edition child needs its actual edition-scoped narrative acceptance")
        live = kernel.artifact(repo, live_ref, work, kind="NARRATIVE_ACCEPTANCE")["payload"]
        require(all(live[key] == acceptance[key] for key in ("draft_ref", "scene_ref", "text_ref")),
                "STALE_SCENE_HANDOFF", "An edition child has been replaced after its atomic acceptance")
        frozen_ref = _selected(repo, work, "P6_FROZEN_SCENE:" + child["scene_id"])
        require(frozen_ref, "STALE_SCENE_HANDOFF", "The edition child is no longer in its current frozen checkpoint")
        current_ref, seen = frozen_ref, set()
        current = kernel.artifact(repo, current_ref, work, kind="ROLLING_CHECKPOINT")["payload"]
        _checkpoint_child(kernel, repo, owner, current, child, live_ref=live_ref)
        while current_ref != atomic["checkpoint_ref"]:
            require(current_ref["sha256"] not in seen and len(seen) < 20000,
                    "STALE_SCENE_HANDOFF", "The atomic edition checkpoint is outside the selected frozen lineage")
            seen.add(current_ref["sha256"])
            current = kernel.artifact(repo, current_ref, work, kind="ROLLING_CHECKPOINT")["payload"]
            require(current.get("revalidation_kind") == "PURE_FORMAL_COVERAGE" and current.get("source_checkpoint_ref")
                    and current.get("edition_ref") == edition_ref,
                    "STALE_SCENE_HANDOFF", "Only a verified unchanged coverage overlay can retain an edition handoff")
            from .world_coverage_recovery import prove_pure_coverage
            prove_pure_coverage(kernel, repo, manifest["world_ref"], current["world_ref"])
            current_ref = current["source_checkpoint_ref"]
            current = kernel.artifact(repo, current_ref, work, kind="ROLLING_CHECKPOINT")["payload"]
            _checkpoint_child(kernel, repo, owner, current, child)
        _checked_child(kernel, repo, owner, manifest["world_ref"], child)
        _validate_handoff(kernel, repo, manifest, child, children)
    child = next((item for item in children if item["draft_ref"] == draft_ref), None)
    predecessor = next((item for item in children if item["draft_ref"] == selected["draft_ref"]), None)
    require(child and predecessor and predecessor["parent_ref"] == prior["draft_ref"]
            and selected["scene_ref"] == prior["scene_ref"] and selected["text_ref"] == predecessor["text_ref"],
            "STALE_SCENE_HANDOFF", "The selected predecessor is outside this exact bound edition")
    return True


def commit_edition(kernel, repo, owner, state, cursor):
    """Called under one work lock; scene acceptances and checkpoint advance commit together."""
    from .rolling_production import _save

    wr = state["binding"]["world_ref"]
    edition_ref = state["edition_ref"]
    manifest = kernel.artifact(repo, edition_ref, wr["work_id"], kind="P6_RETURNED_EDITION_CANDIDATE")["payload"]
    children = {item["scene_id"]: item for item in manifest["candidates"]}
    guard_child(kernel, repo, owner, wr, manifest["candidates"][0]["draft_ref"], edition_ref)
    parent = cursor["last_checkpoint"]
    checkpoints = []
    previous = parent
    while previous:
        payload = kernel.artifact(repo, previous, wr["work_id"], kind="ROLLING_CHECKPOINT")["payload"]
        if payload["volume_id"] != state["volume"]:
            break
        checkpoints.insert(0, payload)
        previous = payload["previous"]
    chapters = [deepcopy(chapter) for checkpoint in checkpoints for chapter in checkpoint["chapters"]]
    accepted = []
    for chapter in chapters:
        chapter["parent_reading_ref"] = chapter["reading_ref"]
        chapter["reading_ref"] = _artifact(kernel, repo, wr["work_id"], owner, "RETURNED_EDITION_CHAPTER_READING", {
            "world_ref": wr, "edition_ref": edition_ref, "chapter_id": chapter["chapter_id"],
            "draft_refs": [children[scene["scene_id"]]["draft_ref"] for scene in chapter["scenes"]],
            "scene_reading_refs": [children[scene["scene_id"]]["literary_ref"] for scene in chapter["scenes"]],
            "scope": "EXACT_CHILD_SCENES", "literary_quality": "PENDING_FRESH_VOLUME_EDIT"})
        for scene in chapter["scenes"]:
            child = children[scene["scene_id"]]
            acceptance = NarrativeService(kernel).accept_narrative(owner, child["draft_ref"], child["reconciliation_ref"], wr,
                "returned-child-accept:" + edition_ref["artifact_revision_id"] + ":" + scene["scene_id"],
                alignment_ref=child["alignment_ref"], edition_ref=edition_ref)
            accepted.append(acceptance)
            facts = kernel.artifact(repo, child["facts_ref"], wr["work_id"], kind="ROLLING_FACT_REPORT")["payload"]
            facts_ref = _artifact(kernel, repo, wr["work_id"], owner, "ROLLING_FACT_REPORT", {
                **facts, "acceptance_ref": acceptance, "source_fact_report_ref": child["facts_ref"], "edition_ref": edition_ref})
            scene.update(draft_ref=child["draft_ref"], text_ref=child["text_ref"], acceptance_ref=acceptance,
                         knowledge_ref=child["knowledge_ref"], facts_report_ref=facts_ref)
            scene.pop("coverage_revalidation_ref", None)
            scene.pop("source_acceptance_ref", None)
    checkpoint = _artifact(kernel, repo, wr["work_id"], owner, "ROLLING_CHECKPOINT", {
        "world_ref": wr, "run_id": cursor["run_id"], "series_id": cursor["series_id"], "volume_id": state["volume"],
        "index": cursor["index"], "previous": previous, "chapters": chapters, "parent_checkpoint": parent,
        "edition_ref": edition_ref})
    for scene_id in children:
        PlanningService(kernel)._select(repo, wr, "P6_FROZEN_SCENE:" + scene_id, checkpoint)
    cursor.update(last_checkpoint=checkpoint)
    _save(kernel, repo, owner, cursor)
    receipt = {"parent_checkpoint": parent, "checkpoint_ref": checkpoint, "edition_ref": edition_ref,
               "acceptance_refs": accepted, "world_effects_applied": 0}
    reference = _artifact(kernel, repo, wr["work_id"], owner, "RETURNED_EDITION_ACCEPTANCE", receipt)
    PlanningService(kernel)._select(repo, wr, "RETURNED_EDITION_ACCEPTANCE:" + edition_ref["artifact_revision_id"], reference)
    for child in children.values():
        PlanningService(kernel)._select(repo, wr, "RETURNED_EDITION_FOR_DRAFT:" + child["draft_ref"]["artifact_revision_id"], edition_ref)
    kernel._receipt(repo, owner, wr, "RETURNED_EDITION_ACCEPT", edition_ref["artifact_revision_id"],
                    {"world_ref": wr, "edition_ref": edition_ref, "parent_checkpoint": parent}, reference)
    state.update(status="EDITION_ACCEPTED", active=None, edition_acceptance_ref=reference)
    state.pop("reason", None)
    return reference
