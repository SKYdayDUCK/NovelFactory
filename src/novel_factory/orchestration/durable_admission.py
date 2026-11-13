"""Durable admission in the same transaction as an authorized TaskService create.

The singleton serializes *admissions* across database clients only when enabled.
Acceptance, cancellation, execution and model accounting retain their own guards.
All nonterminal tasks, including tasks created before activation, consume capacity.
"""
from datetime import timedelta

from sqlalchemy import text

from novel_factory.domain.errors import DomainError


_POLICY_DISABLED = text("""
    SELECT true FROM nf_durable_admission_policy WHERE singleton AND NOT enabled FOR SHARE
""")
_LOCK_POLICY = text("""
    UPDATE nf_durable_admission_policy SET enabled=enabled WHERE singleton AND enabled
    RETURNING total_limit, work_limit, principal_limit, resource_limit,
        max_age_seconds, max_queue_seconds, role_resources
""")
_OCCUPANCY = text("""
    SELECT count(*) AS total,
           count(*) FILTER (WHERE t.work_id=:work) AS work,
           count(*) FILTER (WHERE t.principal_id=:principal) AS principal,
           count(*) FILTER (
             WHERE coalesce(a.resource_class, p.role_resources->>t.role_id, 'MODEL')=:resource
           ) AS resource
    FROM nf_tasks t CROSS JOIN nf_durable_admission_policy p
    LEFT JOIN nf_durable_admissions a ON a.task_id=t.task_id
    WHERE p.singleton AND t.state NOT IN ('ACCEPTED','FAILED','CANCELLED','STALE')
""")


def admit(conn, spec, principal_id, deadline):
    # Share-lock the disabled configuration so activation sees every prior
    # commit. Enabled clients update it in canonical order before counting.
    # An actual row version also fences repeatable-read callers via PostgreSQL.
    if conn.scalar(_POLICY_DISABLED):
        return None
    policy = conn.execute(_LOCK_POLICY).mappings().one_or_none()
    if policy is None:
        # A concurrent disable can change the predicate after our first read.
        # Recheck under a SHARE lock. A missing policy fails closed.
        if conn.scalar(_POLICY_DISABLED):
            return None
        raise DomainError("SERVICE_UNAVAILABLE", "Admission policy changed; retry", True, "WAIT")
    now = conn.scalar(text("SELECT clock_timestamp()"))
    if deadline <= now:
        raise DomainError("DEADLINE_EXCEEDED", "Task deadline has passed")
    if deadline - now > timedelta(seconds=policy["max_age_seconds"]):
        raise DomainError("INVALID_ARGUMENT", "Task deadline exceeds the configured maximum age")
    # Kernel already matched the task role against the caller's locked grant.
    # The resource mapping is trusted database configuration; clients supply no class.
    resource = policy["role_resources"].get(spec["role_id"], "MODEL")
    occupancy = conn.execute(_OCCUPANCY, {
        "work": spec["world_ref"]["work_id"], "principal": str(principal_id), "resource": resource,
    }).mappings().one()
    if any(occupancy[scope] >= policy[scope + "_limit"] for scope in occupancy):
        raise DomainError("BACKPRESSURE", "Durable task capacity is full; retry after one second", True, "WAIT")
    return {"resource_class": resource,
            "queue_deadline": min(deadline, now + timedelta(seconds=policy["max_queue_seconds"]))}
