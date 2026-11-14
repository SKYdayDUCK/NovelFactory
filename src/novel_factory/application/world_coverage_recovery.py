"""Close a proven empty story prefix, then authorize a separate reading cut.

Only the already authorized controller accepts a closed scheduling command.
No draft, source task, production board, prior case or repair allowance changes.
Semantic/actor blockers remain the responsibility of the world dispatcher.
"""
from copy import deepcopy
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import text

from ..contracts.registry import validate_contract
from ..domain.canonical import fingerprint
from ..domain.scheduling import coverage_blockers, state_from_revision
from ..domain.temporal import point_key
from .authorization import Principal
from .kernel import require
from .simulation import _expanded_scopes

COMMAND = "WORLD_COVERAGE_RECOVERY"
TRANSITION_KIND = "WORLD_COVERAGE_READING_TRANSITION"


def _artifact(kernel, repo, owner, work, payload, kind):
    ref = kernel._blob(payload)
    kernel._register_artifact(repo, ref, work, payload, kind, owner.principal_id)
    return ref


def _same_canon(source, target):
    """Allow only historical cross-entity lifecycle-history ordering differences.

    Genesis insertion order and JSONB key order can produce different interleaved
    histories. Each entity's complete ordered history remains authoritative.
    Historical snapshots and their raw hashes are never rewritten.
    """
    left, right = deepcopy(source["snapshot"]), deepcopy(target["snapshot"])
    if left.keys() != right.keys():
        return False
    if "lifecycle_history" in left:
        def grouped(items):
            groups = {}
            for item in items:
                groups.setdefault(item["entity_id"], []).append(item)
            return groups
        left["lifecycle_history"] = grouped(left["lifecycle_history"])
        right["lifecycle_history"] = grouped(right["lifecycle_history"])
    return (left == right and all(source[key] == target[key] for key in ("selection", "interpretation_ref"))
            and state_from_revision(source) == state_from_revision(target))


def _representation(source, target):
    return {"source_snapshot_sha256": fingerprint(source["snapshot"]),
            "reading_snapshot_sha256": fingerprint(target["snapshot"]),
            "snapshot_representation_changed": source["snapshot"] != target["snapshot"],
            "canon_equivalence": "LIFECYCLE_INTER_ENTITY_ORDER_ONLY"}


def prove_pure_coverage(kernel, repo, source_world_ref, target_world_ref):
    """Prove a same-branch path of actual empty SCHEDULE_ADVANCE accepts.

    This is an internal read-only verifier. Its caller must authorize its use;
    the result grants no model role permission and rewrites no historical cut.
    """
    for wr in (source_world_ref, target_world_ref):
        validate_contract("WorldRef", wr)
    require(all(source_world_ref[key] == target_world_ref[key] for key in ("work_id", "branch_id"))
            and repo.ancestor(source_world_ref, target_world_ref), "BRANCH_CHANGED",
            "Coverage compatibility requires one fixed forward branch lineage")
    source = kernel.revision(repo, source_world_ref)
    current = kernel.revision(repo, target_world_ref)
    target = current
    path = []
    while current["world_revision_id"] != source_world_ref["world_revision_id"]:
        require(len(path) < 20000 and current["parent_revision_id"], "INVALID_EVIDENCE",
                "Coverage compatibility path is absent or exceeds the bounded revision history")
        parent_wr = {**target_world_ref, "world_revision_id": current["parent_revision_id"]}
        parent = kernel.revision(repo, parent_wr)
        proposal_ref = current["provenance"].get("proposal_ref")
        require(proposal_ref, "NARRATIVE_ALIGNMENT_REQUIRED", "World revision is not a formal coverage acceptance")
        proposal = kernel.artifact(repo, proposal_ref, source_world_ref["work_id"], kind="WORLD_PROPOSAL")["payload"]
        row = repo.one("proposals", {"proposal_id": proposal["proposal_id"]})
        receipts = repo.conn.execute(text("""SELECT payload,principal_id,idempotency_key FROM nf_receipts
            WHERE work_id=:work AND branch_id=:branch AND command_type='ACCEPT'
              AND proposal_id=:proposal AND payload->'world_ref'->>'world_revision_id'=:revision"""),
            {"work": target_world_ref["work_id"], "branch": target_world_ref["branch_id"],
             "proposal": proposal["proposal_id"], "revision": current["world_revision_id"]}).mappings().all()
        require(row and row["artifact_ref"] == proposal_ref and row["template_id"] == "SCHEDULE_ADVANCE"
                and len(receipts) == 1 and receipts[0]["payload"]["world_ref"] == {**target_world_ref, "world_revision_id": current["world_revision_id"]}
                and receipts[0]["payload"].get("proposal_ref") == proposal_ref
                and not receipts[0]["payload"]["accepted_event_refs"]
                and not proposal["event_candidates"] and not proposal["event_operations"]
                and not proposal.get("record_operations") and not proposal.get("trigger_evaluations")
                and proposal["adjudication_refs"] and _same_canon(source, current) and _same_canon(parent, current),
                "NARRATIVE_ALIGNMENT_REQUIRED", "Only formally accepted empty coverage can reuse a prior reading cut")
        base_wr = proposal["input_world_ref"]
        require(repo.ancestor(base_wr, parent_wr) and _same_canon(kernel.revision(repo, base_wr), parent),
                "NARRATIVE_ALIGNMENT_REQUIRED", "Coverage proposal was not validated against equivalent canonical inputs")
        for ref in proposal["adjudication_refs"]:
            closure = kernel.artifact(repo, ref, target_world_ref["work_id"], kind="CLOSURE")["payload"]
            require(closure["template_id"] == "SCHEDULE_ADVANCE" and closure["proposal_id"] == proposal["proposal_id"]
                    and closure["base_world_ref"] == base_wr and closure["args_sha256"] == fingerprint(row["template_args"]),
                    "INVALID_EVIDENCE", "Coverage acceptance has no exact closed scheduling proof")
        path.append({"world_revision_id": current["world_revision_id"], "proposal_ref": proposal_ref,
                     "acceptance_key": receipts[0]["idempotency_key"], "closure_refs": proposal["adjudication_refs"]})
        current = parent
    require(_same_canon(source, target), "NARRATIVE_ALIGNMENT_REQUIRED", "Canonical state changed across reading cuts")
    proof = {"record_version": 1, "kind": "PURE_FORMAL_COVERAGE", "source_world_ref": source_world_ref,
             "adopted_world_ref": target_world_ref, "step_count": len(path), "accepted_path_sha256": fingerprint(path),
             "registry_sha256": fingerprint(state_from_revision(source)), **_representation(source, target)}
    return {**proof, "proof_sha256": fingerprint(proof)}


