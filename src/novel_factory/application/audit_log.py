"""Scoped, append-only references to original business evidence and MCP invocations."""
import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from sqlalchemy import text

from novel_factory.infrastructure.database import tables
from novel_factory.orchestration.tasks import enqueue_outbox

_FIELDS = (
    "event_id work_id branch_id event_type trace_id span_id parent_span_id causation_id "
    "task_id generation actor_id role_id principal_id world_revision_id attempt_id volume_id "
    "experiment_id model request_id outcome error_code tool_name duration_ms text_characters evidence_id event_time"
).split()
_INSERT = text(f"""INSERT INTO nf_audit_events ({','.join(_FIELDS)})
    VALUES ({','.join(':'+key for key in _FIELDS)})
    ON CONFLICT(event_id) DO NOTHING RETURNING event_id""")
_TOOL_EVENT_AND_OUTBOX = text(f"""
    WITH recorded AS ({_INSERT.text}), published AS (
        INSERT INTO nf_outbox (outbox_id,topic,aggregate_type,aggregate_id,payload,dedupe_key)
        SELECT :outbox_id,'audit.recorded','audit',event_id::text,CAST(:envelope AS jsonb),
               'audit:' || event_id::text FROM recorded
        ON CONFLICT(dedupe_key) DO NOTHING RETURNING outbox_id
    )
    SELECT EXISTS (SELECT FROM recorded), EXISTS (SELECT FROM published)
""")


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError):
        return None


def _task_scope(conn, task_id, generation=None):
    if not _uuid(task_id):
        return {}
    row = conn.execute(text("""
        SELECT t.work_id,t.branch_id,t.task_id,t.role_id,g.actor_id,t.principal_id,
               i.generation,i.spec->'world_ref'->>'world_revision_id' AS world_revision_id,
               tt.trace_id,tt.volume_id,t.task_id AS parent_span_id
        FROM nf_tasks t JOIN nf_grants g USING(grant_id)
        LEFT JOIN nf_task_inputs i ON i.task_id=t.task_id AND i.generation=coalesce(:gen,t.generation)
        LEFT JOIN nf_trace_tasks tt ON tt.task_id=t.task_id WHERE t.task_id=:task
    """), {"task": task_id, "gen": generation}).mappings().one_or_none()
    scope = dict(row) if row else {}
    if scope:
        scope["principal_id"] = _uuid(scope["principal_id"])
    return scope


def _bound_tool_scope(conn, runtime):
    """Read a grant and only its own bound task's audit scope in one snapshot."""
    binding = runtime.binding
    row = conn.execute(text("""
        SELECT coalesce(t.work_id,g.work_id) AS work_id, g.role_id AS grant_role,
               t.role_id AS task_role, g.actor_id, t.principal_id AS task_principal,
               t.branch_id,t.task_id,i.generation,
               i.spec->'world_ref'->>'world_revision_id' AS world_revision_id,
               tt.trace_id,tt.volume_id
        FROM nf_grants g
        LEFT JOIN nf_tasks t ON t.grant_id=g.grant_id AND t.task_id=:task
        LEFT JOIN nf_task_inputs i ON i.task_id=t.task_id AND i.generation=:generation
        LEFT JOIN nf_trace_tasks tt ON tt.task_id=t.task_id
        WHERE g.grant_id=:grant
    """), {"task": binding.task_id, "generation": binding.generation,
           "grant": runtime.principal.grant_id}).mappings().one()
    scope = dict(work_id=row["work_id"], role_id=row["grant_role"], actor_id=row["actor_id"],
                 principal_id=runtime.principal.principal_id)
    if row["task_id"] is not None:
        scope.update({key: row[key] for key in ("branch_id", "task_id", "generation", "world_revision_id",
                                                "trace_id", "volume_id")})
        scope.update(role_id=row["task_role"], principal_id=_uuid(row["task_principal"]),
                     parent_span_id=row["task_id"])
    return scope


def supports_harness_details(conn):
    """Read the active database catalog, including on historical upgrade rehearsals."""
    return conn.scalar(text("""
        SELECT EXISTS (SELECT FROM pg_attribute
           WHERE attrelid=to_regclass('nf_audit_events') AND attname='harness_details' AND NOT attisdropped)
    """))


