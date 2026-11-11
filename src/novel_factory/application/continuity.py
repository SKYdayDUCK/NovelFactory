"""Cross-volume continuity over existing immutable world and release artifacts.

The series snapshot is a derived, explicitly reviewed reading-order manifest.
It does not own facts, modify old prose, dispatch a model, or apply world effects.
"""
from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from novel_factory.application.kernel import require
from novel_factory.application.query_policy import check_profile
from novel_factory.application.release import _payload
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import canonical_bytes, fingerprint
from novel_factory.domain.continuity import validate_append, validate_entries
from novel_factory.domain.editorial import verify_span
from novel_factory.domain.releasing import manifest_digest
from novel_factory.domain.temporal import point_key


def guard_serial_prefix(repo, request):
    """ReleaseService calls this inside its existing work-lock transaction.

    A protected volume can be retained forever and exported by its exact release
    reference. A changed serial edition uses newly reviewed volume identities
    through ContinuityService.REVISE, preserving the old edition and prefix.
    """
    marker = repo.one("selections", {"work_id": request["world_ref"]["work_id"],
                                   "selection_kind": "SERIAL_PREFIX:" + request["volume_id"]})
    require(marker is None, "SERIAL_PREFIX_PROTECTED",
            "This volume belongs to a protected serial edition; publish a separately reviewed new-volume edition")