def prove_review_compatibility(kernel, repo, task, evidence, target_world_ref):
    """Re-evaluate old native STATE selectors without changing the model receipt."""
    from .queries import WorldQueryService
    from .review_mcp import require_review_queries

    source_wr = evidence["world_ref"]
    coverage = prove_pure_coverage(kernel, repo, source_wr, target_world_ref)
    receipts = require_review_queries(kernel, repo, task, evidence)
    context = repo.one("contexts", {"context_bundle_id": task["context_bundle_id"]})
    require(context and context["read_cut"]["world_ref"] == source_wr
            and context["read_cut"]["knowledge_cut"]["plane"] == "WORLD", "REVIEW_QUERY_REQUIRED",
            "Reading-cut compatibility requires the original objective native review")
    source, target = kernel.revision(repo, source_wr), kernel.revision(repo, target_world_ref)
    service = WorldQueryService(kernel)
    compared = []

    def meaning(value):
        if isinstance(value, dict):
            return {key: meaning(item) for key, item in value.items() if key not in {"world_ref", "dependency_handle"}}
        if isinstance(value, list):
            return [meaning(item) for item in value]
        return value

    for receipt in receipts:
        if receipt["tool"] != "nf_query_batch":
            continue
        require(receipt["outcome"] == "OK" and not receipt["result"].get("isError"), "REVIEW_QUERY_REQUIRED",
                "An unsuccessful system query cannot certify a later reading cut")
        rows = {row["item_id"]: row for row in receipt["result"]["structuredContent"].get("items", [])}
        lookups = receipt["arguments"].get("lookups", [])
        require(lookups and len(rows) == len(lookups), "REVIEW_QUERY_REQUIRED", "Every original selector needs its actual result")
        for index, lookup in enumerate(lookups):
            cut = {"plane": lookup.get("plane", "WORLD")}
            require(cut["plane"] in {"WORLD", "BELIEF"}, "REVIEW_QUERY_REQUIRED", "Only authoritative STATE selectors can be reused")
            if cut["plane"] == "BELIEF":
                cut.update(actor_id=lookup["actor_id"], known_at=lookup["known_at"])
            at = lookup.get("at", context["payload"]["story_time"])
            request = {"knowledge_cut": cut, "subject_time": {"kind": "POINT", "at": at},
                       "read_contract": {"process_policy": "ALLOW_PARTIAL"},
                       "query": {"subject_ids": [lookup["subject_id"]], "predicates": [lookup["predicate"]]}}
            def evaluate(revision, wr, request=request, lookup=lookup):
                return service._state({"revision": revision, "read_cut": {"world_ref": wr}}, request,
                                      str(uuid5(UUID(wr["world_revision_id"]), "coverage-selector:" + fingerprint(lookup))))
            old, new = evaluate(source, source_wr), evaluate(target, target_world_ref)
            row = rows.get(str(index), {})
            require(row.get("status") == "SUCCESS" and row.get("payload", {}).get("kind") == "STATE"
                    and meaning(row["payload"]) == meaning(old), "REVIEW_QUERY_REQUIRED",
                    "The stored model query does not match its original authoritative selector")
            require(all(item["answer"]["evaluation_status"] == "EVALUATED"
                        and item["answer"]["value_status"] in {"VALUE", "NONE", "NOT_APPLICABLE"}
                        and item["answer"]["knowledge_status"] == "KNOWN" for item in old["state_items"])
                    and meaning(old) == meaning(new), "REVIEW_QUERY_REQUIRED",
                    "Pending, unknown or changed query answers require a fresh independent review")
            compared.append({"receipt_ref": receipt["receipt_ref"], "selector": lookup,
                             "source_answer_sha256": fingerprint(old), "adopted_answer_sha256": fingerprint(new),
                             "semantic_answer_sha256": fingerprint(meaning(old))})
    require(compared, "REVIEW_QUERY_REQUIRED", "Reading compatibility needs actual stable system-query evidence")
    proof = {"record_version": 1, "kind": "REVIEW_CUT_COMPATIBILITY", "source_world_ref": source_wr,
             "adopted_world_ref": target_world_ref, "reader_task_id": task["task_id"], "generation": task["generation"],
             "attempt_id": evidence["attempt_id"], "source_candidate_ref": evidence["output_ref"],
             "pure_coverage_proof": coverage, "query_comparisons": compared,
             "semantics": "ORIGINAL_MODEL_CUT_PRESERVED_SERVER_REVALIDATED_SELECTORS", "provider_calls_sent": 0}
    return {**proof, "proof_sha256": fingerprint(proof)}


