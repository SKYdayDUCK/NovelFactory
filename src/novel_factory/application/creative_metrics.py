"""Read-only, branch-authorized engineering measurements on a bounded PostgreSQL snapshot."""

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import ceil

from sqlalchemy import text

from novel_factory.application.harness_observations import _version
from novel_factory.application.kernel import require
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.errors import DomainError


def _time(value):
    if value is None:
        return None
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.tzinfo is not None:
            return stamp.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        pass
    raise DomainError("INVALID_ARGUMENT", "Use an ISO 8601 timestamp with an explicit timezone")


def _ratio(numerator, denominator):
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": numerator / denominator if denominator else None,
    }


def _distribution(values):
    series = sorted(float(x) for x in values if x is not None)
    return {
        "n": len(series),
        "sum": sum(series) if series else None,
        "mean": sum(series) / len(series) if series else None,
        "p95": series[ceil(0.95 * len(series)) - 1] if series else None,
    }


def _rows(repo, query, params):
    result = repo.conn.execute(text(query), params).mappings().all()
    require(
        len(result) < params["fetch"],
        "CAPACITY_LIMIT",
        "The complete metric population exceeds the row limit; narrow the time window",
    )
    return result


# Each projection runs only for the authorized window. Manuscript text, actor material,
# issued instructions, evidence quotes and provider responses stay inside PostgreSQL.
_BRANCH = """coalesce(a.payload #>> '{scope,world_ref,branch_id}',
 a.payload #>> '{packet,scope,world_ref,branch_id}', a.payload #>> '{input_world_ref,branch_id}',
 a.payload #>> '{world_ref,branch_id}')"""
_KINDS = (
    "CREATIVE_CONTEXT_SNAPSHOT",
    "CREATIVE_PLAN_CANDIDATE",
    "CREATIVE_SIMULATION_RUN",
    "CREATIVE_PLAN_DECISION",
    "CREATIVE_REVIEW_DETAILS",
    "CREATIVE_REVIEW_DECISION",
    "CREATIVE_REVIEW_REVISION",
    "NARRATIVE_DRAFT",
    "CREATIVE_DYNAMIC_INSTANCE",
    "CREATIVE_DYNAMIC_EVENT",
    "CREATIVE_INSTRUCTION_CANDIDATE",
    "CREATIVE_INSTRUCTION_DECISION",
    "CREATIVE_ADAPTIVE_ENGINEERING_REPORT",
    "CREATIVE_ADAPTIVE_MEASUREMENT",
    "AGENT_CALL_EVIDENCE",
)