def record(conn, event_type, *, event_id=None, conflict_details=None, harness_details=None, compact_outbox=False, **fields):
    """Internal transaction boundary. Only typed identifiers, counters and bounded labels persist."""
    event_id = str(event_id or uuid4())
    values = dict.fromkeys(_FIELDS)
    values.update(fields, event_type=event_type, event_id=event_id)
    values["trace_id"] = values["trace_id"] or values["task_id"] or event_id
    values["span_id"] = values["span_id"] or event_id
    values["event_time"] = values["event_time"] or datetime.now(UTC)
    statement = _INSERT
    extras = {key: value for key, value in (("conflict_details", conflict_details), ("harness_details", harness_details))
              if value is not None}
    if extras:
        columns = ','.join([*_FIELDS, *extras])
        parameters = ','.join([':' + key for key in _FIELDS] + [f'CAST(:{key} AS jsonb)' for key in extras])
        statement = text(f"""INSERT INTO nf_audit_events ({columns}) VALUES ({parameters})
            ON CONFLICT(event_id) DO NOTHING RETURNING event_id""")
        values.update({key: json.dumps(value) for key, value in extras.items()})
    envelope = {k: str(values[k]) for k in ("work_id", "branch_id", "task_id", "trace_id") if values[k]}
    if values["generation"] is not None:
        envelope["generation"] = values["generation"]
    if compact_outbox and not extras:
        inserted, published = conn.execute(_TOOL_EVENT_AND_OUTBOX, {
            **values, "outbox_id": str(uuid4()), "envelope": json.dumps(envelope)
        }).one()
        if published or not inserted:
            return event_id
        # A pre-existing Outbox key requires the normal content check. Run it
        # in a new statement so a concurrent publisher's commit is visible.
    else:
        inserted = conn.execute(statement, values).scalar_one_or_none()
    if inserted:
        enqueue_outbox(conn, tables, "audit.recorded", "audit", event_id, envelope, "audit:"+event_id)
    return event_id