def _scene_subjects(kernel, repo, board, scene, revision):
    contract = scene["scene_contract"]
    subjects = {contract["pov_actor_id"]}
    subjects.update(contract.get("scope_ids", []))
    for check in [*scene["entry_checks"], *scene["exit_checks"]]:
        subjects.update(check[key] for key in ("subject_id", "actor_id") if check.get(key))
    plan = kernel.artifact(repo, scene["plan_ref"], board["world_ref"]["work_id"], kind="PLAN_REVISION")["payload"]
    subjects.update(subject for node in plan["nodes"] if node["node_id"] in scene["plan_node_ids"]
                    for subject in node["subject_ids"])
    seed = set(subjects)
    derived = []
    for process_id, entry in state_from_revision(revision)["processes"].items():
        process = entry["definition"]
        if (seed.intersection(process["participant_ids"])
                and point_key(process["start_at"]) <= point_key(contract["story_interval"]["end"])
                and (process["due_at"] is None or point_key(process["due_at"]) <= point_key(contract["story_interval"]["end"]))):
            related = set(process["participant_ids"]) | set(process["scope_ids"])
            subjects.update(related)
            derived.append({"process_id": process_id, "subject_ids": sorted(related)})
    authorized = subjects & set(board["subject_ids"]) & revision["snapshot"]["entities"].keys()
    return authorized, {"scene_subject_ids": sorted(seed), "registered_couplings": derived,
                        "authorized_subject_ids": sorted(authorized)}


def _identity(board, target):
    writer = board["nodes"][target]
    return {"board_world_ref": board["world_ref"], "volume_id": board["volume_id"],
            "series_id": board["series_id"], "plan_ref": board["plan_ref"],
            "authorization": board["authorization"], "target": target,
            "source_draft_ref": writer["output_ref"], "source_scene_ref": writer["scene_ref"],
            "writer_task_id": writer["task_id"], "writer_generation": writer.get("generation"),
            "revision_count": writer.get("revision_count", 0),
            "repair_allowance": writer.get("repair_allowance", {})}


def _owner(kernel, repo, owner, board):
    wr, fence = board["world_ref"], board["authorization"]
    grant = kernel.authorize(repo, owner, wr["work_id"], "ADMIN", wr["branch_id"])
    require(owner.grant_id == fence["grant_id"] and owner.principal_id == fence["principal_id"]
            and grant["epoch"] == fence["epoch"]
            and repo.one("works", {"work_id": wr["work_id"]})["policy_version"] == fence["policy_version"]
            and repo.now() < datetime.fromisoformat(fence["deadline"]),
            "VIEW_DENIED", "Coverage recovery requires the unchanged production controller")


