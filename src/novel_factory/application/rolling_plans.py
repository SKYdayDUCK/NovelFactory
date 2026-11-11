"""Opt-in bounded editorial decisions on the existing PlanRevision lineage."""

from copy import deepcopy
from uuid import uuid4

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import require
from novel_factory.application.planning import PlanningService
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.scheduling import after
from novel_factory.domain.temporal import point_key
from novel_factory.orchestration.workflow import review_acceptance


def _selection(repo, work, name):
    row = repo.one("selections", {"work_id": work, "selection_kind": name})
    return row["artifact_ref"] if row else None


def _decision_name(change, candidate):
    return (
        "ROLLING_REVIEW:" + change["artifact_revision_id"] + ":" + candidate["artifact_revision_id"]
    )


def _anchor_name(wr, volume):
    return f"REFERENCE_TIMELINE:{wr['branch_id']}:{volume}"


def _first(node):
    target = node["target_time"]
    if target["kind"] == "UNSPECIFIED":
        return None
    return point_key(target["at"] if target["kind"] == "EXACT" else target["earliest"])


def _future_window(plan, after, count):
    protected = set(plan["published_prefix_node_ids"])
    candidates = [
        n
        for n in plan["nodes"]
        if n["node_id"] not in protected
        and n["status"] not in {"REALIZED", "ABANDONED"}
        and _first(n) is not None
        and _first(n) > point_key(after)
    ]
    # Python's stable sort preserves the adopted order for a tie.
    return [n["node_id"] for n in sorted(candidates, key=_first)[:count]]


