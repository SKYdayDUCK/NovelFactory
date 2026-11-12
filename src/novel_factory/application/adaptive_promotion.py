"""Independent review and branch-local publication of immutable instruction selections."""

from datetime import datetime
from decimal import Decimal

from novel_factory.application.authorization import Principal
from novel_factory.application.creative_review import _task
from novel_factory.application.kernel import require
from novel_factory.application.review_independence import _refresh_bindings
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig


def _same_branch(a, b):
    return (a["work_id"], a["branch_id"]) == (b["work_id"], b["branch_id"])


def _until(value):
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(instant.tzinfo is not None, "INVALID_ARGUMENT", "A timezone-qualified deadline is required")
        return instant
    except (ValueError, AttributeError):
        require(False, "INVALID_ARGUMENT", "Invalid authorization deadline")


def _identity(grant):
    return {
        "grant_id": grant["grant_id"],
        "principal_id": grant["principal_id"],
        "permission_epoch": grant["epoch"],
    }


def _idempotency(value):
    require(
        isinstance(value, str) and 1 <= len(value.strip()) <= 200,
        "INVALID_ARGUMENT",
        "A bounded idempotency key is required",
    )


def _reason(value):
    require(
        isinstance(value, str) and 0 < len(value.strip()) <= 4000,
        "INVALID_ARGUMENT",
        "A bounded reason is required",
    )


def _target(role_id, task_kind, skill_name, dynamic_instance_ref=None):
    return {
        "role_id": role_id,
        "task_kind": task_kind,
        "skill_name": skill_name,
        "dynamic_instance_ref": dynamic_instance_ref,
    }


def _key(world_ref, target):
    return fingerprint([world_ref["work_id"], world_ref["branch_id"], target])