def _pending_queries(kernel, repo, board, target, parent_case_hash, receipt_refs):
    """Use server-recorded per-question proof, never model-reported selectors."""
    from .review_mcp import RECEIPT_KIND, _audit_pair

    nodes = [node for node in board["nodes"].values()
             if node.get("stage") == "internal_verification"
             and node.get("internal_case", {}).get("case_hash") == parent_case_hash]
    require(len(nodes) == 1 and nodes[0].get("output_ref"), "INVALID_EVIDENCE",
            "Coverage requires one stored parent internal resolution")
    node = nodes[0]
    case = node["internal_case"]
    record = kernel.artifact(repo, node["output_ref"], board["world_ref"]["work_id"],
                             kind="INTERNAL_REVIEW_RESOLUTION")["payload"]
    require(case["target"] == target and case["source_draft_ref"] == board["nodes"][target]["output_ref"]
            and record["request"] == case and record["task_id"] == node["task_id"]
            and record["generation"] == node["generation"]
            and record["outcome"] == "NEEDS_WORLD_COVERAGE", "INVALID_EVIDENCE",
            "Only the current draft's verified pending-world result can request closure")
    pending = [item for item in record.get("query_evidence", [])
               if item.get("answer_status") == "PENDING_EVALUATION"
               and item.get("evaluation_status") == "PENDING" and item.get("status") == "PENDING"]
    require(pending and len(pending) <= 128 and receipt_refs
            and {fingerprint(item["query_receipt_ref"]) for item in pending}
            == {fingerprint(ref) for ref in receipt_refs}, "INVALID_EVIDENCE",
            "Exact pending query receipt references are required")
    for item in pending:
        receipt = kernel.artifact(repo, item["query_receipt_ref"], board["world_ref"]["work_id"],
                                  kind=RECEIPT_KIND)["payload"]
        require(receipt["tool"] == "nf_query_batch" and receipt["outcome"] == "OK"
                and receipt["world_ref"] == case["world_ref"] == item["world_ref"]
                and receipt["task_id"] == record["task_id"] and receipt["generation"] == record["generation"]
                and receipt["arguments_sha256"] == fingerprint(receipt["arguments"])
                and receipt["result_sha256"] == fingerprint(receipt["result"]),
                "INVALID_EVIDENCE", "Pending query receipt belongs to another cut or reader")
        _audit_pair(repo, receipt)
        from .production_verification import _matches_lookup, _query_observations
        matches = [row for row in _query_observations(receipt, case)
                   if _matches_lookup(item["selector"], row["selector"], case)]
        require(item["selector"]["plane"] == "WORLD" and any(row["status"] == "PENDING"
                and row["world_ref"] == case["world_ref"] for row in matches), "INVALID_EVIDENCE",
                "Pending selector has no matching actual pending objective query item")
    return case, record, pending, node["output_ref"]


