"""Scoped NF runtime profiles, Workshop skill sources and immutable task selection."""

from __future__ import annotations

from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from novel_factory.application.authorization import Principal
from novel_factory.application.creative_context import runtime_provenance
from novel_factory.application.creative_review import _quota
from novel_factory.application.kernel import require
from novel_factory.application.semantic_dependencies import check_task_dependencies
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig


def _id(label, *values):
    return str(uuid5(NAMESPACE_URL, "nf.dynamic." + label + "." + fingerprint(list(values))))


def _ref(row):
    return {k: row[k] for k in ("artifact_id", "artifact_revision_id", "sha256")}


def _same_branch(wr, other):
    return all(wr[k] == other[k] for k in ("work_id", "branch_id"))


def _key(key):
    require(
        isinstance(key, str) and 1 <= len(key.strip()) <= 200,
        "INVALID_ARGUMENT",
        "A bounded idempotency key is required",
    )


class DynamicAgentService:
    def __init__(self, kernel, *, config=None, authority=None):
        self.kernel = kernel
        self.config = config
        self.authority = authority

    def _enabled(self):
        config = self.config if self.config is not None else CreativeHarnessConfig.from_env()
        require(
            config.enabled and config.dynamic_agents, "DYNAMIC_AGENTS_DISABLED", "Dynamic agents are disabled"
        )

    def _authority(self):
        if self.authority is None:
            from .workshop_skills import DynamicSkillAuthority

            return DynamicSkillAuthority(self.kernel)
        return self.authority

    def _record(self, repo, wr, ref, kind):
        row = self.kernel.artifact(repo, ref, wr["work_id"], kind="CREATIVE_DYNAMIC_" + kind)
        data = row["payload"]
        self.kernel.objects.verify(ref["sha256"])
        require(
            fingerprint(data) == ref["sha256"] and _same_branch(wr, data["scope"]["world_ref"]),
            "VIEW_DENIED",
            "Dynamic record is outside its authorized branch",
        )
        return data

    def _put(self, repo, wr, owner, kind, data, *, identifier=None):
        ref = self.kernel._blob(data)
        if identifier is not None:
            ref["artifact_revision_id"] = identifier
        self.kernel._register_artifact(
            repo, ref, wr["work_id"], data, "CREATIVE_DYNAMIC_" + kind, owner.principal_id
        )
        return ref

    @staticmethod
    def _pointer(repo, work, key):
        return repo.one("selections", {"work_id": work, "selection_kind": "DYNAMIC_AGENT:" + key})

    @staticmethod
    def _advance(repo, work, key, ref, epoch):
        where = {"work_id": work, "selection_kind": "DYNAMIC_AGENT:" + key}
        if epoch == 1:
            repo.add("selections", {**where, "artifact_ref": ref, "epoch": epoch})
        else:
            repo.change("selections", where, {"artifact_ref": ref, "epoch": epoch})

    def _control(self, repo, controller, wr, *, mutate=False):
        validate_contract("WorldRef", wr)
        if mutate:
            repo.one("works", {"work_id": wr["work_id"]}, lock="update")
        self.kernel.authorize(repo, controller, wr["work_id"], "ADMIN", wr["branch_id"])
        require(repo.one("revisions", wr) is not None, "VIEW_DENIED", "World revision unavailable")

    def _source(self, repo, controller, task_id, generation, *, mutate=False):
        task = repo.one("tasks", {"task_id": task_id})
        require(
            task and task["generation"] == generation,
            "VIEW_DENIED",
            "Dynamic source assignment is unavailable",
        )
        spec = repo.one("task_inputs", {"task_id": task_id, "generation": generation})["spec"]
        self._control(repo, controller, spec["world_ref"], mutate=mutate)
        task = repo.one("tasks", {"task_id": task_id}, lock="share")
        require(
            task["generation"] == generation and task["state"] not in {"STALE", "FAILED", "CANCELLED"},
            "TASK_REBIND_REQUIRED",
            "Source assignment is no longer current",
        )
        grant = self.kernel.authorize(
            repo,
            Principal(task["grant_id"], task["principal_id"]),
            task["work_id"],
            "READ",
            task["branch_id"],
        )
        require(
            spec["capability_grant_id"] == grant["grant_id"]
            and task["role_id"] == spec["role_id"] == grant["role_id"],
            "VIEW_DENIED",
            "Source grant no longer matches this assignment",
        )
        check_task_dependencies(repo, task_id, generation)
        rows = repo.many(
            "artifacts",
            {
                "work_id": task["work_id"],
                "kind": "AGENT_INPUT",
                "artifact_revision_id": [r["artifact_revision_id"] for r in spec["input_artifacts"]],
            },
        )
        inputs = [
            r["payload"]
            for r in rows
            if (r["payload"]["role_id"], r["payload"]["task_kind"], r["payload"]["runtime_sha256"])
            == (task["role_id"], task["task_kind"], spec["runtime_contract_version"])
        ]
        require(len(inputs) == 1, "DYNAMIC_SOURCE_UNAVAILABLE", "A unique frozen runtime is required")
        source = self.kernel.artifact(repo, inputs[0]["runtime_ref"], task["work_id"], kind="AGENT_RUNTIME")
        runtime = source["payload"]
        require(
            fingerprint(runtime) == inputs[0]["runtime_sha256"] == source["sha256"],
            "DYNAMIC_SOURCE_UNAVAILABLE",
            "Runtime provenance has changed",
        )
        return task, spec, grant, runtime

    def _instance(self, repo, wr, owner, scope, identity):
        identifier = _id("instance", wr["work_id"], wr["branch_id"], identity)
        row = repo.one("artifacts", {"work_id": wr["work_id"], "artifact_revision_id": identifier})
        if row:
            ref = _ref(row)
            require(
                self._record(repo, wr, ref, "INSTANCE")["identity"] == identity,
                "DYNAMIC_VERSION_CONFLICT",
                "Instance scope differs",
            )
            return ref
        return self._put(
            repo, wr, owner, "INSTANCE", {"scope": scope, "identity": identity}, identifier=identifier
        )

    def _head(self, repo, wr, instance_ref):
        self._record(repo, wr, instance_ref, "INSTANCE")
        row = self._pointer(repo, wr["work_id"], instance_ref["artifact_revision_id"])
        if row:
            event = self._record(repo, wr, row["artifact_ref"], "EVENT")
            require(
                event["epoch"] == row["epoch"] and event["instance_ref"] == instance_ref,
                "DYNAMIC_VERSION_CONFLICT",
                "Selection pointer no longer matches its history",
            )
            return {**event, "event_ref": row["artifact_ref"]}
        return {
            "instance_ref": instance_ref,
            "epoch": 0,
            "state": "REGISTERED",
            "revision_ref": None,
            "event_ref": None,
        }

    def _versioned(self, repo, wr, owner, scope, kind, identifier, data):
        row = repo.one("artifacts", {"work_id": wr["work_id"], "artifact_revision_id": identifier})
        if row:
            ref = _ref(row)
            existing = self._record(repo, wr, ref, kind)
            require(
                all(existing[k] == v for k, v in data.items()),
                "DYNAMIC_VERSION_CONFLICT",
                "This version label already identifies different instructions",
            )
            return ref
        return self._put(repo, wr, owner, kind, {"scope": scope, **data}, identifier=identifier)

    def register_revision(
        self,
        controller,
        task_id,
        generation,
        *,
        workshop_binding,
        profile_version,
        role_instructions,
        skill_instructions,
        requested_tools,
        trigger,
        evidence_refs,
        quota,
        idempotency_key,
    ):
        self._enabled()
        _key(idempotency_key)
        validate_contract("WorkshopBinding", workshop_binding, "harness")
        validate_contract("Quota", quota, "harness")
        require(
            isinstance(profile_version, str)
            and 1 <= len(profile_version.strip()) <= 80
            and all(
                isinstance(v, str) and v.strip() and len(v.encode()) <= 200_000
                for v in (role_instructions, skill_instructions)
            )
            and isinstance(requested_tools, list)
            and len(requested_tools) <= 64
            and all(isinstance(v, str) and 0 < len(v) <= 100 for v in requested_tools)
            and len(set(requested_tools)) == len(requested_tools)
            and isinstance(evidence_refs, list)
            and 1 <= len(evidence_refs) <= 32
            and trigger in {"SKILL_GAP", "REVIEW_FINDING", "TASK_DEMAND"},
            "INVALID_ARGUMENT",
            "Invalid dynamic profile, skill or supporting evidence",
        )
        for ref in evidence_refs:
            validate_contract("ArtifactRef", ref)
        requested_tools = sorted(requested_tools)
        role_sha, skill_sha = (
            sha256(v.encode("utf-8")).hexdigest() for v in (role_instructions, skill_instructions)
        )
        require(
            skill_sha == workshop_binding["content_sha"],
            "FROZEN_SOURCE_MISMATCH",
            "Skill differs from its acquired source",
        )
        request = {
            "task_id": task_id,
            "generation": generation,
            "workshop_binding": workshop_binding,
            "profile_version": profile_version,
            "profile_sha256": role_sha,
            "skill_sha256": skill_sha,
            "requested_tools": requested_tools,
            "trigger": trigger,
            "evidence_refs": evidence_refs,
            "quota": quota,
        }
        authority, binding_id = self._authority(), workshop_binding["binding_id"]
        with self.kernel.db.uow(write=False) as repo:
            self._source(repo, controller, task_id, generation)
        live = authority.resolve(task_id, generation, binding_id, freeze=False)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            task, spec, grant, runtime = self._source(repo, controller, task_id, generation, mutate=True)
            wr = spec["world_ref"]
            checked = authority.check(repo, task_id, generation, binding_id, live=live)
            require(
                checked["binding"] == workshop_binding
                and checked["grant_id"] == task["grant_id"]
                and checked["permission_epoch"] == grant["epoch"]
                and checked["principal_id"] == task["principal_id"]
                and (checked["role_id"], checked["task_kind"]) == (task["role_id"], task["task_kind"]),
                "VIEW_DENIED",
                "Workshop and NF do not authorize the same source assignment",
            )
            require(
                set(requested_tools) <= set(runtime["mcp_requested_tools"]),
                "VIEW_DENIED",
                "Dynamic instructions cannot request tools outside the frozen role",
            )
            evidence = list(spec["input_artifacts"])
            if task["current_result_id"]:
                result = repo.one(
                    "task_results",
                    {"result_id": task["current_result_id"], "task_id": task_id, "generation": generation},
                )
                if result:
                    evidence.extend(result["payload"]["artifacts"])
            require(
                all(ref in evidence for ref in evidence_refs),
                "VIEW_DENIED",
                "Evidence is outside the source assignment",
            )
            for ref in evidence_refs:
                self.kernel.artifact(repo, ref, wr["work_id"])
            _quota(repo, task, spec, quota, 0)
            old = self.kernel._prior(
                repo, controller, wr["work_id"], "DYNAMIC_REGISTER", idempotency_key, request
            )
            if old:
                return old
            scope = {"world_ref": wr, "task_id": task_id, "generation": generation}
            identity = {
                k: workshop_binding[k]
                for k in ("installation_id", "org_id", "employee_id", "employment_revision")
            }
            identity.update(role_id=task["role_id"], task_kind=task["task_kind"])
            instance = self._instance(repo, wr, controller, scope, identity)
            head = self._head(repo, wr, instance)
            require(
                head["state"] in {"REGISTERED", "ACTIVE"},
                "DYNAMIC_INSTANCE_INACTIVE",
                "This employment instance is closed",
            )
            instance_id = instance["artifact_revision_id"]
            profile = self._versioned(
                repo,
                wr,
                controller,
                scope,
                "PROFILE",
                _id("profile", instance_id, profile_version),
                {
                    "instance_ref": instance,
                    "profile_version": profile_version,
                    "instructions": role_instructions,
                    "sha256": role_sha,
                    "requested_tools": requested_tools,
                },
            )
            skill = self._versioned(
                repo,
                wr,
                controller,
                scope,
                "SKILL",
                _id("skill", instance_id, workshop_binding["skill_id"], workshop_binding["source_revision"]),
                {
                    "instance_ref": instance,
                    "skill_id": workshop_binding["skill_id"],
                    "source_revision": workshop_binding["source_revision"],
                    "instructions": skill_instructions,
                    "sha256": skill_sha,
                },
            )
            version_id = _id("revision", instance_id, profile_version, workshop_binding)
            existing = repo.one("artifacts", {"work_id": wr["work_id"], "artifact_revision_id": version_id})
            if existing:
                rev = self._record(repo, wr, _ref(existing), "REVISION")
                require(
                    rev["profile_ref"] == profile
                    and rev["skill_ref"] == skill
                    and rev["registration_digest"] == fingerprint(request),
                    "DYNAMIC_VERSION_CONFLICT",
                    "Revision already registered with different provenance",
                )
                result = {
                    "instance_ref": instance,
                    "revision_ref": _ref(existing),
                    "proposal_ref": rev["proposal_ref"],
                }
            else:
                baseline = {
                    "role_id": runtime["role_id"],
                    "task_kind": runtime["task_kind"],
                    "role_version": runtime["role_contract_version"],
                    "skill_name": runtime["skill_name"],
                    "skill_version": runtime["skill_version"],
                    "runtime_sha256": fingerprint(runtime),
                    **runtime_provenance(runtime),
                }
                allowed = sorted(
                    set(checked["capabilities"])
                    & set(grant["capabilities"])
                    & {"READ", "COMPILE", "TASK_CREATE", "TASK_CLAIM", "ARTIFACT_WRITE", "PLAN_READ"}
                )
                proposals = self._pointer(repo, wr["work_id"], "versions:" + instance_id)
                sequence = proposals["epoch"] + 1 if proposals else 1
                proposal = {
                    "schema_version": "1.0",
                    "kind": "ROLE_SKILL_PROPOSAL",
                    "scope": scope,
                    "trigger": trigger,
                    "evidence_refs": evidence_refs,
                    "parent_runtime": baseline,
                    "proposed_role_id": identity["role_id"],
                    "proposed_skill_name": workshop_binding["skill_id"],
                    "proposed_revision": profile_version,
                    "instruction_ref": skill,
                    "requested_workshop_binding": workshop_binding,
                    "scope_limit": {
                        "work_id": wr["work_id"],
                        "branch_id": wr["branch_id"],
                        "task_modes": [task["task_kind"]],
                        "requested_capabilities": allowed,
                        "max_agents": 1,
                        "expires_at": checked["expires_at"],
                    },
                    "quota": quota,
                    "fallback_runtime": baseline,
                    "status": "PROPOSED_NOT_INSTALLED",
                }
                registrar = self.kernel.authorize(repo, controller, wr["work_id"], "ADMIN", wr["branch_id"])
                prior_ref = proposals["artifact_ref"] if proposals else None
                prior_proposal = (
                    self._record(repo, wr, prior_ref, "REVISION")["proposal_ref"] if prior_ref else None
                )
                proposal["candidate"] = {
                    "candidate_id": version_id,
                    "revision": sequence,
                    "parent_ref": prior_proposal,
                    "content_sha256": fingerprint(proposal),
                    "created_by": {
                        "principal_id": controller.principal_id,
                        "grant_id": controller.grant_id,
                        "permission_epoch": registrar["epoch"],
                    },
                }
                validate_contract("RoleSkillProposal", proposal, "harness")
                proposal_ref = self._put(repo, wr, controller, "PROPOSAL", proposal)
                revision = {
                    "scope": scope,
                    "instance_ref": instance,
                    "profile_ref": profile,
                    "skill_ref": skill,
                    "proposal_ref": proposal_ref,
                    "workshop_binding": workshop_binding,
                    "skill_epoch": checked["skill_epoch"],
                    "installation_epoch": checked["installation_epoch"],
                    "requested_capabilities": allowed,
                    "baseline_runtime": baseline,
                    "prior_revision_ref": prior_ref,
                    "sequence": sequence,
                    "registration_digest": fingerprint(request),
                }
                revision_ref = self._put(repo, wr, controller, "REVISION", revision, identifier=version_id)
                self._advance(repo, wr["work_id"], "versions:" + instance_id, revision_ref, sequence)
                result = {
                    "instance_ref": instance,
                    "revision_ref": revision_ref,
                    "proposal_ref": proposal_ref,
                }
            self.kernel._receipt(repo, controller, wr, "DYNAMIC_REGISTER", idempotency_key, request, result)
            return result

    def _source_binding(self, repo, wr, instance_ref, revision_ref):
        instance = self._record(repo, wr, instance_ref, "INSTANCE")
        revision = self._record(repo, wr, revision_ref, "REVISION")
        require(
            revision["instance_ref"] == instance_ref,
            "VIEW_DENIED",
            "Revision belongs to another employee or role",
        )
        identity, binding = instance["identity"], revision["workshop_binding"]
        require(
            all(
                identity[k] == binding[k]
                for k in ("org_id", "installation_id", "employee_id", "employment_revision")
            ),
            "VIEW_DENIED",
            "Revision identity no longer matches the instance",
        )
        installation = repo.one(
            "delegation_installations", {"installation_id": binding["installation_id"]}, lock="share"
        )
        work = repo.one("works", {"work_id": wr["work_id"]})
        require(
            installation
            and installation["active"]
            and not installation["outbound_fenced"]
            and installation["policy_version"] == work["policy_version"]
            and installation["expires_at"] > repo.now()
            and installation["epoch"] == revision["installation_epoch"]
            and _same_branch(wr, installation)
            and installation["org_id"] == binding["org_id"],
            "VIEW_DENIED",
            "Dynamic installation is expired, revoked or out of scope",
        )
        return identity, revision

    def select(self, controller, world_ref, *, instance_ref, revision_ref, expected_epoch, idempotency_key):
        self._enabled()
        _key(idempotency_key)
        require(
            type(expected_epoch) is int and expected_epoch >= 0,
            "INVALID_ARGUMENT",
            "Expected pointer epoch is required",
        )
        wr = world_ref
        with self.kernel.db.uow(write=False) as repo:
            self._control(repo, controller, wr)
            identity, revision = self._source_binding(repo, wr, instance_ref, revision_ref)
        # The source employment can outlive the completed task that justified acquisition.
        # Check the current employee and exact W4 sharing rule without using its historical task lease.
        witness = self._authority().register(
            controller,
            wr,
            workshop_binding=revision["workshop_binding"],
            role_id=identity["role_id"],
            task_kind=identity["task_kind"],
        )
        request = {
            "world_ref": wr,
            "instance_ref": instance_ref,
            "revision_ref": revision_ref,
            "expected_epoch": expected_epoch,
        }
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            self._control(repo, controller, wr, mutate=True)
            _, revision = self._source_binding(repo, wr, instance_ref, revision_ref)
            self._active_witness(repo, revision, witness)
            head = self._head(repo, wr, instance_ref)
            old = self.kernel._prior(
                repo, controller, wr["work_id"], "DYNAMIC_SELECT", idempotency_key, request
            )
            if old:
                return old
            require(
                head["epoch"] == expected_epoch,
                "DYNAMIC_SELECTION_CONFLICT",
                "Another operator advanced the selection",
            )
            require(
                head["state"] in {"REGISTERED", "ACTIVE"},
                "DYNAMIC_INSTANCE_INACTIVE",
                "An exited instance cannot be selected",
            )
            require(
                head["revision_ref"] != revision_ref,
                "DYNAMIC_SELECTION_CONFLICT",
                "This revision is already active",
            )
            return self._event(
                repo,
                controller,
                wr,
                instance_ref,
                head,
                "SELECT",
                "ACTIVE",
                revision_ref,
                idempotency_key,
                request,
            )

    @staticmethod
    def _active_witness(repo, revision, witness):
        from datetime import datetime

        require(
            witness["binding"] == revision["workshop_binding"]
            and witness["skill_epoch"] == revision["skill_epoch"]
            and witness["installation_epoch"] == revision["installation_epoch"]
            and datetime.fromisoformat(witness["expires_at"]) > repo.now(),
            "DYNAMIC_SKILL_DENIED",
            "Skill sharing changed since registration",
        )

    def _event(self, repo, controller, wr, instance, head, action, state, revision, key, request):
        epoch = head["epoch"] + 1
        event = {
            "scope": {"world_ref": wr},
            "instance_ref": instance,
            "revision_ref": revision,
            "previous_event_ref": head["event_ref"],
            "previous_revision_ref": head["revision_ref"],
            "epoch": epoch,
            "state": state,
            "action": action,
            "principal_id": controller.principal_id,
            "recorded_at": repo.now().isoformat(),
        }
        ref = self._put(repo, wr, controller, "EVENT", event)
        self._advance(repo, wr["work_id"], instance["artifact_revision_id"], ref, epoch)
        result = {
            "instance_ref": instance,
            "revision_ref": revision,
            "epoch": epoch,
            "state": state,
            "event_ref": ref,
        }
        command = "DYNAMIC_SELECT" if action == "SELECT" else "DYNAMIC_RETIRE"
        self.kernel._receipt(repo, controller, wr, command, key, request, result)
        return result

    def retire(self, controller, world_ref, *, instance_ref, action, expected_epoch, idempotency_key):
        _key(idempotency_key)
        require(
            action in {"EXIT", "ARCHIVE"} and type(expected_epoch) is int and expected_epoch >= 0,
            "INVALID_ARGUMENT",
            "Expected EXIT or ARCHIVE and a pointer epoch",
        )
        wr = world_ref
        request = {
            "world_ref": wr,
            "instance_ref": instance_ref,
            "expected_epoch": expected_epoch,
            "action": action,
        }
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            self._control(repo, controller, wr, mutate=True)
            head = self._head(repo, wr, instance_ref)
            old = self.kernel._prior(
                repo, controller, wr["work_id"], "DYNAMIC_RETIRE", idempotency_key, request
            )
            if old:
                return old
            require(
                head["epoch"] == expected_epoch,
                "DYNAMIC_SELECTION_CONFLICT",
                "Another operator advanced the selection",
            )
            require(
                head["state"] != "ARCHIVED" and not (head["state"] == "EXITED" and action == "EXIT"),
                "DYNAMIC_INSTANCE_INACTIVE",
                "The requested lifecycle transition has already finished",
            )
            return self._event(
                repo,
                controller,
                wr,
                instance_ref,
                head,
                action,
                "EXITED" if action == "EXIT" else "ARCHIVED",
                None,
                idempotency_key,
                request,
            )

    def history(self, controller, world_ref, *, instance_ref):
        wr = world_ref
        with self.kernel.db.uow(write=False) as repo:
            self._control(repo, controller, wr)
            head = self._head(repo, wr, instance_ref)
            events = []
            ref = head["event_ref"]
            for sequence in range(head["epoch"], 0, -1):
                data = self._record(repo, wr, ref, "EVENT")
                require(
                    data["instance_ref"] == instance_ref and data["epoch"] == sequence,
                    "DYNAMIC_VERSION_CONFLICT",
                    "Invalid dynamic instance history",
                )
                events.append({"event_ref": ref, **data})
                ref = data["previous_event_ref"]
            versions = []
            pointer = self._pointer(repo, wr["work_id"], "versions:" + instance_ref["artifact_revision_id"])
            if pointer:
                ref = pointer["artifact_ref"]
                for sequence in range(pointer["epoch"], 0, -1):
                    data = self._record(repo, wr, ref, "REVISION")
                    require(
                        data["instance_ref"] == instance_ref and data["sequence"] == sequence,
                        "DYNAMIC_VERSION_CONFLICT",
                        "Invalid dynamic revision history",
                    )
                    versions.append(
                        {
                            "revision_ref": ref,
                            "proposal_ref": data["proposal_ref"],
                            "profile_ref": data["profile_ref"],
                            "skill_ref": data["skill_ref"],
                            "sequence": sequence,
                        }
                    )
                    ref = data["prior_revision_ref"]
            return {k: head[k] for k in ("instance_ref", "epoch", "state", "revision_ref")} | {
                "events": list(reversed(events)),
                "revisions": list(reversed(versions)),
            }

    def _freeze_input(self, repo, controller, wr, instance_ref, task_id, generation, role_id, task_kind):
        self._control(repo, controller, wr)
        head = self._head(repo, wr, instance_ref)
        require(
            head["state"] == "ACTIVE",
            "DYNAMIC_INSTANCE_INACTIVE",
            "No active instruction selection for this assignment",
        )
        identifier = _id("freeze", task_id, generation)
        old = repo.one("artifacts", {"work_id": wr["work_id"], "artifact_revision_id": identifier})
        scope = {"world_ref": wr, "task_id": task_id, "generation": generation}
        if old:
            frozen = self._record(repo, wr, _ref(old), "FREEZE")
            require(
                frozen["scope"] == scope
                and frozen["instance_ref"] == instance_ref
                and frozen["role_id"] == role_id
                and frozen["task_kind"] == task_kind,
                "TASK_REBIND_REQUIRED",
                "Task generation already has another freeze",
            )
        else:
            require(
                repo.one("tasks", {"task_id": task_id}) is None
                or repo.one("tasks", {"task_id": task_id})["generation"] < generation,
                "TASK_REBIND_REQUIRED",
                "A running task cannot acquire a new instruction freeze",
            )
        ref = frozen["revision_ref"] if old else head["revision_ref"]
        identity, revision = self._source_binding(repo, wr, instance_ref, ref)
        require(
            (identity["role_id"], identity["task_kind"]) == (role_id, task_kind),
            "ROLE_MODE_DENIED",
            "Employee instance cannot serve this role or task mode",
        )
        return old, head, revision, ref, scope, identifier

    def freeze(self, controller, world_ref, *, role_id, task_kind, task_id, instance_ref, generation=1):
        self._enabled()
        validate_contract("UUID", task_id)
        require(type(generation) is int and generation >= 0, "INVALID_ARGUMENT", "Invalid task generation")
        wr = world_ref
        params = (controller, wr, instance_ref, task_id, generation, role_id, task_kind)
        with self.kernel.db.uow(write=False) as repo:
            _, _, revision, ref, _, _ = self._freeze_input(repo, *params)
        witness = self._authority().register(
            controller,
            wr,
            workshop_binding=revision["workshop_binding"],
            role_id=role_id,
            task_kind=task_kind,
        )
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            self._control(repo, controller, wr, mutate=True)
            old, head, revision, current, scope, identifier = self._freeze_input(repo, *params)
            require(
                current == ref,
                "DYNAMIC_SELECTION_CONFLICT",
                "Instruction selection changed during provisioning",
            )
            self._active_witness(repo, revision, witness)
            if old:
                return _ref(old)
            profile = self._record(repo, wr, revision["profile_ref"], "PROFILE")
            skill = self._record(repo, wr, revision["skill_ref"], "SKILL")
            frozen = {
                "scope": scope,
                "instance_ref": instance_ref,
                "revision_ref": ref,
                "selection_epoch": head["epoch"],
                "role_id": role_id,
                "task_kind": task_kind,
                "profile_version": profile["profile_version"],
                "role_instructions": profile["instructions"],
                "skill_instructions": skill["instructions"],
                "requested_tools": profile["requested_tools"],
                "requested_capabilities": revision["requested_capabilities"],
                "workshop_binding": revision["workshop_binding"],
                "skill_epoch": revision["skill_epoch"],
                "profile_sha256": profile["sha256"],
                "skill_sha256": skill["sha256"],
            }
            return self._put(repo, wr, controller, "FREEZE", frozen, identifier=identifier)

    def load(self, repo, principal, task_id, generation, *, frozen_ref):
        from novel_factory.application.dynamic_runtime import assignment_for
        from novel_factory.application.queries import WorldQueryService

        task, grant, spec = WorldQueryService(self.kernel)._task(repo, principal, task_id, generation)
        wr = spec["world_ref"]
        require(
            assignment_for(repo, task, spec) == frozen_ref,
            "TASK_REBIND_REQUIRED",
            "Dynamic freeze belongs to another assignment or generation",
        )
        frozen = self._record(repo, wr, frozen_ref, "FREEZE")
        initial = frozen["scope"]["generation"]
        require(
            initial <= generation
            and frozen["scope"]["world_ref"] == wr
            and frozen["scope"]["task_id"] == task_id
            and frozen_ref["artifact_revision_id"] == _id("freeze", task_id, initial)
            and frozen["role_id"] == task["role_id"] == grant["role_id"]
            and frozen["task_kind"] == task["task_kind"],
            "VIEW_DENIED",
            "Dynamic freeze and assignment do not match",
        )
        instance = frozen["instance_ref"]
        require(
            self._head(repo, wr, instance)["state"] == "ACTIVE",
            "DYNAMIC_INSTANCE_INACTIVE",
            "Employee instance has exited",
        )
        _, revision = self._source_binding(repo, wr, instance, frozen["revision_ref"])
        receipt = repo.one("delegation_receipts", {"task_id": task_id}, lock="share")
        installation = revision["workshop_binding"]["installation_id"]
        require(
            receipt and receipt["installation_id"] == installation,
            "VIEW_DENIED",
            "Task is outside this Workshop installation",
        )
        slot = repo.one(
            "delegation_slots",
            {"installation_id": installation, "scope_ref": receipt["scope_ref"]},
            lock="share",
        )
        require(
            slot
            and (slot["principal_id"], slot["grant_id"], slot["grant_epoch"], slot["actor_binding_ref"])
            == (principal.principal_id, principal.grant_id, grant["epoch"], grant["actor_id"])
            and slot["spec"]["task_id"] == task_id,
            "VIEW_DENIED",
            "Dynamic assignment grant changed",
        )
        if initial == generation:
            return frozen
        start = repo.one("task_inputs", {"task_id": task_id, "generation": initial})
        current = repo.one("task_inputs", {"task_id": task_id, "generation": generation})
        lifecycle = {"state", "generation", "context_bundle_id", "dependency_fingerprint"}
        require(
            start
            and current
            and task["context_bundle_id"]
            and start["input_hash"] == fingerprint(start["spec"])
            and current["input_hash"] == fingerprint(spec)
            and {k: v for k, v in spec.items() if k not in lifecycle}
            == {k: v for k, v in start["spec"].items() if k not in lifecycle},
            "TASK_REBIND_REQUIRED",
            "Recovery must preserve the complete provisioned assignment",
        )
        lineage = {
            "scope": {"world_ref": wr, "task_id": task_id, "generation": generation},
            "frozen_ref": frozen_ref,
            "original_input_sha256": start["input_hash"],
            "current_input_sha256": current["input_hash"],
            "original_generation": initial,
            "grant_id": grant["grant_id"],
            "permission_epoch": grant["epoch"],
            "installation_id": installation,
            "delegation_receipt": receipt["receipt_ref"],
        }
        return {
            **frozen,
            "scope": lineage["scope"],
            "recovered_from_generation": initial,
            "recovery_lineage": lineage,
        }

    def record_recovery(self, principal, task_id, generation, *, frozen_ref, live):
        """Retain one generation receipt only after the original employee's live W4 check."""
        authority = self._authority()
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            task = repo.one("tasks", {"task_id": task_id})
            require(task and task["generation"] == generation, "STALE_GENERATION", "Task was superseded")
            repo.one("works", {"work_id": task["work_id"]}, lock="update")
            frozen = self.load(repo, principal, task_id, generation, frozen_ref=frozen_ref)
            require(
                "recovery_lineage" in frozen, "TASK_REBIND_REQUIRED", "Initial assignment is already frozen"
            )
            witness = authority.verify(
                repo, principal, task_id, generation, workshop_binding=frozen["workshop_binding"], live=live
            )
            require(
                witness["skill_epoch"] == frozen["skill_epoch"],
                "DYNAMIC_SKILL_DENIED",
                "Original skill sharing changed",
            )
            scope = frozen["scope"]
            return self._versioned(
                repo,
                scope["world_ref"],
                principal,
                scope,
                "GENERATION",
                _id("generation", task_id, generation),
                {
                    "lineage": frozen["recovery_lineage"],
                    "revision_ref": frozen["revision_ref"],
                    "skill_epoch": frozen["skill_epoch"],
                    "binding": frozen["workshop_binding"],
                },
            )