class AdaptivePromotionService:
    def __init__(self, kernel, *, config=None):
        self.kernel = kernel
        self.config = config

    def _enabled(self):
        cfg = self.config or CreativeHarnessConfig.from_env()
        require(
            cfg.enabled and cfg.adaptive_instructions,
            "ADAPTIVE_INSTRUCTIONS_DISABLED",
            "Instruction optimization is disabled",
        )
        return cfg

    def _control(self, repo, owner, wr, *, write=False):
        if write:
            repo.one("works", {"work_id": wr["work_id"]}, lock="update")
        grant = self.kernel.authorize(repo, owner, wr["work_id"], "ADMIN", wr["branch_id"])
        require(
            grant["role_id"] == "control_operator",
            "VIEW_DENIED",
            "An authorized instruction controller is required",
        )
        self.kernel.revision(repo, wr)
        return grant

    def _record(self, repo, wr, ref, kind):
        row = self.kernel.artifact(repo, ref, wr["work_id"], kind="CREATIVE_INSTRUCTION_" + kind)
        data = row["payload"]
        self.kernel.objects.verify(ref["sha256"])
        require(
            fingerprint(data) == ref["sha256"] and _same_branch(wr, data["scope"]["world_ref"]),
            "VIEW_DENIED",
            "Instruction record is outside the requested branch",
        )
        return data

    def _put(self, repo, principal, scope, kind, payload):
        body = {**payload, "scope": scope}
        ref = self.kernel._blob(body)
        self.kernel._register_artifact(
            repo,
            ref,
            scope["world_ref"]["work_id"],
            body,
            "CREATIVE_INSTRUCTION_" + kind,
            principal.principal_id,
        )
        return ref

    def _grant(self, repo, wr, gid, cap):
        row = repo.one("grants", {"grant_id": gid}, lock="share")
        require(row, "VIEW_DENIED", "Participant unavailable")
        return self.kernel.authorize(
            repo, Principal(gid, row["principal_id"]), wr["work_id"], cap, wr["branch_id"]
        )

    def _candidate(self, repo, wr, ref):
        candidate = self._record(repo, wr, ref, "CANDIDATE")
        validate_contract("InstructionCandidate", candidate, "harness")
        design = self._record(repo, wr, candidate["freeze_ref"], "DESIGN")
        require(
            candidate["scope"] == design["scope"]
            and candidate["candidate"]["created_by"] in design["producers"],
            "INVALID_EVIDENCE",
            "Original candidate producer is missing",
        )
        return candidate, design

    def _evaluation(
        self, repo, wr, candidate_ref, engineering_report_ref, run_manifest_ref, *, release=False
    ):
        from .adaptive_evaluation import AdaptiveEvaluationService

        svc = AdaptiveEvaluationService(self.kernel)
        fn = svc.require_release if release else svc.inspect
        return fn(
            repo,
            wr,
            candidate_ref=candidate_ref,
            engineering_report_ref=engineering_report_ref,
            run_manifest_ref=run_manifest_ref,
        )

    def _reviewer(self, repo, principal, wr, refs, task_id, generation):
        if task_id is None:
            matches = []
            for row in repo.many(
                "tasks",
                {
                    "work_id": wr["work_id"],
                    "branch_id": wr["branch_id"],
                    "grant_id": principal.grant_id,
                    "state": "RUNNING",
                },
            ):
                spec = repo.one("task_inputs", {"task_id": row["task_id"], "generation": row["generation"]})
                if spec and all(ref in spec["spec"]["input_artifacts"] for ref in refs):
                    matches.append(row)
            require(len(matches) == 1, "VIEW_DENIED", "A single assigned reviewer task is required")
            task_id, generation = matches[0]["task_id"], matches[0]["generation"]
        task, grant, spec, data, _ = _task(self.kernel, repo, principal, task_id, generation, "review_submit")
        enabled = (
            data.get("creative_harness", {})
            .get("creative_harness", {})
            .get("adaptive_instructions", {})
            .get("enabled")
        )
        require(
            enabled is True, "VIEW_DENIED", "This reviewer assignment did not enable instruction evaluation"
        )
        require(
            _same_branch(spec["world_ref"], wr)
            and all(ref in spec["input_artifacts"] for ref in refs)
            and grant["role_id"] in {"reviewer", "literary_editor", "chief_editor"},
            "VIEW_DENIED",
            "The exact experiment is outside this review assignment",
        )
        return task, _identity(grant)

    def _case(self, repo, wr, candidate_ref, eng, manifest, evaluation, reviewer_task, publisher_grant_id):
        candidate, design = self._candidate(repo, wr, candidate_ref)
        producers = [*design["producers"]]
        if evaluation["evaluator"] not in producers:
            producers.append(evaluation["evaluator"])
        reviewer = _identity(self._grant(repo, wr, reviewer_task["grant_id"], "READ"))
        publisher = _identity(self._grant(repo, wr, publisher_grant_id, "ADMIN"))
        for who in producers:
            require(
                _identity(self._grant(repo, wr, who["grant_id"], "READ")) == who,
                "PROMOTION_REVOKED",
                "A producer or evaluation authority changed",
            )
        ids = {p["principal_id"] for p in producers}
        require(
            reviewer["principal_id"] not in ids
            and publisher["principal_id"] not in ids | {reviewer["principal_id"]},
            "INDEPENDENT_REVIEW_REQUIRED",
            "The author, reviewer and publisher require separate independent subjects",
        )
        return {
            "kind": "INSTRUCTION_PROMOTION_CASE",
            "authority": "NATIVE",
            "world_ref": candidate["scope"]["world_ref"],
            "candidate_ref": candidate_ref,
            "engineering_report_ref": eng,
            "run_manifest_ref": manifest,
            "freeze_ref": evaluation["freeze_ref"],
            "producers": producers,
            "reviewer": reviewer,
            "publisher": publisher,
            "reviewer_task_id": reviewer_task["task_id"],
            "reviewer_generation": reviewer_task["generation"],
        }

    def describe_case(
        self,
        controller,
        world_ref,
        *,
        candidate_ref,
        engineering_report_ref,
        run_manifest_ref,
        reviewer_task_id,
        reviewer_generation,
        publisher_grant_id,
    ):
        self._enabled()
        with self.kernel.db.uow(write=False) as repo:
            self._control(repo, controller, world_ref)
            task = repo.one("tasks", {"task_id": reviewer_task_id})
            require(
                task and task["generation"] == reviewer_generation,
                "VIEW_DENIED",
                "Review assignment unavailable",
            )
            role = Principal(task["grant_id"], task["principal_id"])
            refs = [candidate_ref, engineering_report_ref, run_manifest_ref]
            self._reviewer(repo, role, world_ref, refs, reviewer_task_id, reviewer_generation)
            ev = self._evaluation(repo, world_ref, *refs)
            return self._case(repo, world_ref, *refs, ev, task, publisher_grant_id)

    @staticmethod
    def _case_proof(case):
        return _refresh_bindings([case])[fingerprint(case)]

    def _refresh_lineage(self, wr, candidate_ref, reviewer_task):
        from concurrent.futures import ThreadPoolExecutor
        from .adaptive_instructions import AdaptiveInstructionService, issuer_for_candidate
        from .dynamic_runtime import refresh_task

        with self.kernel.db.uow(write=False) as repo:
            issuer = issuer_for_candidate(self.kernel, repo, wr, candidate_ref)
        reviewer = Principal(reviewer_task["grant_id"], reviewer_task["principal_id"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            source = pool.submit(
                AdaptiveInstructionService(self.kernel).refresh_sources, issuer, wr, candidate_ref
            )
            review = pool.submit(
                refresh_task, self.kernel, reviewer, reviewer_task["task_id"], reviewer_task["generation"]
            )
            return issuer, source.result(), review.result()

    def _validate_lineage(self, repo, wr, candidate_ref, reviewer_task, live):
        from .adaptive_instructions import AdaptiveInstructionService
        from .dynamic_runtime import verify_task

        issuer, source, review = live
        AdaptiveInstructionService(self.kernel).load_evaluation(repo, issuer, wr, candidate_ref, live=source)
        reviewer = Principal(reviewer_task["grant_id"], reviewer_task["principal_id"])
        reviewer_task, *_ = _task(
            self.kernel,
            repo,
            reviewer,
            reviewer_task["task_id"],
            reviewer_task["generation"],
            "review_submit",
            live=False,
        )
        verify_task(self.kernel, repo, reviewer, reviewer_task, review)
        if review:
            from .dynamic_agents import DynamicAgentService

            assignment = DynamicAgentService(self.kernel).load(
                repo, reviewer, reviewer_task["task_id"], reviewer_task["generation"], frozen_ref=review[0]
            )
            require(
                "review_submit" in assignment["requested_tools"],
                "VIEW_DENIED",
                "The dynamic reviewer has no opinion authority",
            )

    def evidence(
        self,
        reviewer,
        world_ref,
        *,
        candidate_ref,
        engineering_report_ref,
        run_manifest_ref,
        reviewer_task_id=None,
        reviewer_generation=None,
    ):
        self._enabled()
        refs = [candidate_ref, engineering_report_ref, run_manifest_ref]
        with self.kernel.db.uow(write=False) as repo:
            task, _ = self._reviewer(repo, reviewer, world_ref, refs, reviewer_task_id, reviewer_generation)
        live = self._refresh_lineage(world_ref, candidate_ref, task)
        with self.kernel.db.uow(write=False) as repo:
            task, who = self._reviewer(repo, reviewer, world_ref, refs, reviewer_task_id, reviewer_generation)
            self._validate_lineage(repo, world_ref, candidate_ref, task, live)
            candidate, design = self._candidate(repo, world_ref, candidate_ref)
            ev = self._evaluation(repo, world_ref, *refs)
            require(
                who["principal_id"]
                not in {p["principal_id"] for p in [*design["producers"], ev["evaluator"]]},
                "INDEPENDENT_REVIEW_REQUIRED",
                "An author or evaluator cannot review their own change",
            )
            # Permit comparisons of objective results; raw prompts and manuscript excerpts remain with NF delivery ports.
            return {
                k: ev[k]
                for k in (
                    "freeze_ref",
                    "engineering_report_ref",
                    "run_manifest_ref",
                    "eligible",
                    "status",
                    "quality",
                    "arm_totals",
                )
            } | {
                "candidate_ref": candidate_ref,
                "change_kind": candidate["change_kind"],
                "target": design["target"],
                "cases": [
                    {
                        k: row[k]
                        for k in (
                            "arm_id",
                            "case_id",
                            "repetition",
                            "status",
                            "held_out",
                            "score",
                            "latency_ns",
                            "amount",
                        )
                        if k in row
                    }
                    for row in ev["results"]
                ],
            }

    def review(
        self,
        reviewer,
        world_ref,
        *,
        candidate_ref,
        engineering_report_ref,
        run_manifest_ref,
        publisher_grant_id,
        verdict,
        reason,
        valid_until,
        idempotency_key,
        reviewer_task_id=None,
        reviewer_generation=None,
    ):
        _idempotency(idempotency_key)
        self._enabled()
        _reason(reason)
        require(verdict in {"APPROVE", "REJECT"}, "INVALID_ARGUMENT", "Invalid reviewer opinion")
        refs = [candidate_ref, engineering_report_ref, run_manifest_ref]
        args = dict(
            refs=refs,
            publisher_grant_id=publisher_grant_id,
            verdict=verdict,
            reason=reason,
            valid_until=valid_until,
            reviewer_task_id=reviewer_task_id,
            reviewer_generation=reviewer_generation,
        )

        def check(repo):
            task, _ = self._reviewer(repo, reviewer, world_ref, refs, reviewer_task_id, reviewer_generation)
            ev = self._evaluation(repo, world_ref, *refs)
            return self._case(repo, world_ref, *refs, ev, task, publisher_grant_id)

        with self.kernel.db.uow(write=False) as repo:
            case = check(repo)
            task = repo.one("tasks", {"task_id": case["reviewer_task_id"]})
        live = self._refresh_lineage(world_ref, candidate_ref, task)
        proof = self._case_proof(case)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": world_ref["work_id"]}, lock="update")
            require(
                case == check(repo) and repo.now() < _until(valid_until) <= proof.valid_until,
                "PROMOTION_REVOKED",
                "Review authority expired or changed",
            )
            require(
                self._case_proof(case).evidence_digest == proof.evidence_digest,
                "PROMOTION_REVOKED",
                "Native independence authorization changed",
            )
            self._validate_lineage(repo, world_ref, candidate_ref, task, live)
            old = self.kernel._prior(
                repo, reviewer, world_ref["work_id"], "INSTRUCTION_REVIEW", idempotency_key, args
            )
            if old:
                return old
            scope = self._candidate(repo, world_ref, candidate_ref)[0]["scope"]
            independence = self._put(
                repo,
                reviewer,
                scope,
                "INDEPENDENCE",
                {
                    "case": case,
                    "evidence_digest": proof.evidence_digest,
                    "expires_at": proof.valid_until.isoformat(),
                },
            )
            record = {
                "candidate_ref": candidate_ref,
                "engineering_report_ref": engineering_report_ref,
                "run_manifest_ref": run_manifest_ref,
                "freeze_ref": case["freeze_ref"],
                "independence_ref": independence,
                "reviewer": case["reviewer"],
                "publisher": case["publisher"],
                "reviewer_task_id": case["reviewer_task_id"],
                "reviewer_generation": case["reviewer_generation"],
                "verdict": verdict,
                "reason": reason,
                "valid_until": valid_until,
            }
            out = {"review_ref": self._put(repo, reviewer, scope, "OPINION", record), "verdict": verdict}
            pointer = {
                "work_id": world_ref["work_id"],
                "selection_kind": "ADAPTIVE_OPINION:" + fingerprint(case),
            }
            current = repo.one("selections", pointer)
            if current:
                repo.change(
                    "selections", pointer, {"artifact_ref": out["review_ref"], "epoch": current["epoch"] + 1}
                )
            else:
                repo.add("selections", {**pointer, "artifact_ref": out["review_ref"], "epoch": 1})
            self.kernel._receipt(repo, reviewer, world_ref, "INSTRUCTION_REVIEW", idempotency_key, args, out)
            return out

    def _current(self, repo, wr, key):
        row = repo.one(
            "selections",
            {"work_id": wr["work_id"], "selection_kind": "ADAPTIVE_RELEASE:" + key},
            lock="share",
        )
        if not row:
            return {"epoch": 0, "selection_ref": None, "decision_ref": None}
        head = self._record(repo, wr, row["artifact_ref"], "HEAD")
        require(
            row["epoch"] == head["epoch"] and head["scope_key"] == key,
            "INVALID_EVIDENCE",
            "Release pointer is corrupt",
        )
        return head

    def _advance(self, repo, owner, scope, key, epoch, selection_ref, decision_ref):
        wr = scope["world_ref"]
        head = {
            "scope_key": key,
            "epoch": epoch + 1,
            "selection_ref": selection_ref,
            "decision_ref": decision_ref,
        }
        ref = self._put(repo, owner, scope, "HEAD", head)
        pk = {"work_id": wr["work_id"], "selection_kind": "ADAPTIVE_RELEASE:" + key}
        if epoch:
            require(
                repo.change("selections", {**pk, "epoch": epoch}, {"artifact_ref": ref, "epoch": epoch + 1})
                == 1,
                "SELECTION_CONFLICT",
                "Release pointer changed",
            )
        else:
            repo.add("selections", {**pk, "artifact_ref": ref, "epoch": 1})
        return head

    def policy(
        self,
        controller,
        world_ref,
        *,
        role_id,
        task_kind,
        skill_name,
        publisher_grant_id,
        valid_until,
        idempotency_key,
        dynamic_instance_ref=None,
    ):
        _idempotency(idempotency_key)
        self._enabled()
        target = _target(role_id, task_kind, skill_name, dynamic_instance_ref)
        args = {
            "target": target,
            "publisher_grant_id": publisher_grant_id,
            "valid_until": valid_until,
            "world_ref": world_ref,
        }
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            issuer = self._control(repo, controller, world_ref, write=True)
            publisher = self._grant(repo, world_ref, publisher_grant_id, "ADMIN")
            require(
                repo.now() < _until(valid_until)
                and all(isinstance(s, str) and 0 < len(s) <= 128 for s in [role_id, task_kind, skill_name]),
                "INVALID_ARGUMENT",
                "An exact target and future expiry are required",
            )
            if dynamic_instance_ref:
                instance = self.kernel.artifact(
                    repo, dynamic_instance_ref, world_ref["work_id"], kind="CREATIVE_DYNAMIC_INSTANCE"
                )["payload"]
                require(
                    _same_branch(world_ref, instance["scope"]["world_ref"])
                    and instance["identity"]["role_id"] == role_id
                    and instance["identity"]["task_kind"] == task_kind,
                    "VIEW_DENIED",
                    "Policy instance is outside the designated runtime",
                )
            prior = self.kernel._prior(
                repo, controller, world_ref["work_id"], "INSTRUCTION_AUTO_POLICY", idempotency_key, args
            )
            if prior:
                return prior
            payload = {
                **args,
                "route": "POLICY_AUTOMATIC",
                "issuer": _identity(issuer),
                "publisher": _identity(publisher),
            }
            out = {"policy_ref": self._put(repo, controller, {"world_ref": world_ref}, "POLICY", payload)}
            self.kernel._receipt(
                repo, controller, world_ref, "INSTRUCTION_AUTO_POLICY", idempotency_key, args, out
            )
            return out

    def _opinion(self, repo, wr, refs, review_ref, owner, *, live=True):
        op = self._record(repo, wr, review_ref, "OPINION")
        require(
            [op["candidate_ref"], op["engineering_report_ref"], op["run_manifest_ref"]] == refs
            and op["publisher"] == _identity(self._grant(repo, wr, owner.grant_id, "ADMIN"))
            and op["publisher"]["principal_id"] == owner.principal_id,
            "VIEW_DENIED",
            "Opinion is for another release or publisher",
        )
        independent = self._record(repo, wr, op["independence_ref"], "INDEPENDENCE")
        case = independent["case"]
        require(
            case["reviewer"] == op["reviewer"]
            and case["publisher"] == op["publisher"]
            and case["candidate_ref"] == refs[0]
            and case["engineering_report_ref"] == refs[1]
            and case["run_manifest_ref"] == refs[2],
            "INVALID_EVIDENCE",
            "Opinion does not match registered case",
        )
        if live:
            current = repo.one(
                "selections",
                {"work_id": wr["work_id"], "selection_kind": "ADAPTIVE_OPINION:" + fingerprint(case)},
            )
            require(
                current and current["artifact_ref"] == review_ref,
                "PROMOTION_REVOKED",
                "Reviewer opinion was superseded",
            )
            require(repo.now() < _until(op["valid_until"]), "PROMOTION_REVOKED", "Reviewer approval expired")
            for person in [*case["producers"], case["reviewer"], case["publisher"]]:
                require(
                    _identity(self._grant(repo, wr, person["grant_id"], "READ")) == person,
                    "PROMOTION_REVOKED",
                    "Participant authority changed",
                )
            task = repo.one("tasks", {"task_id": case["reviewer_task_id"]}, lock="share")
            require(
                task
                and task["generation"] == case["reviewer_generation"]
                and task["state"] not in {"FAILED", "CANCELLED", "STALE", "PAUSED"},
                "PROMOTION_REVOKED",
                "Reviewer assignment changed",
            )
            attest = self._case_proof(case)
            require(
                attest.evidence_digest == independent["evidence_digest"] and attest.valid_until > repo.now(),
                "PROMOTION_REVOKED",
                "Case was replaced or withdrawn",
            )
        return op, case

    def revoke_policy(self, controller, world_ref, *, policy_ref, reason, idempotency_key):
        self._enabled()
        _idempotency(idempotency_key)
        _reason(reason)
        args = {"policy_ref": policy_ref, "reason": reason}
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            grant = self._control(repo, controller, world_ref, write=True)
            policy = self._record(repo, world_ref, policy_ref, "POLICY")
            require(
                policy["route"] == "POLICY_AUTOMATIC",
                "INVALID_ARGUMENT",
                "Only a provisioned automatic policy can be withdrawn",
            )
            old = self.kernel._prior(
                repo, controller, world_ref["work_id"], "INSTRUCTION_POLICY_WITHDRAW", idempotency_key, args
            )
            if old:
                return old
            key = self._policy_revocation_key(policy_ref)
            existing = repo.one("selections", {"work_id": world_ref["work_id"], "selection_kind": key})
            if existing:
                ref = existing["artifact_ref"]
            else:
                ref = self._put(
                    repo,
                    controller,
                    policy["scope"],
                    "POLICY_WITHDRAWAL",
                    {"policy_ref": policy_ref, "reason": reason, "withdrawn_by": _identity(grant)},
                )
                repo.add(
                    "selections",
                    {"work_id": world_ref["work_id"], "selection_kind": key, "artifact_ref": ref, "epoch": 1},
                )
            out = {"revocation_ref": ref, "policy_ref": policy_ref}
            self.kernel._receipt(
                repo, controller, world_ref, "INSTRUCTION_POLICY_WITHDRAW", idempotency_key, args, out
            )
            return out

    @staticmethod
    def _policy_revocation_key(ref):
        return "ADAPTIVE_POLICY_WITHDRAWN:" + ref["artifact_revision_id"]

    def _policy_check(self, repo, wr, policy_ref, target, publisher, until, route):
        policy = self._record(repo, wr, policy_ref, "POLICY")
        require(
            not repo.one(
                "selections",
                {"work_id": wr["work_id"], "selection_kind": self._policy_revocation_key(policy_ref)},
            ),
            "PROMOTION_REVOKED",
            "Instruction release policy was withdrawn",
        )
        require(
            policy["target"] == target
            and policy["route"] == route
            and policy["publisher"] == publisher
            and _identity(self._grant(repo, wr, policy["issuer"]["grant_id"], "ADMIN")) == policy["issuer"]
            and repo.now() < until <= _until(policy["valid_until"]),
            "PROMOTION_REVOKED",
            "Release policy scope, authority or expiry changed",
        )
        return policy

    def _decision(
        self, repo, publisher, candidate, decision, route, cfg, op, case, reason, refs, previous, authority
    ):
        candidate_ref, eng, manifest = refs
        data = {
            "schema_version": "1.0",
            "kind": "PROMOTION_DECISION",
            "scope": candidate["scope"],
            "candidate_ref": candidate_ref,
            "previous_version_ref": previous,
            "decision": decision,
            "route": route,
            "creative_harness": cfg.as_dict()["creative_harness"],
            "independence": {
                "producer": candidate["candidate"]["created_by"],
                "reviewer": case["reviewer"],
                "independence_receipt": op["independence_ref"],
            },
            "evidence": {
                "freeze_ref": case["freeze_ref"],
                "engineering_report_ref": eng,
                "run_manifest_ref": manifest,
                "literary_status": "UNPROVEN",
                "blind_review_refs": [],
                "reviewer_ids": [],
                "decision_reason": reason,
            },
            "authority": authority,
            "rollback_ref": candidate["rollback_ref"],
            "status": "DECISION_RECORD",
        }
        validate_contract("PromotionDecision", data, "harness")
        return self._put(repo, publisher, candidate["scope"], "DECISION", data)

    def decide(
        self,
        publisher,
        world_ref,
        *,
        candidate_ref,
        engineering_report_ref,
        run_manifest_ref,
        review_ref,
        action,
        expected_epoch,
        reason,
        valid_until,
        idempotency_key,
        route="MANUAL",
        policy_ref=None,
    ):
        _idempotency(idempotency_key)
        from .adaptive_instructions import load_instruction
        from .adaptive_runtime import read_selection

        cfg = self._enabled()
        _reason(reason)
        require(
            action in {"APPROVE", "REJECT"}
            and route in {"MANUAL", "POLICY_AUTOMATIC"}
            and type(expected_epoch) is int
            and expected_epoch >= 0
            and (route != "POLICY_AUTOMATIC" or cfg.auto_promote and policy_ref is not None),
            "PROMOTION_NOT_AUTHORIZED",
            "Explicit automatic policy and enabled configuration are required",
        )
        refs = [candidate_ref, engineering_report_ref, run_manifest_ref]
        until = _until(valid_until)
        request = {
            "refs": refs,
            "review_ref": review_ref,
            "action": action,
            "expected_epoch": expected_epoch,
            "reason": reason,
            "valid_until": valid_until,
            "route": route,
            "policy_ref": policy_ref,
        }
        with self.kernel.db.uow(write=False) as repo:
            self._control(repo, publisher, world_ref)
            pre, _ = self._opinion(repo, world_ref, refs, review_ref, publisher)
            reviewer_task = repo.one("tasks", {"task_id": pre["reviewer_task_id"]})
        live = self._refresh_lineage(world_ref, candidate_ref, reviewer_task)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            pub = _identity(self._control(repo, publisher, world_ref, write=True))
            op, case = self._opinion(repo, world_ref, refs, review_ref, publisher)
            require(
                repo.now() < until <= _until(op["valid_until"]),
                "PROMOTION_REVOKED",
                "Publisher authority exceeds the review deadline",
            )
            candidate, design = self._candidate(repo, world_ref, candidate_ref)
            self._validate_lineage(repo, world_ref, candidate_ref, reviewer_task, live)
            ev = self._evaluation(repo, world_ref, *refs, release=(action == "APPROVE"))
            require(
                ev["freeze_ref"] == case["freeze_ref"] and ev["evaluator"] in case["producers"],
                "INVALID_EVIDENCE",
                "Frozen experiment changed",
            )
            require(
                action != "APPROVE" or op["verdict"] == "APPROVE",
                "PROMOTION_NOT_AUTHORIZED",
                "Independent reviewer did not approve",
            )
            target = _target(
                design["target"]["role_id"],
                design["target"]["task_kind"],
                design["target"]["skill_name"],
                design["target"].get("instance_ref"),
            )
            key = _key(world_ref, target)
            scope = candidate["scope"]
            if policy_ref:
                self._policy_check(repo, world_ref, policy_ref, target, pub, until, route)
            old = self.kernel._prior(
                repo, publisher, world_ref["work_id"], "INSTRUCTION_DECIDE", idempotency_key, request
            )
            if old:
                return old
            head = self._current(repo, world_ref, key)
            require(
                head["epoch"] == expected_epoch, "SELECTION_CONFLICT", "Instruction release pointer changed"
            )
            previous = head["selection_ref"] or candidate["rollback_ref"]
            if action == "APPROVE":
                require(
                    head["selection_ref"] == design.get("source_adaptive_ref"),
                    "SELECTION_CONFLICT",
                    "Candidate baseline is no longer selected",
                )
                instruction = load_instruction(self.kernel, repo, world_ref, candidate["instruction_ref"])
                blocks = (
                    []
                    if head["selection_ref"] is None
                    else read_selection(self.kernel, repo, world_ref, head["selection_ref"])
                )
                require(
                    len(blocks) < 32
                    and sum(len(b["body"].encode()) for b in [*blocks, instruction]) <= 200000,
                    "CAPACITY_LIMIT",
                    "Prospective instruction chain exceeds the task limit",
                )
            if not policy_ref:
                policy_ref = self._put(
                    repo,
                    publisher,
                    scope,
                    "POLICY",
                    {
                        "route": "MANUAL",
                        "target": target,
                        "issuer": pub,
                        "publisher": pub,
                        "valid_until": valid_until,
                    },
                )
            auth_ref = self._put(
                repo,
                publisher,
                scope,
                "AUTHORITY",
                {
                    "publisher": pub,
                    "review_ref": review_ref,
                    "policy_ref": policy_ref,
                    "scope_key": key,
                    "valid_until": valid_until,
                    "route": route,
                    "candidate_ref": candidate_ref,
                    "source_epoch": head["epoch"],
                },
            )
            authority = {
                "approver": pub,
                "authority_receipt": auth_ref,
                "policy_ref": policy_ref,
                "valid_until": valid_until,
            }
            decision = self._decision(
                repo, publisher, candidate, action, route, cfg, op, case, reason, refs, previous, authority
            )
            if action == "APPROVE":
                selection = self._put(
                    repo,
                    publisher,
                    scope,
                    "SELECTION",
                    {
                        **target,
                        "scope_key": key,
                        "epoch": head["epoch"] + 1,
                        "valid_until": valid_until,
                        "source_adaptive_ref": head["selection_ref"],
                        "baseline_runtime_sha256": candidate["baseline_runtime"]["runtime_sha256"],
                        "instruction_ref": candidate["instruction_ref"],
                        "candidate_ref": candidate_ref,
                        "decision_ref": decision,
                        "change_kind": candidate["change_kind"],
                    },
                )
                head = self._advance(repo, publisher, scope, key, head["epoch"], selection, decision)
            out = {
                "decision_ref": decision,
                "selection_ref": head["selection_ref"],
                "epoch": head["epoch"],
                "action": action,
            }
            self.kernel._receipt(
                repo, publisher, world_ref, "INSTRUCTION_DECIDE", idempotency_key, request, out
            )
            return out

    def _verify_selection(self, repo, wr, ref, target):
        from .adaptive_instructions import load_instruction

        item = self._record(repo, wr, ref, "SELECTION")
        require(
            all(item.get(k) == v for k, v in target.items()) and item["scope_key"] == _key(wr, target),
            "VIEW_DENIED",
            "Selection belongs to another runtime target",
        )
        decision = self._record(repo, wr, item["decision_ref"], "DECISION")
        auth = decision["authority"]
        proof = self._record(repo, wr, auth["authority_receipt"], "AUTHORITY")
        require(
            decision["decision"] == "APPROVE"
            and decision["candidate_ref"] == item["candidate_ref"]
            and auth["approver"] == proof["publisher"]
            and auth["valid_until"] == item["valid_until"]
            and auth["policy_ref"] == proof["policy_ref"],
            "INVALID_EVIDENCE",
            "Release selection does not match its approval",
        )
        publisher = Principal(proof["publisher"]["grant_id"], proof["publisher"]["principal_id"])
        evidence = decision["evidence"]
        refs = [item["candidate_ref"], evidence["engineering_report_ref"], evidence["run_manifest_ref"]]
        self._opinion(repo, wr, refs, proof["review_ref"], publisher)
        self._policy_check(
            repo,
            wr,
            auth["policy_ref"],
            target,
            auth["approver"],
            _until(auth["valid_until"]),
            decision["route"],
        )
        value = load_instruction(self.kernel, repo, wr, item["instruction_ref"])
        return {**item, "selection_ref": ref, "instructions": value["body"]}

    def selected(self, repo, world_ref, *, role_id, task_kind, skill_name, dynamic_instance_ref=None):
        cfg = self.config or CreativeHarnessConfig.from_env()
        if not cfg.enabled or not cfg.adaptive_instructions:
            return None
        target = _target(role_id, task_kind, skill_name, dynamic_instance_ref)
        head = self._current(repo, world_ref, _key(world_ref, target))
        if not head["selection_ref"]:
            return None
        return self._verify_selection(repo, world_ref, head["selection_ref"], target)

    def rollback(
        self, publisher, world_ref, *, decision_ref, expected_epoch, reason, idempotency_key, review_ref=None
    ):
        _idempotency(idempotency_key)
        cfg = self._enabled()
        _reason(reason)
        request = {
            "decision_ref": decision_ref,
            "expected_epoch": expected_epoch,
            "reason": reason,
            "review_ref": review_ref,
        }
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            actor = _identity(self._control(repo, publisher, world_ref, write=True))
            decision = self._record(repo, world_ref, decision_ref, "DECISION")
            require(
                decision["decision"] == "APPROVE", "INVALID_ARGUMENT", "Rollback requires an approved release"
            )
            evidence, auth = decision["evidence"], decision["authority"]
            authority = self._record(repo, world_ref, auth["authority_receipt"], "AUTHORITY")
            refs = [
                decision["candidate_ref"],
                evidence["engineering_report_ref"],
                evidence["run_manifest_ref"],
            ]
            op, case = self._opinion(
                repo,
                world_ref,
                refs,
                review_ref or authority["review_ref"],
                publisher,
                live=review_ref is not None,
            )
            if review_ref is not None:
                require(
                    op["verdict"] == "REJECT",
                    "PROMOTION_NOT_AUTHORIZED",
                    "A replacement publisher needs an independent rejection opinion",
                )
            require(
                actor == case["publisher"], "VIEW_DENIED", "Rollback requires the scoped release publisher"
            )
            old = self.kernel._prior(
                repo, publisher, world_ref["work_id"], "INSTRUCTION_ROLLBACK", idempotency_key, request
            )
            if old:
                return old
            key = authority["scope_key"]
            head = self._current(repo, world_ref, key)
            require(
                head["epoch"] == expected_epoch and head["selection_ref"],
                "SELECTION_CONFLICT",
                "Release pointer changed",
            )
            item = self._record(repo, world_ref, head["selection_ref"], "SELECTION")
            require(
                item["decision_ref"] == decision_ref,
                "SELECTION_CONFLICT",
                "Only the currently selected release can be rolled back",
            )
            previous = item["source_adaptive_ref"]
            if previous:
                target = {k: item[k] for k in ("role_id", "task_kind", "skill_name", "dynamic_instance_ref")}
                self._verify_selection(repo, world_ref, previous, target)
            candidate, _ = self._candidate(repo, world_ref, refs[0])
            # Withdrawal does not require the release's expired ability to distribute new instructions.
            # The same live NF publisher can remove an expired release; restoring a prior version rechecks that version.
            from datetime import timedelta

            until = (repo.now() + timedelta(seconds=30)).isoformat()
            rollback_authority = self._put(
                repo,
                publisher,
                item["scope"],
                "ROLLBACK_AUTHORITY",
                {
                    "publisher": actor,
                    "valid_until": until,
                    "decision_ref": decision_ref,
                    "restore_ref": previous,
                    "reason": reason,
                },
            )
            rollback_policy = self._put(
                repo,
                publisher,
                item["scope"],
                "POLICY",
                {
                    "route": "MANUAL",
                    "purpose": "ROLLBACK",
                    "target": {
                        k: item[k] for k in ("role_id", "task_kind", "skill_name", "dynamic_instance_ref")
                    },
                    "issuer": actor,
                    "publisher": actor,
                    "valid_until": until,
                },
            )
            retained = {
                "approver": actor,
                "authority_receipt": rollback_authority,
                "policy_ref": rollback_policy,
                "valid_until": until,
            }
            result = self._decision(
                repo,
                publisher,
                candidate,
                "ROLLBACK",
                "MANUAL",
                cfg,
                op,
                case,
                reason,
                refs,
                head["selection_ref"],
                retained,
            )
            head = self._advance(repo, publisher, item["scope"], key, head["epoch"], previous, result)
            out = {
                "decision_ref": result,
                "restored_selection_ref": previous,
                "selection_ref": previous,
                "epoch": head["epoch"],
                "action": "ROLLBACK",
            }
            self.kernel._receipt(
                repo, publisher, world_ref, "INSTRUCTION_ROLLBACK", idempotency_key, request, out
            )
            return out

    def monitor(self, controller, world_ref, *, decision_ref, task_id, generation, idempotency_key):
        _idempotency(idempotency_key)
        self._enabled()
        args = {"decision_ref": decision_ref, "task_id": task_id, "generation": generation}
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            self._control(repo, controller, world_ref, write=True)
            decision = self._record(repo, world_ref, decision_ref, "DECISION")
            require(
                decision["decision"] == "APPROVE", "INVALID_EVIDENCE", "Monitor requires an approved release"
            )
            task = repo.one(
                "tasks",
                {"task_id": task_id, "work_id": world_ref["work_id"], "branch_id": world_ref["branch_id"]},
                lock="share",
            )
            require(
                task and task["generation"] == generation,
                "STALE_GENERATION",
                "Monitoring task changed generation",
            )
            spec = repo.one("task_inputs", {"task_id": task_id, "generation": generation})["spec"]
            inputs = repo.many(
                "artifacts",
                {
                    "work_id": world_ref["work_id"],
                    "kind": "AGENT_INPUT",
                    "artifact_revision_id": [r["artifact_revision_id"] for r in spec["input_artifacts"]],
                },
            )
            chosen = [
                r["payload"]["adaptive_instruction_ref"]
                for r in inputs
                if "adaptive_instruction_ref" in r["payload"]
                and r["payload"]["runtime_sha256"] == spec["runtime_contract_version"]
            ]
            require(
                len(chosen) == 1
                and self._record(repo, world_ref, chosen[0], "SELECTION")["decision_ref"] == decision_ref,
                "INVALID_EVIDENCE",
                "Task did not freeze this release",
            )
            old = self.kernel._prior(
                repo, controller, world_ref["work_id"], "INSTRUCTION_MONITOR", idempotency_key, args
            )
            if old:
                return old
            calls = repo.many(
                "call_attempts", {"task_id": task_id, "generation": generation}, order=["attempt_id"]
            )
            unknown = [c for c in calls if c["actual_amount"] is None]
            observed = {
                "decision_ref": decision_ref,
                "selection_ref": chosen[0],
                "task_id": task_id,
                "generation": generation,
                "task_state": task["state"],
                "result_id": task["current_result_id"],
                "observed_at": repo.now().isoformat(),
                "attempts": [
                    {
                        "attempt_id": c["attempt_id"],
                        "state": c["state"],
                        "actual_tokens": c["actual_tokens"],
                        "actual_amount": str(c["actual_amount"]) if c["actual_amount"] is not None else None,
                    }
                    for c in calls
                ],
                "known_cost": str(
                    sum((c["actual_amount"] for c in calls if c["actual_amount"] is not None), Decimal(0))
                ),
                "unsettled_attempts": len(unknown),
                "literary_status": "UNPROVEN",
            }
            out = {
                "monitor_ref": self._put(
                    repo,
                    controller,
                    {"world_ref": spec["world_ref"], "task_id": task_id, "generation": generation},
                    "MONITOR",
                    observed,
                ),
                **observed,
            }
            self.kernel._receipt(
                repo, controller, world_ref, "INSTRUCTION_MONITOR", idempotency_key, args, out
            )
            return out