def plan_coverage(kernel, repo, owner, board, target, *, parent_case_hash, query_receipt_refs):
    """Freeze a bounded request; this method sends no model or world command."""
    _owner(kernel, repo, owner, board)
    identity = _identity(board, target)
    case, _record, pending, parent_ref = _pending_queries(
        kernel, repo, board, target, parent_case_hash, query_receipt_refs)
    parent_wr = case["world_ref"]
    wr = kernel.head(repo, parent_wr["work_id"], parent_wr["branch_id"])
    require((wr["work_id"], wr["branch_id"]) == (board["world_ref"]["work_id"], board["world_ref"]["branch_id"])
            and repo.ancestor(board["world_ref"], parent_wr) and repo.ancestor(parent_wr, wr),
            "BRANCH_CHANGED", "Coverage cut must descend from the exact parent query and production branch")
    draft = kernel.artifact(repo, identity["source_draft_ref"], wr["work_id"], kind="NARRATIVE_DRAFT")["payload"]
    scene = kernel.artifact(repo, identity["source_scene_ref"], wr["work_id"], kind="SCENE_PLAN")["payload"]
    require(draft["scene_ref"] == identity["source_scene_ref"] and draft["task_id"] == identity["writer_task_id"]
            and (identity["writer_generation"] is None or draft["generation"] == identity["writer_generation"])
            and repo.ancestor(draft["input_world_ref"], wr), "STALE_GENERATION", "Coverage source draft changed")
    interval = scene["scene_contract"]["story_interval"]
    target_frontier = interval["end"]
    subjects = set()
    for item in pending:
        selector = item["selector"]
        require(selector["plane"] == "WORLD" and selector["subject_id"] in board["subject_ids"]
                and selector["predicate"] in board["predicates"]
                and point_key(interval["start"]) <= point_key(selector["at"]) < point_key(target_frontier),
                "VIEW_DENIED", "Coverage requests cannot extend beyond this exact scene interval")
        subjects.add(selector["subject_id"])
    revision = kernel.revision(repo, wr)
    parent_revision = kernel.revision(repo, parent_wr)
    require(subjects <= revision["snapshot"]["entities"].keys(), "INVALID_EVIDENCE", "Coverage cannot register missing identities")
    scene_subjects, derivation = _scene_subjects(kernel, repo, board, scene, revision)
    subjects.update(scene_subjects)
    scopes = sorted({revision["snapshot"]["entities"][subject].get("scope_id", subject) for subject in subjects})
    state = state_from_revision(revision)
    expanded = _expanded_scopes(revision["snapshot"], scopes, target_frontier)
    blockers = coverage_blockers(state, expanded, target_frontier)
    advancing = [scope for scope in scopes if point_key(revision["frontiers"].get(scope, {"tick": 0, "phase": 0})) < point_key(target_frontier)]
    request = {"record_version": 1, "source_identity": identity, "source_identity_sha256": fingerprint(identity),
               "source_world_ref": wr, "parent_query_world_ref": parent_wr,
               "source_draft_ref": identity["source_draft_ref"],
               "source_text_ref": draft["text_ref"], "target": target,
               "parent_case_hash": parent_case_hash, "parent_resolution_ref": parent_ref,
               "query_receipt_refs": deepcopy(query_receipt_refs), "pending_queries": pending,
               "target_frontier": target_frontier, "affected_subject_ids": sorted(subjects),
               "scene_scope_derivation": derivation, "required_scope_ids": scopes, "scope_ids": advancing,
               "expected_frontiers": {scope: revision["frontiers"].get(scope, {"tick": 0, "phase": 0}) for scope in advancing},
               "registry_sha256": fingerprint(state), "blockers": blockers,
               "status": ("WAITING_WORLD_REVALIDATION" if not _same_canon(parent_revision, revision) else
                          "WAITING_WORLD_PROCESS" if blockers else ("READY_FOR_CLOSURE" if advancing else "ALREADY_COVERED"))}
    request["request_sha256"] = fingerprint(request)
    return deepcopy(request)


def _checked_request(request):
    validate_contract("WorldRef", request["source_world_ref"])
    require(request.get("request_sha256") == fingerprint({key: value for key, value in request.items() if key != "request_sha256"})
            and request["source_identity_sha256"] == fingerprint(request["source_identity"]),
            "INVALID_EVIDENCE", "Coverage request source or content hash changed")


