"""Reviewed macro/micro representation replacement on the existing world chain.

Proof creation never edits a world. The only write command is the existing
strict REPAIR_EVENT protocol on an isolated repair branch, including a distinct
reviewer, task-generation fencing, scope invalidation and canon promotion CAS.
"""
from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from novel_factory.application.kernel import require, world_ref
from novel_factory.application.query_policy import check_profile
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.refinement import PROOF_VERSION, prove_refinement
from novel_factory.domain.scheduling import state_from_revision
from novel_factory.domain.temporal import point_key


def _authorized(kernel, repo, principal, wr):
    grant = kernel.authorize(repo, principal, wr["work_id"], "REPAIR", wr["branch_id"])
    check_profile(grant, "WORLD")
    require(grant["actor_id"] is None, "VIEW_DENIED", "Refinement requires a world-control identity")
    revision = kernel.revision(repo, wr)
    require(all(point_key(at) <= point_key(grant["max_story_time"])
                for at in revision["frontiers"].values()),
            "VIEW_DENIED", "Refinement exceeds the grant's story horizon")
    return revision


def validate_refinement_command(kernel, repo, principal, revision, args):
    """Acceptance hook: trusted proof bytes must bind this exact immutable base.

    The expensive deterministic proof was issued outside the write transaction.
    Immutable object/hash and source material bindings are rechecked in proposal,
    certificate and final acceptance transactions. Client-writable TEXT/EVIDENCE
    artifacts cannot impersonate this server-only artifact kind.
    """
    proof_ref = args["equivalence_proof_ref"]
    artifact = kernel.artifact(repo, proof_ref, revision["work_id"], kind="REFINEMENT_PROOF")
    kernel.objects.verify(proof_ref["sha256"])
    proof = artifact["payload"]
    require(proof and fingerprint(proof) == proof_ref["sha256"]
            and proof["proof"]["proof_version"] == PROOF_VERSION
            and proof["proof"]["status"] == "PASS",
            "INVALID_EVIDENCE", "Refinement proof is unavailable or unsupported")
    source_wr = proof["request"]["source_world_ref"]
    source = _authorized(kernel, repo, principal, source_wr)
    _authorized(kernel, repo, principal, world_ref(revision))
    branch = repo.one("branches", {"work_id": revision["work_id"], "branch_id": revision["branch_id"]})
    require(branch and branch["kind"] == "REPAIR" and branch["fork_ref"] == source_wr,
            "VIEW_DENIED", "Refinement must use a repair branch forked from its proved source")
    require(revision["selection"] == source["selection"]
            and revision["frontiers"] == source["frontiers"]
            and revision["state_hash"] == source["state_hash"]
            and revision["interpretation_ref"] == source["interpretation_ref"]
            and state_from_revision(revision) == state_from_revision(source),
            "HEAD_CONFLICT", "The refinement's immutable source material no longer matches")
    require(proof["source_selection_sha256"] == fingerprint(source["selection"])
            and proof["source_state_sha256"] == source["state_hash"]
            and proof["proof"]["schedule_sha256"] == fingerprint(state_from_revision(source)),
            "INVALID_EVIDENCE", "Proof source material does not match its trusted revision")
    require(args["occurrence_id"] == proof["request"]["macro_event_ref"]["occurrence_id"]
            and args["replacement_event"] == proof["replacement_event"]
            and fingerprint(args["replacement_event"]) == proof["proof"]["replacement_event_sha256"],
            "INVALID_EVIDENCE", "Replacement differs from the proved event bytes")
    require(source["selection"].get(args["occurrence_id"])
            == proof["request"]["macro_event_ref"]["event_version_id"],
            "INVALID_EVIDENCE", "The source occurrence is not selected")
    return proof


