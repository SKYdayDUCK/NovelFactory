"""Internal statistics worker and grant-checked projections over immutable PostgreSQL evidence."""

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from math import ceil
from urllib.parse import urlsplit

from sqlalchemy import text

from novel_factory.application.kernel import require
from novel_factory.domain.errors import DomainError

CURRENT_VERSION = 2
_FILTERS = {"volume_id", "trace_id", "actor_id", "role_id", "experiment_id", "model", "task_id"}
_SNAPSHOT = """a.event_id = ANY(CAST(:members AS uuid[]))"""


def _source(conn, event):
    if event["event_type"] not in {
        "model.completed",
        "artifact.review_report",
        "artifact.narrative_draft",
    }:
        return {}
    return conn.execute(
        text("""SELECT payload FROM nf_artifacts
        WHERE work_id=:work AND artifact_revision_id=:id"""),
        {"work": event["work_id"], "id": event["evidence_id"]},
    ).scalar_one()


def _usage(conn, event, data):
    usage = data["usage"]
    config = data["model_configuration"]
    host = urlsplit(config.get("endpoint", "")).hostname or ""
    fixture = (
        config["execution_kind"] not in {"REAL_EXTERNAL", "REAL_HOST"}
        or config.get("adapter_kind") == "DETERMINISTIC_TEST"
        or config.get("provider") == "fixture"
        or host in {"localhost", "127.0.0.1", "::1"}
        or host.endswith((".test", ".invalid", ".localhost"))
    )
    origin = "fixture" if fixture else "reported"
    metrics = {f"model.{origin}.calls": 1}
    for field in ("input_tokens", "output_tokens", "cached_input_tokens"):
        if usage.get(field) is not None:
            metrics[f"model.{origin}.{field}"] = usage[field]
    if usage["input_tokens"] is not None and usage["output_tokens"] is not None:
        metrics[f"model.{origin}.total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    if data.get("response_ref"):
        # Extract only the known counter. The provider JSON stays in its private artifact.
        raw = conn.scalar(
            text("""SELECT (payload->>'provider_response_json')::jsonb
                    #>> '{usage,completion_tokens_details,reasoning_tokens}' FROM nf_artifacts
                    WHERE work_id=:work AND artifact_revision_id=:ref"""),
            {"work": event["work_id"], "ref": data["response_ref"]["artifact_revision_id"]},
        )
        if raw and raw.isdecimal() and len(raw) <= 16:
            metrics[f"model.{origin}.reasoning_tokens"] = int(raw)
    if (
        not fixture
        and host == "openrouter.ai"
        and usage.get("actual_amount") is not None
        and data["accounting_state"] in {"SUCCEEDED", "FAILED"}
        and data.get("provider_receipt")
    ):
        metrics["model.actual_cost." + usage["currency"]] = str(Decimal(usage["actual_amount"]))
    elif fixture and usage.get("actual_amount") is not None:
        metrics["model.fixture.cost." + usage["currency"]] = str(Decimal(usage["actual_amount"]))
    elif data["accounting_state"] == "UNKNOWN" or usage.get("actual_amount") is None:
        metrics[f"model.{origin}.unsettled_calls"] = 1
    return metrics


def calculate(conn, event, version):
    """A pure interpretation of immutable evidence for one version and event."""
    if version not in {1, 2}:
        raise DomainError("INVALID_ARGUMENT", "Unsupported metric version")
    kind = event["event_type"]
    result = {"events": 1, "event." + kind: 1}
    if kind == "task.changed":
        result["task.state." + str(event["outcome"])] = 1
    elif kind == "mcp.finished":
        result["mcp." + event["outcome"]] = 1
        if event["error_code"]:
            result["error." + event["error_code"]] = 1
    elif kind == "world.accepted":
        # revision_events contains the whole selected history at this revision.
        # Count only the immutable receipt's newly accepted references.
        added = conn.scalar(
            text("""SELECT CASE WHEN r.command_type='ACCEPT'
                 THEN jsonb_array_length(r.payload->'accepted_event_refs') ELSE (r.payload->>'new_world_effects')::int END
             FROM nf_commits c JOIN nf_receipts r ON r.idempotency_key=c.request->>'idempotency_key'
             WHERE c.world_revision_id=:revision AND r.work_id=:work AND r.command_type IN ('ACCEPT','RUNTIME_UPGRADE')
             AND r.payload->'world_ref'->>'world_revision_id'=CAST(c.world_revision_id AS text)"""),
            {"work": event["work_id"], "revision": event["evidence_id"]},
        )
        if added is not None:
            result["world.accepted_events"] = added
        else:
            result["world.unmeasured_commits"] = 1
    elif kind == "model.completed":
        result.update(_usage(conn, event, _source(conn, event)))
    elif kind == "artifact.review_report":
        report = _source(conn, event)["submission"]
        for issue in report["issues"]:
            key = "literary.issue." + issue["category"]
            result[key] = result.get(key, 0) + 1
    elif kind == "artifact.narrative_draft":
        result["literary.revisions"] = int(_source(conn, event)["prior_draft_ref"] is not None)
        if event["text_characters"] is not None:
            result["literary.draft_unicode_characters"] = event["text_characters"]
    if version >= 2 and event["duration_ms"] is not None:
        result["duration_ms." + kind] = str(event["duration_ms"])
    return result


def _save(conn, event, version, *, replace=False):
    conflict = (
        "DO UPDATE SET metrics=excluded.metrics,computed_at=clock_timestamp()"
        if replace
        else "DO NOTHING"
    )
    conn.execute(
        text(f"""INSERT INTO nf_metric_contributions(metric_version,event_id,metrics)
        VALUES (:v,:id,CAST(:metrics AS jsonb)) ON CONFLICT(metric_version,event_id) {conflict}"""),
        {
            "v": version,
            "id": event["event_id"],
            "metrics": json.dumps(calculate(conn, event, version)),
        },
    )


def _scope(work_id, branch_id, filters, start, end):
    terms = ["a.work_id=:work", _SNAPSHOT]
    if branch_id is not None:
        terms.append("a.branch_id=:branch")
    args = {"work": work_id, "branch": branch_id}
    for key, value in (filters or {}).items():
        require(key in _FILTERS, "INVALID_ARGUMENT", "Unknown statistics dimension")
        if key == "experiment_id":
            terms.append("""(a.experiment_id=:experiment_id OR EXISTS(SELECT FROM nf_metric_experiments x
                WHERE x.experiment_id=:experiment_id AND x.work_id=a.work_id AND x.branch_id=a.branch_id
                AND a.event_time>=x.starts_at AND a.event_time<x.ends_at))""")
        else:
            terms.append(f"a.{key}=:{key}")
        args[key] = value
    for operator, bound, label in ((">=", start, "start"), ("<", end, "end")):
        if bound is not None:
            require(
                isinstance(bound, datetime) and bound.tzinfo is not None,
                "INVALID_ARGUMENT",
                "Timezone required",
            )
            terms.append(f"a.event_time {operator} :{label}")
            args[label] = bound
    return " AND ".join(terms), args


class StatisticsService:
    @staticmethod
    def handle(conn, envelope):
        event = (
            conn.execute(
                text("""SELECT a.* FROM nf_outbox o JOIN nf_audit_events a
            ON o.aggregate_id=a.event_id::text
            WHERE o.outbox_id=:message AND o.topic='audit.recorded' AND o.aggregate_type='audit'"""),
                {"message": envelope.event_id},
            )
            .mappings()
            .one()
        )
        _save(conn, event, CURRENT_VERSION)

    def __init__(self, kernel):
        self.kernel = kernel

    def _operate(
        self,
        mode,
        principal,
        work_id,
        branch_id,
        *,
        version=CURRENT_VERSION,
        source_cut=None,
        filters=None,
        start=None,
        end=None,
    ):
        require(version in {1, 2}, "INVALID_ARGUMENT", "Unsupported metric version")
        where, args = _scope(work_id, branch_id, filters, start, end)
        with self.kernel.db.uow() as repo:
            grant = self.kernel.authorize(repo, principal, work_id, "ADMIN", branch_id)
            require(
                branch_id is not None or grant["branch_ids"] == ["*"],
                "VIEW_DENIED",
                "Work-wide grant required",
            )
            conn = repo.conn
            # A content-addressed, immutable membership cut also survives logical
            # restore into a different PostgreSQL XID space. Only distinct requested
            # cuts use storage. Ordinary worker updates never materialize manifests.
            scope_key = f"statistics-cut:{work_id}:{branch_id}"
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:scope,0))"),
                {"scope": scope_key},
            )
            if source_cut is None:
                members = conn.scalar(
                    text("""SELECT coalesce(array_agg(event_id ORDER BY event_id),ARRAY[]::uuid[])
                    FROM nf_audit_events WHERE work_id=:work AND (CAST(:branch AS uuid) IS NULL OR branch_id=:branch)"""),
                    args,
                )
                digest = sha256((str(work_id) + str(branch_id)).encode())
                for member in members:
                    digest.update(member.bytes)
                cut = digest.hexdigest()
                conn.execute(
                    text("""INSERT INTO nf_metric_cuts(cut_id,work_id,branch_id,event_ids)
                    VALUES (:cut,:work,:branch,CAST(:members AS uuid[])) ON CONFLICT(cut_id) DO UPDATE SET last_used_at=clock_timestamp()"""),
                    {**args, "cut": cut, "members": members},
                )
                conn.execute(
                    text("""DELETE FROM nf_metric_cuts WHERE cut_id IN (
                    SELECT cut_id FROM nf_metric_cuts WHERE work_id=:work AND branch_id IS NOT DISTINCT FROM CAST(:branch AS uuid)
                    ORDER BY last_used_at DESC,cut_id OFFSET 16)"""),
                    args,
                )
            else:
                cut = source_cut
                members = conn.scalar(
                    text("""UPDATE nf_metric_cuts SET last_used_at=clock_timestamp()
                    WHERE cut_id=:cut AND work_id=:work AND branch_id IS NOT DISTINCT FROM CAST(:branch AS uuid) RETURNING event_ids"""),
                    {**args, "cut": cut},
                )
                require(members is not None, "VIEW_DENIED", "Unavailable statistics cut")
            args.update(members=members, v=version)
            if mode == "rebuild":
                # One transaction publishes a coherent replacement of this slice. Writers
                # covering a common immutable event compute an identical contribution.
                rows = conn.execute(
                    text(f"SELECT a.* FROM nf_audit_events a WHERE {where} ORDER BY event_id"), args
                )
                for event in rows.mappings():
                    _save(conn, event, version, replace=True)
            rows = conn.execute(
                text(f"""SELECT a.*,m.metrics,m.computed_at FROM nf_audit_events a
                LEFT JOIN nf_metric_contributions m ON m.event_id=a.event_id AND m.metric_version=:v
                WHERE {where} ORDER BY a.event_time,a.event_id"""),
                args,
            )
            metrics = Counter()
            tasks, accepted = set(), set()
            latencies = defaultdict(list)
            model_intervals = []
            diffs = []
            difference_count = total = processed = 0
            last_event = last_applied = None
            for event in rows.mappings():
                total += 1
                last_event = event["event_time"]
                actual = event["metrics"]
                if actual is not None:
                    processed += 1
                    last_applied = event["event_time"]
                    metrics.update({k: Decimal(str(v)) for k, v in actual.items()})
                    kind = event["event_type"]
                    if kind == "task.changed" and event["task_id"]:
                        tasks.add(event["task_id"])
                        if event["outcome"] == "ACCEPTED":
                            accepted.add(event["task_id"])
                    if event["duration_ms"] is not None:
                        latency_kind = (
                            {"READY": "workflow.wake", "RUNNING": "task.first_claim"}.get(
                                event["outcome"], kind
                            )
                            if kind == "task.changed"
                            else kind
                        )
                        latencies[latency_kind].append(float(event["duration_ms"]))
                        if kind == "model.completed":
                            end_ms = event["event_time"].timestamp() * 1000
                            if event["duration_ms"] > 0:
                                model_intervals.extend(
                                    [(end_ms - float(event["duration_ms"]), 1), (end_ms, -1)]
                                )
                if mode == "verify":
                    expected = calculate(conn, event, version)
                    if actual != expected:
                        difference_count += 1
                        if len(diffs) < 100:
                            keys = sorted(set(expected) | set(actual or {}))
                            diffs.append(
                                {
                                    "event_id": str(event["event_id"]),
                                    "at": last_event.isoformat(),
                                    "differences": {
                                        k: {
                                            "history": expected.get(k),
                                            "incremental": (actual or {}).get(k),
                                        }
                                        for k in keys
                                        if expected.get(k) != (actual or {}).get(k)
                                    },
                                }
                            )
            status = "COMPLETE" if processed == total and difference_count == 0 else "PARTIAL"
            answer = {
                "metric_version": version,
                "source_cut": cut,
                "computed_at": datetime.now(UTC).isoformat(),
                "processed_events": processed,
                "source_events": total,
                "completeness_status": status,
                "freshness": {
                    "missing_events": total - processed,
                    "latest_source_at": last_event.isoformat() if last_event else None,
                    "latest_applied_at": last_applied.isoformat() if last_applied else None,
                },
                "metrics": {
                    k: int(v) if v == v.to_integral() else str(v) for k, v in metrics.items()
                },
            }
            durations = {}
            for kind, values in latencies.items():
                ordered = sorted(values)
                durations[kind] = {
                    "samples": len(values),
                    "mean_ms": sum(values) / len(values),
                    "p95_ms": ordered[ceil(len(values) * 0.95) - 1],
                    "p99_ms": ordered[ceil(len(values) * 0.99) - 1],
                }
            active = peak = 0
            for _, change in sorted(model_intervals):
                active += change
                peak = max(peak, active)
            answer["measures"] = {
                "task_acceptance": {
                    "accepted_distinct": len(accepted),
                    "observed_distinct": len(tasks),
                    "fraction": len(accepted) / len(tasks) if tasks else None,
                },
                "durations": durations,
                "peak_completed_model_concurrency": peak if model_intervals else None,
                "accepted_tasks_per_second": (
                    len(accepted) / (end - start).total_seconds()
                    if start and end and end > start
                    else None
                ),
                "chapter_cost": None,
                "literary_quality_score": None,
                "estimated_provider_cost": None,
                "unavailable_reasons": {
                    "chapter_cost": "No stable chapter-to-call binding in P5",
                    "literary_quality_score": "Reviews carry issues and decisions without a calibrated score",
                    "estimated_provider_cost": "No versioned provider rate card registered",
                },
            }
            if mode == "verify":
                answer.update(
                    consistent=difference_count == 0,
                    difference_count=difference_count,
                    differences=diffs,
                    time_range=[
                        start.isoformat() if start else None,
                        end.isoformat() if end else None,
                    ],
                )
            return answer

    def read(self, principal, work_id, branch_id, **selection):
        return self._operate("read", principal, work_id, branch_id, **selection)

    def rebuild(self, principal, work_id, branch_id, **selection):
        return self._operate("rebuild", principal, work_id, branch_id, **selection)

    def verify_statistics(self, principal, work_id, branch_id, **selection):
        return self._operate("verify", principal, work_id, branch_id, **selection)

    def current(self, principal, work_id, branch_id):
        """Live authority sample, separately labelled from replayable measurements."""
        with self.kernel.db.uow(write=False, repeatable_read=True) as repo:
            grant = self.kernel.authorize(repo, principal, work_id, "ADMIN", branch_id)
            require(
                branch_id is not None or grant["branch_ids"] == ["*"],
                "VIEW_DENIED",
                "Work-wide grant required",
            )
            params = {"work": work_id, "branch": branch_id}
            scope = "t.work_id=:work AND (CAST(:branch AS uuid) IS NULL OR t.branch_id=:branch)"
            counts = repo.conn.execute(
                text(f"""SELECT t.role_id,t.state,count(*) AS tasks
                FROM nf_tasks t WHERE {scope} GROUP BY t.role_id,t.state"""),
                params,
            ).mappings()
            result = {
                "sampled_at": repo.now().isoformat(),
                "task_counts": [dict(r) for r in counts],
            }
            ledger = repo.conn.execute(
                text(f"""SELECT c.state,b.currency,count(*) AS calls,
                sum(c.reserved_amount) AS reserved_amount,sum(c.reserved_tokens) AS reserved_tokens
                FROM nf_call_attempts c JOIN nf_tasks t USING(task_id) JOIN nf_budgets b USING(budget_id)
                WHERE {scope} AND c.state IN ('SENT','PREPARED','UNKNOWN') GROUP BY c.state,b.currency"""),
                params,
            )
            result["outstanding"] = [dict(r) for r in ledger.mappings()]
            return result

    def import_legacy(self, principal, work_id, branch_id, *, limit=128):
        """Bounded, restartable pre-P5.5 model/commit audit backfill from immutable evidence."""
        from uuid import uuid5

        from novel_factory.application.audit_log import LogService

        require(
            type(limit) is int and 1 <= limit <= 1000, "INVALID_ARGUMENT", "Invalid import limit"
        )
        params = {"work": work_id, "branch": branch_id, "limit": limit}
        with self.kernel.db.uow() as repo:
            self.kernel.authorize(repo, principal, work_id, "ADMIN", branch_id)
            conn = repo.conn
            models = (
                conn.execute(
                    text("""SELECT a.artifact_revision_id,a.payload FROM nf_artifacts a
                JOIN nf_tasks t ON t.task_id::text=a.payload->>'task_id' AND t.work_id=a.work_id
                WHERE a.work_id=:work AND t.branch_id=:branch AND a.kind='AGENT_CALL_EVIDENCE'
                AND NOT EXISTS (SELECT FROM nf_audit_events e WHERE e.event_id=a.artifact_revision_id)
                ORDER BY a.artifact_revision_id LIMIT :limit"""),
                    params,
                )
                .mappings()
                .all()
            )
            for model in models:
                LogService.model(conn, model["artifact_revision_id"], model["payload"])
            worlds = (
                conn.execute(
                    text("""SELECT r.world_revision_id,r.created_at,o.outbox_id
                FROM nf_revisions r JOIN nf_commits c USING(world_revision_id)
                LEFT JOIN nf_outbox o ON o.dedupe_key='world:'||r.world_revision_id::text
                WHERE r.work_id=:work AND r.branch_id=:branch AND NOT EXISTS (
                  SELECT FROM nf_audit_events e WHERE e.event_type='world.accepted'
                  AND e.work_id=r.work_id AND e.evidence_id=r.world_revision_id)
                ORDER BY r.world_revision_id LIMIT :limit"""),
                    params,
                )
                .mappings()
                .all()
            )
            for world in worlds:
                revision = world["world_revision_id"]
                LogService.record_domain(
                    conn,
                    {
                        "outbox_id": world["outbox_id"]
                        or uuid5(revision, "legacy-world-acceptance"),
                        "topic": "world.accepted",
                        "aggregate_type": "revision",
                        "aggregate_id": revision,
                        "payload": {
                            "world_ref": {
                                "work_id": work_id,
                                "branch_id": branch_id,
                                "world_revision_id": str(revision),
                            }
                        },
                        "created_at": world["created_at"],
                    },
                )
            return {
                "model_evidence": len(models),
                "world_commits": len(worlds),
                "more_possible": max(len(models), len(worlds)) == limit,
            }

    def register_experiment(self, principal, work_id, branch_id, experiment_id, start, end):
        require(
            isinstance(start, datetime)
            and isinstance(end, datetime)
            and start.tzinfo
            and end.tzinfo
            and end > start,
            "INVALID_ARGUMENT",
            "A positive timezone-aware experiment window is required",
        )
        params = {
            "id": experiment_id,
            "work": work_id,
            "branch": branch_id,
            "start": start,
            "end": end,
        }
        with self.kernel.db.uow() as repo:
            self.kernel.authorize(repo, principal, work_id, "ADMIN", branch_id)
            repo.conn.execute(
                text("""INSERT INTO nf_metric_experiments(experiment_id,work_id,branch_id,starts_at,ends_at)
                VALUES(:id,:work,:branch,:start,:end) ON CONFLICT DO NOTHING"""),
                params,
            )
            found = repo.conn.scalar(
                text("""SELECT true FROM nf_metric_experiments WHERE experiment_id=:id
                AND work_id=:work AND branch_id=:branch AND starts_at=:start AND ends_at=:end"""),
                params,
            )
            require(found, "VIEW_DENIED", "Unavailable experiment identity")
            return {
                "experiment_id": str(experiment_id),
                "start": start.isoformat(),
                "end": end.isoformat(),
            }
