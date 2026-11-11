"""Evidence-bound world preparation and conditional volume planning.

All content is immutable. Selecting a plan updates only editorial pointers and
their guards; no method in this service applies a world effect. World changes
still go through Kernel.propose/prepare/accept.
"""
from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from novel_factory.application.kernel import require
from novel_factory.application.query_policy import check_profile
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import canonical_bytes, fingerprint
from novel_factory.domain.planning import affected_nodes, condition_result, future_manifest, replan_nodes, validate_graph
from novel_factory.domain.temporal import point_key


def _id():
    return str(uuid4())


def validate_task_plan_binding(repo, kernel, task, spec=None):
    """Synchronously reject stale plan inputs, even before replan workers run.

    Called by the existing task and acceptance boundary, inside its current
    transaction. An absent binding preserves P1 behavior. Registered bindings
    are never editable; unchanged plan nodes may survive an unrelated update.
    """
    binding_selection = repo.one("selections", {"work_id": task["work_id"],
        "selection_kind": "PLAN_TASK:" + task["task_id"] + ":" + str(task["generation"])})
    if binding_selection is None:
        return
    binding = kernel.artifact(repo, binding_selection["artifact_ref"], task["work_id"], kind="PLAN_TASK_BINDING")["payload"]
    require(binding["task_id"] == task["task_id"] and binding["generation"] == task["generation"],
            "STALE_PLAN", "Plan task binding does not match this execution")
    if spec is None:
        spec = repo.one("task_inputs", {"task_id": task["task_id"], "generation": task["generation"]})["spec"]
    require(binding["plan_ref"] in spec["input_artifacts"], "STALE_PLAN", "Plan is absent from immutable task inputs")
    original = kernel.artifact(repo, binding["plan_ref"], task["work_id"], kind="PLAN_REVISION")["payload"]
    require(original["input_world_ref"] == spec["world_ref"], "STALE_PLAN", "Plan and task world cuts differ")
    active = repo.one("selections", {"work_id": task["work_id"], "selection_kind": "PLAN:" + original["volume_id"]})
    require(active is not None, "STALE_PLAN", "No selected plan remains")
    selected = kernel.artifact(repo, active["artifact_ref"], task["work_id"], kind="PLAN_REVISION")["payload"]
    require(selected["input_world_ref"]["branch_id"] == spec["world_ref"]["branch_id"],
            "STALE_PLAN", "Selected plan belongs to a different branch")
    original_nodes = {n["node_id"]: n for n in original["nodes"]}
    selected_nodes = {n["node_id"]: n for n in selected["nodes"]}
    require(all(selected_nodes.get(key) == original_nodes.get(key) for key in binding["node_ids"]),
            "STALE_PLAN", "A selected plan node changed")
    scope_versions = repo.scope_map(task["work_id"], spec["world_ref"]["branch_id"])
    require(all(scope_versions.get(s["scope_key"], 0) == s["version"] for s in binding["scope_versions"]),
            "STALE_PLAN", "Plan node selection guard changed")
    head = kernel.head(repo, task["work_id"], spec["world_ref"]["branch_id"])
    revision = kernel.revision(repo, head)
    branch = repo.one("branches", {"branch_id": head["branch_id"]})
    work = repo.one("works", {"work_id": task["work_id"]})
    if branch["kind"] == "CANON":
        require(work["canon_branch_id"] == head["branch_id"], "STALE_PLAN", "Canonical history changed")
    require(revision["interpretation_ref"] == work["interpretation_ref"],
            "STALE_PLAN", "World interpretation requires renewed plan review")
    impact = affected_nodes(selected, revision["snapshot"], revision["selection"])
    require(not set(binding["node_ids"]).intersection(impact["affected"]), "STALE_PLAN",
            "Accepted world changes invalidate or leave unresolved this plan input")
    # A realized prerequisite is not checked by affected_nodes (its history is
    # already fixed). Verify its evidence along the hard predecessor closure.
    closure = set(binding["node_ids"])
    while True:
        before = len(closure)
        closure.update(edge["source_node_id"] for edge in selected["dependencies"]
                       if edge["kind"] == "HARD_PREREQUISITE" and edge["target_node_id"] in closure)
        if before == len(closure):
            break
    for key in closure:
        node = selected_nodes[key]
        require(node["status"] not in {"BLOCKED", "ABANDONED", "REVIEW_REQUIRED"},
                "STALE_PLAN", "Plan prerequisite requires review or has been abandoned")
        require(all(revision["selection"].get(e["occurrence_id"]) == e["event_version_id"] for e in node["fulfillment_event_refs"]),
                "STALE_PLAN", "A realized planning prerequisite no longer has selected event evidence")


