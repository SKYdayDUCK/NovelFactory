"""Unpaid, paired replay of frozen engineering checks on independently reviewed manuscript versions."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from time import perf_counter_ns
from uuid import UUID, uuid5

from novel_factory.application import adaptive_runtime
from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import require
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain import creative_review
from novel_factory.domain.canonical import canonical_bytes, fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.creative_config import CreativeHarnessConfig

ARMS = ("CONTROL", "TREATMENT")
REPLAY_MODEL_CONFIG = {
    "kind": "LOCAL_DETERMINISTIC_NF_ENGINEERING_REPLAY",
    "version": 1,
    "provider": None,
    "model_tokens": 0,
    "max_case_bytes": 2_000_000,
    "max_pairs": 256,
    "reader": "First grounded suggestion from the assigned NF rubric; eligible additions use the live production review function.",
    "engine_source_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
    "review_implementation_sha256": sha256(Path(creative_review.__file__).read_bytes()).hexdigest(),
    "instruction_renderer_sha256": sha256(Path(adaptive_runtime.__file__).read_bytes()).hexdigest(),
}
REPLAY_RUBRIC = {
    "version": 1,
    "review": "Match exact historical pattern and Unicode span within the assigned independent review layer.",
    "context": "Preserve all authorized sections and values; compare first grounded evidence position in the rendered material order.",
    "release": "Both arms complete on every case, no worsened case, disjoint positive and negative controls, at least one held-out improvement.",
    "literary_conclusion": "UNPROVEN",
}
REVIEW_ROLES = {"reviewer", "literary_editor", "chief_editor"}
ALL_PATTERNS = {pattern for allowed in creative_review.RUBRICS.values() for pattern in allowed}
AUTO_PATTERNS = {
    "ENGINEERING_SERIALIZATION",
    "REPEATED_NAME_FORM",
    "SHARED_VOICE",
    "MECHANICAL_CADENCE",
    "REPEATED_SEAM",
    "REPEATED_BEAT",
    "WORLD_CONTRADICTION",
    "POV_LEAK",
}


def _fixed_id(ref, label):
    return str(uuid5(UUID(ref["artifact_revision_id"]), "adaptive-replay:" + label))


def _ref(row):
    return {k: row[k] for k in ("artifact_id", "artifact_revision_id", "sha256")}


def _read(kernel, repo, world, ref, kind):
    record = kernel.artifact(repo, ref, world["work_id"], kind=kind)
    value = record["payload"]
    require(
        value["scope"]["world_ref"] == world and fingerprint(value) == ref["sha256"],
        "INVALID_EVIDENCE",
        "Evaluation record differs from its frozen work or source",
    )
    kernel.objects.verify(ref["sha256"])
    return value


def _put(kernel, repo, world, controller, kind, value, identifier):
    ref = kernel._blob(value)
    ref["artifact_revision_id"] = identifier
    kernel._register_artifact(repo, ref, world["work_id"], value, kind, controller.principal_id)
    return ref


def _source_api(kernel):
    from .adaptive_instructions import AdaptiveInstructionService

    return AdaptiveInstructionService(kernel)


def _blocks(arm):
    if "blocks" in arm:
        return arm["blocks"]
    return [arm] if arm.get("body") else []


def _review_patterns(blocks):
    # The execution language is deliberately narrow. Ordinary prose cannot be
    # credited with improving a literary work by an automatic rubric.
    selected = sorted(ALL_PATTERNS)
    for block in blocks:
        markers = [
            s.removeprefix("NF_REVIEW_CHECKS:").strip()
            for s in block["body"].splitlines()
            if s.startswith("NF_REVIEW_CHECKS:")
        ]
        if not markers and block["change_kind"] == "CONTEXT_RECIPE":
            continue
        if not markers and block["change_kind"] == "REVIEW_RUBRIC":
            # This is the precise structured list passed by NF's instruction renderer.
            checks = block["review_checks"]
        else:
            require(
                len(markers) == 1, "UNSUPPORTED_INSTRUCTION", "No unambiguous executable review directive"
            )
            try:
                checks = json.loads(markers[0])
            except (ValueError, RecursionError):
                raise DomainError("UNSUPPORTED_INSTRUCTION", "Invalid review directive") from None
        require(
            isinstance(checks, list)
            and all(isinstance(c, str) and c in ALL_PATTERNS for c in checks)
            and len(checks) == len(set(checks))
            and checks == block["review_checks"],
            "UNSUPPORTED_INSTRUCTION",
            "Instruction bytes and applicable review checks do not match",
        )
        selected = checks
    return set(selected)


def _input(sample, kind):
    # Only model-visible inputs enter the runner. Labels and historical verdicts
    # are used exclusively by the independent comparison below.
    if kind != "REVIEW_RUBRIC":
        return {k: sample.get(k) for k in ("role_model_input", "material_budget")}
    return {
        k: sample.get(k)
        for k in (
            "text_ref",
            "text",
            "ordered_texts",
            "layer",
            "creative_materials",
            "material_budget",
            "verified_evidence",
        )
    }


def _material_sections(materials):
    if isinstance(materials, dict):
        return materials
    require(isinstance(materials, list), "UNSUPPORTED_SAMPLE", "Missing source material sections")
    result = {section["section"]: section["items"] for section in materials}
    require(len(result) == len(materials), "SCOPE_MISMATCH", "Repeated source material section")
    return result


def _execute(arm, material, kind):
    blocks = _blocks(arm)
    role = arm.get("base_role_instructions", arm["role_instructions"])
    skill = arm.get("base_skill_instructions", arm["skill_instructions"])
    system = role + "\n" + skill + adaptive_runtime.render_instruction_blocks(blocks)
    require(
        len(canonical_bytes(material)) + len(system.encode()) <= REPLAY_MODEL_CONFIG["max_case_bytes"],
        "CAPACITY_LIMIT",
        "This frozen replay input exceeds the engine bound",
    )
    if kind != "REVIEW_RUBRIC":
        original = material["role_model_input"]
        require(
            isinstance(original, dict)
            and "creative_materials" in original
            and type(material["material_budget"]) is int,
            "UNSUPPORTED_SAMPLE",
            "No authentic frozen creative materials for this case",
        )
        data = _material_sections(original["creative_materials"])
        materials = adaptive_runtime.organize_materials(data, blocks)
        user = {**original, "creative_materials": materials}
        encoded = canonical_bytes(user)
        require(
            len(encoded) <= material["material_budget"],
            "CAPACITY_LIMIT",
            "Rendered delivery exceeds the frozen context allowance",
        )
        if kind == "CONTEXT_RECIPE":
            output = {"materials": materials, "user_sha256": fingerprint(user), "user_bytes": len(encoded)}
        else:
            # Consume the actual rendered envelope locally. There is no model or
            # independently assessed downstream manuscript in this delivery probe.
            received = json.loads(canonical_bytes({"system": system, "user": user}))
            decoded = received["user"]
            scoped = {**decoded, "creative_materials": _material_sections(decoded["creative_materials"])}
            output = {
                "delivery": "LOCAL_RENDER_READINESS",
                "scope_preserved": received["system"] == system
                and scoped == {**original, "creative_materials": data},
                "user_sha256": fingerprint(decoded),
                "user_bytes": len(encoded),
                "system_bytes": len(system.encode()),
                "creative_outcome": "UNSCORABLE",
            }
    else:
        require(
            arm["role_id"] in REVIEW_ROLES,
            "UNSUPPORTED_INSTRUCTION",
            "This engineering reader cannot evaluate free-form creation by this role",
        )
        layer = material["layer"]
        require(
            any(p in AUTO_PATTERNS for p in creative_review.RUBRICS[layer]),
            "UNSUPPORTED_SAMPLE",
            "No deterministic native check for this review layer",
        )
        text_ref, text = material["text_ref"], material["text"]
        ordered = [{"text_ref": v["text_ref"], "text": v["text"]} for v in material["ordered_texts"]]
        sources = {r["text_ref"]["artifact_revision_id"]: (r["text_ref"], r["text"]) for r in ordered}
        verified = material["verified_evidence"] or []
        # This bounded engineering reader submits one specific preliminary
        # observation. Both arms then use the same production S3 submission
        # function, including its treatment of additional authorized checks.
        observations = creative_review._suggestions(layer, text_ref, text, sources, verified)
        priority = _review_patterns(
            [b for b in blocks if b["change_kind"] in {"ROLE_INSTRUCTIONS", "SKILL_INSTRUCTIONS"}]
        )
        first = next((o for o in observations if o["pattern"] in priority), None)
        if kind == "REVIEW_RUBRIC":
            _review_patterns([b for b in blocks if b["change_kind"] == "REVIEW_RUBRIC"])
        items = adaptive_runtime.review_with_guidance(
            layer,
            text_ref,
            text,
            [first] if first else [],
            related_texts=ordered,
            verified_evidence=verified,
            blocks=blocks,
        )
        output = {
            "findings": [
                {
                    "pattern": v["pattern"],
                    "text_ref": v["finding"]["target_ref"],
                    "span": v["finding"]["text_span"],
                    "judgment": v["finding"]["judgment"],
                    "evidence_refs": v["finding"]["evidence_refs"],
                }
                for v in items
            ]
        }
    return {"system_sha256": sha256(system.encode()).hexdigest(), "payload": output}


def _score(sample, output, kind):
    if output.get("delivery") == "LOCAL_RENDER_READINESS":
        require(
            output["scope_preserved"] and output["creative_outcome"] == "UNSCORABLE",
            "SCOPE_MISMATCH",
            "Replay changed the scoped input",
        )
        return {
            "false_positives": 0,
            "false_negatives": 0,
            "evidence_position": 0,
            "positives": 0,
            "detected": 0,
            "content_conserved": True,
            "readiness_only": True,
        }
    if kind == "CONTEXT_RECIPE":
        original = sample["creative_materials"]
        organized = output["materials"]
        sections = (
            [{"section": k, "items": organized[k]} for k in sorted(organized)]
            if isinstance(organized, dict)
            else organized
        )
        actual = {s["section"]: s["items"] for s in sections}
        require(
            len(actual) == len(sections) and actual == original,
            "SCOPE_MISMATCH",
            "Authorized material content changed",
        )
        citations = sample.get("context_evidence_refs", [])
        indexes = {}
        for ref in citations:
            # The writer sees literary bytes. Only the S1 packet's immutable
            # source/content link can connect these bytes with an S3 citation.
            match = next(
                (
                    i
                    for i, s in enumerate(sections)
                    if ref in sample["section_source_refs"].get(s["section"], ())
                ),
                None,
            )
            if match is not None:
                indexes[fingerprint(ref)] = match
        if citations:
            require(indexes, "UNSUPPORTED_SAMPLE", "Cited evidence is absent from the authorized material")
        return {
            "false_positives": 0,
            "false_negatives": 0,
            "evidence_position": sum(indexes.values()),
            "evidence_ranks": indexes,
            "positives": len(indexes),
            "detected": len(indexes),
            "content_conserved": True,
        }
    expected = {(f["pattern"], f["start"], f["end"]) for f in sample["expected_findings"]}
    require(
        all(pattern in AUTO_PATTERNS for pattern, _, _ in expected),
        "UNSUPPORTED_SAMPLE",
        "Historical judgment needs a reader unavailable in the engineering replay",
    )
    emitted = {(f["pattern"], f["span"]["start"], f["span"]["end"]) for f in output["findings"]}
    return {
        "false_positives": len(emitted - expected),
        "false_negatives": len(expected - emitted),
        "evidence_position": 0,
        "positives": len(expected),
        "detected": len(expected & emitted),
        "content_conserved": True,
    }


def _quality(rows):
    complete = all(row["status"] == "SUCCEEDED" for row in rows)
    pairs = {}
    for row in rows:
        pairs.setdefault((row["case_id"], row["repetition"]), {})[row["arm_id"]] = row
    negatives, positives, improved, regressed = set(), set(), set(), set()
    readiness_only = False
    totals = {
        arm: {
            "executions": 0,
            "failures": 0,
            "false_positives": 0,
            "false_negatives": 0,
            "evidence_position": 0,
            "latency_ns": 0,
            "native_rework_count": 0,
            "true_positives": 0,
            "provider_tokens": 0,
            "amount": "0",
        }
        for arm in ARMS
    }
    for row in rows:
        t = totals[row["arm_id"]]
        t["executions"] += int(row["executed"])
        t["failures"] += int(row["status"] != "SUCCEEDED")
        t["latency_ns"] += row["latency_ns"]
        if row["score"]:
            t["true_positives"] += row["score"]["detected"]
            for dimension in ("false_positives", "false_negatives", "evidence_position"):
                t[dimension] += row["score"][dimension]
    for (case_id, _), pair in pairs.items():
        require(set(pair) == set(ARMS), "INVALID_EVIDENCE", "Each preregistered case needs paired outcomes")
        a, b = (pair[arm] for arm in ARMS)
        require(
            a["input_sha256"] == b["input_sha256"]
            and a["held_out"] == b["held_out"]
            and a["seed"] == b["seed"]
            and a["controls_sha256"] == b["controls_sha256"]
            and a["dependencies_sha256"] == b["dependencies_sha256"],
            "INVALID_EVIDENCE",
            "Paired controls differ",
        )
        if a["status"] != "SUCCEEDED" or b["status"] != "SUCCEEDED":
            continue
        if a["score"].get("readiness_only") or b["score"].get("readiness_only"):
            readiness_only = True
            continue
        ranks_a, ranks_b = (r["score"].get("evidence_ranks", {}) for r in (a, b))
        require(ranks_a.keys() == ranks_b.keys(), "SCOPE_MISMATCH", "Arms did not conserve cited sources")
        differences = [
            b["score"][k] - a["score"][k] for k in ("false_positives", "false_negatives", "evidence_position")
        ]
        differences.extend(ranks_b[key] - rank for key, rank in ranks_a.items())
        if any(d > 0 for d in differences):
            regressed.add(case_id)
        if a["held_out"] and a["independent_label"] and b["independent_label"]:
            (positives if a["score"]["positives"] else negatives).add(case_id)
            if all(d <= 0 for d in differences) and any(d < 0 for d in differences):
                improved.add(case_id)
    reasons = []
    for ok, reason in [
        (complete, "INCOMPLETE_OR_FAILED"),
        (not readiness_only, "READINESS_ONLY_NO_INDEPENDENT_OUTPUT"),
        (not regressed, "CASE_REGRESSION"),
        (positives, "NO_HELD_OUT_POSITIVE"),
        (negatives, "NO_HELD_OUT_NEGATIVE"),
        (improved, "NO_HELD_OUT_GAIN"),
    ]:
        if not ok:
            reasons.append(reason)
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "held_out_positives": sorted(positives),
        "held_out_negatives": sorted(negatives),
        "improved": sorted(improved),
        "regressed": sorted(regressed),
    }, totals


class AdaptiveEvaluationService:
    def __init__(self, kernel, *, config=None):
        self.kernel = kernel
        self.config = config

    def _authorize(self, repo, controller, world):
        grant = self.kernel.authorize(repo, controller, world["work_id"], "ADMIN", world["branch_id"])
        return {
            "principal_id": controller.principal_id,
            "grant_id": controller.grant_id,
            "permission_epoch": grant["epoch"],
        }

    @staticmethod
    def _lock(repo, key):
        from sqlalchemy import text

        repo.conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key": "nf-adaptive-run:" + key}
        )

    @staticmethod
    def _freeze(snapshot):
        from .adaptive_instructions import deployed_module_digests

        require(
            snapshot["design"]["release"]["module_sha256"] == deployed_module_digests(),
            "EVALUATION_FREEZE_MISMATCH",
            "The deployed replay and delivery implementation changed",
        )
        freeze = validate_contract("ExperimentFreeze", snapshot["freeze"], "harness")
        candidate = validate_contract("InstructionCandidate", snapshot["candidate"], "harness")
        require(
            freeze["design"] == "A_B"
            and freeze["model_config_sha256"] == fingerprint(REPLAY_MODEL_CONFIG)
            and freeze["works"]
            and freeze["works"][0] == candidate["scope"]["world_ref"]
            and all(
                (work["work_id"], work["branch_id"])
                == (freeze["works"][0]["work_id"], freeze["works"][0]["branch_id"])
                for work in freeze["works"]
            )
            and [a["arm_id"] for a in freeze["arms"]] == list(ARMS),
            "EVALUATION_FREEZE_MISMATCH",
            "Experiment does not freeze this paired engineering runner",
        )
        samples = {s["case_id"]: s for s in snapshot["samples"]}
        schedule = snapshot["allocation"]
        repetitions = freeze["repetitions"]
        require(
            samples
            and len(samples) == len(snapshot["samples"])
            and len(schedule) == len(samples) * repetitions <= REPLAY_MODEL_CONFIG["max_pairs"],
            "CAPACITY_LIMIT",
            "Invalid frozen sample count or repetition bound",
        )
        keys = set()
        blind = set()
        for pair in schedule:
            key = (pair["case_id"], pair["repetition"])
            require(
                pair["case_id"] in samples
                and 1 <= pair["repetition"] <= repetitions
                and key not in keys
                and sorted(pair["order"]) == sorted(ARMS)
                and len(pair["blind_ids"]) == 2
                and len(set(pair["blind_ids"]) - blind) == 2,
                "EVALUATION_FREEZE_MISMATCH",
                "Invalid or duplicate preregistered pair",
            )
            keys.add(key)
            blind.update(pair["blind_ids"])
        return freeze, samples, schedule

    def _saved(self, repo, world, identifier, kind):
        row = repo.one(
            "artifacts", {"work_id": world["work_id"], "artifact_revision_id": identifier, "kind": kind}
        )
        if row:
            ref = _ref(row)
            return ref, _read(self.kernel, repo, world, ref, kind)
        return None, None

    def _start(self, controller, world, candidate_ref, snapshot):
        kind = "CREATIVE_ADAPTIVE_RUN_START"
        identifier = _fixed_id(candidate_ref, "start")
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            self._lock(repo, identifier)
            evaluator = self._authorize(repo, controller, world)
            ref, old = self._saved(repo, world, identifier, kind)
            seed = snapshot["freeze"]["order_seed"]
            scope = snapshot["candidate"]["scope"]
            start = {
                "scope": scope,
                "candidate_ref": candidate_ref,
                "freeze_ref": snapshot["experiment_ref"],
                "design_ref": snapshot["design_ref"],
                "evaluator": evaluator,
                "order_seed": seed,
                "samples_sha256": fingerprint(snapshot["samples"]),
                "allocation_sha256": fingerprint(snapshot["allocation"]),
                "arms_sha256": fingerprint([snapshot["baseline"], snapshot["treatment"]]),
                "model_config_sha256": fingerprint(REPLAY_MODEL_CONFIG),
            }
            if ref:
                require(
                    start == old,
                    "EVALUATION_FREEZE_MISMATCH",
                    "This candidate already has a run with other inputs or evaluator",
                )
                return ref, start
            return _put(self.kernel, repo, world, controller, kind, start, identifier), start

    def _row(
        self,
        controller,
        world,
        candidate_ref,
        freeze,
        start_ref,
        start,
        plan,
        sample,
        arm_id,
        arm,
        kind,
        ordinal,
    ):
        row_kind = "CREATIVE_ADAPTIVE_MEASUREMENT"
        blind_id = plan["blind_ids"][plan["order"].index(arm_id)]
        identifier = _fixed_id(candidate_ref, "row:" + blind_id)
        # This arm performs bounded local CPU work with no external calls. A
        # per-measurement transaction lock makes concurrent retries execute it once.
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            self._lock(repo, identifier)
            require(
                self._authorize(repo, controller, world) == start["evaluator"],
                "VIEW_DENIED",
                "Evaluator authority changed",
            )
            existing, prior = self._saved(repo, world, identifier, row_kind)
            if existing:
                require(
                    prior["start_ref"] == start_ref,
                    "INVALID_EVIDENCE",
                    "Existing measurement uses another freeze",
                )
                return existing, prior
            material = _input(sample, kind)
            row = {
                "scope": start["scope"],
                "candidate_ref": candidate_ref,
                "start_ref": start_ref,
                "sample_ref": sample["sample_ref"],
                "text_ref": sample["text_ref"],
                "case_id": plan["case_id"],
                "repetition": plan["repetition"],
                "seed": freeze["order_seed"],
                "arm_id": arm_id,
                "blind_id": blind_id,
                "input_sha256": fingerprint(material),
                "controls_sha256": fingerprint(
                    [
                        freeze["source_commit"],
                        freeze["works"],
                        freeze["shared_input_ref"],
                        freeze["model_config_sha256"],
                        freeze["budget_policy_ref"],
                        freeze["quota"],
                        freeze["order_seed"],
                    ]
                ),
                "dependencies_sha256": fingerprint(
                    [sample["source_versions"], sample["ordered_texts"][0]["draft_ref"]]
                ),
                "instruction_sha256": fingerprint(arm),
                "held_out": sample["held_out"],
                "independent_label": sample["independent_label"],
                "status": "PENDING",
                "executed": False,
                "latency_ns": 0,
                "provider_tokens": 0,
                "amount": "0",
                "currency": freeze["quota"]["currency"],
                "attempt_refs": [],
                "output": None,
                "score": None,
            }
            deadline = datetime.fromisoformat(freeze["quota"]["deadline"])
            if datetime.now(UTC) >= deadline:
                row["status"] = "DEADLINE_EXCEEDED"
            elif ordinal >= freeze["quota"]["max_attempts"] // 2:
                # Reserve symmetrical slots before either arm runs; random presentation
                # order cannot give one arm the last unit of budget.
                row["status"] = "ATTEMPT_LIMIT"
            else:
                row["executed"] = True
                timer = perf_counter_ns()
                try:
                    row["output"] = _execute(arm, material, kind)
                    row["score"] = _score(sample, row["output"]["payload"], kind)
                    row["status"] = "SUCCEEDED" if datetime.now(UTC) < deadline else "DEADLINE_EXCEEDED"
                except DomainError as error:
                    row["status"] = error.code
                except Exception as error:  # noqa: BLE001 -- keep every preregistered local engine failure
                    # Keep the preregistered failure. Exception messages and stacks can
                    # contain manuscript text and never belong in operator status.
                    row["status"] = "ENGINE_FAILURE"
                    row["error_type"] = type(error).__name__
                finally:
                    row["latency_ns"] = perf_counter_ns() - timer
            return _put(self.kernel, repo, world, controller, row_kind, row, identifier), row

    def run(self, controller, world_ref, *, candidate_ref, idempotency_key):
        flags = self.config or CreativeHarnessConfig.from_env()
        require(
            flags.enabled and flags.adaptive_instructions,
            "ADAPTIVE_INSTRUCTIONS_DISABLED",
            "Adaptive evaluation is disabled",
        )
        validate_contract("WorldRef", world_ref)
        validate_contract("ArtifactRef", candidate_ref)
        require(
            isinstance(idempotency_key, str) and 1 <= len(idempotency_key.strip()) <= 200,
            "INVALID_ARGUMENT",
            "Invalid idempotency key",
        )
        api = _source_api(self.kernel)
        request = {"world_ref": world_ref, "candidate_ref": candidate_ref}
        live = api.refresh_sources(controller, world_ref, candidate_ref)
        with self.kernel.db.uow(write=False) as repo:
            self._authorize(repo, controller, world_ref)
            snapshot = api.load_evaluation(repo, controller, world_ref, candidate_ref, live=live)
            old = self.kernel._prior(
                repo, controller, world_ref["work_id"], "ADAPTIVE_REPLAY", idempotency_key, request
            )
            if old:
                self.inspect(
                    repo,
                    world_ref,
                    candidate_ref=candidate_ref,
                    engineering_report_ref=old["engineering_report_ref"],
                    run_manifest_ref=old["run_manifest_ref"],
                )
                return old
            freeze, samples, schedule = self._freeze(snapshot)
        start_ref, start = self._start(controller, world_ref, candidate_ref, snapshot)
        rows = []
        refs = []
        for ordinal, pair in enumerate(schedule):
            for arm_id in pair["order"]:
                arm = snapshot["baseline" if arm_id == "CONTROL" else "treatment"]
                row_ref, row = self._row(
                    controller,
                    world_ref,
                    candidate_ref,
                    freeze,
                    start_ref,
                    start,
                    pair,
                    samples[pair["case_id"]],
                    arm_id,
                    arm,
                    snapshot["candidate"]["change_kind"],
                    ordinal,
                )
                rows.append(row)
                refs.append(row_ref)
        quality, totals = _quality(rows)
        status = "COMPLETE" if all(r["status"] == "SUCCEEDED" for r in rows) else "INCOMPLETE"
        manifest = {
            "scope": start["scope"],
            "candidate_ref": candidate_ref,
            "freeze_ref": snapshot["experiment_ref"],
            "start_ref": start_ref,
            "allocation_sha256": start["allocation_sha256"],
            "measurement_refs": refs,
            "expected": len(schedule) * 2,
            "executed": sum(r["executed"] for r in rows),
            "status": status,
        }
        live = api.refresh_sources(controller, world_ref, candidate_ref)
        with self.kernel.objects.lock(), self.kernel.db.uow() as repo:
            self._lock(repo, start_ref["artifact_revision_id"])
            require(
                self._authorize(repo, controller, world_ref) == start["evaluator"],
                "VIEW_DENIED",
                "Evaluator authority changed",
            )
            current = api.load_evaluation(repo, controller, world_ref, candidate_ref, live=live)
            require(
                fingerprint(snapshot) == fingerprint(current),
                "EVALUATION_FREEZE_MISMATCH",
                "Frozen experiment changed during replay",
            )
            manifest_kind, report_kind = (
                "CREATIVE_ADAPTIVE_RUN_MANIFEST",
                "CREATIVE_ADAPTIVE_ENGINEERING_REPORT",
            )
            manifest_id, report_id = _fixed_id(candidate_ref, "manifest"), _fixed_id(candidate_ref, "report")
            manifest_ref, old_manifest = self._saved(repo, world_ref, manifest_id, manifest_kind)
            if manifest_ref:
                require(old_manifest == manifest, "INVALID_EVIDENCE", "Completed manifest differs")
            else:
                manifest_ref = _put(
                    self.kernel, repo, world_ref, controller, manifest_kind, manifest, manifest_id
                )
            report = {
                "scope": start["scope"],
                "candidate_ref": candidate_ref,
                "freeze_ref": snapshot["experiment_ref"],
                "run_manifest_ref": manifest_ref,
                "rubric_ref": freeze["rubric_ref"],
                "evaluator": start["evaluator"],
                "model_config_sha256": freeze["model_config_sha256"],
                "quality": quality,
                "arm_totals": totals,
                "literary_status": "UNPROVEN",
                "measurement": "ENGINEERING_ONLY",
                "status": status,
            }
            report_ref, old_report = self._saved(repo, world_ref, report_id, report_kind)
            if report_ref:
                require(old_report == report, "INVALID_EVIDENCE", "Completed evaluation report differs")
            else:
                report_ref = _put(self.kernel, repo, world_ref, controller, report_kind, report, report_id)
            result = {
                "candidate_ref": candidate_ref,
                "freeze_ref": snapshot["experiment_ref"],
                "engineering_report_ref": report_ref,
                "run_manifest_ref": manifest_ref,
                "status": status,
                "eligible": quality["eligible"],
            }
            old = self.kernel._prior(
                repo, controller, world_ref["work_id"], "ADAPTIVE_REPLAY", idempotency_key, request
            )
            if old:
                return old
            self.kernel._receipt(
                repo, controller, world_ref, "ADAPTIVE_REPLAY", idempotency_key, request, result
            )
            return result

    def inspect(self, repo, world_ref, *, candidate_ref, engineering_report_ref, run_manifest_ref):
        require(
            engineering_report_ref["artifact_revision_id"] == _fixed_id(candidate_ref, "report")
            and run_manifest_ref["artifact_revision_id"] == _fixed_id(candidate_ref, "manifest"),
            "INVALID_EVIDENCE",
            "Evaluation does not belong to this candidate",
        )
        report = _read(
            self.kernel, repo, world_ref, engineering_report_ref, "CREATIVE_ADAPTIVE_ENGINEERING_REPORT"
        )
        manifest = _read(self.kernel, repo, world_ref, run_manifest_ref, "CREATIVE_ADAPTIVE_RUN_MANIFEST")
        start = _read(self.kernel, repo, world_ref, manifest["start_ref"], "CREATIVE_ADAPTIVE_RUN_START")
        envelope = _read(
            self.kernel, repo, world_ref, report["freeze_ref"], "CREATIVE_INSTRUCTION_EXPERIMENT"
        )
        freeze = validate_contract("ExperimentFreeze", envelope["freeze"], "harness")
        allocation = _read(
            self.kernel,
            repo,
            world_ref,
            freeze["blind_allocation_ref"],
            "CREATIVE_INSTRUCTION_BLIND_ALLOCATION",
        )["allocation"]
        rubric = _read(self.kernel, repo, world_ref, freeze["rubric_ref"], "CREATIVE_INSTRUCTION_RUBRIC")
        require(
            envelope["candidate_ref"] == candidate_ref
            and envelope["design_ref"] == start["design_ref"]
            and report["rubric_ref"] == freeze["rubric_ref"]
            and fingerprint(rubric["rubric"]) == rubric["sha256"]
            and fingerprint(allocation) == manifest["allocation_sha256"] == start["allocation_sha256"],
            "INVALID_EVIDENCE",
            "Measured cases or objective rubric differ from preregistration",
        )
        evaluator = start["evaluator"]
        identity = Principal(evaluator["grant_id"], evaluator["principal_id"])
        require(
            self._authorize(repo, identity, world_ref) == evaluator and report["evaluator"] == evaluator,
            "VIEW_DENIED",
            "Evaluation authority is no longer current",
        )
        require(
            report["candidate_ref"] == manifest["candidate_ref"] == start["candidate_ref"] == candidate_ref
            and report["run_manifest_ref"] == run_manifest_ref
            and report["freeze_ref"] == start["freeze_ref"] == manifest["freeze_ref"]
            and report["model_config_sha256"] == start["model_config_sha256"] == freeze["model_config_sha256"]
            and report["status"] == manifest["status"],
            "INVALID_EVIDENCE",
            "Invalid engineering experiment lineage",
        )
        rows = [
            _read(self.kernel, repo, world_ref, ref, "CREATIVE_ADAPTIVE_MEASUREMENT")
            for ref in manifest["measurement_refs"]
        ]
        require(
            [(r["case_id"], r["repetition"], r["arm_id"], r["blind_id"]) for r in rows]
            == [
                (p["case_id"], p["repetition"], arm, blind)
                for p in allocation
                for arm, blind in zip(p["order"], p["blind_ids"], strict=True)
            ]
            and len(rows) == manifest["expected"] == len({r["blind_id"] for r in rows})
            and all(
                r["candidate_ref"] == candidate_ref and r["start_ref"] == manifest["start_ref"] for r in rows
            ),
            "INVALID_EVIDENCE",
            "Missing or substituted preregistered measurements",
        )
        quality, totals = _quality(rows)
        require(
            quality == report["quality"] and totals == report["arm_totals"],
            "INVALID_EVIDENCE",
            "Objective measurements differ",
        )
        return {
            **report,
            "engineering_report_ref": engineering_report_ref,
            "results": rows,
            "eligible": quality["eligible"],
        }

    def require_release(self, repo, world_ref, *, candidate_ref, engineering_report_ref, run_manifest_ref):
        report = self.inspect(
            repo,
            world_ref,
            candidate_ref=candidate_ref,
            engineering_report_ref=engineering_report_ref,
            run_manifest_ref=run_manifest_ref,
        )
        require(
            report["status"] == "COMPLETE" and report["eligible"],
            "ADAPTIVE_EVALUATION_REJECTED",
            "Complete, nonregressing evidence and a held-out engineering gain are required",
        )
        return report