class LogService:
    @staticmethod
    def handle(conn, envelope):
        """Domain subscriber. Reload source metadata from PG, never trust a transport identity."""
        row = conn.execute(text("""SELECT outbox_id,topic,aggregate_type,aggregate_id,payload,created_at
            FROM nf_outbox WHERE outbox_id=:id"""), {"id":envelope.event_id}).mappings().one()
        return LogService.record_domain(conn, row)

    @staticmethod
    def record_domain(conn, row):
        topic, kind, resource, payload = row["topic"], row["aggregate_type"], row["aggregate_id"], row["payload"]
        if topic in {"audit.recorded", "semantic.conflict.detected", "semantic.conflict.resolved"} or not _uuid(resource):
            return
        wr = payload.get("world_ref") or {}
        task = resource if kind == "task" else payload.get("task_id")
        if kind == "revision" and topic == "world.accepted":
            task = conn.scalar(text("SELECT request->>'task_id' FROM nf_commits WHERE world_revision_id=:id"), {"id":resource})
        # Rolling windows carry the causal ChangePoint artifact. Recover its exact
        # task lineage even when the original volume notification had no task_id.
        if not task and isinstance(payload.get("change_point_ref"),dict):
            parent = payload["change_point_ref"].get("artifact_revision_id")
            if _uuid(parent):
                task = conn.scalar(text("SELECT payload->>'task_id' FROM nf_artifacts WHERE artifact_revision_id=:id AND kind='CHANGE_POINT'"), {"id":parent})
        fields = _task_scope(conn, task, payload.get("generation"))
        for key in ("work_id","branch_id","world_revision_id"):
            if _uuid(wr.get(key)):
                fields[key] = wr[key]
        if not fields.get("work_id"):
            if kind == "grant":
                scope = conn.execute(text("SELECT work_id,role_id,actor_id,CASE WHEN jsonb_array_length(branch_ids)=1 AND branch_ids->>0 <> '*' THEN branch_ids->>0 END AS branch_id FROM nf_grants WHERE grant_id=:id"), {"id":resource}).mappings().first()
            elif kind == "approval":
                scope = conn.execute(text("SELECT work_id FROM nf_approvals WHERE approval_id=:id"), {"id":resource}).mappings().first()
            else:
                ref = resource if kind == "artifact" else next(
                    (v["artifact_revision_id"] for v in payload.values() if isinstance(v,dict) and _uuid(v.get("artifact_revision_id"))), None)
                scope = conn.execute(text("SELECT work_id, payload->'world_ref'->>'branch_id' AS branch_id FROM nf_artifacts WHERE artifact_revision_id=:id"),
                                     {"id":ref}).mappings().first() if ref else None
            if scope:
                fields.update(scope)
        if not fields.get("work_id") and kind == "branch":
            scope = conn.execute(text("SELECT work_id,branch_id FROM nf_branches WHERE branch_id=:id"),{"id":resource}).mappings().first()
            if scope:
                fields.update(scope)
        if not fields.get("work_id"):
            # Older outbox records without a resolvable scope stay in the authoritative
            # source. Never guess a work based on a coincidentally matching identifier.
            return
        if kind == "volume":
            fields["volume_id"] = resource
        if fields.get("task_id") and fields.get("trace_id"):
            fields["parent_span_id"] = fields["task_id"]
        state = payload.get("state") if topic == "task.changed" else None
        if payload.get("generation") == 0 and state in {"READY","BLOCKED"}:
            initial = conn.scalar(text("SELECT spec->>'state' FROM nf_task_inputs WHERE task_id=:id AND generation=0"),{"id":task})
            if state == initial:
                fields["span_id"] = task
                parent = conn.scalar(text("SELECT coalesce(parent_task_id,trace_id) FROM nf_trace_tasks WHERE task_id=:id"),{"id":task})
                fields["parent_span_id"] = parent
        if state == "RUNNING":
            created = conn.scalar(text("SELECT created_at FROM nf_task_inputs WHERE task_id=:id AND generation=:gen"),
                                  {"id":task,"gen":payload.get("generation")})
            if created:
                fields["duration_ms"] = max(0,round((row["created_at"]-created).total_seconds()*1000,3))
        if state == "READY":
            wake = conn.scalar(text("""
                SELECT extract(epoch FROM w.woken_at - greatest(w.created_at,
                     CASE WHEN w.mode='AND' THEN max(o.created_at) ELSE min(o.created_at) END))*1000
                FROM nf_workflow_waits w JOIN nf_inbox i ON i.consumer=concat('workflow:',w.task_id,':',w.generation)
                JOIN nf_outbox o ON o.outbox_id=i.message_id
                WHERE w.task_id=:id AND w.generation=:gen AND w.woken_at IS NOT NULL
                GROUP BY w.task_id,w.generation
            """), {"id":task,"gen":payload.get("generation")})
            if wake is not None:
                fields["duration_ms"] = max(0,wake)
        if topic == "workflow.wait.closed" and payload.get("reason") in {"WAIT_DEADLINE","PREREQUISITE_CLOSED"}:
            fields["error_code"] = payload["reason"]
        return record(conn, topic, event_id=row["outbox_id"], **fields, outcome=state, evidence_id=resource,
                      event_time=row["created_at"])

    @staticmethod
    def conflict(conn, phase, *, case_id, receipt_id, task_id, generation, outcome,
                 details, duration_ms=None):
        """Freeze the worker decision in its own transaction with the business effect."""
        if phase not in {"detected", "resolved"}:
            raise ValueError("Unsupported conflict phase")
        scope = _task_scope(conn, task_id, generation)
        if not scope or scope["generation"] != generation:
            raise ValueError("Conflict has no matching frozen task generation")
        # Internal writers supply evidence references. Nothing in this payload travels
        # through Redis or a character-role MCP tool. Trace access requires branch ADMIN.
        return record(
            conn, "semantic.conflict." + phase, event_id=receipt_id,
            evidence_id=case_id, outcome=outcome, duration_ms=duration_ms,
            causation_id=case_id if phase == "resolved" else None,
            conflict_details={**details, "conflict_case_id": str(case_id)}, **scope,
        )

    @staticmethod
    def model(conn, evidence_id, data):
        scope = _task_scope(conn, data["task_id"], data["generation"])
        from .harness_observations import model_error, model_metadata
        return record(conn, "model.completed", event_id=evidence_id, **scope, evidence_id=evidence_id,
                      harness_details=model_metadata(conn, data) if supports_harness_details(conn) else None,
                      attempt_id=data["attempt_id"], model=data["model_configuration"]["model"],
                      outcome=data["outcome"], error_code=model_error(data.get("reason")),
                      event_time=datetime.fromisoformat(data["recorded_at"]),
                      duration_ms=int(float(data["elapsed_seconds"])*1000))

    @staticmethod
    def model_stage(conn, task, identity, kind, evidence_id, *, model=None):
        scope = _task_scope(conn, task["task_id"], task["generation"])
        record(conn, "artifact."+kind.lower(), event_id=evidence_id, evidence_id=evidence_id,
               attempt_id=identity if kind != "AGENT_BLOCKED" else None, model=model, **scope)

    @staticmethod
    def artifact(conn, kind, evidence_id, work_id, payload, principal_id, *, text_characters=None, event_time=None):
        binding = payload.get("submission", payload)
        task_id = binding.get("task_id")
        scope = _task_scope(conn, task_id, binding.get("generation"))
        wr = payload.get("input_world_ref") or binding.get("binding", {}).get("world_ref") or {}
        scope.update(work_id=work_id, principal_id=principal_id)
        if wr.get("branch_id"):
            scope.update(branch_id=wr["branch_id"], world_revision_id=wr["world_revision_id"])
        if _uuid(payload.get("volume_id")):
            scope["volume_id"] = payload["volume_id"]
        record(conn, "artifact."+kind.lower(), event_id=evidence_id, evidence_id=evidence_id, text_characters=text_characters, event_time=event_time, **scope)

    @staticmethod
    def begin_tool(runtime, safe_tool, request_id, *, transport="mcp"):
        """Capture an admitted call before execution, including calls later denied by policy."""
        with runtime.kernel.db.uow() as repo:
            if runtime.compact_role_reads and getattr(runtime.binding, "task_id", None):
                scope = _bound_tool_scope(repo.conn, runtime)
            else:
                grant = repo.one("grants", {"grant_id":runtime.principal.grant_id})
                scope = dict(work_id=grant["work_id"], role_id=grant["role_id"], actor_id=grant["actor_id"],
                             principal_id=runtime.principal.principal_id)
                bound_task = getattr(runtime.binding, "task_id", None)
                if bound_task:
                    task = repo.one("tasks", {"task_id":bound_task, "grant_id":grant["grant_id"]})
                    if task:
                        scope.update(_task_scope(repo.conn, bound_task, runtime.binding.generation))
                elif getattr(runtime.binding, "branch_id", None):
                    branch = repo.one("branches", {"work_id":grant["work_id"], "branch_id":runtime.binding.branch_id})
                    if branch and (grant["branch_ids"] == ["*"] or branch["branch_id"] in grant["branch_ids"]):
                        scope["branch_id"] = branch["branch_id"]
            invocation = str(uuid4())
            scope.update(tool_name=safe_tool, request_id=sha256(str(request_id).encode()).hexdigest(),
                         trace_id=scope.get("trace_id") or scope.get("task_id") or invocation)
            record(repo.conn, transport + ".started", event_id=invocation, **scope,
                   compact_outbox=runtime.compact_role_reads)
            return invocation, scope

    @staticmethod
    def finish_tool(runtime, invocation, scope, error, elapsed_ms, evidence_id=None, *, transport="mcp"):
        with runtime.kernel.db.uow() as repo:
            record(repo.conn, transport + ".finished", **{**scope, "parent_span_id":invocation},
                   causation_id=invocation, outcome="ERROR" if error else "OK",
                   compact_outbox=runtime.compact_role_reads,
                   error_code=error, duration_ms=elapsed_ms, evidence_id=_uuid(evidence_id) if not error else None)

    @staticmethod
    def recent_conflicts(kernel, principal, work_id, branch_id, *, limit=30):
        """Operator-only index of case IDs and review state, without message contents."""
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Limit must be between 1 and 100")
        with kernel.db.uow(write=False) as repo:
            kernel.authorize(repo, principal, work_id, "ADMIN", branch_id)
            return [dict(row) for row in repo.conn.execute(text("""
                SELECT c.case_id,c.task_id,c.generation,c.conflict_type,c.strategy,
                    c.world_revision_id,c.created_at,r.outcome,r.receipt_id,
                    t.task_id AS review_task_id, t.state AS review_task_state
                FROM nf_conflict_cases c LEFT JOIN nf_conflict_resolutions r USING(case_id)
                LEFT JOIN nf_receipts reconciled ON reconciled.work_id=c.work_id AND reconciled.branch_id=c.branch_id
                    AND reconciled.command_type='SEMANTIC_RECONCILE' AND reconciled.idempotency_key=c.case_id::text
                LEFT JOIN nf_tasks t ON t.task_id=coalesce(r.details->>'review_task_id',
                                                         reconciled.payload->>'review_task_id')::uuid
                WHERE c.work_id=:work AND c.branch_id=:branch
                ORDER BY c.created_at DESC,c.case_id DESC LIMIT :limit
            """), {"work":work_id, "branch":branch_id, "limit":limit}).mappings()]

    @staticmethod
    def trace(kernel, principal, work_id, branch_id, trace_id):
        with kernel.db.uow(write=False) as repo:
            kernel.authorize(repo, principal, work_id, "ADMIN", branch_id)
            result = repo.conn.execute(text("""SELECT a.* FROM nf_audit_events a
                WHERE a.work_id=:work AND a.branch_id=:branch AND a.trace_id=:trace
                ORDER BY event_time,event_id"""), {"work":work_id,"branch":branch_id,"trace":trace_id})
            return [dict(row) for row in result.mappings()]