# Look up the immutable source generation/artifact by primary key even when created
# before the observation window. This is lineage for an observed event, not an extra event.
_ARTIFACTS = (
    """
WITH population AS MATERIALIZED (
 SELECT a.kind,a.artifact_revision_id,a.work_id,a.sha256,a.payload,a.created_at FROM nf_artifacts a WHERE a.work_id=:work AND a.kind=ANY(CAST(:kinds AS text[]))
 AND a.created_at >= :since AND a.created_at < :until AND """
    + _BRANCH
    + """=:branch
 UNION ALL
 SELECT 'AGENT_CALL_EVIDENCE',r.artifact_revision_id,r.work_id,r.sha256,
   jsonb_build_object('attempt_id',c.attempt_id,'elapsed_seconds',r.payload->'elapsed_seconds',
     'model_configuration',r.payload->'model_configuration','execution_kind',r.payload#>'{model_configuration,execution_kind}',
     'usage',jsonb_build_object('input_tokens',r.payload->'input_tokens','output_tokens',r.payload->'output_tokens'),
     'input_ref',p.payload->'input_ref','dynamic_assignment_ref',r.payload->'dynamic_assignment_ref'),r.created_at
 FROM nf_audit_events e
 JOIN nf_call_attempts c ON c.attempt_id=e.attempt_id AND c.task_id=e.task_id AND c.generation=e.generation
   AND c.call_key='actor-lookup-choice-v1'
 JOIN nf_tasks t ON t.task_id=c.task_id AND t.work_id=e.work_id AND t.branch_id=e.branch_id
 JOIN nf_artifacts r ON r.artifact_revision_id=e.evidence_id AND r.work_id=e.work_id AND r.kind='AGENT_ROLE_TOOL_RESPONSE'
   AND r.payload->>'request_sha256'=c.request_hash
 JOIN nf_audit_events pe ON pe.attempt_id=c.attempt_id AND pe.task_id=c.task_id AND pe.generation=c.generation
   AND pe.work_id=e.work_id AND pe.branch_id=e.branch_id AND pe.event_type='artifact.agent_role_tool_prompt'
 JOIN nf_artifacts p ON p.artifact_revision_id=pe.evidence_id AND p.work_id=e.work_id
   AND p.kind='AGENT_ROLE_TOOL_PROMPT' AND p.payload->>'request_sha256'=c.request_hash
 WHERE e.event_type='artifact.agent_role_tool_response' AND e.work_id=:work AND e.branch_id=CAST(:branch AS uuid)
   AND r.created_at>=:since AND r.created_at<:until
 ORDER BY created_at,artifact_revision_id LIMIT :fetch
)
SELECT a.kind, a.artifact_revision_id::text AS id, CASE a.kind
 WHEN 'CREATIVE_CONTEXT_SNAPSHOT' THEN jsonb_build_object(
  'sources',jsonb_array_length(a.payload#>'{packet,sources}'),
  'snapshot_bytes',(SELECT size FROM nf_objects o WHERE o.hash=a.sha256),
  'ms',packet_event.duration_ms,'input_bytes',to_jsonb(packet_event)#>'{harness_details,packet_bytes}')
 WHEN 'CREATIVE_PLAN_CANDIDATE' THEN jsonb_build_object('alternatives',jsonb_array_length(a.payload->'alternatives'))
 WHEN 'CREATIVE_PLAN_DECISION' THEN jsonb_build_object('status',a.payload->'status')
 WHEN 'CREATIVE_SIMULATION_RUN' THEN jsonb_build_object(
  'candidate',simulation.payload#>>'{plan_candidate_ref,artifact_revision_id}',
  'options',ARRAY(SELECT s->>'option_id' FROM jsonb_array_elements(a.payload->'stops') s
     WHERE CAST(s->>'steps' AS integer)>0))
 WHEN 'CREATIVE_REVIEW_DETAILS' THEN jsonb_build_object(
  'report',a.payload#>>'{report_ref,artifact_revision_id}', 'layer',a.payload->'layer',
  'drafts',ARRAY(SELECT d->>'artifact_revision_id' FROM jsonb_array_elements(a.payload->'scope_draft_refs') d),
  'finding_count',jsonb_array_length(a.payload->'findings'),
  'actionable',ARRAY(SELECT (a.payload->'findings'->CAST(f.i-1 AS integer))->>'finding_id'
    FROM jsonb_array_elements(review.payload->'findings') WITH ORDINALITY AS f(v,i)
    WHERE f.v->>'judgment' IN ('CONTRADICTION','PREFERENCE') AND coalesce(f.v->>'requested_change','')<>''),
  'edited',ARRAY(SELECT f->>'finding_id' FROM jsonb_array_elements(a.payload->'prior_findings') f
    WHERE f->>'judgment' IN ('CONTRADICTION','PREFERENCE') AND CAST(f->>'edited' AS boolean)),
  'parent',a.payload#>>'{previous_report_ref,artifact_revision_id}')
 WHEN 'CREATIVE_REVIEW_DECISION' THEN jsonb_build_object('report',a.payload#>>'{report_ref,artifact_revision_id}','action',a.payload->'action')
 WHEN 'CREATIVE_DYNAMIC_EVENT' THEN jsonb_build_object('action',a.payload->'action')
 WHEN 'CREATIVE_INSTRUCTION_DECISION' THEN jsonb_build_object('decision',a.payload->'decision')
 WHEN 'CREATIVE_ADAPTIVE_ENGINEERING_REPORT' THEN jsonb_build_object('status',a.payload->'status','eligible',a.payload#>'{quality,eligible}')
 WHEN 'CREATIVE_ADAPTIVE_MEASUREMENT' THEN jsonb_build_object(
   'eligible',a.payload->>'status'='SUCCEEDED' AND a.payload->'held_out'='true'::jsonb
       AND a.payload->'independent_label'='true'::jsonb AND jsonb_typeof(a.payload#>'{output,payload,findings}')='array',
   'arm',a.payload->'arm_id','tp',a.payload#>'{score,detected}','fp',a.payload#>'{score,false_positives}',
   'fn',a.payload#>'{score,false_negatives}','gold',a.payload#>'{score,positives}')
 WHEN 'AGENT_CALL_EVIDENCE' THEN jsonb_build_object(
   'ms',CAST(a.payload->>'elapsed_seconds' AS numeric)*1000,'attempt',a.payload->'attempt_id',
    'input_tokens',a.payload#>'{usage,input_tokens}','output_tokens',a.payload#>'{usage,output_tokens}',
   'loopback',coalesce(a.payload#>>'{model_configuration,endpoint}','') ~* '^[a-z]+://([^@/]*@)?(localhost|127[.]0[.]0[.]1|[[]::1[]]|[^/:]+[.](localhost|test|invalid))([/:?#]|$)',
   'fixture',coalesce(a.payload#>>'{model_configuration,adapter_kind}','')='DETERMINISTIC_TEST' OR coalesce(a.payload#>>'{model_configuration,provider}','')='fixture',
   'source_revision',dynamic.payload#>>'{workshop_binding,source_revision}',
   'execution_kind',a.payload->'execution_kind','skill' ,runtime.payload->'skill_name','version',runtime.payload->'skill_version',
   'dynamic_content',dynamic.payload#>'{workshop_binding,content_sha}','dynamic_revision',dynamic.payload#>'{revision_ref,artifact_revision_id}')
 ELSE '{}'::jsonb END AS data
FROM population a
LEFT JOIN nf_audit_events packet_event ON a.kind='CREATIVE_CONTEXT_SNAPSHOT'
 AND packet_event.work_id=a.work_id AND packet_event.branch_id=CAST(:branch AS uuid)
 AND packet_event.event_type='harness.context.packet' AND packet_event.evidence_id=a.artifact_revision_id
LEFT JOIN nf_artifacts simulation ON a.kind='CREATIVE_SIMULATION_RUN' AND simulation.work_id=a.work_id
 AND simulation.kind='CREATIVE_SIMULATION_REPORT' AND simulation.artifact_revision_id=CAST(a.payload#>>'{report_ref,artifact_revision_id}' AS uuid)
LEFT JOIN nf_artifacts review ON a.kind='CREATIVE_REVIEW_DETAILS' AND review.work_id=a.work_id
 AND review.kind='CREATIVE_REVIEW_REPORT' AND review.artifact_revision_id=CAST(a.payload#>>'{report_ref,artifact_revision_id}' AS uuid)
LEFT JOIN nf_artifacts input ON a.kind='AGENT_CALL_EVIDENCE' AND input.kind='AGENT_INPUT' AND input.work_id=a.work_id
 AND input.artifact_revision_id=CAST(a.payload#>>'{input_ref,artifact_revision_id}' AS uuid)
LEFT JOIN nf_artifacts runtime ON runtime.kind='AGENT_RUNTIME' AND runtime.work_id=input.work_id
 AND runtime.artifact_revision_id=CAST(input.payload#>>'{runtime_ref,artifact_revision_id}' AS uuid)
LEFT JOIN nf_artifacts dynamic ON a.kind='AGENT_CALL_EVIDENCE' AND dynamic.kind='CREATIVE_DYNAMIC_FREEZE' AND dynamic.work_id=a.work_id
 AND dynamic.artifact_revision_id=CAST(a.payload#>>'{dynamic_assignment_ref,artifact_revision_id}' AS uuid)
ORDER BY a.created_at,a.artifact_revision_id
"""
)