class PlanningService:
    def __init__(self, kernel):
        self.kernel = kernel

    def _authorize(self, repo, principal, wr, *, capability="ARTIFACT_WRITE", plane="PLAN"):
        grant = self.kernel.authorize(repo, principal, wr["work_id"], capability, wr["branch_id"])
        check_profile(grant, plane)
        revision = self.kernel.revision(repo, wr)
        # A planning cut may contain late accepted facts. Restrict the entire
        # privileged planning input to the live grant instead of silently
        # answering feasibility questions using an unauthorized later state.
        require(all(point_key(p) <= point_key(grant["max_story_time"]) for p in revision["frontiers"].values()),
                "VIEW_DENIED", "Planning cut exceeds the authorized story horizon")
        return grant, revision

    @staticmethod
    def _key(key):
        require(isinstance(key, str) and 0 < len(key) <= 200, "INVALID_ARGUMENT", "Invalid idempotency key")

    def _events(self, revision, event_refs):
        for ref in event_refs:
            require(revision["selection"].get(ref["occurrence_id"]) == ref["event_version_id"],
                    "INVALID_EVIDENCE", "Event is not selected in this planning cut")

    def _refs(self, repo, work_id, refs):
        for ref in refs:
            self.kernel.artifact(repo, ref, work_id)
            self.kernel.objects.verify(ref["sha256"])

    def _save(self, principal, wr, payload, kind, key, validator, *, plane="PLAN", request_context=None, authorize_request=None, selection_name=None):
        self._key(key)
        require(len(canonical_bytes(payload)) <= 2_000_000, "CAPACITY_LIMIT", "Planning artifact is too large")
        request = {"world_ref": wr, "payload": payload, "kind": kind, "request_context": request_context}
        command = "PLANNING_SAVE_" + kind
        with self.kernel.db.uow(write=False) as repo:
            _, revision = self._authorize(repo, principal, wr, plane=plane)
            if authorize_request is not None:
                authorize_request(repo)
            old = self.kernel._prior(repo, principal, wr["work_id"], command, key, request)
            if old:
                return old
            validator(repo, revision)
        ref = self.kernel._blob(payload)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            _, revision = self._authorize(repo, principal, wr, plane=plane)
            repo.one("branches", {"branch_id": wr["branch_id"]}, lock="update")
            if authorize_request is not None:
                authorize_request(repo)
            old = self.kernel._prior(repo, principal, wr["work_id"], command, key, request)
            if old:
                return old
            validator(repo, revision)
            self.kernel._register_artifact(repo, ref, wr["work_id"], payload, kind, principal.principal_id)
            if selection_name is not None:
                self._select(repo, wr, selection_name, ref)
            self.kernel._receipt(repo, principal, wr, command, key, request, ref)
            repo.outbox("planning.candidate", "artifact", ref["artifact_revision_id"],
                        {"kind": kind, "artifact_ref": ref, "world_ref": wr},
                        "planning-candidate:" + ref["artifact_revision_id"])
        return ref

    def _validate_dossier(self, repo, revision, dossier):
        wid = revision["work_id"]
        work = repo.one("works", {"work_id": wid})
        require(dossier["ruleset_ref"] == work["rules_ref"], "INVALID_EVIDENCE",
                "Dossier must bind the accepted ruleset")
        self.kernel.artifact(repo, dossier["ruleset_ref"], wid, kind="RULESET")
        setup = self.kernel.artifact(repo, dossier["initial_setup_ref"], wid, kind="INITIAL_SETUP")
        self._refs(repo, wid, [dossier["ruleset_ref"], dossier["initial_setup_ref"]])
        entities = revision["snapshot"]["entities"]
        require(set(dossier["background_entity_ids"]) <= entities.keys(), "INVALID_EVIDENCE",
                "Background settings must refer to accepted entities")
        require(set(dossier["background_entity_ids"]) <= setup["payload"]["entities"].keys(),
                "INVALID_EVIDENCE", "Background settings lack initial acceptance provenance")
        require(len(set(dossier["background_entity_ids"])) == len(dossier["background_entity_ids"]),
                "INVALID_ARGUMENT", "Background identities repeat")
        faction_ids = [f["entity_id"] for f in dossier["factions"]]
        require(len(faction_ids) == len(set(faction_ids)), "INVALID_ARGUMENT", "Faction identities repeat")
        for faction in dossier["factions"]:
            require(faction["entity_id"] in entities and faction["decision_maker_id"] in entities,
                    "INVALID_EVIDENCE", "Faction and its decision maker must have accepted identities")
            require(entities[faction["entity_id"]]["entity_kind"] == "ORGANIZATION", "INVALID_ARGUMENT",
                    "Faction must identify an accepted organization")
            require(entities[faction["decision_maker_id"]]["entity_kind"] == "CHARACTER", "INVALID_ARGUMENT",
                    "Faction must name an accepted character as its decision maker")
        self._events(revision, dossier["source_event_refs"])

    def save_world_dossier(self, principal, payload, idempotency_key):
        """Save explanatory planning material grounded in accepted world setup.

        Motive/section prose remains a reviewed interpretation. New entities,
        resources or rules listed in proposed_world_changes are never accepted
        by this operation and must be resolved before dossier approval.
        """
        dossier = validate_contract("WorldDossier", payload, "planning")
        return self._save(principal, dossier["input_world_ref"], dossier, "WORLD_DOSSIER", idempotency_key,
                          lambda r, rev: self._validate_dossier(r, rev, dossier), plane="WORLD")

    def review_world_dossier(self, principal, world_ref, dossier_ref, decision, issues, idempotency_key):
        self._key(idempotency_key)
        request = {"world_ref": world_ref, "dossier_ref": dossier_ref, "decision": decision, "issues": issues}

        def check(repo):
            _, revision = self._authorize(repo, principal, world_ref, capability="APPROVE", plane="WORLD")
            dossier = self.kernel.artifact(repo, dossier_ref, world_ref["work_id"], kind="WORLD_DOSSIER")
            require(dossier["created_by"] != principal.principal_id, "SELF_APPROVAL_DENIED",
                    "World preparation requires a distinct reviewer")
            require(dossier["payload"]["input_world_ref"] == world_ref, "HEAD_CONFLICT", "Dossier review cut differs")
            self._validate_dossier(repo, revision, dossier["payload"])
            if decision == "APPROVED":
                require(not dossier["payload"]["proposed_world_changes"]
                        and not any(q["blocking"] for q in dossier["payload"]["open_questions"]),
                        "WORLD_SETUP_INCOMPLETE", "Unaccepted changes or blocking world questions remain")
                require(not issues, "REVIEW_UNRESOLVED", "An approved dossier cannot retain unresolved issues")
            return revision

        with self.kernel.db.uow(write=False) as repo:
            check(repo)
            old = self.kernel._prior(repo, principal, world_ref["work_id"], "DOSSIER_REVIEW", idempotency_key, request)
            if old:
                return old
        report = {"schema_version": "2.1", "review_id": _id(), "input_world_ref": world_ref,
                  "dossier_ref": dossier_ref, "decision": decision, "issues": issues,
                  "reviewer_id": principal.principal_id, "review_basis": "EVIDENCE_BOUND_REVIEWER_ATTESTATION",
                  "world_effects_applied": 0}
        validate_contract("DossierReview", report, "planning")
        ref = self.kernel._blob(report)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            check(repo)
            repo.one("branches", {"branch_id": world_ref["branch_id"]}, lock="update")
            old = self.kernel._prior(repo, principal, world_ref["work_id"], "DOSSIER_REVIEW", idempotency_key, request)
            if old:
                return old
            require(self.kernel.head(repo, world_ref["work_id"], world_ref["branch_id"]) == world_ref,
                    "HEAD_CONFLICT", "Reassess the dossier against the current world")
            self.kernel._register_artifact(repo, ref, world_ref["work_id"], report, "DOSSIER_REVIEW", principal.principal_id)
            self.kernel._receipt(repo, principal, world_ref, "DOSSIER_REVIEW", idempotency_key, request, ref)
            repo.outbox("planning.world_reviewed", "artifact", ref["artifact_revision_id"],
                        {"review_ref": ref, "decision": decision}, "dossier-review:" + ref["artifact_revision_id"])
        return ref

    def _validate_volume(self, repo, revision, contract):
        wid = revision["work_id"]
        require(set(contract["focus_subject_ids"]) <= revision["snapshot"]["entities"].keys(),
                "INVALID_EVIDENCE", "Volume focus must reference accepted identities")
        self._refs(repo, wid, contract["upstream_literary_refs"])
        work = repo.one("works", {"work_id": wid})
        for promise in contract["protected_commitments"]:
            require(promise["policy_ref"] in {work["policy_version"], work["policy_ref"]["sha256"]},
                    "VIEW_DENIED", "Protected commitments require an existing accepted policy")
            for evidence in promise["evidence_refs"]:
                if evidence["kind"] == "EVENT":
                    require(evidence["world_ref"]["work_id"] == wid, "INVALID_EVIDENCE", "Evidence belongs to another work")
                    self._events(revision, [evidence["event_ref"]])
                else:
                    self._refs(repo, wid, [evidence["artifact_ref"]])

    def save_volume_contract(self, principal, payload, idempotency_key):
        contract = validate_contract("VolumeContract", payload, "literary")
        return self._save(principal, contract["input_world_ref"], contract, "VOLUME_CONTRACT", idempotency_key,
                          lambda r, rev: self._validate_volume(r, rev, contract))

    def _validate_window(self, repo, revision, window, volume_contract_ref):
        contract = self.kernel.artifact(repo, volume_contract_ref, revision["work_id"], kind="VOLUME_CONTRACT")["payload"]
        require(window["volume_contract_revision_id"] == contract["contract_revision_id"],
                "INVALID_ARGUMENT", "Window names a different volume contract")
        require(window["status"] == "DRAFT", "INVALID_ARGUMENT", "External windows are candidates, not selected outcomes")
        require(point_key(window["story_interval"]["start"]) < point_key(window["story_interval"]["end"]),
                "INVALID_ARGUMENT", "Planning interval must be nonempty")
        require(set(window["scope_ids"] + window["focus_actor_ids"]) <= revision["snapshot"]["entities"].keys(),
                "INVALID_EVIDENCE", "Window scope references unknown identities")
        from novel_factory.domain.scheduling import state_from_revision
        require(set(window["process_ids"]) <= state_from_revision(revision)["processes"].keys(),
                "INVALID_EVIDENCE", "Window process has not been formally registered")
        self._events(revision, window["adopted_event_refs"])

    def save_window_plan(self, principal, payload, volume_contract_ref, idempotency_key):
        window = validate_contract("WindowPlan", payload, "literary")

        def check(repo, revision):
            self._validate_window(repo, revision, window, volume_contract_ref)

        # The explicit reference belongs in the immutable request, so changing
        # it under the same key cannot accidentally return a prior receipt.
        return self._save(principal, window["input_world_ref"], window, "WINDOW_PLAN", idempotency_key, check,
                          request_context={"volume_contract_ref": volume_contract_ref})

    def _validate_plan(self, repo, revision, plan):
        validate_graph(plan)
        contract = self.kernel.artifact(repo, plan["volume_contract_ref"], revision["work_id"], kind="VOLUME_CONTRACT")["payload"]
        self._refs(repo, revision["work_id"], [plan["volume_contract_ref"]])
        require(contract["volume_id"] == plan["volume_id"], "INVALID_ARGUMENT", "Plan volume and contract differ")
        require(repo.ancestor(contract["input_world_ref"], plan["input_world_ref"]), "BRANCH_CHANGED",
                "Plan must derive from the volume contract's accepted branch")
        if plan["prior_plan_ref"]:
            prior = self.kernel.artifact(repo, plan["prior_plan_ref"], revision["work_id"], kind="PLAN_REVISION")["payload"]
            require(prior["plan_id"] == plan["plan_id"] and prior["volume_id"] == plan["volume_id"],
                    "INVALID_ARGUMENT", "Plan lineage changed identity")
            require(prior["volume_contract_ref"] == plan["volume_contract_ref"], "PROTECTED_COMMITMENT_CHANGED",
                    "A rolling plan cannot silently replace its volume contract")
            require(repo.ancestor(prior["input_world_ref"], plan["input_world_ref"]), "BRANCH_CHANGED",
                    "Plan revisions cannot silently cross world branches")
            previous = {n["node_id"]: n for n in prior["nodes"]}
            current = {n["node_id"]: n for n in plan["nodes"]}
            protected = set(prior["published_prefix_node_ids"]) | {n["node_id"] for n in prior["nodes"] if n["status"] == "REALIZED"}
            require(set(previous) <= current.keys(), "PLAN_HISTORY_LOSS", "Keep abandoned/failed nodes in later plan revisions")
            require(prior["published_prefix_node_ids"] == plan["published_prefix_node_ids"]
                    and all(current.get(key) == previous[key] for key in protected),
                    "PUBLISHED_PREFIX_CONFLICT", "Accepted outcomes and published prefix cannot be changed by planning")
        entities = revision["snapshot"]["entities"]
        for node in plan["nodes"]:
            require(set(node["subject_ids"]) <= entities.keys(), "INVALID_EVIDENCE", "Plan node uses unknown subjects")
            self._events(revision, node["fulfillment_event_refs"])
            for condition in node["hard_preconditions"]:
                require(all(condition[field] in entities for field in ("subject_id", "account_id", "resource_id") if field in condition),
                        "INVALID_EVIDENCE", "Planning precondition references unknown identities")
            if node["status"] == "VIABLE":
                require(all(condition_result(c, revision["snapshot"], revision["selection"]) == "SATISFIED" for c in node["hard_preconditions"]),
                        "PLAN_PRECONDITION_FAILED", "Viable plan has broken or unresolved current prerequisites")

    def save_plan(self, principal, payload, idempotency_key):
        plan = validate_contract("PlanRevision", payload, "planning")
        return self._save(principal, plan["input_world_ref"], plan, "PLAN_REVISION", idempotency_key,
                          lambda r, rev: self._validate_plan(r, rev, plan))

    def get_plan(self, principal, work_id, plan_ref):
        """Privileged PLAN read. Character actors cannot use this evidence path."""
        with self.kernel.db.uow(write=False) as repo:
            self.kernel.authorize(repo, principal, work_id, "PLAN_READ")
            row = self.kernel.artifact(repo, plan_ref, work_id, kind="PLAN_REVISION")
            self._authorize(repo, principal, row["payload"]["input_world_ref"], capability="PLAN_READ")
            return deepcopy(row["payload"])

    @staticmethod
    def selection_key(volume_id):
        return "PLAN:" + volume_id

    def _select(self, repo, wr, name, ref):
        old = repo.one("selections", {"work_id": wr["work_id"], "selection_kind": name})
        if old:
            repo.change("selections", {"work_id": wr["work_id"], "selection_kind": name},
                        {"artifact_ref": ref, "epoch": old["epoch"] + 1})
        else:
            repo.add("selections", {"work_id": wr["work_id"], "selection_kind": name, "artifact_ref": ref, "epoch": 1})

    def adopt_plan(self, principal, world_ref, plan_ref, expected_plan_ref, idempotency_key, *, rolling_review_ref=None):
        """Select an exact candidate with CAS; return a UnifiedQuery PLAN cut."""
        self._key(idempotency_key)
        request = {"world_ref": world_ref, "plan_ref": plan_ref, "expected_plan_ref": expected_plan_ref}
        if rolling_review_ref is not None:
            request["rolling_review_ref"] = rolling_review_ref
        with self.kernel.db.uow(write=False) as repo:
            _, revision = self._authorize(repo, principal, world_ref, capability="APPROVE")
            old = self.kernel._prior(repo, principal, world_ref["work_id"], "PLAN_ADOPT", idempotency_key, request)
            if old:
                return old
            plan = self.kernel.artifact(repo, plan_ref, world_ref["work_id"], kind="PLAN_REVISION")["payload"]
            require(plan["input_world_ref"] == world_ref, "HEAD_CONFLICT", "Adoption requires the exact candidate's world cut")
            self._validate_plan(repo, revision, plan)
        manifest = future_manifest(plan, plan_ref)
        validate_contract("PlanManifest", manifest, "kernel")
        manifest_ref = self.kernel._blob(manifest)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            # Work -> branch is the kernel's common lock order. The work lock
            # covers the first-selection absence race as well as later CAS.
            repo.one("works", {"work_id": world_ref["work_id"]}, lock="update")
            self._authorize(repo, principal, world_ref, capability="APPROVE")
            repo.one("branches", {"branch_id": world_ref["branch_id"]}, lock="update")
            old = self.kernel._prior(repo, principal, world_ref["work_id"], "PLAN_ADOPT", idempotency_key, request)
            if old:
                return old
            require(self.kernel.head(repo, world_ref["work_id"], world_ref["branch_id"]) == world_ref,
                    "HEAD_CONFLICT", "World changed before plan adoption")
            self._validate_plan(repo, self.kernel.revision(repo, world_ref), plan)
            key = self.selection_key(plan["volume_id"])
            current = repo.one("selections", {"work_id": world_ref["work_id"], "selection_kind": key})
            require((current["artifact_ref"] if current else None) == expected_plan_ref,
                    "PLAN_SELECTION_CONFLICT", "Selected plan changed; review the new selection")
            require(plan["prior_plan_ref"] == expected_plan_ref, "PLAN_SELECTION_CONFLICT",
                    "Candidate does not descend from the expected active plan")
            from novel_factory.application.rolling_plans import RollingPlanning
            rolling = RollingPlanning(self.kernel).finalize_adoption(repo, world_ref, plan, plan_ref, rolling_review_ref)
            previous = self.kernel.artifact(repo, expected_plan_ref, world_ref["work_id"], kind="PLAN_REVISION")["payload"] if expected_plan_ref else None
            previous_nodes = {n["node_id"]: n for n in previous["nodes"]} if previous else {}
            changed = [n["node_id"] for n in plan["nodes"] if previous_nodes.get(n["node_id"]) != n]
            self.kernel._register_artifact(repo, manifest_ref, world_ref["work_id"], manifest, "PLAN_MANIFEST", principal.principal_id)
            self._select(repo, world_ref, key, plan_ref)
            self._select(repo, world_ref, "PLAN_MANIFEST:" + plan["volume_id"], manifest_ref)
            keys = {"ARTIFACT_SELECTION:" + key, "ARTIFACT_SELECTION:PLAN_MANIFEST:" + plan["volume_id"]}
            keys.update("ARTIFACT_SELECTION:PLAN_NODE:" + plan["volume_id"] + ":" + node for node in changed)
            scopes = repo.scope_map(world_ref["work_id"], world_ref["branch_id"])
            if keys - scopes.keys():
                keys.add("SCOPE_REGISTRY:world")
            updated = repo.bump(world_ref["work_id"], world_ref["branch_id"], keys)
            for scope in sorted(keys):
                repo.add("invalidations", {"invalidation_id": _id(), **world_ref, "scope_key": scope, "version": updated[scope]})
            fenced = []
            for dependency in repo.many("dependencies", {"work_id": world_ref["work_id"], "branch_id": world_ref["branch_id"], "scope_key": list(keys), "owner_type": "planning_task"}):
                task_id, generation = dependency["owner_id"].rsplit(":", 1)
                task = repo.one("tasks", {"task_id": task_id}, lock="update")
                if task and task["generation"] == int(generation) and task["state"] in {"READY", "RUNNING", "RESULT_STORED", "VALIDATING", "WAITING_REVIEW"}:
                    repo.change("tasks", {"task_id": task_id}, {"state": "STALE", "lease_expiry": None, "updated_at": repo.now()})
                    self.kernel.tasks._emit(repo.conn, task_id, task["generation"], "STALE")
                    fenced.append(task_id)
            result = {"plan_ref": plan_ref, "plan_manifest_ref": manifest_ref, "world_ref": world_ref,
                      "knowledge_cut": {"plane": "PLAN", "plan_manifest_ref": manifest_ref},
                      "changed_node_ids": changed, "fenced_task_ids": sorted(set(fenced)), "world_effects_applied": 0,
                      "selection_scope": "ARTIFACT_SELECTION:" + key, "selection_epoch": updated["ARTIFACT_SELECTION:" + key]}
            if rolling:
                result.update(rolling)
                self._select(repo, world_ref, "WINDOW_PLAN:" + plan["volume_id"], rolling["window_ref"])
            self.kernel._receipt(repo, principal, world_ref, "PLAN_ADOPT", idempotency_key, request, result)
            repo.outbox("planning.selected", "volume", plan["volume_id"], result,
                        "plan-selection:" + plan_ref["artifact_revision_id"])
        return result

    def replan(self, principal, payload, idempotency_key):
        """Create a candidate local revision; an authorized editor adopts it separately."""
        request = validate_contract("ReplanRequest", payload, "planning")
        wr = request["input_world_ref"]
        self._key(idempotency_key)
        with self.kernel.db.uow(write=False) as repo:
            _, revision = self._authorize(repo, principal, wr)
            old = self.kernel._prior(repo, principal, wr["work_id"], "PLAN_REPLAN", idempotency_key, request)
            if old:
                return old
            prior = self.kernel.artifact(repo, request["prior_plan_ref"], wr["work_id"], kind="PLAN_REVISION")["payload"]
            require(repo.ancestor(prior["input_world_ref"], wr), "BRANCH_CHANGED", "Replan requires a successor on the same accepted branch")
            for node in prior["nodes"]:
                self._events(revision, node["fulfillment_event_refs"])
            impact = affected_nodes(prior, revision["snapshot"], revision["selection"])
            require(impact["affected"], "NO_REPLAN_IMPACT", "No affected prerequisites justify this local replan")
            plan = replan_nodes(prior, impact, request["replacements"], request["additional_nodes"], request["additional_dependencies"])
            plan.update(plan_revision_id=_id(), input_world_ref=wr, prior_plan_ref=request["prior_plan_ref"], decision=request["decision"])
            validate_contract("PlanRevision", plan, "planning")
            self._validate_plan(repo, revision, plan)
        plan_ref = self.kernel._blob(plan)
        report = {"schema_version": "2.1", "prior_plan_ref": request["prior_plan_ref"], "new_plan_ref": plan_ref,
                  "input_world_ref": wr, "directly_broken_node_ids": impact["direct"], "affected_node_ids": impact["affected"],
                  "preserved_node_ids": [n["node_id"] for n in prior["nodes"] if n["node_id"] not in impact["affected"]],
                  "review_node_ids": impact["review"], "replaced_node_ids": [n["node_id"] for n in request["replacements"]],
                  "world_effects_applied": 0}
        validate_contract("ReplanReport", report, "planning")
        report_ref = self.kernel._blob(report)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            _, revision = self._authorize(repo, principal, wr)
            repo.one("branches", {"branch_id": wr["branch_id"]}, lock="update")
            old = self.kernel._prior(repo, principal, wr["work_id"], "PLAN_REPLAN", idempotency_key, request)
            if old:
                return old
            self._validate_plan(repo, revision, plan)
            self.kernel._register_artifact(repo, plan_ref, wr["work_id"], plan, "PLAN_REVISION", principal.principal_id)
            self.kernel._register_artifact(repo, report_ref, wr["work_id"], report, "REPLAN_REPORT", principal.principal_id)
            result = {"plan_ref": plan_ref, "report_ref": report_ref, "report": report}
            self.kernel._receipt(repo, principal, wr, "PLAN_REPLAN", idempotency_key, request, result)
            repo.outbox("planning.replanned", "volume", plan["volume_id"], result,
                        "replan:" + plan_ref["artifact_revision_id"])
        return result

    def bind_task(self, principal, task_id, generation, plan_ref, node_ids, idempotency_key):
        """Bind node selection guards before a task runs semantic production.

        Unchanged nodes keep valid qualifications across unrelated replans.
        Any changed selected node synchronously fences its dependent tasks in
        the same selection transaction; late outputs remain auditable via P1.
        """
        self._key(idempotency_key)
        validate_contract("UUID", task_id)
        validate_contract("ArtifactRef", plan_ref)
        require(type(generation) is int and generation > 0, "INVALID_ARGUMENT", "Invalid task generation")
        require(isinstance(node_ids, list) and 0 < len(node_ids) <= 200, "INVALID_ARGUMENT", "Invalid plan node binding")
        for node_id in node_ids:
            validate_contract("UUID", node_id)
        request = {"task_id": task_id, "generation": generation, "plan_ref": plan_ref, "node_ids": sorted(set(node_ids))}
        with self.kernel.db.uow(write=False) as repo:
            task, grant = self.kernel._task_owner(repo, principal, task_id, "READ")
            check_profile(grant, "PLAN")
            task_input = repo.one("task_inputs", {"task_id": task_id, "generation": generation})
            require(task_input is not None and task["generation"] == generation,
                    "STALE_GENERATION", "Task generation is not current")
            wr = task_input["spec"]["world_ref"]
            plan = self.kernel.artifact(repo, plan_ref, wr["work_id"], kind="PLAN_REVISION")["payload"]
            keys = ["ARTIFACT_SELECTION:PLAN_NODE:" + plan["volume_id"] + ":" + node for node in request["node_ids"]]
            scopes = repo.scope_map(wr["work_id"], wr["branch_id"])
            payload = {"schema_version": "2.1", "task_id": task_id, "generation": generation, "plan_ref": plan_ref,
                       "node_ids": request["node_ids"], "scope_versions": [{"scope_key": key, "version": scopes.get(key, 0)} for key in keys]}
        validate_contract("PlanTaskBinding", payload, "planning")
        ref = self.kernel._blob(payload)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": wr["work_id"]}, lock="update")
            self._authorize(repo, principal, wr, capability="READ")
            old = self.kernel._prior(repo, principal, wr["work_id"], "PLAN_BIND_TASK", idempotency_key, request)
            if old:
                return old
            task = self.kernel.tasks.guard_task(repo.conn, task_id, generation)
            spec = repo.one("task_inputs", {"task_id": task_id, "generation": generation})["spec"]
            require(plan_ref in spec["input_artifacts"], "VIEW_DENIED", "Plan is not an immutable task input")
            require(plan["input_world_ref"] == wr, "TASK_REBIND_REQUIRED", "Plan and task must pin the same world")
            current = repo.one("selections", {"work_id": wr["work_id"], "selection_kind": self.selection_key(plan["volume_id"])})
            require(current and current["artifact_ref"] == plan_ref, "PLAN_SELECTION_CONFLICT", "Plan is not selected")
            known = {n["node_id"] for n in plan["nodes"]}
            require(request["node_ids"] and set(request["node_ids"]) <= known, "INVALID_ARGUMENT", "Task binding uses unknown plan nodes")
            owner_id = task_id + ":" + str(generation)
            existing = repo.many("dependencies", {"owner_type": "planning_task", "owner_id": owner_id, "work_id": wr["work_id"]})
            require(not existing, "TASK_REBIND_REQUIRED", "Task generation already has immutable plan guards")
            scopes = repo.scope_map(wr["work_id"], wr["branch_id"])
            require(all(scopes.get(s["scope_key"], 0) == s["version"] for s in payload["scope_versions"]),
                    "PLAN_SELECTION_CONFLICT", "Plan node selection changed during binding")
            self.kernel._register_artifact(repo, ref, wr["work_id"], payload, "PLAN_TASK_BINDING", principal.principal_id)
            selection_kind = "PLAN_TASK:" + task_id + ":" + str(generation)
            require(repo.one("selections", {"work_id": wr["work_id"], "selection_kind": selection_kind}) is None,
                    "TASK_REBIND_REQUIRED", "Task generation already selected a plan binding")
            repo.add("selections", {"work_id": wr["work_id"], "selection_kind": selection_kind, "artifact_ref": ref, "epoch": 1})
            for scope in payload["scope_versions"]:
                repo.add("dependencies", {"dependency_id": _id(), "work_id": wr["work_id"], "branch_id": wr["branch_id"],
                    "owner_type": "planning_task", "owner_id": owner_id, "scope_key": scope["scope_key"],
                    "observed_version": scope["version"], "dependency_kind": "ARTIFACT_SELECTION"})
            self.kernel._receipt(repo, principal, wr, "PLAN_BIND_TASK", idempotency_key, request, ref)
        return ref

    def bind_task_for_controller(self, principal, task_id, generation, plan_ref, node_ids, idempotency_key):
        """Freeze a writer's plan guards without granting it PLAN visibility.

        Only an ADMIN may authorize another principal's immutable execution.
        The target keeps its own live grant, input cut and task provenance.
        """
        from novel_factory.application.authorization import Principal
        self._key(idempotency_key)
        request = {"task_id": task_id, "generation": generation, "plan_ref": plan_ref, "node_ids": sorted(set(node_ids))}

        def check(repo):
            task = repo.one("tasks", {"task_id": task_id})
            require(task is not None, "VIEW_DENIED", "Task unavailable")
            self.kernel.authorize(repo, principal, task["work_id"], "ADMIN", task["branch_id"])
            target = Principal(task["grant_id"], task["principal_id"])
            grant = self.kernel.authorize(repo, target, task["work_id"], "ARTIFACT_WRITE", task["branch_id"])
            require(grant["role_id"] in {"writer", "volume_director", "control_operator"}, "VIEW_DENIED", "Role is outside controlled writing")
            task = self.kernel.tasks.guard_task(repo.conn, task_id, generation)
            spec = repo.one("task_inputs", {"task_id": task_id, "generation": generation})["spec"]
            context = repo.one("contexts", {"context_bundle_id": task["context_bundle_id"]})
            require(context["read_cut"]["permission_epoch"] == grant["epoch"], "VIEW_DENIED", "Target task authorization changed")
            wr = spec["world_ref"]
            self._authorize(repo, principal, wr, capability="ADMIN")
            require(plan_ref in spec["input_artifacts"], "VIEW_DENIED", "Plan is absent from fixed target input")
            plan = self.kernel.artifact(repo, plan_ref, wr["work_id"], kind="PLAN_REVISION")["payload"]
            require(plan["input_world_ref"] == wr, "TASK_REBIND_REQUIRED", "Target plan and task world cuts differ")
            current = repo.one("selections", {"work_id": wr["work_id"], "selection_kind": self.selection_key(plan["volume_id"])})
            require(current and current["artifact_ref"] == plan_ref, "PLAN_SELECTION_CONFLICT", "Target plan is no longer selected")
            require(request["node_ids"] and set(request["node_ids"]) <= {n["node_id"] for n in plan["nodes"]},
                    "INVALID_ARGUMENT", "Target plan nodes are unavailable")
            return task, wr, plan

        with self.kernel.db.uow(write=False) as repo:
            task, wr, plan = check(repo)
            old = self.kernel._prior(repo, principal, wr["work_id"], "PLAN_BIND_CONTROLLED_TASK", idempotency_key, request)
            if old:
                return old
            scopes = repo.scope_map(wr["work_id"], wr["branch_id"])
            keys = ["ARTIFACT_SELECTION:PLAN_NODE:" + plan["volume_id"] + ":" + n for n in request["node_ids"]]
        payload = validate_contract("PlanTaskBinding", {"schema_version": "2.1", "task_id": task_id, "generation": generation,
            "plan_ref": plan_ref, "node_ids": request["node_ids"],
            "scope_versions": [{"scope_key": key, "version": scopes.get(key, 0)} for key in keys]}, "planning")
        ref = self.kernel._blob(payload)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": wr["work_id"]}, lock="update")
            task, wr, plan = check(repo)
            old = self.kernel._prior(repo, principal, wr["work_id"], "PLAN_BIND_CONTROLLED_TASK", idempotency_key, request)
            if old:
                return old
            key = "PLAN_TASK:" + task_id + ":" + str(generation)
            require(repo.one("selections", {"work_id": wr["work_id"], "selection_kind": key}) is None,
                    "TASK_REBIND_REQUIRED", "Target generation already has immutable plan guards")
            current = repo.scope_map(wr["work_id"], wr["branch_id"])
            require(all(current.get(s["scope_key"], 0) == s["version"] for s in payload["scope_versions"]),
                    "PLAN_SELECTION_CONFLICT", "Plan guard changed during binding")
            self.kernel._register_artifact(repo, ref, wr["work_id"], payload, "PLAN_TASK_BINDING", principal.principal_id)
            repo.add("selections", {"work_id": wr["work_id"], "selection_kind": key, "artifact_ref": ref, "epoch": 1})
            for scope in payload["scope_versions"]:
                repo.add("dependencies", {"dependency_id": _id(), "work_id": wr["work_id"], "branch_id": wr["branch_id"],
                    "owner_type": "planning_task", "owner_id": task_id + ":" + str(generation), "scope_key": scope["scope_key"],
                    "observed_version": scope["version"], "dependency_kind": "ARTIFACT_SELECTION"})
            validate_task_plan_binding(repo, self.kernel, task)
            self.kernel._receipt(repo, principal, wr, "PLAN_BIND_CONTROLLED_TASK", idempotency_key, request, ref)
        return ref