class RollingPlanning:
    def __init__(self, kernel):
        self.kernel = kernel
        self.plans = PlanningService(kernel)

    def enable(self, controller, wr, volume, *, scenes, idempotency_key):
        """Pin the actual first adopted revision, including pre-P5 lineages."""
        validate_contract("UUID", volume)
        require(
            type(scenes) is int and 1 <= scenes <= 32,
            "INVALID_ARGUMENT",
            "Window size must be 1..32",
        )
        self.plans._key(idempotency_key)
        request = {"world_ref": wr, "volume_id": volume, "scene_limit": scenes}
        name = _anchor_name(wr, volume)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": wr["work_id"]}, lock="update")
            self.plans._authorize(repo, controller, wr, capability="ADMIN")
            old = self.kernel._prior(
                repo, controller, wr["work_id"], "ROLLING_ENABLE", idempotency_key, request
            )
            if old:
                return old
            require(
                self.kernel.head(repo, wr["work_id"], wr["branch_id"]) == wr,
                "HEAD_CONFLICT",
                "Recheck the current accepted world",
            )
            require(
                not _selection(repo, wr["work_id"], name),
                "REFERENCE_FROZEN",
                "Reference is already frozen",
            )
            ref = _selection(repo, wr["work_id"], self.plans.selection_key(volume))
            require(ref, "PLAN_SELECTION_CONFLICT", "Select a volume plan first")
            while True:
                plan = self.kernel.artifact(repo, ref, wr["work_id"], kind="PLAN_REVISION")[
                    "payload"
                ]
                require(
                    repo.ancestor(plan["input_world_ref"], wr),
                    "BRANCH_CHANGED",
                    "Different reference branch",
                )
                if plan["prior_plan_ref"] is None:
                    break
                ref = plan["prior_plan_ref"]
            payload = {**request, "initial_plan_ref": ref}
            anchor = self.kernel._blob(payload)
            self.kernel._register_artifact(
                repo, anchor, wr["work_id"], payload, "REFERENCE_TIMELINE", controller.principal_id
            )
            repo.add(
                "selections",
                {
                    "work_id": wr["work_id"],
                    "selection_kind": name,
                    "artifact_ref": anchor,
                    "epoch": 1,
                },
            )
            self.kernel._receipt(
                repo, controller, wr, "ROLLING_ENABLE", idempotency_key, request, anchor
            )
            return anchor

    def _role_task(self, repo, principal, wr, binding, *, role, modes, volume, inputs):
        task, context = self.kernel._task_binding(
            repo, principal, binding["task_id"], binding["generation"]
        )
        grant, _ = self.plans._authorize(repo, principal, wr)
        require(
            task["lease_owner"] == binding["worker_id"] and bool(binding["worker_id"]),
            "LEASE_OWNER_MISMATCH",
            "Execution is owned by a different worker",
        )
        spec = repo.one(
            "task_inputs", {"task_id": task["task_id"], "generation": task["generation"]}
        )["spec"]
        cut = context["read_cut"]
        manifest = _selection(repo, wr["work_id"], "PLAN_MANIFEST:" + volume)
        work = repo.one("works", {"work_id": wr["work_id"]})
        require(
            task["world_ref"] == wr
            and spec["role_id"] == grant["role_id"] == role
            and spec["task_kind"] in modes
            and all(ref in spec["input_artifacts"] for ref in inputs)
            and cut["permission_epoch"] == grant["epoch"]
            and spec["policy_version"] == work["policy_version"]
            and manifest
            and cut["knowledge_cut"] == {"plane": "PLAN", "plan_manifest_ref": manifest},
            "VIEW_DENIED",
            "Current editorial task does not cover this decision",
        )
        return spec, grant

    def request_change(
        self,
        director,
        wr,
        *,
        task_id,
        generation,
        worker_id,
        volume,
        prior_plan_ref,
        event_refs,
        reason,
        idempotency_key,
        assessment=None,
    ):
        """Record the volume director's causal account after world acceptance."""
        validate_contract("UUID", volume)
        require(
            isinstance(reason, str)
            and 0 < len(reason.strip()) <= 10000
            and isinstance(event_refs, list)
            and 1 <= len(event_refs) <= 32,
            "INVALID_ARGUMENT",
            "A reason and selected event evidence are required",
        )
        for ref in event_refs:
            validate_contract("EventRef", ref)
        binding = {"task_id": task_id, "generation": generation, "worker_id": worker_id}
        with self.kernel.db.uow(write=False) as repo:
            self.plans._authorize(repo, director, wr)
            anchor_ref = _selection(repo, wr["work_id"], _anchor_name(wr, volume))
            require(anchor_ref, "PLAN_SELECTION_CONFLICT", "Enable rolling planning first")
            anchor = self.kernel.artifact(
                repo, anchor_ref, wr["work_id"], kind="REFERENCE_TIMELINE"
            )["payload"]
            prior = self.kernel.artifact(repo, prior_plan_ref, wr["work_id"], kind="PLAN_REVISION")[
                "payload"
            ]
            spec, grant = self._role_task(
                repo,
                director,
                wr,
                binding,
                role="volume_director",
                modes={"volume_review"},
                volume=volume,
                inputs=[prior_plan_ref, anchor_ref],
            )
            receipt = review_acceptance(
                repo.conn, {**binding, "work_id": wr["work_id"], "branch_id": wr["branch_id"]}, spec
            )
            require(
                receipt["world_ref"] == wr
                and all(ref in receipt["accepted_event_refs"] for ref in event_refs),
                "INVALID_EVIDENCE",
                "Evidence must originate from the accepted joint decision that woke this task",
            )
            after = spec["story_time"]
            window = _future_window(prior, after, anchor["scene_limit"])
            require(window, "NO_REPLAN_IMPACT", "There are no dated future nodes in this window")
            payload = {
                **binding,
                "input_world_ref": wr,
                "prior_plan_ref": prior_plan_ref,
                "volume_id": volume,
                "reference_timeline_ref": anchor_ref,
                "initial_plan_ref": anchor["initial_plan_ref"],
                "after": after,
                "node_ids": window,
                "event_refs": event_refs,
                "reason": reason,
                "grant_id": director.grant_id,
                "permission_epoch": grant["epoch"],
                "policy_version": spec["policy_version"],
            }

            # Omitted by older MCP callers: retain the byte-identical original
            # command payload so a pre-upgrade idempotency receipt still matches.
            if assessment is not None:
                from novel_factory.application.deviation_review import validate_assessment
                payload["assessment"] = validate_assessment(self.kernel, repo, spec, binding,
                    anchor["initial_plan_ref"], prior, window, event_refs, assessment)
                payload["acceptance_command_id"] = receipt["command_id"]

        def check(repo, revision):
            require(
                _selection(repo, wr["work_id"], self.plans.selection_key(volume)) == prior_plan_ref
                and self.kernel.head(repo, wr["work_id"], wr["branch_id"]) == wr,
                "PLAN_SELECTION_CONFLICT",
                "Review current world and plan",
            )
            self.plans._events(revision, event_refs)
            base = self.kernel.revision(repo, prior["input_world_ref"])
            require(
                repo.ancestor(prior["input_world_ref"], wr),
                "BRANCH_CHANGED",
                "Different plan branch",
            )
            for ref in event_refs:
                require(
                    base["selection"].get(ref["occurrence_id"]) != ref["event_version_id"],
                    "INVALID_EVIDENCE",
                    "A deviation requires evidence accepted since the active plan",
                )
                event = repo.one("events", {"work_id": wr["work_id"], **ref})["payload"]
                timing = event["occurrence_time"]
                end = timing.get("at", timing.get("latest"))
                require(
                    end is not None and point_key(end) <= point_key(after),
                    "INVALID_EVIDENCE",
                    "Review cannot cite an event beyond its story time",
                )

        return self.plans._save(
            director,
            wr,
            payload,
            "CHANGE_POINT",
            idempotency_key,
            check,
            authorize_request=lambda repo: self._role_task(
                repo,
                director,
                wr,
                binding,
                role="volume_director",
                modes={"volume_review"},
                volume=volume,
                inputs=[prior_plan_ref, anchor_ref],
            ),
        )

    def propose_window(
        self,
        planner,
        wr,
        *,
        task_id,
        generation,
        worker_id,
        change_point_ref,
        window,
        idempotency_key,
    ):
        """Translate a role's WindowPlan into new goals in the authorized scene slots."""
        self.plans._key(idempotency_key)
        window = validate_contract("WindowPlan", window, "literary")
        binding = {"task_id": task_id, "generation": generation, "worker_id": worker_id}
        request = {
            **binding,
            "world_ref": wr,
            "change_point_ref": change_point_ref,
            "window": window,
        }
        work = wr["work_id"]

        def inspect(repo):
            self.plans._authorize(repo, planner, wr)
            cp = self.kernel.artifact(repo, change_point_ref, work, kind="CHANGE_POINT")["payload"]
            self._role_task(
                repo,
                planner,
                wr,
                binding,
                role="volume_director",
                modes={"replan"},
                volume=cp["volume_id"],
                inputs=[cp["reference_timeline_ref"], cp["prior_plan_ref"], change_point_ref],
            )
            require(
                cp["input_world_ref"] == wr, "HEAD_CONFLICT", "Change point belongs to another cut"
            )
            return cp

        with self.kernel.db.uow(write=False) as repo:
            cp = inspect(repo)
            prior_receipt = self.kernel._prior(
                repo, planner, work, "ROLLING_WINDOW", idempotency_key, request
            )
            if prior_receipt:
                return prior_receipt
            prior = self.kernel.artifact(repo, cp["prior_plan_ref"], work, kind="PLAN_REVISION")[
                "payload"
            ]
        goals = window["candidate_goals"]
        require(
            0 < len(goals) <= len(cp["node_ids"]),
            "WINDOW_CONFLICT",
            "There must be one goal per revised scene slot",
        )
        updates = dict(zip(cp["node_ids"][: len(goals)], goals, strict=True))
        plan = deepcopy(prior)
        for node in plan["nodes"]:
            if node["node_id"] in updates:
                node.update(goal=updates[node["node_id"]], status="PROPOSED")
        plan.update(
            input_world_ref=wr,
            prior_plan_ref=cp["prior_plan_ref"],
            plan_revision_id=str(uuid4()),
            decision={
                "reason": window["next_decision_reason"],
                "considered_options": [
                    {
                        "option_id": str(uuid4()),
                        "summary": "采用此次有限窗口",
                        "disposition": "SELECTED",
                        "reason": window["next_decision_reason"],
                    },
                    {
                        "option_id": str(uuid4()),
                        "summary": "沿用先前场次",
                        "disposition": "REJECTED",
                        "reason": cp["reason"],
                    },
                ],
            },
        )
        validate_contract("PlanRevision", plan, "planning")
        plan_ref, window_ref = self.kernel._blob(plan), self.kernel._blob(window)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            repo.one("works", {"work_id": work}, lock="update")
            cp = inspect(repo)
            old = self.kernel._prior(
                repo, planner, work, "ROLLING_WINDOW", idempotency_key, request
            )
            if old:
                return old
            require(
                self.kernel.head(repo, work, wr["branch_id"]) == wr,
                "HEAD_CONFLICT",
                "World changed during planning",
            )
            revision = self.kernel.revision(repo, wr)
            self.plans._validate_plan(repo, revision, plan)
            self.plans._validate_window(repo, revision, window, prior["volume_contract_ref"])
            self._check_window(repo, wr, cp, plan, window)
            self.kernel._register_artifact(
                repo, plan_ref, work, plan, "PLAN_REVISION", planner.principal_id
            )
            self.kernel._register_artifact(
                repo, window_ref, work, window, "WINDOW_PLAN", planner.principal_id
            )
            result = {
                "plan_ref": plan_ref,
                "window_ref": window_ref,
                "change_point_ref": change_point_ref,
            }
            self.kernel._receipt(
                repo, planner, wr, "ROLLING_WINDOW", idempotency_key, request, result
            )
            repo.outbox(
                "planning.window_proposed",
                "volume",
                cp["volume_id"],
                result,
                "window:" + window_ref["artifact_revision_id"],
            )
            return result

    def review(
        self,
        editor,
        wr,
        *,
        task_id,
        generation,
        worker_id,
        change_point_ref,
        plan_ref,
        window_ref,
        decision,
        reason,
        idempotency_key,
    ):
        require(
            decision in {"APPROVED", "RETURNED"}
            and isinstance(reason, str)
            and 0 < len(reason.strip()) <= 10000,
            "INVALID_ARGUMENT",
            "An editorial decision and reason are required",
        )
        binding = {"task_id": task_id, "generation": generation, "worker_id": worker_id}
        with self.kernel.db.uow(write=False) as repo:
            self.plans._authorize(repo, editor, wr)
            row = self.kernel.artifact(repo, change_point_ref, wr["work_id"], kind="CHANGE_POINT")
            cp = row["payload"]
            require(
                row["created_by"] != editor.principal_id and cp["input_world_ref"] == wr,
                "VIEW_DENIED",
                "Review requires an independent editor on the accepted cut",
            )
            inputs = [change_point_ref, plan_ref, window_ref, cp["reference_timeline_ref"]]

            def authorize(repo):
                return self._role_task(
                    repo,
                    editor,
                    wr,
                    binding,
                    role="chief_editor",
                    modes={"editorial_decision"},
                    volume=cp["volume_id"],
                    inputs=inputs,
                )

            spec, grant = authorize(repo)
        payload = {
            **binding,
            "change_point_ref": change_point_ref,
            "plan_ref": plan_ref,
            "window_ref": window_ref,
            "input_world_ref": wr,
            "decision": decision,
            "reason": reason,
            "grant_id": editor.grant_id,
            "permission_epoch": grant["epoch"],
            "policy_version": spec["policy_version"],
        }

        def check(repo, revision):
            self._validate_candidate(repo, wr, cp, plan_ref, window_ref)
            require(
                self.kernel.head(repo, wr["work_id"], wr["branch_id"]) == wr,
                "HEAD_CONFLICT",
                "World changed during upper planning review",
            )

        return self.plans._save(
            editor,
            wr,
            payload,
            "CHANGE_POINT_REVIEW",
            idempotency_key,
            check,
            authorize_request=authorize,
            selection_name=_decision_name(change_point_ref, plan_ref),
        )

    def _validate_candidate(self, repo, wr, cp, plan_ref, window_ref, *, composing=False):
        work = wr["work_id"]
        plan = self.kernel.artifact(repo, plan_ref, work, kind="PLAN_REVISION")["payload"]
        window = self.kernel.artifact(repo, window_ref, work, kind="WINDOW_PLAN")["payload"]
        self._check_window(repo, wr, cp, plan, window, composing=composing)

    def _check_window(self, repo, wr, cp, plan, window, *, composing=False):
        work = wr["work_id"]
        require(
            (composing or _selection(repo, work, self.plans.selection_key(cp["volume_id"]))
             == cp["prior_plan_ref"])
            and _selection(repo, work, _anchor_name(wr, cp["volume_id"]))
            == cp["reference_timeline_ref"],
            "PLAN_SELECTION_CONFLICT",
            "Active reference or rolling plan changed",
        )
        old = self.kernel.artifact(repo, cp["prior_plan_ref"], work, kind="PLAN_REVISION")[
            "payload"
        ]
        contract = self.kernel.artifact(
            repo, old["volume_contract_ref"], work, kind="VOLUME_CONTRACT"
        )["payload"]
        require(
            plan["prior_plan_ref"] == cp["prior_plan_ref"]
            and plan["input_world_ref"] == wr
            and plan["dependencies"] == old["dependencies"]
            and [n["node_id"] for n in plan["nodes"]] == [n["node_id"] for n in old["nodes"]],
            "WINDOW_CONFLICT",
            "Keep the existing scene slots and dependency graph",
        )
        changed = [
            new for new, prior in zip(plan["nodes"], old["nodes"], strict=True) if new != prior
        ]
        require(
            changed
            and all(n["node_id"] in cp["node_ids"] and n["status"] != "REALIZED" for n in changed)
            and all(
                new["target_time"] == prior["target_time"]
                for new, prior in zip(plan["nodes"], old["nodes"])
            ),
            "WINDOW_CONFLICT",
            "Only named future scenes may change",
        )
        ordered_goals = {n["node_id"]: n["goal"] for n in changed}
        require(
            window["input_world_ref"] == wr
            and window["volume_contract_revision_id"] == contract["contract_revision_id"]
            and window["adopted_event_refs"] == cp["event_refs"]
            and window["candidate_goals"]
            == [ordered_goals[key] for key in cp["node_ids"] if key in ordered_goals],
            "WINDOW_CONFLICT",
            "Window must describe precisely the reviewed scenes and accepted causes",
        )
        start, end = (point_key(window["story_interval"][k]) for k in ("start", "end"))
        slots = [n for n in old["nodes"] if n["node_id"] in cp["node_ids"]]
        latest = max(
            point_key(n["target_time"].get("at") or n["target_time"]["latest"]) for n in slots
        )
        after_latest = point_key(after({"tick": latest[0], "phase": latest[1]}))
        require(
            point_key(cp["after"]) < start
            and all(
                start <= _first(n)
                and point_key(n["target_time"].get("at") or n["target_time"]["latest"]) < end
                for n in changed
            )
            and end <= after_latest,
            "WINDOW_CONFLICT",
            "Window interval exceeds authorized future scenes",
        )

    def finalize_adoption(self, repo, wr, plan, plan_ref, review_ref, *, composing=False):
        """Revalidate the decision and close the editorial tasks inside the adoption transaction."""
        anchor = _selection(repo, wr["work_id"], _anchor_name(wr, plan["volume_id"]))
        if anchor is None:
            require(review_ref is None, "PLAN_SELECTION_CONFLICT", "No rolling timeline registered")
            return None
        require(
            review_ref is not None,
            "REVIEW_REQUIRED",
            "Rolling selection requires an upper planning decision",
        )
        row = self.kernel.artifact(repo, review_ref, wr["work_id"], kind="CHANGE_POINT_REVIEW")
        review = row["payload"]
        require(
            review["decision"] == "APPROVED"
            and review["plan_ref"] == plan_ref
            and review["input_world_ref"] == wr
            and _selection(
                repo, wr["work_id"], _decision_name(review["change_point_ref"], plan_ref)
            )
            == review_ref,
            "REVIEW_REQUIRED",
            "Decision does not approve this candidate",
        )
        origin = self.kernel.artifact(
            repo, review["change_point_ref"], wr["work_id"], kind="CHANGE_POINT"
        )
        cp = origin["payload"]
        for artifact in (origin, row):
            record = artifact["payload"]
            grant = repo.one("grants", {"grant_id": record["grant_id"]}, lock="share")
            principal = Principal(record["grant_id"], artifact["created_by"])
            self.plans._authorize(repo, principal, wr)
            task = repo.one("tasks", {"task_id": record["task_id"]}, lock="share")
            require(
                grant["epoch"] == record["permission_epoch"]
                and record["policy_version"]
                == repo.one("works", {"work_id": wr["work_id"]})["policy_version"]
                and task["deadline"] > repo.now()
                and task["generation"] == record["generation"]
                and task["state"]
                in {"RUNNING", "RESULT_STORED", "VALIDATING", "WAITING_REVIEW", "ACCEPTED"},
                "REVIEW_REQUIRED",
                "Editorial authorization or task generation changed",
            )
            if task["state"] != "ACCEPTED":
                self.kernel.tasks.guard_task(
                    repo.conn, task["task_id"], task["generation"], record["worker_id"]
                )
        self._validate_candidate(repo, wr, cp, plan_ref, review["window_ref"], composing=composing)
        # Close each editorial task in the same transaction as adoption. The
        # corresponding Outbox events can safely release a later planning stage.
        for record in (cp, review):
            task = repo.one("tasks", {"task_id": record["task_id"]})
            if task["state"] != "ACCEPTED":
                self.kernel.tasks.accept_in_transaction(
                    repo.conn, task["task_id"], task["generation"]
                )
        return {
            "reference_timeline_ref": anchor,
            "initial_plan_ref": cp["initial_plan_ref"],
            "change_point_ref": review["change_point_ref"],
            "review_ref": review_ref,
            "window_ref": review["window_ref"],
        }