class WorldRepresentationService:
    def __init__(self, kernel):
        self.kernel = kernel

    def prepare_refinement(self, principal, request):
        """Issue an immutable finite proof; formal replacement remains reviewed."""
        request = validate_contract("RefinementRequest", request, "refinement")
        kernel = self.kernel
        wr = request["source_world_ref"]
        key = request["idempotency_key"]
        with kernel.db.uow(write=False, repeatable_read=True) as repo:
            source = _authorized(kernel, repo, principal, wr)
            old = kernel._prior(repo, principal, wr["work_id"], "REFINEMENT_PROOF", key, request)
            if old:
                return old
            require(source["selection"].get(request["macro_event_ref"]["occurrence_id"])
                    == request["macro_event_ref"]["event_version_id"],
                    "INVALID_EVIDENCE", "Macro event is not selected at the requested source")
            genesis = repo.one("genesis", {"work_id": wr["work_id"]})["payload"]
            rows = repo.many("events", {"work_id": wr["work_id"],
                                        "event_version_id": list(source["selection"].values())})
            require(len(rows) == len(source["selection"]), "INVALID_EVIDENCE", "Selected history is incomplete")
            events = [row["payload"] for row in rows]
            dependencies = repo.many("dependencies", {"work_id": wr["work_id"], "branch_id": wr["branch_id"]})
            # Record every published reference for mandatory subsequent review.
            # No release or narrative selection is mutated by this service.
            releases = []
            for item in repo.many("artifacts", {"work_id": wr["work_id"], "kind": "RELEASE_MANIFEST"}):
                release_wr = item["payload"]["world_ref"]
                try:
                    grant = kernel.authorize(repo, principal, wr["work_id"], "READ", release_wr["branch_id"])
                except DomainError as error:
                    if error.code != "VIEW_DENIED":
                        raise
                    continue
                release_revision = kernel.revision(repo, release_wr)
                if all(point_key(at) <= point_key(grant["max_story_time"])
                       for at in release_revision["frontiers"].values()):
                    releases.append(item)
        replacement, result = prove_refinement(genesis, events, request["macro_event_ref"],
                                               request["steps"], schedule=state_from_revision(source))
        payload = {
            "schema_version": "2.3", "kind": "FINITE_REFINEMENT_PROOF", "request": request,
            "source_selection_sha256": fingerprint(source["selection"]),
            "source_state_sha256": source["state_hash"],
            "source_interpretation_ref": source["interpretation_ref"],
            "replacement_event": replacement, "proof": result,
            "impact": {
                "policy": "CONSERVATIVE_REVIEW_ALL_REGISTERED_DEPENDENCIES",
                "dependencies": sorted([
                    {field: row[field] for field in ("owner_type", "owner_id", "scope_key", "dependency_kind", "observed_version")}
                    for row in dependencies], key=fingerprint),
                "release_refs": [{field: row[field] for field in ("artifact_id", "artifact_revision_id", "sha256")}
                                 for row in releases],
                "release_visibility": "AUTHORIZED_BRANCH_AND_STORY_HORIZON_ONLY",
                "published_text_replaced": False,
                "schedule_evidence": "ORIGINAL_ACCEPTED_EVALUATION_PRESERVED",
            },
        }
        proof_ref = kernel._blob(payload)
        result = {"proof_ref": proof_ref, "replacement_event_ref": result["replacement_event_ref"],
                  "repair_command": {"template_id": "REPAIR_EVENT", "args": {
                      "occurrence_id": request["macro_event_ref"]["occurrence_id"],
                      "replacement_event": replacement,
                      "reason": "Finite registered-effect equivalence; independent review required",
                      "equivalence_proof_ref": proof_ref,
                  }}}
        with kernel.objects.lock(), kernel.db.uow() as repo:
            _authorized(kernel, repo, principal, wr)
            repo.one("branches", {"work_id": wr["work_id"], "branch_id": wr["branch_id"]}, lock="update")
            old = kernel._prior(repo, principal, wr["work_id"], "REFINEMENT_PROOF", key, request)
            if old:
                return old
            kernel._register_artifact(repo, proof_ref, wr["work_id"], payload, "REFINEMENT_PROOF", principal.principal_id)
            # A proof is a derived artifact, not a second authoritative history.
            for scope, version in source["scope_versions"].items():
                repo.add("dependencies", {"dependency_id": str(uuid4()), "work_id": wr["work_id"],
                    "branch_id": wr["branch_id"], "owner_type": "refinement_proof",
                    "owner_id": proof_ref["artifact_revision_id"], "scope_key": scope,
                    "observed_version": version, "dependency_kind": "COMPUTED"})
            kernel._receipt(repo, principal, wr, "REFINEMENT_PROOF", key, request, result)
            repo.outbox("refinement.proved", "artifact", proof_ref["artifact_revision_id"],
                        {"proof_ref": proof_ref, "source_world_ref": wr},
                        "refinement:" + proof_ref["artifact_revision_id"])
        return result

    def propose_refinement(self, principal, task_id, generation, proof_ref, idempotency_key):
        kernel = self.kernel
        with kernel.db.uow(write=False) as repo:
            task, _context = kernel._task_binding(repo, principal, task_id, generation)
            revision = _authorized(kernel, repo, principal, task["world_ref"])
            proof = kernel.artifact(repo, proof_ref, task["work_id"], kind="REFINEMENT_PROOF")["payload"]
            args = {"occurrence_id": proof["request"]["macro_event_ref"]["occurrence_id"],
                    "replacement_event": deepcopy(proof["replacement_event"]),
                    "reason": "Finite registered-effect equivalence; independent review required",
                    "equivalence_proof_ref": proof_ref}
            validate_refinement_command(kernel, repo, principal, revision, args)
        return kernel.propose(principal, task_id, generation, "REPAIR_EVENT", args, idempotency_key)