# A simulation is frozen once per candidate. Decisions observed by the end of the
# window can therefore be compared with the exact options that actually ran.
_DECISIONS = """
SELECT a.payload#>>'{candidate_ref,artifact_revision_id}' AS candidate,
 a.payload->>'option_id' AS option_id,a.payload->>'status' AS status
FROM nf_artifacts a WHERE a.work_id=:work AND a.kind='CREATIVE_PLAN_DECISION'
 AND a.payload#>>'{scope,world_ref,branch_id}'=:branch AND a.created_at>=:since AND a.created_at<:until
 AND a.payload#>>'{candidate_ref,artifact_revision_id}'=ANY(CAST(:candidates AS text[])) LIMIT :fetch
"""

_CALLS = """
SELECT c.attempt_id::text AS attempt,c.state,c.updated_at,c.actual_tokens,c.actual_amount,b.currency,
 scene.payload#>>'{scene_contract,scene_id}' AS scene
FROM nf_call_attempts c JOIN nf_tasks t ON t.task_id=c.task_id
JOIN nf_budgets b ON b.budget_id=c.budget_id AND b.work_id=t.work_id
LEFT JOIN nf_selections w ON w.work_id=t.work_id AND w.selection_kind=concat('WRITING_TASK:',c.task_id,':',c.generation)
LEFT JOIN nf_artifacts binding ON binding.work_id=t.work_id AND binding.kind='WRITING_TASK_BINDING'
 AND binding.artifact_revision_id=CAST(w.artifact_ref->>'artifact_revision_id' AS uuid)
 AND binding.payload#>>'{world_ref,branch_id}'=:branch AND binding.payload->>'task_id'=c.task_id::text
 AND CAST(binding.payload->>'generation' AS integer)=c.generation AND binding.created_at<:until
LEFT JOIN nf_artifacts scene ON scene.work_id=t.work_id AND scene.kind='SCENE_PLAN'
 AND scene.artifact_revision_id=CAST(binding.payload#>>'{scene_ref,artifact_revision_id}' AS uuid)
WHERE t.work_id=:work AND t.branch_id=CAST(:branch AS uuid) AND c.created_at>=:since AND c.created_at<:until
ORDER BY c.created_at,c.attempt_id LIMIT :fetch
"""