def recover_coverage(kernel, owner, request, idempotency_key):
    """Accept only an empty, proven prefix via the existing closed-command path."""
    from .idea_opening import _accept, _uid
    from .production_pipeline import _load

    _checked_request(request)
    require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 200,
            "INVALID_ARGUMENT", "A bounded coverage idempotency key is required")
    wr = request["source_world_ref"]
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": wr["work_id"]}, lock="update")
        bound = kernel.in_transaction(repo)
        board = _load(bound, repo, wr["work_id"], wr["branch_id"])
        require(board, "INVALID_EVIDENCE", "No current production board authorizes this closure")
        _owner(bound, repo, owner, board)
        old = bound._prior(repo, owner, wr["work_id"], COMMAND, idempotency_key, request)
        if old:
            return old
        fresh = plan_coverage(bound, repo, owner, board, request["target"],
                              parent_case_hash=request["parent_case_hash"], query_receipt_refs=request["query_receipt_refs"])
        require(fresh == request, "STALE_GENERATION", "Coverage source or registered influence changed")
        if request["status"] not in {"READY_FOR_CLOSURE", "ALREADY_COVERED"}:
            return {"status": request["status"], "blockers": request["blockers"],
                    "provider_calls_sent": 0, "world_effects_applied": 0}
        source = bound.revision(repo, wr)
        request_ref = _artifact(bound, repo, owner, wr["work_id"], request, "WORLD_COVERAGE_REQUEST")
        if request["status"] == "READY_FOR_CLOSURE":
            args = {"scope_ids": request["scope_ids"], "target": request["target_frontier"]}
            label = "world-coverage:" + request["request_sha256"]

            def propose(principal, task_id, generation, _record_ref, key):
                return bound.propose(principal, task_id, generation, "SCHEDULE_ADVANCE", args, key)

            receipt = _accept(bound, repo, owner, wr, request["target_frontier"], request["scope_ids"], label, request_ref, propose)
            task_id = _uid(wr["work_id"], "opening-kernel:" + label)
            proposal = repo.one("proposals", {"task_id": task_id})
            mode = "EMPTY_PREFIX_CLOSURE"
        else:
            rows = repo.conn.execute(text("""SELECT * FROM nf_receipts
                WHERE work_id=:work AND branch_id=:branch AND command_type='ACCEPT'
                AND payload->'world_ref'->>'world_revision_id'=:revision"""),
                {"work": wr["work_id"], "branch": wr["branch_id"], "revision": wr["world_revision_id"]}).mappings().all()
            require(len(rows) == 1, "INVALID_EVIDENCE", "Existing coverage needs its actual formal acceptance receipt")
            receipt = rows[0]["payload"]
            task_id = rows[0]["idempotency_key"]
            proposal = repo.one("proposals", {"proposal_id": rows[0]["proposal_id"]})
            require(str(rows[0]["principal_id"]) == owner.principal_id and proposal,
                    "VIEW_DENIED", "Existing coverage was not accepted by this production controller")
            mode = "EXISTING_FORMAL_COVERAGE"
        target = bound.revision(repo, receipt["world_ref"])
        require(not receipt["accepted_event_refs"]
                and _same_canon(target, source), "HARD_RULE_VIOLATION",
                "Empty coverage closure cannot change canonical facts or selected events")
        require(proposal["template_id"] == "SCHEDULE_ADVANCE", "INVALID_EVIDENCE",
                "Existing coverage must also come from an empty scheduling acceptance")
        payload = {"record_version": 1, "request": request, "request_ref": request_ref,
                   "transition_mode": mode, **_representation(source, target),
                   "reading_world_ref": receipt["world_ref"], "worldclosure_receipt": receipt,
                   "proposal_ref": proposal["artifact_ref"], "closure_refs": proposal["payload"]["adjudication_refs"],
                   "acceptance_key": task_id, "controller": {"grant_id": owner.grant_id, "principal_id": owner.principal_id},
                   "provider_calls_sent": 0, "world_effects_applied": 0, "allowance_changed": False,
                   "world_revision_changed": wr != receipt["world_ref"]}
        transition = _artifact(bound, repo, owner, wr["work_id"], payload, TRANSITION_KIND)
        result = {"status": "WORLD_COVERAGE_RECOVERED", "reading_world_ref": receipt["world_ref"],
                  "coverage_transition_ref": transition, "worldclosure_receipt": receipt,
                  "proposal_ref": proposal["artifact_ref"], "closure_refs": payload["closure_refs"],
                  "parent_case_hash": request["parent_case_hash"], "provider_calls_sent": 0,
                  "world_effects_applied": 0, "allowance_changed": False,
                  "world_revision_changed": payload["world_revision_changed"]}
        bound._receipt(repo, owner, wr, COMMAND, idempotency_key, request, result)
        return result


