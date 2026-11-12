"""Reviewed, semantically compatible runtime releases on the existing timeline.

Only packaging/version metadata may change in this finite first upgrade path.
Changing a reducer, calendar, predicate meaning or a world-rule value requires
a separate migration/repair proof and is rejected here. Old events, revisions,
contexts and model inputs remain immutable and readable by their original ref.
"""
from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import require
from novel_factory.application.query_policy import check_profile
from novel_factory.application.simulation import inherit_schedule_state
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.reducer import replay
from novel_factory.domain.temporal import point_key


def _without(value, key):
    return {k: v for k, v in value.items() if k != key}


class RuntimeUpgradeService:
    def __init__(self, kernel, *, adapters=None):
        self.kernel, self.adapters = kernel, dict(adapters or {})

    def _payload(self, repo, ref, work_id, kind):
        """A registered kind alone is not durable, content-bound evidence."""
        row = self.kernel.artifact(repo, ref, work_id, kind=kind)
        self.kernel.objects.verify(ref["sha256"])
        require(row["payload"] is not None and fingerprint(row["payload"]) == ref["sha256"],
                "OBJECT_CORRUPT", "Runtime artifact payload differs from its immutable object")
        return row["payload"]

    def _canonical(self, repo, world_ref, *, current):
        work = repo.one("works", {"work_id": world_ref["work_id"]})
        require(work and work["canon_branch_id"] == world_ref["branch_id"],
                "VIEW_DENIED", "Global runtime configuration requires the canonical branch")
        if current:
            require(self.kernel.head(repo, world_ref["work_id"]) == world_ref,
                    "HEAD_CONFLICT", "Global runtime configuration requires the current canonical HEAD")

    def _authorize(self, repo, principal, world_ref, capability="ADMIN"):
        grant = self.kernel.authorize(repo, principal, world_ref["work_id"], capability, world_ref["branch_id"])
        check_profile(grant, "WORLD")
        require(grant["actor_id"] is None, "VIEW_DENIED", "Runtime changes require a world-control identity")
        revision = self.kernel.revision(repo, world_ref)
        require(all(point_key(at) <= point_key(grant["max_story_time"]) for at in revision["frontiers"].values()),
                "VIEW_DENIED", "Runtime release exceeds the authorized story horizon")
        return grant

    def prepare(self, principal, request):
        request = validate_contract("RuntimeUpgradePrepare", request, "upgrades")
        kernel, wr = self.kernel, request["world_ref"]
        with kernel.objects.lock(), kernel.db.uow(write=False) as repo:
            grant = self._authorize(repo, principal, wr)
            prior = kernel._prior(repo, principal, wr["work_id"], "RUNTIME_UPGRADE_PREPARE", request["idempotency_key"], request)
            if prior:
                return prior
            work = repo.one("works", {"work_id": wr["work_id"]})
            require(work["canon_branch_id"] == wr["branch_id"] and kernel.head(repo, wr["work_id"]) == wr,
                    "HEAD_CONFLICT", "Upgrade must be prepared at the current canonical HEAD")
            current = kernel.revision(repo, wr)
            old_rules = self._payload(repo, work["rules_ref"], wr["work_id"], "RULESET")
            old_interpretation = self._payload(repo, work["interpretation_ref"], wr["work_id"], "INTERPRETATION")
            require(_without(request["rules"], "version") == _without(old_rules, "version"),
                    "UPGRADE_REQUIRES_REPAIR", "Rule meaning changes need a separately reviewed migration")
            require(_without(request["interpretation"], "package_release") == _without(old_interpretation, "package_release"),
                    "UPGRADE_REQUIRES_REPAIR", "Interpreter or temporal meaning changes are not proven equivalent")
            require(request["rules"] != old_rules or request["interpretation"] != old_interpretation,
                    "INVALID_ARGUMENT", "Runtime release is unchanged")
            events = [repo.one("events", {"work_id": wr["work_id"], "event_version_id": eid})["payload"]
                      for eid in current["selection"].values()]
            genesis = repo.one("genesis", {"work_id": wr["work_id"]})["payload"]
            # JSONB maps do not preserve insertion order. The original P1
            # bootstrap appended initial lifecycle rows in entity-map order;
            # retain that recorded order when verifying its exact old hash.
            # No old projection or hash is rewritten or normalized away.
            initial_order = [entry["entity_id"] for entry in current["snapshot"]["lifecycle_history"]
                             if entry["event_ref"] is None]
            require(len(initial_order) == len(set(initial_order)) == len(genesis["entities"])
                    and set(initial_order) == set(genesis["entities"]),
                    "PROJECTION_CORRUPT", "Initial lifecycle identities are incomplete")
            genesis = {**genesis, "entities": {key: genesis["entities"][key] for key in initial_order}}
        # Potentially expensive replay is outside the activation transaction.
        require(fingerprint(replay(genesis, events)) == current["state_hash"],
                "PROJECTION_CORRUPT", "Selected history does not reproduce the existing projection")
        package = {"kind": "COMPATIBLE_RUNTIME_PACKAGE", "base_world_ref": wr,
                   "source_rules_ref": work["rules_ref"], "source_interpretation_ref": work["interpretation_ref"],
                   "source_interpretation_epoch": work["interpretation_epoch"],
                   "rules": request["rules"], "interpretation": request["interpretation"],
                   "state_sha256": current["state_hash"], "manifest_sha256": current["manifest_hash"],
                   "selected_event_count": len(events), "created_by": principal.principal_id,
                   "grant_id": principal.grant_id, "grant_epoch": grant["epoch"],
                   "compatibility": "IDENTICAL_SEMANTICS_METADATA_ONLY"}
        package_ref, rules_ref, interpretation_ref = [kernel._blob(p) for p in
                                                     (package, request["rules"], request["interpretation"])]
        result = {"package_ref": package_ref, "rules_ref": rules_ref, "interpretation_ref": interpretation_ref}
        with kernel.objects.lock(), kernel.db.uow() as repo:
            # Serialize only administrative registration. Acquiring the work
            # update lock before authorize avoids a shared-to-update upgrade
            # and lets concurrent same-key requests observe the one receipt.
            repo.one("works", {"work_id": wr["work_id"]}, lock="update")
            self._authorize(repo, principal, wr)
            prior = kernel._prior(repo, principal, wr["work_id"], "RUNTIME_UPGRADE_PREPARE", request["idempotency_key"], request)
            if prior:
                return prior
            require(kernel.head(repo, wr["work_id"]) == wr, "HEAD_CONFLICT", "World changed while preparing upgrade")
            for ref, payload, kind in [(package_ref, package, "RUNTIME_UPGRADE_PACKAGE"),
                                       (rules_ref, request["rules"], "RULESET"),
                                       (interpretation_ref, request["interpretation"], "INTERPRETATION")]:
                kernel._register_artifact(repo, ref, wr["work_id"], payload, kind, principal.principal_id)
            kernel._receipt(repo, principal, wr, "RUNTIME_UPGRADE_PREPARE", request["idempotency_key"], request, result)
        return result

    def review(self, principal, request):
        request = validate_contract("RuntimeUpgradeReview", request, "upgrades")
        kernel, wr = self.kernel, request["world_ref"]
        with kernel.objects.lock(), kernel.db.uow() as repo:
            repo.one("works", {"work_id": wr["work_id"]}, lock="update")
            grant = self._authorize(repo, principal, wr, "APPROVE")
            prior = kernel._prior(repo, principal, wr["work_id"], "RUNTIME_UPGRADE_REVIEW", request["idempotency_key"], request)
            if prior:
                return prior
            package = self._payload(repo, request["package_ref"], wr["work_id"], "RUNTIME_UPGRADE_PACKAGE")
            require(package["base_world_ref"] == wr and package["created_by"] != principal.principal_id,
                    "INDEPENDENT_REVIEW_REQUIRED", "Review must bind the exact package and a different principal")
            approval = {"package_ref": request["package_ref"], "world_ref": wr, "grant_id": grant["grant_id"],
                        "grant_epoch": grant["epoch"], "principal_id": principal.principal_id,
                        "decision": "APPROVE", "notes": request["notes"]}
            ref = kernel._blob(approval)
            kernel._register_artifact(repo, ref, wr["work_id"], approval, "RUNTIME_UPGRADE_APPROVAL", principal.principal_id)
            kernel._receipt(repo, principal, wr, "RUNTIME_UPGRADE_REVIEW", request["idempotency_key"], request, ref)
            return ref

    def activate(self, principal, request):
        request = validate_contract("RuntimeUpgradeActivate", request, "upgrades")
        kernel, wr = self.kernel, request["world_ref"]
        with kernel.objects.lock(), kernel.db.uow() as repo:
            # Every authorized world operation holds a shared work lock. Take
            # its update lock first, like canon promotion; never upgrade a
            # shared lock or serialize ordinary world commits on a new lock.
            work = repo.one("works", {"work_id": wr["work_id"]}, lock="update")
            self._authorize(repo, principal, wr)
            prior = kernel._prior(repo, principal, wr["work_id"], "RUNTIME_UPGRADE", request["idempotency_key"], request)
            if prior:
                return prior
            package = self._payload(repo, request["package_ref"], wr["work_id"], "RUNTIME_UPGRADE_PACKAGE")
            approval = self._payload(repo, request["approval_ref"], wr["work_id"], "RUNTIME_UPGRADE_APPROVAL")
            require(package["base_world_ref"] == wr and approval["world_ref"] == wr
                    and approval["package_ref"] == request["package_ref"]
                    and approval["principal_id"] != package["created_by"],
                    "APPROVAL_STALE", "Upgrade approval does not bind this release")
            creator = self._authorize(repo, Principal(package["grant_id"], package["created_by"]), wr)
            reviewer = self._authorize(repo, Principal(approval["grant_id"], approval["principal_id"]), wr, "APPROVE")
            require(creator["epoch"] == package["grant_epoch"] and reviewer["epoch"] == approval["grant_epoch"],
                    "APPROVAL_STALE", "Upgrade principals have changed qualification")
            require(work["canon_branch_id"] == wr["branch_id"] and kernel.head(repo, wr["work_id"]) == wr,
                    "HEAD_CONFLICT", "Canonical HEAD moved after upgrade review")
            require(work["interpretation_epoch"] == package["source_interpretation_epoch"]
                    and work["rules_ref"] == package["source_rules_ref"]
                    and work["interpretation_ref"] == package["source_interpretation_ref"],
                    "HEAD_CONFLICT", "Runtime source changed after review")
            # The reviewed source is still part of the new revision's audit
            # chain; do not activate through missing or corrupt old objects.
            self._payload(repo, package["source_rules_ref"], wr["work_id"], "RULESET")
            self._payload(repo, package["source_interpretation_ref"], wr["work_id"], "INTERPRETATION")
            current = kernel.revision(repo, wr)
            require(current["state_hash"] == package["state_sha256"] and current["manifest_hash"] == package["manifest_sha256"],
                    "PROJECTION_CORRUPT", "Upgrade source integrity differs")
            branch = repo.one("branches", {"branch_id": wr["branch_id"]}, lock="update")
            rules_ref, interpretation_ref = kernel._blob(package["rules"]), kernel._blob(package["interpretation"])
            for ref, payload, kind in [(rules_ref, package["rules"], "RULESET"),
                                       (interpretation_ref, package["interpretation"], "INTERPRETATION")]:
                kernel._register_artifact(repo, ref, wr["work_id"], payload, kind, principal.principal_id)
            changed = set(current["scope_versions"]) | {"INTERPRETATION:world", "RULESET:world", "SCOPE_REGISTRY:world"}
            scopes = repo.bump(wr["work_id"], wr["branch_id"], changed)
            revision = {k: deepcopy(current[k]) for k in ("work_id", "branch_id", "selection", "frontiers", "snapshot", "manifest_hash", "state_hash")}
            revision.update(world_revision_id=str(uuid4()), parent_revision_id=wr["world_revision_id"],
                            branch_seq=current["branch_seq"] + 1, scope_versions=scopes,
                            interpretation_ref=interpretation_ref,
                            provenance={"kind": "REVIEWED_RUNTIME_UPGRADE", "package_ref": request["package_ref"],
                                        "approval_ref": request["approval_ref"], "original_base": wr,
                                        "history_epoch": branch["history_epoch"]})
            inherit_schedule_state(current, revision)
            repo.add("revisions", revision)
            from .semantic_dependencies import record_change
            record_change(repo, revision)
            repo.add_many("revision_events", [{**{k: row[k] for k in ("occurrence_id", "work_id", "branch_id", "event_version_id", "effect_group_id")},
                                                "world_revision_id": revision["world_revision_id"]}
                                               for row in repo.many("revision_events", {"world_revision_id": wr["world_revision_id"]})])
            repo.add("commits", {"world_revision_id": revision["world_revision_id"], "operations": [],
                                 "request": request, "request_hash": fingerprint(request)})
            repo.add_many("invalidations", [{"invalidation_id": str(uuid4()), "work_id": wr["work_id"],
                            "branch_id": wr["branch_id"], "world_revision_id": revision["world_revision_id"],
                            "scope_key": key, "version": scopes[key]} for key in sorted(changed)])
            require(repo.change("branches", {"branch_id": wr["branch_id"], "head_revision_id": wr["world_revision_id"]},
                                {"head_revision_id": revision["world_revision_id"]}) == 1, "HEAD_CONFLICT", "Upgrade HEAD compare-and-swap failed")
            repo.change("works", {"work_id": wr["work_id"]}, {"rules_ref": rules_ref,
                        "interpretation_ref": interpretation_ref, "interpretation_epoch": work["interpretation_epoch"] + 1})
            new_world = {k: revision[k] for k in ("work_id", "branch_id", "world_revision_id")}
            receipt = {"old_world_ref": wr, "world_ref": new_world, "package_ref": request["package_ref"],
                       "approval_ref": request["approval_ref"], "new_world_effects": 0,
                       "interpretation_epoch": work["interpretation_epoch"] + 1}
            kernel._receipt(repo, principal, wr, "RUNTIME_UPGRADE", request["idempotency_key"], request, receipt)
            repo.outbox("world.accepted", "revision", revision["world_revision_id"],
                        {"world_ref": new_world, "scopes": sorted(changed)}, "world:" + revision["world_revision_id"])
            return receipt

    def select_model(self, principal, request):
        request = validate_contract("ModelRouteSelect", request, "upgrades")
        kernel, wr = self.kernel, request["world_ref"]
        with kernel.db.uow(write=False) as repo:
            self._authorize(repo, principal, wr)
            prior = kernel._prior(repo, principal, wr["work_id"], "MODEL_ROUTE_SELECT", request["idempotency_key"], request)
            if prior:
                return prior
            self._canonical(repo, wr, current=True)
        require(request["adapter_id"] in self.adapters, "MODEL_UNAVAILABLE", "Server model route is unavailable")
        # Only the adapter's declared safe configuration fingerprint is stored;
        # no endpoint credentials or arbitrary caller configuration are accepted.
        config_hash = fingerprint(self.adapters[request["adapter_id"]].configuration)
        payload = {"world_ref": wr, "adapter_id": request["adapter_id"], "configuration_sha256": config_hash,
                   "prior_ref": request["expected_ref"], "existing_tasks": "RETAIN_FROZEN_MODEL_CONFIGURATION"}
        ref = kernel._blob(payload)
        with kernel.objects.lock(), kernel.db.uow() as repo:
            repo.one("works", {"work_id": wr["work_id"]}, lock="update")
            self._authorize(repo, principal, wr)
            prior = kernel._prior(repo, principal, wr["work_id"], "MODEL_ROUTE_SELECT", request["idempotency_key"], request)
            if prior:
                return prior
            self._canonical(repo, wr, current=True)
            current = repo.one("selections", {"work_id": wr["work_id"], "selection_kind": "MODEL_ROUTE"}, lock="update")
            require((current["artifact_ref"] if current else None) == request["expected_ref"],
                    "HEAD_CONFLICT", "Selected model configuration changed")
            # A route ID cannot silently acquire a new configuration while old
            # tasks refer to it. Operators must register a distinct adapter ID.
            previous_routes = repo.many("artifacts", {"work_id": wr["work_id"], "kind": "MODEL_ROUTE_SELECTION"})
            previous_payloads = [self._payload(repo,
                {key: row[key] for key in ("artifact_id", "artifact_revision_id", "sha256")},
                wr["work_id"], "MODEL_ROUTE_SELECTION") for row in previous_routes]
            require(all(old["adapter_id"] != request["adapter_id"]
                        or old["configuration_sha256"] == config_hash for old in previous_payloads),
                    "MODEL_CONFIG_CHANGED", "Register a new immutable model route ID for changed configuration")
            kernel._register_artifact(repo, ref, wr["work_id"], payload, "MODEL_ROUTE_SELECTION", principal.principal_id)
            values = {"artifact_ref": ref, "epoch": current["epoch"] + 1 if current else 1}
            if current:
                repo.change("selections", {"work_id": wr["work_id"], "selection_kind": "MODEL_ROUTE"}, values)
            else:
                repo.add("selections", {"work_id": wr["work_id"], "selection_kind": "MODEL_ROUTE", **values})
            kernel._receipt(repo, principal, wr, "MODEL_ROUTE_SELECT", request["idempotency_key"], request, ref)
            return ref

    def selected_model(self, principal, world_ref):
        with self.kernel.objects.lock(), self.kernel.db.uow(write=False) as repo:
            self._authorize(repo, principal, world_ref)
            self._canonical(repo, world_ref, current=False)
            selection = repo.one("selections", {"work_id": world_ref["work_id"], "selection_kind": "MODEL_ROUTE"})
            require(selection, "MODEL_UNAVAILABLE", "No reviewed model route is selected")
            payload = self._payload(repo, selection["artifact_ref"], world_ref["work_id"], "MODEL_ROUTE_SELECTION")
            adapter = self.adapters.get(payload["adapter_id"])
            require(adapter and fingerprint(adapter.configuration) == payload["configuration_sha256"],
                    "MODEL_CONFIG_CHANGED", "Installed model route differs from the selected configuration")
            return payload["adapter_id"]