_TASK_EVENTS = """
SELECT o.aggregate_id AS task, CAST(o.payload->>'generation' AS integer) AS generation,
 o.payload->>'state' AS state,o.created_at,t.created_at AS started
FROM nf_outbox o JOIN nf_tasks t ON t.task_id::text=o.aggregate_id
WHERE t.work_id=:work AND t.branch_id=CAST(:branch AS uuid) AND o.topic='task.changed'
 AND o.created_at>=:since AND o.created_at<:until
 AND o.payload->>'state' IN ('RESULT_STORED','ACCEPTED','PAUSED','FAILED','CANCELLED')
ORDER BY o.created_at,o.outbox_id LIMIT :fetch
"""
_FENCES = """
SELECT i.task_id::text AS task,i.generation,i.created_at FROM nf_task_inputs i JOIN nf_tasks t USING(task_id)
WHERE t.work_id=:work AND t.branch_id=CAST(:branch AS uuid) AND i.generation>0 AND i.created_at>=:since AND i.created_at<:until
 AND i.spec->>'state' IN ('READY','PAUSED','STALE','FAILED','BLOCKED')
ORDER BY i.created_at,i.task_id,i.generation LIMIT :fetch
"""


def _spend(rows, end, audits):
    known_tokens, known_cost = [], []
    provider_measured = 0
    for row in rows:
        audit = audits.get(row["attempt"], {})
        settled = row["updated_at"] < end and row["state"] in {"SUCCEEDED", "FAILED"}
        if settled and row["actual_tokens"] is not None:
            known_tokens.append(row["actual_tokens"])
        elif all(type(audit.get(k)) is int and audit[k] >= 0 for k in ("input_tokens", "output_tokens")):
            known_tokens.append(audit["input_tokens"] + audit["output_tokens"])
            provider_measured += 1
        if settled and row["actual_amount"] is not None:
            known_cost.append(row["actual_amount"])
    total = sum(known_cost, Decimal(0))
    return {
        "attempts": len(rows),
        "known_tokens": sum(known_tokens),
        "tokens_from_unsettled_provider_receipts": provider_measured,
        "unknown_token_attempts": len(rows) - len(known_tokens),
        "actual_tokens": sum(known_tokens) if len(known_tokens) == len(rows) else None,
        "known_amount": format(total, "f"),
        "unknown_amount_attempts": len(rows) - len(known_cost),
        "actual_amount": format(total, "f") if len(known_cost) == len(rows) else None,
    }


