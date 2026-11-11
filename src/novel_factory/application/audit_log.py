"""Scoped, append-only references to original business evidence and MCP invocations."""
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


def record(conn, event_type, *, event_id=None, **fields):
    """Internal transaction boundary. Only typed identifiers, counters and bounded labels persist."""
    event_id = str(event_id or uuid4())
    values = dict.fromkeys(_FIELDS)
    values.update(fields, event_type=event_type, event_id=event_id)
    values["trace_id"] = values["trace_id"] or values["task_id"] or event_id
    values["span_id"] = values["span_id"] or event_id
    values["event_time"] = values["event_time"] or datetime.now(UTC)
    inserted = conn.execute(_INSERT, values).scalar_one_or_none()
    if inserted:
        envelope = {k:str(values[k]) for k in ("work_id","branch_id","task_id","trace_id") if values[k]}
        if values["generation"] is not None:
            envelope["generation"] = values["generation"]
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
        if topic == "audit.recorded" or not _uuid(resource):
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
    def model(conn, evidence_id, data):
        scope = _task_scope(conn, data["task_id"], data["generation"])
        return record(conn, "model.completed", event_id=evidence_id, **scope, evidence_id=evidence_id,
                      attempt_id=data["attempt_id"], model=data["model_configuration"]["model"],
                      outcome=data["outcome"], event_time=datetime.fromisoformat(data["recorded_at"]),
                      duration_ms=int(float(data["elapsed_seconds"])*1000))

    @staticmethod
    def model_stage(conn, task, identity, kind, evidence_id, *, model=None):
        scope = _task_scope(conn, task["task_id"], task["generation"])
        record(conn, "artifact."+kind.lower(), event_id=evidence_id, evidence_id=evidence_id,
               attempt_id=identity if kind != "AGENT_BLOCKED" else None, model=model, **scope)

    @staticmethod
    def artifact(conn, kind, evidence_id, work_id, payload, principal_id, *, text_characters=None):
        binding = payload.get("submission", payload)
        task_id = binding.get("task_id")
        scope = _task_scope(conn, task_id, binding.get("generation"))
        wr = payload.get("input_world_ref") or binding.get("binding", {}).get("world_ref") or {}
        scope.update(work_id=work_id, principal_id=principal_id)
        if wr.get("branch_id"):
            scope.update(branch_id=wr["branch_id"], world_revision_id=wr["world_revision_id"])
        if _uuid(payload.get("volume_id")):
            scope["volume_id"] = payload["volume_id"]
        record(conn, "artifact."+kind.lower(), event_id=evidence_id, evidence_id=evidence_id, text_characters=text_characters, **scope)

    @staticmethod
    def begin_tool(runtime, safe_tool, request_id):
        """Capture an admitted call before execution, including calls later denied by policy."""
        with runtime.kernel.db.uow() as repo:
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
            record(repo.conn, "mcp.started", event_id=invocation, **scope)
            return invocation, scope

    @staticmethod
    def finish_tool(runtime, invocation, scope, error, elapsed_ms, evidence_id=None):
        with runtime.kernel.db.uow() as repo:
            record(repo.conn, "mcp.finished", **{**scope, "parent_span_id":invocation},
                   causation_id=invocation, outcome="ERROR" if error else "OK",
                   error_code=error, duration_ms=elapsed_ms, evidence_id=_uuid(evidence_id) if not error else None)

    @staticmethod
    def trace(kernel, principal, work_id, branch_id, trace_id):
        with kernel.db.uow(write=False) as repo:
            kernel.authorize(repo, principal, work_id, "ADMIN", branch_id)
            result = repo.conn.execute(text("""SELECT a.* FROM nf_audit_events a
                WHERE a.work_id=:work AND a.branch_id=:branch AND a.trace_id=:trace
                ORDER BY event_time,event_id"""), {"work":work_id,"branch":branch_id,"trace":trace_id})
            return [dict(row) for row in result.mappings()]