def _validate_transition(kernel, repo, board, ref, identity, seen=None):
    from .production_reassessment import MAX_REVISIONS

    seen = set() if seen is None else seen
    require(ref["sha256"] not in seen and len(seen) < MAX_REVISIONS + 1,
            "INVALID_EVIDENCE", "Coverage transition lineage is cyclic or exceeds recorded revisions")
    seen.add(ref["sha256"])
    work = board["world_ref"]["work_id"]
    payload = kernel.artifact(repo, ref, work, kind=TRANSITION_KIND)["payload"]
    request = payload["request"]
    _checked_request(request)
    require(kernel.artifact(repo, payload["request_ref"], work, kind="WORLD_COVERAGE_REQUEST")["payload"] == request,
            "INVALID_EVIDENCE", "Coverage request differs from its immutable artifact")
    require(request["source_identity"] == identity, "STALE_GENERATION", "Coverage transition source identity changed")
    current_identity = _identity(board, identity["target"])
    require(all(identity[key] == current_identity[key] for key in
                ("board_world_ref", "volume_id", "series_id", "plan_ref", "authorization", "target", "source_scene_ref", "repair_allowance")),
            "STALE_GENERATION", "Coverage transition scene, plan, controller or repair allowance changed")
    wr = payload["reading_world_ref"]
    validate_contract("WorldRef", wr)
    owner = Principal(**payload["controller"])
    _owner(kernel, repo, owner, board)
    draft = kernel.artifact(repo, identity["source_draft_ref"], work, kind="NARRATIVE_DRAFT")["payload"]
    task = repo.one("tasks", {"task_id": identity["writer_task_id"]})
    require(task and task["work_id"] == work and task["branch_id"] == wr["branch_id"]
            and draft["task_id"] == identity["writer_task_id"] and draft["generation"] == identity["writer_generation"]
            and draft["scene_ref"] == identity["source_scene_ref"]
            and request["source_draft_ref"] == identity["source_draft_ref"]
            and request["source_text_ref"] == draft["text_ref"]
            and repo.ancestor(draft["input_world_ref"], wr),
            "INVALID_EVIDENCE", "Coverage transition is not bound to the immutable draft and writer task")
    if payload.get("prior_transition_ref"):
        prior_ref = payload["prior_transition_ref"]
        prior_payload = kernel.artifact(repo, prior_ref, work, kind=TRANSITION_KIND)["payload"]
        prior_request = prior_payload["request"]
        prior_identity = prior_request["source_identity"]
        _validate_transition(kernel, repo, board, prior_ref, prior_identity, seen)
        _draft_lineage(kernel, repo, board, identity, prior_identity, wr)
        expected = deepcopy(prior_request)
        expected.update(source_identity=identity, source_identity_sha256=fingerprint(identity),
                        source_draft_ref=identity["source_draft_ref"], source_text_ref=draft["text_ref"])
        expected["request_sha256"] = fingerprint({key: value for key, value in expected.items() if key != "request_sha256"})
        require(request == expected and all(payload[key] == prior_payload[key] for key in
                ("reading_world_ref", "worldclosure_receipt", "proposal_ref", "closure_refs", "acceptance_key", "controller")),
                "INVALID_EVIDENCE", "Renewed reading changed the accepted coverage proof")
        return payload
    source_wr = request["source_world_ref"]
    require(repo.ancestor(source_wr, wr) and (wr["work_id"], wr["branch_id"]) ==
            (board["world_ref"]["work_id"], board["world_ref"]["branch_id"]), "BRANCH_CHANGED", "Reading cut changed branch")
    acceptance = repo.one("receipts", {"work_id": wr["work_id"], "principal_id": owner.principal_id,
                         "command_type": "ACCEPT", "idempotency_key": payload["acceptance_key"]})
    proposal = kernel.artifact(repo, payload["proposal_ref"], wr["work_id"], kind="WORLD_PROPOSAL")["payload"]
    proposal_row = repo.one("proposals", {"proposal_id": proposal["proposal_id"]})
    require(acceptance and acceptance["payload"] == payload["worldclosure_receipt"]
            and acceptance["payload"]["world_ref"] == wr
            and not acceptance["payload"]["accepted_event_refs"]
            and acceptance["payload"].get("proposal_ref") == payload["proposal_ref"]
            and proposal["adjudication_refs"] == payload["closure_refs"] and payload["closure_refs"]
            and proposal_row and proposal_row["artifact_ref"] == payload["proposal_ref"]
            and proposal_row["template_id"] == "SCHEDULE_ADVANCE"
            and proposal_row["principal_id"] == owner.principal_id
            and acceptance["proposal_id"] == proposal["proposal_id"], "INVALID_EVIDENCE",
            "Reading coverage requires its actual formal closure acceptance")
    for closure_ref in payload["closure_refs"]:
        closure = kernel.artifact(repo, closure_ref, wr["work_id"], kind="CLOSURE")["payload"]
        require(closure["template_id"] == "SCHEDULE_ADVANCE" and closure["proposal_id"] == proposal["proposal_id"]
                and closure["args_sha256"] == fingerprint(proposal_row["template_args"])
                and (payload["transition_mode"] == "EXISTING_FORMAL_COVERAGE" or
                     (closure["base_world_ref"] == source_wr
                      and proposal_row["template_args"] == {"scope_ids": request["scope_ids"], "target": request["target_frontier"]})),
                "INVALID_EVIDENCE", "Reading closure differs from the bounded request")
    source, current = kernel.revision(repo, source_wr), kernel.revision(repo, wr)
    parent = kernel.revision(repo, request["parent_query_world_ref"])
    require(_same_canon(source, current) and _same_canon(parent, current)
            and fingerprint(state_from_revision(current)) == request["registry_sha256"]
            and all(point_key(current["frontiers"].get(scope, {"tick": 0, "phase": 0})) >= point_key(request["target_frontier"])
                    for scope in request["required_scope_ids"]),
            "INVALID_EVIDENCE", "Coverage transition changed facts or did not close its exact prefix")
    require(all(payload[key] == value for key, value in _representation(source, current).items()),
            "INVALID_EVIDENCE", "Coverage raw snapshot hash evidence changed")
    return payload