def _review(rows):
    decisions = {d["report"]: d["action"] for _, d in rows["CREATIVE_REVIEW_DECISION"]}
    details = [
        d for _, d in rows["CREATIVE_REVIEW_DETAILS"] if decisions.get(d["report"]) != "WITHDRAW"
    ]
    covered = {draft for d in details for draft in d["drafts"]}
    produced = {draft for draft, _ in rows["NARRATIVE_DRAFT"]}
    actionable = {(d["report"], f) for d in details for f in d["actionable"]}
    fixed = {
        (d["parent"], f)
        for d in details
        if decisions.get(d["report"]) == "ACCEPT_REVISION"
        for f in d["edited"]
    }
    layers = Counter(d["layer"] for d in details)
    errors = {}
    for arm in ("CONTROL", "TREATMENT"):
        labels = [
            d for _, d in rows["CREATIVE_ADAPTIVE_MEASUREMENT"] if d["eligible"] and d["arm"] == arm
        ]
        tp, fp, fn = (sum(d[k] for d in labels) for k in ("tp", "fp", "fn"))
        neg = [d for d in labels if d["gold"] == 0]
        errors[arm] = {
            "labeled_heldout_runs": len(labels),
            "false_positives": fp if labels else None,
            "false_negatives": fn if labels else None,
            "false_discovery_rate": _ratio(fp, fp + tp),
            "miss_rate": _ratio(fn, fn + tp),
            "negative_case_false_alarm_rate": _ratio(sum(d["fp"] > 0 for d in neg), len(neg)),
        }
    return {
        "reports": len(details),
        "reports_by_layer": dict(sorted(layers.items())),
        "covered_drafts": len(covered),
        "coverage": _ratio(len(covered & produced), len(produced)),
        "findings_observed": sum(d["finding_count"] for d in details),
        "actionable_findings": len(actionable),
        "reports_with_actionable_findings": _ratio(
            sum(bool(d["actionable"]) for d in details), len(details)
        ),
        "independently_fixed": _ratio(len(actionable & fixed), len(actionable)),
        "targeted_revisions": len(rows["CREATIVE_REVIEW_REVISION"]),
        "heldout_review_errors": errors,
    }


def _lifecycle(events, fences):
    success = defaultdict(list)
    first_result = []
    episodes = {(e["task"], e["generation"]): e["created_at"] for e in fences}
    durations = defaultdict(list)
    for e in events:
        state, episode = e["state"], (e["task"], e["generation"])
        if state in {"PAUSED", "FAILED"}:
            episodes.setdefault(episode, e["created_at"])
        if state in {"RESULT_STORED", "ACCEPTED"}:
            success[e["task"]].append((e["generation"], e["created_at"]))
        if state == "RESULT_STORED":
            first_result.append((e["created_at"] - e["started"]).total_seconds() * 1000)
        if state in {"ACCEPTED", "FAILED", "CANCELLED"}:
            durations[state].append((e["created_at"] - e["started"]).total_seconds() * 1000)
    recovered = sum(
        any(new_gen >= gen and done > failure for new_gen, done in success[task])
        for (task, gen), failure in episodes.items()
    )
    terminal = [ms for group in durations.values() for ms in group]
    return (
        {
            "terminal_ms": _distribution(terminal),
            "result_stored_ms": _distribution(first_result),
            "by_terminal_state_ms": {k: _distribution(v) for k, v in sorted(durations.items())},
        },
        {
            "failed_or_fenced_generations": len(episodes),
            "success_after_failure": _ratio(recovered, len(episodes)),
        },
    )