class ContinuityService:
    def __init__(self, kernel):
        self.kernel = kernel

    @staticmethod
    def _key(value):
        require(isinstance(value, str) and 0 < len(value) <= 200,
                "INVALID_ARGUMENT", "Invalid idempotency key")

    def _work_access(self, repo, principal, work_id, capability="READ"):
        grant = self.kernel.authorize(repo, principal, work_id, capability)
        check_profile(grant, "WORLD")
        check_profile(grant, "PLAN")
        require(grant["actor_id"] is None, "VIEW_DENIED", "Series control is outside an actor's private view")
        return grant

    def _authorize(self, repo, principal, wr, capability="READ", *, current=False):
        grant = self._work_access(repo, principal, wr["work_id"], capability)
        self.kernel.authorize(repo, principal, wr["work_id"], capability, wr["branch_id"])
        revision = self.kernel.revision(repo, wr)
        require(all(point_key(p) <= point_key(grant["max_story_time"]) for p in revision["frontiers"].values()),
                "VIEW_DENIED", "Continuity world cut exceeds the authorized story horizon")
        if current:
            require(self.kernel.head(repo, wr["work_id"]) == wr, "STALE_CONTINUITY_WORLD",
                    "New continuity decisions must use the current canonical world cut")
        return grant, revision

    def _candidate(self, repo, ref, wid):
        value = _payload(repo, self.kernel, ref, wid, "CONTINUITY_CANDIDATE")
        return validate_contract("ContinuityCandidate", value, "continuity")

    def _snapshot(self, repo, ref, wid):
        if ref is None:
            return None
        return _payload(repo, self.kernel, ref, wid, "CONTINUITY_SNAPSHOT")

    def _release(self, repo, principal, ref, wid):
        record = _payload(repo, self.kernel, ref, wid, "RELEASE_RECORD", "ReleaseRecord")
        manifest = _payload(repo, self.kernel, record["manifest_ref"], wid, "RELEASE_MANIFEST")
        self._authorize(repo, principal, manifest["world_ref"])
        require(manifest_digest(manifest) == manifest["manifest_sha256"]
                and record["release_id"] == manifest["release_id"] and record["world_effects_applied"] == 0,
                "PROJECTION_CORRUPT", "Release manifest identity or digest differs")
        return {"release_ref": ref, "record": record, "manifest": manifest}

    def _releases(self, repo, principal, refs, wid):
        releases = [self._release(repo, principal, ref, wid) for ref in refs]
        require(len({r["record"]["volume_id"] for r in releases}) == len(releases),
                "INVALID_ARGUMENT", "One series edition cannot contain two versions of the same volume")
        return releases

    def _impact(self, repo, principal, snapshot, target_wr, proposed_refs=None):
        target = self.kernel.revision(repo, target_wr)
        if snapshot is None:
            return {"status": "UNCHANGED", "target_world_ref": target_wr, "affected_volumes": [],
                    "changed_occurrence_ids": [], "world_effects_applied": 0}
        old = snapshot["request"]
        releases = self._releases(repo, principal, old["ordered_release_refs"], target_wr["work_id"])
        _, reviewed = self._authorize(repo, principal, old["world_ref"])
        # Entries can cite accepted events added after a volume's frozen cut.
        # Include those explicit dependencies as well as the release cut;
        # otherwise a later revision could change memory without impacting its
        # volume (and all downstream volumes).
        entry_occurrences = {}
        for entry in old["entries"]:
            entry_occurrences.setdefault(entry["volume_ordinal"], set()).update(
                evidence["event_ref"]["occurrence_id"] for evidence in entry["source_events"])
        changed, affected, carry = set(), [], False
        for index, release in enumerate(releases, 1):
            source = self.kernel.revision(repo, release["manifest"]["world_ref"])
            basis = set(source["selection"]) | entry_occurrences.get(index, set())
            differences = {oid for oid in basis
                           if target["selection"].get(oid) != reviewed["selection"].get(oid)}
            reasons = []
            if differences:
                reasons.append("SELECTED_EVENT_CHANGED")
                changed.update(differences)
            if reviewed["interpretation_ref"] != target["interpretation_ref"]:
                reasons.append("WORLD_INTERPRETATION_CHANGED")
            if not repo.ancestor(old["world_ref"], target_wr):
                reasons.append("WORLD_LINEAGE_REVIEW_REQUIRED")
            if proposed_refs is not None and (len(proposed_refs) < index or proposed_refs[index - 1] != release["release_ref"]):
                reasons.append("PUBLISHED_EDITION_CHANGED")
            if carry:
                reasons.append("UPSTREAM_VOLUME_CHANGED")
            if reasons:
                affected.append({"ordinal": index, "volume_id": release["record"]["volume_id"],
                    "release_ref": release["release_ref"], "reasons": reasons, "requires_downstream_review": True})
                carry = True
        result = {"status": "REVIEW_REQUIRED" if affected else "UNCHANGED", "target_world_ref": target_wr,
                  "affected_volumes": affected, "changed_occurrence_ids": sorted(changed), "world_effects_applied": 0}
        return validate_contract("ContinuityImpact", result, "continuity")

    def _validate(self, repo, principal, request):
        wr, wid = request["world_ref"], request["world_ref"]["work_id"]
        grant, revision = self._authorize(repo, principal, wr, current=True)
        selected = repo.one("selections", {"work_id": wid, "selection_kind": "CONTINUITY:" + request["series_id"]})
        require((selected["artifact_ref"] if selected else None) == request["expected_prior_ref"],
                "CONTINUITY_CONFLICT", "Series selection changed; review the current edition")
        previous = self._snapshot(repo, request["expected_prior_ref"], wid)
        require(previous is None or previous["request"]["series_id"] == request["series_id"],
                "INVALID_EVIDENCE", "Prior snapshot belongs to another series")
        validate_append(previous, request)
        validate_entries(request["entries"], len(request["ordered_release_refs"]))
        releases = self._releases(repo, principal, request["ordered_release_refs"], wid)
        impact = self._impact(repo, principal, previous, wr, request["ordered_release_refs"])
        if request["operation"] == "APPEND":
            require(impact["status"] == "UNCHANGED", "CONTINUITY_REVISION_REQUIRED",
                    "Changed source history requires an explicitly reviewed series revision")
        previous_refs = previous["request"]["ordered_release_refs"] if previous else []
        for release in releases:
            if release["release_ref"] not in previous_refs:
                active = repo.one("selections", {"work_id": wid,
                    "selection_kind": "RELEASE:" + release["record"]["volume_id"]})
                require(active and active["artifact_ref"] == release["release_ref"],
                        "RELEASE_SELECTION_CONFLICT", "New volume must use its current accepted release")
            # An old fixed release can be kept as historical prose during an
            # explicit revision. It never becomes newly certified world truth.
            if request["operation"] == "APPEND" and release["release_ref"] not in previous_refs:
                require(repo.ancestor(release["manifest"]["world_ref"], wr),
                        "CONTINUITY_REVISION_REQUIRED", "Cross-lineage release inheritance needs review")
        allowed_texts = {r["release_ref"]["artifact_revision_id"]: (
            index, r["release_ref"], r["manifest"]["ordered_artifacts"])
            for index, r in enumerate(releases, 1)}
        # Retained historic entries may cite the previous edition's text. The
        # old artifacts remain traceable instead of being silently re-anchored.
        if previous and request["operation"] == "REVISE":
            for index, release in enumerate(self._releases(repo, principal,
                    previous["request"]["ordered_release_refs"], wid), 1):
                allowed_texts.setdefault(release["release_ref"]["artifact_revision_id"],
                    (index, release["release_ref"], release["manifest"]["ordered_artifacts"]))
        prior_ids = {entry["entry_id"] for entry in previous["request"]["entries"]} if previous else set()
        for entry in request["entries"]:
            require(set(entry["entity_ids"]) <= revision["snapshot"]["entities"].keys(),
                    "INVALID_EVIDENCE", "Entry entity lacks accepted world identity")
            require(point_key(entry["story_interval"]["end"]) <= point_key(grant["max_story_time"]),
                    "VIEW_DENIED", "Entry evidence exceeds the authorized story horizon")
            if entry["planned_payoff_at"] is not None:
                require(point_key(entry["planned_payoff_at"]) <= point_key(grant["max_story_time"]),
                        "VIEW_DENIED", "Payoff plan exceeds the authorized planning horizon")
            for evidence in entry["source_events"]:
                source_wr, event = evidence["world_ref"], evidence["event_ref"]
                require(source_wr["work_id"] == wid, "INVALID_EVIDENCE", "Evidence belongs to another work")
                _, source_rev = self._authorize(repo, principal, source_wr)
                require(source_rev["selection"].get(event["occurrence_id"]) == event["event_version_id"],
                        "INVALID_EVIDENCE", "Event was not selected in its declared immutable source cut")
                stored = repo.one("events", {"work_id": wid, "event_version_id": event["event_version_id"]})
                occurrence = stored["payload"]["occurrence_time"]
                endpoint = occurrence.get("at") if occurrence["kind"] == "EXACT" else occurrence.get("latest")
                require(endpoint is not None and point_key(endpoint) <= point_key(entry["story_interval"]["end"]),
                        "INVALID_EVIDENCE", "Event source lacks a resolved upper time within this entry's evidence cut")
                if entry["entry_id"] not in prior_ids:
                    require(revision["selection"].get(event["occurrence_id"]) == event["event_version_id"],
                            "CONTINUITY_REVALIDATION_REQUIRED", "New evidence is no longer selected")
            for evidence in entry["source_texts"]:
                available = allowed_texts.get(evidence["release_ref"]["artifact_revision_id"])
                require(available and available[1] == evidence["release_ref"] and available[0] <= entry["volume_ordinal"]
                        and evidence["text_ref"] in [item["artifact_ref"] for item in available[2]],
                        "INVALID_EVIDENCE", "Text source is not in this volume's already published reading prefix")
                self.kernel.artifact(repo, evidence["text_ref"], wid, kind="TEXT")
                original = self.kernel.objects.read(evidence["text_ref"]["sha256"]).decode("utf-8")
                verify_span(original, evidence["span"])
        return previous, releases, impact

    def prepare(self, principal, payload):
        request = validate_contract("ContinuityRequest", payload, "continuity")
        require(len(canonical_bytes(request)) <= 2_000_000, "CAPACITY_LIMIT", "Continuity manifest is too large")
        wr, key = request["world_ref"], request["idempotency_key"]
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": wr["work_id"]}, lock="update")
            self._authorize(repo, principal, wr, "ARTIFACT_WRITE")
            prior = self.kernel._prior(repo, principal, wr["work_id"], "CONTINUITY_PREPARE", key, request)
            if prior:
                return prior
            _, _, impact = self._validate(repo, principal, request)
            candidate = {"schema_version": "2.3", "candidate_id": str(uuid4()), "request": request,
                "impact": impact, "input_sha256": fingerprint(request), "created_by": principal.principal_id,
                "recorded_at": repo.now().isoformat(), "authority": "DERIVED_CONTINUITY_NOT_WORLD_HISTORY",
                "world_effects_applied": 0}
            validate_contract("ContinuityCandidate", candidate, "continuity")
            ref = self.kernel._blob(candidate)
            self.kernel._register_artifact(repo, ref, wr["work_id"], candidate, "CONTINUITY_CANDIDATE", principal.principal_id)
            self.kernel._receipt(repo, principal, wr, "CONTINUITY_PREPARE", key, request, ref)
        return ref

    def read_candidate(self, principal, work_id, candidate_ref, idempotency_key):
        self._key(idempotency_key)
        validate_contract("ArtifactRef", candidate_ref)
        request = {"work_id": work_id, "candidate_ref": candidate_ref}
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": work_id}, lock="update")
            self._work_access(repo, principal, work_id, "READ")
            candidate = self._candidate(repo, candidate_ref, work_id)
            wr = candidate["request"]["world_ref"]
            grant, _ = self._authorize(repo, principal, wr, "APPROVE", current=True)
            _, releases, impact = self._validate(repo, principal, candidate["request"])
            require(impact == candidate["impact"], "CONTINUITY_REVALIDATION_REQUIRED", "Candidate impact changed")
            review_releases = [(index, release, "PROPOSED_EDITION") for index, release in enumerate(releases, 1)]
            if candidate["request"]["expected_prior_ref"] is not None:
                previous = self._snapshot(repo, candidate["request"]["expected_prior_ref"], work_id)
                for index, release in enumerate(self._releases(repo, principal,
                        previous["request"]["ordered_release_refs"], work_id), 1):
                    if release["release_ref"] not in candidate["request"]["ordered_release_refs"]:
                        review_releases.append((index, release, "HISTORICAL_EDITION"))
            texts = []
            for ordinal, release, edition_role in review_releases:
                for item in release["manifest"]["ordered_artifacts"]:
                    text = self.kernel.objects.read(item["artifact_ref"]["sha256"]).decode("utf-8")
                    texts.append({"volume_ordinal": ordinal, "edition_role": edition_role, "release_ref": release["release_ref"],
                                  "text_ref": item["artifact_ref"], "text": text})
            require(sum(len(t["text"].encode("utf-8")) for t in texts) <= 2_000_000,
                    "CAPACITY_LIMIT", "Whole-series review exceeds this finite review window")
            # This records actual delivered originals, not a caller's claim to
            # have read a summary. Comprehension remains an independent question.
            receipt = {"candidate_ref": candidate_ref, "world_ref": wr,
                "principal_id": principal.principal_id, "grant_id": principal.grant_id, "grant_epoch": grant["epoch"],
                "delivered_sha256": fingerprint(texts), "text_refs": [t["text_ref"] for t in texts],
                "input_sha256": candidate["input_sha256"], "mode": "COMPLETE_ORIGINALS_DELIVERED",
                "recorded_at": repo.now().isoformat()}
            prior = self.kernel._prior(repo, principal, work_id, "CONTINUITY_READ", idempotency_key, request)
            if prior:
                reading_ref = prior["reading_ref"]
            else:
                reading_ref = self.kernel._blob(receipt)
                self.kernel._register_artifact(repo, reading_ref, work_id, receipt, "CONTINUITY_READING", principal.principal_id)
                self.kernel._receipt(repo, principal, wr, "CONTINUITY_READ", idempotency_key, request, {"reading_ref": reading_ref})
            self._authorize(repo, principal, wr, "APPROVE", current=True)
        return {"candidate": candidate, "original_texts": texts, "reading_ref": reading_ref,
                "comprehension": "NOT_VERIFIED", "world_effects_applied": 0}

    def review(self, principal, work_id, payload):
        request = validate_contract("ContinuityReviewRequest", payload, "continuity")
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": work_id}, lock="update")
            self._work_access(repo, principal, work_id, "APPROVE")
            candidate = self._candidate(repo, request["candidate_ref"], work_id)
            wr = candidate["request"]["world_ref"]
            grant, _ = self._authorize(repo, principal, wr, "APPROVE", current=True)
            prior = self.kernel._prior(repo, principal, work_id, "CONTINUITY_REVIEW", request["idempotency_key"], request)
            if prior:
                return prior
            _, _, impact = self._validate(repo, principal, candidate["request"])
            require(candidate["created_by"] != principal.principal_id, "SELF_APPROVAL_DENIED",
                    "Continuity approval requires a distinct authenticated principal")
            reading = _payload(repo, self.kernel, request["reading_ref"], work_id, "CONTINUITY_READING")
            require(reading["candidate_ref"] == request["candidate_ref"]
                    and reading["principal_id"] == principal.principal_id and reading["grant_id"] == principal.grant_id
                    and reading["grant_epoch"] == grant["epoch"] and reading["input_sha256"] == candidate["input_sha256"]
                    and reading["mode"] == "COMPLETE_ORIGINALS_DELIVERED",
                    "INVALID_READING_COVERAGE", "Review lacks this principal's current complete original delivery")
            require(impact == candidate["impact"], "CONTINUITY_REVALIDATION_REQUIRED", "Impact differs from the reviewed candidate")
            if request["decision"] == "APPROVED":
                require(sorted(request["acknowledged_affected_ordinals"]) == [v["ordinal"] for v in impact["affected_volumes"]],
                        "CONTINUITY_IMPACT_UNREVIEWED", "Every affected volume and downstream continuation needs review")
            review = {"request": request, "candidate_sha256": request["candidate_ref"]["sha256"],
                "world_ref": wr, "impact_sha256": fingerprint(impact), "principal_id": principal.principal_id,
                "grant_id": principal.grant_id, "grant_epoch": grant["epoch"], "recorded_at": repo.now().isoformat(),
                "attestation": "AUTHENTICATED_REVIEW_DECISION_NOT_VERIFIED_LITERARY_QUALITY"}
            ref = self.kernel._blob(review)
            self.kernel._register_artifact(repo, ref, work_id, review, "CONTINUITY_REVIEW", principal.principal_id)
            self.kernel._receipt(repo, principal, wr, "CONTINUITY_REVIEW", request["idempotency_key"], request, ref)
            if request["decision"] == "APPROVED":
                from novel_factory.application.volume_settlement import publish_after_review
                publish_after_review(self.kernel, repo, work_id, request["candidate_ref"], ref)
        return ref

    def publish(self, principal, work_id, payload, *, fault=None):
        request = validate_contract("ContinuityPublishRequest", payload, "continuity")
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": work_id}, lock="update")
            self._work_access(repo, principal, work_id, "APPROVE")
            candidate = self._candidate(repo, request["candidate_ref"], work_id)
            body, wr = candidate["request"], candidate["request"]["world_ref"]
            self._authorize(repo, principal, wr, "APPROVE")
            # Work was exclusively locked before authorization takes shared
            # guards; never upgrade a shared work lock in competing writers.
            repo.one("branches", {"branch_id": wr["branch_id"]}, lock="update")
            prior_receipt = self.kernel._prior(repo, principal, work_id, "CONTINUITY_PUBLISH", request["idempotency_key"], request)
            if prior_receipt:
                return prior_receipt
            _, releases, impact = self._validate(repo, principal, body)
            require(impact == candidate["impact"], "CONTINUITY_REVALIDATION_REQUIRED", "Candidate impact changed")
            review = _payload(repo, self.kernel, request["review_ref"], work_id, "CONTINUITY_REVIEW")
            require(review["request"]["candidate_ref"] == request["candidate_ref"]
                    and review["request"]["decision"] == "APPROVED"
                    and review["candidate_sha256"] == request["candidate_ref"]["sha256"]
                    and review["impact_sha256"] == fingerprint(impact)
                    and review["principal_id"] != candidate["created_by"],
                    "APPROVAL_REQUIRED", "A distinct reviewer must approve this exact candidate and impact")
            from novel_factory.application.authorization import Principal
            reviewer = Principal(review["grant_id"], review["principal_id"])
            reviewer_grant, _ = self._authorize(repo, reviewer, wr, "APPROVE", current=True)
            require(reviewer_grant["epoch"] == review["grant_epoch"], "APPROVAL_REVOKED",
                    "Reviewer authorization changed after approval")
            key = {"work_id": work_id, "selection_kind": "CONTINUITY:" + body["series_id"]}
            pointer = repo.one("selections", key, lock="update")
            epoch = pointer["epoch"] + 1 if pointer else 1
            revision = self.kernel.revision(repo, wr)
            historical_releases = {release["release_ref"]["artifact_revision_id"] for release in releases
                                   if not repo.ancestor(release["manifest"]["world_ref"], wr)}
            statuses = {entry["entry_id"]: ("HISTORICAL_SOURCE_ONLY" if any(
                revision["selection"].get(e["event_ref"]["occurrence_id"]) != e["event_ref"]["event_version_id"]
                for e in entry["source_events"]) or any(
                e["release_ref"]["artifact_revision_id"] in historical_releases
                or e["release_ref"] not in body["ordered_release_refs"] for e in entry["source_texts"])
                else "CURRENT_SOURCE_BOUND") for entry in body["entries"]}
            snapshot = {"schema_version": "2.3", "request": body, "candidate_ref": request["candidate_ref"],
                "review_ref": request["review_ref"], "impact": impact, "selection_epoch": epoch,
                "volumes": [{"ordinal": index, "volume_id": release["record"]["volume_id"],
                    "release_ref": release["release_ref"], "world_ref": release["manifest"]["world_ref"],
                    "manifest_sha256": release["manifest"]["manifest_sha256"]}
                    for index, release in enumerate(releases, 1)],
                "active_thread_entries": validate_entries(body["entries"], len(releases)),
                "entry_evidence_status": statuses,
                "historical_release_ordinals": [index for index, release in enumerate(releases, 1)
                    if release["release_ref"]["artifact_revision_id"] in historical_releases],
                "authority": "DERIVED_CONTINUITY_NOT_WORLD_HISTORY", "world_effects_applied": 0,
                "recorded_at": repo.now().isoformat()}
            ref = self.kernel._blob(snapshot)
            self.kernel._register_artifact(repo, ref, work_id, snapshot, "CONTINUITY_SNAPSHOT", principal.principal_id)
            if pointer:
                repo.change("selections", key, {"artifact_ref": ref, "epoch": epoch})
            else:
                repo.add("selections", {**key, "artifact_ref": ref, "epoch": epoch})
            for release in releases:
                protection = {"work_id": work_id, "selection_kind": "SERIAL_PREFIX:" + release["record"]["volume_id"]}
                if repo.one("selections", protection) is None:
                    repo.add("selections", {**protection, "artifact_ref": ref, "epoch": 1})
            scope = "ARTIFACT_SELECTION:" + key["selection_kind"]
            known = repo.scope_map(work_id, wr["branch_id"])
            scopes = [scope] + ([] if scope in known else ["SCOPE_REGISTRY:world"])
            versions = repo.bump(work_id, wr["branch_id"], scopes)
            for changed in scopes:
                repo.add("invalidations", {"invalidation_id": str(uuid4()), **wr,
                    "scope_key": changed, "version": versions[changed]})
            result = {"snapshot_ref": ref, "selection_epoch": epoch, "world_ref": wr,
                      "world_effects_applied": 0, "external_delivery": "NOT_REQUESTED"}
            self.kernel._receipt(repo, principal, wr, "CONTINUITY_PUBLISH", request["idempotency_key"], request, result)
            repo.outbox("continuity.published", "series", body["series_id"], result,
                        "continuity:" + ref["artifact_revision_id"])
            if fault:
                fault("before_commit")
        if fault:
            fault("after_commit")
        return result

    def inspect(self, principal, work_id, snapshot_ref, *, target_world_ref=None):
        validate_contract("ArtifactRef", snapshot_ref)
        validate_contract("UUID", work_id)
        if target_world_ref is not None:
            validate_contract("WorldRef", target_world_ref)
            require(target_world_ref["work_id"] == work_id, "VIEW_DENIED", "Target world belongs to another work")
        with self.kernel.objects.lock(), self.kernel.db.uow(write=False, repeatable_read=True) as repo:
            self._work_access(repo, principal, work_id)
            snapshot = self._snapshot(repo, snapshot_ref, work_id)
            grant, _ = self._authorize(repo, principal, snapshot["request"]["world_ref"])
            for entry in snapshot["request"]["entries"]:
                require(point_key(entry["story_interval"]["end"]) <= point_key(grant["max_story_time"])
                        and (entry["planned_payoff_at"] is None
                             or point_key(entry["planned_payoff_at"]) <= point_key(grant["max_story_time"])),
                        "VIEW_DENIED", "Continuity evidence exceeds the current grant horizon")
                for source in entry["source_events"]:
                    self._authorize(repo, principal, source["world_ref"])
            target = target_world_ref or self.kernel.head(repo, work_id)
            self._authorize(repo, principal, target)
            impact = self._impact(repo, principal, snapshot, target)
            return {"snapshot_ref": snapshot_ref, "snapshot": deepcopy(snapshot), "current_impact": impact,
                    "qualification": "HISTORICAL_EDITION_REVIEW_REQUIRED" if impact["status"] != "UNCHANGED" else "NO_NEW_IMPACT_SINCE_REVIEW",
                    "world_effects_applied": 0}

    def _continuity_task(self, repo, principal, task_id, generation, snapshot_ref):
        from novel_factory.application.narrative import NarrativeService
        task, grant, spec, ctx = NarrativeService(self.kernel)._task(
            repo, principal, task_id, generation, allow_accepted=True)
        reader = grant["role_id"] in {"writer", "reviewer", "literary_editor", "chief_editor",
                                      "evidence_maintainer", "control_operator"}
        planner = grant["role_id"] == "volume_director" and spec["task_kind"] in {
            "volume_contract", "window_plan", "replan"}
        require(ctx["principal_id"] == principal.principal_id and ctx["read_cut"]["world_ref"] == spec["world_ref"]
                and ctx["payload"]["policy_version"] == ctx["read_cut"]["policy_version"] == spec["policy_version"],
                "TASK_REBIND_REQUIRED", "Continuity needs the original authorized context cut")
        cut = ctx["read_cut"]["knowledge_cut"]
        if planner and cut["plane"] == "PLAN":
            from novel_factory.application.volume_successor import check_planner_cut
            check_planner_cut(self.kernel, repo, task, spec, cut, snapshot_ref)
        else:
            require((reader or planner) and cut == {"plane": "WORLD"}, "VIEW_DENIED",
                    "Continuity requires an assigned reader or planning task with a fixed cut")
        check_profile(grant, cut["plane"])
        return task, grant, spec

    def read_for_task(self, principal, task_id, generation, snapshot_ref, *, budget_bytes=100_000):
        """Deliver source-bound continuity only to the exact assigned task cut.

        This is a writer/evidence task input, not an actor's cognitive memory.
        The complete snapshot must be an immutable task input; future planning
        fields are removed from this WORLD-only route. It returns no partial
        authoritative-looking result when time, budget or Retcon checks fail.
        """
        validate_contract("ArtifactRef", snapshot_ref)
        require(type(budget_bytes) is int and 0 < budget_bytes <= 2_000_000,
                "INVALID_ARGUMENT", "Invalid continuity context byte budget")
        with self.kernel.objects.lock(), self.kernel.db.uow(write=False, repeatable_read=True) as repo:
            task, grant, spec = self._continuity_task(repo, principal, task_id, generation, snapshot_ref)
            require(grant["actor_id"] is None and snapshot_ref in spec["input_artifacts"],
                    "VIEW_DENIED", "Continuity is not assigned to this non-actor task generation")
            snapshot = self._snapshot(repo, snapshot_ref, task["work_id"])
            wr = snapshot["request"]["world_ref"]
            require(wr == spec["world_ref"], "TASK_REBIND_REQUIRED", "Continuity and task must pin the same world cut")
            head = self.kernel.head(repo, task["work_id"])
            require(repo.ancestor(wr, head), "CONTINUITY_REVALIDATION_REQUIRED",
                    "Canonical history changed after this continuity input was reviewed")
            fixed, current = self.kernel.revision(repo, wr), self.kernel.revision(repo, head)
            require(fixed["interpretation_ref"] == current["interpretation_ref"],
                    "CONTINUITY_REVALIDATION_REQUIRED", "World interpretation changed")
            delivered = []
            active_ids = set(snapshot["active_thread_entries"].values())
            for entry in snapshot["request"]["entries"]:
                if entry["entry_id"] not in active_ids:
                    continue
                require(snapshot["entry_evidence_status"][entry["entry_id"]] == "CURRENT_SOURCE_BOUND",
                        "CONTINUITY_REVALIDATION_REQUIRED", "Historical-only memory requires a new task-specific derivation")
                require(point_key(entry["story_interval"]["end"]) <= point_key(spec["story_time"]),
                        "VIEW_DENIED", "Continuity entry exceeds this task's story-time cut")
                for source in entry["source_events"]:
                    self.kernel.authorize(repo, principal, task["work_id"], "READ", source["world_ref"]["branch_id"])
                    event = source["event_ref"]
                    require(current["selection"].get(event["occurrence_id"]) == event["event_version_id"],
                            "CONTINUITY_REVALIDATION_REQUIRED", "A source occurrence has changed")
                    stored = repo.one("events", {"work_id": task["work_id"], "event_version_id": event["event_version_id"]})
                    occurrence = stored["payload"]["occurrence_time"]
                    endpoint = occurrence.get("at") if occurrence["kind"] == "EXACT" else occurrence.get("latest")
                    require(endpoint is not None and point_key(endpoint) <= point_key(spec["story_time"]),
                            "VIEW_DENIED", "Source event exceeds the task's known story boundary")
                for source in entry["source_texts"]:
                    record = _payload(repo, self.kernel, source["release_ref"], task["work_id"], "RELEASE_RECORD")
                    manifest = _payload(repo, self.kernel, record["manifest_ref"], task["work_id"], "RELEASE_MANIFEST")
                    self.kernel.authorize(repo, principal, task["work_id"], "READ", manifest["world_ref"]["branch_id"])
                    for item, scene_ref in zip(manifest["ordered_artifacts"], record["ordered_scene_refs"], strict=True):
                        if item["artifact_ref"] == source["text_ref"]:
                            scene = _payload(repo, self.kernel, scene_ref, task["work_id"], "SCENE_PLAN")
                            require(point_key(scene["scene_contract"]["story_interval"]["end"]) <= point_key(spec["story_time"]),
                                    "VIEW_DENIED", "Source prose exceeds the task's story-time cut")
                delivered.append({key: deepcopy(value) for key, value in entry.items() if key != "planned_payoff_at"})
            result = {"snapshot_ref": snapshot_ref, "world_ref": wr, "entries": delivered,
                "authority": "DERIVED_EDITORIAL_MEMORY_NOT_WORLD_FACT_OR_ACTOR_BELIEF", "world_effects_applied": 0}
            require(len(canonical_bytes(result)) <= budget_bytes, "CONTEXT_BUDGET_EXCEEDED",
                    "The complete assigned continuity evidence exceeds this context budget")
            self._continuity_task(repo, principal, task_id, generation, snapshot_ref)
        return result