def _draft_lineage(kernel, repo, board, current, prior, reading_wr):
    """Follow actual immutable prior_draft_ref links, never mutable board hints."""
    from .production_reassessment import MAX_REVISIONS

    delta = current["revision_count"] - prior["revision_count"]
    baseline = current["repair_allowance"].get("revision_baseline", 0)
    require(0 < delta <= MAX_REVISIONS and baseline <= current["revision_count"] <= baseline + MAX_REVISIONS
            and current["source_scene_ref"] == prior["source_scene_ref"],
            "INVALID_EVIDENCE", "Reading renewal exceeds the existing scene revision allowance")
    ref, seen = current["source_draft_ref"], set()
    for _ in range(delta):
        require(ref and ref["sha256"] not in seen, "INVALID_EVIDENCE", "Immutable draft lineage is missing or cyclic")
        seen.add(ref["sha256"])
        draft = kernel.artifact(repo, ref, reading_wr["work_id"], kind="NARRATIVE_DRAFT")["payload"]
        require(draft["scene_ref"] == current["source_scene_ref"]
                and repo.ancestor(draft["input_world_ref"], reading_wr),
                "INVALID_EVIDENCE", "Draft lineage changed scene or reading branch")
        ref = draft["prior_draft_ref"]
    require(ref == prior["source_draft_ref"], "INVALID_EVIDENCE", "Draft has no exact immutable prior-draft lineage")


def renew_reading_transition(kernel, repo, owner, board, *, target, prior_transition_ref, idempotency_key):
    """Rebind existing formal coverage to a genuine later draft; no world command."""
    _owner(kernel, repo, owner, board)
    require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 200,
            "INVALID_ARGUMENT", "A bounded renewal idempotency key is required")
    identity = _identity(board, target)
    prior = kernel.artifact(repo, prior_transition_ref, board["world_ref"]["work_id"], kind=TRANSITION_KIND)["payload"]
    prior_identity = prior["request"]["source_identity"]
    require(prior_identity["target"] == target, "VIEW_DENIED", "Reading renewal belongs to another writer")
    _validate_transition(kernel, repo, board, prior_transition_ref, prior_identity)
    renewal_request = {"source_identity": identity, "prior_transition_ref": prior_transition_ref}
    old = kernel._prior(repo, owner, board["world_ref"]["work_id"], "WORLD_COVERAGE_RENEWAL", idempotency_key, renewal_request)
    if old:
        return old
    if identity == prior_identity:
        return {"coverage_transition_ref": prior_transition_ref, "reading_world_ref": prior["reading_world_ref"],
                "provider_calls_sent": 0, "world_effects_applied": 0, "world_revision_changed": False, "allowance_changed": False}
    _draft_lineage(kernel, repo, board, identity, prior_identity, prior["reading_world_ref"])
    draft = kernel.artifact(repo, identity["source_draft_ref"], board["world_ref"]["work_id"], kind="NARRATIVE_DRAFT")["payload"]
    payload = deepcopy(prior)
    request = payload["request"]
    request.update(source_identity=identity, source_identity_sha256=fingerprint(identity),
                   source_draft_ref=identity["source_draft_ref"], source_text_ref=draft["text_ref"])
    request["request_sha256"] = fingerprint({key: value for key, value in request.items() if key != "request_sha256"})
    payload.update(prior_transition_ref=prior_transition_ref, world_revision_changed=False)
    payload["request_ref"] = _artifact(kernel, repo, owner, board["world_ref"]["work_id"], request, "WORLD_COVERAGE_REQUEST")
    ref = _artifact(kernel, repo, owner, board["world_ref"]["work_id"], payload, TRANSITION_KIND)
    _validate_transition(kernel, repo, board, ref, identity)
    result = {"coverage_transition_ref": ref, "reading_world_ref": payload["reading_world_ref"],
            "prior_transition_ref": prior_transition_ref, "provider_calls_sent": 0, "world_effects_applied": 0,
            "world_revision_changed": False, "allowance_changed": False}
    kernel._receipt(repo, owner, board["world_ref"], "WORLD_COVERAGE_RENEWAL", idempotency_key, renewal_request, result)
    return result


def validate_reading_world(kernel, repo, board, node):
    """Resolve only a formally accepted, exact-draft reading-cut transition."""
    if "reading_world_ref" not in node:
        require(not node.get("coverage_transition_ref"), "INVALID_EVIDENCE", "A transition needs its explicit reading cut")
        return node.get("world_ref", board["world_ref"])
    wr = node["reading_world_ref"]
    payload = kernel.artifact(repo, node["coverage_transition_ref"], board["world_ref"]["work_id"], kind=TRANSITION_KIND)["payload"]
    target = payload["request"]["target"]
    require(target in node.get("parents", []) or node.get("internal_case", {}).get("target") == target,
            "VIEW_DENIED", "Coverage transition belongs to another scene reading")
    require(payload["reading_world_ref"] == wr, "INVALID_EVIDENCE", "Reading cut differs from its transition")
    _validate_transition(kernel, repo, board, node["coverage_transition_ref"], _identity(board, target))
    return wr