class CreativeMetricsService:
    def __init__(self, kernel):
        self.kernel = kernel

    def report(self, principal, world_ref, *, since=None, until=None, limit=1000):
        wr = validate_contract("WorldRef", world_ref)
        require(
            type(limit) is int and 1 <= limit <= 5000,
            "INVALID_ARGUMENT",
            "Metric row limit must be between 1 and 5000",
        )
        requested_start, requested_end = _time(since), _time(until)
        with self.kernel.db.uow(write=False, repeatable_read=True) as repo:
            grant = self.kernel.authorize(repo, principal, wr["work_id"], "ADMIN", wr["branch_id"])
            require(
                grant["role_id"] == "control_operator",
                "VIEW_DENIED",
                "Metrics require the branch controller",
            )
            require(
                repo.conn.scalar(
                    text(
                        "SELECT 1 FROM nf_revisions WHERE work_id=:work_id AND branch_id=:branch_id AND world_revision_id=:world_revision_id"
                    ),
                    wr,
                ),
                "VIEW_DENIED",
                "Unknown world revision",
            )
            now = repo.now()
            end = requested_end or now
            start = requested_start or (end - timedelta(days=1))
            require(
                start < end <= now and end - start <= timedelta(days=31),
                "INVALID_ARGUMENT",
                "Choose a completed, nonempty observation interval of at most 31 days",
            )
            params = {
                "work": wr["work_id"],
                "branch": wr["branch_id"],
                "since": start,
                "until": end,
                "fetch": limit + 1,
                "kinds": list(_KINDS),
            }
            artifacts = _rows(repo, _ARTIFACTS, params)
            rows = defaultdict(list)
            for a in artifacts:
                rows[a["kind"]].append((a["id"], a["data"]))
            runs = [r for _, r in rows["CREATIVE_SIMULATION_RUN"]]
            decision_rows = (
                _rows(repo, _DECISIONS, {**params, "candidates": [r["candidate"] for r in runs]})
                if runs
                else []
            )
            calls = _rows(repo, _CALLS, params)
            events = _rows(repo, _TASK_EVENTS, params)
            fences = _rows(repo, _FENCES, params)
            instance_population = repo.conn.scalar(
                text("""
                SELECT count(*) FROM (SELECT 1 FROM nf_artifacts
                WHERE work_id=:work AND kind='CREATIVE_DYNAMIC_INSTANCE' AND created_at<:until
                AND payload#>>'{scope,world_ref,branch_id}'=:branch LIMIT :fetch) AS population"""),
                params,
            )
            compiled = _rows(
                repo,
                """SELECT duration_ms,harness_details->'packet_bytes' AS compiled_bytes FROM nf_audit_events WHERE work_id=:work
                AND branch_id=CAST(:branch AS uuid) AND event_type='context.compiled'
                AND event_time>=:since AND event_time<:until ORDER BY event_time,event_id LIMIT :fetch""",
                params,
            )

        decisions = {r["candidate"]: r for r in decision_rows}
        adopted = abandoned = pending = 0
        for r in runs:
            decision = decisions.get(r["candidate"])
            for option in r["options"]:
                if decision is None:
                    pending += 1
                elif option == decision["option_id"]:
                    adopted += 1
                else:
                    abandoned += 1
        packets = [s for _, s in rows["CREATIVE_CONTEXT_SNAPSHOT"]]
        skill_count = Counter()
        model_latency = defaultdict(list)
        audits = {}
        for _, call in rows["AGENT_CALL_EVIDENCE"]:
            if call["attempt"] in audits:
                continue
            audits[call["attempt"]] = call
            provenance = call["execution_kind"]
            if call["fixture"] or call["loopback"]:
                provenance = "TEST_FIXTURE" if call["fixture"] else "LOOPBACK_OR_TEST_ENDPOINT"
            call["origin"] = provenance
            model_latency[call["origin"]].append(call["ms"])
            skill_count[
                (
                    call["skill"],
                    call["version"],
                    call["dynamic_revision"],
                    _version(call["source_revision"]),
                    call["dynamic_content"],
                )
            ] += 1
        by_scene = defaultdict(list)
        for call in calls:
            by_scene[
                (
                    call["scene"],
                    call["currency"],
                    audits.get(call["attempt"], {}).get("origin", "UNCLASSIFIED_LEDGER"),
                )
            ].append(call)
        scene_costs = [
            {
                "scene_id": scene,
                "currency": currency,
                "execution_origin": origin,
                **_spend(population, end, audits),
            }
            for (scene, currency, origin), population in sorted(
                by_scene.items(), key=lambda x: (x[0][0] or "", *x[0][1:])
            )
        ]
        latency, recovery = _lifecycle(events, fences)
        latency["model_call_ms_by_execution_kind"] = {
            kind: _distribution(group) for kind, group in sorted(model_latency.items())
        }
        release_actions = Counter(d["decision"] for _, d in rows["CREATIVE_INSTRUCTION_DECISION"])
        dynamic_actions = Counter(d["action"] for _, d in rows["CREATIVE_DYNAMIC_EVENT"])
        engineering = [d for _, d in rows["CREATIVE_ADAPTIVE_ENGINEERING_REPORT"]]
        return {
            "schema_version": "1.0",
            "world_ref": wr,
            "coverage": {
                "since": start.isoformat(),
                "until": end.isoformat(),
                "scope": "WORK_BRANCH_EVENT_WINDOW",
                "per_source_limit": limit,
                "complete": True,
                "source_rows": {
                    "artifacts": len(artifacts),
                    "call_attempts": len(calls),
                    "task_events": len(events),
                    "generation_fences": len(fences),
                    "context_compile_events": len(compiled),
                },
            },
            "context": {
                "packets": len(packets),
                "compile_ms": _distribution(c["duration_ms"] for c in compiled),
                "compiled_bytes": _distribution(c["compiled_bytes"] for c in compiled),
                "packet_build_ms": _distribution(p["ms"] for p in packets),
                "model_input_bytes": _distribution(p["input_bytes"] for p in packets),
                "stored_snapshot_bytes": _distribution(p["snapshot_bytes"] for p in packets),
                "loaded_sources": _distribution(p["sources"] for p in packets),
            },
            "planning": {
                "candidates": len(rows["CREATIVE_PLAN_CANDIDATE"]),
                "alternatives": sum(p["alternatives"] for _, p in rows["CREATIVE_PLAN_CANDIDATE"]),
                "decision_counts": dict(
                    Counter(d["status"] for _, d in rows["CREATIVE_PLAN_DECISION"])
                ),
                "simulations": len(runs),
                "simulated_paths": adopted + abandoned + pending,
                "pending_paths": pending,
                "adoption": _ratio(adopted, adopted + abandoned),
                "abandonment": _ratio(abandoned, adopted + abandoned),
            },
            "review": _review(rows),
            "dynamic": {
                "instances_created": len(rows["CREATIVE_DYNAMIC_INSTANCE"]),
                "instances_registered_as_of_end": instance_population
                if instance_population <= limit
                else None,
                "inventory_lower_bound": instance_population,
                "actions": dict(sorted(dynamic_actions.items())),
                "dispatched_skill_versions": [
                    {
                        "skill": skill,
                        "static_version": version,
                        "dynamic_revision_id": revision,
                        "dynamic_source_revision": source_version,
                        "dynamic_content_sha256": digest,
                        "calls": n,
                    }
                    for (skill, version, revision, source_version, digest), n in sorted(
                        skill_count.items(), key=lambda x: tuple(v or "" for v in x[0])
                    )
                ],
            },
            "instructions": {
                "proposed": len(rows["CREATIVE_INSTRUCTION_CANDIDATE"]),
                "evaluated": len(engineering),
                "complete": sum(d["status"] == "COMPLETE" for d in engineering),
                "eligible": sum(bool(d["eligible"]) for d in engineering),
                "promoted": release_actions["APPROVE"],
                "rejected": release_actions["REJECT"],
                "rolled_back": release_actions["ROLLBACK"],
            },
            "scenes": scene_costs,
            "latency": latency,
            "recovery": recovery,
        }
